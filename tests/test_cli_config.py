"""``config`` 子命令：路径与配置回显、单键读写、导出与重置。

配置默认值合并、损坏备份与原子保存本身由 ``tests/test_settings.py`` 覆盖；
本文件只验证 CLI 契约：隔离的路径、``data`` 字段形状、值的 JSON 解析与类型校验、
``--yes`` 保护与退出码。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SHOW_KEYS = {
    "exe_dir",
    "data_dir",
    "log_dir",
    "config_path",
    "portable",
    "warning",
    "settings",
}


def _config_path(cli) -> Path:
    """先跑 ``show`` 让命令准备好隔离目录，再返回本次测试的配置文件路径。"""
    return Path(cli(["config", "show"])["data"]["config_path"])


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_reports_isolated_paths(cli) -> None:
    payload = cli(["config", "show"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "config.show"

    data = payload["data"]
    assert set(data) == SHOW_KEYS
    assert Path(data["config_path"]).name == "config.json"
    assert Path(data["config_path"]).parent == Path(data["data_dir"])
    assert Path(data["log_dir"]) == Path(data["data_dir"]) / "logs"
    assert data["portable"] is False
    assert data["warning"] is None
    assert data["settings"]["app.version"] == APP_VERSION
    assert Path(data["data_dir"]).is_dir()
    # 普通命令只准备路径，不初始化文件日志（唯一的例外是 selftest）。
    assert not (Path(data["log_dir"]) / "app.log").exists()


def test_show_reports_corrupt_config_warning(cli) -> None:
    path = _config_path(cli)
    path.write_text("{not json", encoding="utf-8")

    payload = cli(["config", "show"])
    assert payload["exit_code"] == 0
    data = payload["data"]
    assert isinstance(data["warning"], str) and "损坏" in data["warning"]
    assert data["settings"]["ui.theme"] == "auto"
    # Settings.load 会把损坏文件备份到同目录，不能静默丢弃用户内容。
    assert list(path.parent.glob("config.bad-*.json"))


# ---------------------------------------------------------------------------
# get / set
# ---------------------------------------------------------------------------


def test_set_get_round_trip(cli) -> None:
    set_payload = cli(["config", "set", "--key", "ui.theme", "--value", "dark"])
    assert set_payload["exit_code"] == 0
    assert set_payload["command"] == "config.set"
    assert set_payload["data"] == {"key": "ui.theme", "value": "dark"}

    get_payload = cli(["config", "get", "--key", "ui.theme"])
    assert get_payload["exit_code"] == 0
    assert get_payload["command"] == "config.get"
    assert get_payload["data"] == {"key": "ui.theme", "value": "dark"}

    # 值确实落盘：重新加载（另一个进程视角）也能读到。
    stored = json.loads(_config_path(cli).read_text(encoding="utf-8"))
    assert stored["ui.theme"] == "dark"


def test_value_parsing_follows_default_types(cli) -> None:
    accepted = (
        ("ui.max_display_lines", "20000", 20000),
        ("ui.auto_scroll", "false", False),
        ("ui.window_geometry", "", ""),
        ("serial.encoding", "null", None),
        ("serial.encoding", "utf-8", "utf-8"),
        ("bluetooth.favorites", '["COM3"]', ["COM3"]),
        ("bluetooth.aliases", '{"COM3": "板卡"}', {"COM3": "板卡"}),
    )
    for key, raw, expected in accepted:
        payload = cli(["config", "set", "--key", key, "--value", raw])
        assert payload["exit_code"] == 0, (key, raw)
        assert payload["data"] == {"key": key, "value": expected}, (key, raw)

    # 默认值类型不是目标类型时必须拒绝，而不是悄悄换成字符串。
    rejected = (
        ("ui.max_display_lines", "abc"),  # 字符串，不是 int
        ("ui.max_display_lines", "true"),  # bool 不能当 int
        ("ui.auto_scroll", "1"),  # int 不是 bool
        ("ui.auto_scroll", "no"),  # 字符串不是 bool
        ("ui.theme", "5"),  # int 不是 str
        ("serial.encoding", "5"),  # int 不是 str/null
        ("bluetooth.favorites", "{}"),  # dict 不是 list
        ("bluetooth.aliases", "[]"),  # list 不是 dict
    )
    for key, raw in rejected:
        payload = cli(["config", "set", "--key", key, "--value", raw])
        assert payload["exit_code"] == 3, (key, raw)
        assert payload["error"]["code"] == "invalid_input", (key, raw)


def test_unknown_keys(cli) -> None:
    path = _config_path(cli)
    path.write_text(json.dumps({"custom.key": 5}), encoding="utf-8")

    # 文件里已有的未知键可以读，不会被默认值合并覆盖。
    get_payload = cli(["config", "get", "--key", "custom.key"])
    assert get_payload["exit_code"] == 0
    assert get_payload["data"] == {"key": "custom.key", "value": 5}

    # 但 set 只允许 DEFAULT_SETTINGS 中的键；文件里存在也不能写。
    set_payload = cli(["config", "set", "--key", "custom.key", "--value", "6"])
    assert set_payload["exit_code"] == 3
    assert set_payload["error"]["code"] == "not_found"

    missing = cli(["config", "get", "--key", "nope.nope"])
    assert missing["exit_code"] == 3
    assert missing["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# export / reset
# ---------------------------------------------------------------------------


def test_export_and_reset_require_yes(cli, tmp_path) -> None:
    cli(["config", "set", "--key", "ui.theme", "--value", "dark"])

    out = tmp_path / "config-export.json"
    export_payload = cli(["config", "export", "--out", str(out)])
    assert export_payload["exit_code"] == 0
    assert export_payload["command"] == "config.export"
    assert export_payload["data"] == {"path": str(out)}
    exported = json.loads(out.read_text(encoding="utf-8"))
    assert exported["ui.theme"] == "dark"
    assert exported["app.version"] == APP_VERSION

    missing_yes = cli(["config", "reset"])
    assert missing_yes["exit_code"] == 2
    assert missing_yes["error"]["code"] == "usage"

    reset_payload = cli(["config", "reset", "--yes"])
    assert reset_payload["exit_code"] == 0
    assert reset_payload["data"] == {"reset": True}
    assert cli(["config", "get", "--key", "ui.theme"])["data"]["value"] == "auto"


# ---------------------------------------------------------------------------
# 错误路径
# ---------------------------------------------------------------------------


def test_export_write_failure_is_permission_denied(cli, tmp_path) -> None:
    # 父路径是普通文件：临时文件建不出来，属于写出目标的问题。
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    payload = cli(["config", "export", "--out", str(blocked / "config.json")])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "permission_denied"


def test_missing_arguments_are_usage_errors(cli) -> None:
    for argv in (
        ["config"],
        ["config", "get"],
        ["config", "set", "--key", "ui.theme"],
        ["config", "export"],
        ["config", "no-such-action"],
    ):
        payload = cli(argv)
        assert payload["exit_code"] == 2, argv
        assert payload["error"]["code"] == "usage", argv


# ---------------------------------------------------------------------------
# 输出选项与导入开销
# ---------------------------------------------------------------------------


def test_output_options_are_accepted(cli) -> None:
    text = cli(["config", "show", "--format", "text"])
    assert text["exit_code"] == 0
    assert "ok: true" in text["raw"] and "config.show" in text["raw"]

    pretty = cli(["config", "reset", "--yes", "--pretty"])
    assert pretty["exit_code"] == 0
    assert pretty["ok"] is True and pretty["command"] == "config.reset"


def test_config_module_import_stays_light() -> None:
    """配置路径只依赖标准库：导入命令模块不能连带加载 PyQt6/Pillow/numpy。"""
    code = (
        "import sys\n"
        "import app.cli.commands.config\n"
        "print(sorted(name for name in ('PyQt6', 'PIL', 'numpy') if name in sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"

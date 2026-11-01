"""``c51`` 子命令：配置回显、寄存器/定时器/波特率计算、C 代码与 Keil 工程导出。

寄存器位域、定时器重载与波特率误差本身由 ``tests/test_c51.py`` 覆盖；本文件只验证
CLI 契约：配置来源（``--config FILE|-`` / ``--config-json`` / 缺省默认值）、
``data`` 字段形状、写出的文件与退出码。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CONFIG_KEYS = {
    "crystal_mhz",
    "double_speed",
    "ports",
    "timers",
    "serial",
    "interrupts",
    "power_mode",
    "delay_us",
}
TIMER_ITEM_KEYS = {"mode", "enabled", "gate", "time_us", "reload"}
CALC_KEYS = {"config", "registers", "timers", "baud", "warnings"}
TIMER_RESULT_KEYS = {"th", "tl", "counts", "warnings"}
BAUD_KEYS = {
    "mode",
    "baud",
    "baud_exact",
    "requested",
    "error_percent",
    "reload",
    "th",
    "tl",
    "rcap2h",
    "rcap2l",
    "warnings",
}
KEIL_FILES = {"Main.c", "M51Project.Uv2", "M51Project.Opt", "STARTUP.A51"}


def _strict_json(text: str) -> dict:
    """用拒绝 NaN/Infinity 的解析器读信封，确保 stdout 是标准 JSON。"""

    def _reject(token: str) -> None:
        raise AssertionError(f"stdout 出现非标准 JSON 常量 {token}")

    return json.loads(text, parse_constant=_reject)


# ---------------------------------------------------------------------------
# show-config
# ---------------------------------------------------------------------------


def test_show_config_returns_default_config(cli) -> None:
    payload = cli(["c51", "show-config"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "c51.show-config"

    data = payload["data"]
    assert set(data) == CONFIG_KEYS
    assert data["crystal_mhz"] == 12.0
    assert data["power_mode"] == "normal"
    assert data["ports"] == {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    assert len(data["timers"]) == 3
    assert set(data["timers"][0]) == TIMER_ITEM_KEYS
    assert data["timers"][0] == {
        "mode": 1,
        "enabled": False,
        "gate": False,
        "time_us": 1000,
        "reload": None,
    }
    assert set(data["serial"]) == {
        "mode",
        "baud",
        "ren",
        "sm2",
        "tb8",
        "rb8",
        "smod",
        "generator",
    }
    assert data["serial"]["baud"] == 9600


def test_show_config_out_writes_json(cli, tmp_path) -> None:
    out = tmp_path / "c51-config.json"
    payload = cli(["c51", "show-config", "--out", str(out)])
    assert payload["exit_code"] == 0
    assert json.loads(out.read_text(encoding="utf-8")) == payload["data"]


# ---------------------------------------------------------------------------
# calc
# ---------------------------------------------------------------------------


def test_calc_known_timer_value(cli) -> None:
    cfg = {
        "crystal_mhz": 12.0,
        "timers": [{"mode": 1, "enabled": True, "time_us": 1000}],
    }
    data = cli(["c51", "calc", "--config-json", json.dumps(cfg)])["data"]
    # 12 MHz / 12 = 1 MHz 机器周期，1000 us → 1000 个计数 → 重载 65536-1000=0xFC18。
    assert data["timers"][0]["th"] == 0xFC
    assert data["timers"][0]["tl"] == 0x18
    assert data["timers"][0]["counts"] == 1000
    assert data["registers"]["TH0"] == 0xFC
    assert data["registers"]["TL0"] == 0x18


def test_calc_data_shape(cli) -> None:
    payload = cli(["c51", "calc"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "c51.calc"

    data = payload["data"]
    assert set(data) == CALC_KEYS
    assert set(data["config"]) == CONFIG_KEYS
    assert len(data["timers"]) == 3
    for item in data["timers"]:
        assert set(item) == TIMER_RESULT_KEYS
        assert isinstance(item["warnings"], list)
    assert set(data["baud"]) == BAUD_KEYS
    assert isinstance(data["warnings"], list)
    # 默认配置：12 MHz 晶振、串口方式 1、T1 发生器 → TMOD=(方式2<<4)|方式1，TH1=0xFD。
    assert data["registers"]["TMOD"] == 0x21
    assert data["registers"]["TH1"] == 0xFD
    assert data["baud"]["requested"] == 9600
    assert data["baud"]["reload"] == 0xFD


def test_calc_reads_config_from_file_and_stdin(cli, tmp_path, monkeypatch) -> None:
    cfg = {
        "crystal_mhz": 24.0,
        "timers": [{"mode": 1, "enabled": True, "time_us": 2000}],
    }
    path = tmp_path / "c51.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")

    from_file = cli(["c51", "calc", "--config", str(path)])["data"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(cfg)))
    from_stdin = cli(["c51", "calc", "--config", "-"])["data"]

    # 24 MHz / 12 = 2 MHz 机器周期，2000 us → 4000 个计数 → 重载 65536-4000=0xF060。
    assert from_file["config"]["crystal_mhz"] == 24.0
    assert from_file["timers"][0]["counts"] == 4000
    assert from_file["timers"][0]["th"] == 0xF0
    assert from_file["timers"][0]["tl"] == 0x60
    assert from_stdin["config"] == from_file["config"]
    assert from_stdin["timers"] == from_file["timers"]


# ---------------------------------------------------------------------------
# code
# ---------------------------------------------------------------------------


def test_code_generates_c_and_out(cli, tmp_path) -> None:
    payload = cli(["c51", "code", "--config-json", "{}"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "c51.code"

    data = payload["data"]
    assert set(data) == {"text"}
    assert "TMOD" in data["text"]
    assert "#include <reg51.h>" in data["text"]

    out = tmp_path / "Main.c"
    written = cli(["c51", "code", "--config-json", "{}", "--out", str(out)])
    assert written["exit_code"] == 0
    assert out.read_text(encoding="utf-8") == written["data"]["text"]
    assert written["data"]["text"] == data["text"]


def test_code_uses_config_for_keil_include(cli) -> None:
    # 定时器 2 启用后生成代码改用 reg52.h，说明配置确实透传给核心模块。
    cfg = {"timers": [{}, {}, {"enabled": True, "time_us": 1000}]}
    data = cli(["c51", "code", "--config-json", json.dumps(cfg)])["data"]
    assert "#include <reg52.h>" in data["text"]
    assert "T2CON" in data["text"]


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_keil_project_files(cli, tmp_path) -> None:
    payload = cli(["c51", "export", "--config-json", "{}", "--out-dir", str(tmp_path)])
    assert payload["exit_code"] == 0
    assert payload["command"] == "c51.export"

    data = payload["data"]
    assert set(data) == {"files"}
    assert {Path(path).name for path in data["files"]} == KEIL_FILES
    for path in data["files"]:
        assert Path(path).is_file()
    assert "TMOD" in (tmp_path / "Main.c").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 错误路径
# ---------------------------------------------------------------------------


def test_invalid_config_json_is_invalid_input(cli, tmp_path) -> None:
    for argv in (
        ["c51", "calc"],
        ["c51", "code"],
        ["c51", "export", "--out-dir", str(tmp_path)],
    ):
        payload = cli([*argv, "--config-json", "{"])
        assert payload["exit_code"] == 3, argv
        assert payload["error"]["code"] == "invalid_input", argv

    # JSON 语法正确但不是对象：字段无处可取，同样按输入非法处理。
    payload = cli(["c51", "calc", "--config-json", "[1, 2]"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"
    assert "JSON 对象" in payload["error"]["message"]


def test_missing_config_file_is_not_found(cli, tmp_path) -> None:
    payload = cli(["c51", "calc", "--config", str(tmp_path / "nope.json")])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"


def test_non_finite_numbers_are_invalid_input(capsys) -> None:
    """``json.loads`` 默认接受 NaN/Infinity；它们会让信封不再是标准 JSON。"""
    from app.cli.main import run

    for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
        assert run(["c51", "calc", "--config-json", f'{{"crystal_mhz": {literal}}}']) == 3
        payload = _strict_json(capsys.readouterr().out)
        assert payload["ok"] is False, literal
        assert payload["error"]["code"] == "invalid_input", literal
        assert "有限" in payload["error"]["message"], literal


def test_bad_config_content_from_file_and_stdin(cli, tmp_path, monkeypatch) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    payload = cli(["c51", "calc", "--config", str(broken)])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    nan_file = tmp_path / "nan.json"
    nan_file.write_text('{"crystal_mhz": NaN}', encoding="utf-8")
    payload = cli(["c51", "calc", "--config", str(nan_file)])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    # stdin 与文件走同一条解析路径：坏 JSON 与 NaN 都必须被拦下。
    for content in ('{"crystal_mhz": NaN}', "{not json"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(content))
        payload = cli(["c51", "calc", "--config", "-"])
        assert payload["exit_code"] == 3, content
        assert payload["error"]["code"] == "invalid_input", content

    # 有限小数仍按原行为解析，拦截不能误伤正常配置。
    payload = cli(["c51", "calc", "--config-json", '{"crystal_mhz": 12.5}'])
    assert payload["exit_code"] == 0
    assert payload["data"]["config"]["crystal_mhz"] == 12.5


def test_write_failures_are_permission_denied(cli, tmp_path) -> None:
    # 父路径是普通文件：临时文件建不出来，属于写出目标的问题。
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    for argv in (
        ["c51", "show-config", "--out", str(blocked / "c51.json")],
        ["c51", "code", "--out", str(blocked / "Main.c")],
        ["c51", "export", "--out-dir", str(blocked)],
    ):
        payload = cli(argv)
        assert payload["exit_code"] == 3, argv
        assert payload["error"]["code"] == "permission_denied", argv


def test_usage_errors(cli, tmp_path) -> None:
    # 两种配置来源互斥。
    payload = cli(["c51", "calc", "--config", "x.json", "--config-json", "{}"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    # export 必须给 --out-dir；show-config 不接受配置来源；未知子命令仍是用法错误。
    payload = cli(["c51", "export"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    payload = cli(["c51", "show-config", "--config-json", "{}"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    payload = cli(["c51", "no-such-action"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# 输出选项与导入开销
# ---------------------------------------------------------------------------


def test_output_format_and_pretty(cli) -> None:
    text = cli(["c51", "show-config", "--format", "text"])
    assert text["exit_code"] == 0
    assert "ok: true" in text["raw"] and "c51.show-config" in text["raw"]

    pretty = cli(["c51", "calc", "--pretty"])
    assert pretty["exit_code"] == 0
    assert pretty["ok"] is True and pretty["command"] == "c51.calc"


def test_c51_module_import_stays_light() -> None:
    """``c51`` 核心是纯标准库：导入命令模块不能连带加载 PyQt6/Pillow/numpy。"""
    code = (
        "import sys\n"
        "import app.cli.commands.c51\n"
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

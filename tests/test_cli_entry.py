"""CLI 入口分流与 ``system`` 命令（version/selftest）。

覆盖 ``is_cli_invocation``、argparse 错误转 JSON、``help``/``--version`` 归一、
``selftest`` 的退出码映射，以及根 ``main()`` 在 CLI 参数下不走 GUI。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from app import APP_VERSION
from app.cli import CLI_GROUPS, is_cli_invocation
import app.cli.main as cli_main
from app.cli.main import run
from app.cli.output import add_output_options

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parser_with_handler(handler) -> argparse.ArgumentParser:
    """只含一个 ``boom`` 子命令的替身根解析器，用于覆盖 ``run()`` 的 handler 错误分支。

    通过 monkeypatch 替换 ``app.cli.main.build_parser``，不往生产代码里加测试专用钩子。
    """
    parser = argparse.ArgumentParser(prog="PortHelperFluent", add_help=False)
    subparsers = parser.add_subparsers()
    boom = subparsers.add_parser("boom", add_help=False)
    add_output_options(boom)
    boom.set_defaults(handler=handler, command="boom")
    return parser


def _use_handler(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        cli_main,
        "build_parser",
        lambda selftest_report=None: _parser_with_handler(handler),
    )


def test_is_cli_invocation() -> None:
    assert is_cli_invocation(["checksum"])
    assert is_cli_invocation(["--help"])
    assert is_cli_invocation(["-h"])
    assert is_cli_invocation(["--version"])
    assert is_cli_invocation(["version"])
    assert not is_cli_invocation([])
    assert not is_cli_invocation(["--selftest"])
    assert "help" in CLI_GROUPS


def test_version_returns_json(capsys) -> None:
    assert run(["version"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["data"]["version"] == APP_VERSION
    assert payload["command"] == "version"
    assert set(payload["data"]) == {"version", "python", "frozen", "executable"}
    assert isinstance(payload["data"]["frozen"], bool)
    assert isinstance(payload["elapsed_ms"], int)


def test_unknown_option_returns_usage_json(capsys) -> None:
    assert run(["--nope"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "usage" and payload["command"] == "cli"


def test_help_and_version_flag(capsys) -> None:
    assert run(["help"]) == 0
    out = capsys.readouterr().out
    assert "version" in out and "selftest" in out
    assert run(["help", "version"]) == 0
    assert "--pretty" in capsys.readouterr().out
    assert run(["--version"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "version"


def test_selftest_failure_maps_to_exit_3(capsys) -> None:
    report = {"ok": False, "errors": ["x"]}
    assert run(["selftest"], selftest_report=lambda: report) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["details"]["ok"] is False
    assert payload["error"]["code"] == "selftest_failed"
    assert payload["command"] == "selftest"


def test_selftest_uses_injected_collector(capsys) -> None:
    assert run(["selftest"], selftest_report=lambda: {"ok": True, "errors": []}) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["ok"] is True


def test_selftest_writes_report_file_when_asked(capsys, tmp_path: Path) -> None:
    report = {"ok": True, "errors": []}
    report_path = tmp_path / "reports" / "selftest.json"

    exit_code = run(["selftest", "--report", str(report_path)], selftest_report=lambda: report)

    assert exit_code == 0
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert json.loads(capsys.readouterr().out)["data"]["ok"] is True


def test_selftest_keeps_stdout_machine_readable(capsys) -> None:
    """自检会导入 qfluentwidgets 等第三方库，它们的提示不能混进 stdout 的 JSON。"""

    def noisy_collector() -> dict:
        print("📢 Tips: 第三方库的提示")
        return {"ok": True, "errors": []}

    assert run(["selftest"], selftest_report=noisy_collector) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["data"]["ok"] is True
    assert "第三方库的提示" in captured.err


def test_handler_error_becomes_internal_envelope(monkeypatch, capsys) -> None:
    def raise_error(args) -> dict:
        raise RuntimeError("模拟命令内部错误")

    _use_handler(monkeypatch, raise_error)

    assert run(["boom"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["command"] == "boom"
    assert payload["error"]["code"] == "internal"
    assert "RuntimeError" in payload["error"]["message"]
    # 只向 stderr 写一行摘要，stdout/stderr 都不出现 traceback。
    assert "Traceback" not in captured.out and "Traceback" not in captured.err
    assert len(captured.err.strip().splitlines()) == 1


def test_handler_keyboard_interrupt_maps_to_130(monkeypatch, capsys) -> None:
    def interrupt(args) -> dict:
        raise KeyboardInterrupt

    _use_handler(monkeypatch, interrupt)

    assert run(["boom"]) == 130
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "interrupted"


def test_handler_nonzero_system_exit_keeps_envelope(monkeypatch, capsys) -> None:
    """命令自己 ``sys.exit(3)`` 时 stdout 仍要有完整信封：退出码收敛成 internal/1。"""

    def exit_nonzero(args) -> dict:
        raise SystemExit(3)

    _use_handler(monkeypatch, exit_nonzero)

    assert run(["boom"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "internal"
    assert "3" in payload["error"]["message"]


def test_handler_zero_system_exit_returns_zero(monkeypatch) -> None:
    def exit_zero(args) -> dict:
        raise SystemExit(0)

    _use_handler(monkeypatch, exit_zero)

    assert run(["boom"]) == 0


def test_cli_import_does_not_import_pyqt6() -> None:
    code = (
        "import sys\n"
        "import app.cli, app.cli.main, app.cli.commands.system\n"
        "print('PyQt6' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"


def test_main_dispatches_cli_before_gui(monkeypatch, capsys) -> None:
    import main

    def fail_gui(*args, **kwargs) -> int:
        raise AssertionError("CLI 参数不应启动 GUI")

    monkeypatch.setattr(main, "_run_gui", fail_gui)

    assert main.main(["version"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "version"


def test_main_cli_selftest_uses_collect_selftest_report(monkeypatch, capsys) -> None:
    """冻结后入口是 ``__main__``，根 ``main()`` 必须注入收集器而不是让命令再去 import。"""
    import main

    report = {"ok": True, "errors": []}
    monkeypatch.setattr(main, "collect_selftest_report", lambda: report)

    assert main.main(["selftest"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == report


def test_main_keeps_legacy_selftest_path(monkeypatch, tmp_path: Path) -> None:
    """``--selftest`` 仍走原有 0/1 退出码与报告文件，不进入 CLI 分支。"""
    import main

    calls: list[Path | None] = []
    report_path = tmp_path / "selftest.json"

    def fake_run_selftest(report: Path | None = None) -> int:
        calls.append(report)
        return 1

    monkeypatch.setattr(main, "run_selftest", fake_run_selftest)

    exit_code = main.main(["--selftest", "--report", str(report_path)])

    assert exit_code == 1
    assert calls == [report_path]

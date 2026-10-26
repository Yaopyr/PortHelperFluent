import json
import os
import subprocess
import sys
import threading
from importlib.metadata import version
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _restore_global_state():
    old_excepthook = sys.excepthook
    old_thread_hook = threading.excepthook
    yield
    sys.excepthook = old_excepthook
    threading.excepthook = old_thread_hook
    from app.core.logging_setup import get_logger

    logger = get_logger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def _selftest_env() -> dict[str, str]:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def test_selftest_cli_writes_ok_report(tmp_path: Path) -> None:
    report_path = tmp_path / "reports" / "selftest.json"

    result = subprocess.run(
        [sys.executable, "main.py", "--selftest", "--report", str(report_path)],
        cwd=REPO_ROOT,
        env=_selftest_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["errors"] == []
    assert payload["python"] == sys.version.split()[0]
    assert payload["qt"]
    assert payload["qfluentwidgets"] == version("PyQt6-Fluent-Widgets")
    assert payload["version"] == "1.0.0"
    assert set(payload) == {"ok", "errors", "python", "qt", "qfluentwidgets", "version"}


def test_selftest_cli_creates_missing_report_directory(tmp_path: Path) -> None:
    report_path = tmp_path / "a" / "b" / "selftest.json"

    result = subprocess.run(
        [sys.executable, "main.py", "--selftest", "--report", str(report_path)],
        cwd=REPO_ROOT,
        env=_selftest_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert report_path.is_file()


def test_selftest_reports_import_failure(tmp_path: Path, monkeypatch) -> None:
    import main

    monkeypatch.setattr(main, "REQUIRED_MODULES", ("no_such_module_for_selftest",))
    report_path = tmp_path / "report.json"

    exit_code = main.run_selftest(report_path)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["ok"] is False
    assert any("no_such_module_for_selftest" in error for error in payload["errors"])


def test_binding_check_accepts_current_install() -> None:
    from main import check_fluent_binding

    check_fluent_binding()


def test_placeholder_window_is_a_fluent_window(qapp) -> None:
    from PyQt6.QtWidgets import QWidget
    from qfluentwidgets import FluentWindow

    from main import build_placeholder_window

    window = build_placeholder_window()

    assert isinstance(window, FluentWindow)
    assert isinstance(window, QWidget)
    assert "PortHelper" in window.windowTitle()
    window.close()


def test_create_main_window_falls_back_when_ui_module_is_missing(qapp, monkeypatch) -> None:
    from PyQt6.QtWidgets import QWidget
    from qfluentwidgets import FluentWindow

    import main
    from app.core.settings import Settings

    monkeypatch.setitem(sys.modules, "app.ui.main_window", None)

    window = main.create_main_window(Settings(REPO_ROOT / "unused-config.json"))

    assert isinstance(window, FluentWindow)
    assert isinstance(window, QWidget)
    assert window.findChild(QWidget, "placeholderView") is not None
    window.close()


def test_run_gui_reports_binding_failure_without_generic_startup_handler(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    import main
    from app.services.paths import resolve_paths

    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    dialogs: list[str] = []
    monkeypatch.setattr(main, "resolve_paths", lambda: paths)
    monkeypatch.setattr(main, "check_fluent_binding", _raise_binding_error)
    monkeypatch.setattr(main, "_show_error_dialog", dialogs.append)
    monkeypatch.setattr(main, "_handle_startup_failure", _fail_generic_handler)
    monkeypatch.setattr(main, "create_main_window", _fail_window_creation)

    exit_code = main.main([])

    assert exit_code == 1
    assert dialogs
    assert "qfluentwidgets 当前不是 PyQt6 版本" in dialogs[0]
    reports = list(paths.log_dir.glob("crash-*.txt"))
    assert len(reports) == 1
    assert "qfluentwidgets 当前不是 PyQt6 版本" in reports[0].read_text(encoding="utf-8")
    log_text = (paths.log_dir / "app.log").read_text(encoding="utf-8")
    assert "qfluentwidgets 当前不是 PyQt6 版本" in log_text


def test_handle_startup_failure_writes_chinese_report_and_notifies(
    tmp_path: Path, monkeypatch
) -> None:
    import main
    from app.core.logging_setup import setup_logging
    from app.services.paths import resolve_paths

    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    setup_logging(paths.log_dir)
    dialogs: list[str] = []
    monkeypatch.setattr(main, "resolve_paths", lambda: paths)
    monkeypatch.setattr(main, "_show_error_dialog", dialogs.append)

    main._handle_startup_failure(RuntimeError("模拟启动失败"))

    reports = list(paths.log_dir.glob("crash-*.txt"))
    assert len(reports) == 1
    content = reports[0].read_text(encoding="utf-8")
    assert "崩溃报告" in content
    assert "模拟启动失败" in content
    assert dialogs
    assert "启动失败" in dialogs[0]
    assert "模拟启动失败" in dialogs[0]
    assert "模拟启动失败" in (paths.log_dir / "app.log").read_text(encoding="utf-8")


def _raise_binding_error() -> None:
    from main import BINDING_FAILURE_HINT

    raise RuntimeError(BINDING_FAILURE_HINT)


def _fail_generic_handler(exc: BaseException) -> None:
    pytest.fail(f"绑定自检失败不应走通用启动失败处理：{exc}")


def _fail_window_creation(settings) -> None:
    pytest.fail("绑定自检失败后不应创建主窗口")

import json
import os
import stat
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


def _raise_window_error(settings) -> None:
    raise RuntimeError("模拟窗口创建失败")


class _StubWindow:
    """替代真实 Fluent 窗口，避免测试里创建完整界面。"""

    def __init__(self) -> None:
        self.shown = False

    def show(self) -> None:
        self.shown = True


def _deny_mkdir(monkeypatch, denied: Path) -> None:
    original_mkdir = Path.mkdir

    def fake_mkdir(self: Path, *args, **kwargs) -> None:
        if self == denied:
            raise PermissionError(13, "拒绝访问", str(self))
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)


def _portable_layout(tmp_path: Path):
    from app.services.paths import resolve_paths

    exe_dir = tmp_path / "PortHelper"
    exe_dir.mkdir()
    (exe_dir / "portable.flag").write_text("1", encoding="utf-8")
    appdata = tmp_path / "appdata"
    return resolve_paths(exe_path=exe_dir, appdata=appdata), appdata


def test_startup_failure_uses_effective_appdata_fallback_paths(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    """便携目录不可写时，启动失败的崩溃报告必须落在已回退的 AppData 日志目录。"""
    import main
    from app.services.paths import APP_NAME

    portable_paths, appdata = _portable_layout(tmp_path)
    assert portable_paths.portable is True
    fallback_logs = appdata / APP_NAME / "logs"
    dialogs: list[str] = []
    warnings: list[str] = []

    _deny_mkdir(monkeypatch, portable_paths.data_dir)
    # 便携回退使用默认的 %APPDATA%，这里指向 tmp_path 以免污染真实用户目录。
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(main, "resolve_paths", lambda: portable_paths)
    monkeypatch.setattr(main, "create_main_window", _raise_window_error)
    monkeypatch.setattr(main, "_show_error_dialog", dialogs.append)
    monkeypatch.setattr(main, "_show_warning_dialog", warnings.append)

    exit_code = main.main([])

    assert exit_code == 1
    reports = list(fallback_logs.glob("crash-*.txt"))
    assert len(reports) == 1
    assert "模拟窗口创建失败" in reports[0].read_text(encoding="utf-8")
    # 便携目录根本没建起来，报告不可能落在那里。
    assert not portable_paths.log_dir.exists()
    log_text = (fallback_logs / "app.log").read_text(encoding="utf-8")
    assert "已回退到 AppData" in log_text
    assert "启动失败" in log_text
    assert dialogs and "启动失败" in dialogs[0]
    assert str(fallback_logs) in dialogs[0]
    # 窗口创建失败在对话框之前，提示框不应再弹出。
    assert warnings == []


def test_logging_setup_failure_keeps_effective_appdata_fallback_paths(
    tmp_path: Path, monkeypatch
) -> None:
    """回退到 AppData 后 app.log 只读导致 setup_logging 失败时，仍用生效路径写崩溃报告。"""
    import main
    from app.services.paths import APP_NAME, AppPaths

    portable_paths, appdata = _portable_layout(tmp_path)
    fallback_data_dir = appdata / APP_NAME
    fallback_logs = fallback_data_dir / "logs"
    fallback_logs.mkdir(parents=True)
    read_only_log = fallback_logs / "app.log"
    read_only_log.write_text("已有日志\n", encoding="utf-8")
    # Windows 上置为只读后 RotatingFileHandler 打开 app.log 会抛 PermissionError，
    # 目录本身仍可写，崩溃报告应照常落到这里。
    os.chmod(read_only_log, stat.S_IREAD)

    handled: list[BaseException] = []
    effective_paths: list[AppPaths | None] = []
    dialogs: list[str] = []
    original_handler = main._handle_startup_failure

    def record(exc: BaseException, paths: AppPaths | None = None) -> None:
        handled.append(exc)
        effective_paths.append(paths)
        original_handler(exc, paths)

    _deny_mkdir(monkeypatch, portable_paths.data_dir)
    # 便携回退使用默认的 %APPDATA%，这里指向 tmp_path 以免污染真实用户目录。
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(main, "resolve_paths", lambda: portable_paths)
    monkeypatch.setattr(main, "_handle_startup_failure", record)
    monkeypatch.setattr(main, "_show_error_dialog", dialogs.append)

    try:
        exit_code = main.main([])
    finally:
        os.chmod(read_only_log, stat.S_IWRITE)

    expected_paths = AppPaths(
        exe_dir=portable_paths.exe_dir,
        data_dir=fallback_data_dir,
        config_file=fallback_data_dir / "config.json",
        log_dir=fallback_logs,
        portable=False,
    )
    assert exit_code == 1
    assert len(handled) == 1
    assert isinstance(handled[0], PermissionError)
    # 关键断言：启动失败处理拿到的是已回退的生效路径，而不是重新解析出的便携路径。
    assert effective_paths == [expected_paths]
    reports = list(fallback_logs.glob("crash-*.txt"))
    assert len(reports) == 1
    assert read_only_log.name in reports[0].read_text(encoding="utf-8")
    assert dialogs and "启动失败" in dialogs[0]
    assert str(fallback_logs) in dialogs[0]
    # 便携目录创建被拒绝，崩溃报告不可能落在那里，也不存在第二份报告。
    assert not portable_paths.data_dir.exists()
    assert not portable_paths.log_dir.exists()
    assert list(tmp_path.rglob("crash-*.txt")) == reports


def test_main_routes_gui_error_to_startup_handler(tmp_path: Path, monkeypatch) -> None:
    """main() 的真实分发路径：_run_gui 抛错时交给启动失败处理并返回 1。"""
    import main
    from app.services.paths import resolve_paths

    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    handled: list[BaseException] = []
    dialogs: list[str] = []
    original_handler = main._handle_startup_failure

    def record(exc: BaseException, *args, **kwargs) -> None:
        handled.append(exc)
        original_handler(exc, *args, **kwargs)

    def raise_gui_error() -> int:
        raise RuntimeError("模拟 GUI 启动异常")

    monkeypatch.setattr(main, "resolve_paths", lambda: paths)
    monkeypatch.setattr(main, "_run_gui", raise_gui_error)
    monkeypatch.setattr(main, "_handle_startup_failure", record)
    monkeypatch.setattr(main, "_show_error_dialog", dialogs.append)

    report_path = tmp_path / "selftest-report.json"
    exit_code = main.main(["--report", str(report_path)])

    assert exit_code == 1
    assert len(handled) == 1
    assert "模拟 GUI 启动异常" in str(handled[0])
    reports = list(paths.log_dir.glob("crash-*.txt"))
    assert len(reports) == 1
    assert "模拟 GUI 启动异常" in reports[0].read_text(encoding="utf-8")
    assert not report_path.exists()  # 非 --selftest 分支不写自检报告
    assert dialogs and "启动失败" in dialogs[0]


def test_run_gui_warns_about_portable_fallback_and_keeps_running(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    """便携回退只提示不中断：警告写入日志并调用提示框回调，随后正常启动。"""
    import main
    from app.services.paths import APP_NAME
    from PyQt6.QtWidgets import QApplication

    portable_paths, appdata = _portable_layout(tmp_path)
    fallback_logs = appdata / APP_NAME / "logs"
    stub = _StubWindow()
    warnings: list[str] = []
    errors: list[str] = []

    _deny_mkdir(monkeypatch, portable_paths.data_dir)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(main, "resolve_paths", lambda: portable_paths)
    monkeypatch.setattr(main, "create_main_window", lambda settings: stub)
    monkeypatch.setattr(main, "_show_warning_dialog", warnings.append)
    monkeypatch.setattr(main, "_show_error_dialog", errors.append)
    # 不进入真实事件循环，只验证启动流程本身。
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)

    exit_code = main._run_gui()

    assert exit_code == 0
    assert stub.shown is True
    assert errors == []
    assert len(warnings) == 1
    assert "已回退到 AppData" in warnings[0]
    log_text = (fallback_logs / "app.log").read_text(encoding="utf-8")
    assert "已回退到 AppData" in log_text
    assert list(fallback_logs.glob("crash-*.txt")) == []


def test_thread_exception_hook_queues_dialog_to_gui_thread(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    """threading.excepthook 只能发出信号，由 GUI 线程执行提示回调。"""
    import main
    from app.core.logging_setup import install_exception_hooks, setup_logging
    from app.services.paths import resolve_paths

    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    setup_logging(paths.log_dir)
    calls: list[tuple[str, str]] = []

    def record_dialog(message: str) -> None:
        calls.append((message, threading.current_thread().name))

    monkeypatch.setattr(main, "_show_error_dialog", record_dialog)
    on_error = main._create_error_callback()
    assert on_error is not None
    assert main._ERROR_BRIDGE is not None
    install_exception_hooks(paths.log_dir, on_error=on_error)

    args = threading.ExceptHookArgs(
        (RuntimeError, RuntimeError("子线程崩溃"), None, threading.current_thread())
    )
    worker = threading.Thread(target=threading.excepthook, args=(args,), name="porthelper-worker")
    worker.start()
    worker.join()

    reports = list(paths.log_dir.glob("crash-*.txt"))
    assert len(reports) == 1
    assert "子线程崩溃" in reports[0].read_text(encoding="utf-8")
    # 工作线程里只发信号，不直接创建控件。
    assert calls == []

    qapp.processEvents()

    assert len(calls) == 1
    assert calls[0][1] == threading.main_thread().name
    assert "子线程崩溃" in calls[0][0]

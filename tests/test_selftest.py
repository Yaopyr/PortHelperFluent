import json
import os
import site
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


def _selftest_env(appdata: Path, **overrides: str) -> dict[str, str]:
    """子进程环境：APPDATA 指向临时目录，绝不写入真实用户目录。

    APPDATA 同时决定 Python 用户级 site-packages 的位置；本机依赖装在用户目录，
    因此显式保留 `PYTHONUSERBASE`，否则子进程会“缺依赖”。
    """
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["APPDATA"] = str(appdata)
    env["PYTHONUSERBASE"] = site.getuserbase()
    env.pop("PORTHELPER_NO_DIALOGS", None)
    env.update(overrides)
    return env


def test_selftest_cli_writes_ok_report(tmp_path: Path) -> None:
    report_path = tmp_path / "reports" / "selftest.json"
    appdata = tmp_path / "appdata"

    result = subprocess.run(
        [sys.executable, "main.py", "--selftest", "--report", str(report_path)],
        cwd=REPO_ROOT,
        env=_selftest_env(appdata),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
    # 自检只写临时 APPDATA，并且真的初始化过日志。
    assert (appdata / "PortHelperFluent" / "logs" / "app.log").is_file()


def test_selftest_cli_creates_missing_report_directory(tmp_path: Path) -> None:
    report_path = tmp_path / "a" / "b" / "selftest.json"
    appdata = tmp_path / "appdata"

    result = subprocess.run(
        [sys.executable, "main.py", "--selftest", "--report", str(report_path)],
        cwd=REPO_ROOT,
        env=_selftest_env(appdata),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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


def test_selftest_fails_on_read_only_app_log(tmp_path: Path, monkeypatch) -> None:
    """只读 app.log 只让临时探针通过，自检必须自己发现日志文件不可写。"""
    import main
    from app.services.paths import resolve_paths

    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    app_log = paths.log_dir / "app.log"
    _make_read_only(app_log, "旧日志内容\n")
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(main, "resolve_paths", lambda: paths)
    # 目录/文件探针本身已由 paths 测试覆盖；这里隔离它，验证自检自身的日志探测。
    monkeypatch.setattr(main, "ensure_writable_paths", lambda p: (p, None))

    try:
        exit_code = main.run_selftest(report_path)
    finally:
        _restore_writable(app_log)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["ok"] is False
    assert any("日志文件不可写" in error and "app.log" in error for error in payload["errors"])
    # 自检不能改写或清空已有日志。
    assert app_log.read_text(encoding="utf-8") == "旧日志内容\n"


def test_selftest_fails_on_read_only_config(tmp_path: Path, monkeypatch) -> None:
    import main
    from app.services.paths import resolve_paths

    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    paths.data_dir.mkdir(parents=True)
    paths.log_dir.mkdir()
    config = paths.config_file
    _make_read_only(config, '{"ui.theme": "dark"}\n')
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(main, "resolve_paths", lambda: paths)
    monkeypatch.setattr(main, "ensure_writable_paths", lambda p: (p, None))

    try:
        exit_code = main.run_selftest(report_path)
    finally:
        _restore_writable(config)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["ok"] is False
    assert any("配置文件不可写" in error for error in payload["errors"])
    assert config.read_text(encoding="utf-8") == '{"ui.theme": "dark"}\n'


def test_selftest_keeps_writable_files_and_leaves_no_probe_leftovers(
    tmp_path: Path, monkeypatch
) -> None:
    """可写的 app.log/config.json 在自检前后内容与 mtime 不变，且不留探测残留。"""
    import main
    from app.services.paths import resolve_paths

    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    paths.data_dir.mkdir(parents=True)
    paths.log_dir.mkdir()
    app_log = paths.log_dir / "app.log"
    config = paths.config_file
    app_log.write_text("旧日志内容\n", encoding="utf-8")
    config.write_text('{"ui.theme": "dark"}\n', encoding="utf-8")
    untouched = {
        app_log: ("旧日志内容\n", app_log.stat().st_mtime_ns),
        config: ('{"ui.theme": "dark"}\n', config.stat().st_mtime_ns),
    }
    report_path = tmp_path / "reports" / "selftest.json"
    monkeypatch.setattr(main, "resolve_paths", lambda: paths)
    monkeypatch.setattr(main, "ensure_writable_paths", lambda p: (p, None))

    exit_code = main.run_selftest(report_path)

    assert exit_code == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["ok"] is True
    for path, (text, mtime_ns) in untouched.items():
        assert path.read_text(encoding="utf-8") == text
        assert path.stat().st_mtime_ns == mtime_ns
    # 目录探针与报告原子写入都不能留下 .probe/.tmp 文件。
    leftovers = [
        path
        for path in list(paths.data_dir.rglob("*")) + list(tmp_path.rglob("*.tmp"))
        if path.is_file() and path.suffix in (".probe", ".tmp")
    ]
    assert leftovers == []


def test_selftest_cli_detects_read_only_app_log(tmp_path: Path) -> None:
    """子进程完整链路：真实 APPDATA 指向临时目录，只读 app.log 必须让自检失败。"""
    appdata = tmp_path / "appdata"
    app_log = appdata / "PortHelperFluent" / "logs" / "app.log"
    _make_read_only(app_log, "旧日志内容\n")
    report_path = tmp_path / "report.json"

    try:
        result = subprocess.run(
            [sys.executable, "main.py", "--selftest", "--report", str(report_path)],
            cwd=REPO_ROOT,
            env=_selftest_env(appdata),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    finally:
        _restore_writable(app_log)

    assert result.returncode == 1, result.stderr or result.stdout
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert any("不可写" in error for error in payload["errors"])
    assert "Traceback" not in result.stderr
    assert app_log.read_text(encoding="utf-8") == "旧日志内容\n"


def test_selftest_report_write_failure_returns_one_without_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """报告写不出去时退出码为 1，且不能把异常抛给调用方（窗口化 exe 无 traceback）。"""
    import main

    def fail_write(*args, **kwargs) -> None:
        raise PermissionError(13, "拒绝访问", str(tmp_path / "locked" / "report.json"))

    monkeypatch.setattr(main, "write_text_atomic", fail_write)

    exit_code = main.run_selftest(tmp_path / "report.json")

    assert exit_code == 1
    assert "写入自检报告失败" in capsys.readouterr().err


def test_selftest_reports_broken_main_window(tmp_path: Path, monkeypatch) -> None:
    """主窗口模块损坏必须让自检失败，而不是回退到占位界面。"""
    import main

    monkeypatch.setitem(sys.modules, "app.ui.main_window", None)
    report_path = tmp_path / "report.json"

    exit_code = main.run_selftest(report_path)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["ok"] is False
    assert any("主窗口导入失败" in error for error in payload["errors"])


def test_binding_check_accepts_current_install() -> None:
    from main import check_fluent_binding

    check_fluent_binding()


def test_create_main_window_builds_real_fluent_window(qapp, tmp_path: Path) -> None:
    from qfluentwidgets import FluentWindow

    import main
    from app.core.settings import Settings
    from app.services.paths import resolve_paths

    settings = Settings(tmp_path / "config.json")
    settings.load()
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")

    window = main.create_main_window(settings, paths)

    assert isinstance(window, FluentWindow)
    assert window.windowTitle() == "PortHelper Fluent 1.0.0"
    assert window.smoke_test() is True
    window.close()


def test_create_main_window_fails_loudly_when_ui_module_is_missing(
    qapp, monkeypatch, tmp_path: Path
) -> None:
    import main
    from app.core.settings import Settings
    from app.services.paths import resolve_paths

    monkeypatch.setitem(sys.modules, "app.ui.main_window", None)
    settings = Settings(tmp_path / "config.json")
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")

    with pytest.raises(ModuleNotFoundError):
        main.create_main_window(settings, paths)


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
    tmp_path: Path, monkeypatch, qapp
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


def _fail_window_creation(settings, paths) -> None:
    pytest.fail("绑定自检失败后不应创建主窗口")


def _raise_window_error(settings, paths) -> None:
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


def _make_read_only(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, stat.S_IREAD)


def _restore_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


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
    tmp_path: Path, monkeypatch, qapp
) -> None:
    """回退到 AppData 后日志初始化失败时，仍用生效路径写崩溃报告。"""
    import main
    from app.services.paths import APP_NAME, AppPaths

    portable_paths, appdata = _portable_layout(tmp_path)
    fallback_data_dir = appdata / APP_NAME
    fallback_logs = fallback_data_dir / "logs"

    def fail_setup(log_dir: Path) -> None:
        raise PermissionError(13, "拒绝访问", str(Path(log_dir) / "app.log"))

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
    monkeypatch.setattr(main, "setup_logging", fail_setup)
    monkeypatch.setattr(main, "_handle_startup_failure", record)
    monkeypatch.setattr(main, "_show_error_dialog", dialogs.append)

    exit_code = main.main([])

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
    assert "app.log" in reports[0].read_text(encoding="utf-8")
    assert dialogs and "启动失败" in dialogs[0]
    assert str(fallback_logs) in dialogs[0]
    # 权限类错误要给出只读/占用的具体指引，而不是只有“检查依赖”。
    assert "只读" in dialogs[0]
    assert "portable.flag" in dialogs[0]
    # 便携目录创建被拒绝，崩溃报告不可能落在那里，也不存在第二份报告。
    assert not portable_paths.data_dir.exists()
    assert not portable_paths.log_dir.exists()
    assert list(tmp_path.rglob("crash-*.txt")) == reports


def test_main_routes_gui_error_to_startup_handler(tmp_path: Path, monkeypatch, qapp) -> None:
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

    def raise_gui_error(*args, **kwargs) -> int:
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
    monkeypatch.setattr(main, "create_main_window", lambda settings, paths: stub)
    monkeypatch.setattr(main, "_show_warning_dialog", warnings.append)
    monkeypatch.setattr(main, "_show_error_dialog", errors.append)
    # 不进入真实事件循环，只验证启动流程本身。
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)

    from app.ui.qt_utils import uninstall_qt_message_handler

    try:
        exit_code = main._run_gui()
    finally:
        # _start_gui 会安装全局 Qt 消息处理器，测试结束后交还默认处理。
        uninstall_qt_message_handler()

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


def test_startup_failure_without_qapplication_uses_native_dialog(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    """没有 QApplication 时（窗口化 exe 最早期失败）必须退回系统对话框，不能静默返回。"""
    import main
    from PyQt6.QtWidgets import QApplication

    from app.services.paths import resolve_paths

    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    native: list[str] = []
    monkeypatch.delenv(main.NO_DIALOGS_ENV, raising=False)
    # 真实 _show_error_dialog 会因为没有 QApplication 抛错，从而触发系统对话框兜底。
    monkeypatch.setattr(QApplication, "instance", staticmethod(lambda: None))
    monkeypatch.setattr(main, "_show_native_error", native.append)

    main._handle_startup_failure(PermissionError(13, "拒绝访问", "app.log"), paths)

    assert len(native) == 1, "Qt 不可用时必须退回系统对话框"
    assert "程序启动失败" in native[0]
    assert "拒绝访问" in native[0]
    assert "只读" in native[0]
    reports = list(paths.log_dir.glob("crash-*.txt"))
    assert len(reports) == 1
    assert "拒绝访问" in reports[0].read_text(encoding="utf-8")


def test_startup_failure_falls_back_to_native_when_qt_dialog_fails(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    """Qt 对话框本身失败时同样要退回系统对话框，不能静默吞掉错误。"""
    import main

    from app.services.paths import resolve_paths

    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    native: list[str] = []
    monkeypatch.delenv(main.NO_DIALOGS_ENV, raising=False)

    def broken_dialog(message: str) -> None:
        raise RuntimeError("QMessageBox 创建失败")

    monkeypatch.setattr(main, "_show_error_dialog", broken_dialog)
    monkeypatch.setattr(main, "_show_native_error", native.append)

    main._handle_startup_failure(RuntimeError("模拟启动失败"), paths)

    assert len(native) == 1
    assert "模拟启动失败" in native[0]


class _FakeUser32:
    """替代 ctypes.WinDLL("user32")，记录 MessageBoxW 的调用与返回值。"""

    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[object, str, str, int]] = []

    def MessageBoxW(self, hwnd: object, text: str, caption: str, flags: int) -> int:
        self.calls.append((hwnd, text, caption, flags))
        return self.result


def test_native_error_logs_win32_error_when_messagebox_returns_zero(monkeypatch) -> None:
    """MessageBoxW 返回 0 表示对话框没显示：记录 Win32 错误码，但不抛出异常。"""
    import ctypes

    import main

    messages: list[str] = []
    fake_user32 = _FakeUser32(0)
    monkeypatch.setattr(main, "_log_safely", lambda level, message: messages.append(message))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: fake_user32)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "FormatError", lambda code: "拒绝访问", raising=False)

    main._show_native_error("模拟系统提示")

    assert fake_user32.calls and fake_user32.calls[0][1] == "模拟系统提示"
    assert len(messages) == 1
    assert "Win32" in messages[0]
    assert "5" in messages[0]
    assert "拒绝访问" in messages[0]
    assert "模拟系统提示" in messages[0]


def test_native_error_is_silent_when_messagebox_succeeds(monkeypatch) -> None:
    import ctypes

    import main

    messages: list[str] = []
    fake_user32 = _FakeUser32(1)
    monkeypatch.setattr(main, "_log_safely", lambda level, message: messages.append(message))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: fake_user32)

    main._show_native_error("正常显示")

    assert fake_user32.calls and fake_user32.calls[0][2] == main.APP_TITLE
    assert messages == []


def test_no_dialogs_env_suppresses_runtime_error_and_warning_dialogs(
    monkeypatch, qapp
) -> None:
    """PORTHELPER_NO_DIALOGS 也约束运行期提示：异常桥接不得弹框阻塞 GUI 线程。"""
    import main
    from PyQt6.QtWidgets import QMessageBox

    shown: list[str] = []
    monkeypatch.setenv(main.NO_DIALOGS_ENV, "1")
    monkeypatch.setattr(
        QMessageBox, "critical", staticmethod(lambda *args, **kwargs: shown.append("critical"))
    )
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args, **kwargs: shown.append("warning"))
    )

    on_error = main._create_error_callback()
    assert on_error is not None
    on_error("模拟运行时异常")
    qapp.processEvents()
    main._show_error_dialog("模拟错误提示")
    main._show_warning_dialog("模拟提示")

    assert shown == []

    # 正对照：去掉环境变量后提示框确实会被调用，证明上面的断言不是补丁失效。
    monkeypatch.delenv(main.NO_DIALOGS_ENV)
    main._show_warning_dialog("提示框可用")
    assert shown == ["warning"]


def test_startup_failure_respects_no_dialogs_env(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    """PORTHELPER_NO_DIALOGS=1 时只写日志，绝不弹模态框（无界面子进程/测试用）。"""
    import main
    from PyQt6.QtWidgets import QApplication

    from app.services.paths import resolve_paths

    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    native: list[str] = []
    qt_dialogs: list[str] = []
    monkeypatch.setenv(main.NO_DIALOGS_ENV, "1")
    monkeypatch.setattr(QApplication, "instance", staticmethod(lambda: None))
    monkeypatch.setattr(main, "_show_native_error", native.append)
    monkeypatch.setattr(main, "_show_error_dialog", qt_dialogs.append)

    main._handle_startup_failure(RuntimeError("无界面环境"), paths)

    assert native == []
    assert qt_dialogs == []
    reports = list(paths.log_dir.glob("crash-*.txt"))
    assert len(reports) == 1


def test_startup_failure_subprocess_reports_and_skips_modal(tmp_path: Path) -> None:
    """子进程：只读 app.log + 禁用对话框时，退出码 1、留下崩溃报告且不阻塞。"""
    appdata = tmp_path / "appdata"
    logs = appdata / "PortHelperFluent" / "logs"
    app_log = logs / "app.log"
    _make_read_only(app_log, "旧日志内容\n")

    try:
        # 若仍尝试弹模态框，进程会一直挂起直到 timeout 报错。
        result = subprocess.run(
            [sys.executable, "main.py"],
            cwd=REPO_ROOT,
            env=_selftest_env(appdata, PORTHELPER_NO_DIALOGS="1"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    finally:
        _restore_writable(app_log)

    assert result.returncode == 1, result.stderr or result.stdout
    reports = list(logs.glob("crash-*.txt"))
    assert len(reports) == 1
    content = reports[0].read_text(encoding="utf-8")
    assert "不可写" in content
    assert "app.log" in content
    assert app_log.read_text(encoding="utf-8") == "旧日志内容\n"
    # 启动失败被正常处理：进程没有以未捕获异常退出（没有 "Exception ignored" 之类）。
    assert "Exception ignored" not in result.stderr


def test_smoke_test_cli_exits_zero_without_dialogs(tmp_path: Path) -> None:
    """真实子进程冒烟：隔离 APPDATA、禁用对话框，1.5 秒后自动退出且退出码 0。"""
    appdata = tmp_path / "appdata"

    result = subprocess.run(
        [sys.executable, "main.py", "--smoke-test"],
        cwd=REPO_ROOT,
        env=_selftest_env(appdata, PORTHELPER_NO_DIALOGS="1"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    log_path = appdata / "PortHelperFluent" / "logs" / "app.log"
    assert log_path.is_file()
    log_text = log_path.read_text(encoding="utf-8")
    assert "启动冒烟测试完成" in log_text
    assert "启动冒烟检查失败" not in log_text


class _SmokeWindow:
    """替代真实主窗口：只记录 show/smoke_test 调用并返回预设结果。"""

    def __init__(self, smoke_result: bool = True) -> None:
        self.smoke_result = smoke_result
        self.shown = False
        self.smoke_calls = 0

    def smoke_test(self) -> bool:
        self.smoke_calls += 1
        return self.smoke_result

    def show(self) -> None:
        self.shown = True


def _run_smoke_start_gui(tmp_path: Path, monkeypatch, qapp, stub: _SmokeWindow, exec_code: int) -> int:
    import main
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from app.core.logging_setup import setup_logging
    from app.services.paths import resolve_paths

    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    logger = setup_logging(paths.log_dir)
    monkeypatch.setattr(main, "create_main_window", lambda settings, paths: stub)
    monkeypatch.setattr(QApplication, "exec", lambda self: exec_code)
    # 不把 1.5 秒后的 quit 定时器留给后续测试。
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda *args, **kwargs: None))
    from app.ui.qt_utils import uninstall_qt_message_handler

    try:
        return main._start_gui(paths, None, logger, smoke_test=True)
    finally:
        uninstall_qt_message_handler()


def test_start_gui_smoke_check_failure_returns_nonzero(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    import main

    stub = _SmokeWindow(smoke_result=False)

    exit_code = _run_smoke_start_gui(tmp_path, monkeypatch, qapp, stub, exec_code=0)

    assert exit_code == 2
    assert stub.smoke_calls == 1
    assert stub.shown is False
    log_text = (tmp_path / "appdata" / "PortHelperFluent" / "logs" / "app.log").read_text(
        encoding="utf-8"
    )
    assert "启动冒烟检查失败" in log_text


def test_start_gui_smoke_returns_event_loop_failure_code(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    stub = _SmokeWindow(smoke_result=True)

    exit_code = _run_smoke_start_gui(tmp_path, monkeypatch, qapp, stub, exec_code=3)

    assert exit_code == 3
    assert stub.smoke_calls == 1
    assert stub.shown is True

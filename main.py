"""PortHelper Fluent 入口：启动自检、真实 Fluent 主窗口与启动冒烟测试。

Qt 相关导入都放在函数内部，确保依赖缺失时仍能完成自检并写出报告。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import logging
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from app.core.logging_setup import (
    LOG_FILE_NAME,
    get_logger,
    install_exception_hooks,
    setup_logging,
    write_crash_report,
)
from app.core.settings import DEFAULT_SETTINGS, Settings
from app.services.exports import write_text_atomic
from app.services.paths import AppPaths, ensure_writable_paths, probe_write, resolve_paths

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QWidget

APP_TITLE = "PortHelper Fluent"
APP_VERSION = str(DEFAULT_SETTINGS["app.version"])
BINDING_FAILURE_HINT = "qfluentwidgets 当前不是 PyQt6 版本"
# 置为 1 时只写日志、不弹任何模态框，供无界面测试/子进程使用。
NO_DIALOGS_ENV = "PORTHELPER_NO_DIALOGS"
REQUIRED_MODULES = (
    "PyQt6",
    "qfluentwidgets",
    "serial",
    "psutil",
    "PIL",
    "numpy",
    "hid",
    "qrcode",
)

# 异常提示桥接与类型都延迟创建，保证缺少 PyQt6 时 main 仍可导入（--selftest 依赖）。
_ERROR_BRIDGE: object | None = None
_ERROR_BRIDGE_TYPE: type | None = None


def check_fluent_binding() -> None:
    """确认 qfluentwidgets 绑定的是 PyQt6，避免被 PyQt5 版本覆盖后白屏。"""
    from PyQt6.QtWidgets import QWidget
    from qfluentwidgets import FluentWindow

    if not issubclass(FluentWindow, QWidget):
        raise RuntimeError(BINDING_FAILURE_HINT)


def create_main_window(settings: Settings, paths: AppPaths) -> "QWidget":
    """创建真实主窗口；导入或构造失败直接上抛，由启动失败处理明确报告。

    Task 2 之后不再有「界面构建中」的占位回退：主窗口缺失属于安装损坏，
    静默降级只会让用户以为功能没做。
    """
    from app.ui.main_window import MainWindow

    return MainWindow(settings, paths)


def run_selftest(report_path: Path | None = None) -> int:
    """检查依赖、Qt 绑定与数据目录/日志/配置可写性，并写出 JSON 报告。返回进程退出码。

    自检必须在“程序其实起不来”时也报错：除了新建临时探针，还要真正初始化日志并
    打开已有的 `app.log`、`config.json` 验证文件级写入权限（只读文件同样会让正式
    启动失败）。任何一步出现异常都收敛成 `errors`，报告始终尝试写出，绝不把
    traceback 抛给窗口化 exe。
    """
    errors: list[str] = []
    versions = {"qt": "", "qfluentwidgets": ""}
    try:
        _collect_selftest_errors(errors, versions)
    except Exception as exc:  # 自检自身出错也必须给出可读报告
        errors.append(f"自检过程中出现未预期的错误：{exc!r}")

    report = {
        "ok": not errors,
        "errors": errors,
        "python": platform.python_version(),
        "qt": versions["qt"],
        "qfluentwidgets": versions["qfluentwidgets"],
        "version": APP_VERSION,
    }

    exit_code = 0 if not errors else 1
    if report_path is not None:
        try:
            write_text_atomic(Path(report_path), json.dumps(report, ensure_ascii=False, indent=2))
        except Exception as exc:
            # 报告写不出去本身就是自检失败；只记日志，不抛出 traceback。
            exit_code = 1
            _report_selftest_write_failure(exc)
    return exit_code


def _collect_selftest_errors(errors: list[str], versions: dict[str, str]) -> None:
    for module_name in REQUIRED_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # 自检需要收集全部问题，不能遇到第一个就退出
            errors.append(f"依赖 {module_name} 导入失败：{exc}")

    try:
        check_fluent_binding()
    except Exception as exc:
        errors.append(f"Qt 绑定自检失败：{exc}")

    _check_main_window_import(errors)

    paths: AppPaths | None = None
    try:
        paths, portable_warning = ensure_writable_paths(resolve_paths())
        if portable_warning:
            _log_safely(logging.WARNING, portable_warning)
    except Exception as exc:
        # 这里既可能是目录不可写，也可能是 app.log/config.json 只读，不能只报目录。
        errors.append(f"数据目录或文件不可写：{exc}")

    if paths is not None:
        _check_selftest_writable(paths, errors)

    versions["qt"] = _probe(_qt_version, errors, "PyQt6")
    versions["qfluentwidgets"] = _probe(_qfluentwidgets_version, errors, "PyQt6-Fluent-Widgets")


def _check_main_window_import(errors: list[str]) -> None:
    """主窗口是启动必需组件；模块损坏必须让自检失败，而不是回退占位界面。"""
    try:
        importlib.import_module("app.ui.main_window")
    except Exception as exc:
        errors.append(f"主窗口导入失败：{exc}")


def _check_selftest_writable(paths: AppPaths, errors: list[str]) -> None:
    """探测真实目录与真实文件的可写性：临时探针通过不代表 app.log 能写。"""
    try:
        _check_writable(paths.data_dir)
        _check_writable(paths.log_dir)
    except OSError as exc:
        errors.append(f"数据目录不可写：{exc}")
        return

    app_log = paths.log_dir / LOG_FILE_NAME
    try:
        # 用真实日志目录初始化一次：只读的 app.log 会在这里抛出 PermissionError。
        setup_logging(paths.log_dir)
    except Exception as exc:
        errors.append(f"日志文件不可写（{app_log}）：{exc}")
    else:
        try:
            _probe_existing_file(app_log, "ab")
        except OSError as exc:
            errors.append(f"日志文件不可写（{app_log}）：{exc}")

    if paths.config_file.exists():
        try:
            _probe_existing_file(paths.config_file, "r+b")
        except OSError as exc:
            errors.append(f"配置文件不可写（{paths.config_file}）：{exc}")


def _probe_existing_file(path: Path, mode: str) -> None:
    """以追加/更新方式打开文件后立即关闭，不写入、不截断、不破坏内容。"""
    with path.open(mode):
        pass


def _report_selftest_write_failure(exc: BaseException) -> None:
    """报告写不出去时记录错误：日志可用就写日志，否则退回 stderr。"""
    message = f"写入自检报告失败：{exc}"
    _log_safely(logging.ERROR, message)
    stream = sys.stderr
    if stream is None:  # 窗口化 exe 的 stderr 可能不可用
        return
    try:
        print(message, file=stream)
    except Exception:
        pass


def _log_safely(level: int, message: str) -> None:
    try:
        get_logger().log(level, message)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.selftest:
        return run_selftest(args.report)
    try:
        return _run_gui(smoke_test=args.smoke_test)
    except Exception as exc:
        _handle_startup_failure(exc)
        return 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="PortHelperFluent",
        description="PortHelper Fluent 串口/网络调试助手",
    )
    parser.add_argument("--selftest", action="store_true", help="执行启动自检后退出")
    parser.add_argument("--report", type=Path, default=None, help="自检报告 JSON 的输出路径")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="启动真实主窗口并在 1.5 秒后自动退出（发布前冒烟检查）",
    )
    return parser.parse_args(argv)


def _run_gui(smoke_test: bool = False) -> int:
    """解析生效路径后启动界面；启动失败时把生效路径交给启动失败处理。"""
    paths, portable_warning = ensure_writable_paths(resolve_paths())
    try:
        # 日志初始化与异常钩子都可能失败（例如回退目录里的 app.log 只读），
        # 必须放在 try 内，保证失败时仍用已解析出的生效路径写崩溃报告。
        logger = setup_logging(paths.log_dir)
        # QApplication 还没创建：钩子只写日志与崩溃报告，不触碰任何 Qt 控件。
        # `_start_gui` 在 QApplication 就绪后会换成排队到 GUI 线程的错误提示回调。
        install_exception_hooks(paths.log_dir)
        return _start_gui(paths, portable_warning, logger, smoke_test=smoke_test)
    except Exception as exc:
        # 必须传生效路径：便携目录不可写时崩溃报告要落在 AppData 回退目录。
        _handle_startup_failure(exc, paths)
        return 1


def _start_gui(
    paths: AppPaths,
    portable_warning: str | None,
    logger: logging.Logger,
    *,
    smoke_test: bool = False,
) -> int:
    """在已解析的生效路径上创建 QApplication 与主窗口。"""
    settings = Settings(paths.config_file)
    settings.load()
    if settings.warning:
        logger.warning(settings.warning)

    # 先安装 Qt 消息处理器：它不依赖 QApplication 实例，因此可以捕获平台插件在
    # QApplication 构造期间输出的告警（例如插件加载失败、缩放策略问题）。
    from app.ui.qt_utils import install_qt_message_handler

    install_qt_message_handler(logger)

    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_TITLE)

    # QApplication 就绪后换成桥接回调：错误提示经信号排队到 GUI 线程，
    # 子线程触发 threading.excepthook 时不会直接创建 QMessageBox。
    install_exception_hooks(paths.log_dir, on_error=_create_error_callback())
    if portable_warning:
        logger.warning(portable_warning)

    # 必须在创建 FluentWindow 之前确认绑定，PyQt5 版本会造成白屏。
    try:
        check_fluent_binding()
    except Exception as exc:
        return _handle_binding_failure(exc, paths.log_dir)

    window = create_main_window(settings, paths)
    window.show()
    # 模态提示会阻塞 --smoke-test 的自动退出，冒烟模式只写日志。
    if portable_warning and _dialogs_enabled() and not smoke_test:
        _show_warning_dialog(portable_warning)
    logger.info("%s %s 已启动，数据目录：%s", APP_TITLE, APP_VERSION, paths.data_dir)

    if smoke_test:
        from PyQt6.QtCore import QTimer

        QTimer.singleShot(1500, app.quit)
    exit_code = app.exec()
    if smoke_test:
        logger.info("启动冒烟测试完成")
        return 0
    return exit_code


def _binding_failure_message(exc: BaseException) -> str:
    if isinstance(exc, ImportError):
        summary = f"启动自检失败：无法导入 qfluentwidgets（{exc.name or exc}）"
        cause = "可能原因：PyQt6 或 qfluentwidgets 未安装，或安装不完整。"
        action = "下一步：执行 pip install -r requirements.txt 重新安装依赖。"
    else:
        summary = f"启动自检失败：{BINDING_FAILURE_HINT}"
        cause = (
            "可能原因：qfluentwidgets 被 PyQt5-Fluent-Widgets 覆盖，实际绑定到了 PyQt5，"
            "继续启动会出现空白窗口。"
        )
        action = (
            "下一步：卸载 PyQt5-Fluent-Widgets 后重新安装 PyQt6-Fluent-Widgets，"
            "再执行 pip install -r requirements.txt。"
        )
    return f"{summary}。\n\n{cause}\n\n{action}\n\n技术细节：{exc}"


def _handle_binding_failure(exc: BaseException, log_dir: Path) -> int:
    """处理 Qt 绑定自检失败：写崩溃报告与日志、弹框说明原因，返回退出码 1。"""
    message = _binding_failure_message(exc)
    report_path: Path | None = None
    try:
        report_path = write_crash_report(log_dir, type(exc), exc, exc.__traceback__)
    except Exception:
        report_path = None
    try:
        get_logger().critical("Qt 绑定自检失败：%s", message)
    except Exception:
        pass
    if report_path is not None:
        message += f"\n\n崩溃报告已保存到：{report_path}"
    _notify_startup_failure(message)
    return 1


def _dialogs_enabled() -> bool:
    """`PORTHELPER_NO_DIALOGS=1` 时禁用所有启动期模态框，只写日志。"""
    value = os.environ.get(NO_DIALOGS_ENV, "").strip().lower()
    return value not in ("1", "true", "yes", "on")


def _show_error_dialog(message: str) -> None:
    """显示 Qt 错误提示框；没有 QApplication 或禁用对话框时直接返回。

    该函数也作为异常桥接的排队槽使用，因此不能抛异常：是否退回系统对话框由
    `_notify_startup_failure` 在调用前判断；`PORTHELPER_NO_DIALOGS` 同时约束
    运行期异常提示，避免无界面环境被子线程异常弹框阻塞。
    """
    if not _dialogs_enabled():
        return
    from PyQt6.QtWidgets import QApplication, QMessageBox

    if QApplication.instance() is None:
        return
    QMessageBox.critical(None, "PortHelper Fluent 错误", message)


def _show_native_error(message: str) -> None:
    """无 QApplication 时的兜底提示：Win32 MessageBoxW，窗口化 exe 也能看见。

    依赖缺失、QApplication 创建失败等早期错误发生在 Qt 可用之前，只能走系统
    对话框；失败（例如非 Windows）只记录日志，绝不静默丢弃也不抛出。
    MessageBoxW 返回 0 表示对话框没能显示，此时记录 Win32 错误码便于排查。
    """
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        result = user32.MessageBoxW(None, message, APP_TITLE, 0x10)
        if result == 0:
            error_code = ctypes.get_last_error()
            try:
                detail = ctypes.FormatError(error_code)
            except Exception:
                detail = "未知错误"
            _log_safely(
                logging.ERROR,
                f"系统错误对话框未能显示（Win32 错误 {error_code}：{detail}）：{message}",
            )
    except Exception:
        try:
            get_logger().exception("显示系统错误对话框失败，错误信息：%s", message)
        except Exception:
            pass


def _notify_startup_failure(message: str) -> None:
    """尽力让启动错误可见：Qt 对话框 -> 系统对话框；禁用对话框时只写日志。"""
    if not _dialogs_enabled():
        _log_safely(logging.ERROR, f"已设置 {NO_DIALOGS_ENV}，跳过弹框，仅记录日志：{message}")
        return
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:
        # PyQt6 缺失：Qt 对话框根本不可用，直接走系统对话框。
        _show_native_error(message)
        return
    if QApplication.instance() is None:
        # 早期失败（例如 QApplication 创建前）没有事件循环，只能走系统对话框。
        _show_native_error(message)
        return
    try:
        _show_error_dialog(message)
    except Exception:
        try:
            get_logger().exception("显示 Qt 错误提示框失败，改用系统对话框")
        except Exception:
            pass
        _show_native_error(message)


def _show_warning_dialog(message: str) -> None:
    if not _dialogs_enabled():
        return
    from PyQt6.QtWidgets import QApplication, QMessageBox

    if QApplication.instance() is None:
        return
    QMessageBox.warning(None, "PortHelper Fluent 提示", message)


def _error_bridge_class() -> type:
    """延迟定义 QObject 子类：缺少 PyQt6 时 main 模块仍可导入。"""
    global _ERROR_BRIDGE_TYPE
    if _ERROR_BRIDGE_TYPE is None:
        from PyQt6.QtCore import QObject, pyqtSignal

        class _ErrorBridge(QObject):
            """把异常提示排队到 GUI 线程，避免子线程直接创建 QMessageBox。"""

            show_error = pyqtSignal(str)

        _ERROR_BRIDGE_TYPE = _ErrorBridge
    return _ERROR_BRIDGE_TYPE


def _create_error_callback() -> Callable[[str], None] | None:
    """创建异常提示桥接，返回可跨线程调用的 `on_error`；没有 QApplication 时返回 None。

    `show_error` 用 `Qt.ConnectionType.QueuedConnection` 连接到 `_show_error_dialog`：
    `threading.excepthook` 在子线程触发时只发出信号，真正的 `QMessageBox` 由 GUI
    线程创建。QApplication 尚未创建时返回 None，异常钩子仅写日志与崩溃报告，
    不触碰任何 Qt 控件。
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    if QApplication.instance() is None:
        return None

    global _ERROR_BRIDGE
    bridge = _error_bridge_class()()
    bridge.show_error.connect(_show_error_dialog, Qt.ConnectionType.QueuedConnection)
    _ERROR_BRIDGE = bridge  # 保持引用，避免 QObject 在信号投递前被回收
    return bridge.show_error.emit


def _handle_startup_failure(exc: BaseException, paths: AppPaths | None = None) -> None:
    """写崩溃报告与日志、弹框说明原因。

    `paths` 是启动时实际生效的路径（例如已回退到 AppData 的目录）；只有生效路径
    尚未解析出来时才退回 `resolve_paths()`，避免把报告写进不可写的便携目录。
    没有 QApplication（窗口化 exe 的最早期失败）时改用系统对话框，绝不静默返回。
    """
    if paths is not None:
        log_dir = paths.log_dir
    else:
        try:
            log_dir = resolve_paths().log_dir
        except Exception:
            log_dir = Path(tempfile.gettempdir())
    report_path: Path | None = None
    try:
        report_path = write_crash_report(log_dir, type(exc), exc, exc.__traceback__)
    except Exception:
        report_path = None
    try:
        get_logger().critical("启动失败", exc_info=(type(exc), exc, exc.__traceback__))
    except Exception:
        pass

    message = f"程序启动失败：{exc}"
    if report_path is not None:
        message += f"\n\n崩溃报告已保存到：{report_path}"
    message += f"\n\n{_startup_failure_hint(exc)}"
    _notify_startup_failure(message)


def _startup_failure_hint(exc: BaseException) -> str:
    """按异常类型给出“可能原因 + 下一步”的中文提示，而不只是笼统的“检查依赖”。"""
    if isinstance(exc, PermissionError):
        return (
            "可能原因：日志或配置文件被设为只读、正被其他程序占用，"
            "或当前账户没有数据目录的写入权限。\n"
            "下一步：取消 app.log / config.json 的只读属性并关闭占用它们的程序；"
            "便携模式请确认 data 目录可写，或删除 portable.flag 改用 %APPDATA%；"
            "也可以把程序放到有写入权限的目录后重试。"
        )
    if isinstance(exc, OSError):
        # 路径/目录类失败（含本应用抛出的“便携与 AppData 都不可写”）：给出位置与回退指引。
        return (
            "可能原因：数据目录不可用（目录被删除、只读磁盘、路径被占用或权限不足）。\n"
            "下一步：确认数据目录存在且有写权限；便携模式请检查 data 目录，"
            "必要时删除 portable.flag 回退到 %APPDATA%。"
        )
    return "请检查依赖是否安装完整（pip install -r requirements.txt），或把崩溃报告提供给维护者。"


def _check_writable(directory: Path) -> None:
    """复用路径模块的探针：写一个字节并在 finally 中清理，不留下 .probe 残留。"""
    probe_write(directory)


def _probe(loader: Callable[[], str], errors: list[str], label: str) -> str:
    try:
        return loader()
    except Exception as exc:
        errors.append(f"{label} 版本检测失败：{exc}")
        return ""


def _qt_version() -> str:
    from PyQt6.QtCore import QT_VERSION_STR

    return QT_VERSION_STR


def _qfluentwidgets_version() -> str:
    return importlib.metadata.version("PyQt6-Fluent-Widgets")


if __name__ == "__main__":
    sys.exit(main())

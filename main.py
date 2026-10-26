"""PortHelper Fluent 入口：启动自检与临时占位窗口。

Qt 相关导入都放在函数内部，确保依赖缺失时仍能完成自检并写出报告。
Task 2 会用真实的 Fluent 主窗口替换这里的占位窗口。
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
    get_logger,
    install_exception_hooks,
    setup_logging,
    write_crash_report,
)
from app.core.settings import DEFAULT_SETTINGS, Settings
from app.services.exports import write_text_atomic
from app.services.paths import AppPaths, ensure_writable_paths, resolve_paths

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QWidget

APP_TITLE = "PortHelper Fluent"
APP_VERSION = str(DEFAULT_SETTINGS["app.version"])
BINDING_FAILURE_HINT = "qfluentwidgets 当前不是 PyQt6 版本"
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


def build_placeholder_window() -> "QWidget":
    """Task 2 之前的最小可启动窗口。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QVBoxLayout, QWidget
    from qfluentwidgets import BodyLabel, FluentIcon, FluentWindow, SubtitleLabel

    window = FluentWindow()
    window.setWindowTitle(f"{APP_TITLE} {APP_VERSION}")
    window.resize(960, 640)

    view = QWidget()
    view.setObjectName("placeholderView")
    layout = QVBoxLayout(view)
    layout.setSpacing(12)
    layout.addStretch(1)

    title = SubtitleLabel("主界面正在构建中", view)
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(title)

    hint = BodyLabel("配置、日志与会话记录等核心能力已就绪，完整界面将在后续版本提供。", view)
    hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(hint)

    layout.addStretch(1)
    window.addSubInterface(view, FluentIcon.HOME, "首页")
    return window


def create_main_window(settings: Settings) -> "QWidget":
    """优先使用 app.ui.main_window.MainWindow（Task 2），否则回退到占位窗口。"""
    try:
        from app.ui.main_window import MainWindow
    except ModuleNotFoundError as exc:
        if exc.name not in ("app.ui.main_window", "app.ui"):
            raise
        return build_placeholder_window()
    return MainWindow(settings)


def run_selftest(report_path: Path | None = None) -> int:
    """检查依赖、Qt 绑定与数据目录可写性，并写出 JSON 报告。返回进程退出码。"""
    errors: list[str] = []

    for module_name in REQUIRED_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # 自检需要收集全部问题，不能遇到第一个就退出
            errors.append(f"依赖 {module_name} 导入失败：{exc}")

    try:
        check_fluent_binding()
    except Exception as exc:
        errors.append(f"Qt 绑定自检失败：{exc}")

    try:
        paths, portable_warning = ensure_writable_paths(resolve_paths())
        if portable_warning:
            get_logger().warning(portable_warning)
        _check_writable(paths.data_dir)
        _check_writable(paths.log_dir)
    except Exception as exc:
        errors.append(f"数据目录不可写：{exc}")

    qt_version = _probe(_qt_version, errors, "PyQt6")
    qfluentwidgets_version = _probe(_qfluentwidgets_version, errors, "PyQt6-Fluent-Widgets")
    report = {
        "ok": not errors,
        "errors": errors,
        "python": platform.python_version(),
        "qt": qt_version,
        "qfluentwidgets": qfluentwidgets_version,
        "version": APP_VERSION,
    }

    exit_code = 0 if not errors else 1
    if report_path is not None:
        try:
            write_text_atomic(Path(report_path), json.dumps(report, ensure_ascii=False, indent=2))
        except OSError as exc:
            get_logger().error("写入自检报告失败：%s", exc)
            exit_code = 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.selftest:
        return run_selftest(args.report)
    try:
        return _run_gui()
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
    return parser.parse_args(argv)


def _run_gui() -> int:
    """解析生效路径后启动界面；启动失败时把生效路径交给启动失败处理。"""
    paths, portable_warning = ensure_writable_paths(resolve_paths())
    logger = setup_logging(paths.log_dir)
    # QApplication 还没创建：钩子只写日志与崩溃报告，不触碰任何 Qt 控件。
    # `_start_gui` 在 QApplication 就绪后会换成排队到 GUI 线程的错误提示回调。
    install_exception_hooks(paths.log_dir)
    try:
        return _start_gui(paths, portable_warning, logger)
    except Exception as exc:
        # 必须传生效路径：便携目录不可写时崩溃报告要落在 AppData 回退目录。
        _handle_startup_failure(exc, paths)
        return 1


def _start_gui(paths: AppPaths, portable_warning: str | None, logger: logging.Logger) -> int:
    """在已解析的生效路径上创建 QApplication 与主窗口。"""
    settings = Settings(paths.config_file)
    settings.load()
    if settings.warning:
        logger.warning(settings.warning)

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

    window = create_main_window(settings)
    window.show()
    if portable_warning:
        _show_warning_dialog(portable_warning)
    logger.info("%s %s 已启动，数据目录：%s", APP_TITLE, APP_VERSION, paths.data_dir)
    return app.exec()


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
    try:
        _show_error_dialog(message)
    except Exception:
        pass
    return 1


def _show_error_dialog(message: str) -> None:
    from PyQt6.QtWidgets import QApplication, QMessageBox

    if QApplication.instance() is None:
        return
    QMessageBox.critical(None, "PortHelper Fluent 错误", message)


def _show_warning_dialog(message: str) -> None:
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
    """
    log_dir = paths.log_dir if paths is not None else resolve_paths().log_dir
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
    message += "\n\n请检查依赖是否安装完整，或把崩溃报告提供给维护者。"
    try:
        _show_error_dialog(message)
    except Exception:
        pass


def _check_writable(directory: Path) -> None:
    fd, name = tempfile.mkstemp(prefix="selftest-", suffix=".tmp", dir=directory)
    os.close(fd)
    Path(name).unlink(missing_ok=True)


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

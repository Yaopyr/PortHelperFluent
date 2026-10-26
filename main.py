"""PortHelper Fluent 入口：启动自检与临时占位窗口。

Qt 相关导入都放在函数内部，确保依赖缺失时仍能完成自检并写出报告。
Task 2 会用真实的 Fluent 主窗口替换这里的占位窗口。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
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
from app.services.paths import ensure_dirs, resolve_paths

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QWidget

APP_TITLE = "PortHelper Fluent"
APP_VERSION = str(DEFAULT_SETTINGS["app.version"])
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


def check_fluent_binding() -> None:
    """确认 qfluentwidgets 绑定的是 PyQt6，避免被 PyQt5 版本覆盖后白屏。"""
    from PyQt6.QtWidgets import QWidget
    from qfluentwidgets import FluentWindow

    if not issubclass(FluentWindow, QWidget):
        raise RuntimeError("qfluentwidgets 当前不是 PyQt6 版本")


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
        paths = resolve_paths()
        ensure_dirs(paths)
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
    paths = resolve_paths()
    ensure_dirs(paths)
    logger = setup_logging(paths.log_dir)
    install_exception_hooks(paths.log_dir, on_error=_show_error_dialog)

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

    window = create_main_window(settings)
    window.show()
    logger.info("%s %s 已启动，数据目录：%s", APP_TITLE, APP_VERSION, paths.data_dir)
    return app.exec()


def _show_error_dialog(message: str) -> None:
    from PyQt6.QtWidgets import QApplication, QMessageBox

    if QApplication.instance() is None:
        return
    QMessageBox.critical(None, "PortHelper Fluent 错误", message)


def _handle_startup_failure(exc: BaseException) -> None:
    report_path: Path | None = None
    try:
        report_path = write_crash_report(
            resolve_paths().log_dir, type(exc), exc, exc.__traceback__
        )
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

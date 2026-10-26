"""Qt 消息处理器：Qt 自身的告警要进入统一日志，且不能递归或抛异常。"""

import logging
import threading
from pathlib import Path

import pytest
from PyQt6.QtCore import QtMsgType

from app.core.logging_setup import get_logger, setup_logging


class _RecordingLogger:
    """记录映射后的级别与格式化消息，避免真实文件日志按级别过滤干扰断言。"""

    def __init__(self) -> None:
        self.entries: list[tuple[int, str]] = []

    def log(self, level: int, message: str, *args: object) -> None:
        self.entries.append((level, message % args if args else message))


class _FakeContext:
    """QMessageLogContext 在 PyQt6 中不可实例化，用同名字段替代。"""

    file = "test_qt_utils.py"
    line = 42
    function = "test"


@pytest.fixture(autouse=True)
def _detach_handler():
    yield
    from app.ui.qt_utils import uninstall_qt_message_handler

    uninstall_qt_message_handler()
    logger = get_logger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_qt_warning_is_logged_with_level_and_context(tmp_path: Path, qapp) -> None:
    from PyQt6.QtCore import qWarning

    from app.ui.qt_utils import install_qt_message_handler

    logger = setup_logging(tmp_path / "logs")
    install_qt_message_handler(logger)

    qWarning("qt-message-handler-test")

    for handler in logger.handlers:
        handler.flush()
    content = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "qt-message-handler-test" in content
    assert "WARNING" in content
    # 带来源上下文：文件名、行号与函数名（Qt 无法提供时为占位符）。
    assert "Qt:" in content
    assert ".py" in content or "<unknown>" in content


@pytest.mark.parametrize(
    ("mode", "level"),
    [
        (QtMsgType.QtDebugMsg, logging.DEBUG),
        (QtMsgType.QtInfoMsg, logging.INFO),
        (QtMsgType.QtWarningMsg, logging.WARNING),
        (QtMsgType.QtCriticalMsg, logging.ERROR),
        (QtMsgType.QtFatalMsg, logging.CRITICAL),
    ],
)
def test_qt_message_handler_maps_all_five_levels(qapp, mode, level) -> None:
    """五种 QtMsgType 都要映射到对应日志级别；Fatal 只能直接调用处理器验证。"""
    from app.ui import qt_utils

    logger = _RecordingLogger()
    qt_utils.install_qt_message_handler(logger)
    handler = qt_utils._MESSAGE_HANDLER
    assert handler is not None

    handler(mode, _FakeContext(), f"level-{mode.name}")

    assert len(logger.entries) == 1
    recorded_level, recorded_message = logger.entries[0]
    assert recorded_level == level
    assert f"level-{mode.name}" in recorded_message
    assert recorded_message.startswith("Qt: ")


def test_install_qt_message_handler_is_idempotent(qapp) -> None:
    from app.ui import qt_utils

    first_logger = _RecordingLogger()
    second_logger = _RecordingLogger()
    qt_utils.install_qt_message_handler(first_logger)
    first_handler = qt_utils._MESSAGE_HANDLER
    assert first_handler is not None

    qt_utils.install_qt_message_handler(second_logger)

    assert qt_utils._MESSAGE_HANDLER is first_handler
    first_handler(QtMsgType.QtInfoMsg, _FakeContext(), "仍由首个处理器记录")
    assert first_logger.entries
    assert second_logger.entries == []


def test_uninstall_qt_message_handler_stops_capture(qapp) -> None:
    from PyQt6.QtCore import qWarning

    from app.ui import qt_utils

    logger = _RecordingLogger()
    qt_utils.install_qt_message_handler(logger)
    qWarning("qt-before-uninstall")
    qt_utils.uninstall_qt_message_handler()

    assert qt_utils._MESSAGE_HANDLER is None
    qWarning("qt-after-uninstall")
    messages = [text for _, text in logger.entries]
    assert any("qt-before-uninstall" in text for text in messages)
    assert all("qt-after-uninstall" not in text for text in messages)


def test_qt_message_handler_blocks_same_thread_recursion(qapp) -> None:
    """处理器内部再次写日志触发 Qt 消息时，同一线程必须被递归保护拦住。"""
    from PyQt6.QtCore import qWarning

    from app.ui.qt_utils import install_qt_message_handler

    class RecursiveLogger:
        def __init__(self) -> None:
            self.calls = 0

        def log(self, *args: object, **kwargs: object) -> None:
            self.calls += 1
            qWarning("nested-qt-message")

    logger = RecursiveLogger()
    install_qt_message_handler(logger)

    qWarning("outer-qt-message")

    assert logger.calls == 1


def test_qt_message_handler_guard_is_thread_local(qapp) -> None:
    """一个线程处理消息时，另一个线程的真实 Qt 告警不能被全局标记吞掉。"""
    from PyQt6.QtCore import qWarning

    from app.ui.qt_utils import install_qt_message_handler

    class CrossThreadLogger:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def log(self, level: int, message: str, *args: object) -> None:
            self.messages.append(message % args if args else message)
            if len(self.messages) == 1:
                worker = threading.Thread(target=lambda: qWarning("worker-qt-message"))
                worker.start()
                worker.join()

    logger = CrossThreadLogger()
    install_qt_message_handler(logger)

    qWarning("main-qt-message")

    assert any("main-qt-message" in text for text in logger.messages)
    assert any("worker-qt-message" in text for text in logger.messages)


def test_startup_installs_handler_before_qapplication_creation(
    tmp_path: Path, monkeypatch, qapp
) -> None:
    """平台插件在 QApplication 构造期就会告警，处理器必须先装好。"""
    import PyQt6.QtWidgets as qt_widgets

    import main
    from app.services.paths import resolve_paths
    from app.ui import qt_utils

    events: list[str] = []

    class FailingApplication:
        @classmethod
        def instance(cls):
            return None

        def __init__(self, *args: object, **kwargs: object) -> None:
            events.append("QApplication")
            raise RuntimeError("模拟 QApplication 创建失败")

    monkeypatch.setattr(
        qt_utils, "install_qt_message_handler", lambda logger=None: events.append("install")
    )
    monkeypatch.setattr(qt_widgets, "QApplication", FailingApplication)
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    logger = setup_logging(paths.log_dir)

    with pytest.raises(RuntimeError, match="QApplication"):
        main._start_gui(paths, None, logger)

    assert events == ["install", "QApplication"]


def test_qt_message_handler_tolerates_broken_logger(qapp) -> None:
    """logger 不可用时处理器只能静默放弃，不能抛回 Qt（否则会递归）。"""
    from PyQt6.QtCore import qWarning

    from app.ui.qt_utils import install_qt_message_handler

    class BrokenLogger:
        def log(self, *args, **kwargs) -> None:
            raise RuntimeError("logger 已关闭")

    install_qt_message_handler(BrokenLogger())

    qWarning("logger-broken-test")  # 不抛异常即为通过

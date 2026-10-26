"""Qt 消息处理器：Qt 自身的告警要进入统一日志，且不能递归或抛异常。"""

from pathlib import Path

import pytest

from app.core.logging_setup import get_logger, setup_logging


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


def test_qt_message_handler_tolerates_broken_logger(qapp) -> None:
    """logger 不可用时处理器只能静默放弃，不能抛回 Qt（否则会递归）。"""
    from PyQt6.QtCore import qWarning

    from app.ui.qt_utils import install_qt_message_handler

    class BrokenLogger:
        def log(self, *args, **kwargs) -> None:
            raise RuntimeError("logger 已关闭")

    install_qt_message_handler(BrokenLogger())

    qWarning("logger-broken-test")  # 不抛异常即为通过

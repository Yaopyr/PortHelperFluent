import logging
import logging.handlers
import sys
import threading
from pathlib import Path

import pytest

from app.core.logging_setup import (
    LOGGER_NAME,
    get_logger,
    install_exception_hooks,
    setup_logging,
    write_crash_report,
)


@pytest.fixture(autouse=True)
def _detach_handlers():
    yield
    logger = get_logger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_get_logger_uses_package_name() -> None:
    assert get_logger() is logging.getLogger(LOGGER_NAME)
    assert LOGGER_NAME == "porthelper"


def test_setup_logging_writes_to_rotating_file(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path / "logs", level=logging.INFO)

    logger.info("测试日志")

    log_file = tmp_path / "logs" / "app.log"
    assert log_file.is_file()
    content = log_file.read_text(encoding="utf-8")
    assert "测试日志" in content
    assert "porthelper" in content


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    setup_logging(tmp_path / "logs")
    logger = setup_logging(tmp_path / "logs")

    assert len(logger.handlers) == 1
    assert logger.level == logging.INFO


def test_setup_logging_configures_rotation(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path / "logs")

    handler = logger.handlers[0]
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == 1_000_000
    assert handler.backupCount == 3
    assert handler.encoding.lower() in ("utf-8", "utf8")


def test_write_crash_report_contains_traceback(tmp_path: Path) -> None:
    try:
        raise RuntimeError("模拟崩溃")
    except RuntimeError:
        exc_type, exc, tb = sys.exc_info()

    report = write_crash_report(tmp_path / "logs", exc_type, exc, tb)

    assert report.is_file()
    assert report.name.startswith("crash-")
    assert report.suffix == ".txt"
    content = report.read_text(encoding="utf-8")
    assert "模拟崩溃" in content
    assert "Traceback" in content


def test_install_exception_hooks_reports_and_notifies(tmp_path: Path) -> None:
    messages: list[str] = []
    old_hook = sys.excepthook
    old_thread_hook = threading.excepthook
    try:
        setup_logging(tmp_path / "logs")
        install_exception_hooks(tmp_path / "logs", on_error=messages.append)

        sys.excepthook(RuntimeError, RuntimeError("钩子测试"), None)

        reports = list((tmp_path / "logs").glob("crash-*.txt"))
        assert len(reports) == 1
        assert "钩子测试" in reports[0].read_text(encoding="utf-8")
        assert messages and "钩子测试" in messages[0]
        assert "错误" in messages[0]
    finally:
        sys.excepthook = old_hook
        threading.excepthook = old_thread_hook


def test_install_exception_hooks_handles_thread_exceptions(tmp_path: Path) -> None:
    old_hook = sys.excepthook
    old_thread_hook = threading.excepthook
    try:
        setup_logging(tmp_path / "logs")
        install_exception_hooks(tmp_path / "logs")

        args = threading.ExceptHookArgs(
            (RuntimeError, RuntimeError("线程异常"), None, threading.current_thread())
        )
        threading.excepthook(args)

        reports = list((tmp_path / "logs").glob("crash-*.txt"))
        assert len(reports) == 1
        assert "线程异常" in reports[0].read_text(encoding="utf-8")
    finally:
        sys.excepthook = old_hook
        threading.excepthook = old_thread_hook


def test_install_exception_hooks_keeps_keyboard_interrupt(tmp_path: Path) -> None:
    old_hook = sys.excepthook
    old_thread_hook = threading.excepthook
    try:
        setup_logging(tmp_path / "logs")
        install_exception_hooks(tmp_path / "logs")

        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)

        assert list((tmp_path / "logs").glob("crash-*.txt")) == []
    finally:
        sys.excepthook = old_hook
        threading.excepthook = old_thread_hook


def test_install_exception_hooks_keeps_thread_system_exit(tmp_path: Path) -> None:
    old_hook = sys.excepthook
    old_thread_hook = threading.excepthook
    try:
        setup_logging(tmp_path / "logs")
        install_exception_hooks(tmp_path / "logs")

        args = threading.ExceptHookArgs(
            (SystemExit, SystemExit(0), None, threading.current_thread())
        )
        threading.excepthook(args)

        assert list((tmp_path / "logs").glob("crash-*.txt")) == []
    finally:
        sys.excepthook = old_hook
        threading.excepthook = old_thread_hook

"""统一日志、崩溃报告与全局异常钩子。"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Callable

LOGGER_NAME = "porthelper"
LOG_FILE_NAME = "app.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def setup_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """把 `porthelper` 日志输出到 log_dir/app.log，重复调用不会叠加处理器。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = get_logger()
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        log_dir / LOG_FILE_NAME,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    return logger


def write_crash_report(
    log_dir: Path,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
) -> Path:
    """把 traceback 写入 crash-YYYYmmdd-HHMMSS.txt 并返回报告路径。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now()
    report_path = log_dir / f"crash-{timestamp:%Y%m%d-%H%M%S}.txt"
    detail = "".join(traceback.format_exception(exc_type, exc, tb))
    report_path.write_text(
        f"PortHelper Fluent 崩溃报告\n时间：{timestamp:%Y-%m-%d %H:%M:%S}\n\n{detail}",
        encoding="utf-8",
    )
    return report_path


def install_exception_hooks(
    log_dir: Path,
    on_error: Callable[[str], None] | None = None,
) -> None:
    """安装主线程与子线程异常钩子：写崩溃报告、写日志并通知界面。

    主线程的 KeyboardInterrupt 与子线程的 SystemExit 保持默认行为，不写崩溃报告，
    避免把用户主动中断或正常退出误报为崩溃。
    """
    log_dir = Path(log_dir)
    logger = get_logger()

    def report(
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        message = f"程序遇到未处理的错误：{exc}"
        try:
            report_path = write_crash_report(log_dir, exc_type, exc, tb)
            logger.critical("%s（崩溃报告：%s）", message, report_path, exc_info=(exc_type, exc, tb))
        except Exception:
            logger.exception("写入崩溃报告失败")
        if on_error is not None:
            try:
                on_error(message)
            except Exception:
                logger.exception("执行异常回调失败")

    def main_hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        report(exc_type, exc, tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        report(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = main_hook
    threading.excepthook = thread_hook

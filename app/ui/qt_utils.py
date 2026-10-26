"""Qt 运行时辅助：诊断消息接入统一日志、安全槽与事件循环等待（规格 §8）。

不依赖 QApplication 是否已创建，但需要 PyQt6 可用；因此该模块属于 ui 层，
且 Qt 相关导入都放在函数内部，保证缺少 PyQt6 时 main 仍可导入做自检。
"""

from __future__ import annotations

import functools
import inspect
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from app.core.logging_setup import get_logger, write_crash_report

# qInstallMessageHandler 不会替我们持有回调引用，这里保存以避免被回收。
_MESSAGE_HANDLER: Callable[..., None] | None = None
# 安装前的处理器：卸载时交还给 Qt，而不是强行清空别的组件安装的处理器。
_PREVIOUS_HANDLER: Callable[..., None] | None = None
# 处理器内部写日志时可能再次触发 Qt 消息；递归标记必须按线程隔离，
# 否则一个线程正在处理消息时会把其他线程的真实告警一起吞掉。
_HANDLER_STATE = threading.local()


def install_qt_message_handler(logger: logging.Logger | None = None) -> None:
    """安装 Qt 消息处理器：Debug/Info/Warning/Critical/Fatal 映射到统一日志。

    每条消息都会记录来源文件、行号与函数名，便于定位白屏、样式表错误等问题。
    处理器内部绝不抛出异常，也不重复写日志（按线程的递归保护）；logger 不可用时
    静默放弃。安装是幂等的：已有处理器时直接返回，不会重复接管 Qt 消息。
    ``QtFatalMsg`` 还会额外写一份崩溃报告，便于窗口化 exe 事后排查。
    """
    global _MESSAGE_HANDLER, _PREVIOUS_HANDLER

    if _MESSAGE_HANDLER is not None:
        return

    from PyQt6.QtCore import QtMsgType, qInstallMessageHandler

    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    target = logger if logger is not None else get_logger()

    def handler(mode: Any, context: Any, message: str) -> None:
        if getattr(_HANDLER_STATE, "active", False):
            return
        _HANDLER_STATE.active = True
        try:
            level = levels.get(mode, logging.INFO)
            target.log(
                level,
                "Qt: %s（%s:%s，%s）",
                message,
                getattr(context, "file", None) or "<unknown>",
                getattr(context, "line", None) or 0,
                getattr(context, "function", None) or "<unknown>",
            )
            if mode == QtMsgType.QtFatalMsg:
                _write_fatal_crash_report(target, message)
        except Exception:
            # 处理器内不能再抛异常，否则会递归触发自身。
            pass
        finally:
            _HANDLER_STATE.active = False

    # 先确认接管成功，再保存回调引用：安装失败时不留下“已安装”的假状态。
    previous = qInstallMessageHandler(handler)
    _MESSAGE_HANDLER = handler
    _PREVIOUS_HANDLER = previous


def uninstall_qt_message_handler() -> None:
    """卸载处理器并把 Qt 消息交还给安装前的处理器（测试与诊断用）。"""
    global _MESSAGE_HANDLER, _PREVIOUS_HANDLER

    if _MESSAGE_HANDLER is None:
        return

    previous = _PREVIOUS_HANDLER
    try:
        from PyQt6.QtCore import qInstallMessageHandler

        qInstallMessageHandler(previous)
    except Exception:
        pass
    finally:
        _MESSAGE_HANDLER = None
        _PREVIOUS_HANDLER = None


def _write_fatal_crash_report(logger: logging.Logger, message: str) -> None:
    """Qt 致命错误直接终止进程，窗口化 exe 看不到 traceback，必须落盘报告。"""
    log_dir = _log_dir_from_logger(logger)
    if log_dir is None:
        return
    try:
        error = RuntimeError(f"Qt 致命错误：{message}")
        report_path = write_crash_report(log_dir, RuntimeError, error, None)
    except Exception:
        return
    try:
        logger.log(logging.CRITICAL, "Qt 致命错误崩溃报告已保存到：%s", report_path)
    except Exception:
        pass


def _log_dir_from_logger(logger: logging.Logger) -> Path | None:
    """从日志处理器推断日志目录；没有文件处理器时返回 None（不写报告）。"""
    for handler in getattr(logger, "handlers", ()) or ():
        base_filename = getattr(handler, "baseFilename", None)
        if base_filename:
            return Path(base_filename).parent
    return None


def safe_slot(func: Callable[..., Any]) -> Callable[..., Any]:
    """包装 Qt 槽：捕获异常、写日志并显示可读错误，绝不把异常抛回事件循环。

    PyQt6 遇到未捕获的槽异常会直接中止进程，因此所有连接到信号的槽都必须经过
    这里（或自行捕获全部异常）。父窗口取第一个位置参数（QWidget 实例）。

    Qt 的 ``clicked`` 等信号会附加 ``checked`` 等参数，而无参槽不应因此变成
    ``TypeError``。装饰时按被包装函数的签名算出可接受的位置参数上限：多余的尾部
    位置参数在调用前被裁掉，``*args`` 函数保持原样；绑定方法的签名不含 ``self``，
    类内方法（装饰时仍是未绑定函数）的签名含 ``self``，两种形式都按实际参数个数
    正确处理。裁剪只发生在调用前，函数体内部抛出的 ``TypeError`` 仍会进入统一的
    错误反馈，不会被误当成参数数量问题吞掉。
    """

    max_positional = _max_positional_args(func)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        call_args = args
        if max_positional is not None and len(call_args) > max_positional:
            call_args = call_args[:max_positional]
        try:
            return func(*call_args, **kwargs)
        except Exception as exc:
            try:
                logging.getLogger("porthelper").exception("slot failed")
            except Exception:
                pass
            parent = None
            try:
                from PyQt6.QtWidgets import QWidget

                if args and isinstance(args[0], QWidget):
                    parent = args[0]
            except Exception:
                parent = None
            try:
                from app.ui.feedback import show_error

                show_error(parent, "操作失败", readable_message(exc))
            except Exception:
                try:
                    logging.getLogger("porthelper").exception("显示错误反馈失败")
                except Exception:
                    pass
            return None

    return wrapper


def _max_positional_args(func: Callable[..., Any]) -> int | None:
    """返回函数可接受的位置参数个数；有 ``*args`` 或签名不可读时返回 None。"""
    try:
        parameters = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return None
    if any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return None
    return sum(
        1
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )


def readable_message(exc: BaseException) -> str:
    """把常见技术异常翻译成「发生了什么 + 下一步」的中文提示。"""
    if isinstance(exc, PermissionError):
        return "没有访问权限：请检查文件或目录的只读属性，或改用有写入权限的位置。"
    if isinstance(exc, FileNotFoundError):
        return "找不到指定的文件或目录：请确认路径是否存在。"
    if _is_serial_exception(exc):
        return "串口通信失败：请检查设备是否已连接、端口是否被其他程序占用。"
    if isinstance(exc, OSError):
        return "文件或设备访问失败：请检查连接状态、磁盘空间与访问权限。"
    text = str(exc).strip()
    return text or type(exc).__name__


def _is_serial_exception(exc: BaseException) -> bool:
    try:
        from serial import SerialException
    except Exception:
        return False
    return isinstance(exc, SerialException)


def process_events_until(predicate: Callable[[], bool], timeout_ms: int = 2000) -> bool:
    """处理 Qt 事件直到条件成立或超时，供离屏测试等待信号与定时器。"""
    from PyQt6.QtCore import QEventLoop
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        if app is not None:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(0.005)

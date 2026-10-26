"""Qt 运行时辅助：把 Qt 自身的诊断消息接入统一日志（规格 §8）。

不依赖 QApplication 是否已创建，但需要 PyQt6 可用；因此该模块属于 ui 层，
且 Qt 相关导入都放在函数内部。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from app.core.logging_setup import get_logger

# qInstallMessageHandler 不会替我们持有回调引用，这里保存以避免被回收。
_MESSAGE_HANDLER: Callable[..., None] | None = None
# 处理器内部写日志时可能再次触发 Qt 消息；递归标记必须按线程隔离，
# 否则一个线程正在处理消息时会把其他线程的真实告警一起吞掉。
_HANDLER_STATE = threading.local()


def install_qt_message_handler(logger: logging.Logger | None = None) -> None:
    """安装 Qt 消息处理器：Debug/Info/Warning/Critical/Fatal 映射到统一日志。

    每条消息都会记录来源文件、行号与函数名，便于定位白屏、样式表错误等问题。
    处理器内部绝不抛出异常，也不重复写日志（按线程的递归保护）；logger 不可用时
    静默放弃。安装是幂等的：已有处理器时直接返回，不会重复接管 Qt 消息。
    """
    global _MESSAGE_HANDLER

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
        except Exception:
            # 处理器内不能再抛异常，否则会递归触发自身。
            pass
        finally:
            _HANDLER_STATE.active = False

    _MESSAGE_HANDLER = handler
    qInstallMessageHandler(handler)


def uninstall_qt_message_handler() -> None:
    """卸载处理器并把 Qt 消息交还默认处理（测试与诊断用）。"""
    global _MESSAGE_HANDLER

    try:
        from PyQt6.QtCore import qInstallMessageHandler

        qInstallMessageHandler(None)
    except Exception:
        pass
    _MESSAGE_HANDLER = None

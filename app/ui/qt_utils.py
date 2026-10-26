"""Qt 运行时辅助：把 Qt 自身的诊断消息接入统一日志（规格 §8）。

不依赖 QApplication 是否已创建，但需要 PyQt6 可用；因此该模块属于 ui 层，
且 Qt 相关导入都放在函数内部。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.core.logging_setup import get_logger

# qInstallMessageHandler 不会替我们持有回调引用，这里保存以避免被回收。
_MESSAGE_HANDLER: Callable[..., None] | None = None
# 处理器内部写日志时可能再次触发 Qt 消息，用标记阻断递归。
_IN_HANDLER = False


def install_qt_message_handler(logger: logging.Logger | None = None) -> None:
    """安装 Qt 消息处理器：Debug/Info/Warning/Critical/Fatal 映射到统一日志。

    每条消息都会记录来源文件、行号与函数名，便于定位白屏、样式表错误等问题。
    处理器内部绝不抛出异常，也不重复写日志（递归保护）；logger 不可用时静默放弃。
    """
    global _MESSAGE_HANDLER

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
        global _IN_HANDLER
        if _IN_HANDLER:
            return
        _IN_HANDLER = True
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
            _IN_HANDLER = False

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

"""统一的非阻塞反馈：信息条优先，错误在没有可见父窗口时退回消息框。"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox, QWidget
from qfluentwidgets import InfoBar, InfoBarPosition

from app.core.logging_setup import get_logger

INFO_DURATION_MS = 3000
ERROR_DURATION_MS = 5000

# 非模态 QMessageBox 需要 Python 侧持有引用，否则会在显示前被回收。
_OPEN_MESSAGE_BOXES: list[QMessageBox] = []


def show_info(parent: QWidget | None, title: str, content: str = "") -> None:
    """在可见父窗口右上角显示信息条；父窗口不可见时不打扰用户。"""
    _show(InfoBar.info, INFO_DURATION_MS, parent, title, content)


def show_success(parent: QWidget | None, title: str, content: str = "") -> None:
    _show(InfoBar.success, INFO_DURATION_MS, parent, title, content)


def show_warning(parent: QWidget | None, title: str, content: str = "") -> None:
    _show(InfoBar.warning, INFO_DURATION_MS, parent, title, content)


def show_error(parent: QWidget | None, title: str, content: str = "") -> None:
    """显示错误信息条；没有可见父窗口时退回非模态消息框，绝不阻塞调用方。"""
    if _show(InfoBar.error, ERROR_DURATION_MS, parent, title, content):
        return
    _show_non_modal_message_box(parent, title, content)


def _show(
    factory: Callable[..., object],
    duration: int,
    parent: QWidget | None,
    title: str,
    content: str,
) -> bool:
    """调用 InfoBar 工厂；父窗口不可见或创建失败时返回 False。"""
    try:
        if not _is_visible(parent):
            return False
        factory(
            title=title,
            content=content,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            duration=duration,
            position=InfoBarPosition.TOP_RIGHT,
            parent=parent,
        )
    except Exception:
        get_logger().exception("显示信息条失败：%s", title)
        return False
    return True


def _show_non_modal_message_box(parent: QWidget | None, title: str, content: str) -> None:
    """错误兜底：非模态消息框，不进入嵌套事件循环，不阻塞自动化测试。"""
    try:
        box = QMessageBox(
            QMessageBox.Icon.Critical,
            title,
            content,
            QMessageBox.StandardButton.Ok,
            parent,
        )
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.finished.connect(lambda _result, target=box: _release_message_box(target))
        _OPEN_MESSAGE_BOXES.append(box)
        box.show()
    except Exception:
        # 没有 QApplication 或 Qt 已关闭时只能写日志，反馈层绝不抛出。
        get_logger().exception("显示错误消息框失败：%s", title)


def _release_message_box(box: QMessageBox) -> None:
    try:
        _OPEN_MESSAGE_BOXES.remove(box)
    except ValueError:
        pass


def _is_visible(parent: QWidget | None) -> bool:
    return isinstance(parent, QWidget) and parent.isVisible()

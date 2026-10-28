"""页面布局与滚动原语：统一页面最小尺寸、内容边距与 Fluent 滚动条风格。

各页面统一从这里取滚动容器与内容布局，避免各自新建裸 `QScrollArea`
（原生滚动条与 qfluentwidgets 的 `SmoothScrollBar` 风格不一致），
也避免各页面最小尺寸、边距取值漂移导致窗口缩小时控件重叠。
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QWidget
from qfluentwidgets import SingleDirectionScrollArea

#: 页面内容区四边留白（左、上、右、下），所有页面统一使用。
PAGE_MARGINS: tuple[int, int, int, int] = (24, 20, 24, 24)
#: 页面内容区纵向间距。
PAGE_SPACING: int = 12
#: 页面最小尺寸，低于此尺寸布局必然重叠，统一兜底。
PAGE_MIN_SIZE = QSize(1000, 640)
#: 左右分栏时侧栏面板的最小宽度，保证窗口缩小时不被压扁。
PANEL_MIN_WIDTH: int = 360

#: 让滚动容器自身及其内容层保持透明，融入 Fluent 页面背景。
_SCROLL_AREA_QSS = (
    "QScrollArea { background: transparent; border: none; }"
    " QScrollArea > QWidget > QWidget { background: transparent; }"
)


def scroll_area(
    parent: QWidget | None = None,
    *,
    min_width: int = 0,
) -> SingleDirectionScrollArea:
    """创建统一风格的纵向 Fluent 滚动容器。

    Args:
        parent: 父控件。
        min_width: 大于 0 时作为滚动容器的最小宽度。
    """
    area = SingleDirectionScrollArea(parent, Qt.Orientation.Vertical)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setStyleSheet(_SCROLL_AREA_QSS)
    if min_width > 0:
        area.setMinimumWidth(min_width)
    return area


def scroll_container(
    host: QWidget,
    content: QWidget,
    *,
    min_width: int = 0,
) -> SingleDirectionScrollArea:
    """把 `content` 装进统一风格的滚动容器，返回该容器。

    Args:
        host: 滚动容器的父控件。
        content: 被滚动的页面内容。
        min_width: 大于 0 时作为滚动容器的最小宽度。
    """
    area = scroll_area(host, min_width=min_width)
    area.setWidget(content)
    return area


def page_content(parent: QWidget) -> tuple[QWidget, QVBoxLayout]:
    """创建带统一边距与间距的页面内容层，返回 `(content, layout)`。"""
    content = QWidget(parent)
    layout = QVBoxLayout(content)
    layout.setContentsMargins(*PAGE_MARGINS)
    layout.setSpacing(PAGE_SPACING)
    return content, layout


def apply_page_minimum(page: QWidget) -> None:
    """给页面套用统一的最小尺寸，防止窗口缩小时控件重叠。"""
    page.setMinimumSize(PAGE_MIN_SIZE)

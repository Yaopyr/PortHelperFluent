"""串口调试页占位。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from app.ui.pages.placeholder import PlaceholderPage


class SerialPage(PlaceholderPage):
    def __init__(self, settings=None, paths=None, parent: QWidget | None = None) -> None:
        super().__init__(
            "串口调试",
            "端口参数、收发区与会话记录将在串口调试任务中提供。",
            route="serial",
            settings=settings,
            paths=paths,
            parent=parent,
        )

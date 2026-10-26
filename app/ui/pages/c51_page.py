"""C51 向导页占位。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from app.ui.pages.placeholder import PlaceholderPage


class C51Page(PlaceholderPage):
    def __init__(
        self, settings=None, paths=None, parent: QWidget | None = None, **kwargs
    ) -> None:
        super().__init__(
            "C51 向导",
            "定时器、串口波特率与 Keil 工程生成将在 C51 向导任务中提供。",
            route="c51",
            settings=settings,
            paths=paths,
            parent=parent,
            **kwargs,
        )

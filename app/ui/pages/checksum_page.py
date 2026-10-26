"""校验计算页占位。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from app.ui.pages.placeholder import PlaceholderPage


class ChecksumPage(PlaceholderPage):
    def __init__(self, settings=None, paths=None, parent: QWidget | None = None) -> None:
        super().__init__(
            "校验计算",
            "求和、奇偶校验、CRC 与自定义算法将在校验计算任务中提供。",
            route="checksum",
            settings=settings,
            paths=paths,
            parent=parent,
        )

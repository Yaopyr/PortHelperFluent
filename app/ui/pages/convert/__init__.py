"""编码转换页（含七个子工具）占位。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from app.ui.pages.placeholder import PlaceholderPage


class ConvertPage(PlaceholderPage):
    def __init__(
        self, settings=None, paths=None, parent: QWidget | None = None, **kwargs
    ) -> None:
        super().__init__(
            "编码转换",
            "进制转换、Bin/Hex/Text、数码管、调色板、GIF、音频与二维码将在后续任务中提供。",
            route="convert",
            settings=settings,
            paths=paths,
            parent=parent,
            **kwargs,
        )

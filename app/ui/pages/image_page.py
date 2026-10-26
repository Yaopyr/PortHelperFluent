"""图片取模页占位。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from app.ui.pages.placeholder import PlaceholderPage


class ImagePage(PlaceholderPage):
    def __init__(
        self, settings=None, paths=None, parent: QWidget | None = None, **kwargs
    ) -> None:
        super().__init__(
            "图片取模",
            "图片预览、颜色深度与扫描方向导出将在图片取模任务中提供。",
            route="image",
            settings=settings,
            paths=paths,
            parent=parent,
            **kwargs,
        )

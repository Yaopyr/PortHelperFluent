"""点阵生成页占位。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from app.ui.pages.placeholder import PlaceholderPage


class DotMatrixPage(PlaceholderPage):
    def __init__(
        self, settings=None, paths=None, parent: QWidget | None = None, **kwargs
    ) -> None:
        super().__init__(
            "点阵生成",
            "字体取模、网格编辑与批量导出将在点阵生成任务中提供。",
            route="dotmatrix",
            settings=settings,
            paths=paths,
            parent=parent,
            **kwargs,
        )

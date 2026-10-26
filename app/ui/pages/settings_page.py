"""设置与关于页占位。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from app.ui.pages.placeholder import PlaceholderPage


class SettingsPage(PlaceholderPage):
    def __init__(
        self, settings=None, paths=None, parent: QWidget | None = None, **kwargs
    ) -> None:
        super().__init__(
            "设置与关于",
            "主题、强调色、目录入口与支持范围说明将在设置任务中提供。",
            route="settings",
            settings=settings,
            paths=paths,
            parent=parent,
            **kwargs,
        )

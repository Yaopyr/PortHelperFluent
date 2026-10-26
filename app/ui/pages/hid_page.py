"""USB HID 页占位。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from app.ui.pages.placeholder import PlaceholderPage


class HidPage(PlaceholderPage):
    def __init__(
        self, settings=None, paths=None, parent: QWidget | None = None, **kwargs
    ) -> None:
        super().__init__(
            "USB HID",
            "标准 HID 设备枚举与 Report 收发将在 USB HID 任务中提供。",
            route="hid",
            settings=settings,
            paths=paths,
            parent=parent,
            **kwargs,
        )

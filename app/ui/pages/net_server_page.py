"""网络服务器页占位。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from app.ui.pages.placeholder import PlaceholderPage


class NetServerPage(PlaceholderPage):
    def __init__(
        self, settings=None, paths=None, parent: QWidget | None = None, **kwargs
    ) -> None:
        super().__init__(
            "网络服务器",
            "TCP 监听、客户端列表与广播发送将在网络服务器任务中提供。",
            route="net-server",
            settings=settings,
            paths=paths,
            parent=parent,
            **kwargs,
        )

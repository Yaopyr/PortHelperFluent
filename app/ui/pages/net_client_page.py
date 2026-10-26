"""网络客户端页占位。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from app.ui.pages.placeholder import PlaceholderPage


class NetClientPage(PlaceholderPage):
    def __init__(self, settings=None, paths=None, parent: QWidget | None = None) -> None:
        super().__init__(
            "网络调试",
            "TCP/UDP 客户端、Ping 与网卡信息将在网络调试任务中提供。",
            route="net-client",
            settings=settings,
            paths=paths,
            parent=parent,
        )

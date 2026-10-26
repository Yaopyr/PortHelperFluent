"""串口调试页占位；Task 3 的构造契约在此固定。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from app.ui.pages.placeholder import PlaceholderPage


class SerialPage(PlaceholderPage):
    """串口调试页占位。

    Task 3 将替换为真实页面，并保持构造契约：

    ``SerialPage(settings, paths, parent=None, *, worker_factory=None)``

    ``worker_factory`` 供测试注入 FakeTransport/Worker，生产代码不传时使用默认实现。
    """

    def __init__(
        self,
        settings=None,
        paths=None,
        parent: QWidget | None = None,
        *,
        worker_factory=None,
        **kwargs,
    ) -> None:
        super().__init__(
            "串口调试",
            "端口参数、收发区与会话记录将在串口调试任务中提供。",
            route="serial",
            settings=settings,
            paths=paths,
            parent=parent,
            **kwargs,
        )
        self.worker_factory = worker_factory

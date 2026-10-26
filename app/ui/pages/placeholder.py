"""页面占位基类：统一的空状态、稳定的路由 key 与关闭确认钩子。"""

from __future__ import annotations

from PyQt6.QtWidgets import QVBoxLayout, QWidget

from app.ui.widgets.empty_state import EmptyState


class PlaceholderPage(QWidget):
    """尚未实现功能的页面；后续任务会用真实页面替换子类实现。

    显式提供 `has_active_work` / `stop_workers`，主窗口无需判断页面类型即可
    在退出前汇总连接状态并停止后台任务。
    """

    def __init__(
        self,
        title: str,
        hint: str,
        *,
        route: str,
        settings=None,
        paths=None,
        parent: QWidget | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.paths = paths
        self.setObjectName(route)
        # 后续任务的页面会接收 worker_factory 等扩展参数；占位阶段接受并忽略，
        # 保证主窗口始终可以使用统一的 (settings, paths, parent=...) 构造契约。
        self.extra_options = dict(kwargs)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(EmptyState(title, hint, self))

    def has_active_work(self) -> bool:
        """是否存在需要确认后才会中断的连接/监听/连续发送。"""
        return False

    def stop_workers(self) -> None:
        """停止后台线程与句柄；占位页面无事可做。"""

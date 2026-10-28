"""编码转换页壳：工具选择、堆叠页面与上次工具恢复。"""

from __future__ import annotations

from PyQt6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, SegmentedWidget, TitleLabel

from app.core.logging_setup import get_logger
from app.ui.pages.convert.audio_tool import AudioTool
from app.ui.pages.convert.base_tool import BaseTool
from app.ui.pages.convert.data_tool import DataTool
from app.ui.pages.convert.gif_tool import GifTool
from app.ui.pages.convert.palette_tool import PaletteTool
from app.ui.pages.convert.qr_tool import QrTool
from app.ui.pages.convert.sevenseg_tool import SevenSegTool
from app.ui.qt_utils import safe_slot
from app.ui.widgets.page_layout import apply_page_minimum, scroll_container

# 工具类契约：__init__(settings, paths, parent=...)；提供 title 与 route。
TOOL_CLASSES: list[type[QWidget]] = [
    BaseTool,
    DataTool,
    SevenSegTool,
    PaletteTool,
    GifTool,
    AudioTool,
    QrTool,
]


class ConvertPage(QWidget):
    """编码转换工作台；每个工具是独立控件，页面只负责切换与持久化。"""

    def __init__(
        self,
        settings=None,
        paths=None,
        parent: QWidget | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("convert")
        apply_page_minimum(self)
        self.settings = settings
        self.paths = paths
        self.extra_options = dict(kwargs)
        self.tools: dict[str, QWidget] = {}
        self._build_ui()

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(14)

        self.title_label = TitleLabel("编码转换", self)
        root.addWidget(self.title_label)
        self.subtitle_label = CaptionLabel(
            "进制、Bin/Hex/Text、数码管、调色板、GIF、音频与二维码转换工具",
            self,
        )
        self.subtitle_label.setWordWrap(True)
        root.addWidget(self.subtitle_label)

        self.tool_switch = SegmentedWidget(self)
        self.tool_stack = QStackedWidget(self)

        for tool_class in TOOL_CLASSES:
            tool = tool_class(self.settings, self.paths, parent=self.tool_stack)
            route = str(getattr(tool, "route", "") or tool.objectName() or len(self.tools))
            if route in self.tools:
                raise ValueError(f"编码转换工具路由重复：{route}")
            title = str(getattr(tool, "title", "") or route)
            self.tools[route] = tool
            self.tool_stack.addWidget(tool)
            self.tool_switch.addItem(route, title)
            # 便于测试与其他页面代码直接访问具体工具（如 page.base_tool）。
            setattr(self, f"{route}_tool", tool)

        root.addWidget(self.tool_switch)
        root.addWidget(scroll_container(self, self.tool_stack), 1)

        route = self._restore_route()
        if route:
            # 先完成恢复再连接信号，避免初始化时写配置。
            self.tool_switch.setCurrentItem(route)
            self.tool_stack.setCurrentIndex(list(self.tools).index(route))
        self.tool_switch.currentItemChanged.connect(self._on_tool_changed)

    def _restore_route(self) -> str:
        if not self.tools:
            return ""
        saved = ""
        if self.settings is not None:
            try:
                saved = str(self.settings.get("convert.last_tool", "") or "")
            except Exception:
                get_logger().exception("读取编码转换页工具选择失败")
        if saved in self.tools:
            return saved
        return next(iter(self.tools))

    # ---- 工具切换 -----------------------------------------------------

    def tool_routes(self) -> tuple[str, ...]:
        return tuple(self.tools)

    def current_tool_route(self) -> str:
        route = self.tool_switch.currentRouteKey()
        return str(route) if route in self.tools else ""

    def current_tool(self) -> QWidget | None:
        return self.tools.get(self.current_tool_route())

    def set_tool(self, route: str) -> None:
        """切换到指定工具；未知路由抛出中文错误。"""
        if route not in self.tools:
            raise ValueError(f"未知编码转换工具：{route}")
        self.tool_switch.setCurrentItem(route)
        self._on_tool_changed(route)

    @safe_slot
    def _on_tool_changed(self, route: str, *_args) -> None:
        route = str(route)
        if route not in self.tools:
            return
        self.tool_stack.setCurrentIndex(list(self.tools).index(route))
        self._save_last_tool(route)

    def _save_last_tool(self, route: str) -> None:
        if self.settings is None:
            return
        try:
            self.settings.set("convert.last_tool", route)
            self.settings.save()
        except Exception:
            get_logger().exception("保存编码转换页工具选择失败")

    # ---- 生命周期 -----------------------------------------------------

    def has_active_work(self) -> bool:
        """任一工具报告后台活动时返回 True；单个工具异常不影响判断。"""
        for tool in self.tools.values():
            checker = getattr(tool, "has_active_work", None)
            if not callable(checker):
                continue
            try:
                if checker():
                    return True
            except Exception:
                get_logger().exception("检查编码转换工具活动状态失败：%s", tool.objectName())
        return False

    def stop_workers(self) -> None:
        for tool in self.tools.values():
            stopper = getattr(tool, "stop_workers", None)
            if not callable(stopper):
                continue
            try:
                stopper()
            except Exception:
                get_logger().exception("停止编码转换工具后台任务失败：%s", tool.objectName())

    def closeEvent(self, event) -> None:
        route = self.current_tool_route()
        if route:
            self._save_last_tool(route)
        super().closeEvent(event)

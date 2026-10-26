"""Fluent 主窗口：分组导航、页面装配、主题/Mica 与退出清理。"""

from __future__ import annotations

from PyQt6.QtCore import QByteArray
from PyQt6.QtWidgets import QMessageBox, QWidget
from qfluentwidgets import FluentIcon, FluentWindow, NavigationItemPosition

from app.core.logging_setup import get_logger
from app.core.settings import Settings
from app.services.paths import AppPaths
from app.ui.pages.c51_page import C51Page
from app.ui.pages.checksum_page import ChecksumPage
from app.ui.pages.convert import ConvertPage
from app.ui.pages.dotmatrix_page import DotMatrixPage
from app.ui.pages.hid_page import HidPage
from app.ui.pages.image_page import ImagePage
from app.ui.pages.net_client_page import NetClientPage
from app.ui.pages.net_server_page import NetServerPage
from app.ui.pages.serial_page import SerialPage
from app.ui.pages.settings_page import SettingsPage
from app.ui.theme import apply_theme, enable_mica as enable_window_mica

APP_TITLE = "PortHelper Fluent"
DEFAULT_VERSION = "1.0.0"

# (分组标题, ((路由, 页面类, 图标名, 导航文字), ...))
_PAGE_SPECS = (
    (
        "通信",
        (
            ("serial", SerialPage, "CONNECT", "串口调试"),
            ("net-client", NetClientPage, "GLOBE", "网络调试"),
            ("net-server", NetServerPage, "IOT", "网络服务器"),
            ("hid", HidPage, "DEVELOPER_TOOLS", "USB HID"),
        ),
    ),
    (
        "工具",
        (
            ("checksum", ChecksumPage, "CODE", "校验计算"),
            ("convert", ConvertPage, "CODE", "编码转换"),
            ("image", ImagePage, "PHOTO", "图片取模"),
            ("dotmatrix", DotMatrixPage, "FONT", "点阵生成"),
            ("c51", C51Page, "DEVELOPER_TOOLS", "C51 向导"),
        ),
    ),
    (
        "其他",
        (
            ("settings", SettingsPage, "SETTING", "设置与关于"),
        ),
    ),
)

PAGE_ROUTES = tuple(
    route for _header, specs in _PAGE_SPECS for route, _class, _icon, _text in specs
)


def icon_for(name: str):
    """按名称取 FluentIcon；旧版本缺少某个图标时退回通用图标而不崩溃。"""
    icon = getattr(FluentIcon, name, None)
    return icon if icon is not None else FluentIcon.APPLICATION


class MainWindow(FluentWindow):
    """应用主窗口；接收配置与生效路径，并转交给各页面。"""

    def __init__(
        self,
        settings: Settings,
        paths: AppPaths,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._paths = paths
        self.pages: dict[str, QWidget] = {}

        self.setObjectName("mainWindow")
        self.setWindowTitle(f"{APP_TITLE} {DEFAULT_VERSION}")
        self.setMinimumSize(1180, 760)
        self.resize(1280, 820)
        self._restore_geometry()

        self._build_navigation()
        self.switchTo(self.pages["serial"])
        self._disable_search_line()
        self._apply_appearance()

    # ---- 装配 ---------------------------------------------------------

    def _build_navigation(self) -> None:
        for header, specs in _PAGE_SPECS:
            self.navigationInterface.addItemHeader(header, NavigationItemPosition.TOP)
            for route, page_class, icon_name, text in specs:
                page = page_class(self._settings, self._paths, parent=self)
                self.pages[route] = page
                setattr(self, f"{route.replace('-', '_')}_page", page)
                self.addSubInterface(page, icon_for(icon_name), text)

    def _disable_search_line(self) -> None:
        """部分 qfluentwidgets 版本带导航搜索框；本项目用页面分组即可定位。"""
        for owner in (self.navigationInterface, getattr(self.navigationInterface, "panel", None)):
            search_line = getattr(owner, "searchLine", None)
            if search_line is not None:
                search_line.setEnabled(False)

    def _apply_appearance(self) -> None:
        try:
            apply_theme(self._settings)
        except Exception:
            get_logger().exception("应用主题失败，继续使用默认主题")
        self.enable_mica()

    def enable_mica(self) -> bool:
        """启用 Mica；失败只记日志，绝不阻塞启动。"""
        try:
            return bool(enable_window_mica(self))
        except Exception:
            get_logger().exception("启用 Mica 效果失败")
            return False

    # ---- 窗口状态 -----------------------------------------------------

    def _restore_geometry(self) -> None:
        encoded = self._settings.get("ui.window_geometry", "")
        if not encoded:
            return
        try:
            data = QByteArray.fromBase64(str(encoded).encode("ascii"))
            if not data.isEmpty():
                self.restoreGeometry(data)
        except Exception:
            get_logger().exception("恢复窗口尺寸失败，使用默认尺寸")

    def _save_geometry(self) -> None:
        try:
            encoded = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self._settings.set("ui.window_geometry", encoded)
            self._settings.save()
        except Exception:
            get_logger().exception("保存窗口尺寸失败")

    # ---- 退出 ---------------------------------------------------------

    def has_active_work(self) -> bool:
        """任一页面报告连接/监听/连续发送时返回 True；单个页面出错不影响退出。"""
        for page in self.pages.values():
            try:
                if page.has_active_work():
                    return True
            except Exception:
                get_logger().exception("检查页面活动状态失败：%s", page.objectName())
        return False

    def stop_workers(self) -> None:
        for page in self.pages.values():
            try:
                page.stop_workers()
            except Exception:
                get_logger().exception("停止页面后台任务失败：%s", page.objectName())

    def closeEvent(self, event) -> None:
        try:
            if self.has_active_work() and bool(self._settings.get("ui.confirm_exit", True)):
                answer = QMessageBox.question(
                    self,
                    "退出确认",
                    "仍有连接或监听正在进行，退出会断开它们。确定要退出吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
            self.stop_workers()
        except Exception:
            get_logger().exception("退出前清理页面失败")
        self._save_geometry()
        super().closeEvent(event)

    def smoke_test(self) -> bool:
        """启动冒烟检查：构造成功后返回 True，供 `main.py --smoke-test` 使用。"""
        return True

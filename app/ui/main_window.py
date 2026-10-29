"""Fluent 主窗口：分组导航、页面装配、主题/Mica 与退出清理。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import QByteArray, QPointF, QRectF, QTimer, Qt
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import QMessageBox, QWidget
from qfluentwidgets import FluentIcon, FluentWindow, NavigationItemPosition

from app import APP_VERSION
from app.core.logging_setup import get_logger
from app.core.settings import Settings
from app.io.update_worker import UpdateCheckWorker
from app.services.paths import AppPaths
from app.services.updates import is_newer
from app.ui.feedback import show_info
from app.ui.pages.bluetooth_page import BluetoothPage
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
# 与 main.NO_DIALOGS_ENV 同一语义：无界面/自动测试环境不弹任何模态框。
NO_DIALOGS_ENV = "PORTHELPER_NO_DIALOGS"

# 启动静默检查更新的延迟：让窗口先完成首次绘制，避免与启动耗时叠加。
STARTUP_UPDATE_CHECK_DELAY_MS = 1500

# 导航面板始终以悬浮（MENU）模式展开：qfluentwidgets 在窗口足够宽时会把
# 面板撑进布局、挤压页面内容；把“允许内嵌展开”的门槛抬到不可达即可让
# 面板悬浮在页面上方，页面宽度保持不变。数值本身无意义，只需大于任何真实
# 窗口宽度；它依赖 qfluentwidgets 的 expand() 判定逻辑，升级库后由测试兜底。
NAV_FLOAT_MIN_EXPAND_WIDTH = 1_000_000

# (分组标题, ((路由, 页面类, 图标名, 导航文字), ...))
_PAGE_SPECS = (
    (
        "通信",
        (
            ("serial", SerialPage, "CONNECT", "串口调试"),
            ("bluetooth", BluetoothPage, "BLUETOOTH", "蓝牙调试"),
            ("net-client", NetClientPage, "GLOBE", "网络调试"),
            ("net-server", NetServerPage, "IOT", "网络服务器"),
            ("hid", HidPage, "GAME", "USB HID"),
        ),
    ),
    (
        "工具",
        (
            ("checksum", ChecksumPage, "FINGERPRINT", "校验计算"),
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


# ---- 应用图标 ---------------------------------------------------------

APP_ICON_NAME = "icon.ico"
APP_ICON_RELATIVE = Path("build") / APP_ICON_NAME
FALLBACK_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def find_app_icon(*roots: Path | None) -> Path | None:
    """按优先级查找 `build/icon.ico`；找不到时返回 None（调用方使用内置图标）。

    查找顺序：显式传入的根目录（如 exe 目录）、PyInstaller 解包目录、仓库根目录
    与当前工作目录。源码运行时仓库根目录一定命中；打包后若未携带图标则自然退回
    内置图标，不会因为缺文件而启动失败。
    """
    candidates: list[Path] = []
    for root in roots:
        if root is not None:
            candidates.append(Path(root))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass))
    candidates.append(Path(__file__).resolve().parents[2])
    candidates.append(Path.cwd())

    seen: set[Path] = set()
    for root in candidates:
        for relative in (APP_ICON_RELATIVE, Path(APP_ICON_NAME)):
            path = (root / relative).resolve()
            if path in seen:
                continue
            seen.add(path)
            try:
                if path.is_file() and path.stat().st_size > 0:
                    return path
            except OSError:
                continue
    return None


def app_icon(*roots: Path | None) -> QIcon:
    """加载应用图标；ICO 缺失或损坏时退回代码绘制的小图标。"""
    path = find_app_icon(*roots)
    if path is not None:
        try:
            icon = QIcon(str(path))
            if not icon.isNull() and icon.availableSizes():
                return icon
            get_logger().warning("图标文件无法解析，改用内置图标：%s", path)
        except Exception:
            get_logger().exception("加载图标文件失败，改用内置图标：%s", path)
    return fallback_app_icon()


def fallback_app_icon() -> QIcon:
    """在没有 ICO（例如精简打包）时用 QPainter 绘制蓝色圆角方块图标。"""
    try:
        from PyQt6.QtGui import QGuiApplication

        if QGuiApplication.instance() is None:
            return QIcon()
        icon = QIcon()
        for size in FALLBACK_ICON_SIZES:
            pixmap = _fallback_icon_pixmap(size)
            if not pixmap.isNull():
                icon.addPixmap(pixmap)
        return icon
    except Exception:
        get_logger().exception("生成内置图标失败")
        return QIcon()


def _fallback_icon_pixmap(size: int) -> QPixmap:
    """按 64×64 设计坐标绘制内置图标，保证任意 DPI 下都要么成功要么返回空位图。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter()
    try:
        if not painter.begin(pixmap):
            return QPixmap()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        gradient = QLinearGradient(0, 0, 0, size)
        gradient.setColorAt(0.0, QColor("#3A8CFF"))
        gradient.setColorAt(1.0, QColor("#0947B4"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)

        scale = size / 64.0
        pen = QPen(QColor("#FFFFFF"))
        pen.setWidthF(max(1.0, size * 0.055))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(
            QRectF(12 * scale, 15 * scale, 40 * scale, 34 * scale),
            5 * scale,
            5 * scale,
        )
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(20 * scale, 25 * scale),
                    QPointF(28 * scale, 32 * scale),
                    QPointF(20 * scale, 39 * scale),
                ]
            )
        )
        painter.drawPolyline(
            QPolygonF(
                [
                    QPointF(34 * scale, 32 * scale),
                    QPointF(38 * scale, 32 * scale),
                    QPointF(40 * scale, 28 * scale),
                    QPointF(43 * scale, 37 * scale),
                    QPointF(45 * scale, 32 * scale),
                    QPointF(50 * scale, 32 * scale),
                ]
            )
        )
    except Exception:
        get_logger().exception("绘制内置图标失败")
        return QPixmap()
    finally:
        if painter.isActive():
            painter.end()
    return pixmap


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
        self._smoke_mode = False
        # 更新安装后重启时跳过退出确认；启动检查线程由 stop_workers() 统一收尾。
        self._skip_exit_confirm = False
        self._startup_update_worker = None
        self.pages: dict[str, QWidget] = {}

        self.setObjectName("mainWindow")
        self.setWindowTitle(f"{APP_TITLE} {APP_VERSION}")
        self._apply_window_icon()
        self.setMinimumSize(1180, 760)
        self.resize(1280, 820)
        self._restore_geometry()

        self._build_navigation()
        self.switchTo(self.pages["serial"])
        self._disable_search_line()
        self._apply_appearance()

    # ---- 装配 ---------------------------------------------------------

    def _build_navigation(self) -> None:
        self.navigationInterface.setMinimumExpandWidth(NAV_FLOAT_MIN_EXPAND_WIDTH)
        for header, specs in _PAGE_SPECS:
            self.navigationInterface.addItemHeader(header, NavigationItemPosition.TOP)
            for route, page_class, icon_name, text in specs:
                # 页面构造契约（Task 3+ 必须保持）：
                #   page(settings, paths, parent=self)
                # 具体页面可追加关键字参数（如 SerialPage 的 worker_factory）。
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

    def _apply_window_icon(self) -> None:
        """设置窗口图标；ICO 缺失时使用内置图标，绝不影响启动。"""
        try:
            self.setWindowIcon(app_icon(getattr(self._paths, "exe_dir", None)))
        except Exception:
            get_logger().exception("设置窗口图标失败，继续启动")

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
        worker = self._startup_update_worker
        if worker is not None:
            try:
                worker.requestInterruption()
                worker.wait(2000)
            except Exception:
                get_logger().exception("停止启动更新检查线程失败")
            self._startup_update_worker = None

    # ---- 在线更新 -----------------------------------------------------

    def schedule_update_check(self) -> None:
        """按设置安排一次启动静默检查；不满足条件时什么都不做。"""
        if not bool(self._settings.get("ui.check_updates_on_start", True)):
            return
        QTimer.singleShot(STARTUP_UPDATE_CHECK_DELAY_MS, self._silent_update_check)

    def _silent_update_check(self) -> None:
        """在后台检查最新版本；失败只写日志，绝不打断启动。"""
        if self._startup_update_worker is not None:
            return
        worker = UpdateCheckWorker()
        worker.checked.connect(self._on_startup_update_checked)
        worker.failed.connect(lambda msg: get_logger().warning("检查更新失败：%s", msg))
        worker.finished.connect(self._on_startup_check_finished)
        worker.finished.connect(worker.deleteLater)
        self._startup_update_worker = worker
        worker.start()

    def _on_startup_check_finished(self) -> None:
        """检查线程结束：清引用，避免 stop_workers() 触碰已销毁的对象。"""
        self._startup_update_worker = None

    def _on_startup_update_checked(self, release) -> None:
        try:
            version = str(getattr(release, "version", "") or "")
            if version and is_newer(version):
                show_info(self, f"发现新版本 {version}", "可在「设置 → 更新」中查看并安装。")
        except Exception:
            get_logger().exception("处理启动更新检查结果失败")

    def prepare_for_update(self) -> None:
        """更新安装前跳过退出确认并关闭窗口（几何由 closeEvent 统一保存）。"""
        self._skip_exit_confirm = True
        self.close()

    def closeEvent(self, event) -> None:
        """退出前确认并停止后台任务；确认失败也必须清理，取消则保持窗口打开。"""
        should_close = True
        try:
            should_close = self._confirm_close()
        except Exception:
            # 确认流程本身出错不能把窗口卡死：按用户同意退出处理。
            get_logger().exception("退出确认失败，按直接退出处理")
            should_close = True
        finally:
            if should_close:
                self._stop_workers_logged()
        if not should_close:
            event.ignore()
            return
        if not self._smoke_mode:
            self._save_geometry()
        super().closeEvent(event)

    def _confirm_close(self) -> bool:
        """返回是否允许关闭；更新安装/冒烟模式/无对话框环境直接允许。"""
        if self._skip_exit_confirm:
            return True
        if self._smoke_mode:
            return True
        if not self.has_active_work():
            return True
        if not bool(self._settings.get("ui.confirm_exit", True)):
            return True
        if not _dialogs_enabled():
            get_logger().warning("已设置 %s，跳过退出确认并停止后台任务", NO_DIALOGS_ENV)
            return True
        try:
            answer = QMessageBox.question(
                self,
                "退出确认",
                "仍有连接或监听正在进行，退出会断开它们。确定要退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        except Exception:
            # 测试/无界面环境下模态框可能无法创建：按确认退出处理，
            # 但仍会走 stop_workers()，绝不跳过后台清理。
            get_logger().exception("退出确认对话框失败，按确认退出处理")
            return True
        return answer == QMessageBox.StandardButton.Yes

    def _stop_workers_logged(self) -> None:
        try:
            self.stop_workers()
        except Exception:
            get_logger().exception("退出前停止页面后台任务失败")

    def smoke_test(self) -> bool:
        """启动冒烟检查：标记冒烟模式并确认页面/导航装配完整。

        冒烟模式关闭退出确认与窗口几何保存，避免自动化运行改动用户配置。
        """
        self._smoke_mode = True
        try:
            expected = len(PAGE_ROUTES)
            return len(self.pages) == expected and self.stackedWidget.count() == expected
        except Exception:
            get_logger().exception("启动冒烟检查失败")
            return False


def _dialogs_enabled() -> bool:
    """`PORTHELPER_NO_DIALOGS=1` 时禁用模态框（与 main._dialogs_enabled 同语义）。"""
    value = os.environ.get(NO_DIALOGS_ENV, "").strip().lower()
    return value not in ("1", "true", "yes", "on")

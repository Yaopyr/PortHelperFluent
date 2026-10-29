"""设置与关于页：外观/行为偏好、在线更新、数据目录入口与支持范围说明。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QColorDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SpinBox,
    SwitchButton,
    TitleLabel,
)

from app import APP_VERSION
from app.core.logging_setup import get_logger
from app.core.settings import DEFAULT_SETTINGS
from app.io.update_worker import UpdateCheckWorker, UpdateDownloadWorker
from app.services.exports import write_text_atomic
from app.services.updater_install import can_replace, install_update
from app.services.updates import is_newer, prune_updates, updates_dir
from app.ui.feedback import show_error, show_success, show_warning
from app.ui.qt_utils import readable_message, safe_slot
from app.ui.theme import DEFAULT_ACCENT, apply_theme
from app.ui.widgets.cards import section_card
from app.ui.widgets.page_layout import apply_page_minimum, page_content, scroll_container

MIN_DISPLAY_LINES = 1000
MAX_DISPLAY_LINES = 100_000
MIN_FONT_SIZE = 9
MAX_FONT_SIZE = 24

THEME_ITEMS = (("跟随系统", "auto"), ("浅色", "light"), ("深色", "dark"))
ENCODING_ITEMS = (("ANSI（系统）", "ansi"), ("GBK", "gbk"), ("UTF-8", "utf-8"))

SUBTITLE = "外观、行为、数据目录与支持范围；修改后自动保存并写入配置文件。"

THEME_HINT = "主题与强调色立即生效；跟随系统会随 Windows 明暗模式变化。"
BEHAVIOUR_HINT = (
    "默认编码、显示行数与自动滚动在下次启动应用时生效；主题与退出确认立即生效。"
    "退出确认立即生效；日志字体大小用于收发区等宽字体。"
)

UNSUPPORTED_ITEMS = (
    ("蓝牙调试", "原程序依赖独立的蓝牙助手程序与系统配对流程。"),
    ("内核串口监视", "需要 32 位内核驱动，安装与卸载具有系统侵入性。"),
    ("CH372", "依赖厂商 DLL，纯 Python 单文件无法可靠复刻。"),
    ("Keil 配色替换", "会修改外部 Keil 配置，本项目不做。"),
)

UPDATE_HINT = "只从 GitHub Releases 下载更新并校验 SHA-256；更新文件保存在数据目录下的 updates。"
UPDATE_IDLE_TEXT = "尚未检查。"
UPDATE_NOTES_MAX_CHARS = 4000
UPDATE_NOTES_HEIGHT = 120
UPDATE_INSTALL_QUESTION = (
    "将关闭程序并安装 {version}，完成后自动重新启动。\n"
    "仍有连接/监听时会先断开。\n\n现在安装吗？"
)
UPDATE_INSTALL_CONFIRM_TITLE = "安装更新"

DEPENDENCY_TEXT = (
    "依赖与许可：PyQt6 与 PyQt6-Fluent-Widgets（GPL-3.0）、"
    "pyserial（BSD-3-Clause）、Pillow（MIT-CMU）、numpy（BSD-3-Clause）、"
    "psutil（BSD-3-Clause）、hidapi（BSD-3-Clause）、qrcode（BSD-3-Clause）。"
)

EVIDENCE_TEXT = (
    "REA 证据说明：功能范围依据对原程序公开界面与文档的只读观察整理；"
    "未复制任何二进制资源、图标、截图、驱动或专有代码，"
    "校验、CRC 与 8051 计算均为公开标准的独立实现。"
)


class SettingsPage(QWidget):
    """设置页；所有修改立即写入配置，主题与强调色同时刷新全局样式。"""

    title = "设置与关于"

    def __init__(
        self,
        settings=None,
        paths=None,
        parent: QWidget | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settings")
        apply_page_minimum(self)
        self.settings = settings
        self.paths = paths
        self.extra_options = dict(kwargs)
        self._loading = True
        self.accent_hex = DEFAULT_ACCENT
        # 更新状态：最后一次检查到的 Release、待安装快照与两个后台任务。
        self._release = None
        self._pending_release = None
        self._check_worker = None
        self._download_worker = None
        self._work_dir: Path | None = None
        self._manual_install = False

        self._build_ui()
        self.refresh_fields()
        self._loading = False

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content, root = page_content(self)
        outer.addWidget(scroll_container(self, content))

        header = QHBoxLayout()
        header.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(TitleLabel("设置与关于", content))
        title_box.addWidget(CaptionLabel(SUBTITLE, content))
        header.addLayout(title_box, 1)
        self.status_label = CaptionLabel("修改后自动保存。", content)
        header.addWidget(self.status_label)
        root.addLayout(header)

        root.addWidget(self._build_appearance_card(content))
        root.addWidget(self._build_behaviour_card(content))
        root.addWidget(self._build_paths_card(content))
        root.addWidget(self._build_update_card(content))
        root.addWidget(self._build_about_card(content))
        root.addStretch(1)

    def _build_appearance_card(self, parent: QWidget) -> QWidget:
        card, layout = section_card("外观", THEME_HINT)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(2, 1)

        grid.addWidget(BodyLabel("主题", card), 0, 0)
        self.theme_combo = ComboBox(card)
        for label, key in THEME_ITEMS:
            self.theme_combo.addItem(label, userData=key)
        self.theme_combo.setToolTip("跟随系统会使用 Windows 当前的浅色/深色设置")
        self.theme_combo.setFixedWidth(220)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        grid.addWidget(self.theme_combo, 0, 1)

        grid.addWidget(BodyLabel("强调色", card), 1, 0)
        accent_row = QHBoxLayout()
        accent_row.setSpacing(8)
        self.accent_swatch = QFrame(card)
        self.accent_swatch.setFixedSize(22, 22)
        self.accent_swatch.setToolTip("当前强调色")
        accent_row.addWidget(self.accent_swatch)
        self.accent_label = BodyLabel(DEFAULT_ACCENT, card)
        self.accent_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        accent_row.addWidget(self.accent_label)
        self.accent_button = PushButton("选择强调色…", card)
        self.accent_button.setToolTip("选择后立即应用并保存")
        self.accent_button.clicked.connect(self.choose_accent)
        accent_row.addWidget(self.accent_button)
        accent_row.addStretch(1)
        grid.addLayout(accent_row, 1, 1)

        layout.addLayout(grid)
        return card

    def _build_behaviour_card(self, parent: QWidget) -> QWidget:
        card, layout = section_card("行为", BEHAVIOUR_HINT)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(2, 1)

        grid.addWidget(BodyLabel("默认编码", card), 0, 0)
        self.encoding_combo = ComboBox(card)
        for label, codec in ENCODING_ITEMS:
            self.encoding_combo.addItem(label, userData=codec)
        self.encoding_combo.setToolTip(
            "新打开页面时的默认文本编码；ANSI 使用系统当前代码页（简体中文为 cp936）"
        )
        self.encoding_combo.setFixedWidth(220)
        self.encoding_combo.currentIndexChanged.connect(self._on_encoding_changed)
        grid.addWidget(self.encoding_combo, 0, 1)

        grid.addWidget(BodyLabel("最大显示行数", card), 1, 0)
        self.max_lines_spin = SpinBox(card)
        self.max_lines_spin.setRange(MIN_DISPLAY_LINES, MAX_DISPLAY_LINES)
        self.max_lines_spin.setSingleStep(1000)
        self.max_lines_spin.setSuffix(" 行")
        self.max_lines_spin.setFixedWidth(220)
        self.max_lines_spin.setToolTip("收发区最多保留的显示行数；会话记录不受此限制")
        self.max_lines_spin.valueChanged.connect(self._on_max_lines_changed)
        grid.addWidget(self.max_lines_spin, 1, 1)

        grid.addWidget(BodyLabel("自动滚动", card), 2, 0)
        self.auto_scroll_switch = SwitchButton(card)
        self.auto_scroll_switch.setText("有新数据时滚动到底部")
        self.auto_scroll_switch.checkedChanged.connect(self._on_auto_scroll_changed)
        grid.addWidget(self.auto_scroll_switch, 2, 1)

        grid.addWidget(BodyLabel("退出确认", card), 3, 0)
        self.confirm_exit_switch = SwitchButton(card)
        self.confirm_exit_switch.setText("仍有连接时先确认")
        self.confirm_exit_switch.setToolTip("关闭窗口时若仍有连接、监听或连续发送，先弹框确认")
        self.confirm_exit_switch.checkedChanged.connect(self._on_confirm_exit_changed)
        grid.addWidget(self.confirm_exit_switch, 3, 1)

        grid.addWidget(BodyLabel("日志字体大小", card), 4, 0)
        self.log_font_spin = SpinBox(card)
        self.log_font_spin.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self.log_font_spin.setSuffix(" pt")
        self.log_font_spin.setFixedWidth(220)
        self.log_font_spin.setToolTip("收发区等宽字体的字号，下次启动应用时生效")
        self.log_font_spin.valueChanged.connect(self._on_font_size_changed)
        grid.addWidget(self.log_font_spin, 4, 1)

        layout.addLayout(grid)
        return card

    def _build_paths_card(self, parent: QWidget) -> QWidget:
        card, layout = section_card(
            "数据目录",
            "配置与日志只写入用户数据目录；不写注册表，不安装服务或驱动。",
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        grid.addWidget(BodyLabel("配置文件", card), 0, 0)
        self.config_path_label = LineEdit(card)
        self.config_path_label.setReadOnly(True)
        self.config_path_label.setClearButtonEnabled(False)
        self.config_path_label.setToolTip("配置文件完整路径；可选中复制")
        grid.addWidget(self.config_path_label, 0, 1)

        grid.addWidget(BodyLabel("日志目录", card), 1, 0)
        self.log_path_label = LineEdit(card)
        self.log_path_label.setReadOnly(True)
        self.log_path_label.setClearButtonEnabled(False)
        self.log_path_label.setToolTip("日志目录完整路径；可选中复制")
        grid.addWidget(self.log_path_label, 1, 1)
        layout.addLayout(grid)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.open_config_button = PushButton("打开配置目录", card)
        self.open_config_button.clicked.connect(self.open_config_dir)
        buttons.addWidget(self.open_config_button)
        self.open_log_button = PushButton("打开日志目录", card)
        self.open_log_button.clicked.connect(self.open_log_dir)
        buttons.addWidget(self.open_log_button)
        self.export_config_button = PushButton("导出配置…", card)
        self.export_config_button.setToolTip("把当前配置导出为 JSON 文件，便于备份或迁移")
        self.export_config_button.clicked.connect(self.export_config)
        buttons.addWidget(self.export_config_button)
        buttons.addStretch(1)
        self.reset_config_button = PushButton("恢复默认设置", card)
        self.reset_config_button.setToolTip("恢复全部默认值并立即保存；不会删除日志文件")
        self.reset_config_button.clicked.connect(self.reset_config)
        buttons.addWidget(self.reset_config_button)
        layout.addLayout(buttons)
        return card

    def _build_update_card(self, parent: QWidget) -> QWidget:
        card, layout = section_card("更新", UPDATE_HINT)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        version_row = QHBoxLayout()
        version_row.setSpacing(8)
        version_row.addWidget(BodyLabel("当前版本", card))
        self.current_version_label = BodyLabel(APP_VERSION, card)
        self.current_version_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        version_row.addWidget(self.current_version_label)
        version_row.addStretch(1)
        self.check_update_button = PushButton("检查更新", card)
        self.check_update_button.setToolTip("立即向 GitHub Releases 查询最新正式版本")
        self.check_update_button.clicked.connect(self.check_updates)
        version_row.addWidget(self.check_update_button)
        grid.addLayout(version_row, 0, 0, 1, 2)

        grid.addWidget(BodyLabel("启动时检查", card), 1, 0)
        self.auto_check_switch = SwitchButton(card)
        self.auto_check_switch.setText("发现新版本时提示")
        self.auto_check_switch.setToolTip(
            "启动后在后台静默检查一次；关闭后启动时不再检查，手动检查仍可用"
        )
        self.auto_check_switch.checkedChanged.connect(self._on_auto_check_changed)
        grid.addWidget(self.auto_check_switch, 1, 1)

        self.update_status_label = CaptionLabel(UPDATE_IDLE_TEXT, card)
        self.update_status_label.setWordWrap(True)
        grid.addWidget(self.update_status_label, 2, 0, 1, 2)

        self.update_notes_edit = QPlainTextEdit(card)
        self.update_notes_edit.setReadOnly(True)
        self.update_notes_edit.setFixedHeight(UPDATE_NOTES_HEIGHT)
        self.update_notes_edit.setPlaceholderText("更新说明")
        self.update_notes_edit.hide()
        grid.addWidget(self.update_notes_edit, 3, 0, 1, 2)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.update_progress = ProgressBar(card)
        self.update_progress.setValue(0)
        self.update_progress.hide()
        action_row.addWidget(self.update_progress, 1)
        self.install_update_button = PrimaryPushButton("下载并安装", card)
        self.install_update_button.setToolTip(
            "下载后校验 SHA-256，确认后关闭程序并自动替换、重新启动"
        )
        self.install_update_button.clicked.connect(self._on_install_clicked)
        self.install_update_button.hide()
        action_row.addWidget(self.install_update_button)
        grid.addLayout(action_row, 4, 0, 1, 2)

        layout.addLayout(grid)
        return card

    def _build_about_card(self, parent: QWidget) -> QWidget:
        card, layout = section_card(
            "关于",
            "独立复刻声明、支持范围与依赖许可；更新只从 GitHub Releases 下载并校验 SHA-256。",
        )
        self.version_label = BodyLabel(f"PortHelper Fluent {APP_VERSION}", card)
        self.version_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.version_label)

        self.statement_label = CaptionLabel(
            "功能参考 PortHelper V1.8.8（温子祺），本项目为独立 Python 复刻；"
            "与原作者无隶属关系，也不冒充原程序发布者。",
            card,
        )
        self.statement_label.setWordWrap(True)
        layout.addWidget(self.statement_label)

        unsupported_lines = "不支持的功能：\n" + "\n".join(
            f"· {name}：{reason}" for name, reason in UNSUPPORTED_ITEMS
        )
        self.unsupported_label = CaptionLabel(unsupported_lines, card)
        self.unsupported_label.setWordWrap(True)
        self.unsupported_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.unsupported_label)

        self.dependencies_label = CaptionLabel(DEPENDENCY_TEXT, card)
        self.dependencies_label.setWordWrap(True)
        self.dependencies_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.dependencies_label)

        self.evidence_label = CaptionLabel(EVIDENCE_TEXT, card)
        self.evidence_label.setWordWrap(True)
        self.evidence_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.evidence_label)
        return card

    # ---- 读取与写入 ---------------------------------------------------

    def refresh_fields(self) -> None:
        """按当前配置刷新所有控件；期间屏蔽信号，避免写回旧值。"""
        previous = self._loading
        self._loading = True
        try:
            theme = str(self._value("ui.theme", "auto")).strip().lower()
            self._select_data(self.theme_combo, theme, "auto")

            self._set_accent_display(self._normalized_accent())

            encoding = str(self._value("ui.default_encoding", "utf-8")).strip().lower()
            self._select_data(self.encoding_combo, encoding, "utf-8")

            self.max_lines_spin.setValue(
                _clamp_int(
                    self._value("ui.max_display_lines", DEFAULT_SETTINGS["ui.max_display_lines"]),
                    MIN_DISPLAY_LINES,
                    MAX_DISPLAY_LINES,
                    int(DEFAULT_SETTINGS["ui.max_display_lines"]),
                )
            )
            self.log_font_spin.setValue(
                _clamp_int(
                    self._value("ui.log_font_size", DEFAULT_SETTINGS["ui.log_font_size"]),
                    MIN_FONT_SIZE,
                    MAX_FONT_SIZE,
                    int(DEFAULT_SETTINGS["ui.log_font_size"]),
                )
            )
            self._set_checked(
                self.auto_scroll_switch,
                _as_bool(self._value("ui.auto_scroll", True), True),
            )
            self._set_checked(
                self.confirm_exit_switch,
                _as_bool(self._value("ui.confirm_exit", True), True),
            )
            self._set_checked(
                self.auto_check_switch,
                _as_bool(self._value("ui.check_updates_on_start", True), True),
            )
            self._refresh_paths()
            self.status_label.setText("修改后自动保存。")
        finally:
            self._loading = previous

    def _refresh_paths(self) -> None:
        if self.paths is None:
            self.config_path_label.setText("未提供数据目录")
            self.log_path_label.setText("未提供日志目录")
            for button in (
                self.open_config_button,
                self.open_log_button,
                self.export_config_button,
                self.reset_config_button,
            ):
                button.setEnabled(False)
            return
        self.config_path_label.setText(str(self.paths.config_file))
        self.log_path_label.setText(str(self.paths.log_dir))
        for button in (
            self.open_config_button,
            self.open_log_button,
            self.export_config_button,
            self.reset_config_button,
        ):
            button.setEnabled(True)

    def _value(self, key: str, default):
        if self.settings is None:
            return default
        try:
            return self.settings.get(key, default)
        except Exception:
            get_logger().exception("读取设置项失败：%s", key)
            return default

    def _save_setting(self, key: str, value, *, apply_now: bool = False) -> bool:
        """写入并保存一个设置项；失败时给出可读提示而不是抛回事件循环。"""
        if self.settings is None:
            return False
        try:
            self.settings.set(key, value)
            self.settings.save()
        except Exception:
            get_logger().exception("保存设置项失败：%s", key)
            show_error(
                self,
                "保存设置失败",
                "无法写入配置文件。请检查数据目录是否存在、是否具有写入权限，"
                "或配置文件是否被其他程序占用。",
            )
            return False
        if apply_now:
            try:
                apply_theme(self.settings)
            except Exception:
                get_logger().exception("应用主题失败")
                show_error(
                    self,
                    "应用主题失败",
                    "主题或强调色未能立即生效，已保留配置；重新打开程序后会自动应用。",
                )
                return False
        self.status_label.setText("设置已保存")
        return True

    # ---- 外观 ---------------------------------------------------------

    @safe_slot
    def _on_theme_changed(self, *_args) -> None:
        if self._loading:
            return
        key = str(self.theme_combo.currentData() or "auto")
        self._save_setting("ui.theme", key, apply_now=True)

    @safe_slot
    def choose_accent(self, *_args) -> None:
        color = QColorDialog.getColor(QColor(self.accent_hex), self, "选择强调色")
        if color.isValid():
            self.apply_accent(color.name())

    def apply_accent(self, color_text: str) -> bool:
        """应用并保存强调色；非法颜色给出提示并返回 False。"""
        color = QColor(str(color_text))
        if not color.isValid():
            show_error(
                self,
                "强调色无效",
                f"“{color_text}”不是有效的颜色值，请选择颜色或输入 #RRGGBB。",
            )
            return False
        value = color.name().upper()
        self._set_accent_display(value)
        return self._save_setting("ui.accent", value, apply_now=True)

    def _set_accent_display(self, color_text: str) -> None:
        color = QColor(str(color_text))
        if not color.isValid():
            color = QColor(DEFAULT_ACCENT)
        self.accent_hex = color.name().upper()
        self.accent_label.setText(self.accent_hex)
        self.accent_swatch.setStyleSheet(
            f"QFrame {{ background: {self.accent_hex};"
            " border: 1px solid rgba(120, 120, 120, 0.55);"
            " border-radius: 5px; }}"
        )
        self.accent_label.setToolTip(f"当前强调色 {self.accent_hex}")

    def _normalized_accent(self) -> str:
        raw = str(self._value("ui.accent", DEFAULT_ACCENT)).strip() or DEFAULT_ACCENT
        color = QColor(raw)
        return color.name().upper() if color.isValid() else DEFAULT_ACCENT

    # ---- 行为 ---------------------------------------------------------

    @safe_slot
    def _on_encoding_changed(self, *_args) -> None:
        if self._loading:
            return
        codec = str(self.encoding_combo.currentData() or "utf-8")
        self._save_setting("ui.default_encoding", codec)

    @safe_slot
    def _on_max_lines_changed(self, value: int) -> None:
        if self._loading:
            return
        self._save_setting("ui.max_display_lines", int(value))

    @safe_slot
    def _on_auto_scroll_changed(self, checked: bool) -> None:
        if self._loading:
            return
        self._save_setting("ui.auto_scroll", bool(checked))

    @safe_slot
    def _on_confirm_exit_changed(self, checked: bool) -> None:
        if self._loading:
            return
        self._save_setting("ui.confirm_exit", bool(checked))

    @safe_slot
    def _on_font_size_changed(self, value: int) -> None:
        if self._loading:
            return
        self._save_setting("ui.log_font_size", int(value))

    @safe_slot
    def _on_auto_check_changed(self, checked: bool) -> None:
        if self._loading:
            return
        self._save_setting("ui.check_updates_on_start", bool(checked))

    # ---- 数据目录 -----------------------------------------------------

    @safe_slot
    def open_config_dir(self, *_args) -> None:
        if self.paths is not None:
            self._open_directory(self.paths.config_file.parent, "配置目录")

    @safe_slot
    def open_log_dir(self, *_args) -> None:
        if self.paths is not None:
            self._open_directory(self.paths.log_dir, "日志目录")

    def _open_directory(self, directory: Path, label: str) -> None:
        path = Path(directory)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            get_logger().exception("创建目录失败：%s", path)
            show_error(self, f"无法打开{label}", f"目录不可用：{path}（{exc}）")
            return
        try:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception:
            get_logger().exception("打开目录失败：%s", path)
            opened = False
        if not opened:
            show_warning(
                self,
                f"无法打开{label}",
                f"系统未能打开 {path}。请手动在资源管理器中打开该路径。",
            )

    @safe_slot
    def export_config(self, *_args) -> None:
        if self.settings is None or self.paths is None:
            return
        default_name = self.paths.data_dir / "config-export.json"
        path_text, _selected = QFileDialog.getSaveFileName(
            self,
            "导出配置",
            str(default_name),
            "JSON 文件 (*.json)",
        )
        if not path_text:
            return
        path = Path(path_text)
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        try:
            payload = json.dumps(self.settings.as_dict(), ensure_ascii=False, indent=2)
            write_text_atomic(path, payload)
        except OSError as exc:
            get_logger().exception("导出配置失败：%s", path)
            show_error(
                self,
                "导出配置失败",
                f"无法写入 {path}（{exc}）。请确认目录存在且具有写入权限。",
            )
            return
        show_success(self, "配置已导出", str(path))

    @safe_slot
    def reset_config(self, *_args) -> None:
        if self.settings is None:
            return
        answer = QMessageBox.question(
            self,
            "恢复默认设置",
            "将恢复全部默认设置并立即保存。\n"
            "配置文件与日志文件不会被删除，但当前自定义参数会丢失。确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.settings.reset()
            self.settings.save()
        except Exception:
            get_logger().exception("重置配置失败")
            show_error(
                self,
                "恢复默认设置失败",
                "无法写入配置文件。请检查数据目录权限或文件占用情况后重试。",
            )
            return
        try:
            apply_theme(self.settings)
        except Exception:
            get_logger().exception("重置后应用主题失败")
        self.refresh_fields()
        self.status_label.setText("已恢复默认设置")
        show_success(self, "已恢复默认设置", "所有选项已回到默认值。")

    # ---- 更新 ---------------------------------------------------------

    @safe_slot
    def check_updates(self, *_args) -> None:
        """手动检查最新 Release；所有网络都在后台线程里完成。"""
        if _worker_running(self._check_worker):
            return
        self.check_update_button.setEnabled(False)
        self.update_status_label.setText("正在检查…")
        worker = UpdateCheckWorker()
        worker.checked.connect(self._on_update_checked)
        worker.failed.connect(self._on_update_failed)
        worker.finished.connect(self._on_check_finished)
        worker.finished.connect(worker.deleteLater)
        self._check_worker = worker
        worker.start()

    @safe_slot
    def _on_check_finished(self, *_args) -> None:
        self._check_worker = None
        self.check_update_button.setEnabled(True)

    @safe_slot
    def _on_update_checked(self, release, manual: bool = True) -> None:
        """展示检查结果：有更新时给出说明与安装按钮，否则显示已是最新。"""
        version = str(getattr(release, "version", "") or "").strip()
        if version and is_newer(version):
            self._release = release
            get_logger().info(
                "检查更新：发现新版本 %s（%s）", version, "手动" if manual else "启动"
            )
            self.update_status_label.setText(
                f"发现新版本 {version}（{_release_size(release)}，{_release_date(release)}）"
            )
            notes = str(getattr(release, "notes", "") or "").strip()
            self.update_notes_edit.setPlainText(notes[:UPDATE_NOTES_MAX_CHARS])
            self.update_notes_edit.show()
            self.install_update_button.setEnabled(True)
            self.install_update_button.show()
            return
        self._release = None
        self.update_notes_edit.hide()
        self.install_update_button.hide()
        self.update_progress.setValue(0)
        self.update_progress.hide()
        self.update_status_label.setText(f"已是最新版本（{APP_VERSION}）。")

    @safe_slot
    def _on_update_failed(self, message, manual: bool = True) -> None:
        text = str(message or "").strip() or "未知错误"
        # 失败时清掉上一次的更新结果，避免旧说明与安装按钮残留。
        self._release = None
        self.update_notes_edit.clear()
        self.update_notes_edit.hide()
        self.install_update_button.hide()
        self.update_status_label.setText(f"无法检查更新：{text}")
        if manual:
            show_warning(self, "检查更新失败", text)
        else:
            get_logger().warning("启动检查更新失败：%s", text)

    @safe_slot
    def _on_install_clicked(self, *_args) -> None:
        """下载前先确认能保存、能替换，避免留下无法安装的下载文件。"""
        if _worker_running(self._download_worker):
            return
        release = self._release
        if release is None:
            show_warning(self, "没有可安装的更新", "请先点击“检查更新”。")
            return
        if self.paths is None:
            show_error(self, "无法自动安装", "未提供数据目录，无法保存更新文件。")
            return
        frozen = bool(getattr(sys, "frozen", False))
        target = Path(sys.executable)
        if frozen and not can_replace(target):
            show_error(
                self,
                "无法自动安装",
                f"程序目录不可写：{target.parent}。"
                f"请把程序移到有写入权限的目录，或手动替换 {target.name}。",
            )
            return
        try:
            work_dir = updates_dir(self.paths)
            prune_updates(work_dir)
        except OSError as exc:
            get_logger().exception("准备更新目录失败")
            show_error(self, "无法自动安装", f"更新目录不可用：{exc}")
            return
        self._work_dir = work_dir
        self._manual_install = not frozen
        # 快照本次要安装的 release：下载期间若又检查到新版本，确认框仍按用户点击时
        # 看到的版本来描述。
        self._pending_release = release
        self.update_progress.setValue(0)
        self.update_progress.show()
        self.install_update_button.setEnabled(False)
        self.check_update_button.setEnabled(False)
        self.update_status_label.setText("正在下载更新…")
        worker = UpdateDownloadWorker(release, work_dir)
        worker.progress.connect(self._on_download_progress)
        worker.downloaded.connect(self._on_downloaded)
        worker.failed.connect(self._on_download_failed)
        worker.finished.connect(self._on_download_finished)
        worker.finished.connect(worker.deleteLater)
        self._download_worker = worker
        worker.start()

    @safe_slot
    def _on_download_finished(self, *_args) -> None:
        """下载线程结束（无论成败）：清引用并恢复检查按钮。"""
        self._download_worker = None
        self.check_update_button.setEnabled(True)

    @safe_slot
    def _on_download_progress(self, received: int, total: int) -> None:
        received = max(0, int(received))
        total = max(0, int(total))
        if total > 0:
            percent = max(0, min(100, int(received * 100 / total)))
            self.update_progress.setValue(percent)
            self.update_status_label.setText(f"正在下载更新：{percent}%")
            return
        self.update_status_label.setText(
            f"正在下载更新：已下载 {_format_size(received)}"
        )

    @safe_slot
    def _on_download_failed(self, message) -> None:
        text = str(message or "").strip() or "未知错误"
        self.update_progress.setValue(0)
        self.update_progress.hide()
        self.install_update_button.setEnabled(True)
        self.update_status_label.setText(f"下载更新失败：{text}")
        show_error(self, "下载更新失败", text)

    @safe_slot
    def _on_downloaded(self, path) -> None:
        """下载校验通过：源码运行只提示手动替换，打包运行确认后安装并重启。"""
        self.update_progress.setValue(0)
        self.update_progress.hide()
        downloaded = Path(path)
        self.install_update_button.setEnabled(True)
        if self._manual_install:
            self.update_status_label.setText(f"更新包已下载：{downloaded}")
            show_warning(self, "源码运行模式", "已下载后请手动替换打包后的 exe。")
            self._open_directory(downloaded.parent, "更新目录")
            return
        version = str(getattr(self._pending_release, "version", "") or "")
        answer = QMessageBox.question(
            self,
            UPDATE_INSTALL_CONFIRM_TITLE,
            UPDATE_INSTALL_QUESTION.format(version=version),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.update_status_label.setText(f"更新已下载，稍后可重新安装：{downloaded}")
            return
        self._install_and_restart(downloaded)

    def _install_and_restart(self, downloaded: Path) -> None:
        """启动替换脚本并让主窗口跳过退出确认关闭；失败只提示不改窗口状态。"""
        try:
            work_dir = self._work_dir or updates_dir(self.paths)
            install_update(downloaded, Path(sys.executable), work_dir)
        except Exception as exc:
            get_logger().exception("启动更新安装失败")
            show_error(self, "安装更新失败", readable_message(exc))
            return
        prepare = getattr(self.window(), "prepare_for_update", None)
        if not callable(prepare):
            show_error(
                self,
                "安装更新失败",
                "无法关闭主窗口以完成替换，请手动退出程序后运行更新脚本。",
            )
            return
        prepare()

    # ---- 生命周期 -----------------------------------------------------

    def has_active_work(self) -> bool:
        return False

    def stop_workers(self) -> None:
        """中断并等待更新检查/下载线程，供主窗口关闭时统一调用。"""
        for worker in (self._check_worker, self._download_worker):
            if worker is None:
                continue
            try:
                worker.requestInterruption()
                worker.wait(2000)
            except Exception:
                get_logger().exception("停止更新线程失败")
        self._check_worker = None
        self._download_worker = None

    # ---- 工具 ---------------------------------------------------------

    @staticmethod
    def _select_data(combo: ComboBox, value: str, fallback: str) -> None:
        index = combo.findData(value)
        if index < 0:
            index = combo.findData(fallback)
        combo.setCurrentIndex(max(0, index))

    @staticmethod
    def _set_checked(widget, checked: bool) -> None:
        blocked = widget.blockSignals(True)
        try:
            widget.setChecked(bool(checked))
        finally:
            widget.blockSignals(blocked)


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return default


def _clamp_int(value, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def _worker_running(worker) -> bool:
    """线程是否仍在运行；测试注入的假 worker 不一定实现 isRunning()。"""
    is_running = getattr(worker, "isRunning", None)
    if not callable(is_running):
        return worker is not None
    try:
        return bool(is_running())
    except Exception:
        return False


def _format_size(size: int) -> str:
    if size <= 0:
        return "大小未知"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / 1024:.0f} KB"


def _release_size(release) -> str:
    exe = getattr(release, "exe", None)
    try:
        return _format_size(int(getattr(exe, "size", 0) or 0))
    except (TypeError, ValueError):
        return "大小未知"


def _release_date(release) -> str:
    text = str(getattr(release, "published_at", "") or "").strip()
    return text[:10] if text else "日期未知"

"""USB HID 调试页：标准 HID 枚举、Input/Output Report 收发与会话记录。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QSplitter,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    FluentIcon,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
    StrongBodyLabel,
    TabWidget,
    TableWidget,
    TitleLabel,
    ToolButton,
)

from app.core.encoding import parse_hex
from app.core.logging_setup import get_logger
from app.core.settings import Settings
from app.io.hid_worker import (
    HidBackend,
    HidDeviceInfo,
    HidWorker,
    HidapiBackend,
    build_report,
    list_devices,
)
from app.services.paths import AppPaths
from app.services.session_log import SessionLog
from app.ui.dialogs.session_dialog import SessionDialog
from app.ui.feedback import show_error, show_success, show_warning
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.log_pane import LogPane
from app.ui.widgets.status_chip import StatusChip

SCOPE_NOTICE = "本程序支持标准 HID；CH372 需要厂商 DLL，不在支持范围"
REPORT_HINT = "报告 ID 为 0 表示无编号报告；非 0 时首字节为报告 ID。"
LENGTH_HINT = "按报告长度填充（补 0x00）或截断，范围 1–1024 字节。"
TABLE_COLUMNS = ("VID:PID", "产品", "厂商", "序列号", "Usage Page / Usage", "接口")
FILE_TIMER_MS = 5
FILE_REPORTS_PER_TICK = 8


class HidPage(QWidget):
    """HID 工作台；hidapi 阻塞读写由 HidWorker 线程承担。"""

    def __init__(
        self,
        settings: Settings,
        paths: AppPaths,
        parent: QWidget | None = None,
        *,
        backend: HidBackend | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("hid")
        self.settings = settings
        self.paths = paths
        self.extra_options = dict(kwargs)
        self.backend = backend if backend is not None else HidapiBackend()
        self._uses_default_backend = backend is None
        self._worker = HidWorker(self.backend, self)
        self._devices: list[HidDeviceInfo] = []
        self._loading = True
        self._error_pending = False
        self._scope_notice_shown = False
        self._file_handle: BinaryIO | None = None
        self._file_name = ""
        self._file_size = 0
        self._file_sent = 0
        self._file_report_id = 0
        self._file_report_length = 64
        self._file_chunk = 64

        self._repeat_timer = QTimer(self)
        self._repeat_timer.setInterval(1000)
        self._repeat_timer.timeout.connect(self._send_repeat)

        self._file_timer = QTimer(self)
        self._file_timer.setInterval(FILE_TIMER_MS)
        self._file_timer.timeout.connect(self._send_file_chunk)

        self._build_ui()
        self._connect_worker()
        self._restore_settings()
        self.refresh_devices()
        self._loading = False

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(TitleLabel("USB HID", self))
        title_box.addWidget(CaptionLabel("标准 HID 设备枚举、Input Report 接收与 Output Report 发送", self))
        header.addLayout(title_box)
        header.addStretch(1)
        self.status_chip = StatusChip(self)
        self.status_chip.set_state("idle", "未连接")
        header.addWidget(self.status_chip)
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_column())
        splitter.addWidget(self._build_right_column())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([470, 760])
        root.addWidget(splitter, 1)

    def _build_left_column(self) -> QWidget:
        left = QWidget(self)
        left.setMinimumWidth(430)
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        device_card, device_layout = section_card(
            "设备列表", "通过 hidapi 枚举标准 HID 接口；同一设备可能有多个接口"
        )
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search_edit = LineEdit(device_card)
        self.search_edit.setPlaceholderText("搜索 VID/PID 或产品名…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_changed)
        search_row.addWidget(self.search_edit, 1)
        self.refresh_button = ToolButton(device_card)
        self.refresh_button.setIcon(FluentIcon.SYNC)
        self.refresh_button.setToolTip("刷新 HID 设备列表 (F5)")
        self.refresh_button.clicked.connect(self.refresh_devices)
        search_row.addWidget(self.refresh_button)
        device_layout.addLayout(search_row)

        self.device_table = TableWidget(device_card)
        self.device_table.setColumnCount(len(TABLE_COLUMNS))
        self.device_table.setHorizontalHeaderLabels(list(TABLE_COLUMNS))
        self.device_table.verticalHeader().setVisible(False)
        self.device_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.device_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.device_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.device_table.setAlternatingRowColors(True)
        self.device_table.setWordWrap(False)
        self.device_table.setSortingEnabled(False)
        self.device_table.setMinimumHeight(190)
        header = self.device_table.horizontalHeader()
        for column in range(len(TABLE_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.device_table.itemSelectionChanged.connect(self._on_selection_changed)
        self.device_table.cellDoubleClicked.connect(self._open_selected)
        device_layout.addWidget(self.device_table, 1)

        self.device_count_label = CaptionLabel("正在枚举 HID 设备…", device_card)
        device_layout.addWidget(self.device_count_label)
        self.device_info_label = CaptionLabel("未选择设备", device_card)
        self.device_info_label.setWordWrap(True)
        device_layout.addWidget(self.device_info_label)

        open_row = QHBoxLayout()
        open_row.setSpacing(8)
        self.open_button = PrimaryPushButton("打开设备", device_card)
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._toggle_connection)
        open_row.addWidget(self.open_button)
        open_row.addStretch(1)
        device_layout.addLayout(open_row)

        self.scope_label = CaptionLabel(SCOPE_NOTICE, device_card)
        self.scope_label.setWordWrap(True)
        device_layout.addWidget(self.scope_label)
        layout.addWidget(device_card, 1)

        report_card, report_layout = section_card(
            "Report 参数", "仅发送 Output Report；接收区显示 Input Report"
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        grid.addWidget(BodyLabel("报告 ID", report_card), 0, 0)
        self.report_id_spin = SpinBox(report_card)
        self.report_id_spin.setRange(0, 255)
        self.report_id_spin.setValue(0)
        self.report_id_spin.setToolTip("0 表示无编号报告；非 0 时作为首字节发送")
        self.report_id_spin.valueChanged.connect(self._on_report_settings_changed)
        grid.addWidget(self.report_id_spin, 0, 1)
        grid.addWidget(BodyLabel("报告长度", report_card), 1, 0)
        self.report_length_spin = SpinBox(report_card)
        self.report_length_spin.setRange(1, 1024)
        self.report_length_spin.setValue(64)
        self.report_length_spin.setSuffix(" B")
        self.report_length_spin.valueChanged.connect(self._on_report_settings_changed)
        grid.addWidget(self.report_length_spin, 1, 1)
        report_layout.addLayout(grid)

        report_id_hint = CaptionLabel(REPORT_HINT, report_card)
        report_id_hint.setWordWrap(True)
        report_layout.addWidget(report_id_hint)
        length_hint = CaptionLabel(LENGTH_HINT, report_card)
        length_hint.setWordWrap(True)
        report_layout.addWidget(length_hint)
        self.report_hint_label = StrongBodyLabel("", report_card)
        self.report_hint_label.setWordWrap(True)
        report_layout.addWidget(self.report_hint_label)
        layout.addWidget(report_card)

        action_card, action_layout = section_card(
            "会话与计数", "计数按实际发送的报告长度统计，不按输入文本长度"
        )
        self.counter_label = StrongBodyLabel("接收 0 B · 发送 0 B", action_card)
        action_layout.addWidget(self.counter_label)
        action_grid = QGridLayout()
        action_grid.setHorizontalSpacing(8)
        action_grid.setVerticalSpacing(8)
        self.clear_log_button = PushButton("清空当前日志", action_card)
        self.clear_log_button.clicked.connect(self._clear_current_log)
        action_grid.addWidget(self.clear_log_button, 0, 0)
        self.save_log_button = PushButton("保存日志", action_card)
        self.save_log_button.clicked.connect(self._save_current_log)
        action_grid.addWidget(self.save_log_button, 0, 1)
        self.session_button = PushButton("会话记录", action_card)
        self.session_button.clicked.connect(self._open_session)
        action_grid.addWidget(self.session_button, 1, 0)
        self.reset_counter_button = PushButton("重新计数", action_card)
        self.reset_counter_button.clicked.connect(self._reset_counters)
        action_grid.addWidget(self.reset_counter_button, 1, 1)
        action_layout.addLayout(action_grid)
        layout.addWidget(action_card)
        return left

    def _build_right_column(self) -> QWidget:
        right = QWidget(self)
        layout = QVBoxLayout(right)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        log_card = CardWidget(self)
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(12, 12, 12, 12)
        log_layout.setSpacing(8)
        self.log_tabs = TabWidget(log_card)
        self.rx_log = LogPane("接收 Report", show_direction=False, settings=self.settings, parent=self.log_tabs)
        self.tx_log = LogPane("发送记录", show_direction=False, settings=self.settings, parent=self.log_tabs)
        self.rx_log.log_view.setPlaceholderText("HID Input Report 会显示在这里")
        self.tx_log.log_view.setPlaceholderText("已发送的 Output Report 会显示在这里")
        self.log_tabs.addTab(self.rx_log, "接收 Report", FluentIcon.DOWNLOAD)
        self.log_tabs.addTab(self.tx_log, "发送 Report", FluentIcon.SEND)
        log_layout.addWidget(self.log_tabs, 1)
        layout.addWidget(log_card, 3)

        send_card, send_layout = section_card(
            "发送区", "HEX 输入支持空格、逗号与 0x 前缀；Ctrl+Enter 发送"
        )
        self.send_editor = PlainTextEdit(send_card)
        self.send_editor.setPlaceholderText("输入要发送的 HEX 数据，例如：01 02 03 04…")
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.send_editor.setFont(font)
        self.send_editor.setMinimumHeight(90)
        self.send_editor.setMaximumHeight(170)
        send_layout.addWidget(self.send_editor)

        options = QHBoxLayout()
        options.setSpacing(12)
        self.hex_send_check = CheckBox("HEX 发送", send_card)
        self.hex_send_check.setChecked(True)
        self.hex_send_check.setToolTip("取消勾选时按 UTF-8 文本编码发送")
        options.addWidget(self.hex_send_check)
        self.repeat_check = CheckBox("连续发送", send_card)
        self.repeat_check.setEnabled(False)
        self.repeat_check.toggled.connect(self._on_repeat_toggled)
        options.addWidget(self.repeat_check)
        self.interval_spin = SpinBox(send_card)
        self.interval_spin.setRange(10, 60000)
        self.interval_spin.setValue(1000)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.valueChanged.connect(self._on_interval_changed)
        options.addWidget(self.interval_spin)
        options.addStretch(1)
        send_layout.addLayout(options)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.send_button = PrimaryPushButton("发送 Report", send_card)
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self._send_current)
        buttons.addWidget(self.send_button)
        self.send_file_button = PushButton("发送文件", send_card)
        self.send_file_button.setEnabled(False)
        self.send_file_button.clicked.connect(self._choose_file)
        buttons.addWidget(self.send_file_button)
        self.cancel_file_button = PushButton("取消文件", send_card)
        self.cancel_file_button.setEnabled(False)
        self.cancel_file_button.clicked.connect(self._cancel_file_send)
        buttons.addWidget(self.cancel_file_button)
        buttons.addStretch(1)
        send_layout.addLayout(buttons)
        self.file_progress_label = CaptionLabel("", send_card)
        self.file_progress_label.setWordWrap(True)
        send_layout.addWidget(self.file_progress_label)
        layout.addWidget(send_card, 2)

        self._shortcut_send = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._shortcut_send.activated.connect(self._send_current)
        self._shortcut_send2 = QShortcut(QKeySequence("Ctrl+Enter"), self)
        self._shortcut_send2.activated.connect(self._send_current)
        self._shortcut_refresh = QShortcut(QKeySequence("F5"), self)
        self._shortcut_refresh.activated.connect(self.refresh_devices)
        self._shortcut_clear = QShortcut(QKeySequence("Ctrl+K"), self)
        self._shortcut_clear.activated.connect(self._clear_current_log)
        self._shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self._shortcut_save.activated.connect(self._save_current_log)
        return right

    # ---- 信号与设置 ---------------------------------------------------

    def _connect_worker(self) -> None:
        self._worker.opened.connect(self._on_opened)
        self._worker.closed.connect(self._on_closed)
        self._worker.data_received.connect(self._on_data_received)
        self._worker.error.connect(self._on_error)

    def _restore_settings(self) -> None:
        self.report_id_spin.setValue(
            _clamp_int(self.settings.get("hid.report_id", 0), 0, 255, 0)
        )
        self.report_length_spin.setValue(
            _clamp_int(self.settings.get("hid.report_length", 64), 1, 1024, 64)
        )
        for pane in (self.rx_log, self.tx_log):
            pane.set_hex_mode(True)
        self._update_report_hint()

    def _save_settings(self) -> None:
        if self._loading:
            return
        try:
            self.settings.update(
                {
                    "hid.report_id": self.report_id_spin.value(),
                    "hid.report_length": self.report_length_spin.value(),
                }
            )
            self.settings.save()
        except Exception:
            get_logger().exception("保存 HID 设置失败")

    def _last_device_path(self) -> bytes:
        text = str(self.settings.get("hid.last_device_path", "") or "").strip()
        if not text:
            return b""
        try:
            return bytes.fromhex(text)
        except ValueError:
            return b""

    def _remember_device(self, info: HidDeviceInfo) -> None:
        try:
            self.settings.set("hid.last_device_path", info.path.hex())
            self.settings.save()
        except Exception:
            get_logger().exception("保存上次 HID 设备失败")

    @safe_slot
    def _on_report_settings_changed(self, *_args) -> None:
        self._update_report_hint()
        self._save_settings()

    def _update_report_hint(self) -> None:
        report_id = self.report_id_spin.value()
        length = self.report_length_spin.value()
        if report_id:
            data_length = max(0, length - 1)
            self.report_hint_label.setText(
                f"本次发送：首字节 0x{report_id:02X}，数据区 {data_length} B，报告总长 {length} B。"
            )
        else:
            self.report_hint_label.setText(
                f"本次发送：无报告 ID，数据区 {length} B；不足补 0x00，超出截断。"
            )

    # ---- 设备枚举与选择 -----------------------------------------------

    @safe_slot
    def refresh_devices(self) -> None:
        """重新枚举并把上次/当前选择的接口重新选中。"""
        if self._worker.is_open():
            return
        previous = self._selected_info()
        preferred_path = previous.path if previous is not None else self._last_device_path()
        try:
            devices = (
                list_devices()
                if self._uses_default_backend
                else list(self.backend.enumerate())
            )
        except Exception:
            get_logger().exception("枚举 USB HID 设备失败")
            devices = []
        self._devices = [info for info in devices if isinstance(info, HidDeviceInfo)]

        table = self.device_table
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            table.clearContents()
            table.setRowCount(len(self._devices))
            for row, info in enumerate(self._devices):
                self._fill_row(row, info)
        finally:
            table.blockSignals(False)
            table.setUpdatesEnabled(True)

        self._apply_filter()
        target = self._row_for_path(preferred_path)
        if target < 0 or table.isRowHidden(target):
            target = self._first_visible_row()
        if target >= 0:
            table.selectRow(target)
            table.scrollToItem(table.item(target, 0))
        else:
            table.clearSelection()
        self._on_selection_changed()

    def _fill_row(self, row: int, info: HidDeviceInfo) -> None:
        values = (
            info.id_text,
            info.product or "—",
            info.manufacturer or "—",
            info.serial or "—",
            info.usage_text,
            "" if info.interface < 0 else str(info.interface),
        )
        path_text = info.path.decode("utf-8", errors="replace")
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            item.setToolTip(
                f"路径：{path_text}\nVID:PID {info.id_text} · Usage {info.usage_text} · 接口 {info.interface}"
            )
            if column in (0, 4, 5):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.device_table.setItem(row, column, item)

    @safe_slot
    def _on_search_changed(self, _text: str) -> None:
        self._apply_filter()
        current = self.device_table.currentRow()
        if current < 0 or self.device_table.isRowHidden(current):
            first = self._first_visible_row()
            if first >= 0:
                self.device_table.selectRow(first)
            else:
                self.device_table.clearSelection()
        self._on_selection_changed()

    def _apply_filter(self) -> None:
        needle = self.search_edit.text().strip().lower()
        visible = 0
        for row, info in enumerate(self._devices):
            hidden = not _matches(info, needle)
            self.device_table.setRowHidden(row, hidden)
            if not hidden:
                visible += 1
        total = len(self._devices)
        if total == 0:
            self.device_count_label.setText("未检测到标准 HID 设备；连接设备后点击刷新。")
        elif visible == total:
            self.device_count_label.setText(f"共 {total} 个 HID 接口")
        else:
            self.device_count_label.setText(f"共 {total} 个 HID 接口，筛选后显示 {visible} 个")
        current = self.device_table.currentRow()
        if current >= 0 and self.device_table.isRowHidden(current):
            self.device_table.clearSelection()

    def _row_for_path(self, path: bytes) -> int:
        if not path:
            return -1
        for row, info in enumerate(self._devices):
            if info.path == path:
                return row
        return -1

    def _first_visible_row(self) -> int:
        for row in range(self.device_table.rowCount()):
            if not self.device_table.isRowHidden(row):
                return row
        return -1

    def _selected_info(self) -> HidDeviceInfo | None:
        selected_rows = self.device_table.selectionModel().selectedRows()
        row = selected_rows[0].row() if selected_rows else self.device_table.currentRow()
        if row < 0 or row >= len(self._devices):
            return None
        if self.device_table.isRowHidden(row):
            return None
        return self._devices[row]

    @safe_slot
    def _on_selection_changed(self, *_args) -> None:
        info = self._selected_info()
        if info is None:
            self.device_info_label.setText("未选择设备")
            if not self._worker.is_open():
                self.open_button.setEnabled(False)
            return
        details = [info.id_text, info.display_name]
        if info.manufacturer:
            details.append(info.manufacturer)
        if info.serial:
            details.append(f"SN {info.serial}")
        details.append(f"Usage {info.usage_text}")
        if info.interface >= 0:
            details.append(f"接口 {info.interface}")
        self.device_info_label.setText(" · ".join(details))
        if not self._worker.is_open():
            self.open_button.setEnabled(True)

    # ---- 连接 ---------------------------------------------------------

    @safe_slot
    def _toggle_connection(self) -> None:
        if self._worker.is_open():
            self._close_connection()
        else:
            self._open_selected()

    @safe_slot
    def _open_selected(self, *_args) -> None:
        info = self._selected_info()
        if info is None:
            show_warning(self, "请选择 HID 设备", "先在设备列表中选择一个接口，再点击打开。")
            return
        self.status_chip.set_state("busy", "正在打开…")
        self.open_button.setEnabled(False)
        self._save_settings()
        try:
            self._worker.open_device(info)
        except Exception as exc:
            get_logger().exception("打开 HID 设备异常")
            self._on_error(f"打开 HID 设备失败：{exc}")
        if self.status_chip.state == "busy":
            self.status_chip.set_state("error", "打开失败")
            self.open_button.setEnabled(True)
            self._set_device_controls_enabled(True)

    def _close_connection(self) -> None:
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        self.status_chip.set_state("busy", "正在关闭…")
        self.open_button.setEnabled(False)
        try:
            self._worker.close_device()
        except Exception:
            get_logger().exception("关闭 HID 设备异常")
            self._on_closed()

    @safe_slot
    def _on_opened(self, description: str) -> None:
        self._error_pending = False
        self.status_chip.set_state("ok", f"已打开 {description}")
        self.open_button.setText("关闭设备")
        self.open_button.setEnabled(True)
        self._set_device_controls_enabled(False)
        self._set_send_enabled(True)
        info = self._selected_info()
        if info is not None:
            self._remember_device(info)
        self.rx_log.append_info(
            f"已打开 {description} · Report ID {self.report_id_spin.value()} · "
            f"报告长度 {self.report_length_spin.value()} B"
        )
        if self.repeat_check.isChecked():
            self._start_repeat()

    @safe_slot
    def _on_closed(self) -> None:
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        if self._error_pending:
            self.status_chip.set_state("error", "设备已断开")
        else:
            self.status_chip.set_state("idle", "未连接")
        self.open_button.setText("打开设备")
        self.open_button.setEnabled(self._selected_info() is not None)
        self._set_device_controls_enabled(True)
        self._set_send_enabled(False)
        self.rx_log.append_info("HID 设备已关闭")
        self._error_pending = False

    @safe_slot
    def _on_error(self, message: str) -> None:
        text = str(message)
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        self._error_pending = True
        self.status_chip.set_state("error", "HID 错误")
        self.rx_log.append_info(f"错误：{text}")
        show_error(self, "USB HID 错误", text)
        if not self._worker.is_open():
            self.open_button.setText("打开设备")
            self.open_button.setEnabled(self._selected_info() is not None)
            self._set_device_controls_enabled(True)
            self._set_send_enabled(False)

    def _set_device_controls_enabled(self, enabled: bool) -> None:
        self.search_edit.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self.device_table.setEnabled(enabled)

    def _set_send_enabled(self, enabled: bool) -> None:
        self.send_button.setEnabled(enabled)
        self.send_file_button.setEnabled(enabled)
        self.repeat_check.setEnabled(enabled)

    # ---- 收发 ---------------------------------------------------------

    @safe_slot
    def _on_data_received(self, data: bytes) -> None:
        self.rx_log.append_bytes(bytes(data), "rx")
        self._refresh_counters()

    @safe_slot
    def _send_current(self) -> bool:
        if not self._worker.is_open():
            show_warning(self, "HID 设备未打开", "请先打开一个 HID 设备再发送。")
            self._stop_repeat()
            return False
        try:
            payload = self._build_payload(self.send_editor.toPlainText())
        except ValueError as exc:
            show_error(self, "HEX 格式错误", str(exc))
            self._stop_repeat()
            return False
        if not payload:
            show_warning(self, "没有数据", "请输入要发送的 HEX 数据。")
            return False
        report_id = self.report_id_spin.value()
        report_length = self.report_length_spin.value()
        try:
            report = build_report(payload, report_id, report_length)
        except ValueError as exc:
            show_error(self, "发送参数无效", str(exc))
            self._stop_repeat()
            return False
        if not self._worker.send(payload, report_id, report_length):
            self._stop_repeat()
            return False
        self.tx_log.append_bytes(report, "tx")
        self._refresh_counters()
        return True

    def _build_payload(self, text: str) -> bytes:
        if self.hex_send_check.isChecked():
            return parse_hex(text)
        return text.encode("utf-8")

    @safe_slot
    def _send_repeat(self) -> None:
        if not self._send_current():
            self._stop_repeat()

    def _on_repeat_toggled(self, checked: bool) -> None:
        if checked and self._worker.is_open():
            self._start_repeat()
        elif not checked:
            self._stop_repeat()

    def _start_repeat(self) -> None:
        self._repeat_timer.start(max(10, self.interval_spin.value()))

    def _stop_repeat(self) -> None:
        self._repeat_timer.stop()
        if self.repeat_check.isChecked():
            blocked = self.repeat_check.blockSignals(True)
            try:
                self.repeat_check.setChecked(False)
            finally:
                self.repeat_check.blockSignals(blocked)

    def _on_interval_changed(self, value: int) -> None:
        self._repeat_timer.setInterval(max(10, int(value)))

    # ---- 文件发送 -----------------------------------------------------

    def _report_capacity(self) -> int:
        """一个报告能承载的数据字节数（带报告 ID 时少 1 字节）。"""
        length = self.report_length_spin.value()
        return max(0, length - (1 if self.report_id_spin.value() else 0))

    @safe_slot
    def _choose_file(self) -> None:
        if not self._worker.is_open():
            show_warning(self, "HID 设备未打开", "请先打开 HID 设备再发送文件。")
            return
        capacity = self._report_capacity()
        if capacity <= 0:
            show_warning(
                self,
                "报告长度不足",
                "带报告 ID 时报告长度至少为 2 字节，才能承载文件数据。",
            )
            return
        path_text, _selected = QFileDialog.getOpenFileName(
            self, "选择要发送的文件", str(Path.home()), "所有文件 (*.*)"
        )
        if not path_text:
            return
        path = Path(path_text)
        try:
            handle = path.open("rb")
        except OSError as exc:
            show_error(self, "无法打开文件", f"{path}\n{exc}")
            return
        self._file_handle = handle
        self._file_name = path.name
        self._file_size = path.stat().st_size
        self._file_sent = 0
        self._file_report_id = self.report_id_spin.value()
        self._file_report_length = self.report_length_spin.value()
        self._file_chunk = capacity
        self.send_button.setEnabled(False)
        self.send_file_button.setEnabled(False)
        self.cancel_file_button.setEnabled(True)
        self._update_file_label()
        self._file_timer.start()

    def _send_file_chunk(self) -> None:
        """每个定时器周期最多发送若干报告，避免 5 ms 只发一个字节导致大文件过慢。"""
        for _ in range(FILE_REPORTS_PER_TICK):
            if self._file_handle is None:
                self._stop_file_timer()
                return
            try:
                chunk = self._file_handle.read(self._file_chunk)
            except OSError as exc:
                show_error(self, "读取文件失败", str(exc))
                self._cancel_file_send()
                return
            if not chunk:
                self._finish_file_send()
                return
            report = build_report(chunk, self._file_report_id, self._file_report_length)
            if not self._worker.send(chunk, self._file_report_id, self._file_report_length):
                self._cancel_file_send()
                return
            self.tx_log.append_bytes(report, "tx")
            self._file_sent += len(chunk)
            self._refresh_counters()
            self._update_file_label()

    def _finish_file_send(self) -> None:
        name = self._file_name
        sent = self._file_sent
        self._close_file_handle()
        self._stop_file_timer()
        self.file_progress_label.setText(f"文件发送完成：{name}（{sent} B）")
        self._set_file_buttons_idle()
        show_success(self, "文件发送完成", name)

    @safe_slot
    def _cancel_file_send(self, silent: bool = False) -> None:
        was_active = self._file_handle is not None
        self._close_file_handle()
        self._stop_file_timer()
        if was_active and not silent:
            self.file_progress_label.setText(f"文件发送已取消：{self._file_name}")
        self._set_file_buttons_idle()

    def _close_file_handle(self) -> None:
        if self._file_handle is not None:
            try:
                self._file_handle.close()
            except OSError:
                get_logger().exception("关闭 HID 发送文件句柄失败")
            finally:
                self._file_handle = None

    def _stop_file_timer(self) -> None:
        self._file_timer.stop()

    def _set_file_buttons_idle(self) -> None:
        open_now = self._worker.is_open()
        self.send_button.setEnabled(open_now)
        self.send_file_button.setEnabled(open_now)
        self.cancel_file_button.setEnabled(False)

    def _update_file_label(self) -> None:
        self.file_progress_label.setText(
            f"{self._file_name}：{self._file_sent} / {self._file_size} B"
            f"（每个 Report 数据区 {self._file_chunk} B）"
        )

    # ---- 会话 ---------------------------------------------------------

    @safe_slot
    def _open_session(self) -> None:
        dialog = SessionDialog(
            self._combined_session(),
            self,
            codec="utf-8",
            default_export_dir=_default_export_dir(self.paths),
        )
        dialog.exec()

    def _combined_session(self) -> SessionLog:
        records = list(self.rx_log.session().records()) + list(self.tx_log.session().records())
        records.sort(key=lambda item: item.timestamp)
        total = max(sum(len(item.data) for item in records), 1)
        combined = SessionLog(max_bytes=total, max_records=max(len(records), 1))
        for record in records:
            combined.add(record)
        return combined

    def _refresh_counters(self) -> None:
        rx = self.rx_log.session().total_bytes("rx")
        tx = self.tx_log.session().total_bytes("tx")
        self.counter_label.setText(f"接收 {_format_bytes(rx)} · 发送 {_format_bytes(tx)}")

    @safe_slot
    def _reset_counters(self) -> None:
        self.rx_log.session().reset_counters()
        self.tx_log.session().reset_counters()
        self._refresh_counters()

    def _current_log(self) -> LogPane:
        widget = self.log_tabs.currentWidget()
        return widget if isinstance(widget, LogPane) else self.rx_log

    @safe_slot
    def _clear_current_log(self) -> None:
        self._current_log().clear()

    @safe_slot
    def _save_current_log(self) -> None:
        pane = self._current_log()
        default_name = f"hid-{datetime.now():%Y%m%d-%H%M%S}.txt"
        path_text, _selected = QFileDialog.getSaveFileName(
            self, "保存 HID 会话日志", default_name, "文本文件 (*.txt);;CSV 文件 (*.csv)"
        )
        if not path_text:
            return
        path = Path(path_text)
        try:
            if path.suffix.lower() == ".csv":
                pane.save_csv(path)
            else:
                if path.suffix.lower() != ".txt":
                    path = path.with_suffix(".txt")
                pane.save_txt(path)
        except OSError as exc:
            show_error(self, "保存失败", str(exc))
            return
        show_success(self, "日志已保存", str(path))

    # ---- 生命周期 -----------------------------------------------------

    def has_active_work(self) -> bool:
        return bool(
            self._worker.is_open()
            or self._repeat_timer.isActive()
            or self._file_timer.isActive()
        )

    def stop_workers(self) -> None:
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        try:
            self._worker.close_device()
            self._worker.wait(2000)
        except Exception:
            get_logger().exception("停止 HID 工作时失败")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._scope_notice_shown:
            return
        self._scope_notice_shown = True
        try:
            show_warning(self, "支持范围", SCOPE_NOTICE)
        except Exception:
            get_logger().exception("显示 HID 支持范围提示失败")

    def closeEvent(self, event) -> None:
        self._save_settings()
        self.stop_workers()
        super().closeEvent(event)


def _matches(info: HidDeviceInfo, needle: str) -> bool:
    """搜索匹配 VID/PID（十六进制/十进制）与产品名；多关键词按 AND 组合。"""
    if not needle:
        return True
    values = (
        info.id_text.lower(),
        f"{info.vendor_id:04x}:{info.product_id:04x}",
        f"{info.vendor_id}:{info.product_id}",
        info.product.lower(),
        info.manufacturer.lower(),
        info.serial.lower(),
    )
    tokens = needle.split()
    return all(any(token in value for value in values) for token in tokens)


def _clamp_int(value, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.2f} MB"


def _default_export_dir(paths: AppPaths | None) -> Path:
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()

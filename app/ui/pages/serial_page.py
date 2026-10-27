"""串口调试页：参数配置、收发日志、线路控制、多项发送与会话记录。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    EditableComboBox,
    FluentIcon,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
    StrongBodyLabel,
    SwitchButton,
    TabWidget,
    TitleLabel,
    ToolButton,
)

from app.core.encoding import CODEC_CHOICES, encode_text, parse_hex, resolve_codec
from app.core.logging_setup import get_logger
from app.core.settings import Settings, resolve_page_encoding
from app.io.serial_worker import PortInfo, SerialConfig, SerialWorker, list_ports
from app.services.paths import AppPaths
from app.services.session_log import SessionLog
from app.ui.dialogs.multi_send_dialog import MultiSendDialog
from app.ui.dialogs.session_dialog import SessionDialog
from app.ui.feedback import show_error, show_success, show_warning
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.log_pane import LogPane
from app.ui.widgets.status_chip import StatusChip

BAUD_CHOICES = ("300", "1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600")
PARITY_ITEMS = (("无校验", "none"), ("奇校验", "odd"), ("偶校验", "even"), ("Mark", "mark"), ("Space", "space"))
FLOW_ITEMS = (("无流控", "none"), ("RTS/CTS", "rtscts"), ("XON/XOFF", "xonxoff"))
ENCODING_ITEMS = (("ANSI（系统）", "ansi"), ("GBK", "gbk"), ("UTF-8", "utf-8"))
NEWLINE_ITEMS = (("不追加", "none"), ("CR", "cr"), ("LF", "lf"), ("CRLF", "crlf"))
NEWLINE_BYTES = {"none": b"", "cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}
FILE_CHUNK = 8192


class SerialPage(QWidget):
    """串口工作台；所有串口阻塞 I/O 由 SerialWorker 线程承担。"""

    def __init__(
        self,
        settings: Settings,
        paths: AppPaths,
        parent: QWidget | None = None,
        *,
        worker_factory: Callable[..., object] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("serial")
        self.settings = settings
        self.paths = paths
        self.worker_factory = worker_factory
        self.extra_options = dict(kwargs)
        self._worker_factory = worker_factory if callable(worker_factory) else SerialWorker
        self._worker = None
        self._loading = True
        self._ports_by_device: dict[str, PortInfo] = {}
        self._file_handle: BinaryIO | None = None
        self._file_name = ""
        self._file_size = 0
        self._file_sent = 0
        self._file_awaiting_ack = False
        self._file_pending_chunk = b""

        self._repeat_timer = QTimer(self)
        self._repeat_timer.setInterval(1000)
        self._repeat_timer.timeout.connect(self._send_repeat)

        self._build_ui()
        self._create_worker()
        self._restore_settings()
        self.refresh_ports()
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
        title_box.addWidget(TitleLabel("串口调试", self))
        title_box.addWidget(CaptionLabel("串口收发、线路控制、多项发送与会话记录", self))
        header.addLayout(title_box)
        header.addStretch(1)
        self.status_chip = StatusChip(self)
        header.addWidget(self.status_chip)
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_column())
        splitter.addWidget(self._build_right_column())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([350, 890])
        root.addWidget(splitter, 1)

    def _build_left_column(self) -> QWidget:
        left = QWidget(self)
        left.setMinimumWidth(320)
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        conn_card, conn_layout = section_card("连接参数", "串口帧格式与字符编码")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.addWidget(BodyLabel("端口", conn_card), 0, 0)
        self.port_combo = EditableComboBox(conn_card)
        self.port_combo.setPlaceholderText("COM3，可直接输入")
        self.port_combo.setMinimumWidth(170)
        grid.addWidget(self.port_combo, 0, 1)
        self.refresh_button = ToolButton(conn_card)
        self.refresh_button.setIcon(FluentIcon.SYNC)
        self.refresh_button.setToolTip("刷新串口列表 (F5)")
        self.refresh_button.clicked.connect(self.refresh_ports)
        grid.addWidget(self.refresh_button, 0, 2)

        grid.addWidget(BodyLabel("波特率", conn_card), 1, 0)
        self.baud_combo = EditableComboBox(conn_card)
        self.baud_combo.addItems(BAUD_CHOICES)
        self.baud_combo.setCurrentText("115200")
        grid.addWidget(self.baud_combo, 1, 1, 1, 2)

        grid.addWidget(BodyLabel("数据位", conn_card), 2, 0)
        self.data_bits_combo = ComboBox(conn_card)
        self.data_bits_combo.addItems(["5", "6", "7", "8"])
        self.data_bits_combo.setCurrentIndex(3)
        grid.addWidget(self.data_bits_combo, 2, 1, 1, 2)

        grid.addWidget(BodyLabel("停止位", conn_card), 3, 0)
        self.stop_bits_combo = ComboBox(conn_card)
        self.stop_bits_combo.addItems(["1", "1.5", "2"])
        grid.addWidget(self.stop_bits_combo, 3, 1, 1, 2)

        grid.addWidget(BodyLabel("校验", conn_card), 4, 0)
        self.parity_combo = ComboBox(conn_card)
        _fill_combo(self.parity_combo, PARITY_ITEMS)
        grid.addWidget(self.parity_combo, 4, 1, 1, 2)

        grid.addWidget(BodyLabel("流控", conn_card), 5, 0)
        self.flow_combo = ComboBox(conn_card)
        _fill_combo(self.flow_combo, FLOW_ITEMS)
        grid.addWidget(self.flow_combo, 5, 1, 1, 2)

        grid.addWidget(BodyLabel("字符编码", conn_card), 6, 0)
        self.encoding_combo = ComboBox(conn_card)
        _fill_combo(self.encoding_combo, ENCODING_ITEMS)
        grid.addWidget(self.encoding_combo, 6, 1, 1, 2)
        conn_layout.addLayout(grid)

        self.port_info_label = CaptionLabel("尚未选择串口", conn_card)
        self.port_info_label.setWordWrap(True)
        conn_layout.addWidget(self.port_info_label)

        open_row = QHBoxLayout()
        open_row.setSpacing(8)
        self.open_button = PrimaryPushButton("打开串口", conn_card)
        self.open_button.clicked.connect(self._toggle_connection)
        open_row.addWidget(self.open_button)
        open_row.addStretch(1)
        conn_layout.addLayout(open_row)
        layout.addWidget(conn_card)

        line_card, line_layout = section_card("线路控制", "DTR/RTS 与只读线路状态")
        switches = QHBoxLayout()
        switches.setSpacing(18)
        self.dtr_switch = SwitchButton(line_card)
        self.dtr_switch.setText("DTR")
        self.dtr_switch.checkedChanged.connect(self._on_dtr_changed)
        switches.addWidget(self.dtr_switch)
        self.rts_switch = SwitchButton(line_card)
        self.rts_switch.setText("RTS")
        self.rts_switch.checkedChanged.connect(self._on_rts_changed)
        switches.addWidget(self.rts_switch)
        switches.addStretch(1)
        line_layout.addLayout(switches)

        self.break_button = PushButton("发送 BREAK", line_card)
        self.break_button.setToolTip("拉低 TX 约 250 ms（仅串口打开时可用）")
        self.break_button.clicked.connect(self._send_break)
        line_layout.addWidget(self.break_button)

        line_grid = QGridLayout()
        line_grid.setHorizontalSpacing(8)
        line_grid.setVerticalSpacing(6)
        self.line_chips: dict[str, StatusChip] = {}
        for index, name in enumerate(("CTS", "DSR", "RING", "RLSD")):
            chip = StatusChip(line_card)
            chip.set_state("idle", f"{name} 0")
            self.line_chips[name] = chip
            line_grid.addWidget(chip, index // 2, index % 2)
        line_layout.addLayout(line_grid)
        layout.addWidget(line_card)

        action_card, action_layout = section_card("会话与工具", "记录本程序收发数据，不做系统级驱动监视")
        self.counter_label = StrongBodyLabel("接收 0 B · 发送 0 B", action_card)
        action_layout.addWidget(self.counter_label)
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.multi_send_button = PushButton("多项发送", action_card)
        self.multi_send_button.clicked.connect(self._open_multi_send)
        action_row.addWidget(self.multi_send_button)
        self.session_button = PushButton("会话记录", action_card)
        self.session_button.clicked.connect(self._open_session)
        action_row.addWidget(self.session_button)
        action_layout.addLayout(action_row)
        self.reset_counter_button = PushButton("重新计数", action_card)
        self.reset_counter_button.clicked.connect(self._reset_counters)
        action_layout.addWidget(self.reset_counter_button)
        layout.addWidget(action_card)
        layout.addStretch(1)
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
        self.rx_log = LogPane("接收区", show_direction=False, settings=self.settings, parent=self.log_tabs)
        self.tx_log = LogPane("发送记录", show_direction=False, settings=self.settings, parent=self.log_tabs)
        self.rx_log.log_view.setPlaceholderText("串口接收到的数据会显示在这里")
        self.tx_log.log_view.setPlaceholderText("已发送的数据会显示在这里")
        self.log_tabs.addTab(self.rx_log, "接收区", FluentIcon.DOWNLOAD)
        self.log_tabs.addTab(self.tx_log, "发送记录", FluentIcon.SEND)
        log_layout.addWidget(self.log_tabs, 1)
        layout.addWidget(log_card, 3)

        send_card, send_layout = section_card("发送区", "Ctrl+Enter 发送；HEX 支持空格、逗号与 0x 前缀")
        self.send_editor = PlainTextEdit(send_card)
        self.send_editor.setPlaceholderText("输入要发送的数据…")
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.send_editor.setFont(font)
        self.send_editor.setMinimumHeight(96)
        self.send_editor.setMaximumHeight(180)
        send_layout.addWidget(self.send_editor)

        options = QHBoxLayout()
        options.setSpacing(12)
        self.hex_send_check = CheckBox("HEX 发送", send_card)
        self.hex_send_check.toggled.connect(self._settings_changed)
        options.addWidget(self.hex_send_check)
        options.addWidget(BodyLabel("末尾", send_card))
        self.newline_combo = ComboBox(send_card)
        _fill_combo(self.newline_combo, NEWLINE_ITEMS)
        self.newline_combo.currentIndexChanged.connect(self._settings_changed)
        options.addWidget(self.newline_combo)
        self.repeat_check = CheckBox("连续发送", send_card)
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
        self.send_button = PrimaryPushButton("发送", send_card)
        self.send_button.clicked.connect(self._send_current)
        buttons.addWidget(self.send_button)
        self.send_file_button = PushButton("发送文件", send_card)
        self.send_file_button.clicked.connect(self._choose_file)
        buttons.addWidget(self.send_file_button)
        self.cancel_file_button = PushButton("取消文件", send_card)
        self.cancel_file_button.setEnabled(False)
        self.cancel_file_button.clicked.connect(self._cancel_file_send)
        buttons.addWidget(self.cancel_file_button)
        buttons.addStretch(1)
        send_layout.addLayout(buttons)
        self.file_progress_label = CaptionLabel("", send_card)
        send_layout.addWidget(self.file_progress_label)
        layout.addWidget(send_card, 2)

        self._shortcut_send = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._shortcut_send.activated.connect(self._send_current)
        self._shortcut_send2 = QShortcut(QKeySequence("Ctrl+Enter"), self)
        self._shortcut_send2.activated.connect(self._send_current)
        self._shortcut_refresh = QShortcut(QKeySequence("F5"), self)
        self._shortcut_refresh.activated.connect(self.refresh_ports)
        self._shortcut_clear = QShortcut(QKeySequence("Ctrl+K"), self)
        self._shortcut_clear.activated.connect(self._clear_current_log)
        self._shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self._shortcut_save.activated.connect(self._save_current_log)

        # 参数变化时即时保存并同步接收/发送日志的字符编码。
        self.port_combo.currentTextChanged.connect(self._on_port_text_changed)
        self.baud_combo.currentTextChanged.connect(self._settings_changed)
        for combo in (
            self.data_bits_combo,
            self.stop_bits_combo,
            self.parity_combo,
            self.flow_combo,
            self.encoding_combo,
        ):
            combo.currentIndexChanged.connect(self._settings_changed)
        return right

    # ---- worker -------------------------------------------------------

    def _create_worker(self) -> None:
        if self.worker_factory is not None and not callable(self.worker_factory):
            # 允许测试用哨兵对象检查构造契约；生产路径必须传入可调用工厂。
            get_logger().warning("worker_factory 不是可调用对象，串口功能未初始化")
            self.status_chip.set_state("error", "串口未初始化")
            return
        try:
            try:
                worker = self._worker_factory(self)
            except TypeError:
                worker = self._worker_factory()
        except Exception as exc:
            get_logger().exception("创建串口工作对象失败")
            self.status_chip.set_state("error", "串口不可用")
            show_error(self, "串口初始化失败", str(exc))
            return
        self._worker = worker
        worker.opened.connect(self._on_opened)
        worker.closed.connect(self._on_closed)
        worker.data_received.connect(self._on_data_received)
        worker.data_sent.connect(self._on_data_sent)
        worker.error.connect(self._on_error)
        worker.modem_changed.connect(self._on_modem_changed)

    def _restore_settings(self) -> None:
        self.port_combo.setCurrentText(str(self.settings.get("serial.port", "")))
        self.baud_combo.setCurrentText(str(self.settings.get("serial.baud", 115200)))
        _select_index(self.data_bits_combo, str(self.settings.get("serial.data_bits", 8)))
        _select_index(self.stop_bits_combo, _stopbits_text(self.settings.get("serial.stop_bits", 1)))
        _select_combo_value(self.parity_combo, PARITY_ITEMS, self.settings.get("serial.parity", "none"))
        _select_combo_value(self.flow_combo, FLOW_ITEMS, self.settings.get("serial.flow", "none"))
        _select_combo_value(self.encoding_combo, ENCODING_ITEMS, resolve_page_encoding(self.settings, "serial.encoding"))
        self.hex_send_check.setChecked(bool(self.settings.get("serial.hex_send", False)))
        _select_combo_value(self.newline_combo, NEWLINE_ITEMS, "none")
        hex_display = bool(self.settings.get("serial.hex_display", False))
        timestamp = bool(self.settings.get("serial.timestamp", True))
        for pane in (self.rx_log, self.tx_log):
            pane.set_hex_mode(hex_display)
            pane.set_timestamp_visible(timestamp)
            pane.set_encoding(self._current_encoding())
        self._apply_encoding_to_logs()

    def _save_settings(self) -> None:
        if self._loading:
            return
        try:
            self.settings.update(
                {
                    "serial.port": self.port_combo.currentText().strip(),
                    "serial.baud": _int_or(self.baud_combo.currentText(), 115200),
                    "serial.data_bits": _int_or(self.data_bits_combo.currentText(), 8),
                    "serial.stop_bits": _float_or(self.stop_bits_combo.currentText(), 1.0),
                    "serial.parity": _combo_value(self.parity_combo, PARITY_ITEMS, "none"),
                    "serial.flow": _combo_value(self.flow_combo, FLOW_ITEMS, "none"),
                    "serial.encoding": _combo_value(self.encoding_combo, ENCODING_ITEMS, "utf-8"),
                    "serial.hex_send": self.hex_send_check.isChecked(),
                    "serial.hex_display": self.rx_log.hex_check.isChecked(),
                    "serial.timestamp": self.rx_log.timestamp_check.isChecked(),
                }
            )
            self.settings.save()
        except Exception:
            get_logger().exception("保存串口设置失败")

    @safe_slot
    def _settings_changed(self, *_args) -> None:
        self._save_settings()
        self._apply_encoding_to_logs()

    # ---- 端口与连接 ---------------------------------------------------

    @safe_slot
    def refresh_ports(self) -> None:
        """刷新串口列表，保留手动输入的端口。"""
        current = self.port_combo.currentText().strip()
        ports = list_ports()
        self._ports_by_device = {item.device: item for item in ports}
        try:
            self.port_combo.blockSignals(True)
            self.port_combo.clear()
            for item in ports:
                self.port_combo.addItem(item.device)
            if current:
                self.port_combo.setCurrentText(current)
            elif ports:
                configured = str(self.settings.get("serial.port", "")).strip()
                self.port_combo.setCurrentText(configured or ports[0].device)
            else:
                self.port_combo.setCurrentText(str(self.settings.get("serial.port", "")))
        finally:
            self.port_combo.blockSignals(False)
        self._update_port_info()
        if not ports:
            self.port_info_label.setText("未检测到串口；可手动输入 COM 端口后打开")
        self._save_settings()

    @safe_slot
    def _on_port_text_changed(self, _text: str) -> None:
        self._update_port_info()
        self._settings_changed()

    @safe_slot
    def _update_port_info(self) -> None:
        device = self.port_combo.currentText().strip()
        info = self._ports_by_device.get(device)
        if info is None:
            self.port_info_label.setText(
                f"{device or '未选择端口'}（不在当前设备列表中，可直接按手动端口打开）"
            )
            return
        details = [info.description or "串口设备"]
        if info.vid is not None and info.pid is not None:
            details.append(f"VID:PID {info.vid:04X}:{info.pid:04X}")
        if info.serial_number:
            details.append(f"SN {info.serial_number}")
        self.port_info_label.setText(f"{device} · " + " · ".join(details))

    def _build_config(self) -> SerialConfig | None:
        port = self.port_combo.currentText().strip()
        if not port:
            show_warning(self, "请选择串口", "先选择一个串口，或手动输入 COM 端口。")
            return None
        baud = _int_or(self.baud_combo.currentText(), 0)
        if baud <= 0:
            show_error(self, "波特率无效", "请输入大于 0 的整数波特率。")
            return None
        return SerialConfig(
            port=port,
            baud=baud,
            bytesize=_int_or(self.data_bits_combo.currentText(), 8),
            stopbits=_float_or(self.stop_bits_combo.currentText(), 1.0),
            parity=_combo_value(self.parity_combo, PARITY_ITEMS, "none"),
            flow=_combo_value(self.flow_combo, FLOW_ITEMS, "none"),
            encoding=self._current_encoding(),
        )

    @safe_slot
    def _toggle_connection(self) -> None:
        if self._worker is None:
            show_error(self, "串口不可用", "串口工作对象未初始化。")
            return
        if self._worker.is_open():
            self._close_connection()
        else:
            self._open_connection()

    def _open_connection(self) -> None:
        config = self._build_config()
        if config is None:
            return
        self.status_chip.set_state("busy", "正在打开…")
        self.open_button.setEnabled(False)
        self._set_config_enabled(False)
        self._save_settings()
        try:
            opened = self._worker.open_port(config)
        except Exception as exc:
            get_logger().exception("打开串口异常")
            self._on_error(str(exc))
            opened = False
        if not opened:
            self._set_config_enabled(True)
            self.open_button.setEnabled(True)
            self.open_button.setText("打开串口")
            if self.status_chip.state == "busy":
                self.status_chip.set_state("error", "打开失败")

    def _close_connection(self) -> None:
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        self.status_chip.set_state("busy", "正在关闭…")
        self.open_button.setEnabled(False)
        try:
            self._worker.close_port()
        except Exception:
            get_logger().exception("关闭串口异常")
        self.open_button.setEnabled(True)

    @safe_slot
    def _on_opened(self) -> None:
        self.status_chip.set_state("ok", f"已打开 {self.port_combo.currentText().strip()}")
        self.open_button.setText("关闭串口")
        self.open_button.setEnabled(True)
        self._set_config_enabled(False)
        self._set_line_controls_enabled(True)
        self._worker.set_dtr(self.dtr_switch.isChecked())
        self._worker.set_rts(self.rts_switch.isChecked())
        if self.repeat_check.isChecked():
            self._start_repeat()
        self.rx_log.append_info(
            f"已打开 {self.port_combo.currentText().strip()} · "
            f"{self.baud_combo.currentText().strip()} bps"
        )

    @safe_slot
    def _on_closed(self) -> None:
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        self.status_chip.set_state("idle", "未连接")
        self.open_button.setText("打开串口")
        self.open_button.setEnabled(True)
        self._set_config_enabled(True)
        self._set_line_controls_enabled(False)
        for name, chip in self.line_chips.items():
            chip.set_state("idle", f"{name} 0")
        self.rx_log.append_info("串口已关闭")

    @safe_slot
    def _on_error(self, message: str) -> None:
        text = str(message)
        self.status_chip.set_state("error", "串口错误")
        self.rx_log.append_info(f"错误：{text}")
        show_error(self, "串口错误", text)
        if self._worker is not None and not self._worker.is_open():
            self.open_button.setText("打开串口")
            self.open_button.setEnabled(True)
            self._set_config_enabled(True)

    def _set_config_enabled(self, enabled: bool) -> None:
        for widget in (
            self.port_combo,
            self.baud_combo,
            self.data_bits_combo,
            self.stop_bits_combo,
            self.parity_combo,
            self.flow_combo,
            self.encoding_combo,
            self.refresh_button,
        ):
            widget.setEnabled(enabled)

    def _set_line_controls_enabled(self, enabled: bool) -> None:
        self.break_button.setEnabled(enabled)
        self.dtr_switch.setEnabled(enabled)
        self.rts_switch.setEnabled(enabled)

    # ---- 收发 ---------------------------------------------------------

    def _on_data_received(self, data: bytes) -> None:
        self.rx_log.append_bytes(data, "rx")
        self._refresh_counters()

    def _on_data_sent(self, data: bytes) -> None:
        self.tx_log.append_bytes(data, "tx")
        self._refresh_counters()
        self._on_file_data_sent(data)

    def _on_modem_changed(self, status: dict) -> None:
        mapping = {"CTS": "cts", "DSR": "dsr", "RING": "ri", "RLSD": "cd"}
        for name, key in mapping.items():
            active = bool(status.get(key, False))
            chip = self.line_chips.get(name)
            if chip is not None:
                chip.set_state("ok" if active else "idle", f"{name} {1 if active else 0}")

    def _current_encoding(self) -> str:
        value = _combo_value(self.encoding_combo, ENCODING_ITEMS, "utf-8")
        try:
            return resolve_codec(value)
        except Exception:
            return "utf-8"

    def _apply_encoding_to_logs(self) -> None:
        codec = self._current_encoding()
        for pane in (self.rx_log, self.tx_log):
            try:
                pane.set_encoding(codec)
            except Exception:
                get_logger().exception("应用日志编码失败")

    def _build_payload(self, text: str, hex_mode: bool, newline: str | None = None) -> bytes:
        newline = newline if newline is not None else _combo_value(self.newline_combo, NEWLINE_ITEMS, "none")
        if hex_mode:
            payload = parse_hex(text)
        else:
            payload = encode_text(text, self._current_encoding())
        return payload + NEWLINE_BYTES.get(newline, b"")

    @safe_slot
    def _send_current(self) -> bool:
        if self._file_handle is not None:
            show_warning(self, "文件发送中", "请等待文件发送完成或取消后再发送其他数据。")
            return False
        if self._worker is None or not self._worker.is_open():
            show_warning(self, "串口未打开", "请先打开串口再发送。")
            self._stop_repeat()
            return False
        try:
            payload = self._build_payload(
                self.send_editor.toPlainText(), self.hex_send_check.isChecked()
            )
        except ValueError as exc:
            show_error(self, "HEX 格式错误", str(exc))
            self._stop_repeat()
            return False
        if not payload:
            show_warning(self, "没有数据", "请输入要发送的内容。")
            return False
        if not self._worker.send(payload):
            self._stop_repeat()
            return False
        return True

    @safe_slot
    def _send_repeat(self) -> None:
        if not self._send_current():
            self._stop_repeat()

    def _on_repeat_toggled(self, checked: bool) -> None:
        if checked and self._worker is not None and self._worker.is_open():
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

    @safe_slot
    def _send_text(self, text: str, hex_mode: bool, newline: bool) -> bool:
        """多项发送回调：newline 为 True 时追加 CRLF。"""
        if self._file_handle is not None:
            show_warning(self, "文件发送中", "请等待文件发送完成或取消后再发送其他数据。")
            return False
        if self._worker is None or not self._worker.is_open():
            show_warning(self, "串口未打开", "请先打开串口再发送。")
            return False
        payload = self._build_payload(text, hex_mode, "crlf" if newline else "none")
        if not payload:
            return False
        return bool(self._worker.send(payload))

    @safe_slot
    def _on_dtr_changed(self, checked: bool) -> None:
        if self._worker is not None and self._worker.is_open():
            self._worker.set_dtr(checked)

    @safe_slot
    def _on_rts_changed(self, checked: bool) -> None:
        if self._worker is not None and self._worker.is_open():
            self._worker.set_rts(checked)

    @safe_slot
    def _send_break(self) -> None:
        if self._worker is None or not self._worker.is_open():
            return
        self._worker.send_break()

    # ---- 文件发送 -----------------------------------------------------

    @safe_slot
    def _choose_file(self) -> None:
        if self._worker is None or not self._worker.is_open():
            show_warning(self, "串口未打开", "请先打开串口再发送文件。")
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
        self._file_awaiting_ack = False
        self._file_pending_chunk = b""
        self.send_button.setEnabled(False)
        self.send_file_button.setEnabled(False)
        self.cancel_file_button.setEnabled(True)
        self._update_file_label()
        self._send_next_file_chunk()

    def _send_next_file_chunk(self) -> None:
        """发送一块文件；必须等 worker 的 data_sent 确认后才读下一块，避免无界排队。"""
        if self._file_handle is None or self._file_awaiting_ack:
            return
        try:
            chunk = self._file_handle.read(FILE_CHUNK)
        except OSError as exc:
            show_error(self, "读取文件失败", str(exc))
            self._cancel_file_send()
            return
        if not chunk:
            self._finish_file_send()
            return
        if self._worker is None or not self._worker.send(chunk):
            self._cancel_file_send()
            return
        self._file_pending_chunk = chunk
        self._file_awaiting_ack = True

    def _on_file_data_sent(self, data: bytes) -> None:
        """根据真实写入字节推进文件发送；部分写入时重发剩余部分。"""
        if self._file_handle is None or not self._file_awaiting_ack:
            return
        acked = len(data)
        if acked <= 0:
            return
        self._file_sent += acked
        self._file_pending_chunk = self._file_pending_chunk[acked:]
        self._update_file_label()
        if self._file_pending_chunk:
            if self._worker is None or not self._worker.send(self._file_pending_chunk):
                self._cancel_file_send()
            return
        self._file_awaiting_ack = False
        self._send_next_file_chunk()

    def _finish_file_send(self) -> None:
        self._close_file_handle()
        self._file_awaiting_ack = False
        self._file_pending_chunk = b""
        self.file_progress_label.setText(f"文件发送完成：{self._file_name}（{self._file_sent} B）")
        self._set_file_buttons_idle()
        show_success(self, "文件发送完成", self._file_name)

    @safe_slot
    def _cancel_file_send(self, silent: bool = False) -> None:
        was_active = self._file_handle is not None
        self._close_file_handle()
        self._file_awaiting_ack = False
        self._file_pending_chunk = b""
        if was_active:
            self.file_progress_label.setText(f"文件发送已取消：{self._file_name}")
        self._set_file_buttons_idle()

    def _close_file_handle(self) -> None:
        if self._file_handle is not None:
            try:
                self._file_handle.close()
            except OSError:
                get_logger().exception("关闭发送文件句柄失败")
            finally:
                self._file_handle = None

    def _set_file_buttons_idle(self) -> None:
        self.send_button.setEnabled(True)
        self.send_file_button.setEnabled(True)
        self.cancel_file_button.setEnabled(False)

    def _update_file_label(self) -> None:
        self.file_progress_label.setText(
            f"{self._file_name}：{self._file_sent} / {self._file_size} B"
        )

    # ---- 会话/多项发送 -----------------------------------------------

    @safe_slot
    def _open_multi_send(self) -> None:
        dialog = MultiSendDialog(self._send_text, self)
        dialog.exec()

    @safe_slot
    def _open_session(self) -> None:
        dialog = SessionDialog(
            self._combined_session(),
            self,
            codec=self._current_encoding(),
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
        default_name = f"serial-{datetime.now():%Y%m%d-%H%M%S}.txt"
        path_text, _selected = QFileDialog.getSaveFileName(
            self, "保存会话日志", default_name, "文本文件 (*.txt);;CSV 文件 (*.csv)"
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
            (self._worker is not None and self._worker.is_open())
            or self._repeat_timer.isActive()
            or self._file_handle is not None
        )

    def stop_workers(self) -> None:
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        if self._worker is not None:
            try:
                self._worker.close_port()
                if hasattr(self._worker, "wait"):
                    self._worker.wait(2000)
            except Exception:
                get_logger().exception("停止串口工作时失败")

    def closeEvent(self, event) -> None:
        self._save_settings()
        self.stop_workers()
        super().closeEvent(event)


def _fill_combo(combo: ComboBox, items: tuple[tuple[str, str], ...]) -> None:
    for label, _value in items:
        combo.addItem(label)


def _combo_value(combo: ComboBox, items: tuple[tuple[str, str], ...], default: str) -> str:
    index = combo.currentIndex()
    if 0 <= index < len(items):
        return items[index][1]
    return default


def _select_combo_value(combo: ComboBox, items: tuple[tuple[str, str], ...], value: str) -> None:
    for index, (_label, item_value) in enumerate(items):
        if item_value == value:
            combo.setCurrentIndex(index)
            return
    combo.setCurrentIndex(0)


def _select_index(combo: ComboBox, text: str) -> None:
    index = combo.findText(text)
    combo.setCurrentIndex(index if index >= 0 else 0)


def _int_or(text: str, default: int) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def _float_or(text: str, default: float) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


def _stopbits_text(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "1"
    if abs(number - 1.5) < 0.01:
        return "1.5"
    if abs(number - 2) < 0.01:
        return "2"
    return "1"


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.2f} MB"


def _default_export_dir(paths: AppPaths | None) -> Path:
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()

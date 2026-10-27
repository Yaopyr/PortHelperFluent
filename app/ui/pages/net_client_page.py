"""网络调试页：TCP/UDP 客户端、收发记录、Ping、域名解析与网卡信息。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
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
    CheckBox,
    ComboBox,
    FluentIcon,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
    StrongBodyLabel,
    TabWidget,
    TitleLabel,
    ToolButton,
)

from app.core.encoding import encode_text, parse_hex, resolve_codec
from app.core.logging_setup import get_logger
from app.core.settings import Settings
from app.io.net_client import NetClientController
from app.services.net_tools import adapter_info, ping_host, resolve_host
from app.services.paths import AppPaths
from app.services.session_log import SessionLog
from app.ui.dialogs.session_dialog import SessionDialog
from app.ui.dialogs.text_info_dialog import TextInfoDialog
from app.ui.feedback import show_error, show_success, show_warning
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.log_pane import LogPane
from app.ui.widgets.status_chip import StatusChip

MODE_ITEMS = (("TCP 客户端", "tcp"), ("UDP", "udp"))
ENCODING_ITEMS = (("ANSI（系统）", "ansi"), ("GBK", "gbk"), ("UTF-8", "utf-8"))
FILE_CHUNK = 8192


class _TaskSignals(QObject):
    finished = pyqtSignal(bool, object)


class _BackgroundTask(QRunnable):
    """在 QThreadPool 中执行 ping / DNS / psutil 查询，结果回到 GUI 线程。"""

    def __init__(self, func: Callable[[], object]) -> None:
        super().__init__()
        self.signals = _TaskSignals()
        self._func = func

    def run(self) -> None:
        try:
            result = self._func()
        except Exception as exc:
            self.signals.finished.emit(False, str(exc))
        else:
            self.signals.finished.emit(True, result)


class NetClientPage(QWidget):
    """TCP/UDP 客户端工作台；网络 I/O 全部由 NetClientController 在事件循环内完成。"""

    def __init__(
        self,
        settings: Settings,
        paths: AppPaths,
        parent: QWidget | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("net-client")
        self.settings = settings
        self.paths = paths
        self.extra_options = dict(kwargs)
        self._loading = True
        self._closing = False
        self._tcp_connecting = False
        self._tcp_was_connected = False
        self._udp_was_bound = False
        self._file_handle: BinaryIO | None = None
        self._file_name = ""
        self._file_size = 0
        self._file_sent = 0
        self._file_mode = "tcp"
        self._tasks: set[_BackgroundTask] = set()
        self._info_dialogs: list[TextInfoDialog] = []

        self._repeat_timer = QTimer(self)
        self._repeat_timer.setInterval(1000)
        self._repeat_timer.timeout.connect(self._send_repeat)

        self._file_timer = QTimer(self)
        self._file_timer.setInterval(5)
        self._file_timer.timeout.connect(self._send_file_chunk)

        self.controller = NetClientController(self)
        self._build_ui()
        self._connect_controller()
        self._restore_settings()
        self._apply_mode()
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
        title_box.addWidget(TitleLabel("网络调试", self))
        title_box.addWidget(CaptionLabel("TCP/UDP 客户端、Ping、域名解析与网卡信息", self))
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
        splitter.setSizes([360, 880])
        root.addWidget(splitter, 1)

    def _build_left_column(self) -> QWidget:
        left = QWidget(self)
        left.setMinimumWidth(330)
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        conn_card, conn_layout = section_card("连接参数", "TCP 与 UDP 独立连接；域名可先解析再连接")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        grid.addWidget(BodyLabel("模式", conn_card), 0, 0)
        self.mode_combo = ComboBox(conn_card)
        _fill_combo(self.mode_combo, MODE_ITEMS)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        grid.addWidget(self.mode_combo, 0, 1, 1, 2)

        grid.addWidget(BodyLabel("目标地址", conn_card), 1, 0)
        self.host_edit = LineEdit(conn_card)
        self.host_edit.setPlaceholderText("IP 或域名，例如 127.0.0.1")
        self.host_edit.setClearButtonEnabled(True)
        self.host_edit.textChanged.connect(self._settings_changed)
        grid.addWidget(self.host_edit, 1, 1)
        self.resolve_button = ToolButton(conn_card)
        self.resolve_button.setIcon(FluentIcon.SYNC)
        self.resolve_button.setToolTip("解析域名并显示全部 IP (F5)")
        self.resolve_button.clicked.connect(self._resolve_domain)
        grid.addWidget(self.resolve_button, 1, 2)

        self.tcp_port_label = BodyLabel("TCP 目的端口", conn_card)
        grid.addWidget(self.tcp_port_label, 2, 0)
        self.port_spin = SpinBox(conn_card)
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(8080)
        self.port_spin.setToolTip("TCP 目标服务器端口")
        self.port_spin.valueChanged.connect(self._settings_changed)
        grid.addWidget(self.port_spin, 2, 1, 1, 2)

        self.tcp_local_label = BodyLabel("TCP 本地端口", conn_card)
        grid.addWidget(self.tcp_local_label, 3, 0)
        self.tcp_local_spin = SpinBox(conn_card)
        self.tcp_local_spin.setRange(0, 65535)
        self.tcp_local_spin.setSpecialValueText("自动分配")
        self.tcp_local_spin.setToolTip("0 表示由系统自动分配本地端口")
        grid.addWidget(self.tcp_local_spin, 3, 1, 1, 2)

        self.udp_port_label = BodyLabel("UDP 目的端口", conn_card)
        grid.addWidget(self.udp_port_label, 4, 0)
        self.udp_port_spin = SpinBox(conn_card)
        self.udp_port_spin.setRange(1, 65535)
        self.udp_port_spin.setValue(8080)
        self.udp_port_spin.setToolTip("UDP 目标端口")
        self.udp_port_spin.valueChanged.connect(self._settings_changed)
        grid.addWidget(self.udp_port_spin, 4, 1, 1, 2)

        self.udp_local_label = BodyLabel("UDP 本地端口", conn_card)
        grid.addWidget(self.udp_local_label, 5, 0)
        self.udp_local_spin = SpinBox(conn_card)
        self.udp_local_spin.setRange(0, 65535)
        self.udp_local_spin.setSpecialValueText("自动分配")
        self.udp_local_spin.setToolTip("0 表示由系统自动分配本地端口")
        grid.addWidget(self.udp_local_spin, 5, 1, 1, 2)
        conn_layout.addLayout(grid)

        self.connection_info_label = CaptionLabel("TCP 未连接；UDP 未绑定", conn_card)
        self.connection_info_label.setWordWrap(True)
        conn_layout.addWidget(self.connection_info_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.connect_button = PrimaryPushButton("连接", conn_card)
        self.connect_button.clicked.connect(self._connect_clicked)
        buttons.addWidget(self.connect_button)
        self.disconnect_button = PushButton("断开", conn_card)
        self.disconnect_button.setEnabled(False)
        self.disconnect_button.clicked.connect(self._disconnect_clicked)
        buttons.addWidget(self.disconnect_button)
        buttons.addStretch(1)
        conn_layout.addLayout(buttons)
        layout.addWidget(conn_card)

        assist_card, assist_layout = section_card("网络工具", "Ping 调用系统命令；不做原始套接字 ICMP")
        tool_grid = QGridLayout()
        tool_grid.setHorizontalSpacing(8)
        tool_grid.setVerticalSpacing(8)
        self.ping_button = PushButton("Ping 目标", assist_card)
        self.ping_button.clicked.connect(self._ping_target)
        tool_grid.addWidget(self.ping_button, 0, 0)
        self.adapter_button = PushButton("网卡信息", assist_card)
        self.adapter_button.clicked.connect(self._show_adapter_info)
        tool_grid.addWidget(self.adapter_button, 0, 1)
        self.domain_button = PushButton("域名转 IP", assist_card)
        self.domain_button.clicked.connect(self._resolve_domain)
        tool_grid.addWidget(self.domain_button, 1, 0)
        self.session_button = PushButton("会话记录", assist_card)
        self.session_button.clicked.connect(self._open_session)
        tool_grid.addWidget(self.session_button, 1, 1)
        assist_layout.addLayout(tool_grid)
        layout.addWidget(assist_card)
        layout.addStretch(1)
        return left

    def _build_right_column(self) -> QWidget:
        right = QWidget(self)
        layout = QVBoxLayout(right)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        log_card, log_layout = section_card("收发记录", "TCP 与 UDP 独立记录；暂停只停止刷新，计数继续")
        self.log_tabs = TabWidget(log_card)
        self.tcp_log = LogPane(
            "TCP", show_direction=True, settings=self.settings, parent=self.log_tabs
        )
        self.udp_log = LogPane(
            "UDP", show_direction=True, settings=self.settings, parent=self.log_tabs
        )
        self.tcp_log.log_view.setPlaceholderText("TCP 收发数据会显示在这里")
        self.udp_log.log_view.setPlaceholderText("UDP 收发数据会显示在这里")
        self.log_tabs.addTab(self.tcp_log, "TCP", FluentIcon.CONNECT)
        self.log_tabs.addTab(self.udp_log, "UDP", FluentIcon.GLOBE)
        log_layout.addWidget(self.log_tabs, 1)
        layout.addWidget(log_card, 3)

        send_card, send_layout = section_card(
            "发送区", "Ctrl+Enter 发送当前模式；HEX 支持空格、逗号与 0x 前缀"
        )
        self.send_editor = PlainTextEdit(send_card)
        self.send_editor.setPlaceholderText("输入要发送的数据…")
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.send_editor.setFont(font)
        self.send_editor.setMinimumHeight(84)
        self.send_editor.setMaximumHeight(150)
        send_layout.addWidget(self.send_editor)

        options = QHBoxLayout()
        options.setSpacing(12)
        self.hex_send_check = CheckBox("HEX 发送", send_card)
        self.hex_send_check.toggled.connect(self._settings_changed)
        options.addWidget(self.hex_send_check)
        options.addWidget(BodyLabel("编码", send_card))
        self.encoding_combo = ComboBox(send_card)
        _fill_combo(self.encoding_combo, ENCODING_ITEMS)
        self.encoding_combo.currentIndexChanged.connect(self._settings_changed)
        options.addWidget(self.encoding_combo)
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

        send_buttons = QHBoxLayout()
        send_buttons.setSpacing(8)
        self.tcp_send_button = PrimaryPushButton("TCP 发送", send_card)
        self.tcp_send_button.clicked.connect(self._send_tcp_clicked)
        send_buttons.addWidget(self.tcp_send_button)
        self.udp_send_button = PushButton("UDP 发送", send_card)
        self.udp_send_button.clicked.connect(self._send_udp_clicked)
        send_buttons.addWidget(self.udp_send_button)
        self.send_file_button = PushButton("发送文件", send_card)
        self.send_file_button.clicked.connect(self._choose_file)
        send_buttons.addWidget(self.send_file_button)
        self.cancel_file_button = PushButton("取消文件", send_card)
        self.cancel_file_button.setEnabled(False)
        self.cancel_file_button.clicked.connect(self._cancel_file_send)
        send_buttons.addWidget(self.cancel_file_button)
        send_buttons.addStretch(1)
        send_layout.addLayout(send_buttons)

        self.file_progress_label = CaptionLabel("", send_card)
        send_layout.addWidget(self.file_progress_label)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.counter_label = StrongBodyLabel("接收 0 B · 发送 0 B", send_card)
        footer.addWidget(self.counter_label)
        footer.addStretch(1)
        self.clear_button = PushButton("清空当前", send_card)
        self.clear_button.clicked.connect(self._clear_current_log)
        footer.addWidget(self.clear_button)
        self.save_button = PushButton("保存当前", send_card)
        self.save_button.clicked.connect(self._save_current_log)
        footer.addWidget(self.save_button)
        self.session_button_send = PushButton("会话记录", send_card)
        self.session_button_send.clicked.connect(self._open_session)
        footer.addWidget(self.session_button_send)
        send_layout.addLayout(footer)
        layout.addWidget(send_card, 2)

        self._shortcut_send = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._shortcut_send.activated.connect(self._send_current)
        self._shortcut_send2 = QShortcut(QKeySequence("Ctrl+Enter"), self)
        self._shortcut_send2.activated.connect(self._send_current)
        self._shortcut_refresh = QShortcut(QKeySequence("F5"), self)
        self._shortcut_refresh.activated.connect(self._resolve_domain)
        self._shortcut_clear = QShortcut(QKeySequence("Ctrl+K"), self)
        self._shortcut_clear.activated.connect(self._clear_current_log)
        self._shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self._shortcut_save.activated.connect(self._save_current_log)
        return right

    def _connect_controller(self) -> None:
        self.controller.tcp_state_changed.connect(self._on_tcp_state)
        self.controller.udp_state_changed.connect(self._on_udp_state)
        self.controller.tcp_data.connect(self._on_tcp_data)
        self.controller.udp_data.connect(self._on_udp_data)
        self.controller.error.connect(self._on_error)

    # ---- 设置 ---------------------------------------------------------

    def _restore_settings(self) -> None:
        self.host_edit.setText(str(self.settings.get("net.host", "127.0.0.1")))
        self.port_spin.setValue(_int_or(self.settings.get("net.port", 8080), 8080))
        self.udp_port_spin.setValue(_int_or(self.settings.get("net.udp_port", 8080), 8080))
        _select_combo_value(
            self.encoding_combo, ENCODING_ITEMS, self.settings.get("net.encoding", "utf-8")
        )
        self.hex_send_check.setChecked(bool(self.settings.get("net.hex_send", False)))
        for pane in (self.tcp_log, self.udp_log):
            pane.set_encoding(self._current_encoding())

    def _save_settings(self) -> None:
        if self._loading:
            return
        try:
            self.settings.update(
                {
                    "net.host": self.host_edit.text().strip(),
                    "net.port": int(self.port_spin.value()),
                    "net.udp_port": int(self.udp_port_spin.value()),
                    "net.encoding": _combo_value(
                        self.encoding_combo, ENCODING_ITEMS, "utf-8"
                    ),
                    "net.hex_send": self.hex_send_check.isChecked(),
                }
            )
            self.settings.save()
        except Exception:
            get_logger().exception("保存网络调试设置失败")

    @safe_slot
    def _settings_changed(self, *_args) -> None:
        self._save_settings()
        self._apply_encoding_to_logs()

    # ---- 模式与连接 ---------------------------------------------------

    def _active_mode(self) -> str:
        return _combo_value(self.mode_combo, MODE_ITEMS, "tcp")

    def _active_log(self) -> LogPane:
        return self.tcp_log if self._active_mode() == "tcp" else self.udp_log

    def _current_encoding(self) -> str:
        value = _combo_value(self.encoding_combo, ENCODING_ITEMS, "utf-8")
        try:
            return resolve_codec(value)
        except Exception:
            return "utf-8"

    def _apply_encoding_to_logs(self) -> None:
        codec = self._current_encoding()
        for pane in (self.tcp_log, self.udp_log):
            try:
                pane.set_encoding(codec)
            except Exception:
                get_logger().exception("应用网络日志编码失败")

    @safe_slot
    def _on_mode_changed(self, _index: int) -> None:
        self._apply_mode()
        self._save_settings()

    def _apply_mode(self) -> None:
        tcp_mode = self._active_mode() == "tcp"
        for widget in (
            self.tcp_port_label,
            self.port_spin,
            self.tcp_local_label,
            self.tcp_local_spin,
        ):
            widget.setVisible(tcp_mode)
        for widget in (
            self.udp_port_label,
            self.udp_port_spin,
            self.udp_local_label,
            self.udp_local_spin,
        ):
            widget.setVisible(not tcp_mode)
        self.connect_button.setText("连接" if tcp_mode else "绑定")
        self.disconnect_button.setText("断开" if tcp_mode else "解绑")
        self.log_tabs.setCurrentWidget(self.tcp_log if tcp_mode else self.udp_log)
        self._refresh_chip()
        self._update_connection_controls()
        self._update_connection_info()

    def _refresh_chip(self) -> None:
        if self._active_mode() == "tcp":
            if self.controller.is_tcp_connected():
                remote_host, remote_port = self.controller.tcp_remote()
                self.status_chip.set_state("ok", f"已连接 {remote_host}:{remote_port}")
            elif self._tcp_connecting:
                self.status_chip.set_state("busy", "正在连接…")
            else:
                self.status_chip.set_state("idle", "未连接")
        else:
            if self.controller.is_udp_bound():
                self.status_chip.set_state(
                    "ok", f"UDP 已绑定 本地 {self.controller.udp_local_port()}"
                )
            else:
                self.status_chip.set_state("idle", "UDP 未绑定")

    def _update_connection_controls(self) -> None:
        if self._active_mode() == "tcp":
            ready = self.controller.is_tcp_connected()
            busy = self._tcp_connecting
        else:
            ready = self.controller.is_udp_bound()
            busy = False
        self.connect_button.setEnabled(not ready and not busy)
        self.disconnect_button.setEnabled(ready or busy)

    def _update_connection_info(self) -> None:
        tcp_text = (
            f"TCP 已连接 {self.controller.tcp_remote()[0]}:"
            f"{self.controller.tcp_remote()[1]}"
            if self.controller.is_tcp_connected()
            else "TCP 未连接"
        )
        udp_text = (
            f"UDP 已绑定本地端口 {self.controller.udp_local_port()}"
            if self.controller.is_udp_bound()
            else "UDP 未绑定"
        )
        self.connection_info_label.setText(f"{tcp_text}；{udp_text}")

    def _tcp_target(self) -> tuple[str, int] | None:
        host = self.host_edit.text().strip()
        if not host:
            self.status_chip.set_state("error", "目标地址为空")
            show_error(
                self,
                "目标地址为空",
                "请输入目标 IP 或域名，例如 127.0.0.1；也可以先点击“域名转 IP”。",
            )
            return None
        return host, int(self.port_spin.value())

    def _udp_target(self) -> tuple[str, int] | None:
        host = self.host_edit.text().strip()
        if not host:
            self.status_chip.set_state("error", "目标地址为空")
            show_error(
                self,
                "目标地址为空",
                "请输入目标 IP 或域名，例如 127.0.0.1。",
            )
            return None
        return host, int(self.udp_port_spin.value())

    @safe_slot
    def _connect_clicked(self) -> None:
        if self._active_mode() == "tcp":
            if self.controller.is_tcp_connected() or self._tcp_connecting:
                return
            target = self._tcp_target()
            if target is None:
                return
            host, port = target
            self._save_settings()
            self.status_chip.set_state("busy", "正在连接…")
            self.controller.connect_tcp(host, port, int(self.tcp_local_spin.value()))
        else:
            if self.controller.is_udp_bound():
                return
            self._save_settings()
            self.status_chip.set_state("busy", "正在绑定…")
            self.controller.bind_udp(int(self.udp_local_spin.value()))
        self._update_connection_controls()

    @safe_slot
    def _disconnect_clicked(self) -> None:
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        if self._active_mode() == "tcp":
            self.controller.disconnect_tcp()
        else:
            self.controller.close_udp()

    @safe_slot
    def _on_tcp_state(self, state: str) -> None:
        if state == "connecting":
            self._tcp_connecting = True
        elif state == "connected":
            self._tcp_connecting = False
            if not self._tcp_was_connected:
                host, port = self.controller.tcp_remote()
                self.tcp_log.append_info(f"已连接 {host}:{port}")
            self._tcp_was_connected = True
            if self.repeat_check.isChecked():
                self._start_repeat()
        elif state == "disconnected":
            self._tcp_connecting = False
            if self._tcp_was_connected:
                self.tcp_log.append_info("TCP 连接已断开")
            self._tcp_was_connected = False
            self._stop_repeat()
            self._cancel_file_send(silent=True)
        if self._active_mode() == "tcp":
            self._refresh_chip()
            self._update_connection_controls()
        self._update_connection_info()

    @safe_slot
    def _on_udp_state(self, state: str) -> None:
        if state == "bound":
            if not self._udp_was_bound:
                self.udp_log.append_info(
                    f"UDP 已绑定本地端口 {self.controller.udp_local_port()}"
                )
            self._udp_was_bound = True
            if self.repeat_check.isChecked():
                self._start_repeat()
        elif state == "closed":
            if self._udp_was_bound:
                self.udp_log.append_info("UDP 已解绑")
            self._udp_was_bound = False
            self._stop_repeat()
            self._cancel_file_send(silent=True)
        if self._active_mode() == "udp":
            self._refresh_chip()
            self._update_connection_controls()
        self._update_connection_info()

    @safe_slot
    def _on_tcp_data(self, data: bytes) -> None:
        self.tcp_log.append_bytes(data, "rx")
        self._refresh_counters()

    @safe_slot
    def _on_udp_data(self, data: bytes) -> None:
        self.udp_log.append_bytes(data, "rx")
        self._refresh_counters()

    @safe_slot
    def _on_error(self, message: str) -> None:
        text = str(message)
        self._active_log().append_info(f"错误：{text}")
        self.status_chip.set_state("error", "网络错误")
        self._update_connection_controls()
        show_error(self, "网络错误", text)

    # ---- 发送 ---------------------------------------------------------

    def _build_payload(self, text: str) -> bytes | None:
        if self.hex_send_check.isChecked():
            try:
                payload = parse_hex(text)
            except ValueError as exc:
                show_error(self, "HEX 格式错误", str(exc))
                return None
        else:
            try:
                payload = encode_text(text, self._current_encoding())
            except (UnicodeError, LookupError) as exc:
                show_error(
                    self,
                    "编码失败",
                    f"当前编码无法表示输入内容：{exc}。请改用 UTF-8 编码后重试。",
                )
                return None
        if not payload:
            show_warning(self, "没有数据", "请输入要发送的内容。")
            return None
        return payload

    def _mode_ready(self, mode: str) -> bool:
        if self._is_mode_ready(mode):
            return True
        if mode == "tcp":
            show_warning(self, "TCP 未连接", "请先连接目标服务器再发送数据。")
        else:
            show_warning(self, "UDP 未绑定", "请先绑定本地 UDP 端口再发送数据。")
        return False

    def _is_mode_ready(self, mode: str) -> bool:
        if mode == "tcp":
            return self.controller.is_tcp_connected()
        return self.controller.is_udp_bound()

    def _transmit(self, mode: str, payload: bytes) -> bool:
        if mode == "tcp":
            ok = self.controller.send_tcp(payload)
            pane = self.tcp_log
        else:
            target = self._udp_target()
            if target is None:
                return False
            ok = self.controller.send_udp(payload, *target)
            pane = self.udp_log
        if ok:
            pane.append_bytes(payload, "tx")
            self._refresh_counters()
        return bool(ok)

    def _send_via(self, mode: str) -> bool:
        if not self._mode_ready(mode):
            return False
        payload = self._build_payload(self.send_editor.toPlainText())
        if payload is None:
            return False
        return self._transmit(mode, payload)

    @safe_slot
    def _send_tcp_clicked(self) -> bool:
        return self._send_via("tcp")

    @safe_slot
    def _send_udp_clicked(self) -> bool:
        return self._send_via("udp")

    @safe_slot
    def _send_current(self) -> bool:
        return self._send_via(self._active_mode())

    @safe_slot
    def _send_repeat(self) -> None:
        if not self._send_current():
            self._stop_repeat()

    @safe_slot
    def _on_repeat_toggled(self, checked: bool) -> None:
        if checked and self._is_mode_ready(self._active_mode()):
            self._start_repeat()
        elif not checked:
            self._stop_repeat()

    def _start_repeat(self) -> None:
        self._repeat_timer.start(max(10, int(self.interval_spin.value())))

    def _stop_repeat(self) -> None:
        self._repeat_timer.stop()
        if self.repeat_check.isChecked():
            blocked = self.repeat_check.blockSignals(True)
            try:
                self.repeat_check.setChecked(False)
            finally:
                self.repeat_check.blockSignals(blocked)

    @safe_slot
    def _on_interval_changed(self, value: int) -> None:
        self._repeat_timer.setInterval(max(10, int(value)))

    # ---- 文件发送 -----------------------------------------------------

    @safe_slot
    def _choose_file(self) -> None:
        mode = self._active_mode()
        if not self._mode_ready(mode):
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
        self._file_mode = mode
        self.tcp_send_button.setEnabled(False)
        self.udp_send_button.setEnabled(False)
        self.send_file_button.setEnabled(False)
        self.cancel_file_button.setEnabled(True)
        self._update_file_label()
        self._file_timer.start()

    @safe_slot
    def _send_file_chunk(self) -> None:
        if self._file_handle is None:
            self._stop_file_timer()
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
        if not self._transmit(self._file_mode, chunk):
            self._cancel_file_send()
            return
        self._file_sent += len(chunk)
        self._update_file_label()

    def _finish_file_send(self) -> None:
        self._close_file_handle()
        self._stop_file_timer()
        self.file_progress_label.setText(
            f"文件发送完成：{self._file_name}（{self._file_sent} B）"
        )
        self._set_file_buttons_idle()
        show_success(self, "文件发送完成", self._file_name)

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
                get_logger().exception("关闭发送文件句柄失败")
            finally:
                self._file_handle = None

    def _stop_file_timer(self) -> None:
        self._file_timer.stop()

    def _set_file_buttons_idle(self) -> None:
        self.tcp_send_button.setEnabled(True)
        self.udp_send_button.setEnabled(True)
        self.send_file_button.setEnabled(True)
        self.cancel_file_button.setEnabled(False)

    def _update_file_label(self) -> None:
        self.file_progress_label.setText(
            f"{self._file_name}：{self._file_sent} / {self._file_size} B"
        )

    # ---- 会话与日志 ---------------------------------------------------

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
        records = list(self.tcp_log.session().records()) + list(
            self.udp_log.session().records()
        )
        records.sort(key=lambda item: item.timestamp)
        total = max(sum(len(item.data) for item in records), 1)
        combined = SessionLog(max_bytes=total, max_records=max(len(records), 1))
        for record in records:
            combined.add(record)
        return combined

    def _refresh_counters(self) -> None:
        rx = self.tcp_log.session().total_bytes("rx") + self.udp_log.session().total_bytes(
            "rx"
        )
        tx = self.tcp_log.session().total_bytes("tx") + self.udp_log.session().total_bytes(
            "tx"
        )
        self.counter_label.setText(f"接收 {_format_bytes(rx)} · 发送 {_format_bytes(tx)}")

    def _current_log(self) -> LogPane:
        widget = self.log_tabs.currentWidget()
        return widget if isinstance(widget, LogPane) else self.tcp_log

    @safe_slot
    def _clear_current_log(self) -> None:
        self._current_log().clear()

    @safe_slot
    def _save_current_log(self) -> None:
        pane = self._current_log()
        default_name = f"net-{datetime.now():%Y%m%d-%H%M%S}.txt"
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

    # ---- 网络工具 -----------------------------------------------------

    def _run_task(
        self,
        func: Callable[[], object],
        on_success: Callable[[object], None],
        error_title: str,
    ) -> None:
        task = _BackgroundTask(func)
        task.signals.finished.connect(
            lambda ok, result, t=task, cb=on_success, title=error_title: self._finish_task(
                t, ok, result, cb, title
            )
        )
        self._tasks.add(task)
        QThreadPool.globalInstance().start(task)

    @safe_slot
    def _finish_task(
        self,
        task: _BackgroundTask,
        ok: bool,
        result: object,
        callback: Callable[[object], None],
        error_title: str,
    ) -> None:
        try:
            if self._closing:
                return
            if not ok:
                show_error(self, error_title, str(result))
                return
            callback(result)
        finally:
            self._tasks.discard(task)

    @safe_slot
    def _ping_target(self) -> None:
        host = self.host_edit.text().strip()
        if not host:
            show_error(self, "目标地址为空", "请先输入要 Ping 的 IP 或域名。")
            return
        self._run_task(
            lambda: ping_host(host, count=4, timeout_s=15),
            lambda result: self._show_ping_result(result),
            "Ping 失败",
        )

    def _show_ping_result(self, result) -> None:
        ok = bool(getattr(result, "ok", False))
        output = str(getattr(result, "output", ""))
        host = str(getattr(result, "host", ""))
        conclusion = "成功" if ok else "失败或被目标拒绝"
        text = f"目标：{host}\n结论：{conclusion}\n\n{output}"
        self._show_info_dialog(f"Ping {host}", text, note="使用系统 ping.exe，不使用原始套接字。")

    @safe_slot
    def _resolve_domain(self) -> None:
        host = self.host_edit.text().strip()
        if not host:
            show_error(self, "目标地址为空", "请先输入要解析的域名或 IP 地址。")
            return
        self._run_task(
            lambda: resolve_host(host),
            lambda result: self._show_dns_result(host, result),
            "域名解析失败",
        )

    def _show_dns_result(self, host: str, addresses) -> None:
        items = list(addresses or [])
        text = "\n".join(items) if items else "没有解析到地址。"
        self._show_info_dialog(
            f"域名转 IP：{host}",
            f"域名：{host}\n共 {len(items)} 个地址：\n\n{text}",
        )

    @safe_slot
    def _show_adapter_info(self) -> None:
        self._run_task(
            adapter_info,
            lambda result: self._show_adapter_result(result),
            "读取网卡信息失败",
        )

    def _show_adapter_result(self, adapters) -> None:
        lines: list[str] = []
        for item in list(adapters or []):
            lines.append(
                f"[{item.get('name', '?')}] "
                f"{'已启用' if item.get('isup') else '未启用'}"
            )
            addresses = item.get("addresses") or []
            lines.append(f"  地址：{', '.join(addresses) if addresses else '—'}")
            lines.append(f"  掩码：{item.get('netmask') or '—'}")
            lines.append(f"  网关：{item.get('gateway') or '—'}")
            lines.append(f"  MAC：{item.get('mac') or '—'}")
            lines.append("")
        text = "\n".join(lines).rstrip()
        self._show_info_dialog(
            "网卡信息",
            text or "未检测到网卡信息。",
            note="网关来自系统默认路由（route print -4）。",
        )

    def _show_info_dialog(self, title: str, text: str, note: str = "") -> None:
        if self._closing:
            return
        dialog = TextInfoDialog(title, text, self, note=note)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.finished.connect(
            lambda _result=None, target=dialog: self._forget_dialog(target)
        )
        self._info_dialogs.append(dialog)
        dialog.show()

    def _forget_dialog(self, dialog: TextInfoDialog) -> None:
        try:
            self._info_dialogs.remove(dialog)
        except ValueError:
            pass

    # ---- 生命周期 -----------------------------------------------------

    def has_active_work(self) -> bool:
        return bool(
            self.controller.is_tcp_connected()
            or self.controller.is_udp_bound()
            or self._repeat_timer.isActive()
            or self._file_timer.isActive()
        )

    def stop_workers(self) -> None:
        self._closing = True
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        try:
            self.controller.disconnect_tcp()
        except Exception:
            get_logger().exception("停止 TCP 客户端时失败")
        try:
            self.controller.close_udp()
        except Exception:
            get_logger().exception("停止 UDP 客户端时失败")

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


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.2f} MB"


def _default_export_dir(paths: AppPaths | None) -> Path:
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()

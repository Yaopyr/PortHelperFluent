"""网络服务器页：TCP 监听、客户端列表、按客户端发送、广播与文件发送。"""

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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
    StrongBodyLabel,
    TitleLabel,
)

from app.core.encoding import encode_text, parse_hex, resolve_codec
from app.core.logging_setup import get_logger
from app.core.settings import Settings
from app.io.net_server import NetServerController
from app.services.net_tools import adapter_info
from app.services.paths import AppPaths
from app.services.session_log import SessionLog
from app.ui.dialogs.session_dialog import SessionDialog
from app.ui.feedback import show_error, show_success, show_warning
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.log_pane import LogPane
from app.ui.widgets.status_chip import StatusChip

ENCODING_ITEMS = (("ANSI（系统）", "ansi"), ("GBK", "gbk"), ("UTF-8", "utf-8"))
FILE_CHUNK = 8192
TABLE_COLUMNS = ("客户端", "连接时间", "接收", "发送")


class NetServerPage(QWidget):
    """TCP 服务器工作台；监听与客户端收发由 NetServerController 驱动。"""

    def __init__(
        self,
        settings: Settings,
        paths: AppPaths,
        parent: QWidget | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("net-server")
        self.settings = settings
        self.paths = paths
        self.extra_options = dict(kwargs)
        self._loading = True
        self._closing = False
        self._client_labels: dict[str, str] = {}
        self._file_handle: BinaryIO | None = None
        self._file_name = ""
        self._file_size = 0
        self._file_sent = 0

        self._repeat_timer = QTimer(self)
        self._repeat_timer.setInterval(1000)
        self._repeat_timer.timeout.connect(self._send_repeat)

        self._file_timer = QTimer(self)
        self._file_timer.setInterval(5)
        self._file_timer.timeout.connect(self._send_file_chunk)

        self.controller = NetServerController(self)
        self._build_ui()
        self._connect_controller()
        self._restore_settings()
        self._loading = False
        self._update_status_chip()
        self._update_send_controls()

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(TitleLabel("网络服务器", self))
        title_box.addWidget(
            CaptionLabel("TCP 原始数据服务：多客户端、广播、文件发送与会话记录", self)
        )
        header.addLayout(title_box)
        header.addStretch(1)
        self.status_chip = StatusChip(self)
        self.status_chip.set_state("idle", "未监听")
        header.addWidget(self.status_chip)
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_column())
        splitter.addWidget(self._build_right_column())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([430, 810])
        root.addWidget(splitter, 1)

    def _build_left_column(self) -> QWidget:
        left = QWidget(self)
        left.setMinimumWidth(400)
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        server_card, server_layout = section_card(
            "监听设置", "0.0.0.0 表示全部网卡；端口 0 表示自动分配"
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        grid.addWidget(BodyLabel("监听地址", server_card), 0, 0)
        self.host_combo = ComboBox(server_card)
        for address in _listen_addresses():
            self.host_combo.addItem(address)
        self.host_combo.setToolTip("选择全部网卡（0.0.0.0）或指定本机网卡地址")
        grid.addWidget(self.host_combo, 0, 1)

        grid.addWidget(BodyLabel("端口", server_card), 1, 0)
        self.port_spin = SpinBox(server_card)
        self.port_spin.setRange(0, 65535)
        self.port_spin.setSpecialValueText("自动分配")
        self.port_spin.setValue(8080)
        self.port_spin.setToolTip("监听端口；0 表示由系统自动分配")
        self.port_spin.valueChanged.connect(self._settings_changed)
        grid.addWidget(self.port_spin, 1, 1)

        grid.addWidget(BodyLabel("最大客户端数", server_card), 2, 0)
        self.max_clients_spin = SpinBox(server_card)
        self.max_clients_spin.setRange(1, 64)
        self.max_clients_spin.setValue(16)
        self.max_clients_spin.setToolTip("超过上限的新连接会被拒绝并提示")
        self.max_clients_spin.valueChanged.connect(self._settings_changed)
        grid.addWidget(self.max_clients_spin, 2, 1)
        server_layout.addLayout(grid)

        self.bind_info_label = CaptionLabel("尚未启动监听", server_card)
        self.bind_info_label.setWordWrap(True)
        server_layout.addWidget(self.bind_info_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.start_button = PrimaryPushButton("启动监听", server_card)
        self.start_button.clicked.connect(self._start_clicked)
        buttons.addWidget(self.start_button)
        self.stop_button = PushButton("停止监听", server_card)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_clicked)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        server_layout.addLayout(buttons)
        layout.addWidget(server_card)

        clients_card, clients_layout = section_card(
            "客户端列表", "选中客户端后可单独发送；勾选广播则发给全部"
        )
        self.client_table = QTableWidget(0, len(TABLE_COLUMNS), clients_card)
        self.client_table.setHorizontalHeaderLabels(list(TABLE_COLUMNS))
        self.client_table.verticalHeader().setVisible(False)
        self.client_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.client_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.client_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.client_table.setAlternatingRowColors(True)
        self.client_table.setWordWrap(False)
        self.client_table.setMinimumHeight(160)
        header = self.client_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(TABLE_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.client_table.itemSelectionChanged.connect(self._update_send_controls)
        clients_layout.addWidget(self.client_table, 1)

        clients_footer = QHBoxLayout()
        clients_footer.setSpacing(8)
        self.client_count_label = CaptionLabel("共 0 个客户端", clients_card)
        clients_footer.addWidget(self.client_count_label)
        clients_footer.addStretch(1)
        self.disconnect_client_button = PushButton("断开选中", clients_card)
        self.disconnect_client_button.setEnabled(False)
        self.disconnect_client_button.clicked.connect(self._disconnect_selected)
        clients_footer.addWidget(self.disconnect_client_button)
        clients_layout.addLayout(clients_footer)
        layout.addWidget(clients_card, 1)
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
        self.log_pane = LogPane(
            "接收区", show_direction=True, settings=self.settings, parent=log_card
        )
        self.log_pane.log_view.setPlaceholderText(
            "客户端数据会显示在 来源 IP:端口 前缀之后"
        )
        log_layout.addWidget(self.log_pane, 1)
        layout.addWidget(log_card, 3)

        send_card, send_layout = section_card(
            "发送区", "发给选中客户端或广播；Ctrl+Enter 发送"
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
        for label, _value in ENCODING_ITEMS:
            self.encoding_combo.addItem(label)
        self.encoding_combo.currentIndexChanged.connect(self._settings_changed)
        options.addWidget(self.encoding_combo)
        self.broadcast_check = CheckBox("广播给全部客户端", send_card)
        self.broadcast_check.toggled.connect(self._update_send_controls)
        options.addWidget(self.broadcast_check)
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
        self.send_button = PrimaryPushButton("发送", send_card)
        self.send_button.clicked.connect(self._send_current)
        send_buttons.addWidget(self.send_button)
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
        self.clear_button = PushButton("清空接收区", send_card)
        self.clear_button.clicked.connect(self._clear_log)
        footer.addWidget(self.clear_button)
        self.save_button = PushButton("保存接收区", send_card)
        self.save_button.clicked.connect(self._save_log)
        footer.addWidget(self.save_button)
        self.session_button = PushButton("会话记录", send_card)
        self.session_button.clicked.connect(self._open_session)
        footer.addWidget(self.session_button)
        send_layout.addLayout(footer)
        layout.addWidget(send_card, 2)

        self._shortcut_send = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._shortcut_send.activated.connect(self._send_current)
        self._shortcut_send2 = QShortcut(QKeySequence("Ctrl+Enter"), self)
        self._shortcut_send2.activated.connect(self._send_current)
        self._shortcut_clear = QShortcut(QKeySequence("Ctrl+K"), self)
        self._shortcut_clear.activated.connect(self._clear_log)
        self._shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self._shortcut_save.activated.connect(self._save_log)
        return right

    def _connect_controller(self) -> None:
        self.controller.started.connect(self._on_started)
        self.controller.stopped.connect(self._on_stopped)
        self.controller.client_connected.connect(self._on_client_connected)
        self.controller.client_disconnected.connect(self._on_client_disconnected)
        self.controller.data_received.connect(self._on_data_received)
        self.controller.error.connect(self._on_error)

    # ---- 设置 ---------------------------------------------------------

    def _restore_settings(self) -> None:
        self.port_spin.setValue(_int_or(self.settings.get("server.port", 8080), 8080))
        self.max_clients_spin.setValue(
            _int_or(self.settings.get("server.max_clients", 16), 16)
        )
        _select_combo_value(
            self.encoding_combo,
            ENCODING_ITEMS,
            self.settings.get("server.encoding", "utf-8"),
        )
        self.hex_send_check.setChecked(bool(self.settings.get("server.hex_send", False)))
        self.log_pane.set_encoding(self._current_encoding())

    def _save_settings(self) -> None:
        if self._loading:
            return
        try:
            self.settings.update(
                {
                    "server.port": int(self.port_spin.value()),
                    "server.max_clients": int(self.max_clients_spin.value()),
                    "server.encoding": _combo_value(
                        self.encoding_combo, ENCODING_ITEMS, "utf-8"
                    ),
                    "server.hex_send": self.hex_send_check.isChecked(),
                }
            )
            self.settings.save()
        except Exception:
            get_logger().exception("保存网络服务器设置失败")

    @safe_slot
    def _settings_changed(self, *_args) -> None:
        self._save_settings()
        self._apply_encoding()

    def _current_encoding(self) -> str:
        value = _combo_value(self.encoding_combo, ENCODING_ITEMS, "utf-8")
        try:
            return resolve_codec(value)
        except Exception:
            return "utf-8"

    def _apply_encoding(self) -> None:
        try:
            self.log_pane.set_encoding(self._current_encoding())
        except Exception:
            get_logger().exception("应用服务器日志编码失败")

    # ---- 监听 ---------------------------------------------------------

    @safe_slot
    def _start_clicked(self) -> None:
        if self.controller.is_listening():
            return
        host = self.host_combo.currentText().strip() or "0.0.0.0"
        port = int(self.port_spin.value())
        self._save_settings()
        self.status_chip.set_state("busy", "正在启动…")
        self.start_button.setEnabled(False)
        started = self.controller.start(host, port, int(self.max_clients_spin.value()))
        if not started:
            self.start_button.setEnabled(True)
            if self.status_chip.state == "busy":
                self.status_chip.set_state("error", "启动失败")

    @safe_slot
    def _stop_clicked(self) -> None:
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        self.controller.stop()

    @safe_slot
    def _on_started(self, port: int) -> None:
        blocked = self.port_spin.blockSignals(True)
        try:
            self.port_spin.setValue(int(port))
        finally:
            self.port_spin.blockSignals(blocked)
        self._save_settings()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._set_bind_controls_enabled(False)
        host = self.host_combo.currentText().strip() or "0.0.0.0"
        self.bind_info_label.setText(f"正在监听 {host}:{port}")
        self.log_pane.append_info(f"服务器已启动，监听 {host}:{port}")
        self._update_status_chip()
        self._update_send_controls()

    @safe_slot
    def _on_stopped(self) -> None:
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        self.client_table.setRowCount(0)
        self._client_labels.clear()
        self.client_count_label.setText("共 0 个客户端")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._set_bind_controls_enabled(True)
        self.bind_info_label.setText("尚未启动监听")
        self.status_chip.set_state("idle", "未监听")
        self.log_pane.append_info("服务器已停止监听")
        self._refresh_counters()
        self._update_send_controls()

    def _set_bind_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.host_combo,
            self.port_spin,
            self.max_clients_spin,
        ):
            widget.setEnabled(enabled)

    @safe_slot
    def _on_error(self, message: str) -> None:
        text = str(message)
        self.log_pane.append_info(f"错误：{text}")
        if self.controller.is_listening():
            self._update_status_chip()
        else:
            self.status_chip.set_state("error", "服务器错误")
        show_error(self, "网络服务器错误", text)

    def _update_status_chip(self) -> None:
        if self.controller.is_listening():
            count = len(self.controller.clients())
            self.status_chip.set_state("ok", f"监听中 · {count} 个客户端")
        else:
            self.status_chip.set_state("idle", "未监听")

    # ---- 客户端列表 ---------------------------------------------------

    @safe_slot
    def _on_client_connected(self, client_id: str) -> None:
        info = self._client_info(client_id)
        if info is None:
            return
        label = f"{info.address}:{info.port}"
        self._client_labels[client_id] = label
        row = self.client_table.rowCount()
        self.client_table.insertRow(row)
        values = (
            label,
            f"{info.connected_at:%H:%M:%S}",
            _format_bytes(info.rx_bytes),
            _format_bytes(info.tx_bytes),
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column >= 2:
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            self.client_table.setItem(row, column, item)
        self.client_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, client_id)
        if self.client_table.currentRow() < 0:
            self.client_table.selectRow(row)
        self.log_pane.append_info(f"客户端已连接：{label}")
        self._update_client_count()
        self._update_status_chip()
        self._update_send_controls()

    @safe_slot
    def _on_client_disconnected(self, client_id: str) -> None:
        label = self._client_labels.pop(client_id, client_id)
        row = self._find_client_row(client_id)
        if row >= 0:
            self.client_table.removeRow(row)
        self.log_pane.append_info(f"客户端已断开：{label}")
        self._update_client_count()
        self._update_status_chip()
        self._update_send_controls()

    @safe_slot
    def _on_data_received(self, client_id: str, data: bytes) -> None:
        source = self._client_labels.get(client_id)
        if source is None:
            info = self._client_info(client_id)
            source = f"{info.address}:{info.port}" if info is not None else client_id
        self.log_pane.append_bytes(data, "rx", source)
        self._refresh_counters()
        self._refresh_client_table()

    def _client_info(self, client_id: str):
        for info in self.controller.clients():
            if info.client_id == client_id:
                return info
        return None

    def _find_client_row(self, client_id: str) -> int:
        for row in range(self.client_table.rowCount()):
            item = self.client_table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == client_id:
                return row
        return -1

    def _selected_client_id(self) -> str | None:
        row = self.client_table.currentRow()
        if row < 0:
            return None
        item = self.client_table.item(row, 0)
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def _refresh_client_table(self) -> None:
        infos = {info.client_id: info for info in self.controller.clients()}
        for row in range(self.client_table.rowCount()):
            item = self.client_table.item(row, 0)
            if item is None:
                continue
            info = infos.get(str(item.data(Qt.ItemDataRole.UserRole)))
            if info is None:
                continue
            self.client_table.item(row, 2).setText(_format_bytes(info.rx_bytes))
            self.client_table.item(row, 3).setText(_format_bytes(info.tx_bytes))

    def _update_client_count(self) -> None:
        self.client_count_label.setText(
            f"共 {self.client_table.rowCount()} 个客户端"
        )

    @safe_slot
    def _disconnect_selected(self) -> None:
        client_id = self._selected_client_id()
        if client_id is None:
            show_warning(self, "未选择客户端", "请先在客户端列表中选择一行。")
            return
        self.controller.disconnect_client(client_id)

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

    def _send_payload(self, payload: bytes, *, notify: bool = True) -> bool:
        clients = self.controller.clients()
        if not clients:
            if notify:
                show_warning(self, "没有客户端", "请先让客户端连接到服务器再发送。")
            return False
        if self.broadcast_check.isChecked():
            targets = clients
        else:
            client_id = self._selected_client_id()
            if client_id is None:
                if notify:
                    show_warning(
                        self, "未选择客户端", "请选择一行客户端，或勾选“广播给全部客户端”。"
                    )
                return False
            target = next(
                (info for info in clients if info.client_id == client_id), None
            )
            if target is None:
                if notify:
                    show_error(self, "客户端已断开", "所选客户端已断开，请重新选择。")
                return False
            targets = [target]
        sent = 0
        for info in targets:
            if self.controller.send_to(info.client_id, payload):
                self.log_pane.append_bytes(
                    payload, "tx", f"→ {info.address}:{info.port}"
                )
                sent += 1
        self._refresh_counters()
        self._refresh_client_table()
        if sent == 0 and notify:
            show_warning(self, "发送失败", "没有客户端接收到数据，请检查连接状态。")
        return sent > 0

    @safe_slot
    def _send_current(self) -> bool:
        payload = self._build_payload(self.send_editor.toPlainText())
        if payload is None:
            return False
        return self._send_payload(payload)

    @safe_slot
    def _send_repeat(self) -> None:
        if not self._send_current():
            self._stop_repeat()

    @safe_slot
    def _on_repeat_toggled(self, checked: bool) -> None:
        if checked and self.controller.clients():
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

    @safe_slot
    def _update_send_controls(self, *_args) -> None:
        has_clients = bool(self.controller.clients())
        selected = self._selected_client_id() is not None
        can_send = has_clients and (
            self.broadcast_check.isChecked() or selected
        )
        self.send_button.setEnabled(can_send)
        self.send_file_button.setEnabled(can_send)
        self.disconnect_client_button.setEnabled(selected)

    # ---- 文件发送 -----------------------------------------------------

    @safe_slot
    def _choose_file(self) -> None:
        if not self.controller.clients():
            show_warning(self, "没有客户端", "请先让客户端连接到服务器再发送文件。")
            return
        if not self.broadcast_check.isChecked() and self._selected_client_id() is None:
            show_warning(
                self, "未选择客户端", "请选择一行客户端，或勾选“广播给全部客户端”。"
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
        self.send_button.setEnabled(False)
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
        if not self._send_payload(chunk, notify=False):
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
        self.cancel_file_button.setEnabled(False)
        self._update_send_controls()

    def _update_file_label(self) -> None:
        self.file_progress_label.setText(
            f"{self._file_name}：{self._file_sent} / {self._file_size} B"
        )

    # ---- 日志与会话 ---------------------------------------------------

    def _refresh_counters(self) -> None:
        rx = self.log_pane.session().total_bytes("rx")
        tx = self.log_pane.session().total_bytes("tx")
        self.counter_label.setText(f"接收 {_format_bytes(rx)} · 发送 {_format_bytes(tx)}")

    @safe_slot
    def _clear_log(self) -> None:
        self.log_pane.clear()

    @safe_slot
    def _save_log(self) -> None:
        default_name = f"server-{datetime.now():%Y%m%d-%H%M%S}.txt"
        path_text, _selected = QFileDialog.getSaveFileName(
            self, "保存接收记录", default_name, "文本文件 (*.txt);;CSV 文件 (*.csv)"
        )
        if not path_text:
            return
        path = Path(path_text)
        try:
            if path.suffix.lower() == ".csv":
                self.log_pane.save_csv(path)
            else:
                if path.suffix.lower() != ".txt":
                    path = path.with_suffix(".txt")
                self.log_pane.save_txt(path)
        except OSError as exc:
            show_error(self, "保存失败", str(exc))
            return
        show_success(self, "日志已保存", str(path))

    @safe_slot
    def _open_session(self) -> None:
        dialog = SessionDialog(
            self._session_snapshot(),
            self,
            codec=self._current_encoding(),
            default_export_dir=_default_export_dir(self.paths),
        )
        dialog.exec()

    def _session_snapshot(self) -> SessionLog:
        records = list(self.log_pane.session().records())
        total = max(sum(len(item.data) for item in records), 1)
        snapshot = SessionLog(max_bytes=total, max_records=max(len(records), 1))
        for record in records:
            snapshot.add(record)
        return snapshot

    # ---- 生命周期 -----------------------------------------------------

    def has_active_work(self) -> bool:
        return bool(
            self.controller.is_listening()
            or self.controller.clients()
            or self._repeat_timer.isActive()
            or self._file_timer.isActive()
        )

    def stop_workers(self) -> None:
        self._closing = True
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        try:
            self.controller.stop()
        except Exception:
            get_logger().exception("停止网络服务器时失败")

    def closeEvent(self, event) -> None:
        self._save_settings()
        self.stop_workers()
        super().closeEvent(event)


def _listen_addresses() -> list[str]:
    """监听地址候选：0.0.0.0、回环地址以及本机 IPv4 地址（去重）。"""
    result = ["0.0.0.0", "127.0.0.1"]
    try:
        for item in adapter_info():
            for address in item.get("addresses", []):
                text = str(address).strip()
                if text and text not in result:
                    result.append(text)
    except Exception:
        get_logger().exception("读取本机网卡地址失败，仅保留 0.0.0.0")
    return result


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

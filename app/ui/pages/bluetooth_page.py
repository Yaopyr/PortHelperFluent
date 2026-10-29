"""蓝牙调试页：BLE 与经典蓝牙 SPP 共用一套日志与发送区。

页面只通过 Qt 信号与 `BleWorker`/`SppWorker` 交互，绝不直接调用 bleak/WinRT；
worker 在首次扫描、连接或打开 COM 口时才懒建，打开页面既不自动扫描也不自动连接。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Callable

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QTableWidgetItem,
    QTreeWidgetItem,
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
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SegmentedWidget,
    SpinBox,
    StrongBodyLabel,
    SwitchButton,
    TableWidget,
    TitleLabel,
    ToolButton,
    TreeWidget,
)

from app.core.encoding import encode_text, parse_hex, resolve_codec
from app.core.logging_setup import get_logger
from app.core.settings import Settings, resolve_page_encoding
from app.io.ble_worker import BleCharInfo, BleServiceInfo, BleWorker
from app.io.serial_worker import PortInfo, SerialConfig, list_ports
from app.io.spp_worker import ClassicDeviceInfo, SppWorker
from app.services.bluetooth import (
    chunk_bytes,
    characteristic_label,
    filter_devices,
    format_rssi,
    is_bluetooth_port,
    is_notifiable,
    is_readable,
    is_writable,
    properties_badges,
    rssi_quality,
    service_label,
    short_uuid,
    sort_devices,
    validate_pin,
)
from app.services.paths import AppPaths
from app.ui.dialogs.multi_send_dialog import MultiSendDialog
from app.ui.feedback import show_error, show_success, show_warning
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.log_pane import LogPane
from app.ui.widgets.page_layout import PANEL_MIN_WIDTH, apply_page_minimum, scroll_container
from app.ui.widgets.status_chip import StatusChip

MODE_BLE = "ble"
MODE_SPP = "spp"
MODE_LABELS = {MODE_BLE: "BLE", MODE_SPP: "SPP"}
MODE_TITLES = {MODE_BLE: "低功耗蓝牙（BLE）", MODE_SPP: "经典蓝牙（SPP）"}
SCAN_DURATION_ITEMS = (("5 秒", "5"), ("10 秒", "10"), ("20 秒", "20"))
SPP_SCAN_SECONDS = 12
SORT_ITEMS = (("信号强度", "rssi"), ("名称", "name"), ("最近发现", "recent"))
NEWLINE_ITEMS = (("不追加", "none"), ("CR", "cr"), ("LF", "lf"), ("CRLF", "crlf"))
NEWLINE_BYTES = {"none": b"", "cr": b"\r", "lf": b"\n", "crlf": b"\r\n"}
BAUD_CHOICES = ("9600", "19200", "38400", "57600", "115200", "230400")
DEVICE_COLUMNS = ("名称", "地址", "信号", "状态")
SPP_COLUMNS = ("名称", "地址", "配对", "通道")
FILE_CHUNK_BLE = 8192
FILE_CHUNK_SPP = 4096
ATT_PROTOCOL_OVERHEAD = 3
DEFAULT_MTU = 23
SHUTDOWN_REASON = "程序退出"
NOT_SELECTED_TEXT = "未选择"


class BluetoothPage(QWidget):
    """蓝牙调试工作台；所有蓝牙 I/O 由 BLE/SPP worker 的后台线程承担。"""

    def __init__(
        self,
        settings: Settings,
        paths: AppPaths,
        parent: QWidget | None = None,
        *,
        ble_worker_factory: Callable[..., object] | None = None,
        spp_worker_factory: Callable[..., object] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("bluetooth")
        apply_page_minimum(self)
        self.settings = settings
        self.paths = paths
        self.ble_worker_factory = ble_worker_factory
        self.spp_worker_factory = spp_worker_factory

        self._loading = True
        self._mode = MODE_BLE
        self._ble_worker = None
        self._spp_worker = None
        self._scanning = False
        self._scan_remaining = 0
        self._ble_connected = False
        self._ble_connecting = False
        self._ble_address = ""
        self._ble_name = ""
        self._self_disconnect_reason = ""
        self._ble_devices: dict[str, dict] = {}
        self._ble_services: list[BleServiceInfo] = []
        self._selected_char: BleCharInfo | None = None
        self._subscribed: set[str] = set()
        self._mtu = 0
        self._ignored_notifications = 0
        self._spp_connected = False
        self._spp_connecting = False
        self._spp_channel = ""
        self._spp_devices: list[ClassicDeviceInfo] = []
        self._ports_by_device: dict[str, PortInfo] = {}
        self._last_device = ""
        self._last_device_name = ""
        self._favorites: list[str] = []
        self._aliases: dict[str, str] = {}
        self._file_handle: BinaryIO | None = None
        self._file_name = ""
        self._file_size = 0
        self._file_sent = 0
        self._file_awaiting_ack = False
        self._file_pending_chunk = b""
        self._file_queue: list[bytes] = []
        self._file_pumping = False

        self._repeat_timer = QTimer(self)
        self._repeat_timer.setInterval(1000)
        self._repeat_timer.timeout.connect(self._send_repeat)
        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(1000)
        self._scan_timer.timeout.connect(self._on_scan_tick)

        self._build_ui()
        self._restore_settings()
        self._loading = False
        self._refresh_connection_state()
        self._refresh_counters()
        if self.auto_connect_switch.isChecked() and self._last_device:
            # 显式打开自动连接才补连；推迟到事件循环，构造期间不发任何蓝牙调用。
            QTimer.singleShot(0, self._maybe_auto_connect)

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(TitleLabel("蓝牙调试", self))
        title_box.addWidget(
            CaptionLabel("低功耗蓝牙与经典蓝牙串口，一页完成扫描、配对、收发", self)
        )
        header.addLayout(title_box)
        header.addStretch(1)
        self.status_chip = StatusChip(self)
        header.addWidget(self.status_chip)
        self.open_bt_settings_button = PushButton("打开蓝牙设置", self)
        self.open_bt_settings_button.setToolTip("打开 Windows 设置的蓝牙页面")
        self.open_bt_settings_button.clicked.connect(self._open_bluetooth_settings)
        self.open_bt_settings_button.setVisible(False)
        header.addWidget(self.open_bt_settings_button)
        root.addLayout(header)

        self.mode_segment = SegmentedWidget(self)
        self.mode_segment.addItem(MODE_BLE, MODE_TITLES[MODE_BLE])
        self.mode_segment.addItem(MODE_SPP, MODE_TITLES[MODE_SPP])
        self.mode_segment.currentItemChanged.connect(self._on_mode_changed)
        self.mode_segment.setCurrentItem(MODE_BLE)
        root.addWidget(self.mode_segment)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_column())
        splitter.addWidget(self._build_right_column())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([PANEL_MIN_WIDTH, 780])
        root.addWidget(splitter, 1)

    def _build_left_column(self) -> QWidget:
        # 左栏内容高于窗口时允许滚动，避免卡片把列表压扁造成重叠。
        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.left_stack = QStackedWidget(content)
        self.ble_panel = self._build_ble_panel()
        self.spp_panel = self._build_spp_panel()
        self.left_stack.addWidget(self.ble_panel)
        self.left_stack.addWidget(self.spp_panel)
        layout.addWidget(self.left_stack)
        layout.addStretch(1)
        return scroll_container(self, content, min_width=PANEL_MIN_WIDTH)

    def _build_ble_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        scan_card, scan_layout = section_card("扫描", "打开页面不会自动扫描；扫描期间可随时停止")
        scan_row = QHBoxLayout()
        scan_row.setSpacing(8)
        self.scan_button = PrimaryPushButton("开始扫描", scan_card)
        self.scan_button.clicked.connect(self._toggle_ble_scan)
        scan_row.addWidget(self.scan_button)
        self.scan_duration_combo = ComboBox(scan_card)
        _fill_combo(self.scan_duration_combo, SCAN_DURATION_ITEMS)
        self.scan_duration_combo.setCurrentIndex(1)
        scan_row.addWidget(self.scan_duration_combo)
        self.scan_remain_label = CaptionLabel("", scan_card)
        scan_row.addWidget(self.scan_remain_label)
        scan_row.addStretch(1)
        scan_layout.addLayout(scan_row)

        self.filter_edit = LineEdit(scan_card)
        self.filter_edit.setPlaceholderText("按名称或地址过滤")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_ble_filter_changed)
        scan_layout.addWidget(self.filter_edit)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(12)
        filter_row.addWidget(BodyLabel("排序", scan_card))
        self.sort_combo = ComboBox(scan_card)
        _fill_combo(self.sort_combo, SORT_ITEMS)
        self.sort_combo.currentIndexChanged.connect(self._on_ble_filter_changed)
        filter_row.addWidget(self.sort_combo)
        self.only_named_switch = SwitchButton(scan_card)
        self.only_named_switch.setText("仅显示有名称")
        self.only_named_switch.checkedChanged.connect(self._on_ble_filter_changed)
        filter_row.addWidget(self.only_named_switch)
        filter_row.addStretch(1)
        scan_layout.addLayout(filter_row)
        layout.addWidget(scan_card)

        device_card, device_layout = section_card(
            "设备", "收藏置顶；右键可连接、收藏、重命名或复制地址"
        )
        self.device_table = TableWidget(device_card)
        self.device_table.setColumnCount(len(DEVICE_COLUMNS))
        self.device_table.setHorizontalHeaderLabels(DEVICE_COLUMNS)
        self.device_table.verticalHeader().setVisible(False)
        self.device_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.device_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.device_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.device_table.setMinimumHeight(150)
        header = self.device_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(DEVICE_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.device_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.device_table.customContextMenuRequested.connect(self._show_device_menu)
        self.device_table.itemSelectionChanged.connect(self._on_ble_selection_changed)
        device_layout.addWidget(self.device_table)

        connect_row = QHBoxLayout()
        connect_row.setSpacing(8)
        self.connect_button = PrimaryPushButton("连接", device_card)
        self.connect_button.setEnabled(False)
        self.connect_button.clicked.connect(self._toggle_ble_connect)
        connect_row.addWidget(self.connect_button)
        self.auto_connect_switch = SwitchButton(device_card)
        self.auto_connect_switch.setText("打开页面自动连接上次设备")
        self.auto_connect_switch.checkedChanged.connect(self._on_auto_connect_toggled)
        connect_row.addWidget(self.auto_connect_switch)
        connect_row.addStretch(1)
        device_layout.addLayout(connect_row)
        layout.addWidget(device_card)

        info_card, info_layout = section_card(
            "设备信息", "MTU 由系统协商，只读；刷新不会重新连接"
        )
        self.device_summary_label = BodyLabel("名称：— · 地址：— · 服务数：0", info_card)
        self.device_summary_label.setWordWrap(True)
        info_layout.addWidget(self.device_summary_label)
        mtu_row = QHBoxLayout()
        mtu_row.setSpacing(8)
        self.mtu_label = StrongBodyLabel("当前 MTU：未知", info_card)
        self.mtu_label.setWordWrap(True)
        mtu_row.addWidget(self.mtu_label)
        self.mtu_refresh_button = ToolButton(info_card)
        self.mtu_refresh_button.setIcon(FluentIcon.SYNC)
        self.mtu_refresh_button.setToolTip("重新读取 MTU")
        self.mtu_refresh_button.setEnabled(False)
        self.mtu_refresh_button.clicked.connect(self._refresh_mtu)
        mtu_row.addWidget(self.mtu_refresh_button)
        mtu_row.addStretch(1)
        info_layout.addLayout(mtu_row)
        self.rssi_label = CaptionLabel("最后一次广播：未知", info_card)
        self.rssi_label.setWordWrap(True)
        info_layout.addWidget(self.rssi_label)
        layout.addWidget(info_card)

        service_card, service_layout = section_card("服务与特征值", "选中特征值后可读取、订阅或写入")
        tree_row = QHBoxLayout()
        tree_row.setSpacing(6)
        self.tree_expand_button = ToolButton(service_card)
        self.tree_expand_button.setIcon(FluentIcon.ARROW_DOWN)
        self.tree_expand_button.setToolTip("展开全部")
        self.tree_expand_button.clicked.connect(self._expand_services)
        tree_row.addWidget(self.tree_expand_button)
        self.tree_collapse_button = ToolButton(service_card)
        self.tree_collapse_button.setIcon(FluentIcon.CHEVRON_RIGHT)
        self.tree_collapse_button.setToolTip("折叠全部")
        self.tree_collapse_button.clicked.connect(self._collapse_services)
        tree_row.addWidget(self.tree_collapse_button)
        self.tree_filter_edit = LineEdit(service_card)
        self.tree_filter_edit.setPlaceholderText("按 UUID 或属性过滤")
        self.tree_filter_edit.setClearButtonEnabled(True)
        self.tree_filter_edit.textChanged.connect(self._apply_tree_filter)
        tree_row.addWidget(self.tree_filter_edit, 1)
        service_layout.addLayout(tree_row)

        self.service_tree = TreeWidget(service_card)
        self.service_tree.setHeaderHidden(True)
        self.service_tree.setMinimumHeight(180)
        self.service_tree.itemSelectionChanged.connect(self._on_char_selection_changed)
        self.service_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.service_tree.customContextMenuRequested.connect(self._show_service_menu)
        service_layout.addWidget(self.service_tree)
        layout.addWidget(service_card)

        char_card, char_layout = section_card("特征值操作", "订阅后收到的通知会写进右侧日志")
        char_row = QHBoxLayout()
        char_row.setSpacing(8)
        self.read_button = PushButton("读取", char_card)
        self.read_button.setEnabled(False)
        self.read_button.clicked.connect(self._read_char)
        char_row.addWidget(self.read_button)
        self.subscribe_button = PushButton("订阅", char_card)
        self.subscribe_button.setEnabled(False)
        self.subscribe_button.clicked.connect(self._toggle_subscribe)
        char_row.addWidget(self.subscribe_button)
        self.focus_write_button = PushButton("写入", char_card)
        self.focus_write_button.setToolTip("把焦点移到右侧发送区")
        self.focus_write_button.setEnabled(False)
        self.focus_write_button.clicked.connect(self._focus_send_editor)
        char_row.addWidget(self.focus_write_button)
        self.copy_uuid_button = PushButton("复制 UUID", char_card)
        self.copy_uuid_button.setEnabled(False)
        self.copy_uuid_button.clicked.connect(self._copy_selected_uuid)
        char_row.addWidget(self.copy_uuid_button)
        char_row.addStretch(1)
        char_layout.addLayout(char_row)
        self.ignore_empty_switch = SwitchButton(char_card)
        self.ignore_empty_switch.setText("忽略空通知")
        self.ignore_empty_switch.setChecked(True)
        self.ignore_empty_switch.checkedChanged.connect(self._settings_changed)
        char_layout.addWidget(self.ignore_empty_switch)
        layout.addWidget(char_card)
        layout.addStretch(1)
        return panel

    def _build_spp_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        scan_card, scan_layout = section_card(
            "扫描经典蓝牙", f"一次探寻约 {SPP_SCAN_SECONDS} 秒；打开页面不会自动扫描"
        )
        scan_row = QHBoxLayout()
        scan_row.setSpacing(8)
        self.spp_scan_button = PrimaryPushButton("开始扫描", scan_card)
        self.spp_scan_button.clicked.connect(self._toggle_spp_scan)
        scan_row.addWidget(self.spp_scan_button)
        self.spp_filter_edit = LineEdit(scan_card)
        self.spp_filter_edit.setPlaceholderText("按名称或地址过滤")
        self.spp_filter_edit.setClearButtonEnabled(True)
        self.spp_filter_edit.textChanged.connect(self._on_spp_filter_changed)
        scan_row.addWidget(self.spp_filter_edit, 1)
        scan_layout.addLayout(scan_row)
        self.only_paired_switch = SwitchButton(scan_card)
        self.only_paired_switch.setText("只看已配对设备")
        self.only_paired_switch.checkedChanged.connect(self._on_spp_filter_changed)
        scan_layout.addWidget(self.only_paired_switch)
        layout.addWidget(scan_card)

        device_card, device_layout = section_card("设备", "一键连接会自动配对并建立 RFCOMM 通道")
        self.spp_table = TableWidget(device_card)
        self.spp_table.setColumnCount(len(SPP_COLUMNS))
        self.spp_table.setHorizontalHeaderLabels(SPP_COLUMNS)
        self.spp_table.verticalHeader().setVisible(False)
        self.spp_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.spp_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.spp_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.spp_table.setMinimumHeight(160)
        spp_header = self.spp_table.horizontalHeader()
        spp_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(SPP_COLUMNS)):
            spp_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.spp_table.itemSelectionChanged.connect(self._on_spp_selection_changed)
        device_layout.addWidget(self.spp_table)

        connect_row = QHBoxLayout()
        connect_row.setSpacing(8)
        self.spp_connect_button = PrimaryPushButton("一键连接", device_card)
        self.spp_connect_button.setEnabled(False)
        self.spp_connect_button.clicked.connect(self._toggle_spp_connect)
        connect_row.addWidget(self.spp_connect_button)
        connect_row.addStretch(1)
        device_layout.addLayout(connect_row)
        self.pair_stage_label = CaptionLabel("尚未连接", device_card)
        self.pair_stage_label.setWordWrap(True)
        device_layout.addWidget(self.pair_stage_label)
        layout.addWidget(device_card)

        com_card, com_layout = section_card(
            "备用通道（系统蓝牙 COM 口）", "只有已配对且系统建出口时才可用"
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.addWidget(BodyLabel("COM 口", com_card), 0, 0)
        self.com_combo = EditableComboBox(com_card)
        self.com_combo.setPlaceholderText("COM5，可直接输入")
        self.com_combo.setMinimumWidth(150)
        grid.addWidget(self.com_combo, 0, 1)
        grid.addWidget(BodyLabel("波特率", com_card), 1, 0)
        self.baud_combo = EditableComboBox(com_card)
        self.baud_combo.addItems(BAUD_CHOICES)
        self.baud_combo.setCurrentText("115200")
        self.baud_combo.currentTextChanged.connect(self._settings_changed)
        grid.addWidget(self.baud_combo, 1, 1)
        com_layout.addLayout(grid)
        self.com_combo.currentTextChanged.connect(self._settings_changed)
        self.com_connect_button = PrimaryPushButton("打开 COM 口", com_card)
        self.com_connect_button.clicked.connect(self._toggle_com_connect)
        com_layout.addWidget(self.com_connect_button)
        layout.addWidget(com_card)

        info_card, info_layout = section_card(
            "通道信息", "RFCOMM 与 COM 通道同一时刻只保留一条"
        )
        self.spp_info_label = CaptionLabel("未连接", info_card)
        self.spp_info_label.setWordWrap(True)
        info_layout.addWidget(self.spp_info_label)
        layout.addWidget(info_card)
        layout.addStretch(1)
        return panel

    def _build_right_column(self) -> QWidget:
        right = QWidget(self)
        layout = QVBoxLayout(right)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        log_card = CardWidget(self)
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(12, 12, 12, 12)
        log_layout.setSpacing(8)
        self.log_pane = LogPane("", show_direction=True, settings=self.settings, parent=log_card)
        self.log_pane.log_view.setPlaceholderText("通知、读取结果与收发记录都会显示在这里")
        log_layout.addWidget(self.log_pane, 1)
        layout.addWidget(log_card, 3)

        send_card, send_layout = section_card("发送区", "Ctrl+Enter 发送；HEX 支持空格、逗号与 0x 前缀")
        self.target_label = CaptionLabel("写入目标：未选择可写特征值", send_card)
        self.target_label.setWordWrap(True)
        send_layout.addWidget(self.target_label)

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

        switches = QHBoxLayout()
        switches.setSpacing(18)
        self.write_with_response_switch = SwitchButton(send_card)
        self.write_with_response_switch.setText("写入需应答")
        self.write_with_response_switch.setChecked(True)
        self.write_with_response_switch.checkedChanged.connect(self._settings_changed)
        switches.addWidget(self.write_with_response_switch)
        self.mtu_chunk_switch = SwitchButton(send_card)
        self.mtu_chunk_switch.setText("按 MTU 分片")
        self.mtu_chunk_switch.setChecked(True)
        self.mtu_chunk_switch.checkedChanged.connect(self._settings_changed)
        switches.addWidget(self.mtu_chunk_switch)
        switches.addStretch(1)
        send_layout.addLayout(switches)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.send_button = PrimaryPushButton("发送", send_card)
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
        self.multi_send_button = PushButton("多项发送", send_card)
        self.multi_send_button.clicked.connect(self._open_multi_send)
        buttons.addWidget(self.multi_send_button)
        buttons.addStretch(1)
        send_layout.addLayout(buttons)

        self.counter_label = StrongBodyLabel("接收 0 B · 发送 0 B", send_card)
        send_layout.addWidget(self.counter_label)
        self.file_progress_label = CaptionLabel("", send_card)
        send_layout.addWidget(self.file_progress_label)
        layout.addWidget(send_card, 2)

        self._shortcut_send = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._shortcut_send.activated.connect(self._send_current)
        self._shortcut_send2 = QShortcut(QKeySequence("Ctrl+Enter"), self)
        self._shortcut_send2.activated.connect(self._send_current)
        return right

    # ---- 设置 ---------------------------------------------------------

    def _restore_settings(self) -> None:
        favorites = self.settings.get("bluetooth.favorites", []) or []
        self._favorites = [str(item) for item in favorites if str(item).strip()]
        aliases = self.settings.get("bluetooth.aliases", {}) or {}
        if isinstance(aliases, dict):
            self._aliases = {str(key): str(value) for key, value in aliases.items() if value}
        self._last_device = str(self.settings.get("bluetooth.last_device", "") or "")
        self._last_device_name = str(self.settings.get("bluetooth.last_device_name", "") or "")

        mode = str(self.settings.get("bluetooth.mode", MODE_BLE) or MODE_BLE)
        self._mode = mode if mode in MODE_LABELS else MODE_BLE
        self.mode_segment.setCurrentItem(self._mode)
        self.left_stack.setCurrentIndex(0 if self._mode == MODE_BLE else 1)

        self.hex_send_check.setChecked(bool(self.settings.get("bluetooth.hex_send", False)))
        self.log_pane.set_hex_mode(bool(self.settings.get("bluetooth.hex_display", False)))
        self.write_with_response_switch.setChecked(
            bool(self.settings.get("bluetooth.write_with_response", True))
        )
        self.mtu_chunk_switch.setChecked(bool(self.settings.get("bluetooth.mtu_chunk", True)))
        self.ignore_empty_switch.setChecked(
            bool(self.settings.get("bluetooth.ignore_empty_notify", True))
        )
        self.auto_connect_switch.setChecked(
            bool(self.settings.get("bluetooth.auto_connect", False))
        )
        self.baud_combo.setCurrentText(str(self.settings.get("bluetooth.spp_baud", 115200)))
        self.refresh_com_ports()
        configured_port = str(self.settings.get("bluetooth.spp_com", "") or "").strip()
        if configured_port:
            self.com_combo.setCurrentText(configured_port)
        self.log_pane.set_encoding(self._current_encoding())

    def _save_settings(self) -> None:
        if self._loading:
            return
        try:
            self.settings.update(
                {
                    "bluetooth.mode": self._mode,
                    "bluetooth.last_device": self._last_device,
                    "bluetooth.last_device_name": self._last_device_name,
                    "bluetooth.auto_connect": self.auto_connect_switch.isChecked(),
                    "bluetooth.favorites": list(self._favorites),
                    "bluetooth.aliases": dict(self._aliases),
                    "bluetooth.hex_send": self.hex_send_check.isChecked(),
                    "bluetooth.hex_display": self.log_pane.hex_check.isChecked(),
                    "bluetooth.spp_baud": _int_or(self.baud_combo.currentText(), 115200),
                    "bluetooth.spp_com": self.com_combo.currentText().strip(),
                    "bluetooth.write_with_response": self.write_with_response_switch.isChecked(),
                    "bluetooth.mtu_chunk": self.mtu_chunk_switch.isChecked(),
                    "bluetooth.ignore_empty_notify": self.ignore_empty_switch.isChecked(),
                }
            )
            self.settings.save()
        except Exception:
            get_logger().exception("保存蓝牙设置失败")

    @safe_slot
    def _settings_changed(self, *_args) -> None:
        self._save_settings()

    def _current_encoding(self) -> str:
        """蓝牙页没有独立的编码设置，沿用设置页的默认编码。"""
        try:
            return resolve_codec(resolve_page_encoding(self.settings, "bluetooth.encoding"))
        except Exception:
            return "utf-8"

    @safe_slot
    def refresh_com_ports(self) -> None:
        """只列系统里的蓝牙虚拟串口；手动输入的端口写在编辑器里仍然可用。"""
        current = self.com_combo.currentText().strip()
        ports = [
            item
            for item in list_ports()
            if is_bluetooth_port(item.device, item.description, item.hwid)
        ]
        self._ports_by_device = {item.device: item for item in ports}
        blocked = self.com_combo.blockSignals(True)
        try:
            self.com_combo.clear()
            for item in ports:
                self.com_combo.addItem(item.device)
            if current:
                self.com_combo.setCurrentText(current)
            elif ports:
                self.com_combo.setCurrentText(ports[0].device)
        finally:
            self.com_combo.blockSignals(blocked)

    # ---- 模式 ---------------------------------------------------------

    def current_mode(self) -> str:
        return self._mode

    def _has_connection(self, mode: str) -> bool:
        if mode == MODE_BLE:
            return self._ble_connected or self._ble_connecting
        return self._spp_connected or self._spp_connecting

    @safe_slot
    def _on_mode_changed(self, key: str) -> None:
        if key not in MODE_LABELS or key == self._mode:
            return
        previous = self._mode
        if self._has_connection(previous):
            self._disconnect_mode(previous, f"已断开 {MODE_LABELS[previous]}：切换模式")
        self._mode = key
        self.left_stack.setCurrentIndex(0 if key == MODE_BLE else 1)
        self.log_pane.append_info(f"—— 切换到 {MODE_TITLES[key]} ——")
        self._refresh_connection_state()
        self._refresh_status_text()
        self._save_settings()

    def _disconnect_mode(self, mode: str, reason: str) -> None:
        """主动断开；连接在飞时 worker 不会回 `disconnected`，界面靠自己的状态位复位。"""
        if mode == MODE_BLE:
            worker = self._ble_worker
            self._ble_connected = False
            self._ble_connecting = False
        else:
            worker = self._spp_worker
            self._spp_connected = False
            self._spp_connecting = False
        if self._file_handle is not None:
            # 断开后文件再发不出去：一并取消，别让句柄把 has_active_work() 拖成常真。
            self._cancel_file_send()
        # 先记下主动断开的原因，worker 同步回 `disconnected` 时不再重复记一条日志。
        self._self_disconnect_reason = reason
        if worker is not None:
            try:
                worker.disconnect_device(reason)
            except Exception:
                get_logger().exception("断开蓝牙失败")
        self.log_pane.append_info(reason)
        self._refresh_connection_state()

    @safe_slot
    def _open_bluetooth_settings(self) -> None:
        QDesktopServices.openUrl(QUrl("ms-settings:bluetooth"))

    # ---- worker -------------------------------------------------------

    def _instantiate(self, factory: Callable[..., object] | None, default) -> object | None:
        if factory is not None and not callable(factory):
            get_logger().warning("蓝牙 worker 工厂不是可调用对象，功能未初始化")
            self.status_chip.set_state("error", "蓝牙未初始化")
            return None
        builder = factory if callable(factory) else default
        try:
            worker = builder()
        except Exception as exc:
            get_logger().exception("创建蓝牙工作对象失败")
            self.status_chip.set_state("error", "蓝牙不可用")
            show_error(self, "蓝牙初始化失败", str(exc))
            return None
        start = getattr(worker, "start", None)
        try:
            if callable(start):
                start()
        except Exception:
            get_logger().exception("启动蓝牙后台线程失败")
        return worker

    def _ensure_ble_worker(self):
        if self._ble_worker is not None:
            return self._ble_worker
        worker = self._instantiate(self.ble_worker_factory, BleWorker)
        if worker is None:
            return None
        self._ble_worker = worker
        worker.scan_started.connect(self._on_ble_scan_started)
        worker.scan_finished.connect(self._on_ble_scan_finished)
        worker.device_found.connect(self._on_ble_device_found)
        worker.connected.connect(self._on_ble_connected)
        worker.disconnected.connect(self._on_ble_disconnected)
        worker.services_ready.connect(self._on_ble_services_ready)
        worker.notification.connect(self._on_ble_notification)
        worker.read_result.connect(self._on_ble_read_result)
        worker.write_finished.connect(self._on_ble_write_finished)
        worker.mtu_changed.connect(self._on_ble_mtu_changed)
        worker.error.connect(self._on_bluetooth_error)
        worker.finished.connect(self._on_ble_finished)
        return worker

    def _ensure_spp_worker(self):
        if self._spp_worker is not None:
            return self._spp_worker
        worker = self._instantiate(self.spp_worker_factory, SppWorker)
        if worker is None:
            return None
        self._spp_worker = worker
        worker.scan_started.connect(self._on_spp_scan_started)
        worker.scan_finished.connect(self._on_spp_scan_finished)
        worker.devices_found.connect(self._on_spp_devices_found)
        worker.pairing_started.connect(self._on_spp_pairing_started)
        worker.pairing_prompt.connect(self._on_spp_pairing_prompt)
        worker.pairing_finished.connect(self._on_spp_pairing_finished)
        worker.connected.connect(self._on_spp_connected)
        worker.disconnected.connect(self._on_spp_disconnected)
        worker.data_received.connect(self._on_spp_data_received)
        worker.data_sent.connect(self._on_spp_data_sent)
        worker.error.connect(self._on_bluetooth_error)
        worker.finished.connect(self._on_spp_finished)
        return worker

    @safe_slot
    def _on_ble_finished(self) -> None:
        self._ble_worker = None

    @safe_slot
    def _on_spp_finished(self) -> None:
        self._spp_worker = None

    # ---- BLE：扫描与设备表 --------------------------------------------

    @safe_slot
    def _toggle_ble_scan(self) -> None:
        worker = self._ensure_ble_worker()
        if worker is None:
            return
        if self._scanning:
            worker.stop_scan()
            return
        self._ble_devices.clear()
        self._render_ble_devices()
        seconds = _int_or(_combo_value(self.scan_duration_combo, SCAN_DURATION_ITEMS, "10"), 10)
        worker.start_scan(seconds)

    @safe_slot
    def _on_ble_scan_started(self, seconds: int) -> None:
        self._scanning = True
        self._scan_remaining = max(0, int(seconds))
        self.scan_button.setText("停止扫描")
        self.scan_remain_label.setText(f"剩余 {self._scan_remaining} 秒")
        self._scan_timer.start()
        self._refresh_status_text()

    @safe_slot
    def _on_ble_scan_finished(self) -> None:
        self._scanning = False
        self._scan_timer.stop()
        self._scan_remaining = 0
        self.scan_button.setText("开始扫描")
        self.scan_remain_label.setText("")
        self._refresh_status_text()

    @safe_slot
    def _on_scan_tick(self) -> None:
        if self._scan_remaining <= 0:
            self._scan_timer.stop()
            return
        self._scan_remaining -= 1
        self.scan_remain_label.setText(
            f"剩余 {self._scan_remaining} 秒" if self._scan_remaining else "正在结束扫描…"
        )

    @safe_slot
    def _on_ble_filter_changed(self, *_args) -> None:
        self._render_ble_devices()

    @safe_slot
    def _on_ble_device_found(self, info) -> None:
        address = str(getattr(info, "address", "") or "")
        if not address:
            return
        self._ble_devices[address] = {
            "name": str(getattr(info, "name", "") or ""),
            "address": address,
            "rssi": getattr(info, "rssi", None),
            "seen": datetime.now(),
        }
        if address == self._ble_address:
            self._update_rssi_label()
        self._render_ble_devices()

    def _is_favorite(self, address: str) -> bool:
        folded = address.casefold()
        return any(item.casefold() == folded for item in self._favorites)

    def _display_name(self, address: str, name) -> str:
        alias = self._aliases.get(address)
        if alias:
            return alias
        text = str(name or "").strip()
        return text or "（未命名）"

    def _render_ble_devices(self) -> None:
        # `device_found` 是逐条的，页面自己维护地址→设备的快照，每次整表重画。
        selected = self._selected_address()
        rows = filter_devices(
            list(self._ble_devices.values()),
            self.filter_edit.text(),
            only_named=self.only_named_switch.isChecked(),
        )
        rows = sort_devices(
            rows, _combo_value(self.sort_combo, SORT_ITEMS, "rssi"), self._favorites
        )
        table = self.device_table
        blocked = table.blockSignals(True)
        try:
            table.setRowCount(len(rows))
            for row, device in enumerate(rows):
                address = str(device.get("address") or "")
                favorite = self._is_favorite(address)
                status = "收藏" if favorite else ""
                if self._ble_connected and address == self._ble_address:
                    status = "已连接"
                values = (
                    f"{'★ ' if favorite else ''}{self._display_name(address, device.get('name'))}",
                    address,
                    format_rssi(device.get("rssi")),
                    status,
                )
                for column, text in enumerate(values):
                    item = QTableWidgetItem(str(text))
                    item.setToolTip(str(text))
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, device)
                    table.setItem(row, column, item)
        finally:
            table.blockSignals(blocked)
        self._sync_ble_selection(selected)
        self._refresh_connection_state()

    def _sync_ble_selection(self, address: str) -> None:
        table = self.device_table
        target = address or self._last_device
        if not target:
            return
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is None:
                continue
            device = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(device, dict) and str(device.get("address") or "") == target:
                table.selectRow(row)
                return

    def _selected_ble_device(self) -> dict | None:
        items = self.device_table.selectedItems()
        if not items:
            return None
        item = self.device_table.item(items[0].row(), 0)
        device = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return device if isinstance(device, dict) else None

    def _selected_address(self) -> str:
        device = self._selected_ble_device()
        return str(device.get("address") or "") if device else ""

    @safe_slot
    def _on_ble_selection_changed(self) -> None:
        self._refresh_connection_state()

    @safe_slot
    def _show_device_menu(self, position) -> None:
        device = self._selected_ble_device()
        if device is None:
            return
        address = str(device.get("address") or "")
        menu = QMenu(self)
        connect_action = menu.addAction("连接")
        favorite_action = menu.addAction("取消收藏" if self._is_favorite(address) else "收藏置顶")
        rename_action = menu.addAction("重命名…")
        copy_action = menu.addAction("复制地址")
        chosen = menu.exec(self.device_table.viewport().mapToGlobal(position))
        if chosen is connect_action:
            self._connect_device(address)
        elif chosen is favorite_action:
            self._toggle_favorite(address)
        elif chosen is rename_action:
            self._rename_device(address)
        elif chosen is copy_action:
            QApplication.clipboard().setText(address)
            show_success(self, "已复制地址", address)

    def _connect_device(self, address: str) -> None:
        """菜单「连接」：已连到同一设备就断开，否则选中它并发起连接。"""
        if self._ble_connected and address == self._ble_address:
            self._disconnect_mode(MODE_BLE, "已断开 BLE：用户断开")
            self._refresh_status_text()
            return
        self._sync_ble_selection(address)
        self._start_ble_connect(address)

    def _toggle_favorite(self, address: str) -> None:
        if not address:
            return
        folded = address.casefold()
        if self._is_favorite(address):
            self._favorites = [item for item in self._favorites if item.casefold() != folded]
        else:
            self._favorites.append(address)
        self._save_settings()
        self._render_ble_devices()

    def _rename_device(self, address: str) -> None:
        if not address:
            return
        device = self._ble_devices.get(address) or {}
        current = self._aliases.get(address) or str(device.get("name") or "")
        text, accepted = QInputDialog.getText(
            self, "重命名设备", f"{address} 的显示名称", QLineEdit.EchoMode.Normal, current
        )
        if not accepted:
            return
        alias = str(text).strip()
        if alias:
            self._aliases[address] = alias
        else:
            self._aliases.pop(address, None)
        self._save_settings()
        self._render_ble_devices()

    def _update_rssi_label(self) -> None:
        device = self._ble_devices.get(self._ble_address)
        if device is None:
            self.rssi_label.setText("最后一次广播：未知")
            return
        rssi = device.get("rssi")
        seen = device.get("seen")
        stamp = seen.strftime("%H:%M:%S") if isinstance(seen, datetime) else "未知"
        self.rssi_label.setText(
            f"最后一次广播：{format_rssi(rssi)}（{rssi_quality(rssi)}）· {stamp}"
        )

    # ---- BLE：连接与状态 ----------------------------------------------

    @safe_slot
    def _toggle_ble_connect(self) -> None:
        if self._ble_connected or self._ble_connecting:
            self._disconnect_mode(MODE_BLE, "已断开 BLE：用户断开")
            self._refresh_status_text()
            return
        device = self._selected_ble_device()
        if device is None:
            show_warning(self, "请选择设备", "先在上方列表里选择一个蓝牙设备。")
            return
        self._start_ble_connect(str(device.get("address") or ""))

    def _start_ble_connect(self, address: str) -> None:
        worker = self._ensure_ble_worker()
        if worker is None or not address:
            return
        if self._ble_connected or self._ble_connecting:
            # worker 在已连接时拒绝新的 connect_device，必须先断开。
            self._disconnect_mode(MODE_BLE, "已断开 BLE：切换设备")
        device = self._ble_devices.get(address) or {}
        self._ble_connecting = True
        self._ble_address = address
        self._ble_name = str(device.get("name") or self._aliases.get(address) or "")
        self._last_device = address
        self._last_device_name = self._ble_name
        self._refresh_connection_state()
        self._refresh_status_text()
        self._save_settings()
        if not worker.connect_device(address):
            self._ble_connecting = False
            self.status_chip.set_state("error", "已有连接正在进行，请先断开后再连接其他设备")
            self._refresh_connection_state()

    @safe_slot
    def _on_ble_connected(self, address: str, mtu: int) -> None:
        # 连接中途取消也能收到迟到的 connected，这里一律以信号为准对齐状态。
        self._ble_connecting = False
        self._ble_connected = True
        self._ble_address = str(address)
        self._mtu = int(mtu or 0)
        device = self._ble_devices.get(self._ble_address) or {}
        self._last_device = self._ble_address
        self._last_device_name = str(device.get("name") or self._ble_name or "")
        self._refresh_connection_state()
        self._refresh_status_text()
        self._update_rssi_label()
        self.log_pane.append_info(f"已连接 {self._ble_address} · MTU {self._mtu} B")
        self._render_ble_devices()
        self._save_settings()

    @safe_slot
    def _on_ble_disconnected(self, reason: str) -> None:
        self._ble_connected = False
        self._ble_connecting = False
        self._mtu = 0
        self._subscribed.clear()
        if self._file_handle is not None:
            # 连接没了文件发不完，直接取消，别让句柄一直挂着。
            self._cancel_file_send()
        self._refresh_connection_state()
        if reason and reason != self._self_disconnect_reason:
            self.status_chip.set_state("idle", f"已断开：{reason}")
            self.log_pane.append_info(f"已断开：{reason}")
        else:
            self._refresh_status_text()
        self._self_disconnect_reason = ""
        self._render_ble_devices()

    @safe_slot
    def _on_bluetooth_error(self, message: str) -> None:
        text = str(message) or "蓝牙操作失败"
        self._ble_connecting = False
        self._spp_connecting = False
        self._refresh_connection_state()
        self.status_chip.set_state("error", text)
        self.open_bt_settings_button.setVisible(True)
        self.log_pane.append_info(f"错误：{text}")
        show_error(self, "蓝牙错误", text)

    @safe_slot
    def _on_ble_mtu_changed(self, mtu: int) -> None:
        self._mtu = int(mtu or 0)
        self._update_mtu_label()
        self._refresh_send_state()

    @safe_slot
    def _refresh_mtu(self) -> None:
        worker = self._ble_worker
        if worker is None or not self._ble_connected:
            return
        worker.refresh_mtu()

    # ---- BLE：数据 -----------------------------------------------------

    @safe_slot
    def _on_ble_notification(self, char_uuid: str, data: bytes) -> None:
        payload = bytes(data)
        if not payload and self.ignore_empty_switch.isChecked():
            # 设备会周期性发空包保活；只计数不写日志，避免刷屏。
            self._ignored_notifications += 1
            self._refresh_counters()
            return
        self.log_pane.append_bytes(payload, "rx", source=short_uuid(char_uuid))
        self._refresh_counters()

    @safe_slot
    def _on_ble_read_result(self, char_uuid: str, data: bytes) -> None:
        self.log_pane.append_bytes(
            bytes(data), "rx", source=f"读取 {short_uuid(char_uuid)}"
        )
        self._refresh_counters()

    @safe_slot
    def _on_ble_write_finished(self, _char_uuid: str, length: int) -> None:
        self._handle_file_ack(int(length))

    # ---- BLE：服务树与特征值 ------------------------------------------

    @safe_slot
    def _on_ble_services_ready(self, services) -> None:
        self._ble_services = list(services or [])
        self._selected_char = None
        self._subscribed.clear()
        self._render_service_tree()
        self._update_device_summary()

    def _render_service_tree(self) -> None:
        selected_uuid = self._selected_char.uuid if self._selected_char else ""
        tree = self.service_tree
        blocked = tree.blockSignals(True)
        try:
            tree.clear()
            for service in self._ble_services:
                node = QTreeWidgetItem([service_label(service.uuid)])
                node.setData(0, Qt.ItemDataRole.UserRole, service)
                for char in service.characteristics:
                    badges = " · ".join(properties_badges(char.properties)) or "无属性"
                    suffix = " · 已订阅" if char.uuid in self._subscribed else ""
                    child = QTreeWidgetItem(
                        [f"{characteristic_label(char.uuid)}　[{badges}]{suffix}"]
                    )
                    child.setData(0, Qt.ItemDataRole.UserRole, char)
                    node.addChild(child)
                tree.addTopLevelItem(node)
            tree.expandAll()
        finally:
            tree.blockSignals(blocked)
        self._apply_tree_filter()
        if not (selected_uuid and self._select_char(selected_uuid)):
            self._selected_char = None
        self._refresh_char_actions()

    def _select_char(self, uuid: str) -> bool:
        tree = self.service_tree
        for index in range(tree.topLevelItemCount()):
            node = tree.topLevelItem(index)
            for child_index in range(node.childCount()):
                child = node.child(child_index)
                char = child.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(char, BleCharInfo) and char.uuid == uuid:
                    tree.setCurrentItem(child)
                    return True
        return False

    @safe_slot
    def _apply_tree_filter(self, *_args) -> None:
        needle = self.tree_filter_edit.text().strip().casefold()
        tree = self.service_tree
        for index in range(tree.topLevelItemCount()):
            node = tree.topLevelItem(index)
            service_text = node.text(0).casefold()
            visible = 0
            for child_index in range(node.childCount()):
                child = node.child(child_index)
                matches = (
                    not needle
                    or needle in child.text(0).casefold()
                    or needle in service_text
                )
                child.setHidden(not matches)
                visible += 1 if matches else 0
            node.setHidden(visible == 0)

    @safe_slot
    def _on_char_selection_changed(self) -> None:
        item = self.service_tree.currentItem()
        char = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        self._selected_char = char if isinstance(char, BleCharInfo) else None
        self._refresh_char_actions()

    def _refresh_char_actions(self) -> None:
        char = self._selected_char
        connected = self._ble_connected
        self.read_button.setEnabled(connected and char is not None and is_readable(char.properties))
        notifiable = connected and char is not None and is_notifiable(char.properties)
        self.subscribe_button.setEnabled(notifiable)
        self.subscribe_button.setText(
            "取消订阅" if notifiable and char is not None and char.uuid in self._subscribed else "订阅"
        )
        self.focus_write_button.setEnabled(
            connected and char is not None and is_writable(char.properties)
        )
        self.copy_uuid_button.setEnabled(char is not None)
        self._refresh_send_state()

    @safe_slot
    def _read_char(self) -> None:
        char = self._selected_char
        worker = self._ble_worker
        if char is None or worker is None or not self._ble_connected:
            return
        worker.read_char(char.uuid)

    @safe_slot
    def _toggle_subscribe(self) -> None:
        char = self._selected_char
        worker = self._ble_worker
        if char is None or worker is None or not self._ble_connected:
            return
        label = characteristic_label(char.uuid)
        if char.uuid in self._subscribed:
            if worker.unsubscribe(char.uuid):
                self._subscribed.discard(char.uuid)
                self.log_pane.append_info(f"已取消订阅 {label}")
        elif worker.subscribe(char.uuid):
            self._subscribed.add(char.uuid)
            self.log_pane.append_info(f"已订阅 {label}")
        self._render_service_tree()

    @safe_slot
    def _focus_send_editor(self) -> None:
        self.send_editor.setFocus()

    @safe_slot
    def _copy_selected_uuid(self) -> None:
        char = self._selected_char
        if char is None:
            return
        QApplication.clipboard().setText(char.uuid)
        show_success(self, "已复制 UUID", char.uuid)

    @safe_slot
    def _show_service_menu(self, position) -> None:
        """服务树右键：服务节点复制服务 UUID，特征值节点复制特征值 UUID。"""
        item = self.service_tree.itemAt(position) or self.service_tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if isinstance(data, BleServiceInfo):
            label = "复制服务 UUID"
        elif isinstance(data, BleCharInfo):
            label = "复制特征值 UUID"
        else:
            return
        menu = QMenu(self)
        copy_action = menu.addAction(label)
        chosen = menu.exec(self.service_tree.viewport().mapToGlobal(position))
        if chosen is copy_action:
            QApplication.clipboard().setText(data.uuid)
            show_success(self, "已复制 UUID", data.uuid)

    @safe_slot
    def _expand_services(self) -> None:
        self.service_tree.expandAll()

    @safe_slot
    def _collapse_services(self) -> None:
        self.service_tree.collapseAll()

    # ---- SPP：扫描与设备表 --------------------------------------------

    @safe_slot
    def _toggle_spp_scan(self) -> None:
        worker = self._ensure_spp_worker()
        if worker is None:
            return
        if worker.is_scanning():
            worker.stop_scan()
            return
        self._spp_devices = []
        self._render_spp_devices()
        worker.start_scan(SPP_SCAN_SECONDS)

    @safe_slot
    def _on_spp_scan_started(self, _seconds: int) -> None:
        self.spp_scan_button.setText("停止扫描")
        self._refresh_status_text()

    @safe_slot
    def _on_spp_scan_finished(self) -> None:
        self.spp_scan_button.setText("开始扫描")
        self._refresh_status_text()

    @safe_slot
    def _on_spp_devices_found(self, devices) -> None:
        # devices_found 每次都是全量快照，直接整表替换。
        self._spp_devices = list(devices or [])
        self._render_spp_devices()

    @safe_slot
    def _on_spp_filter_changed(self, *_args) -> None:
        self._render_spp_devices()

    def _render_spp_devices(self) -> None:
        selected = self._selected_spp_address()
        rows = filter_devices(
            [
                {"name": item.name, "address": item.address, "paired": bool(item.paired)}
                for item in self._spp_devices
            ],
            self.spp_filter_edit.text(),
            only_paired=self.only_paired_switch.isChecked(),
        )
        by_address = {item.address: item for item in self._spp_devices}
        table = self.spp_table
        blocked = table.blockSignals(True)
        try:
            table.setRowCount(len(rows))
            for row, device in enumerate(rows):
                address = str(device.get("address") or "")
                info = by_address.get(address)
                channel = ""
                if self._spp_connected and address == self._last_device:
                    channel = self._spp_channel
                values = (
                    self._display_name(address, device.get("name")),
                    address,
                    "已配对" if device.get("paired") else "未配对",
                    channel,
                )
                for column, text in enumerate(values):
                    item = QTableWidgetItem(str(text))
                    item.setToolTip(str(text))
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, info)
                    table.setItem(row, column, item)
        finally:
            table.blockSignals(blocked)
        self._sync_spp_selection(selected)
        self._refresh_connection_state()

    def _sync_spp_selection(self, address: str) -> None:
        table = self.spp_table
        target = address or self._last_device
        if not target:
            return
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is None:
                continue
            info = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(info, ClassicDeviceInfo) and info.address == target:
                table.selectRow(row)
                return

    def _selected_spp_device(self) -> ClassicDeviceInfo | None:
        items = self.spp_table.selectedItems()
        if not items:
            return None
        item = self.spp_table.item(items[0].row(), 0)
        info = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return info if isinstance(info, ClassicDeviceInfo) else None

    def _selected_spp_address(self) -> str:
        device = self._selected_spp_device()
        return device.address if device else ""

    @safe_slot
    def _on_spp_selection_changed(self) -> None:
        self._refresh_connection_state()

    # ---- SPP：一键连接与配对 ------------------------------------------

    @safe_slot
    def _toggle_spp_connect(self) -> None:
        if self._spp_connected or self._spp_connecting:
            self._disconnect_mode(MODE_SPP, "已断开 SPP：用户断开")
            self._refresh_status_text()
            return
        device = self._selected_spp_device()
        if device is None:
            show_warning(self, "请选择设备", "先在列表里选择一个经典蓝牙设备。")
            return
        self._start_spp_connect(device)

    def _start_spp_connect(self, device: ClassicDeviceInfo) -> None:
        worker = self._ensure_spp_worker()
        if worker is None:
            return
        if self._spp_connected or self._spp_connecting:
            self._disconnect_mode(MODE_SPP, "已断开 SPP：切换设备")
        self._spp_connecting = True
        self._spp_channel = ""
        self._last_device = device.address
        self._last_device_name = device.name
        self.pair_stage_label.setText("准备连接…")
        self._refresh_connection_state()
        self._refresh_status_text()
        self._save_settings()
        if not worker.connect_device(device):
            self._spp_connecting = False
            self.status_chip.set_state("error", "已有经典蓝牙连接正在进行，请先断开")
            self._refresh_connection_state()

    @safe_slot
    def _on_spp_pairing_started(self, name: str) -> None:
        self.pair_stage_label.setText(f"配对中…（{name}）")
        self.status_chip.set_state("busy", f"正在配对 {name}…")

    @safe_slot
    def _on_spp_pairing_prompt(self, kind: str, text: str) -> None:
        worker = self._spp_worker
        if worker is None:
            return
        if kind == "pin":
            value, accepted = QInputDialog.getText(
                self, "输入配对码", text, QLineEdit.EchoMode.Normal, "1234"
            )
            if not accepted:
                worker.answer_prompt(None)
                return
            error = validate_pin(str(value))
            if error:
                show_error(self, "配对码无效", error)
                worker.answer_prompt(None)
                return
            worker.answer_prompt(str(value))
            return
        reply = QMessageBox.question(
            self,
            "确认配对",
            f"{text}\n请在两台设备上核对配对码是否一致。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        worker.answer_prompt("ok" if reply == QMessageBox.StandardButton.Yes else None)

    @safe_slot
    def _on_spp_pairing_finished(self, text: str) -> None:
        self.pair_stage_label.setText(str(text) or "配对结束")

    @safe_slot
    def _on_spp_connected(self, channel: str, name: str) -> None:
        self._spp_connecting = False
        self._spp_connected = True
        self._spp_channel = str(channel)
        if name:
            self._last_device_name = str(name)
        self.pair_stage_label.setText(f"已连接 · {self._spp_channel}")
        self._refresh_connection_state()
        self.status_chip.set_state(
            "ok", f"已连接 {name or '经典蓝牙'}（{self._spp_channel}）"
        )
        self.log_pane.append_info(f"已连接 {name or '经典蓝牙'} · 通道 {self._spp_channel}")
        self._render_spp_devices()
        self._save_settings()

    @safe_slot
    def _on_spp_disconnected(self, reason: str) -> None:
        self._spp_connected = False
        self._spp_connecting = False
        self._spp_channel = ""
        self.pair_stage_label.setText("尚未连接")
        if self._file_handle is not None:
            # 连接没了文件发不完，直接取消，别让句柄一直挂着。
            self._cancel_file_send()
        self._refresh_connection_state()
        if reason and reason != self._self_disconnect_reason:
            self.status_chip.set_state("idle", f"已断开：{reason}")
            self.log_pane.append_info(f"已断开：{reason}")
        else:
            self._refresh_status_text()
        self._self_disconnect_reason = ""
        self._render_spp_devices()

    @safe_slot
    def _on_spp_data_received(self, data: bytes) -> None:
        self.log_pane.append_bytes(bytes(data), "rx", source=self._spp_channel)
        self._refresh_counters()

    @safe_slot
    def _on_spp_data_sent(self, data: bytes) -> None:
        payload = bytes(data)
        self.log_pane.append_bytes(payload, "tx", source=self._spp_channel)
        self._refresh_counters()
        self._handle_file_ack(len(payload))

    # ---- SPP：COM 备用通道 --------------------------------------------

    @safe_slot
    def _toggle_com_connect(self) -> None:
        if self._spp_connected or self._spp_connecting:
            self._disconnect_mode(MODE_SPP, "已断开 SPP：用户断开 COM 通道")
            self._refresh_status_text()
            return
        worker = self._ensure_spp_worker()
        if worker is None:
            return
        port = self.com_combo.currentText().strip()
        if not port:
            show_warning(self, "请选择 COM 口", "选择或输入系统蓝牙串口，例如 COM5。")
            return
        baud = _int_or(self.baud_combo.currentText(), 0)
        if baud <= 0:
            show_error(self, "波特率无效", "波特率必须是正整数，例如 115200。")
            return
        self._spp_connecting = True
        self._spp_channel = port
        self._last_device = port
        self._last_device_name = port
        self.pair_stage_label.setText(f"正在打开 {port}…")
        self._refresh_connection_state()
        self.status_chip.set_state("busy", f"正在打开 {port}…")
        self._save_settings()
        if not worker.open_com(SerialConfig(port=port, baud=baud)):
            self._spp_connecting = False
            self._spp_channel = ""
            self.status_chip.set_state(
                "error", f"打开 {port} 失败，请确认设备已配对且端口未被占用"
            )
            self._refresh_connection_state()

    def _update_spp_info(self) -> None:
        if not self._spp_connected:
            self.spp_info_label.setText("未连接")
            return
        name = self._last_device_name or "—"
        address = self._last_device or "—"
        if self._spp_channel.upper().startswith("COM"):
            text = (
                f"{name} · {address} · 通道 {self._spp_channel} · "
                f"{_int_or(self.baud_combo.currentText(), 115200)} bps"
            )
        else:
            text = f"{name} · {address} · 通道 {self._spp_channel}"
        self.spp_info_label.setText(text)

    # ---- 状态联动 -----------------------------------------------------

    def _refresh_connection_state(self) -> None:
        ble_active = self._ble_connected or self._ble_connecting
        self.connect_button.setText("断开" if ble_active else "连接")
        self.connect_button.setEnabled(ble_active or self._selected_ble_device() is not None)
        spp_active = self._spp_connected or self._spp_connecting
        self.spp_connect_button.setText("断开" if spp_active else "一键连接")
        self.spp_connect_button.setEnabled(spp_active or self._selected_spp_device() is not None)
        self.com_connect_button.setText("关闭 COM 口" if spp_active else "打开 COM 口")
        self.mtu_refresh_button.setEnabled(self._ble_connected)
        self._update_device_summary()
        self._update_mtu_label()
        self._update_spp_info()
        self._refresh_char_actions()

    def _update_device_summary(self) -> None:
        if not (self._ble_connected or self._ble_connecting) or not self._ble_address:
            self.device_summary_label.setText("名称：— · 地址：— · 服务数：0")
            return
        address = self._ble_address
        device = self._ble_devices.get(address) or {}
        name = self._display_name(address, device.get("name") or self._ble_name)
        count = len(self._ble_services) if self._ble_connected else 0
        self.device_summary_label.setText(
            f"名称：{name} · 地址：{address} · 服务数：{count}"
        )

    def _update_mtu_label(self) -> None:
        if self._mtu > 0:
            payload = max(1, self._mtu - ATT_PROTOCOL_OVERHEAD)
            self.mtu_label.setText(f"当前 MTU：{self._mtu} B（有效负载 {payload} B）")
        else:
            self.mtu_label.setText("当前 MTU：未知")

    def _write_target(self) -> BleCharInfo | None:
        char = self._selected_char
        if char is None or not self._ble_connected:
            return None
        return char if is_writable(char.properties) else None

    def _can_send(self) -> bool:
        if self._mode == MODE_BLE:
            return self._write_target() is not None
        return self._spp_connected

    def _refresh_send_state(self) -> None:
        target = self._write_target()
        if self._mode == MODE_BLE:
            if target is None:
                self.target_label.setText("写入目标：未选择可写特征值（在左侧服务树中选择）")
            else:
                self.target_label.setText(f"写入目标：{characteristic_label(target.uuid)}")
        else:
            self.target_label.setText(f"写入通道：{self._spp_channel or '未连接'}")
        file_active = self._file_handle is not None
        can_send = self._can_send() and not file_active
        self.send_button.setEnabled(can_send)
        self.send_file_button.setEnabled(can_send)
        self.cancel_file_button.setEnabled(file_active)

    def _refresh_status_text(self) -> None:
        if self._ble_connected:
            self.status_chip.set_state("ok", f"已连接 {self._ble_address}")
        elif self._ble_connecting:
            self.status_chip.set_state("busy", f"正在连接 {self._ble_address}…")
        elif self._spp_connected:
            self.status_chip.set_state(
                "ok", f"已连接 {self._last_device_name or self._spp_channel}"
            )
        elif self._spp_connecting:
            self.status_chip.set_state("busy", "正在连接经典蓝牙…")
        elif self._scanning:
            self.status_chip.set_state("busy", "正在扫描…")
        else:
            self.status_chip.set_state("idle", "未连接")

    def _refresh_counters(self) -> None:
        session = self.log_pane.session()
        text = (
            f"接收 {_format_bytes(session.total_bytes('rx'))} · "
            f"发送 {_format_bytes(session.total_bytes('tx'))}"
        )
        if self._ignored_notifications:
            text += f" · 忽略空通知 {self._ignored_notifications} 包"
        self.counter_label.setText(text)

    @safe_slot
    def _on_auto_connect_toggled(self, checked: bool) -> None:
        self._save_settings()
        if checked:
            self._maybe_auto_connect()

    @safe_slot
    def _maybe_auto_connect(self) -> None:
        """只在显式打开自动连接且记住过设备时补连；SPP 需要先扫到设备 ID 才能连。"""
        if not self.auto_connect_switch.isChecked() or not self._last_device:
            return
        if self._mode != MODE_BLE:
            return
        if self._ble_connected or self._ble_connecting:
            return
        self._start_ble_connect(self._last_device)

    # ---- 发送 ---------------------------------------------------------

    def _build_payload(self, text: str, hex_mode: bool, newline: str | None = None) -> bytes:
        newline = (
            newline
            if newline is not None
            else _combo_value(self.newline_combo, NEWLINE_ITEMS, "none")
        )
        payload = parse_hex(text) if hex_mode else encode_text(text, self._current_encoding())
        return payload + NEWLINE_BYTES.get(newline, b"")

    @safe_slot
    def _send_current(self) -> bool:
        if self._file_handle is not None:
            show_warning(self, "文件发送中", "请等待文件发送完成或取消后再发送其他数据。")
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
            self._stop_repeat()
            return False
        if not self._send_payload(payload):
            self._stop_repeat()
            return False
        return True

    def _send_payload(self, data: bytes) -> bool:
        if self._mode == MODE_BLE:
            char = self._write_target()
            worker = self._ble_worker
            if char is None or worker is None:
                show_warning(
                    self, "未选择写入目标", "先连接设备，并在左侧服务树里选中一个可写特征值。"
                )
                return False
            response = self.write_with_response_switch.isChecked()
            if not worker.write_char(char.uuid, data, response):
                return False
            self.log_pane.append_bytes(data, "tx", source=short_uuid(char.uuid))
            self._refresh_counters()
            return True
        worker = self._spp_worker
        if worker is None or not self._spp_connected:
            show_warning(self, "经典蓝牙未连接", "先一键连接设备，或打开蓝牙 COM 口再发送。")
            return False
        return bool(worker.send(data))

    @safe_slot
    def _send_repeat(self) -> None:
        if not self._send_current():
            self._stop_repeat()

    @safe_slot
    def _on_repeat_toggled(self, checked: bool) -> None:
        if not checked:
            self._stop_repeat()
            return
        if not self._can_send():
            # 没有写入目标时勾上也发不出去：复位勾选并说明原因，避免静默无效。
            self._stop_repeat()
            show_warning(
                self, "无法连续发送", "先连接设备并选中可写特征值，或连上经典蓝牙后再开启。"
            )
            return
        self._start_repeat()

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

    @safe_slot
    def _on_interval_changed(self, value: int) -> None:
        self._repeat_timer.setInterval(max(10, int(value)))

    @safe_slot
    def _send_text(self, text: str, hex_mode: bool, newline: bool) -> bool:
        """多项发送回调：newline 为 True 时追加 CRLF。"""
        if self._file_handle is not None:
            show_warning(self, "文件发送中", "请等待文件发送完成或取消后再发送其他数据。")
            return False
        try:
            payload = self._build_payload(text, hex_mode, "crlf" if newline else "none")
        except ValueError as exc:
            show_error(self, "HEX 格式错误", str(exc))
            return False
        if not payload:
            return False
        return self._send_payload(payload)

    @safe_slot
    def _open_multi_send(self) -> None:
        dialog = MultiSendDialog(self._send_text, self)
        dialog.exec()

    # ---- 文件发送 -----------------------------------------------------

    @safe_slot
    def _choose_file(self) -> None:
        if not self._can_send():
            show_warning(self, "尚未连接", "先连接设备并选中可写特征值，再发送文件。")
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
        self._file_queue = []
        self._update_file_label()
        self._refresh_send_state()
        self._pump_file_send()

    def _read_file_piece(self) -> bytes | None:
        """读一块文件；读失败返回 None，不能当成「文件结束」处理。"""
        if self._file_handle is None:
            return b""
        size = FILE_CHUNK_BLE if self._mode == MODE_BLE else FILE_CHUNK_SPP
        try:
            return self._file_handle.read(size)
        except OSError as exc:
            show_error(self, "读取文件失败", f"{self._file_name}\n{exc}")
            return None

    def _file_piece_chunks(self, piece: bytes) -> list[bytes]:
        if self._mode == MODE_BLE and self.mtu_chunk_switch.isChecked():
            return chunk_bytes(piece, self._mtu or DEFAULT_MTU)
        # SPP 走 RFCOMM/COM 时按 4096 B 整段写，由 worker 负责切分。
        return [piece]

    def _next_file_chunk(self) -> bytes | None:
        if self._file_queue:
            return self._file_queue.pop(0)
        piece = self._read_file_piece()
        if piece is None:
            return None
        if not piece:
            return b""
        chunks = self._file_piece_chunks(piece)
        self._file_queue = chunks[1:]
        return chunks[0]

    def _pump_file_send(self) -> None:
        """确认式推进：只有 worker 回执写完才发下一块，避免无界排队。"""
        if self._file_pumping:
            return
        self._file_pumping = True
        try:
            while self._file_handle is not None and not self._file_awaiting_ack:
                chunk = self._next_file_chunk()
                if chunk is None:
                    # 读文件失败：只报错并取消，绝不弹「发送完成」。
                    self._cancel_file_send()
                    return
                if not chunk:
                    self._finish_file_send()
                    return
                # 先置位再发送：假 worker 可能在 write_char 里同步回执。
                self._file_pending_chunk = chunk
                self._file_awaiting_ack = True
                if not self._send_payload(chunk):
                    self._cancel_file_send()
                    return
        finally:
            self._file_pumping = False

    def _handle_file_ack(self, count: int) -> None:
        """按真实写入字节推进；部分写入时把剩余部分放回队首重发。"""
        if self._file_handle is None or not self._file_awaiting_ack or count <= 0:
            return
        self._file_sent += count
        self._file_pending_chunk = self._file_pending_chunk[count:]
        self._update_file_label()
        if self._file_pending_chunk:
            self._file_queue.insert(0, self._file_pending_chunk)
            self._file_pending_chunk = b""
        self._file_awaiting_ack = False
        self._pump_file_send()

    def _finish_file_send(self) -> None:
        self._close_file_handle()
        self._file_awaiting_ack = False
        self._file_pending_chunk = b""
        self._file_queue = []
        self.file_progress_label.setText(f"文件发送完成：{self._file_name}（{self._file_sent} B）")
        self._refresh_send_state()
        show_success(self, "文件发送完成", self._file_name)

    @safe_slot
    def _cancel_file_send(self, silent: bool = False) -> None:
        was_active = self._file_handle is not None
        self._close_file_handle()
        self._file_awaiting_ack = False
        self._file_pending_chunk = b""
        self._file_queue = []
        if was_active and not silent:
            self.file_progress_label.setText(f"文件发送已取消：{self._file_name}")
        self._refresh_send_state()

    def _close_file_handle(self) -> None:
        if self._file_handle is not None:
            try:
                self._file_handle.close()
            except OSError:
                get_logger().exception("关闭发送文件句柄失败")
            finally:
                self._file_handle = None

    def _update_file_label(self) -> None:
        self.file_progress_label.setText(
            f"{self._file_name}：{self._file_sent} / {self._file_size} B"
        )

    # ---- 生命周期 -----------------------------------------------------

    def has_active_work(self) -> bool:
        return bool(
            self._ble_connected
            or self._ble_connecting
            or self._spp_connected
            or self._spp_connecting
            or (self._ble_worker is not None and self._ble_worker.is_scanning())
            or (self._spp_worker is not None and self._spp_worker.is_scanning())
            or self._repeat_timer.isActive()
            or self._file_handle is not None
        )

    def stop_workers(self) -> None:
        self._stop_repeat()
        self._cancel_file_send(silent=True)
        for worker, label in ((self._ble_worker, "BLE"), (self._spp_worker, "SPP")):
            if worker is None:
                continue
            try:
                worker.disconnect_device(SHUTDOWN_REASON)
            except Exception:
                get_logger().exception("关闭 %s 连接失败", label)
            try:
                worker.shutdown()
            except Exception:
                get_logger().exception("停止 %s 后台线程失败", label)
        self._ble_connected = False
        self._ble_connecting = False
        self._spp_connected = False
        self._spp_connecting = False
        self._scanning = False
        self._scan_remaining = 0
        self._scan_timer.stop()
        self.scan_button.setText("开始扫描")
        self.scan_remain_label.setText("")

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


def _int_or(text: str, default: int) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.2f} MB"

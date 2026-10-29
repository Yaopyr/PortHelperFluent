"""蓝牙调试页测试：假 worker + offscreen 渲染，不碰真适配器、COM 口或网络。

页面在首次扫描/连接/COM 连接时才懒建 worker，所以测试里的“预热”步骤统一表现为
点一次扫描按钮（假 worker 会立刻发 `scan_finished`，页面回到空闲），之后再手工
发信号驱动界面。假 worker 的信号名与 `BleWorker`/`SppWorker` 完全一致。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QObject, pyqtSignal

from app.core.settings import Settings
from app.io.ble_worker import BleCharInfo, BleDeviceInfo, BleServiceInfo
from app.io.serial_worker import PortInfo
from app.io.spp_worker import ClassicDeviceInfo
from app.services.paths import resolve_paths
from app.ui.pages import bluetooth_page as bluetooth_page_module
from app.ui.pages.bluetooth_page import BluetoothPage
from app.ui.qt_utils import process_events_until

ADDRESS = "AA:BB:CC:DD:EE:FF"
OTHER_ADDRESS = "11:22:33:44:55:66"
SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
READ_UUID = "00002a00-0000-1000-8000-00805f9b34fb"
SERVICES = [
    BleServiceInfo(
        SERVICE_UUID,
        (
            BleCharInfo(WRITE_UUID, ("read", "write", "notify")),
            BleCharInfo(READ_UUID, ("read",)),
        ),
    )
]
PAIRED_DEVICE = ClassicDeviceInfo("已配对设备", "11:22:33:44:55:66", True, "id-paired")
BLUETOOTH_PORTS = [
    PortInfo("COM9", "蓝牙串行端口", r"BTHENUM\{00001101-0000-1000-8000-00805F9B34FB}", None, None, None),
    PortInfo("COM4", "USB-SERIAL CH340", "USB VID:PID=1A86:7523", 0x1A86, 0x7523, "SN4"),
]


class FakeBleWorker(QObject):
    """信号名与 `BleWorker` 一致；命令只记录调用，测试按需手工回发结果信号。"""

    scan_started = pyqtSignal(int)
    scan_finished = pyqtSignal()
    device_found = pyqtSignal(object)
    connected = pyqtSignal(str, int)
    disconnected = pyqtSignal(str)
    services_ready = pyqtSignal(object)
    notification = pyqtSignal(str, bytes)
    read_result = pyqtSignal(str, bytes)
    write_finished = pyqtSignal(str, int)
    mtu_changed = pyqtSignal(int)
    rssi_changed = pyqtSignal(str, object)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.write_responses: list[bool] = []
        self.auto_finish_scan = True
        self.ack_writes = True
        self.auto_disconnect = True
        self.connected_flag = False
        self.scanning_flag = False
        self.shutdown_calls = 0

    # ---- 生命周期与命令 ----
    def start(self) -> None:
        self.calls.append(("start",))

    def start_scan(self, duration: float = 10.0) -> bool:
        self.calls.append(("start_scan", duration))
        self.scanning_flag = True
        self.scan_started.emit(int(duration))
        if self.auto_finish_scan:
            self.finish_scan()
        return True

    def finish_scan(self) -> None:
        self.scanning_flag = False
        self.scan_finished.emit()

    def stop_scan(self) -> bool:
        self.calls.append(("stop_scan",))
        self.finish_scan()
        return True

    def connect_device(self, address: str) -> bool:
        self.calls.append(("connect_device", address))
        return True

    def disconnect_device(self, reason: str = "用户断开") -> bool:
        self.calls.append(("disconnect_device", reason))
        self.connected_flag = False
        if self.auto_disconnect:
            self.disconnected.emit(reason)
        return True

    def read_char(self, char_uuid: str) -> bool:
        self.calls.append(("read", char_uuid))
        return True

    def write_char(self, char_uuid: str, data: bytes, response: bool = True) -> bool:
        self.calls.append(("write", char_uuid, bytes(data)))
        self.write_responses.append(bool(response))
        if self.ack_writes:
            self.write_finished.emit(char_uuid, len(data))
        return True

    def subscribe(self, char_uuid: str) -> bool:
        self.calls.append(("subscribe", char_uuid))
        return True

    def unsubscribe(self, char_uuid: str) -> bool:
        self.calls.append(("unsubscribe", char_uuid))
        return True

    def refresh_mtu(self) -> bool:
        self.calls.append(("refresh_mtu",))
        return True

    def is_connected(self) -> bool:
        return self.connected_flag

    def is_scanning(self) -> bool:
        return self.scanning_flag

    def shutdown(self, timeout: float = 3.0) -> None:
        self.calls.append(("shutdown",))
        self.shutdown_calls += 1
        self.finished.emit()

    # ---- 手工回发结果 ----
    def emit_connected(self, address: str = ADDRESS, mtu: int = 23, services=None) -> None:
        self.connected_flag = True
        self.services_ready.emit(list(SERVICES if services is None else services))
        self.connected.emit(address, mtu)
        self.mtu_changed.emit(mtu)

    def emit_disconnected(self, reason: str = "设备主动断开连接") -> None:
        self.connected_flag = False
        self.disconnected.emit(reason)


class FakeSppWorker(QObject):
    """信号名与 `SppWorker` 一致。"""

    scan_started = pyqtSignal(int)
    scan_finished = pyqtSignal()
    devices_found = pyqtSignal(object)
    pairing_started = pyqtSignal(str)
    pairing_prompt = pyqtSignal(str, str)
    pairing_finished = pyqtSignal(str)
    connected = pyqtSignal(str, str)
    disconnected = pyqtSignal(str)
    data_received = pyqtSignal(bytes)
    data_sent = pyqtSignal(bytes)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.devices: list[ClassicDeviceInfo] = []
        self.devices_snapshot: list[ClassicDeviceInfo] = []
        self.auto_finish_scan = True
        self.auto_disconnect = True
        self.ack_writes = True
        self.connected_flag = False
        self.scanning_flag = False
        self.channel_text = ""

    def start(self) -> None:
        self.calls.append(("start",))

    def start_scan(self, duration: float = 12.0) -> bool:
        self.calls.append(("start_scan", duration))
        self.scanning_flag = True
        self.devices_snapshot = []
        self.scan_started.emit(int(duration))
        if self.auto_finish_scan:
            self.finish_scan()
        return True

    def finish_scan(self) -> None:
        self.scanning_flag = False
        self.scan_finished.emit()

    def stop_scan(self) -> bool:
        self.calls.append(("stop_scan",))
        self.finish_scan()
        return True

    def emit_devices(self, devices) -> None:
        """`devices_found` 每次都发全量快照，页面必须整表刷新。"""
        self.devices = list(devices)
        self.devices_snapshot = list(devices)
        self.devices_found.emit(list(devices))

    def connect_device(self, device: ClassicDeviceInfo) -> bool:
        self.calls.append(("connect_device", device.address))
        return True

    def open_com(self, config) -> bool:
        self.calls.append(("open_com", config.port, config.baud))
        return True

    def answer_prompt(self, value) -> None:
        self.calls.append(("answer_prompt", value))

    def send(self, data: bytes) -> bool:
        self.calls.append(("send", bytes(data)))
        if self.ack_writes:
            self.data_sent.emit(bytes(data))
        return True

    def disconnect_device(self, reason: str = "用户断开") -> bool:
        self.calls.append(("disconnect_device", reason))
        self.connected_flag = False
        self.channel_text = ""
        if self.auto_disconnect:
            self.disconnected.emit(reason)
        return True

    def emit_connected(self, channel: str = "RFCOMM", name: str = "已配对设备") -> None:
        self.connected_flag = True
        self.channel_text = channel
        self.connected.emit(channel, name)

    def is_connected(self) -> bool:
        return self.connected_flag

    def is_scanning(self) -> bool:
        return self.scanning_flag

    def channel(self) -> str:
        return self.channel_text

    def shutdown(self, timeout: float = 3.0) -> None:
        self.calls.append(("shutdown",))
        self.finished.emit()


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    # 备用通道下拉只从串口枚举里挑蓝牙口；测试里注入固定列表，绝不枚举真机。
    monkeypatch.setattr(bluetooth_page_module, "list_ports", lambda: list(BLUETOOTH_PORTS))
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    settings = Settings(paths.config_file)
    settings.load()
    ble, spp = FakeBleWorker(), FakeSppWorker()
    widget = BluetoothPage(
        settings,
        paths,
        ble_worker_factory=lambda: ble,
        spp_worker_factory=lambda: spp,
    )
    widget.show()
    yield widget, settings, ble, spp
    _destroy(widget)


def _destroy(widget: BluetoothPage) -> None:
    """关闭并立即销毁页面，避免子控件与日志批量定时器留在整轮测试的对象堆里。"""
    widget.stop_workers()
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


def _warm_ble(widget: BluetoothPage, ble: FakeBleWorker) -> None:
    """点一次扫描按钮触发 BLE worker 懒建，然后清空调用记录。"""
    widget.scan_button.click()
    assert ble.calls, "扫描按钮应懒建 worker 并立即开始扫描"
    ble.calls.clear()


def _warm_spp(widget: BluetoothPage, spp: FakeSppWorker) -> None:
    widget.spp_scan_button.click()
    assert spp.calls
    spp.calls.clear()


def _tree_item(tree, needle: str):
    for service_index in range(tree.topLevelItemCount()):
        service = tree.topLevelItem(service_index)
        for child_index in range(service.childCount()):
            item = service.child(child_index)
            if needle in item.text(0):
                return item
    return None


def _row_address(table, row: int) -> str:
    item = table.item(row, 1)
    return item.text() if item is not None else ""


def _log_text(widget: BluetoothPage) -> str:
    widget.log_pane._flush_pending()
    return widget.log_pane.log_view.toPlainText()


def test_mode_switch_disconnects_current_connection(page):
    widget, _settings, ble, _spp = page
    _warm_ble(widget, ble)
    ble.emit_connected()
    assert widget.current_mode() == "ble"
    widget.log_pane.append_info("切换前的内容")

    widget.mode_segment.setCurrentItem("spp")

    assert widget.current_mode() == "spp"
    assert widget.left_stack.currentIndex() == 1
    disconnects = [call for call in ble.calls if call[0] == "disconnect_device"]
    assert disconnects and "切换模式" in disconnects[0][1]
    log = _log_text(widget)
    assert "已断开 BLE" in log
    assert "切换前的内容" in log


def test_send_disabled_without_writable_characteristic(page):
    widget, _settings, ble, _spp = page
    _warm_ble(widget, ble)
    ble.emit_connected()

    assert widget.send_button.isEnabled() is False
    assert "未选择" in widget.target_label.text()
    assert widget.focus_write_button.isEnabled() is False


def test_write_target_label_and_send(page):
    widget, _settings, ble, _spp = page
    _warm_ble(widget, ble)
    ble.emit_connected()

    item = _tree_item(widget.service_tree, "FFE1")
    assert item is not None
    widget.service_tree.setCurrentItem(item)
    assert "FFE1" in widget.target_label.text()
    assert widget.send_button.isEnabled() is True

    widget.hex_send_check.setChecked(True)
    widget.send_editor.setPlainText("01 02")
    widget.send_button.click()

    assert ("write", WRITE_UUID, b"\x01\x02") in ble.calls
    assert ble.write_responses == [True]
    assert widget.log_pane.session().total_bytes("tx") == 2

    # 关掉「写入需应答」后必须以无响应方式写。
    widget.write_with_response_switch.setChecked(False)
    widget.send_button.click()
    assert ble.write_responses[-1] is False


def test_notification_goes_to_log_and_ignores_empty(page):
    widget, _settings, ble, _spp = page
    _warm_ble(widget, ble)
    ble.emit_connected()
    assert widget.ignore_empty_switch.isChecked() is True

    ble.notification.emit(WRITE_UUID, b"")
    assert "忽略空通知 1" in widget.counter_label.text()
    assert widget.log_pane.session().total_bytes("rx") == 0

    ble.notification.emit(WRITE_UUID, b"\x01\x02")
    assert process_events_until(
        lambda: "FFE1" in widget.log_pane.log_view.toPlainText(), timeout_ms=2000
    )
    assert widget.log_pane.session().total_bytes("rx") == 2


def test_settings_persist_and_restore(qapp, tmp_path, monkeypatch, page):
    widget, settings, ble, _spp = page
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")

    _warm_ble(widget, ble)
    ble.device_found.emit(BleDeviceInfo("设备甲", ADDRESS, -60, 1.0))
    widget._toggle_favorite(ADDRESS)
    monkeypatch.setattr(
        bluetooth_page_module.QInputDialog,
        "getText",
        staticmethod(lambda *args, **kwargs: ("别名设备", True)),
    )
    widget._rename_device(ADDRESS)

    widget.mode_segment.setCurrentItem("spp")
    widget.baud_combo.setCurrentText("9600")
    widget.com_combo.setCurrentText("COM9")
    widget.close()

    assert settings.get("bluetooth.mode") == "spp"
    assert settings.get("bluetooth.spp_baud") == 9600
    assert settings.get("bluetooth.spp_com") == "COM9"
    assert ADDRESS in settings.get("bluetooth.favorites")
    assert settings.get("bluetooth.aliases").get(ADDRESS) == "别名设备"

    reloaded = Settings(paths.config_file)
    reloaded.load()
    assert reloaded.get("bluetooth.mode") == "spp"
    assert reloaded.get("bluetooth.spp_baud") == 9600

    second = BluetoothPage(
        reloaded, paths, ble_worker_factory=FakeBleWorker, spp_worker_factory=FakeSppWorker
    )
    second.show()
    try:
        assert second.current_mode() == "spp"
        assert second.mode_segment.currentRouteKey() == "spp"
        assert second.left_stack.currentIndex() == 1
        assert second.baud_combo.currentText() == "9600"
        # 备用通道只列蓝牙串口；上次选择仍被恢复。
        assert second.com_combo.findText("COM4") < 0
        assert second.com_combo.currentText() == "COM9"
        assert second.hex_send_check.isChecked() is False
    finally:
        _destroy(second)


def test_stop_workers_shuts_down_active_worker(page):
    widget, _settings, ble, _spp = page
    _warm_ble(widget, ble)
    ble.emit_connected()

    widget.stop_workers()

    names = [call[0] for call in ble.calls]
    assert "disconnect_device" in names
    assert "shutdown" in names
    assert [call[1] for call in ble.calls if call[0] == "disconnect_device"] == ["程序退出"]
    assert widget.has_active_work() is False


def test_has_active_work_tracks_connection_and_scan(page):
    widget, _settings, ble, _spp = page
    # 构造页面不发任何蓝牙调用（反目标：打开页面不自动扫描/连接）。
    assert ble.calls == []
    assert widget.has_active_work() is False

    ble.auto_finish_scan = False
    widget.scan_button.click()
    assert widget.has_active_work() is True

    ble.finish_scan()
    assert widget.scan_button.text() == "开始扫描"
    assert widget.has_active_work() is False

    ble.emit_connected()
    assert widget.has_active_work() is True

    ble.emit_disconnected()
    assert widget.has_active_work() is False


def test_bluetooth_error_shows_reason_and_settings_button(page):
    widget, _settings, ble, _spp = page
    _warm_ble(widget, ble)
    assert widget.open_bt_settings_button.isVisible() is False

    ble.error.emit("蓝牙已关闭，请在设置中打开")

    assert "蓝牙已关闭" in widget.status_chip.text()
    assert widget.status_chip.state == "error"
    assert widget.open_bt_settings_button.isVisible() is True
    assert process_events_until(
        lambda: "蓝牙已关闭" in widget.log_pane.log_view.toPlainText(), timeout_ms=2000
    )


def test_alias_overrides_name_and_favorites_sort_first(page, monkeypatch):
    widget, settings, ble, _spp = page
    _warm_ble(widget, ble)
    ble.device_found.emit(BleDeviceInfo("设备甲", ADDRESS, -80, 1.0))
    ble.device_found.emit(BleDeviceInfo("设备乙", OTHER_ADDRESS, -40, 2.0))

    assert widget.device_table.rowCount() == 2
    # 默认按信号强度排序：-40 dBm 的乙在前。
    assert _row_address(widget.device_table, 0) == OTHER_ADDRESS

    widget._toggle_favorite(ADDRESS)
    assert _row_address(widget.device_table, 0) == ADDRESS
    assert settings.get("bluetooth.favorites") == [ADDRESS]

    monkeypatch.setattr(
        bluetooth_page_module.QInputDialog,
        "getText",
        staticmethod(lambda *args, **kwargs: ("甲别名", True)),
    )
    widget._rename_device(ADDRESS)
    assert "甲别名" in widget.device_table.item(0, 0).text()
    assert settings.get("bluetooth.aliases").get(ADDRESS) == "甲别名"


def test_file_send_chunks_by_mtu(page, tmp_path, monkeypatch):
    widget, _settings, ble, _spp = page
    _warm_ble(widget, ble)
    ble.emit_connected(mtu=23)
    widget.service_tree.setCurrentItem(_tree_item(widget.service_tree, "FFE1"))

    path = tmp_path / "payload.bin"
    payload = bytes(range(60))
    path.write_bytes(payload)
    monkeypatch.setattr(
        bluetooth_page_module.QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(path), "")),
    )

    widget.send_file_button.click()
    writes = [call for call in ble.calls if call[0] == "write"]
    assert [len(data) for _name, _uuid, data in writes] == [20, 20, 20]
    assert b"".join(data for _name, _uuid, data in writes) == payload
    assert widget._file_handle is None

    # 关掉「按 MTU 分片」后整段文件一次写完。
    ble.calls.clear()
    widget.mtu_chunk_switch.setChecked(False)
    widget.send_file_button.click()
    writes = [call for call in ble.calls if call[0] == "write"]
    assert [len(data) for _name, _uuid, data in writes] == [60]


def test_disconnect_reason_updates_status_and_buttons(page):
    widget, _settings, ble, _spp = page
    _warm_ble(widget, ble)
    ble.emit_connected(mtu=100)
    assert "100" in widget.mtu_label.text()
    assert widget.connect_button.text() == "断开"
    widget.service_tree.setCurrentItem(_tree_item(widget.service_tree, "FFE1"))
    assert widget.send_button.isEnabled() is True

    ble.emit_disconnected("设备主动断开连接")

    assert "设备主动断开连接" in widget.status_chip.text()
    assert widget.status_chip.state == "idle"
    assert widget.send_button.isEnabled() is False
    assert widget.connect_button.text() == "连接"
    assert "未知" in widget.mtu_label.text()
    assert process_events_until(
        lambda: "设备主动断开连接" in widget.log_pane.log_view.toPlainText(), timeout_ms=2000
    )


def test_spp_one_click_connect_and_com_fallback(page):
    widget, settings, _ble, spp = page
    _warm_spp(widget, spp)
    spp.emit_devices([PAIRED_DEVICE])
    widget.spp_table.selectRow(0)

    widget.spp_connect_button.click()
    assert ("connect_device", PAIRED_DEVICE.address) in spp.calls

    spp.pairing_started.emit(PAIRED_DEVICE.name)
    assert "配对中" in widget.pair_stage_label.text()
    spp.pairing_finished.emit("配对成功")
    spp.emit_connected("RFCOMM", PAIRED_DEVICE.name)
    assert "RFCOMM" in widget.spp_info_label.text()

    widget.spp_connect_button.click()
    assert "disconnect_device" in [call[0] for call in spp.calls]

    # 备用通道：直接打开系统蓝牙 COM 口。
    assert widget.com_combo.findText("COM9") >= 0
    widget.com_combo.setCurrentText("COM9")
    widget.baud_combo.setCurrentText("9600")
    widget.com_connect_button.click()
    assert ("open_com", "COM9", 9600) in spp.calls
    assert settings.get("bluetooth.spp_com") == "COM9"

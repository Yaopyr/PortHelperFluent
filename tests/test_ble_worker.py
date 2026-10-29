"""BLE worker tests: fake backend, real daemon asyncio thread, no hardware or network.

Signals are connected with an explicit direct connection so the handler runs inside the
emitting thread; every wait uses `threading.Event`, never the Qt event loop.
"""

from __future__ import annotations

import asyncio
import threading
import time

from PyQt6.QtCore import Qt

from app.io.ble_worker import BleCharInfo, BleDeviceInfo, BleServiceInfo, BleWorker

DIRECT = Qt.ConnectionType.DirectConnection
DEVICE = BleDeviceInfo("测试设备", "AA:BB:CC:DD:EE:FF", -55, 0.0)
SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"


async def _noop() -> None:
    return None


class FakeBleBackend:
    """按脚本应答的假后端：记录调用、可指定在某一步抛异常。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.devices = [DEVICE]
        self.service_list = [
            BleServiceInfo(SERVICE_UUID, (BleCharInfo(CHAR_UUID, ("read", "write", "notify")),))
        ]
        self.mtu_value = 23
        self.read_value = b"\x01\x02"
        self.connect_delay = 0.0
        self.fail_on: str | None = None
        self.disconnect_cb = None
        self.notify_cb = None
        self.notify_ready = threading.Event()

    def set_disconnect_callback(self, callback) -> None:
        self.disconnect_cb = callback

    async def scan(self, duration, on_device, stop) -> None:
        self.calls.append(("scan", duration))
        if self.fail_on == "scan":
            raise RuntimeError("蓝牙关闭")
        for info in self.devices:
            on_device(info)
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline and not stop():
            await asyncio.sleep(0.02)

    async def connect(self, address) -> None:
        self.calls.append(("connect", address))
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.fail_on == "connect":
            raise RuntimeError("连接失败")

    async def disconnect(self) -> None:
        self.calls.append(("disconnect",))

    async def services(self) -> list[BleServiceInfo]:
        self.calls.append(("services",))
        if self.fail_on == "services":
            raise RuntimeError("服务发现失败")
        return list(self.service_list)

    def mtu(self) -> int:
        return self.mtu_value

    async def read(self, char_uuid) -> bytes:
        self.calls.append(("read", char_uuid))
        if self.fail_on == "read":
            raise RuntimeError("读取失败")
        return self.read_value

    async def write(self, char_uuid, data, response) -> None:
        self.calls.append(("write", char_uuid, bytes(data), response))
        if self.fail_on == "write":
            raise RuntimeError("写入失败")

    async def set_notify(self, char_uuid, enabled, on_data) -> None:
        self.calls.append(("notify", char_uuid, enabled))
        if self.fail_on == "notify":
            raise RuntimeError("订阅失败")
        self.notify_cb = on_data if enabled else None
        self.notify_ready.set()


def test_scan_emits_devices_and_finishes(qapp):
    backend = FakeBleBackend()
    worker = BleWorker(backend)
    started: list[int] = []
    found: list[BleDeviceInfo] = []
    rssi: list[tuple[str, object]] = []
    done = threading.Event()
    worker.scan_started.connect(started.append, type=DIRECT)
    worker.device_found.connect(found.append, type=DIRECT)
    worker.rssi_changed.connect(lambda address, value: rssi.append((address, value)), type=DIRECT)
    worker.scan_finished.connect(done.set, type=DIRECT)
    worker.start()
    try:
        assert worker.thread_alive()
        assert worker.start_scan(1) is True
        assert started == [1]
        assert worker.is_scanning() is True
        assert done.wait(3)
        assert [device.address for device in found] == ["AA:BB:CC:DD:EE:FF"]
        assert rssi == [("AA:BB:CC:DD:EE:FF", -55)]
        assert ("scan", 1) in backend.calls
        assert worker.is_scanning() is False
    finally:
        worker.shutdown()


def test_connect_emits_connected_mtu_and_services(qapp):
    backend = FakeBleBackend()
    worker = BleWorker(backend)
    order: list[str] = []
    services: list[list[BleServiceInfo]] = []
    connected: list[tuple[str, int]] = []
    mtus: list[int] = []
    done = threading.Event()
    mtu_done = threading.Event()

    def on_services(value):
        services.append(value)
        order.append("services")

    def on_connected(address, mtu):
        connected.append((address, mtu))
        order.append("connected")
        done.set()

    worker.services_ready.connect(on_services, type=DIRECT)
    worker.connected.connect(on_connected, type=DIRECT)
    worker.mtu_changed.connect(
        lambda value: (mtus.append(value), mtu_done.set()), type=DIRECT
    )
    worker.start()
    try:
        assert worker.connect_device("AA:BB:CC:DD:EE:FF") is True
        assert done.wait(3)
        # mtu_changed 在 connected 之后发，必须单独等它再断言。
        assert mtu_done.wait(3)
        assert connected == [("AA:BB:CC:DD:EE:FF", 23)]
        assert services == [backend.service_list]
        assert order[:2] == ["services", "connected"]
        assert mtus == [23]
        assert worker.is_connected() is True
        assert ("connect", "AA:BB:CC:DD:EE:FF") in backend.calls
        assert ("services",) in backend.calls
    finally:
        worker.shutdown()


def test_read_write_and_notify_roundtrip(qapp):
    backend = FakeBleBackend()
    worker = BleWorker(backend)
    reads: list[tuple[str, bytes]] = []
    writes: list[tuple[str, int]] = []
    notifications: list[tuple[str, bytes]] = []
    read_done = threading.Event()
    write_done = threading.Event()
    worker.read_result.connect(
        lambda uuid, data: (reads.append((uuid, data)), read_done.set()), type=DIRECT
    )
    worker.write_finished.connect(
        lambda uuid, size: (writes.append((uuid, size)), write_done.set()), type=DIRECT
    )
    worker.notification.connect(
        lambda uuid, data: (notifications.append((uuid, data))), type=DIRECT
    )
    worker.start()
    try:
        assert worker.read_char(CHAR_UUID) is True
        assert read_done.wait(3)
        assert reads == [(CHAR_UUID, b"\x01\x02")]

        assert worker.write_char(CHAR_UUID, b"AB", response=False) is True
        assert write_done.wait(3)
        assert writes == [(CHAR_UUID, 2)]
        assert ("write", CHAR_UUID, b"AB", False) in backend.calls

        assert worker.subscribe(CHAR_UUID) is True
        assert backend.notify_ready.wait(3)
        backend.notify_cb(CHAR_UUID, b"hello")
        assert notifications == [(CHAR_UUID, b"hello")]

        backend.notify_ready.clear()
        assert worker.unsubscribe(CHAR_UUID) is True
        assert backend.notify_ready.wait(3)
        assert backend.notify_cb is None
        assert ("notify", CHAR_UUID, False) in backend.calls
    finally:
        worker.shutdown()


def test_backend_disconnect_callback_emits_reason(qapp):
    backend = FakeBleBackend()
    worker = BleWorker(backend)
    connected = threading.Event()
    reasons: list[str] = []
    worker.connected.connect(lambda address, mtu: connected.set(), type=DIRECT)
    worker.disconnected.connect(reasons.append, type=DIRECT)
    worker.start()
    try:
        assert backend.disconnect_cb is not None
        assert worker.connect_device("AA:BB:CC:DD:EE:FF") is True
        assert connected.wait(3)
        assert worker.is_connected() is True

        backend.disconnect_cb("设备主动断开连接")
        assert reasons == ["设备主动断开连接"]
        assert worker.is_connected() is False
    finally:
        worker.shutdown()


def test_backend_exception_emits_error_and_disconnect_reason(qapp):
    backend = FakeBleBackend()
    backend.fail_on = "connect"
    worker = BleWorker(backend)
    errors: list[str] = []
    reasons: list[str] = []
    error_done = threading.Event()
    worker.error.connect(lambda text: (errors.append(text), error_done.set()), type=DIRECT)
    worker.disconnected.connect(reasons.append, type=DIRECT)
    worker.start()
    try:
        assert worker.connect_device("AA:BB:CC:DD:EE:FF") is True
        assert error_done.wait(3)
        assert errors and "连接失败" in errors[0]
        assert "确认设备未被其他程序占用" in errors[0]
        assert worker.is_connected() is False

        backend.disconnect_cb("设备主动断开连接")
        assert reasons == ["设备主动断开连接"]
    finally:
        worker.shutdown()


def test_shutdown_is_idempotent_and_joins_thread(qapp):
    backend = FakeBleBackend()
    worker = BleWorker(backend)
    finished: list[bool] = []
    worker.finished.connect(lambda: finished.append(True), type=DIRECT)
    worker.start()
    assert worker.thread_alive()
    assert worker.start_scan(10) is True

    worker.shutdown()
    worker.shutdown()

    assert not worker.thread_alive()
    assert finished == [True]


def test_submit_after_shutdown_returns_false(qapp):
    backend = FakeBleBackend()
    worker = BleWorker(backend)
    errors: list[str] = []
    worker.error.connect(errors.append, type=DIRECT)
    worker.start()
    worker.shutdown()

    assert worker.submit(lambda: _noop()) is False
    assert worker.start_scan() is False
    assert worker.connect_device("AA:BB:CC:DD:EE:FF") is False
    assert errors and "蓝牙后台线程未运行" in errors[-1]
    assert worker.is_scanning() is False
    assert not worker.thread_alive()


def test_refresh_mtu_emits_new_value(qapp):
    backend = FakeBleBackend()
    worker = BleWorker(backend)
    mtus: list[int] = []
    mtus_done = threading.Event()
    worker.mtu_changed.connect(lambda value: (mtus.append(value), mtus_done.set()), type=DIRECT)
    worker.start()
    try:
        backend.mtu_value = 247
        assert worker.refresh_mtu() is True
        assert mtus_done.wait(3)
        assert mtus == [247]
    finally:
        worker.shutdown()


def test_second_scan_while_scanning_is_rejected(qapp):
    backend = FakeBleBackend()
    worker = BleWorker(backend)
    started: list[int] = []
    done = threading.Event()
    worker.scan_started.connect(started.append, type=DIRECT)
    worker.scan_finished.connect(done.set, type=DIRECT)
    worker.start()
    try:
        assert worker.start_scan(1) is True
        assert worker.start_scan(1) is False
        assert started == [1]
        assert done.wait(3)
        assert [call for call in backend.calls if call[0] == "scan"] == [("scan", 1)]
        assert worker.is_scanning() is False
    finally:
        worker.shutdown()


def test_second_connect_while_connecting_is_rejected(qapp):
    backend = FakeBleBackend()
    backend.connect_delay = 0.3
    worker = BleWorker(backend)
    connected: list[tuple[str, int]] = []
    done = threading.Event()
    worker.connected.connect(
        lambda address, mtu: (connected.append((address, mtu)), done.set()), type=DIRECT
    )
    worker.start()
    try:
        assert worker.connect_device("AA:BB:CC:DD:EE:FF") is True
        assert worker.connect_device("AA:BB:CC:DD:EE:FF") is False
        assert done.wait(3)
        assert connected == [("AA:BB:CC:DD:EE:FF", 23)]
        assert [call for call in backend.calls if call[0] == "connect"] == [
            ("connect", "AA:BB:CC:DD:EE:FF")
        ]
        assert worker.connect_device("AA:BB:CC:DD:EE:FF") is False
    finally:
        worker.shutdown()


def test_scan_failure_resets_state_and_emits_finished(qapp):
    backend = FakeBleBackend()
    backend.fail_on = "scan"
    worker = BleWorker(backend)
    errors: list[str] = []
    finished: list[bool] = []
    error_done = threading.Event()
    worker.error.connect(lambda text: (errors.append(text), error_done.set()), type=DIRECT)
    worker.scan_finished.connect(lambda: finished.append(True), type=DIRECT)
    worker.start()
    try:
        assert worker.start_scan(1) is True
        # scan_finished 在协程的 finally 里先发，error 随后由 _guard 发，所以等 error。
        assert error_done.wait(3)
        assert errors and "蓝牙关闭" in errors[0]
        assert "广播范围" in errors[0]
        assert finished == [True]
        assert worker.is_scanning() is False

        backend.fail_on = None
        assert worker.start_scan(1) is True
        assert worker.stop_scan() is True
    finally:
        worker.shutdown()


def test_read_error_uses_read_hint(qapp):
    backend = FakeBleBackend()
    backend.fail_on = "read"
    worker = BleWorker(backend)
    errors: list[str] = []
    error_done = threading.Event()
    worker.error.connect(lambda text: (errors.append(text), error_done.set()), type=DIRECT)
    worker.start()
    try:
        assert worker.read_char(CHAR_UUID) is True
        assert error_done.wait(3)
        assert errors and "读取失败" in errors[0]
        assert "确认该特征值支持读取" in errors[0]
    finally:
        worker.shutdown()

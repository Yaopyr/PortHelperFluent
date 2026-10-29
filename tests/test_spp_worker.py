"""Classic Bluetooth SPP worker tests: fake backend + fake serial, real daemon thread.

Signals are connected with an explicit direct connection so the handler runs inside the
emitting thread; every wait uses `threading.Event`, never the Qt event loop. No test
touches WinRT, a real radio or a real COM port.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from PyQt6.QtCore import QObject, Qt, pyqtSignal

import app.io.spp_worker as spp_worker
from app.io.serial_worker import SerialConfig
from app.io.spp_worker import (
    ClassicDeviceInfo,
    SppWorker,
    WinRtRfcommLocator,
    WinRtSppBackend,
)

DIRECT = Qt.ConnectionType.DirectConnection
PAIRED_DEVICE = ClassicDeviceInfo("已配对设备", "11:22:33:44:55:66", True, "id-paired")
UNPAIRED_DEVICE = ClassicDeviceInfo("待配对设备", "AA:BB:CC:DD:EE:FF", False, "id-unpaired")


class FakeSppBackend:
    """按脚本应答的假后端：记录调用、可指定在某一步抛异常或阻塞。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.devices = [PAIRED_DEVICE, UNPAIRED_DEVICE]
        self.pair_status = "PAIRED"
        self.prompt_enabled = False
        self.prompt_kind = "pin"
        self.prompt_answer: str | None = None
        self.connect_delay = 0.0
        self.pair_delay = 0.0
        self.fail_on: str | None = None
        self.closed = False
        self.closed_event = threading.Event()
        self.sent: list[bytes] = []
        self.data_cb = None
        self.prompt_cb = None
        self.disconnect_cb = None

    def set_data_callback(self, callback) -> None:
        self.data_cb = callback

    def set_prompt_callback(self, callback) -> None:
        self.prompt_cb = callback

    def set_disconnect_callback(self, callback) -> None:
        self.disconnect_cb = callback

    def deliver(self, data: bytes) -> None:
        """模拟 RFCOMM 读到数据，在调用线程里直接回调 worker。"""
        assert self.data_cb is not None
        self.data_cb(bytes(data))

    def report_peer_disconnect(self) -> None:
        """模拟读取循环因对端关闭而退出，在调用线程里直接回调 worker。"""
        assert self.disconnect_cb is not None
        self.disconnect_cb("设备主动断开连接")

    async def scan(self, duration, on_device, stop) -> None:
        self.calls.append(("scan", duration))
        if self.fail_on == "scan":
            raise RuntimeError("蓝牙适配器不可用")
        for info in self.devices:
            on_device(info)
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline and not stop():
            await asyncio.sleep(0.02)

    async def pair(self, device) -> str:
        self.calls.append(("pair", device.address))
        if self.fail_on == "pair":
            raise RuntimeError("配对时蓝牙已关闭")
        if self.pair_delay:
            await asyncio.sleep(self.pair_delay)
        if self.prompt_enabled:
            # 真实实现里 WinRT 的配对回调就是这样同步阻塞等用户输入。
            self.prompt_answer = self.prompt_cb(self.prompt_kind, "请输入配对码")
            if self.prompt_answer is None:
                return "REJECTED_BY_HANDLER"
        return self.pair_status

    async def connect(self, device) -> None:
        self.calls.append(("connect", device.address))
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.fail_on == "connect":
            raise RuntimeError("RFCOMM 连接失败")

    async def send(self, data: bytes) -> int:
        self.calls.append(("send", bytes(data)))
        if self.fail_on == "send":
            raise RuntimeError("RFCOMM 写入失败")
        self.sent.append(bytes(data))
        return len(data)

    async def close(self) -> None:
        self.calls.append(("close",))
        self.closed = True
        self.closed_event.set()


class FakeSerialWorker(QObject):
    """假串口：信号名与 `SerialWorker` 一致，`open_port` 立刻回 `opened`。"""

    opened = pyqtSignal()
    closed = pyqtSignal()
    data_received = pyqtSignal(bytes)
    data_sent = pyqtSignal(bytes)
    error = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.configs: list[SerialConfig] = []
        self.sent: list[bytes] = []
        self.close_calls = 0
        self._open = False

    def open_port(self, config: SerialConfig) -> bool:
        self.configs.append(config)
        self._open = True
        self.opened.emit()
        return True

    def close_port(self) -> None:
        self.close_calls += 1
        self._open = False
        self.closed.emit()

    def send(self, data: bytes) -> bool:
        if not self._open:
            self.error.emit("串口未打开，无法发送数据")
            return False
        self.sent.append(bytes(data))
        self.data_sent.emit(bytes(data))
        return True

    def is_open(self) -> bool:
        return self._open


def test_rfcomm_locator_prefers_device_id_service():
    service = object()
    calls: list[tuple] = []

    async def from_device_id(device_id: str):
        calls.append(("from_device_id", device_id))
        return service

    async def from_address(address_int: int):
        calls.append(("from_address", address_int))
        return object()

    locator = WinRtRfcommLocator(from_device_id=from_device_id, from_address=from_address)
    assert asyncio.run(locator.locate(PAIRED_DEVICE)) is service
    # 设备 ID 直查命中就不要再去按地址找服务。
    assert calls == [("from_device_id", "id-paired")]


def test_rfcomm_locator_falls_back_to_serial_port_service_by_address():
    service = object()
    calls: list[tuple] = []

    async def from_device_id(device_id: str):
        calls.append(("from_device_id", device_id))
        # 本机实测：设备级 AEP ID 走 from_id_async 会抛 E_FAIL。
        raise OSError(0x80004005, "E_FAIL")

    async def from_address(address_int: int):
        calls.append(("from_address", address_int))
        return service

    locator = WinRtRfcommLocator(from_device_id=from_device_id, from_address=from_address)
    assert asyncio.run(locator.locate(PAIRED_DEVICE)) is service
    assert calls == [("from_device_id", "id-paired"), ("from_address", 0x112233445566)]


def test_rfcomm_locator_raises_when_no_service_found():
    calls: list[tuple] = []

    async def from_device_id(device_id: str):
        calls.append(("from_device_id", device_id))
        return None

    async def from_address(address_int: int):
        calls.append(("from_address", address_int))
        return None

    locator = WinRtRfcommLocator(from_device_id=from_device_id, from_address=from_address)
    with pytest.raises(RuntimeError, match="SPP 串口服务"):
        asyncio.run(locator.locate(PAIRED_DEVICE))
    assert [call[0] for call in calls] == ["from_device_id", "from_address"]


class FakeDataReader:
    """`DataReader` 替身：先吐给定字节数，之后返回 0（对端关闭）或抛脚本异常。"""

    def __init__(self, sizes: tuple[int, ...] = (), *, error: Exception | None = None) -> None:
        self._sizes = list(sizes)
        self._error = error

    async def load_async(self, _size: int) -> int:
        if self._sizes:
            return self._sizes.pop(0)
        if self._error is not None:
            raise self._error
        return 0

    def read_buffer(self, size: int) -> bytes:
        return b"x" * size


@pytest.mark.parametrize(
    "reader",
    [FakeDataReader((2,)), FakeDataReader(error=OSError("COMError 0x8007048F"))],
    ids=["zero-bytes", "com-error"],
)
def test_read_loop_reports_peer_disconnect_once(reader):
    backend = WinRtSppBackend()
    reasons: list[str] = []
    backend.set_disconnect_callback(reasons.append)
    asyncio.run(backend._read_loop(reader))
    # 同一条链路只报一次：再跑一遍也还是那一条（不能和自己的 close() 叠加成两条）。
    asyncio.run(backend._read_loop(FakeDataReader()))
    assert reasons == ["设备主动断开连接"]


def test_scan_emits_paired_and_unpaired_devices(qapp):
    backend = FakeSppBackend()
    worker = SppWorker(backend)
    started: list[int] = []
    batches: list[list[ClassicDeviceInfo]] = []
    done = threading.Event()
    worker.scan_started.connect(started.append, type=DIRECT)
    worker.devices_found.connect(batches.append, type=DIRECT)
    worker.scan_finished.connect(done.set, type=DIRECT)
    worker.start()
    try:
        assert worker.start_scan(1) is True
        assert started == [1]
        assert done.wait(3)
        assert [call for call in backend.calls if call[0] == "scan"] == [("scan", 1)]
        assert [(d.name, d.address, d.paired) for d in batches[-1]] == [
            ("已配对设备", "11:22:33:44:55:66", True),
            ("待配对设备", "AA:BB:CC:DD:EE:FF", False),
        ]
        assert batches[-1][1].device_id == "id-unpaired"
    finally:
        worker.shutdown()


def test_one_click_pairs_then_connects_rfcomm(qapp):
    backend = FakeSppBackend()
    worker = SppWorker(backend)
    connected: list[tuple[str, str]] = []
    pairing_started: list[str] = []
    pairing_finished: list[str] = []
    done = threading.Event()
    worker.connected.connect(
        lambda channel, name: (connected.append((channel, name)), done.set()), type=DIRECT
    )
    worker.pairing_started.connect(pairing_started.append, type=DIRECT)
    worker.pairing_finished.connect(pairing_finished.append, type=DIRECT)
    worker.start()
    try:
        assert worker.connect_device(UNPAIRED_DEVICE) is True
        assert done.wait(3)
        assert [call[0] for call in backend.calls] == ["pair", "connect"]
        assert connected == [("RFCOMM", "待配对设备")]
        assert pairing_started == ["待配对设备"]
        assert pairing_finished == ["配对成功"]
        assert worker.is_connected() is True
        assert worker.channel() == "RFCOMM"
    finally:
        worker.shutdown()


def test_already_paired_skips_pairing(qapp):
    backend = FakeSppBackend()
    worker = SppWorker(backend)
    connected: list[tuple[str, str]] = []
    done = threading.Event()
    worker.connected.connect(
        lambda channel, name: (connected.append((channel, name)), done.set()), type=DIRECT
    )
    worker.start()
    try:
        assert worker.connect_device(PAIRED_DEVICE) is True
        assert done.wait(3)
        assert "pair" not in [call[0] for call in backend.calls]
        assert connected == [("RFCOMM", "已配对设备")]
        assert backend.prompt_answer is None
    finally:
        worker.shutdown()


def test_pairing_prompt_pin_resumes_pairing(qapp):
    backend = FakeSppBackend()
    backend.prompt_enabled = True
    worker = SppWorker(backend)
    prompts: list[tuple[str, str]] = []
    connected = threading.Event()
    worker.pairing_prompt.connect(
        lambda kind, text: (prompts.append((kind, text)), worker.answer_prompt("4321")),
        type=DIRECT,
    )
    worker.connected.connect(lambda channel, name: connected.set(), type=DIRECT)
    worker.start()
    try:
        assert worker.connect_device(UNPAIRED_DEVICE) is True
        assert connected.wait(3)
        assert prompts[0][0] == "pin"
        assert prompts[0][1]
        assert backend.prompt_answer == "4321"
    finally:
        worker.shutdown()


def test_pair_prompt_timeout_returns_none(monkeypatch, qapp):
    monkeypatch.setattr(SppWorker, "PROMPT_TIMEOUT", 0.2)
    backend = FakeSppBackend()
    backend.prompt_enabled = True
    worker = SppWorker(backend)
    prompts: list[tuple[str, str]] = []
    pairing_finished: list[str] = []
    errors: list[str] = []
    error_done = threading.Event()
    worker.pairing_prompt.connect(lambda kind, text: prompts.append((kind, text)), type=DIRECT)
    worker.pairing_finished.connect(pairing_finished.append, type=DIRECT)
    worker.error.connect(lambda text: (errors.append(text), error_done.set()), type=DIRECT)
    worker.start()
    try:
        assert worker.connect_device(UNPAIRED_DEVICE) is True
        assert error_done.wait(3)
        # 无人应答 → 超时返回 None，按拒绝处理，不进入连接。
        assert prompts and prompts[0][0] == "pin"
        assert backend.prompt_answer is None
        assert "配对失败" in pairing_finished[-1]
        assert [call[0] for call in backend.calls] == ["pair"]
        assert worker.is_connected() is False
    finally:
        worker.shutdown()


def test_pair_failure_maps_chinese_status(qapp):
    backend = FakeSppBackend()
    backend.pair_status = "AUTHENTICATION_FAILURE"
    worker = SppWorker(backend)
    pairing_finished: list[str] = []
    errors: list[str] = []
    error_done = threading.Event()
    worker.pairing_finished.connect(pairing_finished.append, type=DIRECT)
    worker.error.connect(lambda text: (errors.append(text), error_done.set()), type=DIRECT)
    worker.start()
    try:
        assert worker.connect_device(UNPAIRED_DEVICE) is True
        assert error_done.wait(3)
        assert "PIN" in pairing_finished[-1]
        assert "连接" not in [call[0] for call in backend.calls]
        assert "配对模式" in errors[-1]
        assert worker.is_connected() is False
        assert worker.channel() == ""
    finally:
        worker.shutdown()


def test_rfcomm_data_received_and_sent(qapp):
    backend = FakeSppBackend()
    worker = SppWorker(backend)
    received: list[bytes] = []
    sent: list[bytes] = []
    connected = threading.Event()
    sent_done = threading.Event()
    worker.connected.connect(lambda channel, name: connected.set(), type=DIRECT)
    worker.data_received.connect(received.append, type=DIRECT)
    worker.data_sent.connect(lambda data: (sent.append(data), sent_done.set()), type=DIRECT)
    worker.start()
    try:
        assert worker.connect_device(PAIRED_DEVICE) is True
        assert connected.wait(3)
        backend.deliver(b"hello")
        assert received == [b"hello"]

        assert worker.send(b"ping") is True
        assert sent_done.wait(3)
        assert sent == [b"ping"]
        assert backend.sent == [b"ping"]
    finally:
        worker.shutdown()


def test_com_channel_bridges_serial_worker_signals(qapp):
    serial = FakeSerialWorker()
    worker = SppWorker(FakeSppBackend(), serial_factory=lambda: serial)
    connected: list[tuple[str, str]] = []
    received: list[bytes] = []
    sent: list[bytes] = []
    errors: list[str] = []
    worker.connected.connect(
        lambda channel, name: connected.append((channel, name)), type=DIRECT
    )
    worker.data_received.connect(received.append, type=DIRECT)
    worker.data_sent.connect(sent.append, type=DIRECT)
    worker.error.connect(errors.append, type=DIRECT)
    worker.start()
    try:
        assert worker.open_com(SerialConfig("COM7", 115200)) is True
        assert connected == [("COM7", "COM7")]
        assert serial.configs == [SerialConfig("COM7", 115200)]
        assert worker.channel() == "COM7"
        assert worker.is_connected() is True

        serial.data_received.emit(b"abc")
        assert received == [b"abc"]

        assert worker.send(b"tx") is True
        assert serial.sent == [b"tx"]
        assert sent == [b"tx"]

        serial.error.emit("串口读取失败：设备被拔出")
        assert errors == ["串口读取失败：设备被拔出"]
    finally:
        worker.shutdown()


def test_disconnect_reason_emitted_once(qapp):
    backend = FakeSppBackend()
    worker = SppWorker(backend)
    reasons: list[str] = []
    connected = threading.Event()
    reason_done = threading.Event()
    worker.connected.connect(lambda channel, name: connected.set(), type=DIRECT)
    worker.disconnected.connect(
        lambda reason: (reasons.append(reason), reason_done.set()), type=DIRECT
    )
    worker.start()
    try:
        assert worker.connect_device(PAIRED_DEVICE) is True
        assert connected.wait(3)
        assert worker.disconnect_device("测试断开") is True
        assert reason_done.wait(3)
        # 幂等：再点两次既不重复发 disconnected，也不报错。
        assert worker.disconnect_device("再次断开") is True
        assert worker.disconnect_device() is True
        assert reasons == ["测试断开"]
        assert backend.closed is True
        assert worker.is_connected() is False
        assert worker.channel() == ""
    finally:
        worker.shutdown()


def test_peer_disconnect_converges_to_single_reason(qapp):
    backend = FakeSppBackend()
    worker = SppWorker(backend)
    reasons: list[str] = []
    connected = threading.Event()
    reason_done = threading.Event()
    worker.connected.connect(lambda channel, name: connected.set(), type=DIRECT)
    worker.disconnected.connect(
        lambda reason: (reasons.append(reason), reason_done.set()), type=DIRECT
    )
    worker.start()
    try:
        assert worker.connect_device(PAIRED_DEVICE) is True
        assert connected.wait(3)
        assert worker.is_connected() is True
        assert worker.channel() == "RFCOMM"

        # 对端主动断开（读取循环读到 0 字节 / WinRT 抛 COMError）：收敛成一条 disconnected。
        backend.report_peer_disconnect()
        assert reason_done.wait(3)
        assert reasons == ["设备主动断开连接"]
        assert worker.is_connected() is False
        assert worker.channel() == ""
        # 通道清空后发送被拒绝，界面才会禁用发送区。
        assert worker.send(b"ping") is False

        # 幂等：同一条链路再报一次不会重复发 disconnected。
        backend.report_peer_disconnect()
        assert reasons == ["设备主动断开连接"]
    finally:
        worker.shutdown()


def test_shutdown_releases_pending_pairing_prompt(monkeypatch, qapp):
    monkeypatch.setattr(SppWorker, "PROMPT_TIMEOUT", 10.0)
    backend = FakeSppBackend()
    backend.prompt_enabled = True
    worker = SppWorker(backend)
    prompted = threading.Event()
    worker.pairing_prompt.connect(
        lambda kind, text: prompted.set(), type=DIRECT
    )
    worker.start()
    assert worker.connect_device(UNPAIRED_DEVICE) is True
    assert prompted.wait(3)

    # 关停要立刻返回：不能陪着一个没人回答的提示再等满 10 秒。
    worker.shutdown()
    assert not worker.thread_alive()
    assert worker.is_connected() is False


def test_shutdown_closes_socket_and_serial(qapp):
    rfcomm_backend = FakeSppBackend()
    rfcomm = SppWorker(rfcomm_backend)
    connected = threading.Event()
    rfcomm.connected.connect(lambda channel, name: connected.set(), type=DIRECT)
    rfcomm.start()
    assert rfcomm.connect_device(PAIRED_DEVICE) is True
    assert connected.wait(3)

    rfcomm.shutdown()
    assert rfcomm_backend.closed is True
    assert not rfcomm.thread_alive()

    serial = FakeSerialWorker()
    com = SppWorker(FakeSppBackend(), serial_factory=lambda: serial)
    com.start()
    assert com.open_com(SerialConfig("COM7", 115200)) is True
    com.shutdown()
    assert serial.close_calls == 1
    assert not com.thread_alive()


def test_second_scan_while_scanning_is_rejected(qapp):
    backend = FakeSppBackend()
    worker = SppWorker(backend)
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
        assert worker.stop_scan() is False
    finally:
        worker.shutdown()


def test_second_connect_while_connecting_is_rejected(qapp):
    backend = FakeSppBackend()
    backend.connect_delay = 0.3
    worker = SppWorker(backend)
    connected: list[tuple[str, str]] = []
    done = threading.Event()
    worker.connected.connect(
        lambda channel, name: (connected.append((channel, name)), done.set()), type=DIRECT
    )
    worker.start()
    try:
        assert worker.connect_device(PAIRED_DEVICE) is True
        assert worker.connect_device(PAIRED_DEVICE) is False
        assert done.wait(3)
        assert connected == [("RFCOMM", "已配对设备")]
        assert [call for call in backend.calls if call[0] == "connect"] == [
            ("connect", "11:22:33:44:55:66")
        ]
        # 已连接后再连也必须拒绝，换设备前要先断开。
        assert worker.connect_device(PAIRED_DEVICE) is False
    finally:
        worker.shutdown()


def test_rfcomm_and_com_channels_are_mutually_exclusive(qapp):
    backend = FakeSppBackend()
    serial = FakeSerialWorker()
    worker = SppWorker(backend, serial_factory=lambda: serial)
    connected = threading.Event()
    reason_done = threading.Event()
    worker.connected.connect(lambda channel, name: connected.set(), type=DIRECT)
    worker.disconnected.connect(lambda reason: reason_done.set(), type=DIRECT)
    worker.start()
    try:
        assert worker.connect_device(PAIRED_DEVICE) is True
        assert connected.wait(3)
        assert worker.open_com(SerialConfig("COM7", 115200)) is False
        assert serial.configs == []

        assert worker.disconnect_device("换通道") is True
        assert reason_done.wait(3)
        assert worker.open_com(SerialConfig("COM7", 115200)) is True
        assert worker.channel() == "COM7"
        assert worker.connect_device(PAIRED_DEVICE) is False
    finally:
        worker.shutdown()


def test_pair_error_ends_pairing_state(qapp):
    backend = FakeSppBackend()
    backend.fail_on = "pair"
    worker = SppWorker(backend)
    pairing_finished: list[str] = []
    errors: list[str] = []
    error_done = threading.Event()
    worker.pairing_finished.connect(pairing_finished.append, type=DIRECT)
    worker.error.connect(lambda text: (errors.append(text), error_done.set()), type=DIRECT)
    worker.start()
    try:
        assert worker.connect_device(UNPAIRED_DEVICE) is True
        assert error_done.wait(3)
        # 后端抛异常也要收尾：否则界面会一直停在「配对中」。
        assert pairing_finished == ["配对失败"]
        assert "配对模式" in errors[-1]
        assert [call[0] for call in backend.calls] == ["pair"]
        assert worker.is_connected() is False
    finally:
        worker.shutdown()


def test_disconnect_while_connecting_cancels_connection(qapp):
    backend = FakeSppBackend()
    backend.connect_delay = 0.3
    worker = SppWorker(backend)
    connected: list[tuple[str, str]] = []
    worker.connected.connect(
        lambda channel, name: connected.append((channel, name)), type=DIRECT
    )
    worker.start()
    try:
        assert worker.connect_device(PAIRED_DEVICE) is True
        assert worker.disconnect_device("连接中取消") is True
        # 连接协程跑完后必须自己把 socket 关掉，并且一次 connected 都不发。
        assert backend.closed_event.wait(3)
        assert connected == []
        assert worker.is_connected() is False
        assert worker.channel() == ""
    finally:
        worker.shutdown()


def test_pairing_budget_includes_prompt_time(monkeypatch, qapp):
    monkeypatch.setattr(spp_worker, "PAIR_TIMEOUT", 0.1)
    monkeypatch.setattr(SppWorker, "PROMPT_TIMEOUT", 0.5)
    backend = FakeSppBackend()
    # 比 PAIR_TIMEOUT 长，但没超过 PAIR_TIMEOUT + PROMPT_TIMEOUT：
    # 预算只算握手时间的话这里会被判成配对超时。
    backend.pair_delay = 0.3
    worker = SppWorker(backend)
    connected = threading.Event()
    errors: list[str] = []
    worker.connected.connect(lambda channel, name: connected.set(), type=DIRECT)
    worker.error.connect(errors.append, type=DIRECT)
    worker.start()
    try:
        assert worker.connect_device(UNPAIRED_DEVICE) is True
        assert connected.wait(3)
        assert errors == []
    finally:
        worker.shutdown()

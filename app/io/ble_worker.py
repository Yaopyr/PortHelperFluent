"""BLE 调试的后台工作线程：daemon asyncio 线程 + 注入的 BLE 后端，结果只走 Qt 信号。

`BleWorker` 本身不含蓝牙 I/O，全部动作交给注入的 `BleBackend`；生产实现
`BleakBackend` 用 bleak 3.0.2（Windows WinRT 后端）。构造时不联网、不导入设备，
连上之后才第一次访问适配器，因此界面可以安心懒建 worker，测试也只需假后端。

bleak 的 Windows 后端没有 `request_mtu()`，`mtu_size` 是系统协商后的只读值；也没有
`get_rssi()`（连接后设备一般停止广播），所以这里不提供这两个动作，`rssi_changed`
只在扫描期间发出。

所有协程都跑在 `AsyncLoopWorker` 的私有 loop 里，信号在 worker 线程发出；界面线程
按 Qt 的排队连接接收，不会阻塞。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Protocol

from PyQt6.QtCore import QObject, pyqtSignal

from app.io.async_worker import AsyncLoopWorker
from app.services.bluetooth import explain_error

CONNECT_TIMEOUT = 30.0
NOT_RUNNING_MESSAGE = "蓝牙后台线程未运行，请重新扫描"
PEER_DISCONNECT_MESSAGE = "设备主动断开连接"
NOT_CONNECTED_MESSAGE = "蓝牙未连接"
STAGE_LABELS: dict[str, str] = {
    "scan": "扫描",
    "connect": "连接",
    "read": "读取",
    "write": "写入",
    "notify": "订阅",
    "mtu": "MTU",
}


@dataclass(frozen=True)
class BleDeviceInfo:
    """一次广播里的设备信息（`seen_at` 是 `time.monotonic()` 时间戳）。"""

    name: str
    address: str
    rssi: int | None
    seen_at: float


@dataclass(frozen=True)
class BleCharInfo:
    """特征值的原始 UUID 与 bleak 属性名元组（徽章由界面层转换）。"""

    uuid: str
    properties: tuple[str, ...]


@dataclass(frozen=True)
class BleServiceInfo:
    """一个 GATT 服务及其特征值。"""

    uuid: str
    characteristics: tuple[BleCharInfo, ...]


class BleBackend(Protocol):
    """BLE 后端协议；`stop` 是零参可调用对象，返回 True 表示请求提前结束扫描。"""

    def set_disconnect_callback(self, callback: Callable[[str], None]) -> None: ...

    async def scan(
        self,
        duration: float,
        on_device: Callable[[BleDeviceInfo], None],
        stop: Callable[[], bool],
    ) -> None: ...

    async def connect(self, address: str) -> None: ...

    async def disconnect(self) -> None: ...

    async def services(self) -> list[BleServiceInfo]: ...

    def mtu(self) -> int: ...

    async def read(self, char_uuid: str) -> bytes: ...

    async def write(self, char_uuid: str, data: bytes, response: bool) -> None: ...

    async def set_notify(
        self, char_uuid: str, enabled: bool, on_data: Callable[[str, bytes], None]
    ) -> None: ...


class BleakBackend:
    """bleak 3.0.2（WinRT）实现；构造只建空壳，不触碰适配器。"""

    def __init__(self) -> None:
        self._client = None
        self._disconnect_cb: Callable[[str], None] | None = None

    def set_disconnect_callback(self, callback: Callable[[str], None]) -> None:
        self._disconnect_cb = callback

    async def scan(
        self,
        duration: float,
        on_device: Callable[[BleDeviceInfo], None],
        stop: Callable[[], bool],
    ) -> None:
        from bleak import BleakScanner

        def on_advertisement(device, advertisement) -> None:
            on_device(
                BleDeviceInfo(
                    advertisement.local_name or device.name or "",
                    device.address,
                    advertisement.rssi,
                    time.monotonic(),
                )
            )

        scanner = BleakScanner(detection_callback=on_advertisement)
        await scanner.start()
        try:
            deadline = time.monotonic() + max(0.0, float(duration))
            while time.monotonic() < deadline:
                if stop():
                    break
                await asyncio.sleep(0.1)
        finally:
            await scanner.stop()

    async def connect(self, address: str) -> None:
        from bleak import BleakClient

        client = BleakClient(address, disconnected_callback=self._on_disconnect)
        await client.connect()
        self._client = client

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.disconnect()

    async def services(self) -> list[BleServiceInfo]:
        client = self._require_client()
        return [
            BleServiceInfo(
                str(service.uuid),
                tuple(
                    BleCharInfo(str(char.uuid), tuple(char.properties))
                    for char in service.characteristics
                ),
            )
            for service in client.services
        ]

    def mtu(self) -> int:
        return int(self._require_client().mtu_size)

    async def read(self, char_uuid: str) -> bytes:
        return bytes(await self._require_client().read_gatt_char(char_uuid))

    async def write(self, char_uuid: str, data: bytes, response: bool) -> None:
        await self._require_client().write_gatt_char(char_uuid, bytes(data), response=response)

    async def set_notify(
        self, char_uuid: str, enabled: bool, on_data: Callable[[str, bytes], None]
    ) -> None:
        client = self._require_client()
        if enabled:
            await client.start_notify(
                char_uuid, lambda _char, data: on_data(char_uuid, bytes(data))
            )
        else:
            await client.stop_notify(char_uuid)

    def _on_disconnect(self, _client) -> None:
        callback = self._disconnect_cb
        if callback is not None:
            callback(PEER_DISCONNECT_MESSAGE)

    def _require_client(self):
        if self._client is None:
            raise RuntimeError(NOT_CONNECTED_MESSAGE)
        return self._client


class BleWorker(AsyncLoopWorker):
    """BLE 调试的全部后台动作；界面只通过信号拿结果。"""

    scan_started = pyqtSignal(int)  # 秒
    scan_finished = pyqtSignal()
    device_found = pyqtSignal(object)  # BleDeviceInfo
    connected = pyqtSignal(str, int)  # address, mtu
    disconnected = pyqtSignal(str)  # 中文原因
    services_ready = pyqtSignal(object)  # list[BleServiceInfo]
    notification = pyqtSignal(str, bytes)
    read_result = pyqtSignal(str, bytes)
    write_finished = pyqtSignal(str, int)
    mtu_changed = pyqtSignal(int)
    rssi_changed = pyqtSignal(str, object)  # address, rssi

    def __init__(self, backend: BleBackend | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._backend = backend if backend is not None else BleakBackend()
        self._backend.set_disconnect_callback(self._on_backend_disconnect)
        self._scanning = False
        self._scan_stop = threading.Event()
        self._connected = False
        self._connecting = False
        self._self_disconnect = threading.Event()

    def _on_backend_disconnect(self, reason: str) -> None:
        """后端（bleak / 假后端）报告连接已断开。"""
        if self._self_disconnect.is_set():
            return
        self._connected = False
        self.disconnected.emit(reason or PEER_DISCONNECT_MESSAGE)

    # ---- 扫描 ---------------------------------------------------------

    def start_scan(self, duration: float = 10.0) -> bool:
        if self._scanning:
            # 已有扫描在跑：再投一个会让先结束的那个把 _scanning 复位，停止按钮失效。
            return False
        seconds = max(0, int(duration))
        self._scan_stop.clear()
        self._scanning = True
        self.scan_started.emit(seconds)
        if not self._submit_or_report(lambda: self._scan_coroutine(duration)):
            self._scanning = False
            return False
        return True

    def stop_scan(self) -> bool:
        if not self._scanning:
            return False
        self._scan_stop.set()
        return True

    async def _scan_coroutine(self, duration: float) -> None:
        self._stage = "scan"
        try:
            await self._backend.scan(duration, self._on_device, self._scan_stop.is_set)
        finally:
            self._scanning = False
            self.scan_finished.emit()

    def _on_device(self, info: BleDeviceInfo) -> None:
        self.device_found.emit(info)
        self.rssi_changed.emit(info.address, info.rssi)

    # ---- 连接 ---------------------------------------------------------

    def connect_device(self, address: str) -> bool:
        if self._connecting or self._connected:
            # 连接在飞或已连着：再连会覆盖 backend 里的 client，留下一条泄漏连接。
            return False
        self._connecting = True
        if not self._submit_or_report(lambda: self._connect_coroutine(address)):
            self._connecting = False
            return False
        return True

    def disconnect_device(self, reason: str = "用户断开") -> bool:
        return self._submit_or_report(lambda: self._disconnect_coroutine(reason))

    async def _connect_coroutine(self, address: str) -> None:
        self._stage = "connect"
        try:
            try:
                await asyncio.wait_for(self._backend.connect(address), CONNECT_TIMEOUT)
                services = await self._backend.services()
                mtu = int(self._backend.mtu())
            except TimeoutError:
                self.error.emit(explain_error("connect", "连接超时"))
                return
            self._connected = True
        finally:
            self._connecting = False
        self.services_ready.emit(services)
        self.connected.emit(address, mtu)
        self.mtu_changed.emit(mtu)

    async def _disconnect_coroutine(self, reason: str) -> None:
        self._stage = "connect"
        self._self_disconnect.set()
        try:
            await self._backend.disconnect()
        finally:
            self._self_disconnect.clear()
            self._connected = False
        self.disconnected.emit(reason)

    # ---- 读写与通知 ---------------------------------------------------

    def read_char(self, char_uuid: str) -> bool:
        return self._submit_or_report(lambda: self._read_coroutine(char_uuid))

    def write_char(self, char_uuid: str, data: bytes, response: bool = True) -> bool:
        payload = bytes(data)
        return self._submit_or_report(lambda: self._write_coroutine(char_uuid, payload, response))

    def subscribe(self, char_uuid: str) -> bool:
        return self._submit_or_report(lambda: self._notify_coroutine(char_uuid, True))

    def unsubscribe(self, char_uuid: str) -> bool:
        return self._submit_or_report(lambda: self._notify_coroutine(char_uuid, False))

    async def _read_coroutine(self, char_uuid: str) -> None:
        self._stage = "read"
        data = bytes(await self._backend.read(char_uuid))
        self.read_result.emit(char_uuid, data)

    async def _write_coroutine(self, char_uuid: str, data: bytes, response: bool) -> None:
        self._stage = "write"
        await self._backend.write(char_uuid, data, response)
        self.write_finished.emit(char_uuid, len(data))

    async def _notify_coroutine(self, char_uuid: str, enabled: bool) -> None:
        self._stage = "notify"
        await self._backend.set_notify(char_uuid, enabled, self._on_notification)

    def _on_notification(self, char_uuid: str, data: bytes) -> None:
        self.notification.emit(char_uuid, bytes(data))

    # ---- MTU 与状态 ---------------------------------------------------

    def refresh_mtu(self) -> bool:
        return self._submit_or_report(self._mtu_coroutine)

    async def _mtu_coroutine(self) -> None:
        self._stage = "mtu"
        self.mtu_changed.emit(int(self._backend.mtu()))

    def is_connected(self) -> bool:
        return self._connected

    def is_scanning(self) -> bool:
        return self._scanning

    # ---- 内部辅助 -----------------------------------------------------

    def _submit_or_report(
        self, factory: Callable[[], Coroutine[Any, Any, None]]
    ) -> bool:
        """投递任务；后台线程不可用时发一次提示并返回 False。"""
        if self.submit(factory):
            return True
        self.error.emit(NOT_RUNNING_MESSAGE)
        return False

    def _describe_error(self, exc: BaseException) -> str:
        text = explain_error(self._stage, str(exc))
        if text:
            return text
        # 阶段无提示且异常消息为空时也要给出中文文案，不能把空串发给界面。
        return f"{STAGE_LABELS.get(self._stage, '蓝牙')}操作失败"

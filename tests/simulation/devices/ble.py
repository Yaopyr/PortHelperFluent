"""有状态虚拟 BLE 后端；实现生产代码的 BleBackend 协议，不导入 bleak。"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field

from app.io.ble_worker import (
    BleBackend,
    BleCharInfo,
    BleDeviceInfo,
    BleServiceInfo,
)

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
READ_UUID = "00002a00-0000-1000-8000-00805f9b34fb"

DEFAULT_BLE_DEVICES = (
    BleDeviceInfo("模拟透传模块", "AA:BB:CC:DD:EE:FF", -48, 0.0),
    BleDeviceInfo("", "11:22:33:44:55:66", -86, 0.0),
)

DEFAULT_BLE_SERVICES = (
    BleServiceInfo(
        SERVICE_UUID,
        (
            BleCharInfo(WRITE_UUID, ("read", "write", "notify")),
            BleCharInfo(READ_UUID, ("read",)),
        ),
    ),
)


class VirtualBleBackend(BleBackend):
    def __init__(
        self,
        devices=DEFAULT_BLE_DEVICES,
        services=DEFAULT_BLE_SERVICES,
    ) -> None:
        self.devices = [BleDeviceInfo(item.name, item.address, item.rssi, time.monotonic()) for item in devices]
        self.service_list = list(services)
        self.mtu_value = 23
        self.read_value = b"\x01\x02"
        self.connect_delay = 0.0
        self.scan_hold_s = 0.15
        self.fail_on: str | None = None
        self.suppress_notifications = False
        self.connected = False
        self.calls: list[tuple] = []
        self.notify_cb = None
        self.notify_ready = threading.Event()
        self.disconnect_cb = None
        self._lock = threading.Lock()

    def set_disconnect_callback(self, callback) -> None:
        self.disconnect_cb = callback

    async def scan(self, duration, on_device, stop) -> None:
        self.calls.append(("scan", duration))
        if self.fail_on == "scan":
            raise RuntimeError("模拟蓝牙适配器不可用")
        for info in self.devices:
            on_device(info)
        deadline = time.monotonic() + max(0.0, min(float(duration), self.scan_hold_s))
        while time.monotonic() < deadline:
            if stop():
                return
            await asyncio.sleep(0.02)

    async def connect(self, address: str) -> None:
        self.calls.append(("connect", address))
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.fail_on == "connect":
            raise RuntimeError("模拟 BLE 连接失败")
        with self._lock:
            self.connected = True

    async def disconnect(self) -> None:
        self.calls.append(("disconnect",))
        with self._lock:
            self.connected = False

    async def services(self) -> list[BleServiceInfo]:
        self.calls.append(("services",))
        if self.fail_on == "services":
            raise RuntimeError("模拟服务发现失败")
        return list(self.service_list)

    def mtu(self) -> int:
        self.calls.append(("mtu",))
        return int(self.mtu_value)

    async def read(self, char_uuid: str) -> bytes:
        self.calls.append(("read", char_uuid))
        if self.fail_on == "read":
            raise RuntimeError("模拟特征值读取失败")
        return bytes(self.read_value)

    async def write(self, char_uuid: str, data: bytes, response: bool) -> None:
        self.calls.append(("write", char_uuid, bytes(data), bool(response)))
        if self.fail_on == "write":
            raise RuntimeError("模拟特征值写入失败")

    async def set_notify(self, char_uuid: str, enabled: bool, on_data) -> None:
        self.calls.append(("notify", char_uuid, bool(enabled)))
        if self.fail_on == "notify":
            raise RuntimeError("模拟订阅失败")
        self.notify_cb = on_data if enabled else None
        if enabled:
            self.notify_ready.set()

    # ---- 模拟控制 -----------------------------------------------------

    def emit_notification(self, char_uuid: str, data: bytes) -> None:
        if self.suppress_notifications:
            return
        callback = self.notify_cb
        if callback is not None:
            callback(char_uuid, bytes(data))

    def drop(self, reason: str = "模拟设备主动断开连接") -> None:
        with self._lock:
            self.connected = False
        if self.disconnect_cb is not None:
            self.disconnect_cb(reason)

    def snapshot(self) -> dict:
        return {
            "devices": [
                {"name": item.name, "address": item.address, "rssi": item.rssi}
                for item in self.devices
            ],
            "services": [
                {
                    "uuid": service.uuid,
                    "characteristics": [
                        {"uuid": char.uuid, "properties": list(char.properties)}
                        for char in service.characteristics
                    ],
                }
                for service in self.service_list
            ],
            "mtu": self.mtu_value,
            "connected": self.connected,
            "calls": [tuple(call) for call in self.calls],
        }

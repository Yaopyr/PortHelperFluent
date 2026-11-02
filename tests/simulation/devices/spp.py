"""有状态虚拟经典蓝牙 SPP 后端；实现生产代码的 SppBackend 协议。"""

from __future__ import annotations

import asyncio
import threading
import time

from app.io.spp_worker import ClassicDeviceInfo, SppBackend


DEFAULT_SPP_DEVICES = (
    ClassicDeviceInfo("模拟已配对设备", "11:22:33:44:55:66", True, "sim-paired"),
    ClassicDeviceInfo("模拟未配对设备", "AA:BB:CC:DD:EE:01", False, "sim-unpaired"),
)


class VirtualSppBackend(SppBackend):
    def __init__(self, devices=DEFAULT_SPP_DEVICES) -> None:
        self.devices = list(devices)
        self.prompt_kind: str | None = "confirm"
        self.prompt_text = "请在设备上确认配对"
        self.pair_status = "PAIRED"
        self.pair_delay = 0.0
        self.connect_delay = 0.0
        self.scan_hold_s = 0.15
        self.fail_on: str | None = None
        self.connected = False
        self.calls: list[tuple] = []
        self.data_cb = None
        self.prompt_cb = None
        self.disconnect_cb = None
        self._lock = threading.Lock()

    def set_data_callback(self, callback) -> None:
        self.data_cb = callback

    def set_prompt_callback(self, callback) -> None:
        self.prompt_cb = callback

    def set_disconnect_callback(self, callback) -> None:
        self.disconnect_cb = callback

    async def scan(self, duration, on_device, stop) -> None:
        self.calls.append(("scan", duration))
        if self.fail_on == "scan":
            raise RuntimeError("模拟经典蓝牙扫描失败")
        for device in self.devices:
            on_device(device)
        deadline = time.monotonic() + max(0.0, min(float(duration), self.scan_hold_s))
        while time.monotonic() < deadline:
            if stop():
                return
            await asyncio.sleep(0.02)

    async def pair(self, device: ClassicDeviceInfo) -> str:
        self.calls.append(("pair", device.address))
        if self.fail_on == "pair":
            raise RuntimeError("模拟配对失败")
        if self.pair_delay:
            await asyncio.sleep(self.pair_delay)
        if device.paired:
            return "ALREADY_PAIRED"
        if self.prompt_cb is not None and self.prompt_kind:
            answer = self.prompt_cb(self.prompt_kind, self.prompt_text)
            if answer is None:
                return "REJECTED"
        return self.pair_status

    async def connect(self, device: ClassicDeviceInfo) -> None:
        self.calls.append(("connect", device.address))
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.fail_on == "connect":
            raise RuntimeError("模拟 RFCOMM 连接失败")
        with self._lock:
            self.connected = True

    async def send(self, data: bytes) -> int:
        payload = bytes(data)
        self.calls.append(("send", payload))
        if not self.connected:
            raise RuntimeError("模拟 SPP 未连接")
        if self.fail_on == "send":
            raise RuntimeError("模拟 SPP 发送失败")
        return len(payload)

    async def close(self) -> None:
        self.calls.append(("close",))
        with self._lock:
            self.connected = False

    # ---- 模拟控制 -----------------------------------------------------

    def deliver(self, data: bytes) -> None:
        if self.data_cb is not None:
            self.data_cb(bytes(data))

    def drop_peer(self, reason: str = "模拟对端主动断开") -> None:
        with self._lock:
            self.connected = False
        if self.disconnect_cb is not None:
            self.disconnect_cb(reason)

    def snapshot(self) -> dict:
        return {
            "devices": [
                {
                    "name": device.name,
                    "address": device.address,
                    "paired": device.paired,
                }
                for device in self.devices
            ],
            "connected": self.connected,
            "calls": [tuple(call) for call in self.calls],
        }

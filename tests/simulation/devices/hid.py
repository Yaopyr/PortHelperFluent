"""有状态虚拟 HID 后端；实现生产代码的 HidBackend 协议。"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field

from app.io.hid_worker import HidBackend, HidDeviceInfo


@dataclass(frozen=True)
class VirtualHidHandle:
    path: bytes


DEFAULT_HID_DEVICES = (
    HidDeviceInfo(
        path=b"sim-hid-keyboard",
        vendor_id=0x1234,
        product_id=0x5678,
        product="模拟键盘",
        manufacturer="Sim Devices",
        serial="SIM-KB-1",
        usage_page=0x01,
        usage=0x06,
        interface=0,
    ),
    HidDeviceInfo(
        path=b"sim-hid-vendor",
        vendor_id=0x0483,
        product_id=0x5750,
        product="模拟厂商接口",
        manufacturer="Sim Devices",
        serial="SIM-VENDOR-1",
        usage_page=0xFF00,
        usage=0x0001,
        interface=1,
    ),
)


class VirtualHidBackend(HidBackend):
    def __init__(self, devices=DEFAULT_HID_DEVICES) -> None:
        self.devices = list(devices)
        self.writes: list[tuple[bytes, bytes]] = []
        self.opened_paths: list[bytes] = []
        self.closed_paths: list[bytes] = []
        self.fail_open = False
        self.fail_write = False
        self.short_write: int | None = None
        self.fail_read = False
        self._queues: dict[bytes, queue.Queue[bytes]] = {
            info.path: queue.Queue() for info in self.devices
        }
        self._unplugged: set[bytes] = set()
        self._lock = threading.Lock()

    def enumerate(self) -> list[HidDeviceInfo]:
        return list(self.devices)

    def open(self, path: bytes) -> object:
        if self.fail_open:
            raise OSError("模拟 HID 设备拒绝打开（被占用）")
        handle = VirtualHidHandle(bytes(path))
        with self._lock:
            self.opened_paths.append(handle.path)
            self._queues.setdefault(handle.path, queue.Queue())
            self._unplugged.discard(handle.path)
        return handle

    def write(self, handle: object, data: bytes) -> int:
        path = getattr(handle, "path", b"")
        if path in self._unplugged:
            raise OSError("模拟 HID 设备已拔出")
        if self.fail_write:
            raise OSError("模拟 HID 写入失败")
        payload = bytes(data)
        written = len(payload)
        if self.short_write is not None:
            written = min(max(0, int(self.short_write)), written)
            self.short_write = None
        with self._lock:
            self.writes.append((path, payload[:written]))
        return written

    def read(self, handle: object, size: int, timeout_ms: int) -> bytes:
        path = getattr(handle, "path", b"")
        if path in self._unplugged:
            raise OSError("模拟 HID 设备已拔出")
        if self.fail_read:
            raise OSError("模拟 HID 读取失败")
        queue_for_path = self._queues.setdefault(path, queue.Queue())
        try:
            chunk = queue_for_path.get(timeout=max(0.0, timeout_ms / 1000.0))
        except queue.Empty:
            return b""
        return bytes(chunk[: max(0, int(size))])

    def close(self, handle: object) -> None:
        path = getattr(handle, "path", b"")
        with self._lock:
            self.closed_paths.append(path)

    # ---- 模拟控制 -----------------------------------------------------

    def push_input(self, data: bytes, path: bytes | None = None) -> None:
        target = bytes(path) if path is not None else self.devices[0].path
        self._queues.setdefault(target, queue.Queue()).put(bytes(data))

    def unplug(self, path: bytes | None = None) -> None:
        with self._lock:
            if path is None:
                self._unplugged.update(info.path for info in self.devices)
            else:
                self._unplugged.add(bytes(path))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "devices": [
                    {
                        "path": info.path.decode("utf-8", "replace"),
                        "id": info.id_text,
                        "product": info.product,
                        "usage": info.usage_text,
                        "interface": info.interface,
                    }
                    for info in self.devices
                ],
                "opened_paths": [path.decode("utf-8", "replace") for path in self.opened_paths],
                "closed_paths": [path.decode("utf-8", "replace") for path in self.closed_paths],
                "writes": [
                    {"path": path.decode("utf-8", "replace"), "data": data.hex()}
                    for path, data in self.writes
                ],
                "unplugged": [path.decode("utf-8", "replace") for path in self._unplugged],
            }

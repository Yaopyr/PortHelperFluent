"""线程安全的虚拟串口设备；实现生产代码的 SerialTransport 协议。"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

from app.io.serial_worker import PortInfo, SerialConfig, SerialTransport


@dataclass
class VirtualSerialDevice:
    """一个有状态的虚拟串口；读取、写入、线路控制和故障都可观察/注入。"""

    port: str
    description: str = "虚拟串口"
    hwid: str = ""
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    echo: bool = False
    fail_open: bool = False
    fail_read: bool = False
    fail_write: bool = False
    silent_write: bool = False
    short_write_once: int | None = None
    periodic: bytes = b""
    period_s: float = 0.0
    modem: dict[str, bool] = field(
        default_factory=lambda: {"cts": False, "dsr": False, "ri": False, "cd": False}
    )
    writes: list[bytes] = field(default_factory=list)
    commands: list[tuple[str, object]] = field(default_factory=list)
    open_config: SerialConfig | None = None
    open_count: int = 0
    close_count: int = 0
    dropped_reason: str = ""
    _rx: queue.Queue[bytes] = field(default_factory=queue.Queue)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _opened: bool = False
    _dropped: bool = False
    _next_periodic: float = 0.0

    def open(self, config: SerialConfig) -> None:
        with self._lock:
            if self.fail_open:
                raise OSError("模拟设备拒绝打开（端口被占用）")
            self._opened = True
            self._dropped = False
            self.dropped_reason = ""
            self.open_config = config
            self.open_count += 1
            self._next_periodic = time.monotonic() + max(0.0, self.period_s)

    def close(self) -> None:
        with self._lock:
            if not self._opened:
                return
            self._opened = False
            self.close_count += 1

    def _queue_periodic(self) -> None:
        if not self.periodic or self.period_s <= 0:
            return
        now = time.monotonic()
        if now >= self._next_periodic:
            self._rx.put(bytes(self.periodic))
            self._next_periodic = now + self.period_s

    def read(self, size: int, timeout: float) -> bytes:
        if size <= 0:
            return b""
        if not self._opened:
            raise OSError("串口未打开")
        if self._dropped:
            raise OSError(self.dropped_reason or "模拟设备已断开")
        if self.fail_read:
            raise OSError("模拟读取失败（设备拔出）")
        self._queue_periodic()
        try:
            chunk = self._rx.get(timeout=max(0.0, float(timeout)))
        except queue.Empty:
            return b""
        if self._dropped:
            raise OSError(self.dropped_reason or "模拟设备已断开")
        if len(chunk) > size:
            self._rx.put(chunk[size:])
            chunk = chunk[:size]
        return bytes(chunk)

    def write(self, data: bytes) -> int:
        payload = bytes(data)
        with self._lock:
            if not self._opened:
                raise OSError("串口未打开")
            if self._dropped:
                raise OSError(self.dropped_reason or "模拟设备已断开")
            if self.fail_write:
                raise OSError("模拟写入失败（设备拔出）")
            written = len(payload)
            if self.short_write_once is not None:
                written = min(max(0, int(self.short_write_once)), written)
                self.short_write_once = None
            chunk = payload[:written]
            if self.silent_write:
                return written
            self.writes.append(chunk)
            if self.echo and chunk:
                self._rx.put(chunk)
            return written

    def set_dtr(self, value: bool) -> None:
        with self._lock:
            if not self._opened:
                raise OSError("串口未打开")
            self.commands.append(("dtr", bool(value)))

    def set_rts(self, value: bool) -> None:
        with self._lock:
            if not self._opened:
                raise OSError("串口未打开")
            self.commands.append(("rts", bool(value)))

    def send_break(self, duration: float = 0.25) -> None:
        with self._lock:
            if not self._opened:
                raise OSError("串口未打开")
            self.commands.append(("break", float(duration)))

    def modem_status(self) -> dict[str, bool]:
        with self._lock:
            if self._dropped:
                return {"cts": False, "dsr": False, "ri": False, "cd": False}
            return dict(self.modem)

    # ---- 模拟控制 -----------------------------------------------------

    def push_rx(self, data: bytes) -> None:
        if data:
            self._rx.put(bytes(data))

    def set_modem(self, **values: bool) -> None:
        with self._lock:
            for key, value in values.items():
                if key in self.modem:
                    self.modem[key] = bool(value)

    def drop(self, reason: str = "模拟设备已断开") -> None:
        with self._lock:
            self._dropped = True
            self.dropped_reason = reason

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "port": self.port,
                "opened": self._opened,
                "dropped": self._dropped,
                "open_count": self.open_count,
                "close_count": self.close_count,
                "writes": [chunk.hex() for chunk in self.writes],
                "write_bytes": sum(len(chunk) for chunk in self.writes),
                "commands": list(self.commands),
                "modem": dict(self.modem),
                "config": None if self.open_config is None else self.open_config.__dict__.copy(),
            }


class VirtualSerialHub:
    def __init__(self) -> None:
        self.devices: dict[str, VirtualSerialDevice] = {}
        self.add(
            VirtualSerialDevice(
                "COM7",
                "虚拟串口（模拟设备）",
                "VIRTUAL VID:PID=1234:5678",
                0x1234,
                0x5678,
                "SIM-SERIAL-1",
                echo=True,
            )
        )
        self.add(
            VirtualSerialDevice(
                "COM8",
                "蓝牙串行端口",
                r"BTHENUM\{00001101-0000-1000-8000-00805F9B34FB}",
            )
        )

    def add(self, device: VirtualSerialDevice) -> VirtualSerialDevice:
        self.devices[device.port] = device
        return device

    def ensure(self, port: str) -> VirtualSerialDevice:
        device = self.devices.get(port)
        if device is None:
            device = self.add(VirtualSerialDevice(port, "虚拟串口（手动输入）"))
        return device

    def device(self, port: str) -> VirtualSerialDevice:
        return self.ensure(port)

    def ports(self) -> list[PortInfo]:
        return [
            PortInfo(
                device.port,
                device.description,
                device.hwid,
                device.vid,
                device.pid,
                device.serial_number,
            )
            for device in self.devices.values()
        ]

    def transport(self) -> "VirtualSerialTransport":
        return VirtualSerialTransport(self)

    def snapshot(self) -> dict:
        return {port: device.snapshot() for port, device in self.devices.items()}


class VirtualSerialTransport(SerialTransport):
    def __init__(self, hub: VirtualSerialHub) -> None:
        self._hub = hub
        self._device: VirtualSerialDevice | None = None

    def open(self, config: SerialConfig) -> None:
        self._device = self._hub.ensure(config.port)
        self._device.open(config)

    def close(self) -> None:
        if self._device is not None:
            self._device.close()

    def _require(self) -> VirtualSerialDevice:
        if self._device is None:
            raise OSError("串口未打开")
        return self._device

    def read(self, size: int, timeout: float) -> bytes:
        return self._require().read(size, timeout)

    def write(self, data: bytes) -> int:
        return self._require().write(data)

    def set_dtr(self, value: bool) -> None:
        self._require().set_dtr(value)

    def set_rts(self, value: bool) -> None:
        self._require().set_rts(value)

    def send_break(self, duration: float = 0.25) -> None:
        self._require().send_break(duration)

    def modem_status(self) -> dict[str, bool]:
        return self._require().modem_status()

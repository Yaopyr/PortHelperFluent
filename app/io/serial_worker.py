"""串口 transport 与工作线程：所有阻塞读写都在工作线程中完成。"""

from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import serial
from serial.tools import list_ports as serial_list_ports
from PyQt6.QtCore import QThread, pyqtSignal

from app.core.logging_setup import get_logger

READ_CHUNK = 4096
READ_TIMEOUT_S = 0.05
MODEM_INTERVAL_S = 0.1

_PARITY = {
    "none": serial.PARITY_NONE,
    "odd": serial.PARITY_ODD,
    "even": serial.PARITY_EVEN,
    "mark": serial.PARITY_MARK,
    "space": serial.PARITY_SPACE,
}
_STOPBITS = {
    1: serial.STOPBITS_ONE,
    1.5: serial.STOPBITS_ONE_POINT_FIVE,
    2: serial.STOPBITS_TWO,
}


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str
    hwid: str
    vid: int | None
    pid: int | None
    serial_number: str | None


@dataclass(frozen=True)
class SerialConfig:
    port: str
    baud: int = 115200
    bytesize: int = 8
    stopbits: float = 1
    parity: str = "none"
    flow: str = "none"
    encoding: str = "utf-8"


class SerialTransport(Protocol):
    def open(self, config: SerialConfig) -> None: ...
    def close(self) -> None: ...
    def read(self, size: int, timeout: float) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def set_dtr(self, value: bool) -> None: ...
    def set_rts(self, value: bool) -> None: ...
    def send_break(self, duration: float = 0.25) -> None: ...
    def modem_status(self) -> dict[str, bool]: ...


def list_ports() -> list[PortInfo]:
    """返回当前串口；枚举失败由调用方显示为空列表而不是崩溃。"""
    result: list[PortInfo] = []
    try:
        for item in serial_list_ports.comports():
            result.append(
                PortInfo(
                    device=item.device,
                    description=item.description or "",
                    hwid=item.hwid or "",
                    vid=item.vid,
                    pid=item.pid,
                    serial_number=item.serial_number,
                )
            )
    except Exception:
        get_logger().exception("枚举串口失败")
        return []
    return sorted(result, key=lambda item: _natural_key(item.device))


def _natural_key(text: str) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


class PyserialTransport:
    """pyserial 适配层；不持有 Qt 对象，便于测试替换。"""

    def __init__(self) -> None:
        self._serial: serial.Serial | None = None

    def open(self, config: SerialConfig) -> None:
        self._serial = serial.Serial(
            port=config.port,
            baudrate=int(config.baud),
            bytesize=int(config.bytesize),
            stopbits=_STOPBITS.get(float(config.stopbits), serial.STOPBITS_ONE),
            parity=_PARITY.get(config.parity, serial.PARITY_NONE),
            timeout=READ_TIMEOUT_S,
            write_timeout=2,
            rtscts=config.flow == "rtscts",
            xonxoff=config.flow == "xonxoff",
        )

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                get_logger().exception("关闭串口句柄失败")
            finally:
                self._serial = None

    def _require(self) -> serial.Serial:
        if self._serial is None:
            raise serial.SerialException("串口尚未打开")
        return self._serial

    def read(self, size: int, timeout: float) -> bytes:
        handle = self._require()
        handle.timeout = timeout
        return handle.read(size)

    def write(self, data: bytes) -> int:
        return int(self._require().write(data) or 0)

    def set_dtr(self, value: bool) -> None:
        self._require().dtr = bool(value)

    def set_rts(self, value: bool) -> None:
        self._require().rts = bool(value)

    def send_break(self, duration: float = 0.25) -> None:
        self._require().send_break(duration)

    def modem_status(self) -> dict[str, bool]:
        handle = self._require()
        return {
            "cts": bool(getattr(handle, "cts", False)),
            "dsr": bool(getattr(handle, "dsr", False)),
            "ri": bool(getattr(handle, "ri", False)),
            "cd": bool(getattr(handle, "cd", False)),
        }


class SerialWorker(QThread):
    """单线程串口读写；写队列与线路控制命令由同一线程顺序执行。"""

    opened = pyqtSignal()
    closed = pyqtSignal()
    data_received = pyqtSignal(bytes)
    data_sent = pyqtSignal(bytes)
    error = pyqtSignal(str)
    modem_changed = pyqtSignal(dict)

    def __init__(self, transport: SerialTransport | None = None, parent=None) -> None:
        super().__init__(parent)
        self._transport = transport if transport is not None else PyserialTransport()
        self._writes: queue.Queue[bytes] = queue.Queue()
        self._commands: queue.Queue[tuple[str, object]] = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._opened = False

    def open_port(self, config: SerialConfig) -> bool:
        """打开端口并启动读线程；失败时发 error 并返回 False。"""
        if self.isRunning():
            self.error.emit("串口已经在运行")
            return False
        try:
            self._transport.open(config)
        except Exception as exc:
            self.error.emit(f"打开串口失败：{exc}")
            return False
        self._writes = queue.Queue()
        self._commands = queue.Queue()
        self._stop.clear()
        with self._lock:
            self._opened = True
        self.start()
        self.opened.emit()
        return True

    def close_port(self) -> None:
        """停止读线程并等待句柄关闭；不会从 UI 线程直接碰串口句柄。"""
        self._stop.set()
        if self.isRunning():
            if not self.wait(2000):
                get_logger().warning("串口线程未在 2 秒内退出")
            return
        try:
            self._transport.close()
        finally:
            with self._lock:
                self._opened = False
            self.closed.emit()

    def send(self, data: bytes) -> bool:
        if not data:
            return True
        if not self.is_open():
            self.error.emit("串口未打开，无法发送数据")
            return False
        self._writes.put(bytes(data))
        return True

    def set_dtr(self, value: bool) -> bool:
        if not self.is_open():
            return False
        self._commands.put(("dtr", bool(value)))
        return True

    def set_rts(self, value: bool) -> bool:
        if not self.is_open():
            return False
        self._commands.put(("rts", bool(value)))
        return True

    def send_break(self) -> bool:
        if not self.is_open():
            self.error.emit("串口未打开，无法发送 BREAK")
            return False
        self._commands.put(("break", None))
        return True

    def is_open(self) -> bool:
        with self._lock:
            return bool(self._opened and self.isRunning())

    def run(self) -> None:
        last_modem = 0.0
        try:
            while not self._stop.is_set():
                if not self._drain_commands():
                    break
                if not self._drain_writes():
                    break
                try:
                    chunk = self._transport.read(READ_CHUNK, READ_TIMEOUT_S)
                except Exception as exc:
                    self.error.emit(f"串口读取失败：{exc}")
                    break
                if chunk:
                    self.data_received.emit(bytes(chunk))
                now = time.monotonic()
                if now - last_modem >= MODEM_INTERVAL_S:
                    last_modem = now
                    try:
                        self.modem_changed.emit(self._transport.modem_status())
                    except Exception:
                        get_logger().exception("读取串口线路状态失败")
        except Exception as exc:
            self.error.emit(f"串口线程异常：{exc}")
        finally:
            try:
                self._transport.close()
            except Exception:
                get_logger().exception("串口线程关闭句柄失败")
            with self._lock:
                self._opened = False
            self.closed.emit()

    def _drain_writes(self) -> bool:
        while True:
            try:
                data = self._writes.get_nowait()
            except queue.Empty:
                return True
            try:
                written = self._transport.write(data)
            except Exception as exc:
                self.error.emit(f"串口写入失败：{exc}")
                return False
            if written:
                self.data_sent.emit(bytes(data[:written]))

    def _drain_commands(self) -> bool:
        while True:
            try:
                name, value = self._commands.get_nowait()
            except queue.Empty:
                return True
            try:
                if name == "dtr":
                    self._transport.set_dtr(bool(value))
                elif name == "rts":
                    self._transport.set_rts(bool(value))
                elif name == "break":
                    self._transport.send_break()
            except Exception as exc:
                self.error.emit(f"串口线路控制失败：{exc}")
                return False

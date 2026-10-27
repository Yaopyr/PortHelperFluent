"""USB HID 后端与读取线程：hidapi 的阻塞调用全部在工作线程中完成。

`hid` 模块只在真正枚举/打开时才导入，缺少 hidapi DLL 时应用仍可正常启动；
失败统一收敛为「空列表 + 警告日志」或 `error` 信号，不向 UI 抛异常。
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Protocol

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.logging_setup import get_logger

READ_TIMEOUT_MS = 50
READ_CHUNK = 1024
MIN_REPORT_LENGTH = 1
MAX_REPORT_LENGTH = 1024
CLOSE_WAIT_MS = 2000


@dataclass(frozen=True)
class HidDeviceInfo:
    """一个 HID 接口的枚举结果；同一物理设备可能有多个接口。"""

    path: bytes
    vendor_id: int
    product_id: int
    product: str
    manufacturer: str
    serial: str
    usage_page: int
    usage: int
    interface: int

    @property
    def id_text(self) -> str:
        return f"{self.vendor_id:04X}:{self.product_id:04X}"

    @property
    def usage_text(self) -> str:
        return f"0x{self.usage_page:04X} / 0x{self.usage:04X}"

    @property
    def display_name(self) -> str:
        return self.product.strip() or self.manufacturer.strip() or "HID 设备"


class HidBackend(Protocol):
    """hidapi 适配层协议；测试用假后端替换，不依赖真实硬件。"""

    def enumerate(self) -> list[HidDeviceInfo]: ...
    def open(self, path: bytes) -> object: ...
    def write(self, handle: object, data: bytes) -> int: ...
    def read(self, handle: object, size: int, timeout_ms: int) -> bytes: ...
    def close(self, handle: object) -> None: ...


def describe_device(info: HidDeviceInfo) -> str:
    """状态栏/日志使用的一行设备描述。"""
    return f"{info.display_name} ({info.id_text})"


def build_report(data: bytes, report_id: int = 0, report_length: int = 64) -> bytes:
    """生成一个 Output Report：按需前置报告 ID，再填充/截断到报告长度。

    报告长度是包含报告 ID 的总长度；ID 为 0 表示无编号报告，不加首字节。
    返回值长度始终等于（收敛后的）报告长度，避免 hidapi 写入长度不一致。
    """
    try:
        identifier = int(report_id)
        length = int(report_length)
    except (TypeError, ValueError) as exc:
        raise ValueError("报告 ID 与报告长度必须是整数") from exc
    if not 0 <= identifier <= 0xFF:
        raise ValueError("报告 ID 必须在 0–255 之间")
    length = max(MIN_REPORT_LENGTH, min(MAX_REPORT_LENGTH, length))
    payload_length = length - 1 if identifier else length
    try:
        payload = bytes(data)[:payload_length]
    except (TypeError, ValueError) as exc:
        raise ValueError("发送数据必须是字节串") from exc
    if len(payload) < payload_length:
        payload += b"\x00" * (payload_length - len(payload))
    if identifier:
        return bytes((identifier,)) + payload
    return payload


def _as_path_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return str(value or "").encode("utf-8", "replace")


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _device_from_mapping(item: dict) -> HidDeviceInfo:
    return HidDeviceInfo(
        path=_as_path_bytes(item.get("path")),
        vendor_id=_as_int(item.get("vendor_id")),
        product_id=_as_int(item.get("product_id")),
        product=_as_text(item.get("product_string")),
        manufacturer=_as_text(item.get("manufacturer_string")),
        serial=_as_text(item.get("serial_number")),
        usage_page=_as_int(item.get("usage_page")),
        usage=_as_int(item.get("usage")),
        interface=_as_int(item.get("interface_number")),
    )


class HidapiBackend:
    """hidapi 适配层；延迟导入 `hid`，DLL 缺失时应用仍能启动。"""

    def __init__(self) -> None:
        self._module = None

    def _hid(self):
        if self._module is None:
            import hid  # 延迟导入：缺少 DLL 时只影响 HID 功能

            self._module = hid
        return self._module

    def enumerate(self) -> list[HidDeviceInfo]:
        try:
            raw_devices = self._hid().enumerate()
            devices: list[HidDeviceInfo] = []
            for item in raw_devices:
                info = _device_from_mapping(item)
                if info.path:
                    devices.append(info)
            return devices
        except Exception:
            # 枚举失败按“没有设备”处理；日志保留技术细节供排查。
            get_logger().warning("枚举 USB HID 设备失败，列表按空处理", exc_info=True)
            return []

    def open(self, path: bytes) -> object:
        device = self._hid().device()
        try:
            device.open_path(bytes(path))
        except Exception:
            try:
                device.close()
            except Exception:
                pass
            raise
        return device

    def write(self, handle: object, data: bytes) -> int:
        return int(handle.write(bytes(data)))  # type: ignore[attr-defined]

    def read(self, handle: object, size: int, timeout_ms: int) -> bytes:
        chunk = handle.read(int(size), timeout_ms=int(timeout_ms))  # type: ignore[attr-defined]
        return bytes(chunk) if chunk else b""

    def close(self, handle: object) -> None:
        try:
            handle.close()  # type: ignore[attr-defined]
        except Exception:
            get_logger().exception("关闭 HID 句柄失败")


def list_devices() -> list[HidDeviceInfo]:
    """使用默认 hidapi 后端枚举；任何失败都返回空列表而不是抛异常。"""
    try:
        return list(HidapiBackend().enumerate())
    except Exception:
        get_logger().warning("枚举 USB HID 设备失败", exc_info=True)
        return []


class HidWorker(QThread):
    """单线程 HID 读写：写队列与读循环在同一线程顺序执行。"""

    opened = pyqtSignal(str)
    closed = pyqtSignal()
    data_received = pyqtSignal(bytes)
    error = pyqtSignal(str)

    def __init__(self, backend: HidBackend | None = None, parent=None) -> None:
        super().__init__(parent)
        self._backend = backend if backend is not None else HidapiBackend()
        self._writes: queue.Queue[bytes] = queue.Queue()
        self._stop = threading.Event()
        self._started = threading.Event()
        self._lock = threading.Lock()
        self._handle: object | None = None
        self._opened = False

    # ---- 公开 API -----------------------------------------------------

    def open_device(self, info: HidDeviceInfo) -> None:
        """打开设备并启动读取线程；失败时发 error，不启动线程。"""
        if self.is_open() or self.isRunning():
            self._emit_error("HID 设备已经打开，请先关闭当前设备")
            return
        with self._lock:
            busy = self._handle is not None
        if busy:
            self._emit_error("HID 设备已经打开，请先关闭当前设备")
            return
        try:
            handle = self._backend.open(info.path)
        except Exception as exc:
            self._emit_error(
                f"打开 HID 设备失败：{exc}。请确认设备已连接且未被其他程序占用。"
            )
            return
        self._writes = queue.Queue()
        self._stop.clear()
        self._started.clear()
        with self._lock:
            self._handle = handle
            self._opened = True
        try:
            self.start()
        except Exception as exc:
            self._discard_handle(handle)
            self._emit_error(f"启动 HID 读取线程失败：{exc}")
            return
        self.opened.emit(describe_device(info))

    def close_device(self) -> None:
        """停止读线程并等待句柄关闭；正常由线程自己关闭，避免跨线程操作句柄。"""
        self._stop.set()
        with self._lock:
            opened = self._opened
        # start() 是异步的：刚打开就关闭时先等线程真正进入 run()，避免在
        # 另一线程尚未就绪时直接关闭句柄。
        if opened and not self.isRunning():
            self._started.wait(CLOSE_WAIT_MS / 1000.0)
        if self.isRunning():
            if not self.wait(CLOSE_WAIT_MS):
                get_logger().warning("HID 线程未在 %d ms 内退出", CLOSE_WAIT_MS)
            return
        # 线程从未启动或已经结束：兜底清理可能残留的句柄。
        with self._lock:
            handle = self._handle
            self._handle = None
            self._opened = False
        if handle is not None:
            self._close_handle(handle)
            self.closed.emit()

    def send(
        self, data: bytes, report_id: int = 0, report_length: int = 64
    ) -> bool:
        """把填充/截断后的单个 Output Report 放入写队列。"""
        if not self.is_open():
            self._emit_error("HID 设备未打开，无法发送数据")
            return False
        try:
            report = build_report(data, report_id, report_length)
        except ValueError as exc:
            self._emit_error(f"发送参数无效：{exc}")
            return False
        self._writes.put(report)
        return True

    def is_open(self) -> bool:
        """句柄是否打开；线程启动瞬间也返回 True，避免刚打开就发送被误判。"""
        with self._lock:
            return bool(self._opened)

    # ---- 线程 ---------------------------------------------------------

    def run(self) -> None:
        self._started.set()
        with self._lock:
            handle = self._handle
        try:
            while not self._stop.is_set():
                if not self._drain_writes(handle):
                    break
                try:
                    chunk = self._backend.read(handle, READ_CHUNK, READ_TIMEOUT_MS)
                except Exception as exc:
                    self._emit_error(
                        f"读取 HID Report 失败：{exc}。设备可能已拔出，已自动关闭。"
                    )
                    break
                if chunk:
                    self.data_received.emit(bytes(chunk))
        except Exception as exc:
            self._emit_error(f"HID 读取线程异常：{exc}")
        finally:
            with self._lock:
                self._handle = None
                self._opened = False
            if handle is not None:
                self._close_handle(handle)
            self.closed.emit()

    def _drain_writes(self, handle: object | None) -> bool:
        while True:
            try:
                report = self._writes.get_nowait()
            except queue.Empty:
                return True
            try:
                written = int(self._backend.write(handle, report))  # type: ignore[arg-type]
            except Exception as exc:
                self._emit_error(
                    f"发送 HID Report 失败：{exc}。设备可能已拔出，已自动关闭。"
                )
                return False
            if written < 0:
                self._emit_error("发送 HID Report 失败：设备返回错误，已自动关闭。")
                return False

    # ---- 内部辅助 -----------------------------------------------------

    def _emit_error(self, message: str) -> None:
        get_logger().warning("USB HID：%s", message)
        self.error.emit(message)

    def _close_handle(self, handle: object) -> None:
        try:
            self._backend.close(handle)
        except Exception:
            get_logger().exception("关闭 HID 句柄失败")

    def _discard_handle(self, handle: object) -> None:
        with self._lock:
            self._handle = None
            self._opened = False
        self._close_handle(handle)

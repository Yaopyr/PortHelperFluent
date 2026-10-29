"""经典蓝牙 SPP 的后台工作线程：daemon asyncio 线程 + 注入的 SPP 后端。

和 BLE 侧一样，`SppWorker` 不含蓝牙 I/O：设备枚举、一键配对、RFCOMM 建连都在注入的
`SppBackend` 里，生产实现 `WinRtSppBackend` 用 WinRT Python 投影 3.2.1（懒导入，构造
时不触碰适配器）；COM 备用通道直接复用既有的 `SerialWorker`（QThread）。

经典蓝牙的 COM 口是系统为已配对设备创建的虚拟串口，只有拿到 COM 号才能收发；没配对
成功或驱动没建出口时，就只能走 RFCOMM 直连，所以这两条通道互斥：同一时刻只保留一条，
换通道必须先 `disconnect_device()`。

配对提示是「同步等待」：WinRT 的配对回调在等用户输入 PIN / 确认，所以后端的
prompt 回调会在 worker 线程里阻塞，等界面调用 `answer_prompt()` 唤醒；`pairing_prompt`
信号发出后最多等 `PROMPT_TIMEOUT` 秒，超时按拒绝处理（返回 None），不会卡死线程。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Protocol

from PyQt6.QtCore import QObject, Qt, pyqtSignal

from app.core.logging_setup import get_logger
from app.io.async_worker import DEFAULT_SHUTDOWN_TIMEOUT, AsyncLoopWorker
from app.io.serial_worker import SerialConfig, SerialWorker
from app.services.bluetooth import address_from_device_id, explain_error, pair_status_text

PAIR_TIMEOUT = 30.0
SPP_CONNECT_TIMEOUT = 30.0
TEARDOWN_WAIT = 2.0
READ_CHUNK = 4096
CHANNEL_RFCOMM = "RFCOMM"
PAIR_SUCCESS = frozenset({"PAIRED", "ALREADY_PAIRED"})
ALREADY_PAIRED_TEXT = "已配对，跳过配对"
NOT_CONNECTED_MESSAGE = "经典蓝牙未连接"
SPP_SERVICE_MISSING_MESSAGE = "设备未提供 SPP 串口服务"
NOT_RUNNING_MESSAGE = "经典蓝牙后台线程未运行，请重新扫描"
SERIAL_CLOSED_MESSAGE = "串口已关闭"
SHUTDOWN_REASON = "程序退出"
# 经典蓝牙（BR/EDR）设备的 AEP 协议 ID，用于在设备枚举里只挑经典蓝牙节点。
CLASSIC_BT_AQS = 'System.Devices.Aep.ProtocolId:="{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}"'
STAGE_LABELS: dict[str, str] = {
    "scan": "扫描",
    "pair": "配对",
    "connect": "连接",
    "send": "发送",
    "com": "串口",
}


@dataclass(frozen=True)
class ClassicDeviceInfo:
    """一次枚举里的经典蓝牙设备；`device_id` 是后续配对/建连要用的系统 ID。"""

    name: str
    address: str
    paired: bool
    device_id: str


class SppBackend(Protocol):
    """SPP 后端协议；`stop` 是零参可调用对象，返回 True 表示请求提前结束扫描。"""

    def set_data_callback(self, callback: Callable[[bytes], None]) -> None: ...

    def set_prompt_callback(self, callback: Callable[[str, str], str | None]) -> None: ...

    async def scan(
        self,
        duration: float,
        on_device: Callable[[ClassicDeviceInfo], None],
        stop: Callable[[], bool],
    ) -> None: ...

    async def pair(self, device: ClassicDeviceInfo) -> str: ...

    async def connect(self, device: ClassicDeviceInfo) -> None: ...

    async def send(self, data: bytes) -> int: ...

    async def close(self) -> None: ...


def _to_classic_device(info: Any) -> ClassicDeviceInfo:
    """WinRT `DeviceInformation` → `ClassicDeviceInfo`（地址从设备 ID 里解析）。"""
    device_id = str(getattr(info, "id", "") or "")
    return ClassicDeviceInfo(
        name=str(getattr(info, "name", "") or ""),
        address=address_from_device_id(device_id),
        paired=bool(info.pairing.is_paired),
        device_id=device_id,
    )


def _address_to_int(address: str) -> int | None:
    """`"AA:BB:CC:DD:EE:FF"` → 蓝牙地址整数（`BluetoothDevice` 的入参格式）。"""
    text = str(address or "").replace(":", "").replace("-", "").strip()
    if len(text) != 12:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


async def _winrt_service_from_device_id(device_id: str) -> Any:
    from winrt.windows.devices.bluetooth.rfcomm import RfcommDeviceService

    return await RfcommDeviceService.from_id_async(device_id)


async def _winrt_service_from_address(address_int: int) -> Any:
    from winrt.windows.devices.bluetooth import BluetoothDevice
    from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId

    bt_device = await BluetoothDevice.from_bluetooth_address_async(address_int)
    if bt_device is None:
        return None
    result = await bt_device.get_rfcomm_services_for_id_async(RfcommServiceId.serial_port)
    services = list(getattr(result, "services", None) or [])
    return services[0] if services else None


class WinRtRfcommLocator:
    """拿到设备的 SPP `RfcommDeviceService`。

    扫描 watcher 给的是**设备级 AEP ID**（`DeviceInformationKind.DEVICE`），而
    `RfcommDeviceService.from_id_async` 要的是 `System.Devices.AepService.*` 维度的
    **RFCOMM 服务 ID**：拿设备 ID 直查在多数系统上返回 None 或抛 `OSError E_FAIL`。
    所以先试设备 ID（个别系统能直接命中），失败再按蓝牙地址取本机的 `serial_port` 服务。

    两个 hook 都可以注入，测试用它锁住「先设备 ID、再地址」的顺序，不碰真实 WinRT。
    """

    def __init__(
        self,
        *,
        from_device_id: Callable[[str], Awaitable[Any]] | None = None,
        from_address: Callable[[int], Awaitable[Any]] | None = None,
    ) -> None:
        self._from_device_id = from_device_id or _winrt_service_from_device_id
        self._from_address = from_address or _winrt_service_from_address

    async def locate(self, device: ClassicDeviceInfo) -> Any:
        service = await self._try(self._from_device_id, device.device_id)
        if service is not None:
            return service
        address_int = _address_to_int(device.address)
        if address_int is not None:
            service = await self._try(self._from_address, address_int)
            if service is not None:
                return service
        raise RuntimeError(SPP_SERVICE_MISSING_MESSAGE)

    async def _try(self, factory: Callable[[Any], Awaitable[Any]], argument: Any) -> Any | None:
        try:
            return await factory(argument)
        except Exception:
            # 两条路径都可能以异常表示「这个 ID 上没有服务」，统一当作没找到。
            get_logger().debug("解析 RFCOMM 服务失败：%s", argument, exc_info=True)
            return None


class WinRtSppBackend:
    """WinRT 3.2.1 实现：设备 watcher 扫描、自定义配对、RFCOMM socket 收发。"""

    def __init__(self, locator: WinRtRfcommLocator | None = None) -> None:
        self._data_cb: Callable[[bytes], None] | None = None
        self._prompt_cb: Callable[[str, str], str | None] | None = None
        self._locator = locator if locator is not None else WinRtRfcommLocator()
        self._socket = None
        self._reader = None
        self._reader_task: asyncio.Task | None = None

    def set_data_callback(self, callback: Callable[[bytes], None]) -> None:
        self._data_cb = callback

    def set_prompt_callback(self, callback: Callable[[str, str], str | None]) -> None:
        self._prompt_cb = callback

    # ---- 扫描 ---------------------------------------------------------

    async def scan(
        self,
        duration: float,
        on_device: Callable[[ClassicDeviceInfo], None],
        stop: Callable[[], bool],
    ) -> None:
        from winrt.windows.devices.enumeration import DeviceInformation, DeviceInformationKind

        # pywinrt 把带 kind 的工厂命名为 ..._with_kind_aqs_filter_and_additional_properties。
        watcher = (
            DeviceInformation.create_watcher_with_kind_aqs_filter_and_additional_properties(
                CLASSIC_BT_AQS, [], DeviceInformationKind.DEVICE
            )
        )
        token = watcher.add_added(lambda _sender, info: on_device(_to_classic_device(info)))
        watcher.start()
        try:
            deadline = time.monotonic() + max(0.0, float(duration))
            while time.monotonic() < deadline:
                if stop():
                    break
                await asyncio.sleep(0.1)
        finally:
            watcher.stop()
            watcher.remove_added(token)

    # ---- 一键配对 -----------------------------------------------------

    async def pair(self, device: ClassicDeviceInfo) -> str:
        from winrt.windows.devices.enumeration import DeviceInformation, DevicePairingKinds

        info = await DeviceInformation.create_from_id_async(device.device_id)
        if info is None:
            raise RuntimeError("系统里已找不到该设备，请重新扫描")
        if info.pairing.is_paired:
            return "ALREADY_PAIRED"
        custom = info.pairing.custom
        token = custom.add_pairing_requested(self._on_pairing_requested)
        try:
            result = await custom.pair_async(
                DevicePairingKinds.CONFIRM_ONLY | DevicePairingKinds.PROVIDE_PIN
            )
        finally:
            custom.remove_pairing_requested(token)
        return result.status.name

    def _on_pairing_requested(self, _sender: Any, args: Any) -> None:
        """WinRT 配对回调：按需向界面要 PIN 或确认，拿到答复才 accept。"""
        kind = args.pairing_kind.name
        if kind == "PROVIDE_PIN":
            pin = self._ask("pin", "请输入配对码（PIN）")
            if pin:
                args.accept_with_pin(pin)
            return
        # CONFIRM_ONLY 等其它方式：系统会给出配对码，用户确认即可。
        pin = str(getattr(args, "pin", "") or "")
        text = f"请确认配对码 {pin}" if pin else "请在设备上确认配对"
        if self._ask("confirm", text) is not None:
            args.accept()

    def _ask(self, kind: str, text: str) -> str | None:
        callback = self._prompt_cb
        if callback is None:
            # 没有界面可问就直接拒绝，避免配对一直挂着。
            return None
        return callback(kind, text)

    # ---- RFCOMM 连接与收发 ---------------------------------------------

    async def connect(self, device: ClassicDeviceInfo) -> None:
        from winrt.windows.networking.sockets import StreamSocket
        from winrt.windows.storage.streams import DataReader, InputStreamOptions

        service = await self._locator.locate(device)
        socket = StreamSocket()
        try:
            await socket.connect_async(
                service.connection_host_name, service.connection_service_name
            )
        except BaseException:
            socket.close()
            raise
        self._socket = socket
        reader = DataReader(socket.input_stream)
        reader.input_stream_options = InputStreamOptions.PARTIAL
        self._reader = reader
        self._reader_task = asyncio.create_task(self._read_loop(reader))

    async def _read_loop(self, reader: Any) -> None:
        """把 socket 收到的东西原样交给 data 回调；读到 0 字节表示对端关闭。"""
        try:
            while True:
                size = int(await reader.load_async(READ_CHUNK))
                if size == 0:
                    break
                self._deliver(bytes(reader.read_buffer(size)))
        except asyncio.CancelledError:
            raise
        except Exception:
            # 设备断电/主动断开时 WinRT 会让 load_async 抛 COMError；连接已经断了，
            # 下一次发送会拿到中文错误，这里只记日志，不要把异常抛到界面上。
            get_logger().debug("经典蓝牙读取循环结束", exc_info=True)

    def _deliver(self, data: bytes) -> None:
        callback = self._data_cb
        if callback is not None and data:
            callback(data)

    async def send(self, data: bytes) -> int:
        from winrt.windows.storage.streams import DataWriter

        socket = self._socket
        if socket is None:
            raise RuntimeError(NOT_CONNECTED_MESSAGE)
        writer = DataWriter(socket.output_stream)
        try:
            writer.write_bytes(bytes(data))
            await writer.store_async()
        finally:
            # detach 之后再释放 writer 不会关掉 socket 的输出流，下一条还能发。
            writer.detach_stream()
        return len(data)

    async def close(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                get_logger().debug("等待经典蓝牙读取循环退出时出错", exc_info=True)
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.close()
        socket, self._socket = self._socket, None
        if socket is not None:
            socket.close()


class SppWorker(AsyncLoopWorker):
    """经典蓝牙 SPP 的全部后台动作；界面只通过信号拿结果。"""

    # 类属性，测试用 monkeypatch 缩短；界面线程等答案最多这么久。
    PROMPT_TIMEOUT = 30.0

    scan_started = pyqtSignal(int)  # 秒
    scan_finished = pyqtSignal()
    devices_found = pyqtSignal(object)  # list[ClassicDeviceInfo]（本轮已发现的全集）
    pairing_started = pyqtSignal(str)  # 设备名
    pairing_prompt = pyqtSignal(str, str)  # ("pin"/"confirm", 提示文本)
    pairing_finished = pyqtSignal(str)  # 中文结果
    connected = pyqtSignal(str, str)  # 通道 "RFCOMM"/"COMn", 设备名
    disconnected = pyqtSignal(str)  # 中文原因
    data_received = pyqtSignal(bytes)
    data_sent = pyqtSignal(bytes)

    def __init__(
        self,
        backend: SppBackend | None = None,
        parent: QObject | None = None,
        *,
        serial_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend if backend is not None else WinRtSppBackend()
        self._backend.set_data_callback(self._on_backend_data)
        self._backend.set_prompt_callback(self._prompt)
        self._serial_factory = serial_factory if serial_factory is not None else SerialWorker
        # 通道状态会被界面线程和后台线程同时读写，统一用一把锁守。
        self._state_lock = threading.Lock()
        self._scanning = False
        self._scan_stop = threading.Event()
        self._found: list[ClassicDeviceInfo] = []
        self._connecting = False
        self._connected = False
        self._channel = ""
        self._serial: Any | None = None
        self._silent_close = threading.Event()
        self._async_teardown = threading.Event()
        self._closing = threading.Event()
        self._connect_cancelled = threading.Event()
        self._prompt_lock = threading.Lock()
        self._prompt_value: str | None = None
        self._prompt_ready = threading.Event()

    # ---- 扫描 ---------------------------------------------------------

    def start_scan(self, duration: float = 12.0) -> bool:
        with self._state_lock:
            if self._scanning:
                # 已有扫描在跑：再投一个会让先结束的那个把状态复位，停止按钮失效。
                return False
            self._scanning = True
            self._found = []
        self._scan_stop.clear()
        self.scan_started.emit(max(0, int(duration)))
        if not self._submit_or_report(lambda: self._scan_coroutine(duration)):
            with self._state_lock:
                self._scanning = False
            return False
        return True

    def stop_scan(self) -> bool:
        with self._state_lock:
            if not self._scanning:
                return False
        self._scan_stop.set()
        return True

    def is_scanning(self) -> bool:
        with self._state_lock:
            return self._scanning

    async def _scan_coroutine(self, duration: float) -> None:
        self._stage = "scan"
        try:
            await self._backend.scan(duration, self._on_device, self._scan_stop.is_set)
        finally:
            with self._state_lock:
                self._scanning = False
            self.scan_finished.emit()

    def _on_device(self, info: ClassicDeviceInfo) -> None:
        with self._state_lock:
            found = self._found
            if any(item.device_id == info.device_id for item in found):
                return
            found.append(info)
            snapshot = list(found)
        self.devices_found.emit(snapshot)

    # ---- 一键配对 + RFCOMM 连接 ----------------------------------------

    def connect_device(self, device: ClassicDeviceInfo) -> bool:
        with self._state_lock:
            if self._connecting or self._connected or self._channel or self._serial is not None:
                # 连接在飞或已有通道：再连会留下第二条泄漏连接。
                return False
            self._connect_cancelled.clear()
            self._connecting = True
        if not self._submit_or_report(lambda: self._connect_coroutine(device)):
            with self._state_lock:
                self._connecting = False
            return False
        return True

    async def _connect_coroutine(self, device: ClassicDeviceInfo) -> None:
        try:
            self._stage = "pair"
            self.pairing_started.emit(device.name)
            status = "ALREADY_PAIRED"
            if not device.paired:
                status = await self._pair_device(device)
                if status not in PAIR_SUCCESS:
                    return
            self.pairing_finished.emit("配对成功" if status == "PAIRED" else ALREADY_PAIRED_TEXT)

            self._stage = "connect"
            try:
                await asyncio.wait_for(self._backend.connect(device), SPP_CONNECT_TIMEOUT)
            except TimeoutError:
                self.error.emit(explain_error("connect", "连接超时"))
                return
            if self._closing.is_set() or self._connect_cancelled.is_set():
                # 关停开始、或用户在连接中点过断开：这条连接建得太晚，直接关掉，
                # 既不置通道状态也不发 connected，界面不会出现「断开后又连上」。
                await self._backend.close()
                return
            with self._state_lock:
                self._connected = True
                self._channel = CHANNEL_RFCOMM
            self.connected.emit(CHANNEL_RFCOMM, device.name)
        finally:
            with self._state_lock:
                self._connecting = False
            self._connect_cancelled.clear()

    async def _pair_device(self, device: ClassicDeviceInfo) -> str:
        """跑一次配对；失败文案在返回前就发出去，返回的状态不成功时调用方直接收尾。"""
        try:
            # 预算要把用户输入 PIN 的时间算进去，否则第 25 秒才答完就只剩几秒握手。
            status = await asyncio.wait_for(
                self._backend.pair(device), PAIR_TIMEOUT + float(self.PROMPT_TIMEOUT)
            )
        except TimeoutError:
            self.pairing_finished.emit("配对超时，请重试")
            self.error.emit(explain_error("pair", "配对超时"))
            return ""
        except Exception as exc:
            # 后端自己抛异常（例如系统里已找不到该设备）也必须收尾配对状态，
            # 否则界面会一直停在「配对中」等一个永远不来的结果。
            self.pairing_finished.emit("配对失败")
            self.error.emit(self._describe_error(exc))
            return ""
        if status in PAIR_SUCCESS:
            return status
        text = pair_status_text(status)
        self.pairing_finished.emit(text)
        self.error.emit(explain_error("pair", text))
        return status

    # ---- 配对提示（同步等待界面答复）-----------------------------------

    def _prompt(self, kind: str, text: str) -> str | None:
        """后端在配对回调里同步调用：发提示、等 `answer_prompt()`，超时按拒绝。"""
        if self._closing.is_set():
            # 已开始关停：界面不会再答复，直接按拒绝处理，别让后台线程再等一整轮超时。
            return None
        with self._prompt_lock:
            self._prompt_value = None
            self._prompt_ready.clear()
        self.pairing_prompt.emit(kind, text)
        if not self._prompt_ready.wait(max(0.0, float(self.PROMPT_TIMEOUT))):
            return None
        with self._prompt_lock:
            return self._prompt_value

    def answer_prompt(self, value: str | None) -> None:
        with self._prompt_lock:
            self._prompt_value = value
        self._prompt_ready.set()

    # ---- COM 备用通道 --------------------------------------------------

    def open_com(self, config: SerialConfig) -> bool:
        with self._state_lock:
            if self._connecting or self._connected or self._channel or self._serial is not None:
                return False
            serial = self._serial_factory()
            # 先占住通道号，防止并发点两次开出两条串口。
            self._serial = serial
            self._channel = config.port
        port = config.port
        # 转发必须和发信号同一个线程：回调用普通 callable + DirectConnection，
        # 否则会被排到界面事件循环，界面卡住时数据就积压了。
        serial.opened.connect(
            lambda: self._on_serial_opened(port), type=Qt.ConnectionType.DirectConnection
        )
        serial.closed.connect(
            lambda: self._on_serial_closed(), type=Qt.ConnectionType.DirectConnection
        )
        serial.data_received.connect(
            lambda data: self.data_received.emit(bytes(data)),
            type=Qt.ConnectionType.DirectConnection,
        )
        serial.data_sent.connect(
            lambda data: self.data_sent.emit(bytes(data)), type=Qt.ConnectionType.DirectConnection
        )
        serial.error.connect(
            lambda text: self.error.emit(str(text)), type=Qt.ConnectionType.DirectConnection
        )
        self._stage = "com"
        try:
            opened = bool(serial.open_port(config))
        except Exception as exc:
            opened = False
            self.error.emit(self._describe_error(exc))
        if not opened:
            with self._state_lock:
                self._serial = None
                self._channel = ""
            return False
        return True

    def _on_serial_opened(self, port: str) -> None:
        with self._state_lock:
            self._connected = True
            if not self._channel:
                self._channel = port
        self.connected.emit(port, port)

    def _on_serial_closed(self) -> None:
        if self._silent_close.is_set():
            # 主动断开：原因由 `disconnect_device()` 统一发，避免一条断开两条文案。
            return
        with self._state_lock:
            self._connected = False
            self._channel = ""
            self._serial = None
        self.disconnected.emit(SERIAL_CLOSED_MESSAGE)

    # ---- 断开、发送与状态 ----------------------------------------------

    def disconnect_device(self, reason: str = "用户断开") -> bool:
        with self._state_lock:
            channel, self._channel = self._channel, ""
            serial, self._serial = self._serial, None
            self._connected = False
            connecting = self._connecting
        if serial is not None:
            self._close_serial(serial)
            self.disconnected.emit(reason)
            return True
        if not channel:
            if connecting:
                # 连接还在飞：挂上取消标志，连接协程建好 socket 后会自己关掉，
                # 否则「断开」返回 True 之后连接照样建立。
                self._connect_cancelled.set()
            # 幂等：没有通道也安全返回，且不发 disconnected。
            return True
        self._async_teardown.clear()
        if not self._submit_or_report(lambda: self._disconnect_coroutine(reason)):
            return False
        return True

    async def _disconnect_coroutine(self, reason: str) -> None:
        self._stage = "connect"
        try:
            await self._backend.close()
        finally:
            self._async_teardown.set()
            with self._state_lock:
                self._connected = False
                self._channel = ""
        self.disconnected.emit(reason)

    def _close_serial(self, serial: Any) -> None:
        """串口和串口页一样在调用线程里同步关闭，不把等待 2 秒的活留到界面之外。"""
        self._silent_close.set()
        try:
            serial.close_port()
        except Exception:
            get_logger().exception("关闭经典蓝牙串口失败")
        finally:
            self._silent_close.clear()

    def send(self, data: bytes) -> bool:
        payload = bytes(data)
        with self._state_lock:
            channel = self._channel
            serial = self._serial
        if channel == CHANNEL_RFCOMM:
            return self._submit_or_report(lambda: self._send_coroutine(payload))
        if serial is not None:
            return bool(serial.send(payload))
        self.error.emit(NOT_CONNECTED_MESSAGE)
        return False

    async def _send_coroutine(self, data: bytes) -> None:
        self._stage = "send"
        written = int(await self._backend.send(data))
        if written:
            self.data_sent.emit(data[:written])

    def is_connected(self) -> bool:
        with self._state_lock:
            return bool(self._connected)

    def channel(self) -> str:
        with self._state_lock:
            return self._channel

    # ---- 生命周期 -----------------------------------------------------

    def shutdown(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        # 正在等答复的配对提示先按拒绝放行，否则后台线程会一直被 prompt 卡住。
        self._closing.set()
        self.answer_prompt(None)
        with self._state_lock:
            closing_rfcomm = self._channel == CHANNEL_RFCOMM
        # 断开要赶在 loop 停掉之前完成：RFCOMM 的 close() 是协程，COM 是同步关串口。
        if self.disconnect_device(SHUTDOWN_REASON) and closing_rfcomm:
            self._async_teardown.wait(TEARDOWN_WAIT)
        super().shutdown(timeout)

    # ---- 内部辅助 -----------------------------------------------------

    def _on_backend_data(self, data: bytes) -> None:
        self.data_received.emit(bytes(data))

    def _submit_or_report(self, factory: Callable[[], Coroutine[Any, Any, None]]) -> bool:
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
        return f"{STAGE_LABELS.get(self._stage, '经典蓝牙')}操作失败"

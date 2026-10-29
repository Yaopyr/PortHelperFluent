# 蓝牙调试页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 PortHelper Fluent 增加一页「蓝牙调试」，同一页内以分段控件切换 BLE 与经典蓝牙 SPP，完成扫描、一键配对连接、读写/通知、日志与发送。

**Architecture:** 三层：`app/services/bluetooth.py` 只放无 Qt 的纯函数（UUID 名称表、属性徽章、排序过滤、MTU 分片、COM 识别、PIN 校验、状态文案）；`app/io/ble_worker.py` 与 `app/io/spp_worker.py` 各自是 daemon 线程 + 私有 asyncio loop 的 `AsyncLoopWorker` 子类，通过注入的后端对象（bleak / WinRT）干活，结果只用 Qt 信号回传 UI；`app/ui/pages/bluetooth_page.py` 只做界面与状态编排，左右两列沿用串口页结构，BLE/SPP 面板放在左列的 `QStackedWidget` 里，右列日志与发送区共用。

**Tech Stack:** PyQt6 6.11、PyQt6-Fluent-Widgets 1.11.2、bleak 3.0.2（WinRT 后端）、WinRT Python 投影 3.2.1（`winrt-Windows.Devices.Bluetooth.Rfcomm` / `.Networking` / `.Networking.Sockets`）、pyserial 3.5（复用 `SerialWorker`）。

**Spec:** `docs/superpowers/specs/2026-10-29-bluetooth-debug-design.md`

## Global Constraints

- 只支持 Windows；BLE 只经 bleak 的 WinRT 后端，SPP 只经 WinRT RFCOMM（COM 口为内置回退）。
- 新增后台任务必须用 **daemon `threading.Thread` + `QObject` 信号**，不用 `QThread`（`app/io/update_worker.py` 的教训：阻塞调用无法被 `requestInterruption()` 打断，QThread 退出会 abort）。
- 所有 bleak / WinRT 调用只在 worker 线程的 asyncio loop 内执行；UI 线程只用 `loop.call_soon_threadsafe` 下发命令，只通过 Qt 信号接收结果。
- 页面必须满足 `app/ui/widgets/page_layout.py`：`apply_page_minimum(page)`、左列 `scroll_container(..., min_width=PANEL_MIN_WIDTH)`，`tests/test_ui_layout.py` 会自动纳入新路由。
- 同一时间只保留一条活动连接；切换模式先断开当前连接（日志写「已断开 …：切换模式」）。
- 打开页面不自动扫描、不自动连接；`bluetooth.auto_connect` 默认 `false`。
- 配置键统一 `bluetooth.*`，全部写进 `app/core/settings.py` 的 `DEFAULT_SETTINGS`。
- 新依赖必须进 `requirements.txt` 并保证 PyInstaller onefile 可用：`bleak==3.0.2`、`winrt-Windows.Devices.Bluetooth.Rfcomm==3.2.1`、`winrt-Windows.Networking==3.2.1`、`winrt-Windows.Networking.Sockets==3.2.1`。
- 测试绝不碰真机/真实蓝牙/网络：worker 测试注入假 backend，页面测试注入假 worker；测试环境无蓝牙适配器也必须全绿。
- 测试基线 683 passed；每任务提交前跑 `python -m pytest`，新测试计入后同步文档里的计数。
- 所有面向用户的失败提示为中文且可执行（说明下一步怎么做），不允许只写日志。
- 仓库无 git 用户配置，提交统一用 `git -c user.name="Yaopyr" -c user.email="113556181@qq.com" commit ...`；只提交本任务涉及文件。

## Review Focus

- **无适配器 / 系统蓝牙关闭**：点扫描或连接必须得到具体中文原因（而不是转圈或崩溃），且按钮回到可点状态。→ Task 2/3 的 `test_backend_exception_emits_error_and_disconnect_reason`、`test_pair_failure_maps_chinese_status` 与 Task 4 的 `test_bluetooth_error_shows_reason_and_settings_button` 覆盖。
- **连接中途设备消失 / 对端主动断开**：必须收敛成一条 `disconnected(reason)`，日志与状态行一致，发送区自动禁用。→ Task 2 的 `test_backend_disconnect_callback_emits_reason`、Task 4 的 `test_disconnect_reason_updates_status_and_buttons`。
- **扫描/配对/文件发送过程中切模式或关窗口**：必须停掉线程、释放 socket/串口，`shutdown()` 可重复调用不报错。→ Task 2 的 `test_shutdown_is_idempotent_and_joins_thread`、Task 3 的 `test_shutdown_closes_socket_and_serial`、Task 4 的 `test_stop_workers_shuts_down_active_worker`。
- **不可写特征值**：没有选中可写特征值时发送按钮必须禁用并显示「写入目标：未选择」，不能被点了才抛异常。→ Task 4 的 `test_send_disabled_without_writable_characteristic`。
- **空通知风暴**：默认忽略空包（`bluetooth.ignore_empty_notify=true`），空包只累计计数不写日志；非空通知按 UUID 前缀标注来源。→ Task 4 的 `test_notification_goes_to_log_and_ignores_empty`。

---

### Task 1: 蓝牙纯函数服务

**Files:**
- Create: `app/services/bluetooth.py`
- Test: `tests/test_bluetooth_service.py`

**Interfaces:**
- Consumes: 无（只依赖标准库）。
- Produces（后续任务按此签名调用）:
  - `GATT_BASE_SUFFIX: str = "-0000-1000-8000-00805f9b34fb"`
  - `expand_uuid(value: str) -> str`
  - `short_uuid(value: str) -> str`
  - `service_label(uuid: str) -> str`
  - `characteristic_label(uuid: str) -> str`
  - `properties_badges(properties: Sequence[str]) -> tuple[str, ...]`
  - `is_readable(properties: Sequence[str]) -> bool`、`is_writable(...)`、`is_notifiable(...)`
  - `chunk_bytes(data: bytes, mtu: int) -> list[bytes]`
  - `sort_devices(devices: Sequence[dict], mode: str, favorites: Sequence[str] = ()) -> list[dict]`
  - `filter_devices(devices: Sequence[dict], query: str, only_named: bool = False, only_paired: bool = False) -> list[dict]`
  - `is_bluetooth_port(device: str, description: str, hwid: str) -> bool`
  - `format_rssi(rssi: int | None) -> str`、`rssi_quality(rssi: int | None) -> str`
  - `validate_pin(text: str) -> str | None`
  - `pair_status_text(status_name: str) -> str`
  - `explain_error(stage: str, message: str) -> str`（stage ∈ `"scan"`/`"connect"`/`"pair"`/`"write"`）
  - `address_from_device_id(device_id: str) -> str`
  - `KNOWN_SERVICES`、`KNOWN_CHARACTERISTICS`（`dict[str, str]`，键为小写完整 UUID）

- [ ] **Step 1: 写失败测试**

`tests/test_bluetooth_service.py`（纯 pytest，无 Qt）：

```python
def test_expand_uuid_accepts_short_and_full_forms():
    assert expand_uuid("FFE0") == "0000ffe0-0000-1000-8000-00805f9b34fb"
    assert expand_uuid("0000FFE0-0000-1000-8000-00805F9B34FB") == "0000ffe0-0000-1000-8000-00805f9b34fb"
    assert expand_uuid("bad") == "bad"

def test_short_uuid_keeps_short_form_and_passes_through_vendor_128bit():
    assert short_uuid("0000ffe1-0000-1000-8000-00805f9b34fb") == "FFE1"
    assert short_uuid("6e400001-b5a3-f393-e0a9-e50e24dcca9e") == "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"

def test_service_label_marks_known_and_custom_services():
    assert service_label("0000ffe0-0000-1000-8000-00805f9b34fb") == "FFE0（自定义透传）"
    assert service_label("0000180f-0000-1000-8000-00805f9b34fb") == "180F（电池服务）"
    assert service_label("1234abcd-b5a3-f393-e0a9-e50e24dcca9e").endswith("（自定义）")

def test_properties_badges_maps_known_bits_in_fixed_order():
    assert properties_badges(["notify", "read", "write-without-response", "write", "indicate", "bogus"]) == (
        "Read", "Write", "WriteNoRsp", "Notify", "Indicate")

def test_capability_predicates():
    assert is_readable(["read"]) and not is_readable(["notify"])
    assert is_writable(["write-without-response"]) and not is_writable(["read"])
    assert is_notifiable(["indicate"]) and not is_notifiable(["write"])

def test_chunk_bytes_respects_mtu_minus_three():
    assert chunk_bytes(b"abcdefgh", 23) == [b"abcdefgh"[:20]]
    assert [len(c) for c in chunk_bytes(b"x" * 45, 23)] == [20, 20, 5]
    assert chunk_bytes(b"abc", 3) == [b"a", b"b", b"c"]
    assert chunk_bytes(b"", 23) == []

def test_sort_devices_puts_favorites_first_then_mode_key():
    devices = [{"address": "B", "name": "beta", "rssi": -80},
               {"address": "A", "name": "Alpha", "rssi": -40},
               {"address": "C", "name": "", "rssi": None}]
    assert [d["address"] for d in sort_devices(devices, "rssi")] == ["A", "B", "C"]
    assert [d["address"] for d in sort_devices(devices, "name")] == ["A", "B", "C"]
    assert [d["address"] for d in sort_devices(devices, "recent")] == ["B", "A", "C"]
    assert sort_devices(devices, "rssi", favorites=["B"])[0]["address"] == "B"
    assert sort_devices(devices, "bogus")[0]["address"] == "B"

def test_filter_devices_by_text_name_and_pairing_state():
    devices = [{"address": "AA:BB", "name": "HC-05", "paired": True},
               {"address": "CC:DD", "name": "", "paired": False}]
    assert [d["address"] for d in filter_devices(devices, "hc")] == ["AA:BB"]
    assert [d["address"] for d in filter_devices(devices, "aa:bb")] == ["AA:BB"]
    assert [d["address"] for d in filter_devices(devices, "", only_named=True)] == ["AA:BB"]
    assert [d["address"] for d in filter_devices(devices, "", only_paired=True)] == ["AA:BB"]
    assert len(filter_devices(devices, "")) == 2

def test_is_bluetooth_port_matches_driver_and_description():
    assert is_bluetooth_port("COM7", "蓝牙链接上的标准串行", "BTHENUM\\{00001101}")
    assert is_bluetooth_port("COM8", "Standard Serial over Bluetooth link", "")
    assert not is_bluetooth_port("COM3", "USB-SERIAL CH340", "USB VID:PID=1A86:7523")

def test_validate_pin_accepts_digits_and_rejects_bad_input():
    assert validate_pin("1234") is None
    assert validate_pin("") is not None
    assert validate_pin("abcd") is not None
    assert validate_pin("1" * 17) is not None

def test_pair_status_text_and_explain_error_are_chinese():
    assert pair_status_text("PAIRED") == "配对成功"
    assert "PIN" in pair_status_text("AUTHENTICATION_FAILURE")
    assert pair_status_text("SOMETHING_NEW").startswith("配对失败")
    assert "广播范围" in explain_error("scan", "扫描超时")
    assert explain_error("connect", "连接失败") == "连接失败（确认设备未被其他程序占用，且已开机）"

def test_address_from_device_id_extracts_mac():
    assert address_from_device_id("Bluetooth#Bluetooth00:1A:7D:DA:71:13-4c:5e:0c:11:22:33") == "00:1A:7D:DA:71:13"
    assert address_from_device_id("no-mac-here") == "no-mac-here"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_bluetooth_service.py -v`
Expected: `ModuleNotFoundError: No module named 'app.services.bluetooth'`

- [ ] **Step 3: 实现 `app/services/bluetooth.py`**

模块 docstring 写明「无 Qt、无蓝牙 I/O 的纯函数」；常量与函数只按上面的签名实现。`KNOWN_SERVICES` 至少含 `1800 通用访问`、`1801 通用属性`、`180a 设备信息`、`180f 电池服务`、`180d 心率服务`、`1812 HID`、`ffe0 自定义透传`、`fff0 自定义透传`；`KNOWN_CHARACTERISTICS` 至少含 `2a00 设备名称`、`2a19 电量`、`2a29 制造商`、`ffe1 FFE0 数据`、`fff1 FFF0 数据`、`fff2 FFF0 数据`。

实现要点（签名与测试之外只剩这些选择）：`sort_devices` 先按 `address in favorites` 分桶再按模式排序，"rssi" 用 `-(rssi or -999)`（`None` 排最后）、"name" 用 `(未命名垫底, name.casefold())`、未知模式等同 "recent"（稳定保持入参顺序）——以上以 Step 1 的测试断言为准（2026-10-29 裁决）；`explain_error` 用 `{"scan": "确认设备在广播范围内，且系统蓝牙已打开", "connect": "确认设备未被其他程序占用，且已开机", "pair": "先让设备进入配对模式（指示灯快闪）", "write": "确认该特征值支持写入"}` 拼接 `f"{message}（{hint}）"`，`message` 为空时只返回 hint。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_bluetooth_service.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 全量回归并提交**

Run: `python -m pytest` → Expected: 683 + 新增全部通过

```bash
git add app/services/bluetooth.py tests/test_bluetooth_service.py
git -c user.name="Yaopyr" -c user.email="113556181@qq.com" commit -m "feat: add bluetooth pure-function service layer"
```

---

### Task 2: asyncio worker 基类与 BLE 工作线程

**Files:**
- Create: `app/io/async_worker.py`
- Create: `app/io/ble_worker.py`
- Test: `tests/test_ble_worker.py`

**Interfaces:**
- Consumes: `app.services.bluetooth`（Task 1 的 `expand_uuid`/`properties_badges`/`explain_error`/`is_writable`）。
- Produces:
  - `app/io/async_worker.py`：
    ```python
    class AsyncLoopWorker(QObject):
        error = pyqtSignal(str)
        finished = pyqtSignal()
        def __init__(self, parent: QObject | None = None) -> None
        def start(self) -> None                       # 起 daemon 线程 + asyncio loop，可重复调用
        def submit(self, factory: Callable[[], Coroutine[Any, Any, None]]) -> bool
        def shutdown(self, timeout: float = 3.0) -> None   # 幂等
        def _describe_error(self, exc: BaseException) -> str
    ```
  - `app/io/ble_worker.py`：
    ```python
    @dataclass(frozen=True)
    class BleDeviceInfo:  name: str; address: str; rssi: int | None; seen_at: float
    @dataclass(frozen=True)
    class BleCharInfo:    uuid: str; properties: tuple[str, ...]
    @dataclass(frozen=True)
    class BleServiceInfo: uuid: str; characteristics: tuple[BleCharInfo, ...]

    class BleBackend(Protocol):
        def set_disconnect_callback(self, callback: Callable[[str], None]) -> None: ...
        async def scan(self, duration: float, on_device: Callable[[BleDeviceInfo], None], stop: Callable[[], bool]) -> None: ...
        async def connect(self, address: str) -> None: ...
        async def disconnect(self) -> None: ...
        async def services(self) -> list[BleServiceInfo]: ...
        def mtu(self) -> int: ...
        async def read(self, char_uuid: str) -> bytes: ...
        async def write(self, char_uuid: str, data: bytes, response: bool) -> None: ...
        async def set_notify(self, char_uuid: str, enabled: bool, on_data: Callable[[str, bytes], None]) -> None: ...

    class BleakBackend:            # 生产实现，构造时不联网
        def __init__(self) -> None

    class BleWorker(AsyncLoopWorker):
        scan_started = pyqtSignal(int)        # 秒
        scan_finished = pyqtSignal()
        device_found = pyqtSignal(object)     # BleDeviceInfo
        connected = pyqtSignal(str, int)      # address, mtu
        disconnected = pyqtSignal(str)        # 中文原因
        services_ready = pyqtSignal(object)   # list[BleServiceInfo]
        notification = pyqtSignal(str, bytes)
        read_result = pyqtSignal(str, bytes)
        write_finished = pyqtSignal(str, int)
        mtu_changed = pyqtSignal(int)
        rssi_changed = pyqtSignal(str, object)
        def __init__(self, backend: BleBackend | None = None, parent: QObject | None = None) -> None
        def start_scan(self, duration: float = 10.0) -> bool
        def stop_scan(self) -> bool
        def connect_device(self, address: str) -> bool
        def disconnect_device(self, reason: str = "用户断开") -> bool
        def read_char(self, char_uuid: str) -> bool
        def write_char(self, char_uuid: str, data: bytes, response: bool = True) -> bool
        def subscribe(self, char_uuid: str) -> bool
        def unsubscribe(self, char_uuid: str) -> bool
        def refresh_mtu(self) -> bool
        def is_connected(self) -> bool
        def is_scanning(self) -> bool
    ```

- [ ] **Step 1: 写失败测试**

`tests/test_ble_worker.py` 用假 backend（纯 Python 类，异步方法记录调用、按脚本回调），起真线程，用 `threading.Event` 等信号；**不要**用 Qt 事件循环等信号——连接的是普通 lambda，AutoConnection 会在 worker 线程直接调用：

```python
class FakeBleBackend:
    def __init__(self):
        self.calls = []
        self.devices = []
        self.services = [BleServiceInfo("0000ffe0-0000-1000-8000-00805f9b34fb", (BleCharInfo("0000ffe1-0000-1000-8000-00805f9b34fb", ("read", "write", "notify")),))]
        self.mtu_value = 23
        self.fail_on = None
        self.disconnect_cb = None
        self.notify_cb = None
        self.stop_flag = False
    def set_disconnect_callback(self, cb): self.disconnect_cb = cb
    async def scan(self, duration, on_device, stop):
        if self.fail_on == "scan": raise RuntimeError("蓝牙关闭")
        for info in self.devices: on_device(info)
        await asyncio.sleep(0)
    def mtu(self): return self.mtu_value

def test_scan_emits_devices_and_finishes(qapp):
    ...
    worker.device_found.connect(found.append); worker.scan_finished.connect(done.set)
    assert worker.start_scan(1) is True
    assert done.wait(3); assert [d.address for d in found] == ["AA:BB:CC:DD:EE:FF"]

def test_connect_emits_connected_mtu_and_services(qapp): ...
def test_read_write_and_notify_roundtrip(qapp): ...   # 断言 read_result/notification/write_finished 值
def test_backend_disconnect_callback_emits_reason(qapp): ...  # backend.disconnect_cb("设备主动断开连接")
def test_backend_exception_emits_error_and_disconnect_reason(qapp): ...  # fail_on="connect" → error 含 "连接"
def test_shutdown_is_idempotent_and_joins_thread(qapp): ...   # 两次 shutdown()，断言 not worker.thread_alive()
def test_submit_after_shutdown_returns_false(qapp): ...       # shutdown 后 start_scan() is False，不抛异常
```

另加一条纯逻辑测试 `test_chunking_uses_mtu`（在页面任务里做，见 Task 4），本任务只保证 `mtu_changed` 在 `refresh_mtu()` 后发出新值。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_ble_worker.py -v`
Expected: `ModuleNotFoundError: No module named 'app.io.ble_worker'`

- [ ] **Step 3: 实现 `app/io/async_worker.py` 与 `app/io/ble_worker.py`**

`AsyncLoopWorker` 要点：`start()` 建 `threading.Thread(target=self._run_loop, daemon=True, name="bluetooth-async")`；`_run_loop` 内 `asyncio.new_event_loop()` + `asyncio.set_event_loop()` + `loop.run_forever()`，退出后关 loop；`submit()` 用 `loop.call_soon_threadsafe(lambda: loop.create_task(self._guard(factory())))`，loop 未起或已停返回 `False`；`_guard` 捕获 `Exception` → `self.error.emit(self._describe_error(exc))`，捕获 `asyncio.CancelledError` 时静默；`shutdown()` 先 `_cancel_tasks()`（`asyncio.all_tasks(loop)` 逐个 `cancel()`，`call_soon_threadsafe` 投递）、再 `loop.call_soon_threadsafe(loop.stop)`、`thread.join(timeout)`，最后 `finished.emit()`，重复调用直接返回。

`BleakBackend` 要点（照已核对的 bleak 3.0.2 API）：`scan()` 用 `BleakScanner(detection_callback=lambda device, adv: on_device(BleDeviceInfo(adv.local_name or device.name or "", device.address, adv.rssi, time.monotonic())))`，`await scanner.start()` 后以 0.1 s 步进 `asyncio.sleep` 直到 `duration` 或 `stop()` 为真，`finally: await scanner.stop()`；`connect()` 用 `BleakClient(address, disconnected_callback=lambda _c: self._disconnect_cb and self._disconnect_cb("设备主动断开连接"))` + `await client.connect()`；`services()` 遍历 `client.services` 与 `service.characteristics` 组装 `BleServiceInfo`（`tuple(char.properties)` 原样保留，交 Task 1 的 `properties_badges` 转中文徽章）；`mtu()` 返回 `int(client.mtu_size)`（Windows 只读，系统协商值）；`read`/`write`/`set_notify` 走 `read_gatt_char`/`write_gatt_char(..., response=response)`/`start_notify`/`stop_notify`，通知回调转成 `on_data(char_uuid, bytes(data))`。不提供 `request_mtu`/`get_rssi`（Windows 后端没有），`rssi_changed` 只在扫描期间发。

`BleWorker` 要点：`start_scan` 先发 `scan_started(int(duration))`、置 `_scanning=True`；扫描协程结束时发 `scan_finished`；`connect_device` 用 `asyncio.wait_for(backend.connect(address), 30)` 包裹，超时发 `error(explain_error("connect", "连接超时"))`，成功后 `await backend.services()` 发 `services_ready` 并发 `connected(address, backend.mtu())`、`mtu_changed(mtu)`；`disconnect_device` 调 `backend.disconnect()` 后发 `disconnected(reason)`；`submit` 返回 False 时发 `error("蓝牙后台线程未运行，请重新扫描")`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_ble_worker.py -v` → 全部 PASS；再 `python -m pytest` 全绿。

- [ ] **Step 5: 提交**

```bash
git add app/io/async_worker.py app/io/ble_worker.py tests/test_ble_worker.py
git -c user.name="Yaopyr" -c user.email="113556181@qq.com" commit -m "feat: add BLE worker on daemon asyncio thread"
```

---

### Task 3: 经典蓝牙 SPP 工作线程（一键配对 + RFCOMM + COM 回退）

**Files:**
- Create: `app/io/spp_worker.py`
- Test: `tests/test_spp_worker.py`

**Interfaces:**
- Consumes: Task 2 的 `AsyncLoopWorker`；`app.services.bluetooth.pair_status_text/explain_error/address_from_device_id`；`app.io.serial_worker.SerialConfig/SerialWorker`（`open_port`/`close_port`/`send`/`is_open` 与 `opened`/`closed`/`data_received`/`data_sent`/`error` 信号）。
- Produces:
  ```python
  @dataclass(frozen=True)
  class ClassicDeviceInfo:
      name: str; address: str; paired: bool; device_id: str

  class SppBackend(Protocol):
      def set_data_callback(self, callback: Callable[[bytes], None]) -> None: ...
      def set_prompt_callback(self, callback: Callable[[str, str], str | None]) -> None: ...
      async def scan(self, duration: float, on_device: Callable[[ClassicDeviceInfo], None], stop: Callable[[], bool]) -> None: ...
      async def pair(self, device: ClassicDeviceInfo) -> str: ...      # 返回状态名，如 "PAIRED"
      async def connect(self, device: ClassicDeviceInfo) -> None: ...
      async def send(self, data: bytes) -> int: ...
      async def close(self) -> None: ...

  class WinRtSppBackend:      # 生产实现
      def __init__(self) -> None

  class SppWorker(AsyncLoopWorker):
      scan_started = pyqtSignal(int)
      scan_finished = pyqtSignal()
      devices_found = pyqtSignal(object)      # list[ClassicDeviceInfo]
      pairing_started = pyqtSignal(str)       # 设备名
      pairing_prompt = pyqtSignal(str, str)   # ("pin"/"confirm", 提示文本)
      pairing_finished = pyqtSignal(str)      # 中文结果
      connected = pyqtSignal(str, str)        # 通道 "RFCOMM"/"COMn", 设备名
      disconnected = pyqtSignal(str)
      data_received = pyqtSignal(bytes)
      data_sent = pyqtSignal(bytes)
      def __init__(self, backend: SppBackend | None = None, parent=None, *, serial_factory=None) -> None
      def start_scan(self, duration: float = 12.0) -> bool
      def stop_scan(self) -> bool
      def connect_device(self, device: ClassicDeviceInfo) -> bool
      def open_com(self, config: SerialConfig) -> bool
      def answer_prompt(self, value: str | None) -> None
      def disconnect_device(self, reason: str = "用户断开") -> bool
      def send(self, data: bytes) -> bool
      def is_connected(self) -> bool
      def channel(self) -> str            # "" / "RFCOMM" / "COMn"
  ```

- [ ] **Step 1: 写失败测试**

`tests/test_spp_worker.py`：假 backend + 假 serial（`open_port` 记录并立即 `opened.emit()`，`send` 返回写入长度），起真线程，`threading.Event` 等信号：

```python
def test_scan_emits_paired_and_unpaired_devices(qapp): ...
def test_one_click_pairs_then_connects_rfcomm(qapp):
    # 未配对设备 → 先 pair()，再 connect()，connected 通道为 "RFCOMM"
def test_already_paired_skips_pairing(qapp):
    # device.paired=True → backend.calls 不含 "pair"
def test_pairing_prompt_pin_resumes_pairing(qapp):
    # backend.pair 内调用 prompt("pin", "1234") 阻塞；测试收到 pairing_prompt 后 worker.answer_prompt("4321")
    assert "4321" == received_pin
def test_pair_prompt_timeout_returns_none(monkeypatch, qapp): ...   # 把超时压到 0.2 s，answer 不调用
def test_pair_failure_maps_chinese_status(qapp):
    # pair 返回 "AUTHENTICATION_FAILURE" → pairing_finished 文案含 "PIN"，且不进入 connect
def test_rfcomm_data_received_and_sent(qapp): ...
def test_com_channel_bridges_serial_worker_signals(qapp):
    # open_com(SerialConfig("COM7", 115200)) → connected("COM7", "COM7")；串口 data_received 转发到 data_received
def test_disconnect_reason_emitted_once(qapp): ...
def test_shutdown_closes_socket_and_serial(qapp):
    # RFCOMM 与 COM 两条路径都建连后 shutdown()，断言 backend.close 与 serial.close_port 均被调用
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_spp_worker.py -v`
Expected: `ModuleNotFoundError: No module named 'app.io.spp_worker'`

- [ ] **Step 3: 实现 `app/io/spp_worker.py`**

`WinRtSppBackend` 要点（API 已随包安装核对过）：
- 扫描：`DeviceInformation.create_watcher_with_kind_aqs_filter(DeviceInformationKind.DEVICE, CLASSIC_BT_AQS, [])`，`CLASSIC_BT_AQS = 'System.Devices.Aep.ProtocolId:="{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}"'`；`watcher.add_added(lambda _s, info: ...)` 里用 `info.name`、`address_from_device_id(info.id)`、`bool(info.pairing.is_paired)`、`info.id` 组 `ClassicDeviceInfo`；按 0.1 s 步进等到 `duration` 或 `stop()`，`finally: watcher.stop()`。
- 配对：`info = await DeviceInformation.create_from_id_async(device.device_id)`；`info.pairing.is_paired` → 直接返回 `"ALREADY_PAIRED"`；否则 `custom = info.pairing.custom`，`token = custom.add_pairing_requested(handler)`，`await custom.pair_async(DevicePairingKinds.CONFIRM_ONLY | DevicePairingKinds.PROVIDE_PIN)`，`finally: custom.remove_pairing_requested(token)`，返回 `result.status.name`。`handler(args)`：`kind = args.pairing_kind.name`；`"PROVIDE_PIN"` → `pin = prompt("pin", "1234")`，有值则 `args.accept_with_pin(pin)`；其它 → `prompt("confirm", str(getattr(args, "pin", "") or ""))` 非 `None` 时 `args.accept()`（返回 `None` 表示拒绝，不 accept）。
- 连接：`service = await RfcommDeviceService.from_id_async(device.device_id)`，`None` → `raise RuntimeError("设备未提供 SPP 串口服务")`；`socket = StreamSocket()`；`await socket.connect_async(service.connection_host_name, service.connection_service_name)`；起读任务：`reader = DataReader(socket.input_stream)`、`reader.input_stream_options = InputStreamOptions.PARTIAL`，循环 `n = await reader.load_async(4096)`，`n == 0` 结束，否则 `callback(bytes(reader.read_buffer(n)))`（`IBuffer` 支持 `bytes()`）。
- 发送：`writer = DataWriter(socket.output_stream)`、`writer.write_bytes(data)`、`await writer.store_async()`、`writer.detach_stream()`，返回 `len(data)`。

`SppWorker` 要点：`connect_device` 顺序 = `pairing_started` → `pair(...)`（`ALREADY_PAIRED`/`PAIRED` 视为成功，其它用 `pair_status_text` 发 `pairing_finished` 并 `error`）→ `connect()` → `connected("RFCOMM", name)`；`pair` 与 `connect` 都用 `asyncio.wait_for(..., 30)` 包裹，超时分别用 `explain_error("pair", "配对超时")` / `explain_error("connect", "连接超时")`；`open_com` 用 `serial_factory`（默认 `SerialWorker`）建实例、接 5 个信号转发（`opened`→`connected(f"COM{n}", port)`、`closed`→`disconnected("串口已关闭")`、`data_received`/`data_sent` 直转、`error`→`error`），`send` 按当前通道分流；`prompt` 通过 `pairing_prompt.emit(kind, text)` + `threading.Event` 等 `answer_prompt()`，超时 30 s 返回 `None`；`shutdown()` 先 `disconnect_device("程序退出")` 再走基类 `shutdown()`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_spp_worker.py -v` → 全部 PASS；再 `python -m pytest` 全绿。

- [ ] **Step 5: 提交**

```bash
git add app/io/spp_worker.py tests/test_spp_worker.py
git -c user.name="Yaopyr" -c user.email="113556181@qq.com" commit -m "feat: add classic bluetooth SPP worker with one-click pairing"
```

---

### Task 4: 蓝牙调试页面（模式切换、面板、共用日志与发送）

**Files:**
- Create: `app/ui/pages/bluetooth_page.py`
- Modify: `app/core/settings.py`（在 `DEFAULT_SETTINGS` 的 `hid.*` 之后插入 13 个 `bluetooth.*` 键）
- Test: `tests/test_bluetooth_page.py`

**Interfaces:**
- Consumes: Task 1 全部函数；Task 2 `BleWorker`/`BleDeviceInfo`；Task 3 `SppWorker`/`ClassicDeviceInfo`；`LogPane`、`section_card`、`StatusChip`、`scroll_container`、`MultiSendDialog`、`SerialConfig`、`list_ports`、`parse_hex`/`encode_text`/`resolve_codec`。
- Produces: `class BluetoothPage(QWidget)`，构造 `BluetoothPage(settings, paths, parent=None, *, ble_worker_factory=None, spp_worker_factory=None)`，公开方法 `has_active_work() -> bool`、`stop_workers() -> None`、`current_mode() -> str`（`"ble"`/`"spp"`）；控件属性名固定为：`mode_segment`、`left_stack`、`ble_panel`、`spp_panel`、`status_chip`、`open_bt_settings_button`、`log_pane`、`send_editor`、`send_button`、`hex_send_check`、`newline_combo`、`repeat_check`、`interval_spin`、`send_file_button`、`cancel_file_button`、`multi_send_button`、`target_label`、`write_with_response_switch`、`mtu_chunk_switch`、`counter_label`；BLE 侧 `scan_button`、`scan_duration_combo`、`filter_edit`、`sort_combo`、`only_named_switch`、`scan_remain_label`、`device_table`、`connect_button`、`mtu_label`、`mtu_refresh_button`、`rssi_label`、`service_tree`、`tree_expand_button`、`tree_collapse_button`、`tree_filter_edit`、`read_button`、`subscribe_button`、`focus_write_button`、`copy_uuid_button`、`ignore_empty_switch`；SPP 侧 `spp_scan_button`、`spp_filter_edit`、`only_paired_switch`、`spp_table`、`spp_connect_button`、`pair_stage_label`、`com_combo`、`baud_combo`、`com_connect_button`、`spp_info_label`。

**设置键（写进 `DEFAULT_SETTINGS`，顺序照此）：** `bluetooth.mode`="ble"、`bluetooth.last_device`=""、`bluetooth.last_device_name`=""、`bluetooth.auto_connect`=False、`bluetooth.favorites`=[]、`bluetooth.aliases`={}、`bluetooth.hex_send`=False、`bluetooth.hex_display`=False、`bluetooth.spp_baud`=115200、`bluetooth.spp_com`=""、`bluetooth.write_with_response`=True、`bluetooth.mtu_chunk`=True、`bluetooth.ignore_empty_notify`=True。

- [ ] **Step 1: 写失败测试**

`tests/test_bluetooth_page.py` 用假 worker（`QObject` 子类，信号名与真 worker 完全一致，命令方法记录到 `self.calls` 并可选立刻回发信号；`start()` 空实现，`is_connected()`/`is_scanning()` 返回可控布尔，`shutdown()` 记录调用）：

```python
@pytest.fixture
def page(qapp, tmp_path):
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    settings = Settings(paths.config_file); settings.load()
    ble, spp = FakeBleWorker(), FakeSppWorker()
    widget = BluetoothPage(settings, paths, ble_worker_factory=lambda: ble,
                           spp_worker_factory=lambda: spp)
    widget.show()
    yield widget, settings, ble, spp
    widget.stop_workers(); widget.close()

def test_mode_switch_disconnects_current_connection(page):
    # ble.connected.emit("AA:BB", 23) 后切到 "spp" → ble.calls 含 "disconnect_device"，日志含 "已断开"
def test_send_disabled_without_writable_characteristic(page):
    # BLE 模式连接后未选特征值：send_button.isEnabled() is False，target_label 含 "未选择"
def test_write_target_label_and_send(page):
    # 选中 FFE1 可写特征值 → target_label 含 "FFE1"；输入 "01 02" + HEX → ble.calls 记录 ("write", uuid, b"\x01\x02")
def test_notification_goes_to_log_and_ignores_empty(page):
    # ignore 开关默认开：空通知只加计数不写日志；非空通知日志含 "FFE1"
def test_settings_persist_and_restore(qapp, tmp_path):
    # 切 spp、改波特率 9600、收藏/别名，close 后重开页面验证 settings 值与恢复结果
def test_stop_workers_shuts_down_active_worker(page):
    # ble.connected 后 stop_workers() → "shutdown" 在 ble.calls
def test_has_active_work_tracks_connection_and_scan(page): ...
def test_bluetooth_error_shows_reason_and_settings_button(page):
    # ble.error.emit("蓝牙已关闭") → 状态行含该原因、open_bt_settings_button 可见
def test_alias_overrides_name_and_favorites_sort_first(page): ...
def test_file_send_chunks_by_mtu(page, monkeypatch): ...   # mtu=23 → 每块 ≤20 B，写满后发下一块
def test_disconnect_reason_updates_status_and_buttons(page): ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_bluetooth_page.py -v`
Expected: `ModuleNotFoundError: No module named 'app.ui.pages.bluetooth_page'`

- [ ] **Step 3: 实现设置键与页面骨架**

先加 `DEFAULT_SETTINGS` 的 13 个键（`tests/test_settings.py` 的 `as_dict() == DEFAULT_SETTINGS` 断言自动兼容新键）。再实现 `BluetoothPage`：

- 装配：`TitleLabel("蓝牙调试")` + `CaptionLabel("低功耗蓝牙与经典蓝牙串口，一页完成扫描、配对、收发")` + `StatusChip`；`mode_segment = SegmentedWidget()`，`addItem("ble", "低功耗蓝牙 (BLE)")`、`addItem("spp", "经典蓝牙 (SPP)")`，`currentItemChanged` → `_on_mode_changed`；`QSplitter` 左列 `scroll_container(self, stack_container, min_width=PANEL_MIN_WIDTH)`（内含 `left_stack = QStackedWidget`，两页分别是 `_build_ble_panel()`/`_build_spp_panel()`），右列 `log_pane = LogPane("", show_direction=True, settings=settings)` + 发送卡片（编辑器、HEX 发送、行尾、连续发送、间隔、发送/发送文件/取消文件、`target_label`、计数器）。`Ctrl+Enter` 与 `Ctrl+Return` 都接 `_send_current`。
- 懒建 worker：`_ensure_ble_worker()` / `_ensure_spp_worker()` 首次点扫描/连接/COM 连接时才 `factory()` 并连信号；`_ble_worker`/`_spp_worker` 初始 `None`。页面构造不发任何蓝牙调用（反目标）。
- 模式切换 `_on_mode_changed(key)`：`key != self._mode` 且旧模式有活动连接 → 先 `_disconnect_active(f"已断开 {旧模式标签}：切换模式")`，日志 `append_info`，再 `left_stack.setCurrentIndex`。切模式不清空日志、不改 `bluetooth.mode` 之外的配置。
- BLE 面板：扫描卡片（开始扫描/停止、时长 5/10/20 s 默认 10、`filter_edit`、排序下拉「信号强度/名称/最近发现」、`only_named_switch`、`scan_remain_label` 用 `QTimer` 每秒递减）→ 设备表（4 列：名称/地址/信号/状态，`TableWidget`，右键菜单：收藏/取消收藏、重命名（`QInputDialog`）、复制地址、连接）→ 设备信息卡（名称/地址/MTU + 刷新/最后广播 RSSI + 时间/服务数）→ 服务树（`TreeWidget`，服务节点 `service_label`，特征值节点 `characteristic_label` + `properties_badges`；工具栏三个控件 `tree_expand_button`/`tree_collapse_button`/`tree_filter_edit`（按 UUID 过滤）；右键复制服务/特征值 UUID）→ 操作行（读取/订阅/取消订阅/写入（只把焦点交给右列发送框）/复制 UUID，全部按选中特征值属性启用）→ `ignore_empty_switch`。
- SPP 面板：扫描卡片（开始扫描、`spp_filter_edit`、`only_paired_switch`）→ 设备表（名称/地址/配对状态/通道，已连接行在通道列标 `RFCOMM` 或 `COMn`）→ `一键连接`（`pair_stage_label` 显示 配对中 → 连接中 → 已连接）→ 备用通道（`com_combo` 由 `list_ports()` 过滤 `is_bluetooth_port`，允许手输；`baud_combo` 默认 115200；连接按钮；说明 CaptionLabel「RFCOMM 直连失败或驱动未创建 COM 口时使用」）→ 已连接信息。
- 蓝牙不可用提示：worker `error` 时状态行写原因 + `open_bt_settings_button`（`QDesktopServices.openUrl(QUrl("ms-settings:bluetooth"))`）`show()`。
- 发送：`_build_payload` 复用串口页逻辑（`parse_hex`/`encode_text` + 行尾）；BLE 必须选中可写特征值，否则禁用并提示；SPP 写当前通道。`multi_send_button` 复用 `MultiSendDialog`（点条目即发，与串口页一致）；`write_with_response_switch`（BLE）、`mtu_chunk_switch`（默认开）放在发送卡片内。文件发送：BLE 按 `chunk_bytes(data, mtu)`（`mtu_chunk_switch` 关时整包发）分片，SPP 按 4096 B 分片，等 `write_finished`/`data_sent` 确认后再发下一块（照 `SerialPage._send_next_file_chunk` 的确认式推进）。
- 通知：`notification(uuid, data)` → 空 `data` 且 `ignore_empty_notify` 开 → 只 `_refresh_counters()`；否则 `log_pane.append_bytes(data, "rx", source=short_uuid(uuid))`。
- 持久化：`_restore_settings()` 读 13 键并回填控件（含自动选中 `bluetooth.last_device`，`auto_connect` 开才自动连）；`_save_settings()` 写回并 `settings.save()`，`_loading` 守卫照抄 `SerialPage`。
- 生命周期：`has_active_work()` = 任一 worker 已连接/扫描中/文件发送中/连续发送定时器运行；`stop_workers()` 停定时器、取消文件、`disconnect_device("程序退出")` + `shutdown()` 两个 worker（`None` 跳过），异常只记日志；`closeEvent` 保存配置后 `stop_workers()`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_bluetooth_page.py -v` → 全部 PASS；`python -m pytest tests/test_settings.py tests/test_ui_layout.py -v` → 全绿（此时页面还没注册路由，`test_ui_layout` 不受影响）。

- [ ] **Step 5: 全量回归并提交**

Run: `python -m pytest` → 全绿

```bash
git add app/core/settings.py app/ui/pages/bluetooth_page.py tests/test_bluetooth_page.py
git -c user.name="Yaopyr" -c user.email="113556181@qq.com" commit -m "feat: add bluetooth debug page with BLE and SPP modes"
```

---

### Task 5: 注册路由、打包依赖、文档同步与构建验收

**Files:**
- Modify: `app/ui/main_window.py:55-64`（`_PAGE_SPECS` 的「通信」组，在 `serial` 之后插入）
- Modify: `requirements.txt`
- Modify: `build/app.spec:32-52`（`hiddenimports`）
- Modify: `main.py:41-50`（`REQUIRED_MODULES`）
- Modify: `build/build.ps1:78`（`Assert-SelftestReport` 的模块清单）
- Modify: `tests/test_main_window.py:181-192`（导航文字）、`:222`（图标唯一性计数）
- Modify: `tests/test_ui_layout.py:142`（左右分栏路由元组）
- Modify: `README.md`（功能总览加一行、删除「明确不支持」里的蓝牙行、测试计数）
- Modify: `docs/USAGE.md`（新增 `## 蓝牙调试`）
- Modify: `docs/ARCHITECTURE.md`（services/io 模块表、线程与数据流、已知技术债）
- Modify: `CHANGELOG.md`（顶部 `## [Unreleased]` 的 Added）

**Interfaces:**
- Consumes: Task 4 的 `BluetoothPage`；Task 1–3 的模块。
- Produces: 路由 `bluetooth`（导航文字「蓝牙调试」，图标 `BLUETOOTH`，位置在「串口调试」之后）；打包产物可导入 bleak 与 WinRT RFCOMM。

- [ ] **Step 1: 注册路由并更新路由断言**

`main_window.py` 的「通信」组改为 `("serial", SerialPage, "CONNECT", "串口调试"), ("bluetooth", BluetoothPage, "BLUETOOTH", "蓝牙调试"), ...`，并 `from app.ui.pages.bluetooth_page import BluetoothPage`。`tests/test_main_window.py` 导航文字列表在「串口调试」后插入「蓝牙调试」，`test_navigation_icons_are_unique` 的 `== 10` 改 `== 11`；`tests/test_ui_layout.py` 的 `test_side_panels_use_shared_min_width` 路由元组加 `"bluetooth"`。

Run: `python -m pytest tests/test_main_window.py tests/test_ui_layout.py -v`
Expected: PASS（布局测试会以真实窗口在 1180×760 下逐页检查裁切/重叠/滚动越界；若左列出现裁切，收窄表格列宽、给长文案加 `setWordWrap(True)`，不得改共享布局常量）

- [ ] **Step 2: 依赖与打包**

`requirements.txt` 追加四行固定版本（`bleak==3.0.2`、`winrt-Windows.Devices.Bluetooth.Rfcomm==3.2.1`、`winrt-Windows.Networking==3.2.1`、`winrt-Windows.Networking.Sockets==3.2.1`，保持与其他依赖相同的 `==` 风格与排序）。`build/app.spec` 在 `hiddenimports` 末尾追加：

```python
hiddenimports += collect_submodules("bleak") + collect_submodules("winrt")
```

（文件顶部已有 `from PyInstaller.utils.hooks import collect_data_files`，同处补 `collect_submodules`；这些 `winrt-*` 包的实现是 `.pyd`，只能靠 `collect_submodules` 收进来。）

`main.py` 的 `REQUIRED_MODULES` 追加 `"bleak"`、`"winrt.windows.devices.bluetooth.rfcomm"`、`"winrt.windows.networking.sockets"`；`build/build.ps1` 的 `Assert-SelftestReport` 模块列表追加同名三项（PowerShell 用 `$payload.resources.modules.'winrt.windows.devices.bluetooth.rfcomm'` 形式访问带点的键）。

Run: `python -c "import app.io.ble_worker, app.io.spp_worker; print('ok')"` 与 `python main.py --selftest --report "$env:TEMP\selftest-bt.json"` → Expected: `ok`；报告 `ok=true` 且 `resources.modules` 含新增三项。

- [ ] **Step 3: 文档同步**

- `README.md`：功能总览表「串口调试」后加 `| 蓝牙调试 | BLE 扫描/连接、服务树、读/写/通知订阅、MTU 与 RSSI 显示；经典蓝牙扫描、一键配对（PIN 弹窗）、RFCOMM 直连与系统蓝牙 COM 口回退；文本/HEX、行尾、连续/文件发送、会话导出 |`；删除「明确不支持」表中的 `蓝牙调试` 行；「项目结构」树的 `app/io/` 与 `app/services/` 下补三个新文件；测试计数改成实际值。
- `docs/USAGE.md`：在 `## 串口调试` 之后新增 `## 蓝牙调试`，写清两种模式的步骤、一键配对流程（含 PIN 默认 1234）、COM 回退适用场景、收藏/别名/记住上次设备、以及「蓝牙关闭/无适配器」的排查（指向 `ms-settings:bluetooth`）。
- `docs/ARCHITECTURE.md`：services 表加 `bluetooth.py`、io 表加 `async_worker.py`/`ble_worker.py`/`spp_worker.py`，「线程与数据流」加 `### 蓝牙`（daemon asyncio 线程 → 信号 → 页面；同一时间一条活动连接），已知技术债加「BLE MTU/RSSI 只读、SPP 依赖 WinRT、无跨平台」。
- `CHANGELOG.md`：顶部加 `## [Unreleased]` / `### Added`，条目覆盖 BLE、SPP 一键配对、COM 回退、配置键、新增依赖与测试数。

- [ ] **Step 4: 全量测试与构建**

Run: `python -m pytest` → Expected: 全绿，记录准确计数并回填文档
Run: `powershell -ExecutionPolicy Bypass -File build/build.ps1` → Expected: `Build and verification succeeded.`，selftest/smoke 均为 exit 0，`dist/build.log` 的 `Tests` 计数与上一步一致
Run: `python -m PyInstaller.utils.cliutils.archive_viewer -l -r -b dist/PortHelperFluent.exe | Select-String "winrt|bleak"` → Expected: 含 `rfcomm`、`bleak` 条目（证明 WinRT `.pyd` 已收进 onefile）

- [ ] **Step 5: 提交**

```bash
git add app/ui/main_window.py requirements.txt build/app.spec main.py build/build.ps1 tests/test_main_window.py tests/test_ui_layout.py README.md docs/USAGE.md docs/ARCHITECTURE.md CHANGELOG.md
git -c user.name="Yaopyr" -c user.email="113556181@qq.com" commit -m "feat: register bluetooth page and package winrt dependencies"
```

---

## 手工验收（有真机时，构建后执行）

| 场景 | 步骤 | 期望 |
| --- | --- | --- |
| BLE 扫描 | 打开蓝牙页 → 开始扫描 10 s | 列表出现附近设备，期间界面可拖动、可切页 |
| BLE 读写通知 | 连接透传模块 → 展开服务树 → 订阅 FFE1 → 发送 HEX | 通知按 `FFE1:` 前缀进日志；写入回显 `write_finished`；MTU 显示系统值 |
| SPP 一键配对 | 未配对 HC-05 选中 → 一键连接 | 弹 PIN 框（预填 1234）→ 配对成功 → 自动连 RFCOMM → 收发正常 |
| COM 回退 | 系统已建蓝牙 COM 口 → 备用通道选 COM 连接 | 收发正常，波特率可改 |
| 模式互斥 | BLE 已连接时切到 SPP | 日志出现「已断开 …：切换模式」，BLE 状态回到未连接 |
| 蓝牙关闭 | 关闭系统蓝牙 → 扫描 | 状态行给出具体原因并可点「打开蓝牙设置」 |
| 退出清理 | 连接中关闭主窗口 | 出现退出确认，确认后线程退出、无残留进程 |

## 不在本次范围

- BLE 固件升级（DFU/OTA）、经典蓝牙音频/HID 主机模式、配对信息删除、多设备并发连接、经典蓝牙完整服务浏览。
- 版本号升位与 GitHub Release：本次只在 CHANGELOG 记为 `Unreleased`，发布由用户另行指示。

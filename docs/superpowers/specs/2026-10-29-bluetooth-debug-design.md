# 蓝牙调试页设计（2026-10-29）

## 意图

给 PortHelper Fluent 增加一页「蓝牙调试」，同时覆盖 **BLE（低功耗蓝牙）** 与 **经典蓝牙 SPP** 两类设备。使用者是调单片机通信模块的工程师（HC-05/JDY-31 串口透传、BLE 透传模块、传感器、手环等），目标是一页之内完成「找到设备 → 连上 → 收发数据 → 看清服务结构」，不需要再去装别的串口工具或厂商 App。

已定方向（用户选定）：BLE 与 SPP 都要；SPP 必须能**一键配对并连接**；界面要人性化；常用功能齐全；其余自由发挥。

## 目标

- 一页内用分段控件切换 **BLE / 经典蓝牙**，两种模式共用日志区、发送区、计数与导出。
- BLE：扫描、连接、服务/特征值树、读/写、通知订阅、MTU 显示、信号强度显示。
- SPP：扫描经典蓝牙设备、一键配对（需要 PIN 时弹窗输入）、RFCOMM 直连；系统蓝牙 COM 口作为备用通道。
- 记住上次设备与各项配置；收藏与别名。
- 全程不阻塞 GUI；失败必须给中文原因。
- 自带测试，不依赖真机即可覆盖状态机与 UI 行为。

## 非目标

- 跨平台：只做 Windows（WinRT 后端）。
- 不做 BLE 固件升级（DFU/OTA）、不做经典蓝牙音频/HID 主机模式。
- 不做配对信息管理（删除配对、清除 bond）、不做多设备并发连接。
- 不做经典蓝牙的完整服务浏览（经典侧只针对 SPP）。

## 反目标

以下任一出现即视为这次做失败：

- BLE 扫描/连接/读写、WinRT 配对调用落在 GUI 线程上导致界面卡顿。
- 打开页面就自动扫描或自动连接（侵扰）。
- 失败只是静默无响应，或只写日志不提示。
- 没有蓝牙适配器/蓝牙关闭时崩溃、卡死或按钮永远转圈。
- 打包后的 exe 启动即崩（WinRT/bleak 模块缺失）。

## 约束（来自现有项目）

- 页面必须遵守 `app/ui/widgets/page_layout.py` 的规范：`PAGE_MIN_SIZE`、`SingleDirectionScrollArea`、面板最小宽度；`tests/test_ui_layout.py` 会逐路由校验，新路由自动纳入。
- 后台任务沿用现有约定：**daemon `threading.Thread` + `QObject` 信号**，不用 `QThread`（见 `app/io/update_worker.py` 的教训：阻塞调用无法被打断，QThread 退出会 abort）。
- 复用 `LogPane`、`SessionLog`、`multi_send_dialog`、`SerialWorker`/`PyserialTransport`，不另造日志与串口实现。
- 配置键统一 `bluetooth.*`，加入 `DEFAULT_SETTINGS`；页面配置遵循现有 `_restore_settings` / `_save_settings` 写法。
- 新增依赖必须写进 `requirements.txt`，并保证 PyInstaller onefile 打包可用。
- 文档同步：`README.md`（功能表/目录树/测试数）、`docs/USAGE.md`、`docs/ARCHITECTURE.md`、`CHANGELOG.md`。

## 方案取舍

**方案 1（否决）：** BLE 用 `PyQt6.QtBluetooth`，SPP 只列系统已配对的 COM 口。零新增依赖，但 Windows 上拿不到 MTU/配对/断开原因，SPP 也做不到一键配对。

**方案 2（选定）：** BLE 用 `bleak`（WinRT 后端）；SPP 用 WinRT `RfcommDeviceService` + `StreamSocket` 直连，配对走 `DeviceInformation.Pairing.PairAsync`（需要 PIN 时由 UI 提供）。真一键，且不污染系统 COM 口列表。

**方案 3（作为内置回退）：** SPP 只做配对，连接复用系统蓝牙 COM 口 + `SerialWorker`。保留在方案 2 内部：RFCOMM 连不上或驱动没建口时，用户可切到 COM 口通道。

代价：新增 `bleak`、`winrt-Windows.Devices.Bluetooth.Rfcomm`、`winrt-Windows.Networking.Sockets`、`winrt-Windows.Networking`（`winrt-*` 传输包已随环境存在），两个 worker 各自跑 asyncio loop，PyInstaller 需要收集 WinRT 的 `.pyd`，exe 体积会增加。

## 页面结构

导航「通信」组，插在「串口调试」之后：路由 `bluetooth`，图标 `BLUETOOTH`，文字「蓝牙调试」。其余分组与顺序不变。

顶部一行 `SegmentedWidget`：`低功耗蓝牙 (BLE)` / `经典蓝牙 (SPP)`，切换只切换左列面板与发送区的可用能力，日志区保留当前内容（切换时在日志插入一条系统分隔线）。

左右两列沿用串口页的比例（左列配置、右列日志 + 发送），左列可滚动。

### BLE 模式左列

1. **扫描卡片**：`开始扫描 / 停止` 按钮、扫描时长下拉（5 / 10 / 20 秒，默认 10）、名称/地址过滤框、排序下拉（信号强度 / 名称 / 最近发现）、`仅显示有名称的设备` 开关。扫描期间显示剩余秒数。
2. **设备表**（`TableWidget`，列：名称 / 地址 / 信号 / 状态）：收藏的设备置顶并带星标；选中一行后底部 `连接` 按钮可用；右键菜单：收藏/取消收藏、重命名、复制地址、连接。
3. **设备信息卡片**（连接后显示）：名称、地址、当前 MTU（Windows 由系统协商，只读，带刷新按钮）、最后一次广播的 RSSI（带时间，连接后不再刷新）、服务数。
4. **服务树**（`TreeWidget`）：服务节点显示 UUID 与已知名称（如 `0000ffe0-…` 显示 `FFE0（自定义）`），特征值节点显示 UUID、属性徽章（Read / Write / WriteNoRsp / Notify / Indicate）与订阅状态。工具条：展开全部、折叠全部、按 UUID 过滤。
5. **特征值操作区**（选中特征值后可用）：`读取`、`订阅 / 取消订阅`、`写入`（把焦点交给右列发送区，不在左列再放第二个写入框）、复制 UUID。所有操作结果都进日志。

### SPP 模式左列

1. **扫描卡片**：`开始扫描` 按钮（经典蓝牙一次探寻，约 10–20 秒，可随时停止）、过滤框、`仅显示已配对` 开关。
2. **设备表**（名称 / 地址 / 配对状态）：行内提示当前通道。
3. **连接区**：`一键连接` —— 已配对设备直接连 RFCOMM；未配对设备先 `PairAsync`，按系统返回的请求类型处理：需要 PIN 时弹输入框（预填 `1234`，可改），只要求确认时弹应用内确认框（显示匹配码）。配对成功后自动接着连 RFCOMM。过程按阶段在按钮下方显示：配对中 → 连接中 → 已连接。
4. **备用通道**：`系统蓝牙串口` 下拉（自动识别描述或 HWID 含 Bluetooth 的 COM 口）+ 波特率（默认 115200，可填 9600）+ `连接` 按钮；旁边一行说明「RFCOMM 直连失败或驱动未创建 COM 口时使用」。
5. **已连接信息**：设备名、地址、通道（RFCOMM / COMx）、当前波特率（仅 COM 通道）。

### 模式切换

同一时间只保留一条活动连接。切换到另一模式时，若当前模式已连接，先断开（日志记一条系统分隔线并写明「已断开 BLE：切换模式」），再让新模式可用。切回原模式需要重新连接（上次设备仍被记住并自动选中）。`has_active_work()` 只看当前活动连接与扫描/发送任务，退出确认因此始终针对一条链路。

### 右列（两种模式共用）

- **日志区**：`LogPane`，新增「来源」显示（通知 / 读取 / 写入 / 系统），BLE 通知按特征值 UUID 前缀标注；保留 Hex 切换、时间戳、暂停、自动滚动、清空、导出 txt/csv。
- **发送区**：文本 + Hex 开关 + 行尾（无 / CR / LF / CRLF）+ 循环发送（间隔 ms）+ `发送`；`Ctrl+Enter` 发送。BLE 模式下发送区顶部显示 `写入目标：<当前选中的可写特征值>`，没有选中可写特征值时禁用；行尾、循环发送、文件分片、快捷命令都写向该目标。SPP 模式下发送区写向当前 RFCOMM 或 COM 通道。
- **文件发送**：选择文件后分块发送，BLE 侧按 `MTU - 3` 自动分片（窗口默认开启），SPP 侧按 4 KiB 连续发送；可停止。
- **快捷命令**：复用 `multi_send_dialog`，条目对所有模式通用。
- **计数**：收/发字节数与包数，可清零。

## 人性化细节（我的决定，可推翻）

- **记住上次设备**（地址 + 名称），下次打开页面自动选中，但**不自动连接**；「打开页面自动连接上次设备」是显式开关，默认关闭。
- **收藏与别名**持久化到配置；别名优先于广播名称显示。
- **蓝牙不可用**（无适配器 / 系统蓝牙关闭 / 无 WinRT 权限）时给出具体原因，并提供「打开 Windows 蓝牙设置」按钮（`ms-settings:bluetooth`）。
- **断开原因**显示在状态行与日志里（例如设备主动断开、连接超时、配对被拒绝）。
- **数据量的默认值保守**：日志按现有 `ui.max_display_lines` 截断，通知空包默认折叠为一个计数（`忽略空通知` 开关，默认开）。
- **常用值可复制**：右键复制服务/特征值 UUID、复制设备地址。
- 扫描与连接都有超时，超时给可执行建议（「确认设备在广播范围内」「先确认已在系统里配对」）。

## 技术设计

### 模块划分

| 模块 | 职责 |
| --- | --- |
| `app/services/bluetooth.py` | 纯函数：UUID 名称表（SIG 常用服务/特征值）、属性位解析、设备排序/过滤、MTU 分片、蓝牙 COM 口识别、提示文案、PIN 校验 |
| `app/io/ble_worker.py` | `BleWorker(QObject)`：daemon 线程 + 私有 asyncio loop，封装 `BleakScanner`/`BleakClient`；信号：`scan_started` `scan_finished` `device_found` `connected` `disconnected` `services_ready` `notification` `read_result` `write_finished` `mtu_changed` `rssi_changed` `error` |
| `app/io/spp_worker.py` | `SppWorker(QObject)`：daemon 线程 + asyncio loop，封装 WinRT 枚举/配对/RFCOMM socket；COM 通道在内部持有 `SerialWorker` 并把它的信号收敛进同一套信号；信号：`scan_started` `devices_found` `pairing_started` `pairing_prompt(kind, pin)` `connected` `disconnected` `data_received` `data_sent` `error` |
| `app/ui/pages/bluetooth_page.py` | `BluetoothPage(QWidget)`：模式切换、两个子面板（`BlePanel` / `SppPanel` 内部类或方法）、共用日志与发送区、配置持久化、`has_active_work()` / `stop_workers()` |
| `app/ui/main_window.py` | 注册路由与图标；`stop_workers()` 串接本页 |
| `app/core/settings.py` | 新增配置键（见下） |

### 平台事实（决定了两处控件形态）

- `bleak 3.0.2` 的 Windows 后端**没有** `request_mtu()`，`mtu_size` 只读（系统协商结果）。所以不做「请求 MTU」按钮，只显示当前值 + 刷新。
- 同后端也**没有** `get_rssi()`；BLE 设备连接后一般停止广播。所以设备信息卡只显示最后一次广播的 RSSI（带时间），连接期间标注为「停更」。
- 上述两条不改功能目标，只影响控件形态与测试用例：worker 不提供 `request_mtu`/`get_rssi`，`rssi_changed` 只在扫描期间发出。

### 线程与生命周期

- 所有 bleak / WinRT 调用只在 worker 线程的 asyncio loop 内执行；UI 线程通过线程安全的命令队列（`loop.call_soon_threadsafe`）下发指令，只通过 Qt 信号回传结果。
- worker 不持有父对象（`parent=None`），由页面保活；`finished` 后清空引用，避免解释器退出时残留。
- `stop_workers()`：取消所有任务 → 关客户端/socket → 停 loop → `join(≤3 s)`；页面 `closeEvent` 与 `MainWindow.stop_workers()` 都会调用。
- BLE 主动断开与异常断开都在同一处收敛成 `disconnected(reason)`，避免 UI 出现两种状态。

### 配置键

`bluetooth.mode`（`"ble"` / `"spp"`）、`bluetooth.last_device`（地址）、`bluetooth.last_device_name`、`bluetooth.auto_connect`（默认 `false`）、`bluetooth.favorites`（地址列表）、`bluetooth.aliases`（地址→别名）、`bluetooth.hex_send`、`bluetooth.hex_display`、`bluetooth.spp_baud`（默认 115200）、`bluetooth.spp_com`、`bluetooth.write_with_response`（默认 `true`）、`bluetooth.mtu_chunk`（默认 `true`）、`bluetooth.ignore_empty_notify`（默认 `true`）。

### 打包

- `requirements.txt` 增加 `bleak==3.0.2`、`winrt-Windows.Devices.Bluetooth.Rfcomm`、`winrt-Windows.Networking.Sockets`、`winrt-Windows.Networking`（与已装的 `winrt-*` 同版本系列）。
- 构建脚本需收集 WinRT 命名空间（`--collect-all winrt` 或等价 hook），并确认 onefile 产物可启动、可扫描。
- 若 WinRT 收集无法稳定通过，退回方案 3（SPP 仅 COM 通道）作为发布兜底，并在文档中如实说明。

### 测试策略

- `tests/test_bluetooth_service.py`：UUID 名称/属性位/排序过滤/MTU 分片/COM 口识别/PIN 校验等纯函数。
- `tests/test_ble_worker.py`、`tests/test_spp_worker.py`：注入假 scanner/client/配对器/socket，起真线程，事件等待；覆盖扫描→发现→连接→服务→读写→通知→断开→取消→shutdown，不联网、不碰真机。
- `tests/test_bluetooth_page.py`：offscreen 下用假 worker 发信号驱动 UI，覆盖模式切换、按钮启用、状态文案、配置持久化、`stop_workers`。
- `tests/test_ui_layout.py`：新路由自动纳入现有布局断言。
- `tests/test_settings.py`、`tests/test_main_window.py`：默认键与路由/图标一致性。
- 构建后跑 `selftest` + `smoke`，并在有真机时手工验收：BLE 扫描/读写/通知、SPP 一键配对连接（含 PIN）与 COM 通道。

## 留给实现者的判断

控件间距与顺序、服务树图标与徽章样式、具体中文文案、错误分类粒度、文件发送的块大小调参、测试用假对象的拆分方式、WinRT 收集的具体写法（hook 还是 `--collect-all`）。实现中若发现 WinRT 配对事件在该系统版本上有额外要求，可在不改页面结构与信号契约的前提下调整。

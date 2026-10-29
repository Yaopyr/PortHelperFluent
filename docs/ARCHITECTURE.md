# 架构设计

本文描述 PortHelper Fluent 的分层、线程模型、数据流与扩展方式。面向维护者，不要求先读源码。

## 设计目标

1. **界面与协议解耦**：核心算法（校验、编码、图像、字模、C51）不导入 Qt，可在 pytest 中直接测试。
2. **IO 与 UI 解耦**：串口/HID 的阻塞读写放在 `QThread`；网络使用 Qt 事件循环；页面只消费信号。
3. **页面可替换**：主窗口只依赖 11 个页面的构造契约，页面内部可以独立演进。
4. **单文件交付**：PyInstaller onefile，不安装服务/驱动，不写注册表，不需要管理员权限。
5. **失败可读**：所有用户可见错误使用中文「发生了什么 + 可能原因 + 下一步」，技术细节写日志。

非目标：系统级串口驱动监视、厂商 DLL（CH372）、蓝牙调试以外的蓝牙助手功能（音频、文件传输等）、增量/签名更新。

## 分层

```mermaid
flowchart TB
    UI["ui/<br/>FluentWindow · 页面 · 控件 · 对话框"]
    SVC["services/<br/>paths · settings · session_log · exports · net_tools<br/>bluetooth · updates · updater_install"]
    IO["io/<br/>serial_worker · hid_worker · net_client · net_server<br/>async_worker · ble_worker · spp_worker"]
    CORE["core/（Qt-free）<br/>encoding · checksum · color · imaging · fontmatrix · c51 · audio/gif/qr"]
    WIN["Windows / 第三方<br/>pyserial · hidapi · Qt Network · bleak · WinRT · Pillow · numpy · qrcode · ffmpeg"]
    FS[("AppData / 便携 data")]

    UI --> SVC
    UI --> IO
    UI --> CORE
    SVC --> FS
    IO --> WIN
    CORE --> WIN
```

| 层 | 职责 | 禁止 |
| --- | --- | --- |
| `app/core` | 纯算法、数据格式、配置模型、日志基础设施 | 导入 Qt、页面、IO |
| `app/services` | 路径、配置持久化、会话记录、导出、Ping/DNS/网卡 | 导入页面 |
| `app/io` | 串口/HID 工作线程、TCP/UDP 客户端、TCP 服务端、蓝牙 BLE/SPP 后台任务 | 直接操作控件 |
| `app/ui` | 窗口、页面、控件、对话框、主题、反馈 | 在页面内实现协议/算法 |

> 已知偏差：`app/core/imaging.py` 与 `app/core/fontmatrix.py` 引用了 `app/services/exports.py` 的文件名/原子写入辅助函数。它们是 Qt-free 的，但严格来说 `core → services` 不是理想方向；后续可将这些纯文件工具下沉到 `core/files.py`。

## 模块地图

### core

| 模块 | 内容 |
| --- | --- |
| `settings.py` | 默认值、JSON 配置、损坏备份、原子保存、页面编码回退 |
| `logging_setup.py` | 轮转日志、崩溃报告、`sys`/`threading` 异常钩子 |
| `encoding.py` | HEX 解析、编码解析、进制转换、七段数码管、Bin/Hex/Text |
| `checksum.py` | 流式 Sum/XOR/奇偶/Intel HEX/TCP-IP、CRC-8/16/32 预设与自定义 |
| `color.py` | RGB888/RGB565/RGB332 编解码 |
| `imaging.py` | 图片→C/BIN/LVGL8/9、扫描/方向/位深/字节序/阈值、批量 |
| `fontmatrix.py` | 文字/BMP→点阵、分页、位序/走向、C 导出 |
| `c51.py` | 定时器/波特率/寄存器、警告、C 与 Keil 工程生成 |
| `audio_tools.py` | WAV/MP3 解码、时长、C 数组 |
| `gif_tools.py` | GIF 帧读取、BMP 导出 |
| `qr_tools.py` | 二维码生成/保存 |

### io

| 模块 | 内容 |
| --- | --- |
| `serial_worker.py` | `SerialTransport` 协议、pyserial 适配、`SerialWorker(QThread)`、端口枚举 |
| `hid_worker.py` | `HidBackend` 协议、hidapi 适配、`HidWorker(QThread)`、设备枚举 |
| `net_client.py` | `NetClientController(QObject)`，TCP/UDP、异步域名、状态/错误信号 |
| `net_server.py` | `NetServerController(QObject)`，监听、多客户端、广播、计数 |
| `async_worker.py` | daemon 线程 + 私有 asyncio loop 的通用外壳：线程安全命令投递、信号回传、`shutdown()` 有限等待 |
| `ble_worker.py` | `BleWorker(QObject)`，封装 `BleakScanner`/`BleakClient`：扫描、连接、服务、读/写、通知、MTU、断开 |
| `spp_worker.py` | `SppWorker(QObject)`，封装 WinRT 设备枚举/配对/RFCOMM socket；COM 通道在内部持有 `SerialWorker` |
| `update_worker.py` | daemon 线程 + QObject 信号的检查/下载任务（不阻塞界面、退出不阻塞进程） |

### services

| 模块 | 内容 |
| --- | --- |
| `paths.py` | AppData/便携路径、可写探测、回退 |
| `exports.py` | 文件名清洗、唯一路径、原子写 |
| `session_log.py` | 记录、字节/条数上限、生命周期计数、TXT/CSV 导出 |
| `net_tools.py` | 系统 `ping.exe`、DNS、psutil 网卡信息、默认网关 |
| `bluetooth.py` | 纯函数：SIG UUID 名称表、特征值属性解析、设备排序/过滤、MTU 分片、蓝牙 COM 口识别、PIN 校验、中文诊断提示 |
| `updates.py` | GitHub Release 查询、版本比较、资产选择、https+白名单下载、SHA-256 校验 |
| `updater_install.py` | 目标目录可写检测、备份、ASCII PowerShell 自替换脚本与重启 |

### ui

| 模块 | 内容 |
| --- | --- |
| `main_window.py` | `FluentWindow`、分组导航、几何保存、关闭确认、图标、导航悬浮展开（不挤压页面） |
| `theme.py` | 浅色/深色/跟随系统、强调色、Mica 探测 |
| `qt_utils.py` | `safe_slot`、`readable_message`、`process_events_until`、Qt 消息处理器 |
| `feedback.py` | InfoBar、错误非模态退路 |
| `widgets/` | 日志面板、文件选择、HEX 输入、状态胶囊、卡片、矩阵视图 |
| `widgets/page_layout.py` | 统一页面边距/间距、Fluent 纵向滚动容器、页面最小尺寸 1000×640、左右分栏侧栏最小宽 360 |
| `dialogs/` | 会话记录、多项发送、文本信息、自定义 CRC |
| `pages/` | 11 个页面；`convert/` 内含 7 个转换工具 |

## 线程与数据流

### 串口

```mermaid
sequenceDiagram
    participant UI as SerialPage
    participant W as SerialWorker(QThread)
    participant P as pyserial
    UI->>W: open_port(config)
    W->>P: open()
    W-->>UI: opened
    loop 读循环（50 ms 超时）
        W->>P: read(4096)
        P-->>W: bytes
        W-->>UI: data_received(bytes)
        W->>P: write(queue)
        W-->>UI: data_sent(bytes)
        W-->>UI: modem_changed(cts/dsr/ri/cd)
    end
    UI->>W: close_port()
    W->>P: close()
    W-->>UI: closed
```

- 写队列与 DTR/RTS/BREAK 命令都在工作线程顺序执行，避免 UI 线程直接碰串口句柄。
- 文件发送按 `data_sent` 确认推进：只有上一块真正写入后才读下一块，避免无界队列。
- 接收数据由 `LogPane` 接收，33 ms 批量刷新；暂停只停渲染，不停计数。

### 网络

- TCP 客户端：`QTcpSocket`；连接/断开/错误/数据都转成信号。
- UDP：`QUdpSocket`；数值 IP 直接发送，域名通过 `QHostInfo.lookupHost` 异步解析。
- 域名按主机排队，lookup ID 防止陈旧回调消费其他主机的数据；解析结果缓存 300 秒。
- TCP 服务端：`QTcpServer`，每个客户端一个 `QTcpSocket`；按客户端 ID 维护地址、连接时间、RX/TX 计数。
- 所有网络槽函数通过 `safe_slot` 兜底；错误消息区分拒绝、超时、DNS、端口占用、防火墙等。

### HID

- `HidWorker(QThread)` 读循环 50 ms 超时；发送队列；报告 ID 可选，报告长度按 1–1024 补齐/截断。
- 设备拔出或读写失败：发 `error`，关闭句柄，发 `closed`，页面自动复位。
- 仅支持标准 HID；CH372/CH375 需要厂商 DLL，不在支持范围。

### 蓝牙

```mermaid
sequenceDiagram
    participant UI as BluetoothPage
    participant W as BleWorker/SppWorker(QObject)
    participant L as 私有 asyncio loop（daemon 线程）
    participant D as bleak / WinRT
    UI->>W: submit(factory)（线程安全投递）
    W->>L: loop.call_soon_threadsafe(task)
    L->>D: BleakScanner / BleakClient · RfcommDeviceService / StreamSocket
    D-->>L: 协程结果
    L-->>UI: 信号（device_found / connected / services_ready / notification / …）
    UI->>W: shutdown()
    L->>D: 取消任务、关闭客户端/socket
    W-->>UI: finished
```

- 所有 bleak 与 WinRT 调用都只在 worker 线程的私有 asyncio loop 里执行；界面线程只用 `loop.call_soon_threadsafe` 下发指令，结果一律通过 Qt 信号回传，界面不会因为扫描/配对/连接卡住。
- worker 不持有父对象（`parent=None`）由页面保活，`finished` 后清空引用；`stop_workers()` / `closeEvent` 会取消任务 → 关闭客户端或 socket → 停 loop → `join(≤3 s)`，重复调用安全。
- 页面构造期间**零**蓝牙调用：worker 在首次扫描/连接时才创建，因此路由与布局测试不会触发任何扫描。
- 同一时间只有一条活动连接。切换 BLE/SPP 模式或切换设备前先断开当前链路，并在日志插入系统分隔线说明断开原因；主动断开与异常断开（对端断开、设备消失、读取循环异常退出）都收敛成同一个 `disconnected(reason)` 信号，避免界面出现两种状态。连接在飞时点「断开」会挂取消标志，迟到建立的链路被立即拆掉且不发 `connected`，不会在断开后又复活一条连接。
- SPP 的 COM 备用通道复用 `SerialWorker`（`QThread`），由 `SppWorker` 把它的信号收敛进同一套信号；RFCOMM 与 COM 通道互斥。
- 连接后 MTU 只读（Windows 系统协商，可刷新重读）；RSSI 只在扫描期间发出，连接后设备一般停止广播，界面标注停更。

### 日志

```text
IO 信号 → LogPane.append_bytes()
        → SessionLog.add()（生命周期计数、8 MB 上限）
        → pending deque
        → 33 ms QTimer 批量渲染（每次 2000 条/256 KB）
        → QPlainTextEdit（最多 20000 行）
```

- 超过会话上限时丢弃最旧记录；`LogPane` 显示「会话记录已截断」，TXT 导出带说明行。
- 显示区有上限，导出使用 `SessionLog` 的完整保留内容；CSV 使用 UTF-8 BOM。

### 更新

```text
启动（1.5 s 延时）或设置页「检查更新」
        → UpdateCheckWorker（daemon 线程，urllib + 10 s 超时）
        → ReleaseInfo（tag/版本/说明/资产）
        → 用户点「下载并安装」→ UpdateDownloadWorker（进度/取消）
        → 写入 data\updates\PortHelperFluent-<version>.exe.part → os.replace
        → SHA-256 校验（digest 或 SHA256.txt）→ 用户确认
        → MainWindow.prepare_for_update()：跳过退出确认，仍 stop_workers()，关闭窗口
        → PowerShell 脚本（DETACHED_PROCESS）：[System.IO.File]::Replace 原子替换
          exe → 写 update.log → Start-Process 重启
```

- 检查分两种入口：启动静默检查由 `MainWindow.schedule_update_check()` 发起（条件：非 smoke、未设 `PORTHELPER_NO_DIALOGS` 与 `PORTHELPER_NO_UPDATE_CHECK`、设置项 `ui.check_updates_on_start` 为真；发现新版本只弹 InfoBar），手动检查由设置页「更新」卡片发起（失败弹警告）。单元测试不经过 `main._start_gui`，不会联网。
- 下载只接受 https 与白名单主机（`github.com`/`*.githubusercontent.com`），重定向后的最终 URL 再校验一次；`requestInterruption()` 即取消，取消或异常都会删除 `.part`。
- 安装脚本等进程退出后再替换：`[System.IO.File]::Replace` 要求源与目标同卷，跨卷失败时回退 `Copy-Item -Force`（同样只覆盖目标 exe，不在目标目录产生旁路文件）；替换失败用备份回滚并重启旧版，`update.log` 留证据。脚本正文纯 ASCII、以 UTF-8 BOM 落盘，避免 PowerShell 5.1 在非 ASCII 路径下乱码。
- 更新相关文件（下载包、`.part`、备份、脚本、日志）都写在 `paths.data_dir/updates`：非便携模式即 `%APPDATA%\PortHelperFluent\updates`，不会在 exe 所在目录（如桌面）留下文件（可写性探针短暂创建后立即删除）；便携模式按设计落 `data\updates`。
- 不用 `QThread` 承载更新任务：`fetcher` 里是阻塞的 urllib 读取，`requestInterruption()` 无法打断它；`wait()` 超时后窗口析构时线程仍在运行，Qt 会报 `QThread: Destroyed while thread is still running` 并 abort（0xC0000409）。因此改用 daemon `threading.Thread` 执行 `run()`，结果通过 QObject 的 `pyqtSignal` 排队投递回界面线程，`wait(ms)` 只做有限等待，退出时不会卡住进程。
- 源码运行（`sys.frozen` 为假）只检查与下载，不执行替换，避免误替换 `python.exe`；目标目录不可写时同样不启动脚本。

## 配置与路径

| 项 | 默认 |
| --- | --- |
| 配置 | `%APPDATA%\PortHelperFluent\config.json` |
| 日志 | `%APPDATA%\PortHelperFluent\logs\app.log` |
| 崩溃报告 | `%APPDATA%\PortHelperFluent\logs\crash-*.txt` |
| 便携模式 | exe 同目录 `portable.flag` → `data\` |
| 便携写入失败 | 回退 AppData，显示中文警告 |
| 配置损坏 | 备份 `config.bad-*.json`，恢复默认 |
| 写入方式 | 临时文件 + `os.replace`，避免半截 JSON |

不写注册表，不安装服务/驱动，不请求管理员权限。

## 错误处理

- `safe_slot` 捕获所有 Qt 槽异常，写日志并显示 InfoBar/非模态错误框；PyQt6 不会因槽异常静默 abort。
- 启动失败：Qt 对话框 → 原生 `MessageBoxW` → 日志；`PORTHELPER_NO_DIALOGS=1` 用于无头测试。
- 所有窗口化异常写 `crash-*.txt`；Qt 消息处理器把 Qt 警告写入统一日志。
- 串口/网络/HID 错误都区分「未打开/被占用/对端断开/DNS/防火墙」并给出下一步建议。

## 打包

- `build/app.spec`：PyInstaller onefile、windowed、图标、版本信息、qfluentwidgets 资源、hidapi/qrcode/Pillow/numpy/psutil/pyserial。
- 蓝牙依赖用 `collect_submodules("bleak") + collect_submodules("winrt")` 显式收集：`winrt-*` 的实现是 `.pyd`，modulegraph 跟不进去，漏收会让 exe 启动即崩；`main.py` 的 `--selftest` 与 `build/build.ps1` 都会断言 `bleak`、`winrt.windows.devices.bluetooth.rfcomm`、`winrt.windows.networking.sockets` 三项可导入。
- 排除 PyQt5、WebEngine、Quick/QML、3D、Multimedia、tkinter、matplotlib、scipy、pandas。
- 不使用 UPX；不请求管理员权限。
- `build/build.ps1`：测试 → 图标 → 打包 → 归档检查 → 临时目录 selftest/smoke → SHA/日志。
- 归档不包含 `analysis/`、`docs/`、`tests/`、`.git`。

## 扩展方式

### 新增页面

1. 在 `app/ui/pages/` 创建 `QWidget` 页面，设置稳定 `objectName`，实现 `has_active_work()`/`stop_workers()`。
2. 在 `app/ui/main_window.py` 的 `_PAGE_SPECS` 中注册路由、类、图标、导航文字。
3. 页面构造契约：`page(settings, paths, parent=self)`；需要注入依赖时使用关键字参数。
4. 页面骨架用 `app/ui/widgets/page_layout.py`：`page_content()` 统一边距与间距，`scroll_container()` 包住可滚动内容，`apply_page_minimum(page)` 统一最小尺寸；左右分栏左栏宽度用 `PANEL_MIN_WIDTH`（360）。
5. 通用逻辑放 `core`/`services`，不要复制到页面。

### 新增转换工具

1. 在 `app/ui/pages/convert/` 创建工具控件，暴露标题与可选设置保存方法。
2. 在 `convert/page.py` 的 `TOOL_CLASSES` 注册；选中项自动持久化到 `convert.last_tool`。
3. 纯算法放 `app/core/`，补充核心测试。

### 新增校验算法

1. 在 `app/core/checksum.py` 增加流式累加器与 `compute_all` 注册。
2. 用公开已知向量或独立参考实现验证，避免用同一公式自证。
3. 在 `checksum_page.py` 的预设列表中加入显示名。

## 已知技术债

- `image_page.py`、`dotmatrix_page.py`、`c51_page.py`、`net_client_page.py` 等文件 900–1200 行，`bluetooth_page.py` 已达 1959 行（仓库内最大页面，优先拆分）；后续可拆分为控制器 + 视图。
- 图片/点阵批量与音频解码仍在 GUI 线程；超大文件可能短暂卡顿。
- `core → services` 的文件工具引用需要下沉。
- 分栏比例、最近导出目录尚未全部记忆。
- 日志方向仅用 `[RX]/[TX]` 前缀区分，未做逐行颜色。
- 真实串口/HID/蓝牙设备/Keil 属手工验收，不在自动测试范围。
- 蓝牙：BLE 的 MTU 与连接后的 RSSI 都只读（bleak 3 的 Windows 后端不提供 `request_mtu`/`get_rssi`），不能主动请求 MTU；SPP 依赖 WinRT（配对、RFCOMM），只在 Windows 10/11 可用，没有跨平台后端；不做删除配对、多设备并发与 BLE 固件升级。
- 更新仅支持 GitHub 正式 Release，无增量更新与代码签名。

这些是维护性/性能债务，不影响当前功能正确性。

# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的基本格式。

## [Unreleased]

新增「蓝牙调试」页面：同时覆盖 BLE 与经典蓝牙 SPP，一页完成扫描、配对、收发。

### Added

- 「通信」分组新增「蓝牙调试」路由（导航在「串口调试」之后），页内用分段控件切换 BLE / 经典蓝牙，日志与发送区两种模式共用。
- BLE：扫描（5/10/20 秒、名称/地址过滤、按信号强度/名称/最近发现排序、仅显示有名称）、连接、服务/特征值树（SIG 名称、属性徽章、UUID 过滤）、读取、写（含写入需应答）、通知订阅、MTU 显示与刷新、最后一次广播的 RSSI、`忽略空通知`。
- 经典蓝牙 SPP：设备扫描（约 12 秒、只看已配对）、一键配对 + 连接（需要 PIN 时弹窗预填 `1234`、只需确认时应用内核对配对码）、RFCOMM 直连；系统蓝牙 COM 口作为备用通道（波特率默认 115200）。
- 两种模式共用文本/HEX、行尾、连续发送、文件发送（BLE 按 MTU-3 分片、SPP 按 4 KiB）、多项发送、收发计数与会话 TXT/CSV 导出；同一时刻只保留一条活动连接。
- 收藏与别名（右键菜单）、记住上次设备（只自动选中不自动连接，「打开页面自动连接上次设备」显式开关默认关闭）。
- 蓝牙不可用/失败时给出中文原因与下一步建议，并提供「打开蓝牙设置」按钮（`ms-settings:bluetooth`）。
- 新增配置键：`bluetooth.mode`、`bluetooth.last_device`、`bluetooth.last_device_name`、`bluetooth.auto_connect`、`bluetooth.favorites`、`bluetooth.aliases`、`bluetooth.hex_send`、`bluetooth.hex_display`、`bluetooth.spp_baud`、`bluetooth.spp_com`、`bluetooth.write_with_response`、`bluetooth.mtu_chunk`、`bluetooth.ignore_empty_notify`。
- 新增依赖 `bleak==3.0.2`、`winrt-Windows.Devices.Bluetooth.Rfcomm==3.2.1`、`winrt-Windows.Networking==3.2.1`、`winrt-Windows.Networking.Sockets==3.2.1`；PyInstaller 用 `collect_submodules` 收集 bleak 与 `winrt`，`--selftest` 与构建脚本断言三项模块可导入。
- 748 项 pytest 测试；蓝牙服务纯函数、BLE/SPP worker（注入假 scanner/client/配对器/socket，起真线程）与页面（假 worker 发信号）全部覆盖，不需要真机或蓝牙适配器。

### Known Limitations

- BLE 的 MTU 与连接后的 RSSI 都只读（Windows 由系统协商，bleak 3 无 `request_mtu`/`get_rssi`），不能主动请求 MTU。
- SPP 依赖 WinRT，仅 Windows 10/11；不做删除配对、多设备并发与 BLE 固件升级。
- 真实蓝牙设备（HC-05/JDY-31、BLE 透传模块等）的扫描/配对/收发属手工验收项。

## [1.0.1] - 2026-10-28

在线更新：从 GitHub Releases 检查并安装新版本，同时保留 1.0.0 的全部功能。

### Added

- 在线更新：启动时静默检查（1.5 秒延时）与设置页手动检查，查询 `https://api.github.com/repos/Yaopyr/PortHelperFluent/releases/latest`，只认正式 Release。
- 发现新版本时显示更新说明（Release notes）、版本号与安装包大小；「下载并安装」在后台下载并显示进度/百分比，下载期间关闭窗口会中断下载。
- 下载安全：仅允许 https 且主机在 GitHub 白名单内（重定向后的最终地址再校验一次），优先用 Release 元数据的 `sha256:` digest，否则下载 `SHA256.txt`；哈希不符立即删除文件并中止。
- 安装方式：下载校验通过后确认一次，关闭程序（跳过退出确认、仍停止后台任务），由 ASCII PowerShell 脚本等待进程退出后原子替换 exe（跨卷回退 `Copy-Item`）并自动重启；失败写 `update.log` 并用备份回滚。
- 更新文件（下载包、`.part`、备份、安装脚本、`update.log`）统一放在 `%APPDATA%\PortHelperFluent\updates`，不会在 exe 所在目录（如桌面）留下文件；便携模式落在 `data\updates`。
- 设置页「更新」卡片：当前版本、启动时检查开关（`ui.check_updates_on_start`，关闭后启动时不再检查、手动检查仍可用）、状态与进度、下载并安装按钮。
- 源码运行时不执行替换，只提示已下载路径；程序目录不可写时拒绝自动安装并给出提示。
- 683 项 pytest 测试；更新模块与安装器全部用注入的假 opener/假 popen 覆盖，不产生真实网络请求。

### Changed

- 版本号升至 1.0.1（`app/__init__.py` 与 `build/version_info.txt` 同步）。
- 「关于」卡片与文档已同步更新来源与校验方式（更新只从 GitHub Releases 下载并校验 SHA-256），不再把更新列在支持范围之外。

### Known Limitations

- 更新仅支持 Windows，且只认 GitHub 正式 Release（draft/prerelease 不参与）；仓库转私有后未带 token 的检查会失败。
- 不做增量/差分更新与代码签名，仅依赖 https + SHA-256 + 主机白名单。
- 便携模式下更新文件落在 `data\updates`（便携模式本就把数据放在 exe 旁）。
- 目标目录不可写（如 `Program Files`）时不自动替换，只提示已下载路径。

## [1.0.0] - 2026-10-28

首个公开版本：Windows 11 风格的单片机多功能调试助手，独立 Python 复刻。

### Added

- Fluent 侧边导航主窗口、浅色/深色/跟随系统主题、强调色、Mica 探测与降级。
- 统一页面布局与滚动容器：10 个页面共用 Fluent 滚动条、边距与 1000×640 最小尺寸，最小窗口下不再出现控件裁切/重叠；侧边导航展开为悬浮面板（不挤压页面），10 个导航图标互不重复。
- 串口调试：端口枚举/描述、帧格式、编码、DTR/RTS/BREAK、线路状态、文本/HEX、时间戳、暂停、连续/文件/多项发送、会话记录与 TXT/CSV 导出。
- 网络调试：TCP 客户端、UDP、域名解析、Ping、网卡信息（含默认网关）、连续/文件发送、TCP/UDP 会话记录。
- 网络服务器：TCP 监听、客户端列表、按客户端发送、广播、断开、收发计数。
- USB HID：标准 HID 枚举/过滤、报告 ID/长度、读写、设备拔出自动复位；CH372 明确不支持。
- 校验计算：Sum-8/16/32、XOR/BCC、奇偶、Intel HEX、TCP/IP、CRC-8/16/32 预设、自定义 CRC、取反、字节序、文件流式处理、源码预览。
- 编码转换：2–36 进制、Bin/Hex/Text、七段数码管、调色板、GIF→BMP、音频→C、二维码。
- 图片取模：C/BIN/LVGL8/LVGL9、扫描/方向/位深/字节序/缩放/1 位阈值、批量导出、代码预览。
- 点阵生成：文字/BMP、系统字体、粗体/斜体、扫描/走向/位序、分页、单元格编辑、C/BIN 导出、BMP 批量。
- C51 向导：晶振/6T/12T、端口、中断、定时器、串口/波特率、功耗/延时、警告、C 与 Keil 工程导出。
- 设置与关于：主题、默认编码、显示行数、自动滚动、退出确认、日志字号、路径、配置导出/重置、支持范围与致谢。
- 应用图标、单文件 PyInstaller 构建脚本、版本信息、`--selftest`/`--smoke-test`。
- 覆盖核心算法、页面与 IO 的 pytest 全量测试；打包产物在隔离临时目录中通过自检与冒烟。

### Changed / Hardened

- UDP 域名发送改为按主机排队、lookup ID 校验与缓存，避免高速发送丢包和陈旧回调串包。
- 串口文件发送改为按真实写入确认逐块推进，避免无界队列和提前报告完成。
- 会话记录达到上限时在界面与 TXT 导出中提示截断。
- 设置页默认编码作为页面编码的回退；1 位图片阈值可配置；C51 延时使用 `unsigned long`。
- 串口左栏在窗口高度不足时滚动，避免参数控件被压缩重叠。
- 启动失败支持 Qt → 原生消息框 → 日志的多级提示；便携目录写入失败回退 AppData。
- 配置损坏自动备份并恢复默认；日志轮转与崩溃报告。

### Known Limitations

- 图片/点阵批量转换与音频解码仍在 GUI 线程，超大文件可能短暂卡顿。
- 真实串口/HID/Keil 工程需要手工验收。
- 不包含蓝牙、系统级串口监视、CH372、Keil 配色替换。
- 部分页面 900–1200 行，后续可拆分。
- 显示类设置（日志字号、最大行数等）在下次启动生效。

### Legal

- 本项目为独立实现，MIT License；原 PortHelper 及其资源版权归温子祺所有。
- 不包含原程序代码、图标、截图、驱动、蓝牙助手或反编译产物。

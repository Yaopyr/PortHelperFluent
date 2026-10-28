# 逆向工程说明

本文记录 PortHelper Fluent 的逆向分析范围、证据、方法与边界。目标是理解原程序的功能与交互，独立实现一个可维护的 Python 替代品，而不是复制原程序。

## 目标身份

| 项 | 值 |
| --- | --- |
| 文件 | `PortHelper.exe` |
| 产品名 | 单片机多功能调试助手（PortHelper） |
| 作者 | 温子祺 |
| 文件版本 | 1.8.3（关于页显示 1.8.8） |
| 架构 | PE32 / x86 / MFC |
| 大小 | 5,664,768 字节 |
| SHA-256 | `90df87b4dc4ffac89c59e79b0e7e198531d46daa04e04c2f2eb350a6075c514a` |
| 分析日期 | 2026-10-26 |

原程序未包含在本仓库中；本仓库也不分发原程序的任何二进制资源。

## 使用的方法

1. **REA 资源检查**：使用 [REA](https://github.com/morluto/rea) 的 `inspect-pe-resources`（v6.8.0）读取 PE 资源树、类型、语言、大小与摘要，形成第一手证据。
2. **PE/资源解析**：使用 Python `pefile` + 自定义解析器读取对话框模板、String Table、菜单/图标/位图/PNG、版本信息与自定义资源。
3. **字符串提取**：提取 UTF-16LE 与 GBK 中文串，按偏移归档，用于还原标签页名称、提示文案与功能边界。
4. **内嵌资源阅读**：读取 Keil 工程模板、校验算法源码、Keil 配色预览图，以及串口监视驱动/蓝牙助手载荷的元数据（不运行、不复制）。
5. **公开标准算法**：CRC、校验和、8051 定时/波特率、RGB565/RGB332、点阵取模等以公开规范与已知向量实现，并用独立测试验证。

**没有做**：

- 没有运行原程序（避免驱动安装、注册表写入、联网更新等副作用）。
- 没有安装 Ghidra/IDA/Hopper，没有做原生反编译或运行时调试。
- 没有复制原程序的代码、图标、截图、驱动、外部助手或 UI 资源。

## 证据概览

| 证据 | 结果 |
| --- | --- |
| 对话框模板 | 26 个 |
| String Table | 90 条 |
| 标签页 | 12 个（串口调试、USB 调试、网络调试、网络服务器、蓝牙调试、小工具、C51 代码向导、校验、编码转换、图片取模、点阵生成、升级与说明） |
| 自定义资源 | `DRIVER`、`EXE`（蓝牙助手）、`KEILPROJECT`、`VERIFY`（校验源码）、`PNG`（Keil 配色预览） |
| 导入表 | kernel32/user32/advapi32/setupapi/hid/iphlpapi/ws2_32/wininet/winmm 等，印证串口/网络/HID/服务/驱动/音频能力 |
| 版本信息 | CompanyName/Author 温子祺；FileDescription/ProductName 单片机多功能调试助手；FileVersion 1.8.3 |

原始证据文件（文本）随仓库提供：

- [dialogs.txt](../analysis/parsed/dialogs.txt)：对话框、控件、标签、坐标。
- [strings.txt](../analysis/parsed/strings.txt)：String Table。
- [tabs_and_ui.txt](../analysis/parsed/tabs_and_ui.txt)：标签页与相邻 UI 串。
- [key_hits.txt](../analysis/strings/key_hits.txt)：按关键词筛选的中文提示。
- [extract_res.py](../analysis/extract_res.py)：资源/对话框/String Table 解析脚本。

REA 生成的 `pe_resources.json` 含本机绝对路径（`C:\Users\...\Desktop\PortHelper.exe`），未提交到仓库，只保留在本地分析目录。原始资源目录（`analysis/resources/`、`analysis/images/`）同样未提交，避免分发原程序资产。

## 功能映射

| 原程序标签/功能 | 本项目页面 | 实现方式 |
| --- | --- | --- |
| 串口调试 | 串口调试 | pyserial + QThread；保留线路控制、多项发送、文件发送、会话记录 |
| USB 调试 | USB HID | hidapi；标准 HID 报告读写；CH372 不支持 |
| 网络调试 | 网络调试 | QTcpSocket/QUdpSocket；TCP/UDP、Ping、DNS、网卡信息 |
| 网络服务器 | 网络服务器 | QTcpServer；多客户端、广播、按客户端发送 |
| 蓝牙调试 | 不支持 | 原程序调用外部蓝牙助手 exe，依赖外部驱动/配对 |
| 小工具 | 编码转换 | 进制、Bin/Hex/Text、数码管、调色板、GIF→BMP、音频→C、二维码 |
| C51 代码向导 | C51 向导 | 定时器/波特率/中断/端口/功耗；C 与 Keil 工程 |
| 校验 | 校验计算 | Sum/XOR/奇偶/Intel HEX/TCP-IP/CRC 预设 + 自定义 CRC |
| 编码转换 | 编码转换 | 进制与 Bin/Hex/Text；补充七段数码管 |
| 图片取模 | 图片取模 | Pillow/numpy；C/BIN/LVGL8/9、扫描/方向/位深/字节序/阈值 |
| 点阵生成 | 点阵生成 | 文字/BMP→矩阵；分页、位序、可编辑预览、C/BIN/BMP 批量 |
| 升级与说明 | 设置与关于 | 主题/默认值/路径/重置/致谢；更新只从本项目 GitHub Releases 获取并校验 SHA-256（不使用原程序更新链路） |
| 串口高级监视 | 不支持 | 原程序依赖 32 位内核驱动，安装/卸载具有系统侵入性 |
| Keil 配色替换 | 不支持 | 会修改外部 Keil 配置；本项目只生成工程文件 |

## 关键行为证据

- **串口线路控制**：对话框与中文提示包含 DTR/RTS/BREAK、CTS/DSR/RING/RLSD、字符编码、连续发送、文件发送、多项发送等；本项目用 pyserial 标准 API 实现，不依赖原驱动。
- **USB HID**：原程序区分 HID 与 CH372/CH37X，存在 EP1/EP2 监视提示；本项目只实现标准 HID 的 Input/Output Report，并在界面明确 CH372 不支持。
- **校验**：原程序 `VERIFY` 资源包含校验和/奇偶/CRC 的 C 源码；本项目按公开算法独立实现，并用已知向量验证，不复制该 C 源码。
- **C51**：原程序内嵌 Keil `STARTUP.A51`、`M51Project.Uv2`、`M51Project.Opt` 等模板与配色预览；本项目只使用自写的极简启动文件与 uVision2 工程文本，页面/寄存器计算按公开 8051 资料实现。
- **图片/点阵**：原程序提供“图片取模”“点阵生成”“调色板”“GIF 转 BMP”等标签；本项目用 Pillow/numpy 独立实现，支持 C/BIN/LVGL 与批量导出。
- **排除项**：`DRIVER`（串口监视驱动）与 `EXE`（蓝牙助手）仅识别元数据，不运行、不复制、不打包。

## 证据边界

- 观察到的资源/字符串只能证明“原程序存在这些界面与提示”，不能证明其全部运行时行为。
- 没有原生反编译，因此无法逐函数确认边界条件；本项目以公开标准算法与独立测试向量为准，可能与原程序在极端输入上的行为不同。
- 没有真实硬件录像；串口/HID/网络/Keil 的真实链路需手工验收。
- 截图/图标等原始 UI 资产未复用；本仓库的图标、截图由本项目自行生成。

## 法律与归属

- 原程序 **单片机多功能调试助手（PortHelper）** 的版权归 **温子祺** 所有；本仓库不包含其代码、图标、截图、驱动、蓝牙助手或反编译产物。
- 本仓库是独立 Python 实现，使用 [MIT License](../LICENSE)；MIT 仅适用于本项目代码与自制资源，不适用于原程序。
- 本项目与原作者无隶属关系，也未获得其背书。
- 若你是权利人并认为本仓库的某些内容不妥，请通过 Issue 联系维护者，我们会及时处理。

## 参考

- [REA：Reverse Engineer Anything](https://github.com/morluto/rea)
- [设计规格](superpowers/specs/2026-10-26-porthelper-fluent-design.md)
- [实施计划](superpowers/plans/2026-10-26-porthelper-fluent.md)
- [架构设计](ARCHITECTURE.md)

# 命令行（AI Agent）

PortHelper Fluent 的同一个可执行文件同时提供图形界面与命令行：双击进入 GUI，带子命令参数时进入无界面 CLI。CLI 面向 AI Agent、脚本与 CI，默认输出 JSON，不创建 `QApplication`、不弹任何对话框、不打开主窗口。

- 仅支持 Windows（与 GUI 一致）。
- 已构建的 exe：`dist\PortHelperFluent.exe`，命令形如 `PortHelperFluent.exe checksum ...`。
- 源码入口等价：`python main.py checksum ...`（本文示例统一用源码入口，换成 exe 名即可）。
- 离线工具（校验、转换、图片、点阵、C51、配置、版本）启动时不导入 PyQt6；硬件命令按需加载后端类，但不启动 Qt 事件循环。
- 每次 CLI 调用都是独立进程；onefile exe 每次启动都要解压运行时，因此不承诺毫秒级启动。

```text
PortHelperFluent.exe <group> <action> [options]
PortHelperFluent.exe help [group]
PortHelperFluent.exe <group> <action> --help
```

`help`、`--help`、`-h` 是唯一不输出 JSON 的路径：直接打印 argparse 帮助文本并以 0 退出。`-V`/`--version` 等价于 `version` 子命令。

## 输出契约

默认 `--format json`，stdout 只输出一个 JSON 信封（流式命令除外）。

成功：

```json
{"ok": true, "command": "checksum", "version": "1.2.0", "elapsed_ms": 12, "data": {"size": 9, "results": []}}
```

失败：

```json
{"ok": false, "command": "serial.send", "version": "1.2.0", "elapsed_ms": 45,
 "error": {"code": "device_unavailable", "message": "打开串口 COM3 失败：..."}}
```

- `command` 是命中的命令名（如 `serial.send`、`image.batch`）；argparse 解析失败时固定为 `"cli"`。
- `error.code` 是稳定英文枚举；`error.message` 沿用现有模块的中文错误文案，不翻译、不改写。只有存在细节时才附加 `error.details`（例如多个 HID 候选设备）。
- 失败时 stderr 额外输出一行人类可读摘要；stdout 始终保留完整 JSON。
- `--format text` 输出 `key: value` 行，仅方便人工查看，Agent 不应依赖其排版。
- 输出统一 UTF-8（`errors="replace"`，换行 `\n`），不受控制台代码页影响。
- 默认紧凑 JSON；`--pretty` 缩进 2 空格，仅适用于非流式命令。

退出码是契约的一部分：

| 退出码 | 代表 `error.code` | 含义 |
| --- | --- | --- |
| 0 | — | 成功 |
| 1 | `internal` | 未预期异常（只输出一行摘要，不打 traceback） |
| 2 | `usage` | 参数缺失/非法（含 argparse 错误） |
| 3 | `not_found` / `invalid_input` / `device_unavailable` / `ambiguous_device` / `connection_failed` / `permission_denied` / `unsupported` / `update_failed` | 输入、环境或操作失败 |
| 4 | `expect_mismatch` / `timeout` | 等待失败：`serial`/`spp` 的 `--expect` 未命中为 `expect_mismatch`，`net` 等待回包超时为 `timeout` |
| 130 | `interrupted` | 用户中断（Ctrl+C） |

## 输入契约

需要数据的命令统一支持以下四种输入，互斥且必须给一个：

| 参数 | 说明 |
| --- | --- |
| `--text VALUE` | 文本，按 `--encoding` 编码。 |
| `--hex HEX` | 十六进制文本；支持空格、逗号、换行、`0x`、`\x` 前缀。 |
| `--file PATH` | 以原始字节读取文件。 |
| `--stdin` | 从标准输入读取原始字节。 |

`--encoding` 默认 `utf-8`，可写 `gbk`、`ansi`、`utf-16-le` 等（复用 GUI 的编码解析）。给两个及以上输入、或一个都不给，都是 `usage`（退出码 2）。

## 流式契约

`serial monitor`、`hid monitor`、`ble notify`、`net server`、`net udp --listen-only` 属于流式命令：

- 每行一个紧凑 JSON 事件（NDJSON），例如 `{"event":"data","at":"12:00:00.123","hex":"...","size":4}`。
- 必须给 `--duration`（秒，1–3600），到点自行结束；不允许无限运行。
- 所有流式命令支持 `--max-bytes N`（累计收到 N 字节后结束）与 `--max-events N`（累计 N 个事件后结束）。
- `serial monitor`、`ble notify` 另支持 `--until TEXT` / `--until-hex HEX`，命中即结束；`net server` 与 `net udp --listen-only` 不接受 `--until`（退出码 2）。
- 最后一行是标准信封（成功或失败），Agent 以最后一行为准。
- 固定输出 NDJSON：`--format text` 与 `--pretty` 视为参数错误（退出码 2）。
- `serial monitor`、`hid monitor`、`ble notify`、`net server` 运行中失败（设备拔出、连接断开等）先输出一行 `{"event":"error",...}`，再输出标准错误信封并以退出码 3 结束；启动或打开即失败则只输出错误信封。
- `net udp --listen-only` 是扩展流式模式：运行中失败直接输出标准错误信封，不额外发 `error` 事件。

## 命令总表

运行 `python main.py <group> --help` 或 `python main.py <group> <action> --help` 查看完整参数、默认值与取值范围。

### system

| 命令 | 用途 |
| --- | --- |
| `version` | 输出版本、Python 版本、是否冻结与可执行文件路径。 |
| `selftest` | 执行启动自检并把同一份报告输出到 stdout；`--report PATH` 额外写文件。 |

### checksum

| 命令 | 用途 |
| --- | --- |
| `checksum` | 计算 Sum/XOR/奇偶/Intel HEX/TCP-IP/CRC 全部结果；`--only`/`--crc` 过滤，`--custom-crc` 追加自定义 CRC，`--source` 返回算法源码。 |

### convert

| 命令 | 用途 |
| --- | --- |
| `convert base` | 2–36 进制换算，输出 2/8/10/16 进制文本；支持位宽与有符号补码。 |
| `convert binhex` | Bin/Hex/文本互转：`bin-to-hex`、`hex-to-text`、`hex-to-bytes`。 |
| `convert sevenseg` | 七段数码管字型码：给字符或段名，输出共阴/共阳码值。 |
| `convert palette` | 调色板换算：HEX/RGB 正向换算与 RGB565/332/888 解码。 |
| `convert gif` | GIF 拆帧，每帧导出一个 BMP。 |
| `convert audio` | WAV/MP3 转 C 数组并导出 `.c` 与 `.h`（MP3 需要 ffmpeg）。 |
| `convert qr` | 生成二维码 PNG/BMP。 |

### image

| 命令 | 用途 |
| --- | --- |
| `image convert` | 单图取模，导出 C/BIN/LVGL8/LVGL9。 |
| `image batch` | 文件夹批量取模；单张失败不影响其余文件。 |

### dotmatrix

| 命令 | 用途 |
| --- | --- |
| `dotmatrix text` | 文字点阵：按字号分页，可 `--toggle` 翻转单元格，导出 C/BIN。 |
| `dotmatrix bmp` | 单张图片二值化为点阵，导出 C/BIN。 |
| `dotmatrix batch` | 文件夹内每张图片各生成一个独立 C 文件。 |

### c51

| 命令 | 用途 |
| --- | --- |
| `c51 show-config` | 回显默认 C51 配置；`--out` 写出 JSON。 |
| `c51 calc` | 计算寄存器、定时器重载、波特率与警告。 |
| `c51 code` | 生成 8051 初始化 C 代码；`--out` 写出文件。 |
| `c51 export` | 导出 Keil 工程（`Main.c`、`M51Project.Uv2`、`M51Project.Opt`、`STARTUP.A51`）。 |

`calc`/`code`/`export` 可用 `--config FILE`（`-` 表示 stdin）或 `--config-json JSON` 传入配置，格式即 `c51 show-config` 的输出。

### serial

| 命令 | 用途 |
| --- | --- |
| `serial list` | 枚举串口，可按设备名/描述/硬件 ID 过滤。 |
| `serial send` | 打开串口发送 1..N 次，支持 DTR/RTS/BREAK、行尾、`--expect`/`--expect-hex` 等待回包。 |
| `serial monitor` | 持续接收，按 NDJSON 输出 `start/data/end` 事件（有界）。 |
| `serial modem` | 读取 CTS/DSR/RING/RLSD 调制解调器线状态。 |

`--baud`、`--data-bits`、`--stop-bits`、`--parity`、`--flow` 与 GUI 的串口参数一致。

### net

| 命令 | 用途 |
| --- | --- |
| `net tcp` | TCP 客户端：连接、发送、等待回包。 |
| `net udp` | UDP 发送/等待回包；`--listen-only` 只收不发（NDJSON，有界）。 |
| `net server` | 本机 TCP 服务端：`--echo`/`--reply`/`--greeting`，按 NDJSON 输出连接与数据事件。 |
| `net ping` | 调用系统 ping 探测主机。 |
| `net resolve` | 域名或 IP 解析，返回去重地址列表。 |
| `net adapters` | 枚举本机网卡信息（含默认网关）。 |

TCP/UDP 用标准库 `socket`，不依赖 QtNetwork。

### hid

| 命令 | 用途 |
| --- | --- |
| `hid list` | 枚举 USB HID 设备，可按关键词或 VID/PID 过滤。 |
| `hid send` | 向 HID 设备写入一个 Output Report，可选读取 Input Report。 |
| `hid monitor` | 持续读取 Input Report，按 NDJSON 输出 `data` 事件（有界）。 |

`--index` 指最近一次 `hid list` 的顺序；跨调用稳定定位请用 `hid list` 返回的 `path_b64`。仅 `--vid/--pid` 命中多台设备时返回 `ambiguous_device`（退出码 3），`error.details` 给出候选设备。

### ble

| 命令 | 用途 |
| --- | --- |
| `ble scan` | 扫描 BLE 广播，返回名称、地址与 RSSI。 |
| `ble services` | 列出服务、特征值、属性与 MTU。 |
| `ble read` | 读取一个特征值，返回 hex、text 与字节数。 |
| `ble write` | 写入特征值，可选无响应写入与按 MTU 分片。 |
| `ble notify` | 订阅通知，按 NDJSON 输出 `notification/end` 事件（有界）。 |

每条命令独立「连接 → 操作 → 断开」；默认超时 30 秒。

### spp

| 命令 | 用途 |
| --- | --- |
| `spp scan` | 扫描经典蓝牙设备，返回名称、地址、配对状态与设备 ID。 |
| `spp send` | 配对后走 RFCOMM 收发；`--pin`/`--confirm` 应答系统配对提示。 |
| `spp com` | 通过系统蓝牙 COM 口收发（等价于串口发送）。 |

未配对且未给 `--pin`/`--confirm` 时不会静默接受系统提示，会提示先在系统设置中配对。`--device-id` 可跳过扫描，适合先执行 `spp scan` 的 Agent。

### config

| 命令 | 用途 |
| --- | --- |
| `config show` | 输出配置路径、日志路径、数据目录、便携模式与生效配置。 |
| `config get` | 读取单个配置项。 |
| `config set` | 修改单个配置项；只允许内置键，并按默认值类型校验。 |
| `config export` | 把生效配置导出为 JSON 文件。 |
| `config reset` | 恢复默认配置，破坏性操作，必须 `--yes`。 |

### update

| 命令 | 用途 |
| --- | --- |
| `update check` | 查看最新 Release 以及是否有新版本。 |
| `update download` | 下载最新 exe 到数据目录并校验 SHA-256。 |
| `update install` | 替换当前 exe 并重启 GUI；必须 `--yes`，且只在冻结 exe 中可用。 |

`update install --file PATH` 必须同时给 `--sha256 HEX`（取自 `update download` 的 `data.sha256`），不联网也能安装；`--latest` 先检查并下载再安装。源码运行时拒绝替换，只提示已下载。

## Agent 示例

以下命令可直接在 Windows PowerShell 中复制执行；把 `python main.py` 换成 `PortHelperFluent.exe` 即为打包后的用法。示例 2 需要 `.\assets` 是包含图片的文件夹（可换成任意图片目录）；示例 3 需要真实 `COM3` 端口与设备（按实际端口修改）；示例 4 需要先启动服务端，再在另一个终端运行客户端。

**1. 校验已知向量（期望 `CBF43926`）**

```powershell
python main.py checksum --text 123456789 --only "CRC-32/ISO-HDLC"
```

**2. 图片批量取模（输出 `out\*.c`）**

```powershell
python main.py image batch --folder .\assets --out-dir .\out --type c --depth 16 --width 128 --height 64
```

**3. 串口一问一答（发送 `AT` + CRLF，等待 `OK`）**

```powershell
python main.py serial send --port COM3 --baud 115200 --text AT --line-ending crlf --expect OK --timeout 3
```

**4. TCP 回环收发（先开服务端，再运行客户端）**

```powershell
# 终端 A：有界应答服务端，对每次收到的数据回发 PONG，5 秒后自动结束
python main.py net server --bind 127.0.0.1 --port 18765 --reply PONG --duration 5

# 终端 B：发送 PING 并等待 PONG
python main.py net tcp --host 127.0.0.1 --port 18765 --text PING --expect PONG --timeout 3
```

**5. C51 导出 Keil 工程**

```powershell
python main.py c51 export --config-json '{"crystal_mhz":12.0}' --out-dir .\keil-project
```

解析结果请以 JSON 的 `ok`、`data` 与退出码为准。例如 `--expect` 未命中时退出码为 4，`ok` 为 `false`；`error.code` 在 `serial`/`spp` 下为 `expect_mismatch`，在 `net` 下为 `timeout`。

## 限制与注意

- 硬件命令（`serial`、`net`、`hid`、`ble`、`spp` 的实际收发）需要真实设备或可达网络；自动化测试用假后端与本机回环覆盖。
- `update install` 只在冻结后的 exe 中替换文件，并且必须显式 `--yes`；安装后由 PowerShell 脚本重启 GUI。
- CLI 默认不写 `logs/app.log`，日志只走 stderr；唯一的例外是 `selftest`，它本身要验证 `app.log` 可写。
- 除 `config set`/`config reset`、`update install` 外不修改用户状态；需要产物时显式给 `--out`/`--out-dir`。
- 输出默认紧凑 JSON；`--pretty` 只适用于非流式命令。二进制数据只写文件，`data` 中返回路径。
- `net server` 有客户端上限（默认 16）与运行时长上限；流式命令都不会无限运行。
- 不提供 MCP Server、常驻守护进程、会话复用、TUI 或交互式提示；每次调用都是独立进程。

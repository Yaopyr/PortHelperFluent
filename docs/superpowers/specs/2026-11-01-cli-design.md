# 面向 AI Agent 的命令行接口设计（2026-11-01）

## 意图

给 PortHelper Fluent 的全部功能增加一套命令行入口，让 AI Agent（以及脚本、CI）不必打开 GUI
就能完成校验、编解码、取模、C51 生成、串口/网络/HID/蓝牙收发、配置与更新等操作。

用户明确约束：**用户电脑上通常只有一个由本项目构建的 exe**。因此 CLI 必须与 GUI 共用
`PortHelperFluent.exe`，不能再发行第二个可执行文件或依赖旁路脚本。

## 目标

- 单文件 exe 同时承担 GUI 与 CLI：双击仍然是图形界面，带子命令参数时是无界面命令行。
- 覆盖 `docs/USAGE.md` 中列出的全部页面能力；无法无头复现的交互（如实时编辑点阵单元格）
  以参数化形式提供等价能力。
- 面向 Agent 的稳定契约：默认 JSON、固定退出码、UTF-8、stdin 输入、有界的长任务。
- CLI 路径不创建 `QApplication`、不弹任何对话框、不打开主窗口。
- 离线工具启动时不导入 PyQt6；硬件命令按需加载，且不依赖 Qt 事件循环。
- 保持现有 GUI、`--selftest`、`--smoke-test` 行为与打包验证不变。

## 非目标

- 不做 MCP Server、不做常驻守护进程、不做会话复用（每次调用都是独立进程）。
- 不做跨平台：与 GUI 一致，只支持 Windows。
- 不做 TUI、交互式提示、分页浏览。
- 不把二进制数据直接写到 stdout（一律写文件并在 JSON 中返回路径）。
- 不提供 CH372/CH375 等 GUI 也不支持的能力。

## 反目标

以下任一出现即视为本次做失败：

- 双击 exe 后留下一个可见的控制台窗口，或 GUI 启动多出命令行窗口。
- 为了 CLI 额外产出一个 exe、批处理或 PowerShell 包装脚本。
- CLI 命令需要 `QApplication`、显示平台插件或任何模态框才能运行。
- `PortHelperFluent.exe checksum ...` 在 PowerShell/cmd/Agent 子进程中拿不到 stdout 或
  退出码，或 shell 不等待进程结束。
- CLI 失败时静默退出、只写日志、或把异常 traceback 直接甩给用户。
- 破坏 `--selftest` / `--smoke-test` / 现有 GUI 测试。
- `serial monitor`、`net server` 之类的流式命令没有时间上界，Agent 调用后永久挂起。

## 约束（来自现有项目）

- PyInstaller onefile，当前 `console=False`；产物是 `dist\PortHelperFluent.exe`。
- GUI 入口在 `main.py`，顶层已导入的模块（`app.core.settings`、`app.core.logging_setup`、
  `app.services.paths`、`app.services.exports`）都是纯 Python，可以在 CLI 路径复用。
- 纯逻辑集中在 `app/core/` 与 `app/services/`，已被 755 项测试覆盖，应尽量直接调用，
  不在 CLI 里重写算法。
- 串口/HID/BLE/SPP 的 worker 依赖 Qt 信号，但其**后端类**（`PyserialTransport`、
  `HidapiBackend`、`BleakBackend`、`WinRtSppBackend`）本身不依赖 Qt 事件循环；
  TCP/UDP 现有实现基于 QtNetwork，CLI 不复用。
- 配置文件、便携模式、更新目录都通过 `app.services.paths` 解析，CLI 必须沿用同一套规则。
- 所有新增依赖需写入 `requirements.txt`；本次不新增依赖。
- 文档同步：`README.md`、`docs/USAGE.md`、`docs/ARCHITECTURE.md`、`docs/BUILDING.md`、
  `CHANGELOG.md`；版本号统一升到 `1.2.0`。

## 方案取舍

**方案 1（选定）：Qt 无关的 CLI 层 + 单 exe 双模式入口。**
新建 `app/cli/`，离线命令直接调用 `app/core`、`app/services`；硬件命令复用上述后端类，
网络收发用标准库 `socket`，流式命令用有界循环。`main.py` 先判断参数：命中 CLI 子命令
就交给 CLI，否则走现有 GUI 启动。打包改为控制台子系统，并用 PyInstaller
`hide_console="hide-early"` 只在「程序自己新建了控制台」时隐藏窗口。

**方案 2（否决）：CLI 复用 GUI worker + QApplication 事件循环。**
每次调用都要初始化 Qt、平台插件和事件循环，启动慢、无头环境脆弱，且所有命令都被迫
导入 PyQt6；与 Agent 的高频短调用场景冲突。

**方案 3（否决）：先只做离线工具，硬件功能后续补。**
范围最小，但不满足「所有功能」。

**方案 4（否决）：GUI exe 保持窗口子系统，CLI 用 AttachConsole 接管父控制台。**
窗口子系统程序从 PowerShell/cmd 启动时 shell 不会等待，管道与退出码不可靠；
Agent 的 `subprocess + capture_output` 场景也不稳定。

**方案 5（否决）：GUI exe 内嵌一个独立的控制台 exe，运行时释放并转发。**
仍然只有一个分发文件，但顶层入口是窗口子系统，shell 不等待的问题没有解决；同时体积
翻倍、构建复杂。

## 单文件 exe 的双模式

### 子系统与控制台

`build/app.spec` 改为：

```python
exe = EXE(
    ...,
    console=True,
    hide_console="hide-early",
    ...
)
```

依据（PyInstaller 6.19 帮助原文）：「in console-enabled executable, have bootloader
automatically hide or minimize the console window **if the program owns the console
window (i.e., was not launched from an existing console window)**」。

- 双击：Windows 为程序新建控制台，bootloader 判定「自己拥有」→ 立即隐藏；GUI 正常显示。
- 从 PowerShell/cmd/Agent 启动：控制台属于父进程 → 不隐藏，stdout/stderr 与退出码可用，
  shell 按控制台程序正常等待。
- Agent 用管道捕获输出：stdout 是管道，不受控制台隐藏影响。

不自行写 `GetConsoleWindow`/`GetConsoleProcessList` 逻辑，交给 bootloader，避免 onefile
父子进程计数导致的误判。

### 参数分流

`main.py` 的 `main()` 在最前面判断：

```python
CLI_COMMANDS = {
    "checksum", "convert", "image", "dotmatrix", "c51",
    "serial", "net", "hid", "ble", "spp",
    "config", "update", "selftest", "version", "help",
}
```

- `argv[0]` 命中 `CLI_COMMANDS`，或 `argv[0]` ∈ `{-h, --help, -V, --version}` → CLI。
- 无参数、`--selftest`、`--report`、`--smoke-test` → 现有 GUI 启动路径，行为不变。
- CLI 分支不得调用 `_run_gui`、`setup_logging`、`install_exception_hooks`、更新检查。
- CLI 自身的异常由 CLI 入口收敛成 JSON 错误，不弹框。

### 编码

CLI 入口把 `sys.stdout` / `sys.stderr` 重新配置为 UTF-8（`errors="replace"`，换行 `\n`），
JSON 使用 `ensure_ascii=False`。冻结后的控制台代码页不影响管道输出。

## CLI 契约

### 调用形式

```text
PortHelperFluent.exe <group> <action> [options]
PortHelperFluent.exe help [group]
PortHelperFluent.exe <group> <action> --help
```

源码运行等价：`python main.py <group> <action> ...`。

### 输出

默认 `--format json`：stdout 只输出一个 JSON 对象（流式命令除外）。

成功：

```json
{"ok": true, "command": "checksum", "version": "1.2.0", "elapsed_ms": 12, "data": {...}}
```

失败：

```json
{"ok": false, "command": "serial.send", "version": "1.2.0",
 "error": {"code": "device_unavailable", "message": "打开串口 COM3 失败：..."}}
```

- `error.code` 是稳定英文枚举；`message` 是现有模块的中文错误文案，不翻译、不改写。
- stderr 同时输出一行人类可读的错误摘要；stdout 始终保留完整 JSON。
- `--format text` 输出键值行，供人工查看；Agent 不应依赖 text 格式。

错误码与退出码：

| 退出码 | error.code 代表 | 含义 |
| --- | --- | --- |
| 0 | — | 成功 |
| 2 | `usage` | 参数缺失/非法；argparse 错误也转成 JSON |
| 3 | `not_found` / `invalid_input` / `device_unavailable` / `ambiguous_device` / `connection_failed` / `permission_denied` / `unsupported` / `update_failed` | 输入、环境或操作失败 |
| 4 | `expect_mismatch` / `timeout` | 等待回包超时或 `--expect` 未命中 |
| 1 | `internal` | 未预期异常（同时输出摘要，不打 traceback） |
| 130 | `interrupted` | 用户中断（Ctrl+C） |

argparse 解析失败时 `command` 固定为 `"cli"`；`help` 与 `--help` 是唯一不输出 JSON 的命令，
直接打印 argparse 帮助文本并以 0 退出。

### 输入

所有需要数据的命令统一支持（互斥，必须且只能给一个）：

```text
--text VALUE        # 文本，按 --encoding 编码
--hex HEX           # 十六进制文本；支持空格/逗号/换行/0x/\x；复用 parse_hex
--file PATH         # 原始字节
--stdin             # 从 stdin 读取；文本类命令按 --encoding 解码
```

`--encoding` 默认 `utf-8`，可写 `gbk`、`utf-16-le` 等，复用 `resolve_codec`。

### 流式命令

`serial monitor`、`hid monitor`、`ble notify`、`net server` 属于流式：

- 每行一个 JSON（NDJSON），形如 `{"event": "data", "at": "...", ...}`。
- 必须给 `--duration`（秒，1–3600），到点自行结束。
- 所有流式命令都支持 `--max-bytes N`（累计收到 N 字节后结束）和 `--max-events N`
  （累计 N 个事件后结束）；`serial monitor`、`ble notify` 另支持
  `--until TEXT` / `--until-hex HEX` 命中即结束，`net server` 不支持 `--until`。
- 最后一行是标准信封（成功或失败），Agent 以最后一行为准。
- 流式命令固定输出 NDJSON，`--format text` 对它们视为参数错误（退出码 2）。
- 运行中失败（设备拔出、连接断开等）先输出一行 `{"event":"error"...}`，再输出标准错误信封
  并以退出码 3 结束；启动即失败则只输出错误信封。
- 不加 `--duration` 视为参数错误（退出码 2），不允许无限运行。
- 后面的命令细节只列必选参数与命令特有参数；上述通用流式选项适用于所有流式命令。

### 其他通用约定

- 所有命令默认不写文件；需要产物时显式给 `--out` / `--out-dir`。
- CLI 不写 `logs/app.log`（避免与 GUI 多进程轮转冲突）；日志只走 stderr。
- 除 `config set/reset`、`update install` 外不修改用户状态。
- `config reset`、`update install` 必须带 `--yes`，否则退出码 2。
- 所有硬件连接/等待都受 `--timeout` 约束，默认 5 秒；蓝牙连接默认 30 秒。
- `--repeat` / `--interval-ms` 提供 GUI 的「连续发送」等价能力；默认发送 1 次。

## 命令总览

| 分组 | 子命令 | 对应 GUI 能力 |
| --- | --- | --- |
| checksum | `checksum` | 校验计算：文本/HEX/文件、编码、取反、大端/小端显示、全部预设、自定义 CRC、源码预览 |
| convert | `convert base` | 进制转换（2–36、32/64 位、有符号补码） |
| convert | `convert binhex` | Bin/Hex/Text 三种互转 |
| convert | `convert sevenseg` | 七段数码管（共阴/共阳、按段输入） |
| convert | `convert palette` | 调色板 RGB888/565/332 正反向 |
| convert | `convert gif` | GIF→BMP 批量导出 |
| convert | `convert audio` | WAV/MP3→C/H（MP3 需 ffmpeg） |
| convert | `convert qr` | 二维码生成 PNG/BMP |
| image | `image convert` | 单图取模 C/BIN/LVGL8/LVGL9 |
| image | `image batch` | 文件夹批量取模 |
| dotmatrix | `dotmatrix text` | 文字点阵、分页、C/BIN 导出 |
| dotmatrix | `dotmatrix bmp` | BMP 点阵、C/BIN 导出 |
| dotmatrix | `dotmatrix batch` | BMP 文件夹批量生成独立 C |
| c51 | `c51 show-config` | C51 默认配置 |
| c51 | `c51 calc` | 寄存器/定时器/波特率/警告计算 |
| c51 | `c51 code` | 生成 C 代码预览 |
| c51 | `c51 export` | 导出 Keil 工程（Main.c、Uv2、Opt、STARTUP.A51） |
| serial | `serial list` | 串口枚举 |
| serial | `serial send` | 打开、DTR/RTS/BREAK、文本/HEX/文件、行尾、连续发送、等待回包 |
| serial | `serial monitor` | 持续接收（有界 NDJSON） |
| serial | `serial modem` | CTS/DSR/RING/RLSD 等调制解调器线状态 |
| net | `net tcp` | TCP 客户端连接/发送/接收 |
| net | `net udp` | UDP 绑定/发送/接收/仅监听 |
| net | `net ping` | Ping |
| net | `net resolve` | 域名转 IP |
| net | `net adapters` | 网卡信息（含网关） |
| net | `net server` | TCP 服务端（有界 NDJSON，支持 echo/reply/greeting） |
| hid | `hid list` | HID 枚举/过滤 |
| hid | `hid send` | 打开、报告 ID/长度、写 Output Report、可选读 Input Report |
| hid | `hid monitor` | 持续读取 Input Report（有界 NDJSON） |
| ble | `ble scan` | BLE 扫描 |
| ble | `ble services` | 服务/特征值树（含已知名称、属性、MTU） |
| ble | `ble read` | 特征值读取 |
| ble | `ble write` | 特征值写入（含需应答、按 MTU 分片） |
| ble | `ble notify` | 订阅通知（有界 NDJSON） |
| spp | `spp scan` | 经典蓝牙扫描（是否已配对） |
| spp | `spp send` | 一键配对（`--pin`/`--confirm`）+ RFCOMM 收发 |
| spp | `spp com` | 系统蓝牙 COM 备用通道收发 |
| config | `config show/get/export/set/reset` | 设置页路径、配置导出/重置；`set` 补足无头修改 |
| update | `update check/download/install` | 检查更新、下载校验、替换并重启 |
| system | `version` | 版本信息 |
| system | `selftest` | 启动自检（输出与 `--selftest --report` 相同结构） |

不在 CLI 中提供：主题即时预览、窗口几何、点阵单元格鼠标点击（以 `--toggle` 参数替代）、
会话日志 GUI 面板的暂停/自动滚动（流式命令以参数与 NDJSON 替代）。

## 命令细节

### checksum

```text
checksum (--text|--hex|--file|--stdin) [--encoding utf-8]
          [--invert] [--byte-order big|little]
          [--only NAME]... [--crc NAME]... [--custom-crc W,POLY,INIT,REFIN,REFOUT,XOROUT]
          [--source NAME]
```

- 默认计算 `compute_all` 的全部结果；`--only` 按名称过滤（大小写不敏感，可多次）。
- `--crc` 只保留匹配的 `CRC_PRESETS`；`--custom-crc` 追加一项 `Custom CRC`。
- 过滤条件没有匹配时返回空 `results` 与 `ok:true`，不视为错误（Agent 可据此核对拼写）。
- `--byte-order` 只影响返回的 `hex`/`bin` 展示（与 GUI 的「小端显示」列一致），`value`
  永远是掩码后的数值。
- `--source NAME` 返回该结果对应的 Python 源码文本（`source_text`）。
- `data` 含 `size`、`encoding`、`inverted`、`results[]`，每项：`name`、`width`、`value`、
  `hex`、`bin`、`dec`。

### convert

```text
convert base --value TEXT --from-base INT [--bits 32|64] [--signed]
convert binhex --mode bin-to-hex|hex-to-text|hex-to-bytes (输入) [--encoding utf-8] [--out PATH]
convert sevenseg (--char C | --segments a,b,c,d,e,f,g,dp) [--common-anode]
convert palette (--color #RRGGBB | --rgb R,G,B | --decode VALUE --format rgb565|rgb332|rgb888)
convert gif --file GIF --out-dir DIR [--width N] [--height N] [--stem NAME]
convert audio --file WAV|MP3 --out-dir DIR [--name NAME] [--max-samples N]
convert qr --text CONTENT --out PATH [--ec L|M|Q|H] [--box-size 8] [--border 4]
           [--fg #000000] [--bg #FFFFFF]
```

- `binhex` 三种方向对应 GUI 的三个工具：
  - `bin-to-hex`：输入按字节处理（`--file`/`--stdin` 最常用，也接受 `--text`/`--hex`），
    返回大写十六进制文本 `hex_text`；
  - `hex-to-text`：输入按十六进制文本解析，按 `--encoding` 解码，返回 `text`；
  - `hex-to-bytes`：输入按十六进制文本解析，返回 `hex`/`size`；给 `--out` 时写原始字节文件。
- `sevenseg --segments` 与 `--char` 互斥；输出 `code_hex`、`code_bin`、`segments`。
- `palette` 正反向互斥；输出 `rgb`、`hex`、`rgb888`、`rgb565`、`rgb332`。
- `gif` 复用 `load_gif` + `export_frames`，返回 `frames` 与 `files`。
- `audio` 复用 `load_audio` + `audio_to_c` + `audio_to_h`，文件名默认取输入 stem，
  写出 `<name>.c` / `<name>.h`，返回 `sample_rate`、`channels`、`sample_width`、
  `duration_s`、`files`。
- `qr` 复用 `make_qr` + `save_qr`，只接受 `.png`/`.bmp`。

### image

```text
image convert --file IMAGE --out-dir DIR [params] [--name NAME]
image batch --folder DIR --out-dir DIR [--recursive] [params]
```

`params`：`--type c|bin|lvgl8|lvgl9`、`--scan horizontal|vertical`、
`--direction ltr|rtl|ttb|btt`、`--depth 1|8|16|24|32`、`--byte-order little|big`、
`--width N`、`--height N`、`--background #RRGGBB`、`--invert`、`--threshold 0..255`。

- 单图复用 `convert_file`；批量复用 `list_image_files` + `batch_convert`。
- 返回 `width`、`height`、`bytes`、`files`；批量另返回 `errors` 与 `canceled=false`。
- 不返回预览图；需要预览的用户用 GUI。

### dotmatrix

```text
dotmatrix text --text TEXT --out-dir DIR [--format c|bin] [--name NAME] [font params] [--toggle P,R,C]...
dotmatrix bmp --file IMAGE --out-dir DIR [--format c|bin] [--name NAME] [bmp params] [--toggle ...]
dotmatrix batch --folder DIR --out-dir DIR [--recursive] [bmp params]
```

`font params`：`--font-family NAME` / `--font-file PATH`、`--size 1..256`、`--bold`、
`--italic`、`--threshold`、`--invert`、`--scan`、`--direction`、`--bit-order msb|lsb`、
`--page-chars 1..256`。`--font-family` 用 `resolve_font_file` 解析，找不到时按 GUI 的
Pillow 内置字体兜底。

- 文字：`matrix_from_text` 得到分页；`--toggle` 以 `页,行,列`（0 基）在导出前翻转单元格。
- BMP：`matrix_from_image` 单页；分页参数不适用。
- 批量：对文件夹内每张图生成一个独立 C 文件，文件名取输入 stem。
- 返回 `pages`、`width`、`height`、`bytes`、`files`。

### c51

```text
c51 show-config [--out PATH]
c51 calc  (--config FILE|- | --config-json JSON)?
c51 code  (--config FILE|- | --config-json JSON)? [--out PATH]
c51 export (--config FILE|- | --config-json JSON)? --out-dir DIR
```

- 配置格式即 `C51Config.to_dict()`；不给配置时使用 `C51Config()` 默认值。
- `calc` 返回 `config`、`registers`、`timers[0..2]`、`baud`、`warnings`。
- `code` 返回 `text`；给 `--out` 时同时写文件。
- `export` 复用 `generate_keil_project`，返回写出的文件列表。

### serial

```text
serial list [--filter TEXT]
serial send --port PORT [conn opts] (输入) [--line-ending none|cr|lf|crlf]
            [--repeat N] [--interval-ms N]
            [--expect TEXT | --expect-hex HEX] [--timeout 5] [--read-for 1]
            [--dtr on|off] [--rts on|off] [--send-break]
serial monitor --port PORT [conn opts] --duration N [--until TEXT|--until-hex HEX]
serial modem --port PORT [conn opts]
```

`conn opts`：`--baud 115200`、`--data-bits 8`、`--stop-bits 1|1.5|2`、
`--parity none|even|odd|mark|space`、`--flow none|rtscts|xonxoff|dsrdtr`、
`--encoding utf-8`。

- 复用 `list_ports`、`SerialConfig`、`PyserialTransport`；不启动 `SerialWorker`。
- `send`：打开 → 可选 DTR/RTS → 可选 BREAK → 按 `--repeat` 写入 → 等待
  `--expect`/`--read-for` → 关闭。返回 `sent`、`received`（`hex`/`text`/`size`）、
  `matched`、`modem`。
- `--expect` 按 `--encoding` 解码后做子串匹配；`--expect-hex` 按原始字节匹配。
  未命中退出码 4。
- `monitor`：NDJSON 事件 `start/data/end`；`data` 含 `hex`、`text`、`size`、`at`；
  `--until` 命中后提前结束。
- `modem`：打开后读取 `modem_status()` 并立即关闭，返回 `cts/dsr/ring/rlsd`。

### net

```text
net tcp --host HOST --port PORT [--local-port 0] [--timeout 5]
        (输入) [--repeat N] [--interval-ms N] [--expect TEXT|--expect-hex HEX] [--read-for 1]
net udp --host HOST --port PORT [--bind-port 0] [--timeout 5]
        (输入 [--repeat N] [--interval-ms N] [--expect ...] [--read-for 1]
         | --listen-only --duration N [--max-bytes N] [--max-events N])
net ping --host HOST [--count 4] [--timeout 15]
net resolve --host HOST
net adapters
net server --bind 127.0.0.1 --port 0 [--max-clients 16] --duration N
           [--echo | --reply TEXT] [--greeting TEXT]
```

- TCP/UDP 用标准库 `socket`，不引入 QtNetwork；连接超时与读窗口都受参数约束。
- 默认发送后读 1 秒；`--read-for 0` 表示不读。`net tcp` 必须给输入；只有 `net udp`
  支持 `--listen-only`，它与发送参数、`--read-for` 互斥，要求 `--duration`，只收不发，
  输出 NDJSON `data` 事件与最终汇总。
- 返回 `sent`、`received`（`hex`/`text`/`size`、`chunks`）、`matched`、`remote`。
- `ping` 返回 `ok`、`output`、`host`；`resolve` 返回去重地址；`adapters` 返回
  `adapter_info()` 的列表。
- `net server` 是流式 NDJSON：`listening`、`client_connected`、`client_data`、
  `client_disconnected`、`error`、`end`。`--echo` 原样回发收到的数据；
  `--reply` 对每次收到数据回发固定文本；`--greeting` 在新客户端连接时发送。

### hid

```text
hid list [--filter TEXT] [--vid HEX] [--pid HEX]
hid send (--index N | --path-b64 B64 | --vid HEX --pid HEX [--serial S] [--interface N])
         (输入) [--report-id 0] [--report-length 64] [--read] [--read-length 64]
         [--timeout 1] [--repeat N] [--interval-ms N]
hid monitor (--index N | --path-b64 B64 | --vid/--pid ...) [--report-length 64] --duration N
```

- 复用 `list_devices`/`HidapiBackend`/`build_report`；不启动 `HidWorker`。
- `list` 返回 `index`、`path_b64`、`vid`、`pid`、`product`、`manufacturer`、`serial`、
  `usage_page`、`usage`、`interface`。`--index` 指最近一次 `hid list` 的顺序；
  需要跨调用稳定定位时用 `--path-b64`。
- `--vid/--pid`（未给 `--serial`/`--interface`）命中多台设备时返回
  `ambiguous_device`（退出码 3），`error.details` 给出候选的 `index`、`path_b64`、
  `serial`、`interface`，由 Agent 消歧后重试；不会静默取第一台。
- `send` 返回 `written`、`report`（实际写出的完整报告 hex）、可选 `input_report`。
- `monitor` 输出 `data` 事件与最终汇总；读超时按 `--duration` 结束。

### ble

```text
ble scan --duration N [--filter TEXT]
ble services --address ADDR [--timeout 30]
ble read --address ADDR --char UUID [--timeout 30]
ble write --address ADDR --char UUID (输入) [--no-response] [--mtu-chunk]
          [--timeout 30] [--repeat N] [--interval-ms N]
ble notify --address ADDR --char UUID --duration N [--timeout 30] [--until-hex HEX]
```

- 复用 `BleakBackend`（`asyncio.run` 驱动），每条命令独立「连接→操作→断开」。
- `scan` 返回 `name`、`address`、`rssi`。
- `services` 返回 `mtu` 与 `services[]{uuid,label,characteristics[]{uuid,label,properties,subscribed=false}}`；
  名称用 `service_label` / `characteristic_label`。
- `read` 返回 `hex`/`text`；`write` 返回 `chunks`、`bytes`；`--mtu-chunk` 用
  `chunk_bytes(data, mtu)`（MTU-3）。
- `notify` 是 NDJSON：`notification`（`char`、`hex`、`text`、`at`）+ `end`。

### spp

```text
spp scan --duration N [--filter TEXT] [--paired-only]
spp send (--address ADDR | --device-id ID) [--scan-duration 8] (输入)
         [--pin 1234] [--confirm] [--repeat N] [--interval-ms N]
         [--expect TEXT|--expect-hex HEX] [--timeout 30] [--read-for 1]
spp com --port COMx [--baud 115200] [conn opts] (输入) [--expect ...] [--timeout 5] [--read-for 1]
```

- 复用 `WinRtSppBackend`，prompt 回调由 CLI 提供：
  `--pin` 回答 `pin` 请求；`--confirm` 自动接受确认请求。未提供时返回 `None`，
  配对按拒绝处理，并给出「请先在系统设置中配对，或加 --pin/--confirm」的提示。
- `spp send` 若设备已配对则直接 RFCOMM；未配对时先配对，再连接。
  `--address` 会用一次短扫描（`--scan-duration`，默认 8 秒，找到即停）解析出
  `device_id` 与配对状态；`--device-id` 跳过扫描，适合先执行 `spp scan` 的 Agent。
- `spp com` 等价于使用系统蓝牙 COM 口的 `serial send`，复用 `PyserialTransport`。
- `scan` 返回 `name`、`address`、`paired`、`device_id`。

### config

```text
config show
config get --key KEY
config export --out PATH
config set --key KEY --value VALUE
config reset --yes
```

- 路径与便携模式复用 `resolve_paths` / `ensure_writable_paths`。
- `show` 返回 `config_path`、`log_dir`、`data_dir`、`portable`、`settings`。
- `set` 仅允许 `DEFAULT_SETTINGS` 中已存在的键，并按默认值的类型校验；
  `VALUE` 先按 JSON 解析，失败则按字符串。
- `export` 写当前加载后的配置 JSON（与 GUI 导出一致）。
- `reset` 写回默认配置；缺 `--yes` 为参数错误。
- CLI 不初始化文件日志，不写 `app.log`。

### update

```text
update check
update download
update install --yes (--latest | --file PATH --sha256 HEX)
```

- `check` 复用 `fetch_latest_release` + `is_newer`，返回 `current`、`latest`、`newer`、
  `title`、`notes`、`page_url`、`published_at`、`asset`。
- `download` 复用 `updates_dir(paths)` + `download_release`，返回本地路径、大小、SHA-256。
- `install`：
  - `--file` 必须同时给 `--sha256`（`update download` 会在 `data.sha256` 返回该值），
    用 `verify_sha256` 校验后才安装；不联网也能安装已下载的文件；
  - `--latest` 先检查并下载（期望值取自 Release digest / SHA256.txt），再安装；
  - 源码运行（无 `sys.frozen`）拒绝安装，提示「仅下载，不替换」；
  - 复用 `install_update` 启动 PowerShell 替换脚本，CLI 随后退出；脚本会重启 GUI。
  - 缺 `--yes` 为参数错误。

### system

```text
version
selftest [--report PATH]
```

- `version` 返回 `version`、`python`、`frozen`、`executable`。
- `selftest` 调用现有 `run_selftest`，把同一份报告打印到 stdout；给 `--report` 时额外写文件。
  保留 `--selftest --report` 旧入口不变。
- `selftest` 是「CLI 不写 app.log」的唯一例外：自检本身要验证 `app.log` 可写，必须初始化
  日志；这是自检的职责，不是普通命令的副作用。
- 退出码：报告 `ok=true` → 0；`ok=false` → 3。旧入口 `--selftest` 仍返回原有 0/1。

## 架构

```text
app/cli/
  __init__.py
  main.py            # argparse 根解析器、分流、JSON/文本输出、异常与退出码
  errors.py          # CliError、错误码映射
  output.py          # 信封、NDJSON、UTF-8 输出
  inputs.py          # --text/--hex/--file/--stdin、类型与范围校验
  transports/
    serial_io.py     # PyserialTransport + list_ports 的无头封装
    hid_io.py        # HidapiBackend 的无头封装
    ble_io.py        # BleakBackend + asyncio.run
    spp_io.py        # WinRtSppBackend + CLI prompt 回调
    net_io.py        # socket 版 TCP/UDP 客户端与服务端
  commands/
    checksum.py  convert.py  image.py  dotmatrix.py  c51.py
    serial.py    net.py      hid.py    ble.py        spp.py
    config.py    update.py   system.py
```

- `commands/*` 只做参数映射与结果组装；算法/IO 调用核心模块与 `transports/*`。
- `transports/*` 的工厂函数在模块级暴露，测试用 monkeypatch 注入假实现。
- 每个 command 模块暴露 `register(subparsers)` 与 `run(args) -> dict`；
  `main.py` 统一包装为信封与输出。
- 硬件命令按需导入 PyQt6 依赖的模块（`app.io.serial_worker` 等），离线命令不触发；
  即使导入，也不创建 `QApplication`、不启动 Qt 事件循环。
- 流式命令在 `run()` 内直接写 NDJSON，最后返回汇总字典。

## 打包与构建

`build/app.spec`：

- `console=True`、`hide_console="hide-early"`。
- 不新增 hiddenimports（标准库 socket、asyncio 无需额外收集；现有依赖已覆盖）。
- `build/build.ps1` 的隔离验证新增 CLI 检查：
  - `& $Exe version` 解析 JSON，版本等于 `1.2.0`；
  - `& $Exe checksum --text 123456789 --only CRC-32/ISO-HDLC` 期望
    `CBF43926`（大小写不敏感）；
  - `& $Exe convert base --value 255 --from-base 10 --bits 8` 期望十六进制 `FF`；
  - 保留现有 `--selftest`、`--smoke-test`。
- 构建日志增加 CLI 检查结果。
- 手工验收：双击 exe 不出现残留控制台窗口；从 PowerShell 运行 CLI 有输出；
  从资源管理器启动后 GUI 正常。

## 测试策略

- `tests/test_cli_core.py`：分流、JSON 信封、退出码、输入解析、checksum 已知向量、
  convert 各工具、image/dotmatrix/c51 输出到 `tmp_path`。
- `tests/test_cli_hardware.py`：注入假串口/HID/BLE/SPP 后端与假时钟；真实本地
  TCP/UDP 回环覆盖 `net tcp`、`net udp`、`net server` 的发送、等待、超时、NDJSON。
- `tests/test_cli_entry.py`：`main()` 在 CLI 参数下不导入 GUI、`--selftest` 保持原行为、
  argparse 错误输出 JSON、UTF-8 输出。
- `tests/test_cli_config_update.py`：`config show/get/set/reset/export`，更新检查与
  `update install` 的 `--yes`/源码模式拒绝（不联网、不真替换）。
- 现有 755 项测试必须全部通过；新测试计入基线并同步 README/BUILDING 的计数。
- 不依赖真实硬件；真实串口/网络/HID/蓝牙仍按 `docs/BUILDING.md` 的手工验收清单执行，
  并新增 CLI 对应条目。

## 文档与版本

- 新增 `docs/CLI.md`：完整命令参考、JSON 契约、Agent 调用示例（校验、图片批处理、
  串口一问一答、TCP 回环、BLE 读写、C51 导出）。
- `README.md`：功能总览加「AI Agent CLI」行；快速开始加 CLI 示例；测试计数更新。
- `docs/USAGE.md`：新增「命令行」一节并链接 `docs/CLI.md`。
- `docs/ARCHITECTURE.md`：加入 `app/cli` 分层与单 exe 双模式说明。
- `docs/BUILDING.md`：说明 `console=True` + `hide_console`、CLI 验证步骤、测试计数。
- `CHANGELOG.md`：新增 `[1.2.0]`，记录 CLI、单 exe 双模式、打包变更。
- `app/__init__.py` 与 `build/version_info.txt` 升到 `1.2.0`。

## 风险与缓解

1. **控制台子系统改变双击体验**：依靠 `hide_console="hide-early"` 只在程序自己拥有控制台时
   隐藏；验收必须覆盖「双击」「PowerShell 启动」「Agent 管道捕获」三种场景。
2. **onefile 每次调用重新解压，CLI 启动不是零成本**：离线命令不导入 PyQt6 以缩短 Python
   阶段；解压开销无法消除，文档如实说明，不承诺毫秒级启动。
3. **硬件命令复用 worker 模块会间接导入 QtCore**：只导入类、不建 `QApplication`；
   若未来需要彻底去 Qt，可把后端类拆到 Qt 无关模块（本次不做，避免动 GUI）。
4. **网络命令与 GUI 的 QtNetwork 行为不完全相同**：CLI 用标准 socket，错误文案按 Windows
   常见错误映射；测试覆盖拒绝连接、超时、DNS 失败。
5. **SPP 无头配对涉及系统授权**：默认不自动接受，必须显式 `--pin`/`--confirm`；
   失败时提示先在系统设置中配对。
6. **更新安装会重启 GUI**：`update install` 必须 `--yes`，源码模式拒绝；文档明确说明。
7. **流式命令产生大量输出**：强制 `--duration`，并提供 `--until`/`--max-bytes`；
   不提供无限模式。

## 留给实现者的判断

- argparse 子解析器的组织方式、命令模块注册的具体写法。
- JSON 字段的次要命名（如 `hex`/`hex_text`）、text 格式的排版。
- 假后端的注入粒度、测试夹具的拆分。
- 网络错误文案的具体映射表、`--expect` 的超时轮询间隔。
- 构建脚本里 CLI 验证的具体断言写法。
- 是否为 `help` 增加机器可读的命令清单（若实现成本低可做，但不影响验收）。
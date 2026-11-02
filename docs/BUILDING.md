# 构建与打包

本项目在 Windows 11 + Python 3.13.9 上验证。命令默认在仓库根目录执行。

## 环境要求

| 项 | 版本/说明 |
| --- | --- |
| 操作系统 | Windows 10/11 x64（主要验证 Windows 11） |
| Python | 3.13（验证 3.13.9） |
| 包管理 | pip |
| 可选 | ffmpeg（MP3→WAV 解码；不装仍支持 WAV） |
| 权限 | 普通用户即可，不需要管理员 |

项目**不要求 venv**，下面的命令直接使用系统 Python。若你希望隔离环境，可自行创建 venv，不影响构建脚本。

## 安装依赖

```bash
python -m pip install -r requirements.txt
```

核心依赖：PyQt6、PyQt6-Fluent-Widgets、pyserial、psutil、Pillow、numpy、hidapi、qrcode、bleak、winrt-Windows.*（蓝牙，仅 Windows）；测试与打包：pytest、PyInstaller。

确认 qfluentwidgets 当前绑定的是 PyQt6：

```bash
python -c "from PyQt6.QtWidgets import QWidget; from qfluentwidgets import FluentWindow; print(issubclass(FluentWindow, QWidget))"
```

预期输出 `True`。如果本机同时安装 PyQt5/PyQt6 两套 Fluent-Widgets，后安装的发行包会覆盖同一个 `qfluentwidgets/` 目录；启动自检会在正常启动前检查绑定，失败时给出明确提示。

## 从源码运行

```bash
python main.py
```

首次启动会在 `%APPDATA%\PortHelperFluent\` 创建配置和日志。便携模式：在 exe/入口所在目录放一个 `portable.flag`，配置和日志写入 `data\`。

同一个入口也提供命令行：带子命令参数时进入无界面 CLI，不创建 `QApplication`，默认输出 JSON：

```bash
python main.py version
python main.py checksum --text 123456789 --only CRC-32/ISO-HDLC
python main.py serial list
```

完整命令与 Agent 示例见 [CLI 文档](CLI.md)。

隐藏自检（旧入口保持不变）：

```bash
python main.py --selftest --report selftest.json
python main.py --smoke-test
```

## 运行测试

```bash
python -m pytest
```

测试默认离屏（`tests/conftest.py` 设置 `QT_QPA_PLATFORM=offscreen`），覆盖：

- 校验已知向量、CRC 独立参考、C51 寄存器、RGB565/RGB332、扫描/位序；
- 配置损坏恢复、便携目录回退、日志上限、截断提示；
- 本地 TCP/UDP 回环、TCP 服务端多客户端、UDP 域名队列；
- 串口/HID 假传输、蓝牙 BLE/SPP 假设备、页面离屏实例化、真实按钮点击；
- CLI 命令注册、JSON 信封与退出码、输入解析、流式命令有界结束、假后端与本机 TCP/UDP 回环。

当前基线：**1226 项测试**（含 CLI 离线/假后端/本机回环测试；图形环境受限时可 `python -m pytest --ignore=tests/test_main_window.py`，通过 1203 项）。硬件 I/O（串口/HID/蓝牙）、Keil 打开工程属于手工验收。

单独跑某类测试：

```bash
python -m pytest tests/test_serial_worker.py -v
python -m pytest tests/test_net_client.py tests/test_net_server.py -v
python -m pytest tests/test_imaging.py tests/test_fontmatrix.py tests/test_c51.py -v
python -m pytest tests/test_cli_core.py tests/test_cli_entry.py tests/test_cli_serial.py tests/test_cli_hid.py tests/test_cli_ble.py tests/test_cli_net.py tests/test_cli_spp.py -v
python -m pytest tests/test_ui_layout.py tests/test_main_window.py -v
```

## 命令行（CLI）验证

CLI 与 GUI 是同一个 exe，构建后可直接在终端验证（不需要打开窗口）：

```powershell
dist\PortHelperFluent.exe version | Out-String
dist\PortHelperFluent.exe checksum --text 123456789 --only "CRC-32/ISO-HDLC" | Out-String
dist\PortHelperFluent.exe convert base --value 255 --from-base 10 --bits 8 | Out-String
dist\PortHelperFluent.exe help
```

期望：`version` 的 `data.version` 为 `1.2.0`；checksum 返回 `CBF43926`；`convert base` 的 16 进制输出为 `FF`；stdout 是 JSON 信封，退出码 0。参数错误（例如缺少子命令）应输出 `{"ok": false, ...}` 并以退出码 2 结束，stderr 有一行摘要。

exe 是 GUI 子系统（`console=False`），交互式 PowerShell 里直接调用**不会等待进程**，所以上面用 `| Out-String` 强制等待并收集输出；需要显式退出码时用 `& dist\PortHelperFluent.exe version | Out-String; $LASTEXITCODE` 或 `Start-Process -Wait -PassThru`。Agent 的 `subprocess.run(..., capture_output=True)` 天然等待，无需管道。

`build/build.ps1` 会在隔离临时目录里对交付副本执行同样的三项断言，并在 `dist/build.log` 的 `Packaged verification` 段记录 `cli version` / `cli checksum` / `cli convert base`；构建完成后还会检查 `dist/` 下**恰好一个 exe**。

## 构建单文件 exe

```powershell
powershell -ExecutionPolicy Bypass -File build/build.ps1
```

脚本会：

1. 清理 `dist/` 与 `build_work/`；
2. 运行全量 pytest（失败即中止）；
3. 重新生成 `build/icon.ico`；
4. 用 `build/app.spec` 构建 onefile 图形程序（GUI 子系统，不带控制台）；
5. 检查 `dist/` 下恰好一个 exe；
6. 校验归档中不含 `analysis/`、`docs/`、`tests/`、`.git`、PyQt5、WebEngine；
7. 从隔离临时目录执行 `--selftest`、`--smoke-test`，并对交付副本执行 `version`、`checksum`、`convert base` 三项 CLI 断言；
8. 写出 `dist/SHA256.txt` 与 `dist/build.log`。

产物：

```text
dist\PortHelperFluent.exe
dist\SHA256.txt
dist\build.log
```

示例产物约 60 MB，具体大小/SHA 以构建日志为准。构建脚本不会提交二进制；`dist/` 已在 `.gitignore` 中。

### GUI 子系统与双击行为

`build/app.spec` 使用 `console=False`（GUI 子系统），让同一个 exe 同时服务 GUI 与 CLI：

- 双击：不创建任何控制台/终端，直接显示 GUI，不会残留窗口。
- CLI 走继承的标准流：从 PowerShell/cmd 调用时 stdout/stderr 与退出码正常可用；Agent 用 `subprocess.run(..., capture_output=True)` 会被自动等待，能完整拿到 JSON 与退出码。
- 交互式 PowerShell 直接调用 GUI 子系统的 exe 不会等待进程：调用运算符立即返回，`$LASTEXITCODE` 可能是旧值。需要等待结果/拿退出码时必须用管道或显式等待，例如 `& dist\PortHelperFluent.exe version | Out-String`、`Start-Process -Wait -PassThru`（构建脚本里的 `Invoke-CliJson` 就是用 `| Out-String` 强制等待）。
- 为什么放弃 `hide_console="hide-early"`：Windows 11 的默认终端是 Windows Terminal（窗口类 `CASCADIA_HOSTING_WINDOW_CLASS`），窗口属于 Terminal 进程；PyInstaller 的 hide 只操作 bootloader 自建的控制台宿主窗口，无法隐藏/关闭 Terminal 窗口，双击或命令行调用仍会残留常驻终端。改用 GUI 子系统后双击根本不创建控制台，问题从根上消失。
- 极少见的无标准流场景（例如从没有标准句柄的宿主直接拉起 CLI）：`app.cli.output.ensure_console_streams()` 在 `sys.stdout`/`sys.stderr` 缺失时才 `AttachConsole(-1)`，失败再 `AllocConsole()`，并用 `CONOUT$` 重建缺失的流；两个流都在时直接返回，绝不附着控制台（否则会把输出从管道挪到控制台，破坏 Agent 捕获）。所有 ctypes 异常静默。
- 限制：onefile 每次 CLI 调用都要重新解压运行时，启动有固定开销；CLI 不提供常驻/会话复用。
- 验收：双击无控制台/终端窗口、GUI 正常；PowerShell 直接调用有 JSON 输出；`| Out-String` 与 `Start-Process -Wait` 能等到退出码；Agent 管道捕获正常。

### 手动打包（等价命令）

```powershell
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build_work build/app.spec
```

`build/app.spec` 已包含 qfluentwidgets 资源、hidapi、qrcode、Pillow、numpy、psutil、pyserial、bleak 与 winrt，以及图标/版本信息。bleak 与 winrt 用 `collect_submodules` 收集全部子模块——WinRT 的实现是 `.pyd`，modulegraph 跟不进去，漏收会让 exe 启动即崩。不要额外关闭这些依赖，否则运行时会缺模块。不要在 spec 里改回 `console=True` + `hide_console`：Windows 11 的默认终端是 Windows Terminal，hide 只能操作 bootloader 自建的控制台宿主窗口，无法关闭 Terminal 窗口，会留下常驻终端；CLI 的等待问题由调用方用管道或 `Start-Process -Wait` 解决。

## 发布 Release

内置更新器只读 `https://api.github.com/repos/Yaopyr/PortHelperFluent/releases/latest`，并按固定资产名下载，因此发布顺序不能省略任何一步。

1. **版本一致性**：`app/__init__.py` 的 `APP_VERSION` 与 `build/version_info.txt` 的 `filevers`/`prodvers`/`FileVersion`/`ProductVersion` 必须是同一个版本（本项目为 `1.2.0`）；`CHANGELOG.md` 与 `README.md` 的测试计数同步更新。`version_info.txt` 是手工维护的独立副本，不会从 `APP_VERSION` 自动生成。
2. **构建**：`powershell -ExecutionPolicy Bypass -File build/build.ps1`，确认全量测试通过、隔离目录 `--selftest`/`--smoke-test` 退出码为 0、`version`/`checksum`/`convert base` 三项 CLI 断言通过，然后确认 `dist/` 下只有一个 exe，并用下面命令确认 exe 里的版本资源：

   ```powershell
   (Get-Item dist\PortHelperFluent.exe).VersionInfo | Select-Object FileVersion, ProductVersion
   ```

   期望两者均为 `1.2.0`（或 `1.2.0.0`）。`dist/SHA256.txt` 只是 exe 的校验和、`dist/build.log` 是构建日志，两者都不含应用版本行，不能用来核对版本。
3. **打标签**：
   ```powershell
   git tag -a v1.2.0 -m "PortHelper Fluent 1.2.0"
   ```
4. **推送**：
   ```powershell
   git push origin main
   git push origin v1.2.0
   ```
5. **创建 Release**（`notes.md` 内容取自 `CHANGELOG.md` 的 `[1.2.0]` 节）：
   ```powershell
   gh release create v1.2.0 dist/PortHelperFluent.exe dist/SHA256.txt `
     --title "PortHelper Fluent 1.2.0" --notes-file notes.md
   ```
6. **校验**：
   ```powershell
   gh release view v1.2.0 --json tagName,isDraft,assets
   ```
   期望 `tagName` 为 `v1.2.0`、`isDraft` 为 `false`、资产正好两个。

**资产名必须严格为 `PortHelperFluent.exe` 与 `SHA256.txt`**：更新器按这两个名字匹配（exe 大小写不敏感，找不到同名 exe 才回退第一个 `.exe`；校验优先用 Release 元数据的 `sha256:` digest，否则下载 `SHA256.txt`）。改名、少传 `SHA256.txt` 或把资产打包成 zip 都会让在线更新失效。

发布后可用一条命令验证线上元数据：

```powershell
python -c "from app.services.updates import fetch_latest_release, is_newer; r=fetch_latest_release(); print(r.version, r.exe.name, r.exe.size, r.exe.digest); print('newer:', is_newer(r.version))"
```

回滚：`gh release delete v1.2.0 --yes`、`git push origin :refs/tags/v1.2.0`，再重新打标签发布。

## 打包后自检

```powershell
dist\PortHelperFluent.exe --selftest --report selftest.json
dist\PortHelperFluent.exe --smoke-test
```

`--selftest` 写入 JSON：

```json
{
  "ok": true,
  "errors": [],
  "python": "3.13.9",
  "qt": "6.11.0",
  "qfluentwidgets": "1.11.2",
  "version": "1.2.0"
}
```

`--smoke-test` 会创建主窗口、显示 1.5 秒后退出，退出码 0 表示启动链路正常。

从独立目录验证（不依赖源码目录）：

```powershell
$tmp = Join-Path $env:TEMP "porthelper-verify"
New-Item -ItemType Directory -Force $tmp | Out-Null
Copy-Item dist\PortHelperFluent.exe $tmp
Push-Location $tmp
$env:APPDATA = Join-Path $tmp "appdata"
$env:PORTHELPER_NO_DIALOGS = "1"
.\PortHelperFluent.exe --selftest --report selftest.json
.\PortHelperFluent.exe --smoke-test
Pop-Location
```

## 常见问题

### 启动时报 qfluentwidgets 不是 PyQt6 版本

本机同时安装了两套 Fluent-Widgets，实际生效的是 PyQt5 版。重新安装 PyQt6 版：

```bash
python -m pip install --force-reinstall PyQt6-Fluent-Widgets==1.11.2
```

### 无法导入 hid / qrcode

```bash
python -m pip install hidapi==0.15.0 qrcode==8.2
```

### 无法导入 bleak / winrt（蓝牙页报错，或 exe 启动即崩）

这四组包只在 Windows 上有可用后端：

```bash
python -m pip install bleak==3.0.2 winrt-Windows.Devices.Bluetooth.Rfcomm==3.2.1 winrt-Windows.Networking==3.2.1 winrt-Windows.Networking.Sockets==3.2.1
```

打包后如果漏收 WinRT 的 `.pyd`，`--selftest` 会报「依赖 winrt.windows.… 导入失败」，构建脚本会以「module not available: winrt.windows.networking.sockets」中止；确认 `build/app.spec` 里的 `collect_submodules("bleak") + collect_submodules("winrt")` 没被删掉。

### MP3 转换提示缺少 ffmpeg

安装 ffmpeg 并加入 PATH；或改用 WAV。WAV 只支持 PCM。

### 串口打开失败

检查端口是否被其他程序占用、设备是否已连接、是否被防火墙/安全软件拦截。程序会显示具体原因；详细技术信息在 `logs/app.log`。

### 打包后缺 qfluentwidgets 图标/字体

确认使用仓库的 `build/app.spec`，不要手工删掉 `collect_data_files('qfluentwidgets')`。构建日志中的 PyInstaller 警告可查看 `build_work/app/warn-app.txt`。

### 构建时 PyInstaller 报可选 scipy/colorthief 警告

qfluentwidgets 的 acrylic 辅助代码引用了可选模块；本项目使用 Mica/纯色降级，scipy 被有意排除。只要 `--selftest`/`--smoke-test` 通过，该警告不影响交付。

## 手工验收清单

自动测试无法替代真实硬件。发布前建议逐项确认：

- [ ] 真实串口：枚举、打开/关闭、波特率、DTR/RTS、BREAK、文本/HEX 收发、连续发送、文件发送、会话导出。
- [ ] 真实 HID：枚举、VID/PID/产品名、报告 ID/长度、读写、拔出自动复位。
- [ ] 蓝牙：BLE 扫描/连接/服务树/读/写/通知订阅（可与手机端工具比对），SPP 一键配对（PIN 弹窗与配对码确认）、RFCOMM 收发、系统蓝牙 COM 备用通道，断开后界面复位。
- [ ] 网络：TCP 连接/断开/被拒绝、UDP 绑定/收发、域名解析、Ping、网卡信息、服务端多客户端与广播。
- [ ] 校验：与独立工具对比 CRC/Sum/Intel HEX/TCP-IP 结果。
- [ ] 图片：C/BIN/LVGL 导出在目标屏或查看器中正确。
- [ ] 点阵：字体、扫描方向、位序、分页、单元格编辑后的导出正确。
- [ ] C51：Keil 打开 `M51Project.Uv2`，编译 `Main.c`；核对定时/波特率。
- [ ] 设置：浅色/深色、强调色、日志上限、配置导出/重置、便携模式。
- [ ] exe：从无 Python 环境的目录启动，`--selftest`/`--smoke-test` 通过。
- [ ] CLI：双击 exe 不出现控制台/终端窗口；PowerShell 运行 `dist\PortHelperFluent.exe checksum --text 123456789 --only "CRC-32/ISO-HDLC" | Out-String` 有 JSON 输出且退出码为 0；Agent 用 `subprocess.run(capture_output=True)` 能拿到同一份输出；串口/HID/蓝牙命令用真实设备执行 `list`/`send` 一次。

# 软件模拟验证套件

用有状态虚拟设备替代真实串口/HID/BLE/SPP，用本机回环替代外部网络，在离屏 Qt 中启动真实 `MainWindow` 与真实 worker，逐页验证 PortHelper Fluent 的功能接线、状态机、错误路径、导出与退出清理。

## 运行

在仓库根目录（PowerShell）：

```powershell
# 只跑仿真，不跑现有基线
python 'C:\Users\Yaopyr\Documents\Code\Projects\PortHelperFluent\tools\run_simulation.py' --strict --screenshots always

# 仿真 + 现有 pytest 基线
python 'C:\Users\Yaopyr\Documents\Code\Projects\PortHelperFluent\tools\run_simulation.py' --strict --screenshots always --with-baseline

# 只跑某组，例如串口/蓝牙
python 'C:\Users\Yaopyr\Documents\Code\Projects\PortHelperFluent\tools\run_simulation.py' --only SER --only BT
```

不要用默认 `python -m pytest` 跑仿真；默认 `pytest.ini` 已忽略 `tests/simulation`，以保持原有测试基线不变。直接运行仿真请使用 `pytest -c pytest-sim.ini tests/simulation`。

## 状态定义

| 状态 | 含义 |
|---|---|
| PASS | 场景有断言证据且通过 |
| FAIL | 断言失败、线程未清理、报告生成失败或应用真实缺陷 |
| SKIP | 环境缺字体/ffmpeg 等，报告写明原因，不计入 PASS |
| MANUAL | 真实硬件、Keil、更新替换、音频听感、打包 exe 等人工验收 |
| N/A | 文档明确不支持（CH372、内核串口监视、Keil 配色替换、独立蓝牙助手） |

任何 SKIP/MANUAL 都不会被计为 PASS；没有断言证据的场景不会判 PASS。

## 报告

默认输出到 `validation-reports/sim-<时间戳>/`：

- `report.md`：中文详细报告；
- `report.json`：机器可读结果；
- `results.jsonl`：逐场景增量结果；
- `screenshots/`：页面与关键状态截图；
- `artifacts/`：虚拟设备快照与导出文件证据；
- `baseline.json` / `baseline.log`：使用 `--with-baseline` 时的现有回归基线。

`validation-reports/latest.txt` 指向最近一次报告。生成物不入库。

## 验证边界

- 虚拟串口/HID/BLE/SPP 验证应用逻辑与协议接线，不验证驱动、芯片兼容性和真实时序。
- TCP/UDP 只连 `127.0.0.1`；Ping/DNS/网卡/更新检查/下载/安装全部注入，不联网、不执行 PowerShell 自我替换。
- `--selftest`/`--smoke-test` 子进程按真实入口运行；`--smoke-test` 会只读枚举本机硬件。
- 真实串口/HID/蓝牙、Keil 编译、真实更新替换、声音播放、打包 exe 在无 Python 环境运行，见 [manual-acceptance.md](manual-acceptance.md)。

## 设计约束

- 生产代码零改动；所有替换在构造 `MainWindow` 之前通过模块级 monkeypatch 注入。
- 不新增 pip 依赖；使用 pytest、PyQt6.QtTest 与现有 `process_events_until`。
- 每个场景使用独立临时目录、独立 `Settings` 与虚拟设备 hub，结束时等待线程退出并恢复主题。
- 负控制位于 `tests/simulation/test_selfcheck.py`，用于证明场景能捕获丢写、通知抑制和错误校验结果。

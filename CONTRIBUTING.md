# 贡献指南

感谢你愿意改进 PortHelper Fluent。提交前请先阅读以下约定。

## 开发环境

- Windows 10/11 x64，Python 3.13。
- 不要求 venv；本项目在系统 Python 3.13.9 验证。
- 安装依赖：

```bash
python -m pip install -r requirements.txt
```

- 可选安装 ffmpeg（MP3 解码）并加入 PATH。

## 运行与测试

```bash
python main.py
python -m pytest
```

打包前请额外运行：

```powershell
powershell -ExecutionPolicy Bypass -File build/build.ps1
```

详细构建、自检与手工验收见 [docs/BUILDING.md](docs/BUILDING.md)。

## 架构约定

- `app/core` 不导入 Qt，保持可直接单测。
- `app/services` 不放页面逻辑；`app/io` 不直接操作控件。
- 页面构造契约：`page(settings, paths, parent=self)`；需要注入依赖时使用关键字参数。
- 页面应实现 `has_active_work()` 与 `stop_workers()`，主窗口退出时统一调用。
- 所有 Qt 槽函数使用 `safe_slot` 或自行捕获异常；PyQt6 槽内未捕获异常会导致进程静默退出。
- 新页面在 `app/ui/main_window.py` 的 `_PAGE_SPECS` 注册；新转换工具在 `app/ui/pages/convert/page.py` 的 `TOOL_CLASSES` 注册。

更完整的分层与线程模型见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 代码风格

- Python：type hints、pathlib、4 空格；命名英文，界面文案中文。
- 只写必要的注释：解释「为什么」，不要复述代码做了什么。
- 不引入未使用的依赖；新依赖必须写入 `requirements.txt` 并在 `build/app.spec` 中正确收集。
- UI 变更需在浅色/深色下检查；不要用离屏截图单独证明 GUI 正确性，状态断言与手工验收结合。
- 文件保持单一职责；新增代码尽量不要让单文件继续膨胀。

## 测试

- 修 bug：先写能复现的失败测试，再修复。
- 新算法：使用公开已知向量或独立参考实现，避免用同一公式自证。
- 网络/串口/HID：优先注入假传输或本地回环，不依赖真实硬件。
- 测试必须保持隔离：`tests/conftest.py` 会把 `APPDATA` 指向临时目录。
- 提交前运行全量 `python -m pytest`，不要只跑新增测试。

当前基线：683 项测试。硬件相关路径（真实串口/HID/Keil）属于手工验收。

## 提交与 PR

- 提交信息使用前缀：`feat:`、`fix:`、`docs:`、`test:`、`build:`、`refactor:`。
- 一个 PR 聚焦一个主题；不要把无关重构混进来。
- PR 描述包含：改了什么、为什么、如何验证、已知限制。
- 不要提交 `dist/`、`build_work/`、`__pycache__/`、`.pytest_cache/`、原始逆向资源。
- 不要提交原程序的二进制、图标、截图、驱动、蓝牙助手或反编译产物。
- 不要强制推送 `main`；不要修改全局 git 配置。

## 逆向/法律边界

- 本项目是独立 Python 实现；原 PortHelper 版权归温子祺所有。
- 只参考功能事实、公开标准与自制资源；不要复制原程序代码/资产。
- 新增逆向证据时，优先提交文本/脚本，不要提交原始二进制资源；证据不得包含个人绝对路径或隐私。
- 详见 [docs/REVERSE_ENGINEERING.md](docs/REVERSE_ENGINEERING.md)。

## Issue

提交 Issue 时请附：

- 版本/提交号、Windows 版本、Python/Qt 版本；
- 使用的页面与设备（串口型号、HID VID/PID、网络对端等）；
- 复现步骤、预期与实际结果；
- 日志片段（`%APPDATA%\PortHelperFluent\logs\app.log`），注意先移除敏感信息。

## 许可

提交代码即表示你同意以本项目的 [MIT License](LICENSE) 授权你的贡献。原程序及其原始资源不适用该许可。

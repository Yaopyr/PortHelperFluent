"""面向 AI Agent 的命令行层：不依赖 PyQt6，可被单文件 exe 的 CLI 模式直接调用。

模块划分（早期任务只建立基础契约，命令模块由后续任务逐步加入）：

- ``errors``：稳定的错误码、退出码与 :class:`CliError`；
- ``output``：JSON 信封、NDJSON 事件与 UTF-8 标准流；
- ``inputs``：``--text/--hex/--file/--stdin`` 互斥输入；
- ``reads``：有界的等待读取与流式选项；
- ``main``：根解析器、``help``/``--version`` 归一与错误→退出码收敛；
- ``commands``：各命令组（当前只有 ``system`` 的 ``version``/``selftest``）。

本包顶层不导入任何子模块与 Qt，避免离线命令启动时产生额外开销；顶层只放
``CLI_GROUPS`` 与 :func:`is_cli_invocation`，供根 ``main.py`` 在导入 GUI 依赖前分流。
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["CLI_FLAGS", "CLI_GROUPS", "is_cli_invocation"]

# 所有 CLI 命令组（含 ``help``）。命令组由后续任务逐个注册；在某个组补齐前，
# 例如 ``PortHelperFluent.exe checksum`` 会先报用法错误（退出码 2），这是预期行为。
CLI_GROUPS = frozenset(
    {
        "checksum",
        "convert",
        "image",
        "dotmatrix",
        "c51",
        "serial",
        "net",
        "hid",
        "ble",
        "spp",
        "config",
        "update",
        "selftest",
        "version",
        "help",
    }
)

# 顶层帮助/版本开关：与命令组一样直接进入 CLI 分支，其余参数保持 GUI 启动路径。
CLI_FLAGS = frozenset({"-h", "--help", "-V", "--version"})


def is_cli_invocation(argv: Sequence[str]) -> bool:
    """只看首个参数：命中命令组或帮助/版本开关时才走 CLI。

    无参数、``--selftest``、``--report``、``--smoke-test`` 都保持原有 GUI 启动路径。
    """
    if not argv:
        return False
    return argv[0] in CLI_GROUPS or argv[0] in CLI_FLAGS

"""CLI 错误类型与固定退出码。

退出码对 Agent 是契约的一部分，任何命令都只能使用这几个值；``code`` 是稳定的
英文枚举，``message`` 沿用现有模块的中文错误文案，不翻译、不改写。
"""

from __future__ import annotations

from typing import Any

# 成功。
EXIT_OK = 0
# 未预期异常（同时向 stderr 输出一行摘要，不打印 traceback）。
EXIT_INTERNAL = 1
# 参数缺失或非法（含 argparse 解析失败）。
EXIT_USAGE = 2
# 输入、环境或操作失败（not_found/invalid_input/device_unavailable 等）。
EXIT_OPERATION = 3
# 等待回包超时或 --expect 未命中。
EXIT_EXPECT = 4
# 用户中断（Ctrl+C）。
EXIT_INTERRUPTED = 130

__all__ = [
    "CliError",
    "EXIT_EXPECT",
    "EXIT_INTERNAL",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_OPERATION",
    "EXIT_USAGE",
]


class CliError(Exception):
    """可预期的 CLI 失败：携带稳定错误码、中文消息、退出码与可选细节。

    ``main`` 会把异常收敛成标准错误信封；``details`` 用于让 Agent 消歧，例如
    ``ambiguous_device`` 附上候选设备列表。
    """

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int = EXIT_OPERATION,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details

    def __str__(self) -> str:
        return self.message

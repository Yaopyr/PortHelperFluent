"""CLI 根入口：参数分流、命令注册、JSON 输出与退出码收敛。

根 ``main.py`` 在导入任何 GUI 依赖前用 :func:`app.cli.is_cli_invocation` 分流；
本模块只依赖标准库与 Qt 无关的 CLI 原语，命令模块按已实现的部分逐个注册。

- ``help`` / ``--help`` 直接打印 argparse 文本，是唯一不输出 JSON 的路径；
- ``-V`` / ``--version`` 归一成 ``version`` 子命令；
- argparse 错误、``CliError``、未预期异常都输出错误信封；``command`` 为 ``"cli"``
  或命中的命令名，退出码 0/1/2/3/4/130。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from typing import Any, NoReturn

from app.cli.commands import checksum, convert, image, system
from app.cli.errors import EXIT_INTERNAL, EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE, CliError
from app.cli.output import configure_stdio, envelope_error, envelope_ok, write_payload

__all__ = ["build_parser", "run"]

# 顶层版本开关：等价于 ``version`` 子命令。
_VERSION_FLAGS = ("-V", "--version")


class _Parser(argparse.ArgumentParser):
    """把 argparse 的用法错误转成 :class:`CliError`，由 ``run`` 输出 JSON 信封。

    argparse 默认往 stderr 打印用法并 ``exit(2)``，Agent 只能读到非结构化文本；
    这里统一改成 ``usage``/2，``help`` 走的 ``--help`` 仍然直接打印文本并以 0 退出。
    """

    def error(self, message: str) -> NoReturn:
        raise CliError("usage", f"参数错误：{message}", EXIT_USAGE)


def build_parser(
    selftest_report: Callable[[], dict[str, Any]] | None = None,
) -> argparse.ArgumentParser:
    """构造根解析器并注册已实现的命令组。

    ``selftest_report`` 由根 ``main()`` 注入（冻结后入口是 ``__main__``，命令不能再
    ``import main``）；源码/测试直接调用时留给命令自己惰性兜底。
    """
    parser = _Parser(
        prog="PortHelperFluent",
        description="PortHelper Fluent 命令行：默认输出 JSON，供 AI Agent 与脚本调用。",
    )
    subparsers = parser.add_subparsers(title="命令组", metavar="<command>")
    system.register(subparsers, selftest_report=selftest_report)
    checksum.register(subparsers)
    convert.register(subparsers)
    image.register(subparsers)
    return parser


def run(
    argv: list[str],
    *,
    selftest_report: Callable[[], dict[str, Any]] | None = None,
) -> int:
    """执行一次 CLI 调用并返回进程退出码；stdout 只输出 JSON（``help`` 除外）。"""
    configure_stdio()
    started = time.perf_counter()
    parser = build_parser(selftest_report=selftest_report)
    try:
        args = parser.parse_args(_normalize_argv(list(argv)))
    except SystemExit as exc:  # ``help`` / ``--help`` 打印文本后以 0 退出
        return _exit_code(exc)
    except CliError as exc:
        return _finish_error("cli", exc, started)

    command = getattr(args, "command", "cli")
    fmt = getattr(args, "format", "json")
    pretty = bool(getattr(args, "pretty", False))
    handler = getattr(args, "handler", None)
    if handler is None:
        return _finish_error(
            "cli",
            CliError("usage", "缺少子命令：请先运行 help 查看可用命令。", EXIT_USAGE),
            started,
        )
    try:
        data = handler(args)
    except CliError as exc:
        return _finish_error(command, exc, started, fmt=fmt, pretty=pretty)
    except KeyboardInterrupt:
        return _finish_error(
            command,
            CliError("interrupted", "已中断（Ctrl+C）。", EXIT_INTERRUPTED),
            started,
            fmt=fmt,
            pretty=pretty,
        )
    except SystemExit as exc:
        # 命令自己 ``sys.exit()``：0 视为成功；非 0 也必须是完整信封（internal/1），
        # 不能直接返回原始退出码让 stdout 空着。
        if _exit_code(exc) == EXIT_OK:
            return EXIT_OK
        return _finish_error(
            command,
            CliError(
                "internal",
                f"命令以 SystemExit({exc.code!r}) 提前结束，未返回结果。",
                EXIT_INTERNAL,
            ),
            started,
            fmt=fmt,
            pretty=pretty,
        )
    except Exception as exc:  # 未预期异常只给一行摘要，不打 traceback
        summary = f"命令执行失败（{type(exc).__name__}）：{exc}"
        return _finish_error(
            command,
            CliError("internal", summary, EXIT_INTERNAL),
            started,
            fmt=fmt,
            pretty=pretty,
        )

    write_payload(envelope_ok(command, data, _elapsed_ms(started)), fmt=fmt, pretty=pretty)
    return EXIT_OK


def _normalize_argv(argv: list[str]) -> list[str]:
    """把 ``help`` 与 ``-V``/``--version`` 归一成 argparse 能直接处理的参数。"""
    if not argv:
        return argv
    first, rest = argv[0], argv[1:]
    if first == "help":
        return ["--help"] if not rest else [rest[0], "--help"]
    if first in _VERSION_FLAGS:
        return ["version"]
    return argv


def _exit_code(exc: SystemExit) -> int:
    """``SystemExit(None)`` 视作成功；非整型退出码收敛成 ``internal``。"""
    code = exc.code
    if code is None:
        return EXIT_OK
    return code if isinstance(code, int) else EXIT_INTERNAL


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _finish_error(
    command: str,
    error: CliError,
    started: float,
    *,
    fmt: str = "json",
    pretty: bool = False,
) -> int:
    """向 stderr 写一行摘要、向 stdout 写错误信封，并返回该错误的退出码。"""
    _write_stderr(f"错误（{error.code}）：{error.message}")
    write_payload(envelope_error(command, error, _elapsed_ms(started)), fmt=fmt, pretty=pretty)
    return error.exit_code


def _write_stderr(message: str) -> None:
    """窗口化 exe 的 stderr 可能不可用：写摘要失败时静默跳过。"""
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(message + "\n")
        stream.flush()
    except Exception:
        pass

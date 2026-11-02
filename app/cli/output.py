"""CLI 输出：标准信封、NDJSON 事件与 UTF-8 标准流。

stdout 始终是机器可读的（JSON 或流式 NDJSON）；``--format text`` 只供人工查看，
Agent 不应依赖其排版。
"""

from __future__ import annotations

import ctypes
import json
import sys
from typing import Any

from app import APP_VERSION
from app.cli.errors import CliError

__all__ = [
    "add_output_options",
    "configure_stdio",
    "emit_event",
    "ensure_console_streams",
    "envelope_error",
    "envelope_ok",
    "write_payload",
]


# `sys.stdout.encoding` 可能写成 `utf-8` / `UTF8` / `utf_8`，按下表归一后比较。
_UTF8_ENCODINGS = frozenset({"utf-8", "utf8"})


def ensure_console_streams() -> None:
    """GUI 子系统的 exe 在缺少标准流时兜底接上控制台（仅 Windows）。

    ``console=False`` 的 exe 被 shell/Agent 调用时继承父进程的 stdout/stderr（管道、
    重定向或普通终端），行为与源码运行一致，此时本函数立即返回：绝不 ``AttachConsole``
    以免把输出从管道挪到控制台，破坏 Agent 的捕获。只有两个流都缺失（极少见，例如
    从无标准句柄的宿主直接拉起）才附着到父进程控制台，失败则新建一个，再用
    ``CONOUT$`` 重建缺失的流。任何失败都静默，CLI 仍以其余路径尽力输出。
    """
    if sys.platform != "win32":
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        if kernel32.AttachConsole(-1) == 0:
            kernel32.AllocConsole()
        for name in ("stdout", "stderr"):
            if getattr(sys, name) is not None:
                continue
            try:
                stream = open("CONOUT$", "w", encoding="utf-8", errors="replace", newline="\n")
            except Exception:
                continue
            setattr(sys, name, stream)
    except Exception:
        pass


def configure_stdio() -> None:
    """把 stdout/stderr 固定为 UTF-8（``errors="replace"``、换行 ``\\n``）。

    Windows 控制台的代码页不再影响管道输出；冻结后的 exe 也一样。已经是 UTF-8 的流
    不需要再配置，直接保持原样，避免无谓地改动宿主（包括测试框架）的捕获流：
    ``reconfigure`` 会改掉流的编码/错误策略/换行，这些改动对本来正确的流毫无收益。
    流为 ``None``、没有 ``reconfigure`` 或 ``encoding`` 缺失/为空时无法判断，同样跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        encoding = getattr(stream, "encoding", None)
        if reconfigure is None or not isinstance(encoding, str) or not encoding.strip():
            continue
        if _is_utf8(encoding):
            continue
        reconfigure(encoding="utf-8", errors="replace", newline="\n")


def _is_utf8(encoding: str) -> bool:
    """把 ``utf-8`` / ``UTF8`` / ``utf_8`` 等写法归一后判断是否为 UTF-8。"""
    return encoding.strip().lower().replace("_", "-") in _UTF8_ENCODINGS


def envelope_ok(command: str, data: Any, elapsed_ms: int) -> dict[str, Any]:
    """成功信封：``ok/command/version/elapsed_ms/data``。"""
    return {
        "ok": True,
        "command": command,
        "version": APP_VERSION,
        "elapsed_ms": elapsed_ms,
        "data": data,
    }


def envelope_error(command: str, error: CliError, elapsed_ms: int) -> dict[str, Any]:
    """失败信封：``error`` 至少含 ``code``/``message``，有 ``details`` 时才附加。"""
    body: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.details is not None:
        body["details"] = error.details
    return {
        "ok": False,
        "command": command,
        "version": APP_VERSION,
        "elapsed_ms": elapsed_ms,
        "error": body,
    }


def write_payload(payload: dict, *, fmt: str = "json", pretty: bool = False) -> None:
    """把信封写到 stdout：json 默认紧凑、``pretty`` 时缩进 2；text 为 ``key: value`` 行。"""
    if fmt == "json":
        text = json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None)
    else:
        text = _render_text(payload)
    sys.stdout.write(text + "\n")


def emit_event(event: dict) -> None:
    """写一行紧凑 JSON（NDJSON），供流式命令逐条输出事件。"""
    sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def add_output_options(parser) -> None:
    """给叶子解析器加上 ``--format json|text`` 与 ``--pretty``。"""
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="输出格式：json（默认，供 Agent）或 text（供人工查看）。",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="json 输出缩进 2 空格，便于人工阅读。",
    )


def _render_text(payload: dict) -> str:
    """顶层按 ``key: value`` 逐行输出；嵌套对象/列表压成一行 JSON。"""
    lines: list[str] = []
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif value is None:
            rendered = "null"
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)

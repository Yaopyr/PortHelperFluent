"""有界的等待读取与流式命令的通用选项。

等待读取只依赖一个 ``fetch(seconds)`` 回调，因此串口、socket 等传输层都可以用
同一个循环；所有循环都受截止时间与 ``max_bytes`` 约束，不存在无限等待。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from app.cli.errors import EXIT_USAGE, CliError
from app.core.encoding import decode_bytes, parse_hex, resolve_codec

__all__ = [
    "ReadOutcome",
    "StreamOptions",
    "add_stream_options",
    "make_matcher",
    "read_stream_options",
    "read_until",
]

# 流式命令的时长上限，避免 Agent 调用后永久挂起。
MIN_STREAM_DURATION = 1.0
MAX_STREAM_DURATION = 3600.0


@dataclass(frozen=True)
class ReadOutcome:
    """一次有界读取的结果。

    - ``data``：累计收到的数据（已按 ``max_bytes`` 截断）；
    - ``matched``：给了 ``match`` 且命中；
    - ``timed_out``：等待 ``match`` 到截止时间仍未命中（未给 ``match`` 时为 False）；
    - ``chunks``：返回了非空数据的 ``fetch`` 调用次数；
    - ``elapsed_s``：从开始到返回的秒数。
    """

    data: bytes
    matched: bool
    timed_out: bool
    chunks: int
    elapsed_s: float


@dataclass(frozen=True)
class StreamOptions:
    """流式命令的通用上界参数（由 :func:`read_stream_options` 校验后给出）。"""

    duration: float
    max_bytes: int | None
    max_events: int | None
    until: str | None
    until_hex: str | None


def read_until(
    fetch: Callable[[float], bytes],
    *,
    timeout: float,
    match: Callable[[bytes], bool] | None = None,
    read_for: float | None = None,
    max_bytes: int | None = None,
    poll: float = 0.05,
    clock: Callable[[], float] = time.monotonic,
) -> ReadOutcome:
    """在截止时间前反复调用 ``fetch`` 累积数据。

    ``fetch(seconds)`` 必须返回 bytes，且最多阻塞 ``seconds`` 秒。给了 ``match`` 时
    截止时间为 ``timeout``，命中即提前结束；没给 ``match`` 时用 ``read_for`` 作为读取
    窗口（``read_for`` 为 None 时退回 ``timeout``）。每一轮把 ``poll`` 收缩到剩余时间
    （``fetch(min(poll, 剩余)``），因此单次读取不会越过截止时间；截止时间一到就不再
    调用 ``fetch``。``max_bytes`` 一到就把数据截断到该长度并结束。

    ``chunks`` 只统计返回了非空数据的 ``fetch`` 调用次数（空轮询不计）。
    """
    start = clock()
    window = timeout if (match is not None or read_for is None) else read_for
    deadline = start + window

    buffer = bytearray()
    chunks = 0
    matched = False
    timed_out = False
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            # 只有「等待匹配」才算超时；固定读取窗口到点是正常结束。
            timed_out = match is not None
            break
        chunk = fetch(min(poll, remaining))
        if chunk:
            buffer.extend(chunk)
            chunks += 1
        if match is not None and match(bytes(buffer)):
            matched = True
            break
        if max_bytes is not None and len(buffer) >= max_bytes:
            break

    data = bytes(buffer)
    if max_bytes is not None:
        data = data[:max_bytes]
    return ReadOutcome(
        data=data,
        matched=matched,
        timed_out=timed_out,
        chunks=chunks,
        elapsed_s=clock() - start,
    )


def make_matcher(
    *,
    expect: str | None = None,
    expect_hex: str | None = None,
    encoding: str = "utf-8",
) -> Callable[[bytes], bool] | None:
    """把 ``--expect``/``--expect-hex``（或 ``--until``/``--until-hex``）变成匹配器。

    文本模式按 ``encoding`` 解码累计数据后做子串匹配；十六进制模式直接匹配原始字节，
    因此同一个回包被拆成多次读取（如 ``b"O"``、``b"K"``）也能命中。两个都没给时
    返回 None；同时给两个、或给了空模式（``--expect ""``、只含分隔符的
    ``--expect-hex``）都视为参数错误——空模式会让任何数据立即「命中」。
    """
    if expect is not None and expect_hex is not None:
        raise CliError(
            "usage",
            "文本匹配与十六进制匹配不能同时使用（--expect/--expect-hex 或 --until/--until-hex）。",
            EXIT_USAGE,
        )
    if expect_hex is not None:
        try:
            needle = parse_hex(expect_hex)
        except ValueError as exc:
            raise CliError("usage", str(exc), EXIT_USAGE) from exc
        if not needle:
            raise CliError(
                "usage",
                "--expect-hex/--until-hex 解析后为空，请给出至少一个字节。",
                EXIT_USAGE,
            )
        return lambda data: needle in data
    if expect is not None:
        if not expect:
            raise CliError(
                "usage",
                "--expect/--until 的文本不能为空。",
                EXIT_USAGE,
            )
        try:
            codec = resolve_codec(encoding or "utf-8")
        except ValueError as exc:
            raise CliError("usage", str(exc), EXIT_USAGE) from exc
        text = expect
        return lambda data: text in decode_bytes(data, codec)
    return None


def add_stream_options(parser, *, require_duration: bool = True) -> None:
    """给流式叶子解析器加上 ``--duration``/``--max-bytes``/``--max-events``/``--until*``。"""
    parser.add_argument(
        "--duration",
        type=float,
        required=require_duration,
        metavar="SECONDS",
        help="运行时长（1–3600 秒），到点自行结束。",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        metavar="N",
        help="累计收到 N 字节后结束。",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        metavar="N",
        help="累计 N 个事件后结束。",
    )
    parser.add_argument(
        "--until",
        metavar="TEXT",
        help="收到该文本（按 --encoding 解码）后提前结束。",
    )
    parser.add_argument(
        "--until-hex",
        metavar="HEX",
        help="收到该十六进制字节序列后提前结束。",
    )


def read_stream_options(args) -> StreamOptions:
    """校验流式参数并返回 :class:`StreamOptions`；越界值一律报参数错误。"""
    duration = getattr(args, "duration", None)
    if duration is None:
        raise CliError("usage", "流式命令必须提供 --duration（1–3600 秒）。", EXIT_USAGE)
    if not MIN_STREAM_DURATION <= float(duration) <= MAX_STREAM_DURATION:
        raise CliError(
            "usage",
            f"--duration 必须在 1–3600 秒之间，收到 {duration}。",
            EXIT_USAGE,
        )

    max_bytes = getattr(args, "max_bytes", None)
    if max_bytes is not None and max_bytes <= 0:
        raise CliError("usage", f"--max-bytes 必须是正整数，收到 {max_bytes}。", EXIT_USAGE)

    max_events = getattr(args, "max_events", None)
    if max_events is not None and max_events <= 0:
        raise CliError("usage", f"--max-events 必须是正整数，收到 {max_events}。", EXIT_USAGE)

    until = getattr(args, "until", None)
    until_hex = getattr(args, "until_hex", None)
    if until is not None and until_hex is not None:
        raise CliError("usage", "--until 与 --until-hex 不能同时使用。", EXIT_USAGE)

    return StreamOptions(float(duration), max_bytes, max_events, until, until_hex)

"""``net`` 命令组：TCP/UDP 收发、UDP 只收流、TCP 服务端、ping、域名解析与网卡枚举。

- ``tcp``/``udp``：标准库 socket 收发，发送后按 ``--expect``/``--read-for`` 有界读取；
- ``udp --listen-only``：只收不发，按 NDJSON 输出 ``start``/``data``/``end`` 事件；
- ``server``：有界 TCP 服务端，按 NDJSON 输出 ``listening``/``client_*``/``end`` 事件；
- ``ping``/``resolve``/``adapters``：复用 :mod:`app.services.net_tools`，不重复实现。

模块级只导入标准库、CLI 原语与 ``net_tools``（后者只依赖标准库/psutil），不会把
Qt 带进 CLI 进程。
"""

from __future__ import annotations

import math
from typing import Any

from app.cli.errors import EXIT_EXPECT, EXIT_OPERATION, EXIT_USAGE, CliError
from app.cli.inputs import add_input_options, read_input
from app.cli.output import add_output_options, emit_event
from app.cli.reads import (
    add_stream_options,
    make_matcher,
    read_stream_options,
)
from app.cli.transports import net_io
from app.core.encoding import decode_bytes, resolve_codec
from app.services.net_tools import adapter_info, ping_host, resolve_host

__all__ = [
    "register",
    "run_adapters",
    "run_ping",
    "run_resolve",
    "run_server",
    "run_tcp",
    "run_udp",
]

_DEFAULT_TIMEOUT = 5.0
_DEFAULT_READ_FOR = 1.0
_DEFAULT_REPEAT = 1
_DEFAULT_INTERVAL_MS = 100
_DEFAULT_PING_COUNT = 4
_DEFAULT_MAX_CLIENTS = 16
_MIN_PORT = 1
_MAX_PORT = 65535


def register(subparsers) -> None:
    """注册 ``net`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "net",
        help="网络：TCP/UDP 收发、UDP 只收、TCP 服务端、ping、域名解析、网卡枚举。",
        description="直接使用标准库 socket 与系统网络工具，不启动 GUI；"
        "可用子命令：tcp、udp、server、ping、resolve、adapters。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    _register_tcp(group)
    _register_udp(group)
    _register_server(group)
    _register_ping(group)
    _register_resolve(group)
    _register_adapters(group)


# ---------------------------------------------------------------------------
# tcp
# ---------------------------------------------------------------------------


def _register_tcp(subparsers) -> None:
    parser = subparsers.add_parser(
        "tcp",
        help="向 TCP 服务端发送数据并等待回包。",
        description="连接后按 --repeat 写入输入，可用 --expect/--expect-hex 等待匹配；"
        "未命中以退出码 4 结束。",
    )
    parser.add_argument("--host", required=True, metavar="HOST", help="目标主机。")
    parser.add_argument("--port", type=int, required=True, metavar="PORT", help="目标端口。")
    parser.add_argument(
        "--local-port",
        type=int,
        default=0,
        metavar="PORT",
        help="客户端绑定端口，0 表示由系统分配（默认 0）。",
    )
    _add_send_options(parser)
    add_input_options(parser, required=True)
    add_output_options(parser)
    parser.set_defaults(handler=run_tcp, command="net.tcp")


def run_tcp(args) -> dict[str, Any]:
    """发送输入并读取回包，返回 ``sent``/``received``/``matched``/``remote``/``local_port``。"""
    data = read_input(args)
    host = str(getattr(args, "host", "") or "").strip()
    port = _port(getattr(args, "port", None), "--port", minimum=_MIN_PORT)
    local_port = _port(
        getattr(args, "local_port", 0), "--local-port", minimum=0
    )
    repeat, interval_ms, timeout, read_for = _validate_send(args)
    matcher = make_matcher(
        expect=getattr(args, "expect", None),
        expect_hex=getattr(args, "expect_hex", None),
        encoding=data.encoding,
    )

    try:
        result = net_io.tcp_exchange(
            host,
            port,
            data.data,
            local_port=local_port,
            timeout=timeout,
            read_for=read_for,
            matcher=matcher,
            repeat=repeat,
            interval_ms=interval_ms,
        )
    except CliError:
        raise
    except OSError as exc:
        raise CliError(
            "connection_failed",
            f"连接 {host}:{port} 失败：{exc}",
            EXIT_OPERATION,
        ) from exc

    _check_expect(matcher, result["matched"], host, port)
    return {
        "sent": result["sent"],
        "received": _received_data(
            result["received"], data.encoding, result["chunks"]
        ),
        "matched": result["matched"],
        "remote": result["remote"],
        "local_port": result["local_port"],
    }


# ---------------------------------------------------------------------------
# udp
# ---------------------------------------------------------------------------


def _register_udp(subparsers) -> None:
    parser = subparsers.add_parser(
        "udp",
        help="向 UDP 目标发送数据并等待回包，或只收不发（--listen-only）。",
        description="发送模式按 --repeat 写入并按需等待回包；--listen-only 只收不发，"
        "在所有 IPv4 接口上监听，必须给 --duration，输出 NDJSON start/data/end 事件，"
        "且不接受发送参数与 --format text/--pretty。",
    )
    parser.add_argument("--host", metavar="HOST", help="目标主机（发送模式必填）。")
    parser.add_argument("--port", type=int, metavar="PORT", help="目标端口（发送模式必填）。")
    parser.add_argument(
        "--bind-port",
        type=int,
        default=0,
        metavar="PORT",
        help="本地绑定端口，0 表示由系统分配（默认 0）。",
    )
    parser.add_argument(
        "--listen-only",
        action="store_true",
        help="只收不发：在所有 IPv4 接口上监听，必须搭配 --duration，按 NDJSON 输出事件。",
    )
    add_input_options(parser, required=False)
    _add_send_options(parser)
    add_stream_options(parser, require_duration=False)
    add_output_options(parser)
    parser.set_defaults(handler=run_udp, command="net.udp")


def run_udp(args) -> dict[str, Any]:
    """发送模式返回 ``sent``/``received``/``matched``/``remote``/``local_port``。"""
    if bool(getattr(args, "listen_only", False)):
        return _run_listen_only(args)

    data = read_input(args)
    host = str(getattr(args, "host", "") or "").strip()
    if not host:
        raise CliError("usage", "发送模式必须提供 --host。", EXIT_USAGE)
    port = _port(getattr(args, "port", None), "--port", minimum=_MIN_PORT)
    bind_port = _port(getattr(args, "bind_port", 0), "--bind-port", minimum=0)
    repeat, interval_ms, timeout, read_for = _validate_send(args)
    matcher = make_matcher(
        expect=getattr(args, "expect", None),
        expect_hex=getattr(args, "expect_hex", None),
        encoding=data.encoding,
    )

    try:
        result = net_io.udp_exchange(
            host,
            port,
            data.data,
            bind_port=bind_port,
            timeout=timeout,
            read_for=read_for,
            matcher=matcher,
            repeat=repeat,
            interval_ms=interval_ms,
        )
    except CliError:
        raise
    except OSError as exc:
        raise CliError(
            "connection_failed",
            f"发送 UDP 数据到 {host}:{port} 失败：{exc}",
            EXIT_OPERATION,
        ) from exc

    _check_expect(matcher, result["matched"], host, port)
    return {
        "sent": result["sent"],
        "received": _received_data(
            result["received"], data.encoding, result["chunks"]
        ),
        "matched": result["matched"],
        "remote": result["remote"],
        "local_port": result["local_port"],
    }


def _run_listen_only(args) -> dict[str, Any]:
    """校验只收模式并调用 :func:`net_io.udp_listen`，返回最终汇总。"""
    _require_ndjson(args)
    for attribute, option in (
        ("source", "--text/--hex/--file/--stdin"),
        ("repeat", "--repeat"),
        ("interval_ms", "--interval-ms"),
        ("expect", "--expect"),
        ("expect_hex", "--expect-hex"),
        ("read_for", "--read-for"),
    ):
        if getattr(args, attribute, None) is not None:
            raise CliError(
                "usage",
                f"--listen-only 只收不发，不能同时使用 {option}。",
                EXIT_USAGE,
            )
    if getattr(args, "until", None) is not None or getattr(args, "until_hex", None) is not None:
        raise CliError(
            "usage",
            "--listen-only 不支持 --until/--until-hex，请用 --duration/--max-bytes/"
            "--max-events 结束。",
            EXIT_USAGE,
        )

    options = read_stream_options(args)
    encoding = str(getattr(args, "encoding", None) or "utf-8")
    try:
        codec = resolve_codec(encoding)
    except ValueError as exc:
        raise CliError("usage", str(exc), EXIT_USAGE) from exc
    bind_port = _port(getattr(args, "bind_port", 0), "--bind-port", minimum=0)

    try:
        return net_io.udp_listen(
            bind_port,
            duration=options.duration,
            emit=_listen_emitter(codec),
            max_bytes=options.max_bytes,
            max_events=options.max_events,
        )
    except CliError:
        raise
    except OSError as exc:
        raise CliError(
            "connection_failed",
            f"监听 UDP 端口 {bind_port} 失败：{exc}",
            EXIT_OPERATION,
        ) from exc


def _listen_emitter(codec: str):
    """包装 ``emit_event``：数据事件的 ``text`` 按 ``--encoding`` 重新解码。"""

    def emit(event: dict) -> None:
        if event.get("event") == "data":
            event = {
                **event,
                "text": decode_bytes(bytes.fromhex(str(event["hex"])), codec),
            }
        emit_event(event)

    return emit


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------


def _register_server(subparsers) -> None:
    parser = subparsers.add_parser(
        "server",
        help="在本机启动有界 TCP 服务端，按 NDJSON 输出连接与数据事件。",
        description="监听 --bind/--port 后按 --duration 运行；--echo 原样回发收到的数据，"
        "--reply 对每次收到的数据回发固定文本，--greeting 在新客户端连接时立即发送。"
        "必须给 --duration，且不接受 --format text/--pretty、--until/--until-hex。",
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        metavar="HOST",
        help="监听地址，默认 127.0.0.1。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        metavar="PORT",
        help="监听端口，0 表示由系统分配（默认 0）。",
    )
    parser.add_argument(
        "--max-clients",
        type=int,
        default=_DEFAULT_MAX_CLIENTS,
        metavar="N",
        help=f"同时接入的客户端上限，默认 {_DEFAULT_MAX_CLIENTS}。",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--echo",
        action="store_true",
        help="把收到的数据原样回发给对应客户端。",
    )
    action.add_argument(
        "--reply",
        metavar="TEXT",
        help="对每次收到的数据回发固定文本（UTF-8）。",
    )
    parser.add_argument(
        "--greeting",
        metavar="TEXT",
        help="新客户端连接时立即发送的问候文本（UTF-8）。",
    )
    add_stream_options(parser)
    add_output_options(parser)
    parser.set_defaults(handler=run_server, command="net.server")


def run_server(args) -> dict[str, Any]:
    """校验流式参数并调用 :func:`net_io.serve_tcp`，返回最终汇总。"""
    _require_ndjson(args)
    if getattr(args, "until", None) is not None or getattr(args, "until_hex", None) is not None:
        raise CliError(
            "usage",
            "net server 不支持 --until/--until-hex，请用 --duration/--max-bytes/"
            "--max-events 结束。",
            EXIT_USAGE,
        )

    options = read_stream_options(args)
    bind = str(getattr(args, "bind", "") or "").strip()
    if not bind:
        raise CliError("usage", "--bind 不能为空。", EXIT_USAGE)
    port = _port(getattr(args, "port", 0), "--port", minimum=0)
    max_clients = _positive_int(
        getattr(args, "max_clients", None), "--max-clients", _DEFAULT_MAX_CLIENTS
    )

    try:
        return net_io.serve_tcp(
            bind,
            port,
            duration=options.duration,
            max_clients=max_clients,
            emit=emit_event,
            echo=bool(getattr(args, "echo", False)),
            reply=getattr(args, "reply", None),
            greeting=getattr(args, "greeting", None),
            max_bytes=options.max_bytes,
            max_events=options.max_events,
        )
    except CliError:
        raise
    except OSError as exc:
        raise CliError(
            "connection_failed",
            f"监听 {bind}:{port} 失败：{exc}",
            EXIT_OPERATION,
        ) from exc


# ---------------------------------------------------------------------------
# ping / resolve / adapters
# ---------------------------------------------------------------------------


def _register_ping(subparsers) -> None:
    parser = subparsers.add_parser(
        "ping",
        help="调用系统 ping 命令探测主机。",
        description="调用系统 ping 并返回原始输出；ping 失败是结果（ok=false），仍以 0 退出。",
    )
    parser.add_argument("--host", required=True, metavar="HOST", help="目标主机。")
    parser.add_argument(
        "--count",
        type=int,
        default=4,
        metavar="N",
        help="探测次数，默认 4。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15,
        metavar="SECONDS",
        help="ping 命令的整体超时秒数，默认 15。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_ping, command="net.ping")


def run_ping(args) -> dict[str, Any]:
    """返回 ``host``/``ok``/``output``/``count``；ping 失败不抛错误。"""
    host = str(getattr(args, "host", "") or "").strip()
    count = _positive_int(getattr(args, "count", None), "--count", _DEFAULT_PING_COUNT)
    timeout_s = _finite(
        getattr(args, "timeout", None), "--timeout", default=15.0
    )
    result = ping_host(host, count=count, timeout_s=timeout_s)
    return {
        "host": result.host,
        "ok": bool(result.ok),
        "output": result.output,
        "count": count,
    }


def _register_resolve(subparsers) -> None:
    parser = subparsers.add_parser(
        "resolve",
        help="解析域名或 IP，返回去重后的地址列表。",
        description="使用标准库 getaddrinfo 解析；失败以 not_found（退出码 3）结束。",
    )
    parser.add_argument("--host", required=True, metavar="HOST", help="要解析的域名或 IP。")
    add_output_options(parser)
    parser.set_defaults(handler=run_resolve, command="net.resolve")


def run_resolve(args) -> dict[str, Any]:
    """返回 ``host``/``addresses``/``count``。"""
    host = str(getattr(args, "host", "") or "").strip()
    try:
        addresses = resolve_host(host)
    except ValueError as exc:
        raise CliError("not_found", str(exc), EXIT_OPERATION) from exc
    return {"host": host, "addresses": addresses, "count": len(addresses)}


def _register_adapters(subparsers) -> None:
    parser = subparsers.add_parser(
        "adapters",
        help="枚举本机网卡信息。",
        description="返回网卡名称、地址、掩码、网关、MAC 与启用状态；"
        "读取失败以 unsupported（退出码 3）结束。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_adapters, command="net.adapters")


def run_adapters(args) -> dict[str, Any]:
    """返回 ``adapters`` 列表与 ``count``。"""
    try:
        adapters = adapter_info()
    except RuntimeError as exc:
        raise CliError("unsupported", str(exc), EXIT_OPERATION) from exc
    return {"adapters": adapters, "count": len(adapters)}


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


def _require_ndjson(args) -> None:
    """流式命令固定输出 NDJSON：拒绝 ``--format text`` 与 ``--pretty``。"""
    if str(getattr(args, "format", "json")) == "text":
        raise CliError(
            "usage",
            "流式命令固定输出 NDJSON，不支持 --format text。",
            EXIT_USAGE,
        )
    if bool(getattr(args, "pretty", False)):
        raise CliError(
            "usage",
            "流式命令固定输出 NDJSON，不支持 --pretty。",
            EXIT_USAGE,
        )


def _add_send_options(parser) -> None:
    """添加 TCP/UDP 发送参数；默认值延迟到命令里解析，便于识别 ``--listen-only`` 冲突。"""
    parser.add_argument(
        "--repeat",
        type=int,
        default=None,
        metavar="N",
        help="发送次数，默认 1。",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=None,
        metavar="N",
        help="发送之间的间隔毫秒数，默认 100。",
    )
    parser.add_argument("--expect", metavar="TEXT", help="等待该文本（按 --encoding 解码）。")
    parser.add_argument("--expect-hex", metavar="HEX", help="等待该十六进制字节序列。")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="等待 --expect/--expect-hex 的秒数，默认 5；"
        "0 表示 expect 不等待（连接阶段仍用 5 秒兜底）。",
    )
    parser.add_argument(
        "--read-for",
        type=float,
        default=None,
        metavar="SECONDS",
        help="没有 --expect 时的固定读取窗口，默认 1 秒；0 表示不读。",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        metavar="CODEC",
        help="文本编码，默认 utf-8。",
    )


def _validate_send(args) -> tuple[int, int, float, float]:
    """校验 ``--repeat``/``--interval-ms``/``--timeout``/``--read-for`` 的默认值与范围。"""
    repeat = _positive_int(getattr(args, "repeat", None), "--repeat", _DEFAULT_REPEAT)
    interval_ms = _non_negative_int(
        getattr(args, "interval_ms", None), "--interval-ms", _DEFAULT_INTERVAL_MS
    )
    timeout = _finite(getattr(args, "timeout", None), "--timeout", _DEFAULT_TIMEOUT)
    read_for = _finite(getattr(args, "read_for", None), "--read-for", _DEFAULT_READ_FOR)
    return repeat, interval_ms, timeout, read_for


def _port(value, name: str, *, minimum: int) -> int:
    """端口必须是整数且在 ``minimum``–65535 之间。"""
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise CliError("usage", f"{name} 必须是整数。", EXIT_USAGE) from exc
    if not minimum <= port <= _MAX_PORT:
        raise CliError(
            "usage",
            f"{name} 必须在 {minimum}–{_MAX_PORT} 之间，收到 {value}。",
            EXIT_USAGE,
        )
    return port


def _finite(value, name: str, default: float) -> float:
    """有限非负数；nan/inf/负数一律报参数错误（``value`` 为 None 时用默认值）。"""
    number = default if value is None else float(value)
    if not math.isfinite(number) or number < 0:
        raise CliError(
            "usage", f"{name} 必须是有限的非负数，收到 {value}。", EXIT_USAGE
        )
    return number


def _positive_int(value, name: str, default: int) -> int:
    """正整数（``value`` 为 None 时用默认值）。"""
    number = default if value is None else int(value)
    if number < 1:
        raise CliError("usage", f"{name} 必须是正整数，收到 {value}。", EXIT_USAGE)
    return number


def _non_negative_int(value, name: str, default: int) -> int:
    """非负整数（``value`` 为 None 时用默认值）。"""
    number = default if value is None else int(value)
    if number < 0:
        raise CliError("usage", f"{name} 不能为负数，收到 {value}。", EXIT_USAGE)
    return number


def _check_expect(matcher, matched: bool, host: str, port: int) -> None:
    """给了匹配器但未命中时，以 ``timeout``（退出码 4）结束。"""
    if matcher is not None and not matched:
        raise CliError(
            "timeout",
            f"等待 {host}:{port} 的回包超时：--expect/--expect-hex 未命中。",
            EXIT_EXPECT,
        )


def _received_data(raw: bytes, encoding: str, chunks: int) -> dict[str, Any]:
    """把原始回包整理成 ``hex``/``text``/``size``/``chunks``。"""
    return {
        "hex": raw.hex(),
        "text": decode_bytes(raw, encoding),
        "size": len(raw),
        "chunks": chunks,
    }

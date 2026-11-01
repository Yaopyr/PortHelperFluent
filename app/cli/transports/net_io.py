"""TCP/UDP 传输：标准库 ``socket`` 的有界收发与 UDP 只收模式。

只依赖标准库，不引入 QtNetwork；所有读循环都复用
:func:`app.cli.reads.read_until` 的截止时间约束，因此单次收包不会越过等待窗口。
连接阶段的 ``OSError`` 原样上抛，由命令层映射成 ``connection_failed``；读阶段的
``socket.timeout`` 返回空块，其它 ``OSError`` 转成 :class:`CliError`。
"""

from __future__ import annotations

import socket
import time
from datetime import datetime
from typing import Callable

from app.cli.errors import EXIT_OPERATION, CliError
from app.cli.reads import read_until
from app.core.encoding import decode_bytes

__all__ = ["tcp_exchange", "udp_exchange", "udp_listen"]

# 单次 socket 读取的等待上界：read_until 会把每轮 poll 收缩到剩余时间。
POLL = 0.05
# read_until 传入的窗口可能极小，settimeout 不接受 0（会变成非阻塞）。
MIN_WAIT = 0.001
# 连接超时缺省：--timeout 0 只表示「不等待 --expect」，连接仍需要一个有限上界。
CONNECT_TIMEOUT_FALLBACK = 5.0
TCP_RECV_CHUNK = 4096
# UDP 数据报最大 65507 字节，读满上限避免截断。
UDP_RECV_CHUNK = 65535


def _clock() -> float:
    """可替换的单调时钟调用点；测试可替换它来压缩 ``--duration``。"""
    return time.monotonic()


def _timestamp() -> str:
    """本地时区的 ISO 8601 时间戳，供流式事件的 ``at`` 字段使用。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def tcp_exchange(
    host: str,
    port: int,
    payload: bytes,
    *,
    local_port: int = 0,
    timeout: float = 5.0,
    read_for: float = 1.0,
    matcher: Callable[[bytes], bool] | None = None,
    repeat: int = 1,
    interval_ms: int = 100,
) -> dict:
    """连接 TCP 服务端，写入 ``repeat`` 次 ``payload`` 后按需读取回包。

    返回 ``sent``（``bytes``/``writes``，写入总字节数与次数）、``received``（原始
    字节）、``matched``（``matcher`` 是否命中）、``chunks``（返回非空数据的读取次数）、
    ``remote``（``host:port``）与 ``local_port``（连接后的实际本地端口）。给了
    ``matcher`` 时等待 ``timeout``，否则按 ``read_for`` 做固定窗口读取；``read_for``
    为 0 时不读。连接阶段的异常原样上抛。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout if timeout > 0 else CONNECT_TIMEOUT_FALLBACK)
        if local_port:
            sock.bind(("", local_port))
        sock.connect((host, port))
        actual_local_port = int(sock.getsockname()[1])
        sent = _send_repeated(
            lambda data: sock.sendall(data), payload, repeat, interval_ms
        )

        def fetch(seconds: float) -> bytes:
            return _fetch_tcp(sock, seconds, host, port)

        outcome = _read_response(
            fetch, matcher=matcher, timeout=timeout, read_for=read_for
        )
    finally:
        sock.close()
    return {
        "sent": sent,
        "received": outcome.data,
        "matched": outcome.matched,
        "chunks": outcome.chunks,
        "remote": f"{host}:{port}",
        "local_port": actual_local_port,
    }


def udp_exchange(
    host: str,
    port: int,
    payload: bytes,
    *,
    bind_port: int = 0,
    timeout: float = 5.0,
    read_for: float = 1.0,
    matcher: Callable[[bytes], bool] | None = None,
    repeat: int = 1,
    interval_ms: int = 100,
) -> dict:
    """向 UDP 目标发送 ``repeat`` 次数据报，并按需等待回包。

    返回 ``sent``、``received``、``matched``、``chunks``（返回非空数据的读取次数）、
    ``remote``（最后一个回包的来源 ``ip:port``，没收到时为 ``host:port``）与
    ``local_port``（绑定后的实际端口）。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sources: list[str] = []
    try:
        sock.bind(("", bind_port))
        actual_local_port = int(sock.getsockname()[1])
        sent = _send_repeated(
            lambda data: sock.sendto(data, (host, port)), payload, repeat, interval_ms
        )

        def fetch(seconds: float) -> bytes:
            chunk, address = _fetch_udp(sock, seconds, host, port)
            if address is not None:
                sources.append(f"{address[0]}:{address[1]}")
            return chunk

        outcome = _read_response(
            fetch, matcher=matcher, timeout=timeout, read_for=read_for
        )
    finally:
        sock.close()
    return {
        "sent": sent,
        "received": outcome.data,
        "matched": outcome.matched,
        "chunks": outcome.chunks,
        "remote": sources[-1] if sources else f"{host}:{port}",
        "local_port": actual_local_port,
    }


def udp_listen(
    bind_port: int,
    *,
    duration: float,
    emit: Callable[[dict], None],
    max_bytes: int | None = None,
    max_events: int | None = None,
) -> dict:
    """在所有 IPv4 接口上只收不发，按 NDJSON 事件输出有界监听结果。

    事件依次为 ``start``（实际绑定端口）、每个非空数据报一条 ``data``
    （``hex``/``text``/``size``/``at``/``from``）与 ``end``（``duration_s``/``bytes``/
    ``events``/``matched:false``/``at``）。``max_bytes`` 截断最后一个数据报，
    ``max_events`` 达到即停；循环每轮检查截止时间，不会悬挂。返回最终汇总
    （``duration_s``/``bytes``/``events``/``port``）。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("", bind_port))
        actual_port = int(sock.getsockname()[1])
        started = _clock()
        deadline = started + duration
        emit({"event": "start", "port": actual_port, "at": _timestamp()})

        total = 0
        events = 0
        while True:
            remaining = deadline - _clock()
            if remaining <= 0:
                break
            sock.settimeout(min(POLL, remaining))
            try:
                chunk, address = sock.recvfrom(UDP_RECV_CHUNK)
            except TimeoutError:
                continue
            except OSError as exc:
                raise CliError(
                    "connection_failed",
                    f"监听 UDP 端口 {actual_port} 失败：{exc}",
                    EXIT_OPERATION,
                ) from exc
            if not chunk:
                continue

            if max_bytes is not None:
                budget = max_bytes - total
                if budget <= 0:
                    break
                if len(chunk) > budget:
                    chunk = chunk[:budget]

            total += len(chunk)
            events += 1
            emit(
                {
                    "event": "data",
                    "hex": chunk.hex(),
                    "text": decode_bytes(chunk, "utf-8"),
                    "size": len(chunk),
                    "at": _timestamp(),
                    "from": f"{address[0]}:{address[1]}",
                }
            )
            if max_events is not None and events >= max_events:
                break
            if max_bytes is not None and total >= max_bytes:
                break

        summary = {
            "duration_s": round(max(_clock() - started, 0.0), 3),
            "bytes": total,
            "events": events,
            "port": actual_port,
        }
        emit({"event": "end", **summary, "matched": False, "at": _timestamp()})
        return summary
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _send_repeated(
    send: Callable[[bytes], object],
    payload: bytes,
    repeat: int,
    interval_ms: int,
) -> dict:
    """发送 ``repeat`` 次，仅在两次之间按 ``interval_ms`` 休眠；返回字节数与次数。"""
    for index in range(repeat):
        if index and interval_ms:
            time.sleep(interval_ms / 1000.0)
        send(payload)
    return {"bytes": len(payload) * repeat, "writes": repeat}


def _read_response(fetch, *, matcher, timeout: float, read_for: float):
    """给了匹配器就等待命中，否则按 ``read_for`` 做固定窗口读取（0 不读）。"""
    if matcher is not None:
        return read_until(fetch, timeout=timeout, match=matcher)
    return read_until(fetch, timeout=0, read_for=read_for)


def _fetch_tcp(sock, seconds: float, host: str, port: int) -> bytes:
    """单次 TCP 读取：等待窗口内无数据返回空块，其它错误转 ``CliError``。

    对端已关闭（``recv`` 返回 ``b""``）时 sleep 一个轮询窗口，避免 ``read_until``
    在固定读取窗口或 ``--expect`` 等待中空转占满 CPU。
    """
    sock.settimeout(max(seconds, MIN_WAIT))
    try:
        chunk = sock.recv(TCP_RECV_CHUNK)
    except TimeoutError:
        return b""
    except OSError as exc:
        raise CliError(
            "connection_failed",
            f"从 {host}:{port} 接收数据失败：{exc}",
            EXIT_OPERATION,
        ) from exc
    if not chunk:
        time.sleep(max(seconds, MIN_WAIT))
        return b""
    return chunk


def _fetch_udp(sock, seconds: float, host: str, port: int) -> tuple[bytes, object]:
    """单次 UDP 读取：返回数据与来源地址；等待窗口内无数据返回 ``(b"", None)``。"""
    sock.settimeout(max(seconds, MIN_WAIT))
    try:
        return sock.recvfrom(UDP_RECV_CHUNK)
    except TimeoutError:
        return b"", None
    except OSError as exc:
        raise CliError(
            "connection_failed",
            f"从 {host}:{port} 接收数据失败：{exc}",
            EXIT_OPERATION,
        ) from exc

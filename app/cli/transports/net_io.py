"""TCP/UDP 传输：标准库 ``socket`` 的有界收发、UDP 只收模式与 TCP 服务端。

只依赖标准库，不引入 QtNetwork；所有读循环都复用
:func:`app.cli.reads.read_until` 的截止时间约束，因此单次收包不会越过等待窗口。
连接阶段的 ``OSError`` 原样上抛，由命令层映射成 ``connection_failed``；读阶段的
``socket.timeout`` 返回空块，其它 ``OSError`` 转成 :class:`CliError`。TCP 服务端
用 ``selectors`` 在同一条线程里处理监听与所有客户端，每轮 select 都收缩到剩余时长。
"""

from __future__ import annotations

import selectors
import socket
import time
from datetime import datetime
from typing import Callable

from app.cli.errors import EXIT_OPERATION, CliError
from app.cli.reads import read_until
from app.core.encoding import decode_bytes

__all__ = ["serve_tcp", "tcp_exchange", "udp_exchange", "udp_listen"]

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


def serve_tcp(
    host: str,
    port: int,
    *,
    duration: float,
    max_clients: int,
    emit: Callable[[dict], None],
    echo: bool = False,
    reply: str | None = None,
    greeting: str | None = None,
    max_bytes: int | None = None,
    max_events: int | None = None,
    on_listening: Callable[[int], None] | None = None,
) -> dict:
    """在 ``host:port`` 上有界地提供 TCP 服务，按 NDJSON 事件输出结果。

    绑定成功后回调 ``on_listening(实际端口)`` 并发 ``listening`` 事件；此后每轮
    ``select`` 等待 ``min(POLL, 剩余时长)``，监听 socket 可读时 accept，客户端可读时
    ``recv``。非空数据发 ``client_data``（``address``/``hex``/``text``/``size``/
    ``at``），``echo`` 原样回发、``reply`` 回发固定文本（二者互斥，``echo`` 优先）；
    新客户端发 ``client_connected``，给了 ``greeting`` 时立即发送；对端关闭时注销
    并发 ``client_disconnected``。连接数已达 ``max_clients`` 时只发
    ``code=max_clients`` 的 ``error`` 事件并立刻关闭该新 socket，不纳入统计。

    结束条件：``duration`` 到点、累计字节达到 ``max_bytes``（截断最后一个块）或
    ``client_data`` 事件达到 ``max_events``。结束时发 ``end`` 并返回汇总
    （``port``/``clients``/``bytes_received``/``bytes_sent``/``duration_s``/``events``）。
    ``clients`` 是成功接入的客户端累计数（被拒绝的不计）。绑定失败只抛
    ``connection_failed``；收发过程中的 ``OSError`` 先发 ``error`` 事件再抛
    ``CliError``。所有 socket 都在 ``finally`` 里关闭。
    """
    selector = selectors.DefaultSelector()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    clients: dict[socket.socket, str] = {}
    try:
        try:
            listener.bind((host, port))
            listener.listen(max(max_clients, 1))
            listener.setblocking(False)
        except OSError as exc:
            raise CliError(
                "connection_failed",
                f"监听 {host}:{port} 失败：{exc}",
                EXIT_OPERATION,
            ) from exc

        actual_port = int(listener.getsockname()[1])
        started = _clock()
        deadline = started + duration
        selector.register(listener, selectors.EVENT_READ)
        if on_listening is not None:
            on_listening(actual_port)
        emit(
            {
                "event": "listening",
                "bind": host,
                "port": actual_port,
                "at": _timestamp(),
            }
        )

        received = 0
        sent = 0
        data_events = 0
        clients_total = 0
        while True:
            remaining = deadline - _clock()
            if remaining <= 0:
                break
            stop = False
            for key, _mask in selector.select(min(POLL, remaining)):
                if key.fileobj is listener:
                    while True:
                        try:
                            conn, peer = listener.accept()
                        except (BlockingIOError, TimeoutError):
                            break
                        except OSError as exc:
                            raise _connection_failed(
                                emit, f"接受 {host}:{actual_port} 的新连接失败：{exc}"
                            ) from exc
                        address = f"{peer[0]}:{peer[1]}"
                        if len(clients) >= max_clients:
                            emit(
                                {
                                    "event": "error",
                                    "code": "max_clients",
                                    "message": f"客户端数量已达上限 {max_clients}，"
                                    f"拒绝 {address}。",
                                    "at": _timestamp(),
                                }
                            )
                            _close_socket(conn)
                            continue
                        conn.settimeout(POLL)
                        clients[conn] = address
                        clients_total += 1
                        selector.register(conn, selectors.EVENT_READ)
                        emit(
                            {
                                "event": "client_connected",
                                "address": address,
                                "at": _timestamp(),
                            }
                        )
                        if greeting:
                            sent += _send_chunk(
                                conn, greeting.encode("utf-8"), address, emit
                            )
                    continue

                conn = key.fileobj
                address = clients.get(conn, "")
                try:
                    chunk = conn.recv(TCP_RECV_CHUNK)
                except TimeoutError:
                    continue
                except OSError as exc:
                    raise _connection_failed(
                        emit, f"从 {address} 接收数据失败：{exc}"
                    ) from exc
                if not chunk:
                    _drop_client(selector, clients, conn)
                    emit(
                        {
                            "event": "client_disconnected",
                            "address": address,
                            "at": _timestamp(),
                        }
                    )
                    continue

                if max_bytes is not None:
                    budget = max_bytes - received
                    if budget <= 0:
                        stop = True
                        break
                    if len(chunk) > budget:
                        chunk = chunk[:budget]

                received += len(chunk)
                data_events += 1
                emit(
                    {
                        "event": "client_data",
                        "address": address,
                        "hex": chunk.hex(),
                        "text": decode_bytes(chunk, "utf-8"),
                        "size": len(chunk),
                        "at": _timestamp(),
                    }
                )
                if echo:
                    sent += _send_chunk(conn, chunk, address, emit)
                elif reply is not None:
                    sent += _send_chunk(
                        conn, reply.encode("utf-8"), address, emit
                    )

                if max_events is not None and data_events >= max_events:
                    stop = True
                    break
                if max_bytes is not None and received >= max_bytes:
                    stop = True
                    break
            if stop:
                break

        summary = {
            "port": actual_port,
            "clients": clients_total,
            "bytes_received": received,
            "bytes_sent": sent,
            "duration_s": round(max(_clock() - started, 0.0), 3),
            "events": data_events,
        }
        emit({"event": "end", **summary, "at": _timestamp()})
        return summary
    finally:
        for conn in list(clients):
            try:
                selector.unregister(conn)
            except (KeyError, ValueError):
                pass
            _close_socket(conn)
        clients.clear()
        try:
            selector.unregister(listener)
        except (KeyError, ValueError):
            pass
        _close_socket(listener)
        selector.close()


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _connection_failed(emit: Callable[[dict], None], message: str) -> CliError:
    """先发一行 ``error`` 事件，再返回由调用方 ``raise`` 的 ``CliError``。"""
    emit(
        {
            "event": "error",
            "code": "connection_failed",
            "message": message,
            "at": _timestamp(),
        }
    )
    return CliError("connection_failed", message, EXIT_OPERATION)


def _send_chunk(
    conn: socket.socket,
    payload: bytes,
    address: str,
    emit: Callable[[dict], None],
) -> int:
    """向客户端写出 ``payload``，返回写出的字节数；失败时转成 ``connection_failed``。"""
    try:
        conn.sendall(payload)
    except OSError as exc:
        raise _connection_failed(emit, f"向 {address} 发送数据失败：{exc}") from exc
    return len(payload)


def _drop_client(
    selector: selectors.BaseSelector,
    clients: dict[socket.socket, str],
    conn: socket.socket,
) -> None:
    """注销并关闭一个已经断开的客户端（关闭失败静默）。"""
    clients.pop(conn, None)
    try:
        selector.unregister(conn)
    except (KeyError, ValueError):
        pass
    _close_socket(conn)


def _close_socket(sock: socket.socket) -> None:
    """关闭 socket；关闭阶段的 ``OSError`` 不影响最终汇总。"""
    try:
        sock.close()
    except OSError:
        pass


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

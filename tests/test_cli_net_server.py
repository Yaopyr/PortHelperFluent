"""``net server``：有界 TCP 服务端（NDJSON 流式）的 CLI 契约。

所有用例只走本机回环 ``127.0.0.1``：客户端线程在 ``finally`` 里关闭 socket 并
join，服务端由 ``--duration 1`` 自行结束，因此测试不会访问外网、也不会悬挂。
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time

import pytest

from app.cli.transports import net_io

_CONNECT_TIMEOUT = 3.0
_CONNECT_RETRY = 0.02
_CLIENT_WAIT = 2.0


# ---------------------------------------------------------------------------
# 本机回环辅助
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """借系统分配一个空闲端口后立即释放（用于预先指定服务端端口）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _connect(port: int, *, timeout: float = _CONNECT_TIMEOUT) -> socket.socket | None:
    """带重试地连接本机端口，超时返回 None（CLI 可能晚于客户端线程开始监听）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                sock.connect(("127.0.0.1", port))
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"连接 127.0.0.1:{port} 超时。")
                time.sleep(_CONNECT_RETRY)
                continue
            sock.settimeout(_CLIENT_WAIT)
            return sock
    except BaseException:
        sock.close()
        raise


def _recv_until(sock: socket.socket, expected: bytes, *, timeout: float = _CLIENT_WAIT) -> bytes:
    """读取直到累计数据包含 ``expected``；超时或对端关闭时返回已收到的数据。"""
    deadline = time.monotonic() + timeout
    buffer = b""
    while expected not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(max(remaining, 0.001))
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            break
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
    return buffer


@contextlib.contextmanager
def _client_thread(target):
    """启动客户端线程，并在退出时 join（线程仍存活则断言失败）。"""
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    try:
        yield thread
    finally:
        thread.join(timeout=5)
    assert not thread.is_alive()


def _events(payload: dict) -> list[dict]:
    """把 ``raw`` 里的 NDJSON 行解析成事件列表（最后一项是标准信封）。"""
    lines = [line for line in payload["raw"].splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _kinds(events: list[dict]) -> list[str]:
    """事件名列表（不含最后的标准信封）。"""
    return [str(event.get("event")) for event in events[:-1]]


def _install_socket_failure(monkeypatch, *, send=None, recv=None):
    """给 ``net_io`` 的监听 socket 与 accept 到的客户端注入一次性故障。

    钩子只在第一次触发后失效，之后委托给真实 socket；函数返回真实的 socket 类，
    客户端要用它自己建连接（``monkeypatch`` 改掉的是全局的 ``socket.socket``）。
    """
    real_socket = socket.socket
    hooks = {"sendall": send, "recv": recv}

    class _WrappedConn:
        def __init__(self, sock: socket.socket) -> None:
            self._sock = sock

        def __getattr__(self, name):
            return getattr(self._sock, name)

        def sendall(self, data: bytes) -> None:
            hook = hooks["sendall"]
            if hook is not None:
                hooks["sendall"] = None
                hook()
            self._sock.sendall(data)

        def recv(self, size: int) -> bytes:
            hook = hooks["recv"]
            if hook is not None:
                hooks["recv"] = None
                hook()
            return self._sock.recv(size)

    class _WrappedListener:
        def __init__(self, *args, **kwargs) -> None:
            self._sock = real_socket(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._sock, name)

        def accept(self):
            conn, peer = self._sock.accept()
            return _WrappedConn(conn), peer

    monkeypatch.setattr(net_io.socket, "socket", _WrappedListener)
    return real_socket


def _storm_clients(
    port: int, *, count: int = 40, budget: float = 0.6
) -> list[socket.socket]:
    """在预算时间内尽力建立 ``count`` 个连接（被内核拒绝时短暂重试）。"""
    deadline = time.monotonic() + budget
    clients: list[socket.socket] = []
    while len(clients) < count and time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        try:
            sock.connect(("127.0.0.1", port))
        except OSError:
            sock.close()
            time.sleep(0.005)
            continue
        clients.append(sock)
    return clients


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------


def test_serve_tcp_echo_events_and_summary() -> None:
    events: list[dict] = []
    ports: list[int] = []
    ready = threading.Event()
    summary: dict = {}
    received: list[bytes] = []

    def on_listening(port: int) -> None:
        ports.append(port)
        ready.set()

    def run_server() -> None:
        summary.update(
            net_io.serve_tcp(
                "127.0.0.1",
                0,
                duration=1,
                max_clients=4,
                emit=events.append,
                echo=True,
                on_listening=on_listening,
            )
        )

    server = threading.Thread(target=run_server, daemon=True)
    server.start()
    try:
        assert ready.wait(timeout=3)
        sock = _connect(ports[0])
        try:
            sock.sendall(b"hi")
            received.append(_recv_until(sock, b"hi"))
        finally:
            sock.close()
    finally:
        server.join(timeout=5)
    assert not server.is_alive()

    assert received == [b"hi"]
    assert [event["event"] for event in events] == [
        "listening",
        "client_connected",
        "client_data",
        "client_disconnected",
        "end",
    ]

    listening = events[0]
    assert listening["bind"] == "127.0.0.1"
    assert listening["port"] == ports[0] > 0
    assert isinstance(listening["at"], str)

    connected = events[1]
    assert connected["address"].startswith("127.0.0.1:")

    data_event = events[2]
    assert data_event["address"] == connected["address"]
    assert data_event["hex"] == "6869"
    assert data_event["text"] == "hi"
    assert data_event["size"] == 2
    assert isinstance(data_event["at"], str)

    assert events[3]["address"] == connected["address"]

    end = events[4]
    assert end["port"] == ports[0]
    assert end["clients"] == 1
    assert end["bytes_received"] == 2
    assert end["bytes_sent"] == 2
    assert end["duration_s"] >= 0.5
    assert summary["port"] == ports[0]
    assert summary["clients"] == 1
    assert summary["bytes_received"] == 2
    assert summary["bytes_sent"] == 2
    assert summary["duration_s"] == end["duration_s"]
    assert summary["events"] == 1

    # 返回后监听 socket 必须已关闭：同一端口可以再次绑定。
    again = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        again.bind(("127.0.0.1", ports[0]))
    finally:
        again.close()


def test_serve_tcp_slow_client_is_dropped_not_fatal(monkeypatch) -> None:
    """发送超时只丢弃该客户端：服务端继续运行到 duration，不报 connection_failed。"""

    def raise_timeout() -> None:
        raise TimeoutError("模拟发送超时")

    real_socket = _install_socket_failure(monkeypatch, send=raise_timeout)
    events: list[dict] = []
    ports: list[int] = []
    ready = threading.Event()
    summary: dict = {}
    failures: list[BaseException] = []

    def on_listening(port: int) -> None:
        ports.append(port)
        ready.set()

    def run_server() -> None:
        try:
            summary.update(
                net_io.serve_tcp(
                    "127.0.0.1",
                    0,
                    duration=1,
                    max_clients=4,
                    emit=events.append,
                    echo=True,
                    on_listening=on_listening,
                )
            )
        except BaseException as exc:  # 测试要断言服务端没有把错误抛出去
            failures.append(exc)

    server = threading.Thread(target=run_server, daemon=True)
    server.start()
    try:
        assert ready.wait(timeout=3)
        client = real_socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.settimeout(2)
            client.connect(("127.0.0.1", ports[0]))
            client.sendall(b"hi")
            try:
                assert client.recv(64) == b""
            except OSError:
                pass
        finally:
            client.close()
    finally:
        server.join(timeout=5)
    assert not server.is_alive()
    assert failures == []

    kinds = [event["event"] for event in events]
    assert kinds[0] == "listening"
    assert kinds[-1] == "end"
    assert "client_data" in kinds
    assert kinds.count("client_disconnected") >= 1
    assert not [event for event in events if event.get("event") == "error"]
    assert summary["clients"] == 1
    assert summary["bytes_received"] == 2
    assert summary["bytes_sent"] == 0
    assert summary["events"] == 1


def test_serve_tcp_reset_client_is_disconnect(monkeypatch) -> None:
    """recv 被对端重置按客户端断开处理：不发全局 error、不终止服务端。"""

    def raise_reset() -> None:
        raise ConnectionResetError("模拟对端重置")

    real_socket = _install_socket_failure(monkeypatch, recv=raise_reset)
    events: list[dict] = []
    ports: list[int] = []
    ready = threading.Event()
    summary: dict = {}
    failures: list[BaseException] = []

    def on_listening(port: int) -> None:
        ports.append(port)
        ready.set()

    def run_server() -> None:
        try:
            summary.update(
                net_io.serve_tcp(
                    "127.0.0.1",
                    0,
                    duration=1,
                    max_clients=4,
                    emit=events.append,
                    on_listening=on_listening,
                )
            )
        except BaseException as exc:  # 测试要断言服务端没有把错误抛出去
            failures.append(exc)

    server = threading.Thread(target=run_server, daemon=True)
    server.start()
    try:
        assert ready.wait(timeout=3)
        client = real_socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.settimeout(2)
            client.connect(("127.0.0.1", ports[0]))
            client.sendall(b"x")
            try:
                client.recv(64)
            except OSError:
                pass
        finally:
            client.close()
    finally:
        server.join(timeout=5)
    assert not server.is_alive()
    assert failures == []

    kinds = [event["event"] for event in events]
    assert kinds[0] == "listening"
    assert kinds[-1] == "end"
    assert kinds.count("client_disconnected") >= 1
    assert not [event for event in events if event.get("event") == "error"]
    assert summary["clients"] == 1
    assert summary["bytes_received"] == 0
    assert summary["bytes_sent"] == 0


def test_serve_tcp_connection_storm_is_bounded(monkeypatch) -> None:
    """40 个并发连接 + max_clients 2：拒绝事件受全局上限约束，服务按 duration 结束。"""
    monkeypatch.setattr(net_io, "MAX_ERROR_EVENTS", 3)
    events: list[dict] = []
    ports: list[int] = []
    ready = threading.Event()
    summary: dict = {}
    failures: list[BaseException] = []

    def on_listening(port: int) -> None:
        ports.append(port)
        ready.set()

    def run_server() -> None:
        try:
            summary.update(
                net_io.serve_tcp(
                    "127.0.0.1",
                    0,
                    duration=1,
                    max_clients=2,
                    emit=events.append,
                    on_listening=on_listening,
                )
            )
        except BaseException as exc:  # 测试要断言连接风暴没有把服务端打崩
            failures.append(exc)

    started = time.monotonic()
    server = threading.Thread(target=run_server, daemon=True)
    server.start()
    clients: list[socket.socket] = []
    try:
        assert ready.wait(timeout=3)
        clients = _storm_clients(ports[0], count=40)
    finally:
        for sock in clients:
            sock.close()
        server.join(timeout=5)
    elapsed = time.monotonic() - started
    assert not server.is_alive()
    assert failures == []

    errors = [event for event in events if event.get("event") == "error"]
    assert all(event["code"] == "max_clients" for event in errors)
    assert len(errors) <= 3
    assert len(errors) == min(summary["rejected"], 3)
    assert summary["clients"] == 2
    assert summary["rejected"] >= 3
    assert 0.9 <= summary["duration_s"] <= 2.0
    assert 0.9 <= elapsed <= 2.5


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_server_reply_and_greeting(cli) -> None:
    port = _free_port()
    received: dict[str, bytes] = {}

    def client_run() -> None:
        sock = _connect(port)
        try:
            received["greeting"] = _recv_until(sock, b"HELLO")
            sock.sendall(b"ping")
            received["reply"] = _recv_until(sock, b"OK")
        finally:
            sock.close()

    with _client_thread(client_run):
        payload = cli(
            [
                "net",
                "server",
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--duration",
                "1",
                "--reply",
                "OK",
                "--greeting",
                "HELLO",
            ]
        )

    assert payload["exit_code"] == 0
    assert payload["command"] == "net.server"
    assert received["greeting"] == b"HELLO"
    assert received["reply"] == b"OK"

    events = _events(payload)
    envelope = events[-1]
    assert envelope["ok"] is True
    assert payload["data"] == envelope["data"]

    kinds = _kinds(events)
    assert kinds[0] == "listening"
    assert kinds[-1] == "end"
    assert kinds.count("client_connected") == 1
    assert kinds.count("client_data") == 1

    data_event = next(event for event in events if event.get("event") == "client_data")
    assert data_event["hex"] == "70696e67"
    assert data_event["text"] == "ping"
    assert data_event["size"] == 4
    assert data_event["address"].startswith("127.0.0.1:")

    summary = payload["data"]
    assert summary["port"] == port
    assert summary["clients"] == 1
    assert summary["bytes_received"] == 4
    assert summary["bytes_sent"] == len(b"HELLO") + len(b"OK")


def test_cli_server_without_clients_listens_and_ends(cli) -> None:
    payload = cli(["net", "server", "--duration", "1"])

    assert payload["exit_code"] == 0
    events = _events(payload)
    assert _kinds(events) == ["listening", "end"]

    listening = events[0]
    assert listening["bind"] == "127.0.0.1"
    assert listening["port"] > 0
    assert isinstance(listening["at"], str)

    summary = payload["data"]
    assert summary["port"] == listening["port"]
    assert summary["clients"] == 0
    assert summary["bytes_received"] == 0
    assert summary["bytes_sent"] == 0
    assert summary["events"] == 0
    assert summary["duration_s"] >= 0.9


def test_cli_server_max_clients_rejects_extra(cli) -> None:
    port = _free_port()
    shared: dict[str, bytes] = {}
    first_echoed = threading.Event()
    reject_done = threading.Event()

    def first_client() -> None:
        sock = _connect(port)
        try:
            sock.sendall(b"a")
            shared["first"] = _recv_until(sock, b"a")
            first_echoed.set()
            assert reject_done.wait(timeout=_CLIENT_WAIT)
            sock.sendall(b"b")
            shared["second"] = _recv_until(sock, b"b")
        finally:
            sock.close()

    def second_client() -> None:
        assert first_echoed.wait(timeout=_CLIENT_WAIT)
        sock = _connect(port)
        try:
            sock.sendall(b"x")
            try:
                shared["rejected"] = sock.recv(64)
            except OSError:
                shared["rejected"] = b""
        finally:
            reject_done.set()
            sock.close()

    with _client_thread(first_client), _client_thread(second_client):
        payload = cli(
            [
                "net",
                "server",
                "--duration",
                "1",
                "--port",
                str(port),
                "--max-clients",
                "1",
                "--echo",
            ]
        )

    assert payload["exit_code"] == 0
    assert shared["first"] == b"a"
    assert shared["second"] == b"b"
    assert shared["rejected"] == b""

    events = _events(payload)
    assert _kinds(events)[0] == "listening"
    assert _kinds(events)[-1] == "end"
    errors = [event for event in events if event.get("event") == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "max_clients"
    assert "max_clients" not in events[-1]

    summary = payload["data"]
    assert summary["clients"] == 1
    assert summary["rejected"] == 1
    assert summary["bytes_received"] == 2
    assert summary["bytes_sent"] == 2


def test_cli_server_max_bytes_ends_early(cli) -> None:
    port = _free_port()

    def client_run() -> None:
        sock = _connect(port)
        try:
            sock.sendall(b"abcdefgh")
        finally:
            sock.close()

    with _client_thread(client_run):
        payload = cli(
            [
                "net",
                "server",
                "--duration",
                "1",
                "--port",
                str(port),
                "--max-bytes",
                "4",
            ]
        )

    assert payload["exit_code"] == 0
    events = _events(payload)
    assert _kinds(events)[-1] == "end"

    # 内核可能把一次 sendall 拆成多个块，因此按累计字节而非「单次 recv 整块」断言。
    data_events = [event for event in events if event.get("event") == "client_data"]
    assert sum(event["size"] for event in data_events) == 4
    assert "".join(event["hex"] for event in data_events) == "61626364"

    summary = payload["data"]
    assert summary["bytes_received"] == 4
    assert summary["events"] == len(data_events)
    assert summary["duration_s"] < 0.8
    assert payload["elapsed_ms"] < 1500


def test_cli_server_max_events_ends_early(cli) -> None:
    port = _free_port()

    def client_run() -> None:
        sock = _connect(port)
        try:
            sock.sendall(b"one")
            time.sleep(0.1)
            try:
                sock.sendall(b"two")
            except OSError:
                pass
        finally:
            sock.close()

    with _client_thread(client_run):
        payload = cli(
            [
                "net",
                "server",
                "--duration",
                "1",
                "--port",
                str(port),
                "--max-events",
                "1",
            ]
        )

    assert payload["exit_code"] == 0
    events = _events(payload)
    data_events = [event for event in events if event.get("event") == "client_data"]
    summary = payload["data"]
    # 单次 sendall 可能被内核拆包，这里只断言「1 个事件」与累计字节。
    assert summary["events"] == 1
    assert len(data_events) == 1
    assert summary["bytes_received"] == data_events[0]["size"]
    assert summary["bytes_received"] < 6
    assert summary["duration_s"] < 0.8
    assert payload["elapsed_ms"] < 1500


def test_cli_server_connection_failed(cli) -> None:
    """端口已被占用：只输出错误信封，以 connection_failed（退出码 3）结束。"""
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = int(occupied.getsockname()[1])
    try:
        payload = cli(["net", "server", "--duration", "1", "--port", str(port)])
    finally:
        occupied.close()

    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "connection_failed"
    assert str(port) in payload["error"]["message"]


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["net", "server"],
        ["net", "server", "--duration", "1", "--pretty"],
        ["net", "server", "--duration", "1", "--until", "x"],
        ["net", "server", "--duration", "1", "--until-hex", "00"],
        ["net", "server", "--duration", "1", "--echo", "--reply", "OK"],
        ["net", "server", "--duration", "1", "--port", "-1"],
        ["net", "server", "--duration", "1", "--port", "65536"],
        ["net", "server", "--duration", "1", "--max-clients", "0"],
        ["net", "server", "--duration", "0.5"],
        ["net", "server", "--duration", "3601"],
        ["net", "server", "--duration", "1", "--max-bytes", "0"],
        ["net", "server", "--duration", "1", "--max-events", "0"],
    ],
)
def test_server_invalid_options_are_usage_errors(cli, argv) -> None:
    payload = cli(argv)
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


def test_server_rejects_text_format(cli) -> None:
    # --format text 的错误信封本身也按 text 渲染，因此只能看退出码与 raw。
    payload = cli(["net", "server", "--duration", "1", "--format", "text"])
    assert payload["exit_code"] == 2
    assert "usage" in payload["raw"]

"""``net`` 命令组：TCP/UDP 收发、只收流、ping、resolve 与 adapters 的 CLI 契约。

网络用例只走本机回环：服务端监听 ``127.0.0.1`` 的系统分配端口，``cli`` 调用完成后
在 ``finally`` 里关闭 socket 与线程；测试不访问外网、不依赖真实 DNS（ping/resolve/
adapters 都用 monkeypatch 替换服务函数）。
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading

import pytest

from app.cli.commands import net
from app.cli.transports import net_io
from app.services.net_tools import PingResult


# ---------------------------------------------------------------------------
# 本机回环服务与辅助
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """借系统分配一个空闲端口后立即释放（用于未监听端口/客户端本地端口）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@contextlib.contextmanager
def _tcp_echo_server():
    """单连接 TCP echo 服务：收一次数据后原样回发并关闭。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = int(server.getsockname()[1])

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        try:
            with conn:
                data = conn.recv(4096)
                if data:
                    conn.sendall(data)
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.close()
        thread.join(timeout=2)


@contextlib.contextmanager
def _udp_echo_server():
    """UDP echo 服务：每个数据报原样回发到来源地址。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(0.2)
    port = int(server.getsockname()[1])
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                data, addr = server.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            if not data:
                continue
            try:
                server.sendto(data, addr)
            except OSError:
                return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        server.close()
        thread.join(timeout=2)


def _send_udp(port: int, payload: bytes, count: int = 1) -> None:
    """向本机回环端口发送 ``count`` 个数据报。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(count):
            sock.sendto(payload, ("127.0.0.1", port))
    finally:
        sock.close()


def _install_listen_sender(
    monkeypatch, payload: bytes, *, count: int = 1
) -> list[threading.Thread]:
    """在 ``start`` 事件里拿到实际绑定端口，并从发送线程向它发包。"""
    threads: list[threading.Thread] = []
    real_emit = net.emit_event

    def fake_emit(event: dict) -> None:
        real_emit(event)
        if event.get("event") != "start":
            return
        thread = threading.Thread(
            target=_send_udp,
            args=(int(event["port"]), payload, count),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    monkeypatch.setattr(net, "emit_event", fake_emit)
    return threads


def _join(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=2)


def _events(payload: dict) -> list[dict]:
    """把 ``raw`` 里的 NDJSON 行解析成事件列表（最后一项是标准信封）。"""
    lines = [line for line in payload["raw"].splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# TCP
# ---------------------------------------------------------------------------


def test_tcp_echo_with_expect_and_local_port(cli) -> None:
    local_port = _free_port()
    with _tcp_echo_server() as port:
        payload = cli(
            [
                "net",
                "tcp",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--text",
                "ping",
                "--expect",
                "ping",
                "--local-port",
                str(local_port),
                "--timeout",
                "2",
            ]
        )

    assert payload["exit_code"] == 0
    assert payload["command"] == "net.tcp"
    data = payload["data"]
    assert data["sent"] == {"bytes": 4, "writes": 1}
    assert data["received"] == {
        "hex": "70696e67",
        "text": "ping",
        "size": 4,
        "chunks": 1,
    }
    assert data["matched"] is True
    assert data["remote"] == f"127.0.0.1:{port}"
    assert data["local_port"] == local_port


def test_tcp_read_for_zero_skips_read(cli) -> None:
    with _tcp_echo_server() as port:
        payload = cli(
            [
                "net",
                "tcp",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--text",
                "ping",
                "--read-for",
                "0",
            ]
        )

    assert payload["exit_code"] == 0
    data = payload["data"]
    assert data["matched"] is False
    assert data["received"]["size"] == 0
    assert data["received"]["chunks"] == 0
    assert data["remote"] == f"127.0.0.1:{port}"
    assert data["local_port"] != 0


def test_tcp_connection_refused_maps_to_connection_failed(cli) -> None:
    port = _free_port()
    payload = cli(
        [
            "net",
            "tcp",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--text",
            "x",
            "--timeout",
            "1",
        ]
    )

    assert payload["exit_code"] == 3
    assert payload["command"] == "net.tcp"
    assert payload["error"]["code"] == "connection_failed"
    assert f"127.0.0.1:{port}" in payload["error"]["message"]


def test_tcp_expect_timeout_exits_4(cli) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = int(server.getsockname()[1])
    try:
        payload = cli(
            [
                "net",
                "tcp",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--text",
                "ping",
                "--expect",
                "never",
                "--timeout",
                "0",
            ]
        )
    finally:
        server.close()

    assert payload["exit_code"] == 4
    assert payload["error"]["code"] == "timeout"
    assert f"127.0.0.1:{port}" in payload["error"]["message"]


def test_tcp_expect_timeout_when_server_closes(cli) -> None:
    """对端读完即关闭：recv 返回 b"" 后仍按截止时间结束，不因 EOF 忙等或报错。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = int(server.getsockname()[1])

    def serve() -> None:
        conn, _ = server.accept()
        try:
            conn.recv(4096)
        finally:
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        payload = cli(
            [
                "net",
                "tcp",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--text",
                "ping",
                "--expect",
                "never",
                "--timeout",
                "0.2",
            ]
        )
    finally:
        server.close()
        thread.join(timeout=2)

    assert payload["exit_code"] == 4
    assert payload["error"]["code"] == "timeout"
    assert payload["elapsed_ms"] < 2000


def test_fetch_tcp_sleeps_when_peer_closed(monkeypatch) -> None:
    """``recv`` 返回 b"" 时 sleep 一个轮询窗口，避免 read_until 空转。"""
    sleeps: list[float] = []
    monkeypatch.setattr(net_io.time, "sleep", sleeps.append)

    client, server = socket.socketpair()
    try:
        server.close()
        assert net_io._fetch_tcp(client, 0.05, "127.0.0.1", 1) == b""
    finally:
        client.close()
    assert sleeps == [0.05]


# ---------------------------------------------------------------------------
# UDP 发送
# ---------------------------------------------------------------------------


def test_udp_echo_with_expect(cli) -> None:
    with _udp_echo_server() as port:
        payload = cli(
            [
                "net",
                "udp",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--text",
                "hi",
                "--expect",
                "hi",
                "--timeout",
                "2",
            ]
        )

    assert payload["exit_code"] == 0
    assert payload["command"] == "net.udp"
    data = payload["data"]
    assert data["sent"] == {"bytes": 2, "writes": 1}
    assert data["received"]["text"] == "hi"
    assert data["received"]["chunks"] == 1
    assert data["matched"] is True
    assert data["remote"] == f"127.0.0.1:{port}"
    assert data["local_port"] != 0
    assert "from" not in data


# ---------------------------------------------------------------------------
# UDP 只收模式
# ---------------------------------------------------------------------------


def test_udp_listen_only_streams_events(cli, monkeypatch) -> None:
    threads = _install_listen_sender(monkeypatch, b"ping")
    payload = cli(["net", "udp", "--listen-only", "--duration", "1"])
    _join(threads)

    assert payload["exit_code"] == 0
    assert payload["command"] == "net.udp"

    events = _events(payload)
    assert [event.get("event") for event in events[:-1]] == ["start", "data", "end"]
    envelope = events[-1]
    assert envelope["ok"] is True
    assert payload["data"] == envelope["data"]

    start = events[0]
    assert start["port"] > 0
    assert isinstance(start["at"], str)

    data_event = events[1]
    assert data_event["hex"] == "70696e67"
    assert data_event["text"] == "ping"
    assert data_event["size"] == 4
    assert data_event["from"].startswith("127.0.0.1:")
    assert isinstance(data_event["at"], str)

    summary = payload["data"]
    assert summary["port"] == start["port"]
    assert summary["bytes"] == 4
    assert summary["events"] == 1
    assert summary["duration_s"] >= 0

    end = events[2]
    assert end["matched"] is False
    assert end["bytes"] == 4


def test_udp_listen_max_bytes_truncates_and_decodes(cli, monkeypatch) -> None:
    threads = _install_listen_sender(monkeypatch, b"\xc4\xe3AB")
    payload = cli(
        [
            "net",
            "udp",
            "--listen-only",
            "--duration",
            "1",
            "--max-bytes",
            "2",
            "--encoding",
            "gbk",
        ]
    )
    _join(threads)

    assert payload["exit_code"] == 0
    events = _events(payload)
    assert [event.get("event") for event in events[:-1]] == ["start", "data", "end"]
    assert events[1]["hex"] == "c4e3"
    assert events[1]["text"] == "你"
    assert events[1]["size"] == 2
    assert payload["data"]["bytes"] == 2
    assert payload["data"]["events"] == 1


def test_udp_listen_max_events_stops_after_first(cli, monkeypatch) -> None:
    threads = _install_listen_sender(monkeypatch, b"one", count=2)
    payload = cli(
        ["net", "udp", "--listen-only", "--duration", "1", "--max-events", "1"]
    )
    _join(threads)

    assert payload["exit_code"] == 0
    events = _events(payload)
    assert [event.get("event") for event in events[:-1]] == ["start", "data", "end"]
    assert payload["data"]["events"] == 1
    assert payload["data"]["bytes"] == 3


def test_udp_listen_binds_all_ipv4_interfaces(monkeypatch) -> None:
    """只收模式绑定所有 IPv4 接口（地址为空串），而不是仅回环。"""
    binds: list[tuple] = []
    closed: list[bool] = []
    real_socket = socket.socket

    class SpySocket:
        def __init__(self, *args, **kwargs):
            self._socket = real_socket(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._socket, name)

        def bind(self, address):
            binds.append(address)
            return self._socket.bind(address)

        def close(self):
            closed.append(True)
            return self._socket.close()

    monkeypatch.setattr(net_io.socket, "socket", SpySocket)
    monkeypatch.setattr(net_io, "_clock", lambda: 0.0)
    summary = net_io.udp_listen(0, duration=0, emit=lambda event: None)

    assert binds == [("", 0)]
    assert closed == [True]
    assert summary["events"] == 0
    assert summary["port"] > 0


@pytest.mark.parametrize(
    "extra",
    [
        ["--text", "hi"],
        ["--hex", "00"],
        ["--repeat", "2"],
        ["--interval-ms", "50"],
        ["--expect", "x"],
        ["--expect-hex", "00"],
        ["--read-for", "1"],
        ["--pretty"],
        ["--until", "x"],
    ],
)
def test_udp_listen_only_rejects_send_and_text_options(cli, extra) -> None:
    payload = cli(["net", "udp", "--listen-only", "--duration", "1", *extra])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


def test_udp_listen_only_rejects_text_format(cli) -> None:
    # --format text 的错误信封本身也按 text 渲染，因此只能看退出码与 raw。
    payload = cli(["net", "udp", "--listen-only", "--duration", "1", "--format", "text"])
    assert payload["exit_code"] == 2
    assert "usage" in payload["raw"]


def test_udp_listen_only_requires_duration(cli) -> None:
    payload = cli(["net", "udp", "--listen-only"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert "--duration" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# ping / resolve / adapters
# ---------------------------------------------------------------------------


def test_ping_uses_service_and_returns_fields(cli, monkeypatch) -> None:
    calls: dict = {}

    def fake_ping(host, count=4, timeout_s=15):
        calls.update({"host": host, "count": count, "timeout_s": timeout_s})
        return PingResult(True, "reply from 127.0.0.1", host)

    monkeypatch.setattr(net, "ping_host", fake_ping)
    payload = cli(
        ["net", "ping", "--host", "example.local", "--count", "2", "--timeout", "3"]
    )

    assert payload["exit_code"] == 0
    assert payload["command"] == "net.ping"
    assert payload["data"] == {
        "host": "example.local",
        "ok": True,
        "output": "reply from 127.0.0.1",
        "count": 2,
    }
    assert calls == {"host": "example.local", "count": 2, "timeout_s": 3.0}


def test_ping_failure_is_a_result_not_an_error(cli, monkeypatch) -> None:
    def fake_ping(host, count=4, timeout_s=15):
        return PingResult(False, "请求超时", host)

    monkeypatch.setattr(net, "ping_host", fake_ping)
    payload = cli(["net", "ping", "--host", "10.0.0.1"])

    assert payload["exit_code"] == 0
    assert payload["data"]["ok"] is False
    assert payload["data"]["output"] == "请求超时"
    assert payload["data"]["count"] == 4


def test_resolve_returns_addresses(cli, monkeypatch) -> None:
    monkeypatch.setattr(
        net, "resolve_host", lambda host: ["127.0.0.1", "::1"]
    )
    payload = cli(["net", "resolve", "--host", "example.local"])

    assert payload["exit_code"] == 0
    assert payload["command"] == "net.resolve"
    assert payload["data"] == {
        "host": "example.local",
        "addresses": ["127.0.0.1", "::1"],
        "count": 2,
    }


def test_resolve_failure_maps_to_not_found(cli, monkeypatch) -> None:
    def boom(host):
        raise ValueError(f"无法解析“{host}”。")

    monkeypatch.setattr(net, "resolve_host", boom)
    payload = cli(["net", "resolve", "--host", "bad.local"])

    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"
    assert "bad.local" in payload["error"]["message"]


def test_adapters_returns_list(cli, monkeypatch) -> None:
    monkeypatch.setattr(net, "adapter_info", lambda: [{"name": "eth0"}])
    payload = cli(["net", "adapters"])

    assert payload["exit_code"] == 0
    assert payload["command"] == "net.adapters"
    assert payload["data"] == {"adapters": [{"name": "eth0"}], "count": 1}


def test_adapters_failure_maps_to_unsupported(cli, monkeypatch) -> None:
    def boom():
        raise RuntimeError("读取网卡信息失败：psutil 不可用。")

    monkeypatch.setattr(net, "adapter_info", boom)
    payload = cli(["net", "adapters"])

    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "unsupported"


def test_ping_accepts_text_format(cli, monkeypatch) -> None:
    monkeypatch.setattr(
        net, "ping_host", lambda host, count=4, timeout_s=15: PingResult(True, "ok", host)
    )
    payload = cli(["net", "ping", "--host", "example.local", "--format", "text"])

    assert payload["exit_code"] == 0
    assert "ok: true" in payload["raw"]
    assert "command: net.ping" in payload["raw"]


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        # 端口范围
        ["net", "tcp", "--host", "127.0.0.1", "--port", "0", "--text", "x"],
        ["net", "tcp", "--host", "127.0.0.1", "--port", "65536", "--text", "x"],
        [
            "net",
            "tcp",
            "--host",
            "127.0.0.1",
            "--port",
            "1",
            "--local-port",
            "65536",
            "--text",
            "x",
        ],
        ["net", "udp", "--host", "127.0.0.1", "--port", "1", "--bind-port", "-1", "--text", "x"],
        ["net", "udp", "--host", "127.0.0.1", "--port", "65536", "--text", "x"],
        # 超时 / 读取窗口
        ["net", "tcp", "--host", "127.0.0.1", "--port", "1", "--text", "x", "--timeout", "nan"],
        ["net", "tcp", "--host", "127.0.0.1", "--port", "1", "--text", "x", "--timeout", "inf"],
        ["net", "tcp", "--host", "127.0.0.1", "--port", "1", "--text", "x", "--timeout", "-1"],
        ["net", "tcp", "--host", "127.0.0.1", "--port", "1", "--text", "x", "--read-for", "-1"],
        ["net", "udp", "--host", "127.0.0.1", "--port", "1", "--text", "x", "--timeout", "inf"],
        ["net", "udp", "--host", "127.0.0.1", "--port", "1", "--text", "x", "--read-for", "nan"],
        # 重复次数与间隔
        ["net", "tcp", "--host", "127.0.0.1", "--port", "1", "--text", "x", "--repeat", "0"],
        ["net", "udp", "--host", "127.0.0.1", "--port", "1", "--text", "x", "--interval-ms", "-1"],
        # 输入与匹配器
        ["net", "tcp", "--host", "127.0.0.1", "--port", "1"],
        ["net", "udp", "--host", "127.0.0.1", "--port", "1"],
        [
            "net",
            "tcp",
            "--host",
            "127.0.0.1",
            "--port",
            "1",
            "--text",
            "x",
            "--expect",
            "a",
            "--expect-hex",
            "00",
        ],
        # 编码
        ["net", "tcp", "--host", "127.0.0.1", "--port", "1", "--text", "x", "--encoding", "nope"],
        [
            "net",
            "udp",
            "--listen-only",
            "--duration",
            "1",
            "--encoding",
            "nope",
        ],
        # 流式时长
        ["net", "udp", "--listen-only", "--duration", "0.5"],
        ["net", "udp", "--listen-only", "--duration", "3601"],
        ["net", "udp", "--listen-only", "--duration", "1", "--max-bytes", "0"],
        # ping 参数
        ["net", "ping", "--host", "example.local", "--timeout", "nan"],
        ["net", "ping", "--host", "example.local", "--count", "0"],
    ],
)
def test_invalid_options_are_usage_errors(cli, argv) -> None:
    payload = cli(argv)
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

"""Network client controller tests against local loopback echo servers."""

from __future__ import annotations

from PyQt6.QtNetwork import QHostAddress, QHostInfo, QTcpServer, QUdpSocket

from app.io import net_client as net_client_module
from app.io.net_client import NetClientController
from app.ui.qt_utils import process_events_until


class TcpEchoServer:
    def __init__(self) -> None:
        self.server = QTcpServer()
        self.connections = []
        self.server.newConnection.connect(self._accept)

    def start(self) -> int:
        assert self.server.listen(QHostAddress("127.0.0.1"), 0)
        return int(self.server.serverPort())

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                break
            self.connections.append(socket)
            socket.readyRead.connect(lambda current=socket: self._echo(current))

    @staticmethod
    def _echo(socket) -> None:
        data = bytes(socket.readAll())
        if data:
            socket.write(data)

    def stop(self) -> None:
        for socket in self.connections:
            socket.abort()
            socket.deleteLater()
        self.connections.clear()
        self.server.close()


class UdpEchoServer:
    def __init__(self) -> None:
        self.socket = QUdpSocket()
        self.socket.readyRead.connect(self._echo)

    def start(self) -> int:
        assert self.socket.bind(QHostAddress("127.0.0.1"), 0)
        return int(self.socket.localPort())

    def _echo(self) -> None:
        while self.socket.hasPendingDatagrams():
            data, host, port = self.socket.readDatagram(65535)
            if data:
                self.socket.writeDatagram(data, host, port)

    def stop(self) -> None:
        self.socket.close()


def test_tcp_connect_send_receive_and_disconnect(qapp) -> None:
    echo = TcpEchoServer()
    port = echo.start()
    controller = NetClientController()
    states: list[str] = []
    received: list[bytes] = []
    errors: list[str] = []
    controller.tcp_state_changed.connect(states.append)
    controller.tcp_data.connect(received.append)
    controller.error.connect(errors.append)
    try:
        controller.connect_tcp("127.0.0.1", port)
        assert process_events_until(controller.is_tcp_connected, timeout_ms=3000)
        assert states[0] == "connecting"
        assert "connected" in states
        assert errors == []
        assert controller.tcp_local_port() > 0

        assert controller.send_tcp(b"hello") is True
        assert process_events_until(
            lambda: b"hello" in received, timeout_ms=3000
        ), received

        controller.disconnect_tcp()
        assert process_events_until(
            lambda: not controller.is_tcp_connected(), timeout_ms=3000
        )
        assert states[-1] == "disconnected"
        assert controller.send_tcp(b"x") is False
        assert errors and "尚未连接" in errors[-1]
    finally:
        controller.disconnect_tcp()
        echo.stop()


def test_tcp_connection_refused_reports_readable_error(qapp) -> None:
    probe = QTcpServer()
    assert probe.listen(QHostAddress("127.0.0.1"), 0)
    port = int(probe.serverPort())
    probe.close()

    controller = NetClientController()
    errors: list[str] = []
    states: list[str] = []
    controller.error.connect(errors.append)
    controller.tcp_state_changed.connect(states.append)
    try:
        controller.connect_tcp("127.0.0.1", port)
        assert process_events_until(lambda: bool(errors), timeout_ms=8000), errors
        assert "拒绝" in errors[-1]
        assert not controller.is_tcp_connected()
        assert states[-1] == "disconnected"
    finally:
        controller.disconnect_tcp()


def test_tcp_invalid_arguments_emit_errors(qapp) -> None:
    controller = NetClientController()
    errors: list[str] = []
    controller.error.connect(errors.append)
    try:
        controller.connect_tcp("", 8080)
        assert errors and "目标地址" in errors[-1]
        controller.connect_tcp("127.0.0.1", 0)
        assert "目标端口" in errors[-1]
        controller.connect_tcp("127.0.0.1", 8080, local_port=70000)
        assert "本地端口" in errors[-1]
    finally:
        controller.disconnect_tcp()


def test_udp_bind_send_receive_and_close(qapp) -> None:
    echo = UdpEchoServer()
    echo_port = echo.start()
    controller = NetClientController()
    datagrams: list[bytes] = []
    states: list[str] = []
    errors: list[str] = []
    controller.udp_data.connect(datagrams.append)
    controller.udp_state_changed.connect(states.append)
    controller.error.connect(errors.append)
    try:
        controller.bind_udp(0)
        assert controller.is_udp_bound()
        assert controller.udp_local_port() > 0
        assert states == ["bound"]

        assert controller.send_udp(b"ping", "127.0.0.1", echo_port) is True
        assert process_events_until(
            lambda: b"ping" in datagrams, timeout_ms=3000
        ), datagrams
        assert errors == []

        controller.close_udp()
        assert not controller.is_udp_bound()
        assert states[-1] == "closed"
        assert controller.send_udp(b"x", "127.0.0.1", echo_port) is False
        assert "尚未绑定" in errors[-1]
    finally:
        controller.close_udp()
        echo.stop()


def test_udp_bind_port_in_use_emits_error(qapp) -> None:
    blocker = QUdpSocket()
    assert blocker.bind(QHostAddress.SpecialAddress.AnyIPv4, 0)
    port = int(blocker.localPort())
    controller = NetClientController()
    errors: list[str] = []
    controller.error.connect(errors.append)
    try:
        controller.bind_udp(port)
        assert errors, "占用端口时应发出错误信号"
        assert any("绑定失败" in message for message in errors), errors
        assert not controller.is_udp_bound()
    finally:
        controller.close_udp()
        blocker.close()


class _FakeHostInfo:
    """QHostInfo 的最小替身，避免测试依赖真实 DNS。"""

    def __init__(
        self,
        addresses=(),
        error=QHostInfo.HostInfoError.NoError,
        lookup_id: int = 0,
    ) -> None:
        self._addresses = list(addresses)
        self._error = error
        self._lookup_id = int(lookup_id)

    def error(self):
        return self._error

    def addresses(self):
        return list(self._addresses)

    def lookupId(self):
        return self._lookup_id


def test_udp_hostname_lookup_is_async_and_cached(qapp, monkeypatch) -> None:
    echo = UdpEchoServer()
    echo_port = echo.start()
    controller = NetClientController()
    datagrams: list[bytes] = []
    errors: list[str] = []
    controller.udp_data.connect(datagrams.append)
    controller.error.connect(errors.append)
    lookups: list[tuple[str, object]] = []

    def fake_lookup(host: str, callback) -> int:
        lookups.append((host, callback))
        return 99

    monkeypatch.setattr(net_client_module, "_lookup_host_name", fake_lookup)
    try:
        controller.bind_udp(0)
        assert controller.send_udp(b"async", "example.local", echo_port) is True
        assert lookups and lookups[0][0] == "example.local"
        # 解析尚未完成：不应阻塞，也不应提前写入套接字。
        assert datagrams == []

        callback = lookups[0][1]
        callback(_FakeHostInfo([QHostAddress("127.0.0.1")], lookup_id=99))
        assert process_events_until(
            lambda: b"async" in datagrams, timeout_ms=3000
        ), datagrams
        assert errors == []

        # 同一个域名第二次发送命中缓存，不再发起 DNS 查询。
        assert controller.send_udp(b"again", "example.local", echo_port) is True
        assert len(lookups) == 1
        assert process_events_until(
            lambda: b"again" in datagrams, timeout_ms=3000
        ), datagrams
    finally:
        controller.close_udp()
        echo.stop()


def test_udp_hostname_lookup_failure_emits_readable_error(qapp, monkeypatch) -> None:
    controller = NetClientController()
    errors: list[str] = []
    controller.error.connect(errors.append)
    lookups: list[object] = []

    def fake_lookup(host: str, callback) -> int:
        lookups.append(callback)
        return 100

    monkeypatch.setattr(net_client_module, "_lookup_host_name", fake_lookup)
    try:
        controller.bind_udp(0)
        assert controller.send_udp(b"x", "missing.local", 9) is True
        lookups[0](
            _FakeHostInfo(
                error=QHostInfo.HostInfoError.HostNotFound, lookup_id=100
            )
        )
        assert errors and "无法解析" in errors[-1]
    finally:
        controller.close_udp()


def test_udp_hostname_rapid_sends_queue_and_stale_callbacks_are_ignored(qapp, monkeypatch) -> None:
    receiver = QUdpSocket()
    assert receiver.bind(QHostAddress("127.0.0.1"), 0)
    receive_port = int(receiver.localPort())
    controller = NetClientController()
    sent: list[bytes] = []
    controller.udp_sent.connect(sent.append)
    lookups: list[tuple[str, int, object]] = []
    next_id = 201

    def fake_lookup(host: str, callback) -> int:
        nonlocal next_id
        lookup_id = next_id
        next_id += 1
        lookups.append((host, lookup_id, callback))
        return lookup_id

    monkeypatch.setattr(net_client_module, "_lookup_host_name", fake_lookup)
    try:
        controller.bind_udp(0)
        assert controller.send_udp(b"a1", "a.local", receive_port)
        assert controller.send_udp(b"a2", "a.local", receive_port)
        assert controller.send_udp(b"b1", "b.local", receive_port)
        assert [name for name, _id, _cb in lookups] == ["a.local", "b.local"]

        by_host = {name: (lookup_id, callback) for name, lookup_id, callback in lookups}
        a_id, a_callback = by_host["a.local"]
        b_id, b_callback = by_host["b.local"]
        a_callback(_FakeHostInfo([QHostAddress("127.0.0.1")], lookup_id=a_id))
        # 陈旧/重复回调不能再次消费队列。
        a_callback(_FakeHostInfo([QHostAddress("127.0.0.1")], lookup_id=a_id))
        b_callback(_FakeHostInfo([QHostAddress("127.0.0.1")], lookup_id=b_id))
        assert process_events_until(lambda: len(sent) >= 3, timeout_ms=3000), sent
        assert sorted(sent) == [b"a1", b"a2", b"b1"]
    finally:
        controller.close_udp()
        receiver.close()

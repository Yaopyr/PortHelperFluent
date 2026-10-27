"""TCP server controller tests with real loopback client sockets."""

from __future__ import annotations

from PyQt6.QtNetwork import QHostAddress, QTcpSocket

from app.io.net_server import NetServerController
from app.ui.qt_utils import process_events_until


def _connect_client(port: int) -> QTcpSocket:
    socket = QTcpSocket()
    socket.connectToHost(QHostAddress("127.0.0.1"), port)
    return socket


def _close_clients(clients) -> None:
    for socket in clients:
        try:
            socket.abort()
            socket.deleteLater()
        except Exception:
            pass


def test_start_on_port_zero_reports_actual_port(qapp) -> None:
    server = NetServerController()
    started: list[int] = []
    server.started.connect(started.append)
    try:
        assert server.start("0.0.0.0", 0, max_clients=4) is True
        assert server.is_listening()
        assert server.server_port() > 0
        assert started == [server.server_port()]
    finally:
        server.stop()


def test_two_clients_receive_send_to_selected_broadcast_and_disconnect(qapp) -> None:
    server = NetServerController()
    connected: list[str] = []
    disconnected: list[str] = []
    received: list[tuple[str, bytes]] = []
    errors: list[str] = []
    server.client_connected.connect(connected.append)
    server.client_disconnected.connect(disconnected.append)
    server.data_received.connect(lambda cid, data: received.append((cid, bytes(data))))
    server.error.connect(errors.append)
    clients: list[QTcpSocket] = []
    try:
        assert server.start("0.0.0.0", 0, max_clients=4)
        port = server.server_port()
        clients = [_connect_client(port), _connect_client(port)]
        assert process_events_until(
            lambda: len(server.clients()) == 2, timeout_ms=5000
        ), server.clients()
        assert len(connected) == 2
        assert errors == []

        first_client = clients[0]
        first_client.write(b"hello")
        assert process_events_until(lambda: bool(received), timeout_ms=5000), received
        client_id, payload = received[0]
        assert payload == b"hello"
        info = next(item for item in server.clients() if item.client_id == client_id)
        assert info.rx_bytes == 5
        assert info.address in ("127.0.0.1", "::1")

        target = next(
            socket for socket in clients if socket.localPort() == info.port
        )
        other = next(socket for socket in clients if socket is not target)
        assert server.send_to(client_id, b"reply") is True
        assert process_events_until(
            lambda: target.bytesAvailable() >= 5, timeout_ms=5000
        )
        assert bytes(target.readAll()) == b"reply"
        assert other.bytesAvailable() == 0

        assert server.broadcast(b"all") == 2
        assert process_events_until(
            lambda: all(socket.bytesAvailable() >= 3 for socket in clients),
            timeout_ms=5000,
        )
        assert bytes(target.readAll()) == b"all"
        assert bytes(other.readAll()) == b"all"

        snapshots = {item.client_id: item for item in server.clients()}
        assert snapshots[client_id].tx_bytes == 8  # reply(5) + broadcast(3)
        other_id = next(cid for cid in snapshots if cid != client_id)
        assert snapshots[other_id].tx_bytes == 3

        first_client.disconnectFromHost()
        assert process_events_until(
            lambda: len(server.clients()) == 1, timeout_ms=5000
        )
        assert client_id in disconnected
    finally:
        _close_clients(clients)
        server.stop()


def test_stop_closes_clients_and_emits_stopped(qapp) -> None:
    server = NetServerController()
    stopped: list[bool] = []
    disconnected: list[str] = []
    server.stopped.connect(lambda: stopped.append(True))
    server.client_disconnected.connect(disconnected.append)
    clients: list[QTcpSocket] = []
    try:
        assert server.start("0.0.0.0", 0)
        clients = [_connect_client(server.server_port())]
        assert process_events_until(
            lambda: len(server.clients()) == 1, timeout_ms=5000
        )
        server.stop()
        assert not server.is_listening()
        assert server.clients() == []
        assert stopped == [True]
        assert disconnected
        assert process_events_until(
            lambda: clients[0].state().name == "UnconnectedState", timeout_ms=5000
        )
    finally:
        _close_clients(clients)
        server.stop()


def test_max_clients_rejects_extra_connection(qapp) -> None:
    server = NetServerController()
    errors: list[str] = []
    server.error.connect(errors.append)
    clients: list[QTcpSocket] = []
    try:
        assert server.start("0.0.0.0", 0, max_clients=1)
        clients = [
            _connect_client(server.server_port()),
            _connect_client(server.server_port()),
        ]
        assert process_events_until(lambda: bool(errors), timeout_ms=5000), errors
        assert any("最大客户端数" in message for message in errors), errors
        assert len(server.clients()) == 1
    finally:
        _close_clients(clients)
        server.stop()


def test_disconnect_client_closes_selected_socket(qapp) -> None:
    server = NetServerController()
    clients: list[QTcpSocket] = []
    try:
        assert server.start("0.0.0.0", 0)
        clients = [_connect_client(server.server_port())]
        assert process_events_until(
            lambda: len(server.clients()) == 1, timeout_ms=5000
        )
        client_id = server.clients()[0].client_id
        assert server.disconnect_client(client_id) is True
        assert process_events_until(
            lambda: server.clients() == [], timeout_ms=5000
        )
        assert server.disconnect_client(client_id) is False
    finally:
        _close_clients(clients)
        server.stop()

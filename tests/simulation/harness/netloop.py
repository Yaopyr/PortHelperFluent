"""本机回环 TCP/UDP 对端；绝不访问外部网络。"""

from __future__ import annotations

from PyQt6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket, QUdpSocket
from PyQt6.QtWidgets import QApplication

from app.ui.qt_utils import process_events_until


class TcpEchoServer:
    def __init__(self) -> None:
        self.server = QTcpServer()
        self.connections = []
        self.received = bytearray()
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

    def _echo(self, socket) -> None:
        data = bytes(socket.readAll())
        if data:
            self.received.extend(data)
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
        self.received = bytearray()
        self.socket.readyRead.connect(self._echo)

    def start(self) -> int:
        assert self.socket.bind(QHostAddress("127.0.0.1"), 0)
        return int(self.socket.localPort())

    def _echo(self) -> None:
        while self.socket.hasPendingDatagrams():
            data, host, port = self.socket.readDatagram(65535)
            if data:
                self.received.extend(data)
                self.socket.writeDatagram(data, host, port)

    def stop(self) -> None:
        self.socket.close()


class TcpTestClient:
    def __init__(self) -> None:
        self.socket = QTcpSocket()
        self.received = bytearray()
        self.socket.readyRead.connect(self._read)

    def _read(self) -> None:
        data = bytes(self.socket.readAll())
        if data:
            self.received.extend(data)

    def connect_to(self, port: int, timeout_ms: int = 5000) -> bool:
        self.socket.connectToHost(QHostAddress("127.0.0.1"), int(port))
        return process_events_until(
            lambda: self.socket.state() == QTcpSocket.SocketState.ConnectedState,
            timeout_ms=timeout_ms,
        )

    def send(self, data: bytes) -> None:
        self.socket.write(bytes(data))
        QApplication.processEvents()

    def wait_for(self, expected: bytes, timeout_ms: int = 5000) -> bool:
        return process_events_until(lambda: expected in bytes(self.received), timeout_ms=timeout_ms)

    def close(self) -> None:
        try:
            self.socket.abort()
            self.socket.deleteLater()
        except Exception:
            pass


class UdpTestPeer:
    def __init__(self) -> None:
        self.socket = QUdpSocket()
        self.received = bytearray()
        self.socket.readyRead.connect(self._read)
        assert self.socket.bind(QHostAddress("127.0.0.1"), 0)

    def _read(self) -> None:
        while self.socket.hasPendingDatagrams():
            data, _host, _port = self.socket.readDatagram(65535)
            self.received.extend(data)

    def send_to(self, port: int, data: bytes) -> None:
        self.socket.writeDatagram(bytes(data), QHostAddress("127.0.0.1"), int(port))

    def wait_for(self, expected: bytes, timeout_ms: int = 5000) -> bool:
        return process_events_until(lambda: expected in bytes(self.received), timeout_ms=timeout_ms)

    def close(self) -> None:
        self.socket.close()

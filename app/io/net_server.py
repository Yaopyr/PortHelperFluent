"""TCP 网络服务器控制器：在 Qt 事件循环内驱动 QTcpServer 与客户端套接字。

对外只暴露客户端标识、来源地址与收发计数；界面层通过信号获得通知，
不直接接触 QTcpSocket（规格 §4.3、§6）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import QAbstractSocket, QHostAddress, QTcpServer, QTcpSocket

from app.core.logging_setup import get_logger

_SOCKET_STATE = QAbstractSocket.SocketState
_SOCKET_ERROR = QAbstractSocket.SocketError

MAX_CLIENTS_LIMIT = 64


@dataclass(frozen=True)
class ClientInfo:
    """单个客户端的只读快照；client_id 在服务器生命周期内唯一。"""

    client_id: str
    address: str
    port: int
    connected_at: datetime
    rx_bytes: int
    tx_bytes: int


class _Client:
    """内部可变的客户端记录（不直接暴露给界面层）。"""

    __slots__ = (
        "client_id",
        "socket",
        "address",
        "port",
        "connected_at",
        "rx_bytes",
        "tx_bytes",
    )

    def __init__(self, client_id: str, socket: QTcpSocket) -> None:
        self.client_id = client_id
        self.socket = socket
        peer = socket.peerAddress()
        self.address = peer.toString() if peer is not None else ""
        self.port = int(socket.peerPort())
        self.connected_at = datetime.now()
        self.rx_bytes = 0
        self.tx_bytes = 0

    def snapshot(self) -> ClientInfo:
        return ClientInfo(
            client_id=self.client_id,
            address=self.address,
            port=self.port,
            connected_at=self.connected_at,
            rx_bytes=self.rx_bytes,
            tx_bytes=self.tx_bytes,
        )


class NetServerController(QObject):
    """TCP 服务器：支持监听、多客户端、按客户端发送与广播。"""

    started = pyqtSignal(int)  # 实际监听端口（支持端口 0）
    stopped = pyqtSignal()
    client_connected = pyqtSignal(str)
    client_disconnected = pyqtSignal(str)
    data_received = pyqtSignal(str, bytes)
    error = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._server.acceptError.connect(self._on_accept_error)
        self._clients: dict[str, _Client] = {}
        self._next_client_id = 1
        self._max_clients = 16

    # ---- 生命周期 -----------------------------------------------------

    def start(self, host: str, port: int, max_clients: int = 16) -> bool:
        """开始监听；port 为 0 时由系统分配，并通过 started 发射实际端口。"""
        if self._server.isListening():
            self.error.emit("服务器已在监听，请先停止后再重新启动。")
            return False
        if not _valid_port(port, allow_zero=True):
            self.error.emit("监听端口无效，请输入 0–65535 之间的端口号（0 表示自动分配）。")
            return False
        try:
            limit = int(max_clients)
        except (TypeError, ValueError):
            limit = 16
        if limit < 1:
            self.error.emit("最大客户端数必须至少为 1。")
            return False
        limit = min(limit, MAX_CLIENTS_LIMIT)

        address = _parse_listen_address(host)
        if address is None:
            self.error.emit(
                f"监听地址“{host}”无效。请选择“0.0.0.0（全部网卡）”或本机网卡地址。"
            )
            return False

        self._max_clients = limit
        if not self._server.listen(address, int(port)):
            display = (host or "0.0.0.0").strip() or "0.0.0.0"
            self.error.emit(
                f"无法在 {display}:{int(port)} 启动 TCP 服务器："
                f"{self._server.errorString()}。端口可能已被其他程序占用，"
                "或所选地址当前不可用。"
            )
            return False
        self.started.emit(int(self._server.serverPort()))
        return True

    def stop(self) -> None:
        """停止监听并断开全部客户端；清理后发射 stopped。"""
        was_active = self._server.isListening() or bool(self._clients)
        self._server.close()
        for client_id, client in list(self._clients.items()):
            self._clients.pop(client_id, None)
            try:
                # 服务端主动停止时屏蔽套接字信号，避免 disconnected 回调重复清理。
                client.socket.blockSignals(True)
                client.socket.abort()
                client.socket.deleteLater()
            except Exception:
                get_logger().exception("关闭服务器客户端套接字失败：%s", client_id)
            self.client_disconnected.emit(client_id)
        self._max_clients = 16
        if was_active:
            self.stopped.emit()

    def is_listening(self) -> bool:
        return self._server.isListening()

    def server_port(self) -> int:
        return int(self._server.serverPort())

    # ---- 客户端 -------------------------------------------------------

    def clients(self) -> list[ClientInfo]:
        """按连接顺序返回客户端快照。"""
        ordered = sorted(self._clients.values(), key=lambda item: int(item.client_id))
        return [client.snapshot() for client in ordered]

    def send_to(self, client_id: str, data: bytes) -> bool:
        client = self._clients.get(str(client_id))
        if client is None:
            self.error.emit(f"客户端 {client_id} 已断开，无法发送数据。")
            return False
        payload = bytes(data)
        if not payload:
            self.error.emit("发送内容为空，请输入要发送的数据。")
            return False
        if client.socket.state() != _SOCKET_STATE.ConnectedState:
            self.error.emit(
                f"客户端 {client.address}:{client.port} 已断开，无法发送数据。"
            )
            return False
        written = client.socket.write(payload)
        if written < 0:
            self.error.emit(
                f"向 {client.address}:{client.port} 发送失败："
                f"{client.socket.errorString()}。请检查客户端连接状态。"
            )
            return False
        client.tx_bytes += written
        if written != len(payload):
            self.error.emit(
                f"向 {client.address}:{client.port} 只发送了 "
                f"{written}/{len(payload)} 字节：{client.socket.errorString()}。"
            )
            return False
        return True

    def broadcast(self, data: bytes) -> int:
        """向所有客户端发送，返回成功发送的客户端数量。"""
        payload = bytes(data)
        if not payload:
            self.error.emit("发送内容为空，请输入要发送的数据。")
            return 0
        sent = 0
        for client_id in list(self._clients):
            if self.send_to(client_id, payload):
                sent += 1
        return sent

    def disconnect_client(self, client_id: str) -> bool:
        """主动断开指定客户端；不存在或已断开时返回 False 并给出提示。"""
        client = self._clients.get(str(client_id))
        if client is None:
            self.error.emit(f"客户端 {client_id} 已断开，无法再次断开。")
            return False
        if client.socket.state() == _SOCKET_STATE.UnconnectedState:
            self._on_client_disconnected(str(client_id))
            return True
        client.socket.disconnectFromHost()
        if client.socket.state() == _SOCKET_STATE.UnconnectedState:
            self._on_client_disconnected(str(client_id))
        return True

    # ---- 内部槽 -------------------------------------------------------

    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                break
            try:
                peer = socket.peerAddress()
                peer_text = (
                    f"{peer.toString()}:{int(socket.peerPort())}"
                    if peer is not None
                    else "未知地址"
                )
                if len(self._clients) >= self._max_clients:
                    self.error.emit(
                        f"已达到最大客户端数（{self._max_clients}），"
                        f"已拒绝 {peer_text} 的连接。请先断开其他客户端或调大上限。"
                    )
                    socket.abort()
                    socket.deleteLater()
                    continue
                client_id = str(self._next_client_id)
                self._next_client_id += 1
                client = _Client(client_id, socket)
                self._clients[client_id] = client
                socket.readyRead.connect(
                    lambda cid=client_id: self._on_ready_read(cid)
                )
                socket.disconnected.connect(
                    lambda cid=client_id: self._on_client_disconnected(cid)
                )
                socket.errorOccurred.connect(
                    lambda err, cid=client_id: self._on_socket_error(cid, err)
                )
                self.client_connected.emit(client_id)
            except Exception:
                get_logger().exception("处理新的客户端连接失败")
                try:
                    socket.abort()
                    socket.deleteLater()
                except Exception:
                    pass

    def _on_ready_read(self, client_id: str) -> None:
        client = self._clients.get(client_id)
        if client is None:
            return
        self._drain_client(client)

    def _on_client_disconnected(self, client_id: str) -> None:
        client = self._clients.get(client_id)
        if client is None:
            return
        # 断开前可能还有未读数据：先读完并通知，再把来源从列表中移除。
        self._drain_client(client)
        self._clients.pop(client_id, None)
        try:
            client.socket.deleteLater()
        except Exception:
            get_logger().exception("释放客户端套接字失败：%s", client_id)
        self.client_disconnected.emit(client_id)

    def _drain_client(self, client: _Client) -> None:
        try:
            data = bytes(client.socket.readAll())
        except Exception:
            get_logger().exception("读取客户端数据失败：%s", client.client_id)
            return
        if not data:
            return
        client.rx_bytes += len(data)
        self.data_received.emit(client.client_id, data)

    def _on_socket_error(self, client_id: str, socket_error) -> None:
        try:
            if socket_error == _SOCKET_ERROR.RemoteHostClosedError:
                return
            client = self._clients.get(client_id)
            if client is None:
                return
            self.error.emit(
                f"客户端 {client.address}:{client.port} 连接出错："
                f"{client.socket.errorString()}。请检查客户端状态。"
            )
        except Exception:
            get_logger().exception("处理客户端套接字错误失败")

    def _on_accept_error(self, socket_error) -> None:
        try:
            self.error.emit(
                f"接受客户端连接失败：{self._server.errorString()}。"
                "请检查监听地址、端口与防火墙设置。"
            )
        except Exception:
            get_logger().exception("处理服务器接受连接错误失败")


def _valid_port(port: int, *, allow_zero: bool) -> bool:
    try:
        value = int(port)
    except (TypeError, ValueError):
        return False
    lower = 0 if allow_zero else 1
    return lower <= value <= 65535


def _parse_listen_address(host: str) -> QHostAddress | None:
    text = (host or "").strip()
    if not text or text in ("0.0.0.0", "*"):
        return QHostAddress(QHostAddress.SpecialAddress.AnyIPv4)
    address = QHostAddress(text)
    return None if address.isNull() else address

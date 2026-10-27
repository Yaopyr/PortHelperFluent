"""TCP/UDP 网络客户端控制器：在 Qt 事件循环内驱动 QTcpSocket / QUdpSocket。

所有连接、收发与状态变化都通过信号交给界面层；本模块不导入任何 UI 代码，
也不创建线程（规格 §6：网络使用 Qt 网络类，在事件循环内运行）。
"""

from __future__ import annotations

import weakref

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtNetwork import (
    QAbstractSocket,
    QHostAddress,
    QHostInfo,
    QTcpSocket,
    QUdpSocket,
)

from app.core.logging_setup import get_logger

MAX_DATAGRAM = 65535

_SOCKET_STATE = QAbstractSocket.SocketState
_SOCKET_ERROR = QAbstractSocket.SocketError
_BIND_FLAG = QAbstractSocket.BindFlag


class NetClientController(QObject):
    """封装一个 TCP 客户端套接字与一个 UDP 套接字；两者互不影响。"""

    tcp_state_changed = pyqtSignal(str)  # disconnected / connecting / connected
    udp_state_changed = pyqtSignal(str)  # closed / bound
    tcp_data = pyqtSignal(bytes)
    udp_data = pyqtSignal(bytes)
    error = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tcp_state = "disconnected"
        self._udp_state = "closed"
        self._remote = ("", 0)
        # UDP 域名异步解析：缓存最近解析结果，避免每次发送都阻塞 GUI 线程。
        self._address_cache: dict[str, QHostAddress] = {}
        self._pending_udp: tuple[bytes, str, int] | None = None
        self._lookup_id = 0

        self._tcp_socket = QTcpSocket(self)
        self._tcp_socket.connected.connect(self._on_tcp_connected)
        self._tcp_socket.disconnected.connect(self._on_tcp_disconnected)
        self._tcp_socket.readyRead.connect(self._on_tcp_ready_read)
        self._tcp_socket.errorOccurred.connect(self._on_tcp_error)

        self._udp_socket = QUdpSocket(self)
        self._udp_socket.readyRead.connect(self._on_udp_ready_read)
        self._udp_socket.errorOccurred.connect(self._on_udp_error)

    # ---- TCP ----------------------------------------------------------

    def connect_tcp(self, host: str, port: int, local_port: int = 0) -> None:
        """连接 TCP 服务器；local_port 为 0 时由系统选择本地端口。"""
        host = (host or "").strip()
        if not host:
            self.error.emit("目标地址不能为空，请输入 IP 地址或域名。")
            return
        if not _valid_port(port, allow_zero=False):
            self.error.emit("目标端口无效，请输入 1–65535 之间的端口号。")
            return
        if not _valid_port(local_port, allow_zero=True):
            self.error.emit("本地端口无效，请输入 0–65535 之间的端口号（0 表示自动分配）。")
            return

        self._abort_tcp()
        if local_port:
            if not self._tcp_socket.bind(
                QHostAddress.SpecialAddress.AnyIPv4,
                int(local_port),
                _BIND_FLAG.DefaultForPlatform,
            ):
                self._set_tcp_state("disconnected")
                self.error.emit(
                    f"绑定本地端口 {int(local_port)} 失败：{self._tcp_socket.errorString()}。"
                    "该端口可能已被其他程序占用，请更换本地端口或选择自动分配。"
                )
                return

        self._remote = (host, int(port))
        self._set_tcp_state("connecting")
        self._tcp_socket.connectToHost(host, int(port))

    def disconnect_tcp(self) -> None:
        """断开 TCP；连接中直接取消，已连接时优雅关闭。"""
        state = self._tcp_socket.state()
        if state == _SOCKET_STATE.UnconnectedState:
            self._set_tcp_state("disconnected")
            return
        if state in (_SOCKET_STATE.HostLookupState, _SOCKET_STATE.ConnectingState):
            self._tcp_socket.abort()
            self._set_tcp_state("disconnected")
            return
        self._tcp_socket.disconnectFromHost()
        # 没有待发送数据时 disconnectFromHost() 会同步关闭，信号不一定再到达。
        if self._tcp_socket.state() == _SOCKET_STATE.UnconnectedState:
            self._set_tcp_state("disconnected")

    def send_tcp(self, data: bytes) -> bool:
        payload = bytes(data)
        if not payload:
            self.error.emit("发送内容为空，请输入要发送的数据。")
            return False
        if self._tcp_socket.state() != _SOCKET_STATE.ConnectedState:
            self.error.emit("TCP 尚未连接，无法发送数据。请先连接目标服务器。")
            return False
        written = self._tcp_socket.write(payload)
        if written < 0:
            self.error.emit(
                f"TCP 发送失败：{self._tcp_socket.errorString()}。请检查连接状态后重试。"
            )
            return False
        if written != len(payload):
            self.error.emit(
                f"TCP 只发送了 {written}/{len(payload)} 字节："
                f"{self._tcp_socket.errorString()}。请重试或检查网络。"
            )
            return False
        return True

    def is_tcp_connected(self) -> bool:
        return self._tcp_socket.state() == _SOCKET_STATE.ConnectedState

    def tcp_local_port(self) -> int:
        return int(self._tcp_socket.localPort())

    def tcp_remote(self) -> tuple[str, int]:
        return self._remote

    # ---- UDP ----------------------------------------------------------

    def bind_udp(self, local_port: int) -> None:
        """绑定 UDP 本地端口；0 表示由系统分配。"""
        if not _valid_port(local_port, allow_zero=True):
            self._set_udp_state("closed")
            self.error.emit("UDP 本地端口无效，请输入 0–65535 之间的端口号（0 表示自动分配）。")
            return
        if self._udp_socket.state() == _SOCKET_STATE.BoundState:
            self._udp_socket.close()
            self._set_udp_state("closed")
        if not self._udp_socket.bind(
            QHostAddress.SpecialAddress.AnyIPv4,
            int(local_port),
            _BIND_FLAG.DontShareAddress,
        ):
            self._set_udp_state("closed")
            port_text = f"{int(local_port)}" if local_port else "（自动分配）"
            self.error.emit(
                f"UDP 端口 {port_text} 绑定失败：{self._udp_socket.errorString()}。"
                "端口可能已被其他程序占用，请更换端口后重试。"
            )
            return
        self._set_udp_state("bound")

    def close_udp(self) -> None:
        self._cancel_host_lookup()
        self._pending_udp = None
        if self._udp_socket.state() != _SOCKET_STATE.UnconnectedState:
            self._udp_socket.close()
        self._set_udp_state("closed")

    def send_udp(self, data: bytes, host: str, port: int) -> bool:
        """发送 UDP 数据报；IP 直接发送，域名异步解析后由回调补发。

        返回 True 表示“已接受发送请求”。域名首次发送时不会阻塞 GUI 线程：
        解析完成后在事件循环中写套接字，解析失败通过 `error` 信号报告。
        """
        payload = bytes(data)
        if not payload:
            self.error.emit("发送内容为空，请输入要发送的数据。")
            return False
        if self._udp_socket.state() != _SOCKET_STATE.BoundState:
            self.error.emit("UDP 尚未绑定本地端口，无法发送数据。请先绑定。")
            return False
        host = (host or "").strip()
        if not host:
            self.error.emit("目标地址不能为空，请输入 IP 地址或域名。")
            return False
        if not _valid_port(port, allow_zero=False):
            self.error.emit("目标端口无效，请输入 1–65535 之间的端口号。")
            return False
        target_port = int(port)

        direct = QHostAddress(host)
        if not direct.isNull():
            return self._write_udp(payload, direct, target_port)
        cached = self._address_cache.get(host.casefold())
        if cached is not None:
            return self._write_udp(payload, cached, target_port)
        return self._start_host_lookup(payload, host, target_port)

    def _write_udp(self, payload: bytes, address: QHostAddress, port: int) -> bool:
        written = self._udp_socket.writeDatagram(payload, address, int(port))
        if written < 0:
            self.error.emit(
                f"UDP 发送失败：{self._udp_socket.errorString()}。"
                "请检查目标地址、端口与网络连接。"
            )
            return False
        if written != len(payload):
            self.error.emit(
                f"UDP 只发送了 {written}/{len(payload)} 字节："
                f"{self._udp_socket.errorString()}。"
            )
            return False
        return True

    def _start_host_lookup(self, payload: bytes, host: str, port: int) -> bool:
        """发起异步域名解析；只保留最近一次待发送数据，旧查询被取消。"""
        self._cancel_host_lookup()
        self._pending_udp = (payload, host, port)
        controller_ref = weakref.ref(self)

        def _finished(info: QHostInfo) -> None:
            target = controller_ref()
            if target is not None:
                target._on_udp_host_resolved(info)

        try:
            self._lookup_id = _lookup_host_name(host, _finished)
        except Exception:
            get_logger().exception("发起域名解析失败：%s", host)
            self._lookup_id = 0
            self._pending_udp = None
            self.error.emit(
                f"无法解析目标地址“{host}”。请检查地址拼写、网络连接或 DNS 设置。"
            )
            return False
        return True

    def _cancel_host_lookup(self) -> None:
        lookup_id, self._lookup_id = self._lookup_id, 0
        if lookup_id > 0:
            try:
                QHostInfo.abortHostLookup(lookup_id)
            except Exception:
                get_logger().exception("取消域名解析失败：%s", lookup_id)

    def _on_udp_host_resolved(self, info: QHostInfo) -> None:
        """域名解析完成：缓存 IPv4 优先地址并补发待发送数据。"""
        try:
            self._lookup_id = 0
            pending, self._pending_udp = self._pending_udp, None
            if pending is None:
                return
            payload, host, port = pending
            address = _address_from_info(info)
            if address is None:
                self.error.emit(
                    f"无法解析目标地址“{host}”。请检查地址拼写、网络连接或 DNS 设置。"
                )
                return
            self._address_cache[host.casefold()] = address
            if self._udp_socket.state() != _SOCKET_STATE.BoundState:
                # 解析期间用户关闭了 UDP：丢弃这次发送，不再打扰用户。
                get_logger().warning("UDP 已关闭，丢弃解析完成后的待发送数据：%s", host)
                return
            self._write_udp(payload, address, port)
        except Exception:
            get_logger().exception("处理 UDP 域名解析结果失败")

    def is_udp_bound(self) -> bool:
        return self._udp_socket.state() == _SOCKET_STATE.BoundState

    def udp_local_port(self) -> int:
        return int(self._udp_socket.localPort())

    # ---- 内部槽 -------------------------------------------------------

    def _on_tcp_connected(self) -> None:
        self._set_tcp_state("connected")

    def _on_tcp_disconnected(self) -> None:
        self._set_tcp_state("disconnected")

    def _on_tcp_ready_read(self) -> None:
        try:
            data = bytes(self._tcp_socket.readAll())
        except Exception:
            get_logger().exception("读取 TCP 数据失败")
            return
        if data:
            self.tcp_data.emit(data)

    def _on_tcp_error(self, socket_error) -> None:
        try:
            if socket_error == _SOCKET_ERROR.RemoteHostClosedError:
                # 对端正常关闭：disconnected 槽会更新状态，不弹出错误。
                return
            message = _tcp_error_message(socket_error, self._tcp_socket.errorString())
            if self._tcp_socket.state() != _SOCKET_STATE.ConnectedState:
                # 连接阶段或断开后的错误：把逻辑状态拉回未连接，避免界面停在“正在连接”。
                self._set_tcp_state("disconnected")
            self.error.emit(message)
        except Exception:
            get_logger().exception("处理 TCP 错误失败")

    def _on_udp_ready_read(self) -> None:
        while self._udp_socket.hasPendingDatagrams():
            try:
                datagram, _host, _port = self._udp_socket.readDatagram(MAX_DATAGRAM)
            except Exception:
                get_logger().exception("读取 UDP 数据失败")
                return
            if datagram:
                self.udp_data.emit(bytes(datagram))

    def _on_udp_error(self, socket_error) -> None:
        try:
            message = _udp_error_message(socket_error, self._udp_socket.errorString())
            if self._udp_socket.state() != _SOCKET_STATE.BoundState:
                self._set_udp_state("closed")
            self.error.emit(message)
        except Exception:
            get_logger().exception("处理 UDP 错误失败")

    # ---- 状态与清理 ---------------------------------------------------

    def _abort_tcp(self) -> None:
        if self._tcp_socket.state() != _SOCKET_STATE.UnconnectedState:
            self._tcp_socket.abort()

    def _set_tcp_state(self, state: str) -> None:
        if state == self._tcp_state:
            return
        self._tcp_state = state
        self.tcp_state_changed.emit(state)

    def _set_udp_state(self, state: str) -> None:
        if state == self._udp_state:
            return
        self._udp_state = state
        self.udp_state_changed.emit(state)


def _valid_port(port: int, *, allow_zero: bool) -> bool:
    try:
        value = int(port)
    except (TypeError, ValueError):
        return False
    lower = 0 if allow_zero else 1
    return lower <= value <= 65535


def _lookup_host_name(host: str, callback) -> int:
    """发起异步域名解析；独立成函数便于测试替换，不阻塞调用线程。"""
    return int(QHostInfo.lookupHost(host, callback))


def _address_from_info(info: QHostInfo) -> QHostAddress | None:
    """从解析结果中选地址：优先 IPv4，与界面上的常规 IPv4 目标保持一致。"""
    if info.error() != QHostInfo.HostInfoError.NoError:
        return None
    addresses = [address for address in info.addresses() if not address.isNull()]
    if not addresses:
        return None
    for address in addresses:
        text = address.toString()
        if "." in text and ":" not in text:
            return address
    return addresses[0]


def _tcp_error_message(socket_error, error_string: str) -> str:
    detail = f"（{error_string}）" if error_string else ""
    if socket_error == _SOCKET_ERROR.ConnectionRefusedError:
        return (
            f"TCP 连接被拒绝{detail}。目标端口可能未监听，或被防火墙拦截。"
            "请确认对方服务已启动并允许该端口。"
        )
    if socket_error == _SOCKET_ERROR.HostNotFoundError:
        return (
            f"无法解析目标主机{detail}。请检查 IP/域名拼写、网络连接或 DNS 设置。"
        )
    if socket_error in (
        _SOCKET_ERROR.SocketTimeoutError,
        _SOCKET_ERROR.ProxyConnectionTimeoutError,
    ):
        return (
            f"TCP 连接超时{detail}。请检查目标地址是否可达、网络是否通畅，"
            "或对方是否在防火墙中放行该端口。"
        )
    if socket_error == _SOCKET_ERROR.NetworkError:
        return f"网络错误{detail}。请检查网线/Wi-Fi 连接与目标网络状态。"
    if socket_error == _SOCKET_ERROR.AddressInUseError:
        return f"本地地址或端口已被占用{detail}。请更换本地端口后重试。"
    if socket_error == _SOCKET_ERROR.SocketAccessError:
        return f"没有创建套接字的权限{detail}。请检查防火墙或安全软件设置。"
    if socket_error == _SOCKET_ERROR.UnsupportedSocketOperationError:
        return f"当前系统不支持该套接字操作{detail}。"
    return f"TCP 通信错误{detail}。请检查连接状态后重试。"


def _udp_error_message(socket_error, error_string: str) -> str:
    detail = f"（{error_string}）" if error_string else ""
    if socket_error == _SOCKET_ERROR.ConnectionRefusedError:
        return (
            f"UDP 目标端口没有接收程序{detail}。"
            "系统可能收到端口不可达提示，请确认目标地址与端口。"
        )
    if socket_error == _SOCKET_ERROR.DatagramTooLargeError:
        return f"UDP 数据报超过系统允许的最大长度{detail}。请减小单次发送的数据量。"
    if socket_error == _SOCKET_ERROR.HostNotFoundError:
        return f"无法解析目标主机{detail}。请检查地址拼写、网络连接或 DNS 设置。"
    if socket_error == _SOCKET_ERROR.NetworkError:
        return f"网络错误{detail}。请检查网线/Wi-Fi 连接与目标网络状态。"
    if socket_error == _SOCKET_ERROR.SocketAccessError:
        return f"没有创建套接字的权限{detail}。请检查防火墙或安全软件设置。"
    return f"UDP 通信错误{detail}。请检查绑定状态、目标地址与端口后重试。"

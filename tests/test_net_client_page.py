"""Offscreen tests for the Fluent network client page using loopback echo servers."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtNetwork import QHostAddress, QTcpServer, QUdpSocket
from PyQt6.QtWidgets import QApplication

from app.core.settings import Settings
from app.services.paths import resolve_paths
from app.ui.dialogs.text_info_dialog import TextInfoDialog
from app.ui.pages import net_client_page as net_client_page_module
from app.ui.pages.net_client_page import NetClientPage
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


def _make_page(tmp_path, qapp):
    settings = Settings(tmp_path / "config.json")
    paths = resolve_paths(exe_path=tmp_path / "app", appdata=tmp_path / "appdata")
    page = NetClientPage(settings, paths)
    page.show()
    qapp.processEvents()
    return page, settings


def test_tcp_connect_send_receive_counters_and_disconnect(qapp, tmp_path) -> None:
    echo = TcpEchoServer()
    port = echo.start()
    page, _settings = _make_page(tmp_path, qapp)
    try:
        page.host_edit.setText("127.0.0.1")
        page.port_spin.setValue(port)
        page.connect_button.click()
        assert process_events_until(
            page.controller.is_tcp_connected, timeout_ms=5000
        )
        assert page.status_chip.state == "ok"
        assert "已连接" in page.status_chip.text()
        assert page.connect_button.isEnabled() is False
        assert page.disconnect_button.isEnabled() is True
        assert page.has_active_work() is True

        page.send_editor.setPlainText("hello")
        page.tcp_send_button.click()
        # 同一日志面板同时记录 TX/RX；等待回环 RX 计数，而不是只看文本，
        # 否则本地 TX 记录会提前满足文本条件，产生竞态。
        assert process_events_until(
            lambda: page.tcp_log.session().total_bytes("rx") == 5, timeout_ms=5000
        )
        assert process_events_until(
            lambda: "hello" in page.tcp_log.log_view.toPlainText(), timeout_ms=5000
        )
        assert page.tcp_log.session().total_bytes("tx") == 5
        assert "接收 5 B" in page.counter_label.text()
        assert "发送 5 B" in page.counter_label.text()

        page.disconnect_button.click()
        assert process_events_until(
            lambda: not page.controller.is_tcp_connected(), timeout_ms=5000
        )
        assert page.status_chip.state == "idle"
        assert page.has_active_work() is False
    finally:
        page.close()
        echo.stop()


def test_udp_bind_send_receive_and_close(qapp, tmp_path) -> None:
    echo = UdpEchoServer()
    echo_port = echo.start()
    page, _settings = _make_page(tmp_path, qapp)
    try:
        page.mode_combo.setCurrentIndex(1)
        assert page.connect_button.text() == "绑定"
        page.udp_local_spin.setValue(0)
        page.udp_port_spin.setValue(echo_port)
        page.connect_button.click()
        assert page.controller.is_udp_bound()
        assert page.status_chip.state == "ok"
        assert "绑定" in page.status_chip.text()
        assert page.has_active_work() is True

        page.send_editor.setPlainText("udp-hello")
        page.udp_send_button.click()
        assert process_events_until(
            lambda: "udp-hello" in page.udp_log.log_view.toPlainText(),
            timeout_ms=5000,
        )
        assert page.udp_log.session().total_bytes("rx") == len("udp-hello")
        assert page.udp_log.session().total_bytes("tx") == len("udp-hello")
        assert "接收 9 B" in page.counter_label.text()

        page.disconnect_button.click()
        assert not page.controller.is_udp_bound()
        assert page.status_chip.state == "idle"
        assert page.has_active_work() is False
    finally:
        page.close()
        echo.stop()


def test_empty_host_shows_error_and_stays_idle(qapp, tmp_path, monkeypatch) -> None:
    errors: list[tuple] = []
    monkeypatch.setattr(
        net_client_page_module, "show_error", lambda *args: errors.append(args)
    )
    page, _settings = _make_page(tmp_path, qapp)
    try:
        page.host_edit.setText("   ")
        page.connect_button.click()
        assert errors
        assert "目标地址" in str(errors[-1])
        assert page.status_chip.state == "error"
        assert page.has_active_work() is False
        assert page.connect_button.isEnabled() is True
    finally:
        page.close()


def test_invalid_hex_blocks_send_and_valid_hex_is_transmitted(
    qapp, tmp_path, monkeypatch
) -> None:
    errors: list[tuple] = []
    monkeypatch.setattr(
        net_client_page_module, "show_error", lambda *args: errors.append(args)
    )
    echo = TcpEchoServer()
    port = echo.start()
    page, _settings = _make_page(tmp_path, qapp)
    try:
        page.host_edit.setText("127.0.0.1")
        page.port_spin.setValue(port)
        page.connect_button.click()
        assert process_events_until(
            page.controller.is_tcp_connected, timeout_ms=5000
        )

        page.hex_send_check.setChecked(True)
        page.send_editor.setPlainText("0G")
        page.tcp_send_button.click()
        assert errors
        assert "HEX" in str(errors[-1])
        assert page.tcp_log.session().total_bytes("tx") == 0

        page.send_editor.setPlainText("0x01 02")
        page.tcp_send_button.click()
        assert process_events_until(
            lambda: page.tcp_log.session().total_bytes("tx") == 2, timeout_ms=5000
        )
        assert process_events_until(
            lambda: page.tcp_log.session().total_bytes("rx") == 2, timeout_ms=5000
        )
    finally:
        page.close()
        echo.stop()


def test_settings_round_trip(qapp, tmp_path) -> None:
    page, settings = _make_page(tmp_path, qapp)
    try:
        page.host_edit.setText("example.local")
        page.port_spin.setValue(9000)
        page.udp_port_spin.setValue(9001)
        page.encoding_combo.setCurrentIndex(1)  # GBK
        page.hex_send_check.setChecked(True)
        page.close()

        reloaded = Settings(settings._path)
        reloaded.load()
        assert reloaded.get("net.host") == "example.local"
        assert reloaded.get("net.port") == 9000
        assert reloaded.get("net.udp_port") == 9001
        assert reloaded.get("net.encoding") == "gbk"
        assert reloaded.get("net.hex_send") is True
    finally:
        page.close()


def test_file_send_uses_chunked_timer_and_counts_bytes(
    qapp, tmp_path, monkeypatch
) -> None:
    echo = TcpEchoServer()
    port = echo.start()
    payload = bytes(range(256)) * 40  # 10240 B，会分成两个 8192 B 块
    file_path = tmp_path / "payload.bin"
    file_path.write_bytes(payload)
    monkeypatch.setattr(
        net_client_page_module.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(file_path), ""),
    )
    page, _settings = _make_page(tmp_path, qapp)
    try:
        page.host_edit.setText("127.0.0.1")
        page.port_spin.setValue(port)
        page.connect_button.click()
        assert process_events_until(
            page.controller.is_tcp_connected, timeout_ms=5000
        )

        page.send_file_button.click()
        assert process_events_until(
            lambda: "完成" in page.file_progress_label.text(), timeout_ms=8000
        ), page.file_progress_label.text()
        assert page.tcp_log.session().total_bytes("tx") == len(payload)
        assert process_events_until(
            lambda: page.tcp_log.session().total_bytes("rx") == len(payload),
            timeout_ms=8000,
        )
        assert page.cancel_file_button.isEnabled() is False
    finally:
        page.close()
        echo.stop()


def test_text_info_dialog_is_read_only_and_copies_all(qapp) -> None:
    dialog = TextInfoDialog("Ping 结果", "Ping 输出")
    try:
        assert dialog.windowTitle() == "Ping 结果"
        assert dialog.text_edit.isReadOnly() is True
        assert dialog.text() == "Ping 输出"
        dialog.copy_button.click()
        assert QApplication.clipboard().text() == "Ping 输出"
    finally:
        dialog.close()


def test_error_feedback_is_non_blocking(qapp, tmp_path) -> None:
    page, _settings = _make_page(tmp_path, qapp)
    try:
        page._on_error("测试错误")
        assert page.status_chip.state == "error"
        assert process_events_until(
            lambda: "测试错误" in page.tcp_log.log_view.toPlainText(), timeout_ms=2000
        )
        # 调用立即返回即说明没有进入模态循环。
        assert True
    finally:
        page.close()


def test_stop_workers_closes_active_tcp_connection(qapp, tmp_path) -> None:
    echo = TcpEchoServer()
    port = echo.start()
    page, _settings = _make_page(tmp_path, qapp)
    try:
        page.host_edit.setText("127.0.0.1")
        page.port_spin.setValue(port)
        page.connect_button.click()
        assert process_events_until(
            page.controller.is_tcp_connected, timeout_ms=5000
        )
        assert page.has_active_work() is True

        page.stop_workers()
        assert page.has_active_work() is False
        assert page.controller.is_udp_bound() is False
    finally:
        page.close()
        echo.stop()

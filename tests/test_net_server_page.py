"""Offscreen tests for the Fluent network server page with loopback clients."""

from __future__ import annotations

from PyQt6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from app.core.settings import Settings
from app.services.paths import resolve_paths
from app.ui.pages import net_server_page as net_server_page_module
from app.ui.pages.net_server_page import NetServerPage
from app.ui.qt_utils import process_events_until


def _make_page(tmp_path, qapp):
    settings = Settings(tmp_path / "config.json")
    paths = resolve_paths(exe_path=tmp_path / "app", appdata=tmp_path / "appdata")
    page = NetServerPage(settings, paths)
    page.show()
    qapp.processEvents()
    return page, settings


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


def _start_page_server(page):
    page.port_spin.setValue(0)
    page.start_button.click()
    assert page.controller.is_listening()
    return page.controller.server_port()


def test_start_list_clients_and_source_prefixes(qapp, tmp_path) -> None:
    page, _settings = _make_page(tmp_path, qapp)
    clients = []
    try:
        port = _start_page_server(page)
        assert port > 0
        assert page.status_chip.state == "ok"
        assert "监听中" in page.status_chip.text()
        assert page.port_spin.value() == port
        assert page.start_button.isEnabled() is False
        assert page.stop_button.isEnabled() is True
        assert page.has_active_work() is True

        clients = [_connect_client(port)]
        assert process_events_until(
            lambda: page.client_table.rowCount() == 1, timeout_ms=5000
        )
        label = page.client_table.item(0, 0).text()
        assert "127.0.0.1" in label
        assert page.client_table.item(0, 1).text()
        assert page.client_count_label.text() == "共 1 个客户端"

        clients[0].write(b"ping")
        assert process_events_until(
            lambda: "ping" in page.log_pane.log_view.toPlainText(), timeout_ms=5000
        )
        log_text = page.log_pane.log_view.toPlainText()
        assert "127.0.0.1" in log_text
        assert page.log_pane.session().total_bytes("rx") == 4
        assert "接收 4 B" in page.counter_label.text()
        assert page.client_table.item(0, 2).text() == "4 B"
        assert page.send_button.isEnabled() is True
    finally:
        page.close()
        _close_clients(clients)


def test_send_to_selected_broadcast_and_disconnect(qapp, tmp_path) -> None:
    page, _settings = _make_page(tmp_path, qapp)
    clients = []
    try:
        port = _start_page_server(page)
        clients = [_connect_client(port), _connect_client(port)]
        assert process_events_until(
            lambda: page.client_table.rowCount() == 2, timeout_ms=5000
        )
        assert page.client_table.currentRow() >= 0

        page.send_editor.setPlainText("selected")
        page.send_button.click()
        assert process_events_until(
            lambda: any(socket.bytesAvailable() >= 8 for socket in clients),
            timeout_ms=5000,
        )
        received = [bytes(socket.readAll()) for socket in clients]
        assert received.count(b"selected") == 1
        assert page.log_pane.session().total_bytes("tx") == 8
        assert "发送 8 B" in page.counter_label.text()

        page.broadcast_check.setChecked(True)
        page.send_editor.setPlainText("all")
        page.send_button.click()
        assert process_events_until(
            lambda: all(socket.bytesAvailable() >= 3 for socket in clients),
            timeout_ms=5000,
        )
        assert bytes(clients[0].readAll()) == b"all"
        assert bytes(clients[1].readAll()) == b"all"
        assert page.log_pane.session().total_bytes("tx") == 14  # 8 + 3 + 3

        page.client_table.selectRow(0)
        assert page.disconnect_client_button.isEnabled() is True
        page.disconnect_client_button.click()
        assert process_events_until(
            lambda: page.client_table.rowCount() == 1, timeout_ms=5000
        )
        assert page.client_count_label.text() == "共 1 个客户端"
    finally:
        page.close()
        _close_clients(clients)


def test_stop_clears_clients_and_returns_idle(qapp, tmp_path) -> None:
    page, _settings = _make_page(tmp_path, qapp)
    clients = []
    port = None
    try:
        port = _start_page_server(page)
        clients = [_connect_client(port)]
        assert process_events_until(
            lambda: page.client_table.rowCount() == 1, timeout_ms=5000
        )
        page.stop_button.click()
        assert not page.controller.is_listening()
        assert page.client_table.rowCount() == 0
        assert page.status_chip.state == "idle"
        assert page.start_button.isEnabled() is True
        assert page.stop_button.isEnabled() is False
        assert page.has_active_work() is False
    finally:
        page.close()
        _close_clients(clients)


def test_start_on_used_port_shows_error_without_modal(
    qapp, tmp_path, monkeypatch
) -> None:
    errors: list[tuple] = []
    monkeypatch.setattr(
        net_server_page_module, "show_error", lambda *args: errors.append(args)
    )
    blocker = QTcpServer()
    assert blocker.listen(QHostAddress("0.0.0.0"), 0)
    occupied_port = int(blocker.serverPort())
    page, _settings = _make_page(tmp_path, qapp)
    try:
        page.port_spin.setValue(occupied_port)
        page.start_button.click()
        assert errors
        assert "启动" in str(errors[-1]) or "占用" in str(errors[-1])
        assert not page.controller.is_listening()
        assert page.status_chip.state == "error"
        assert page.start_button.isEnabled() is True
    finally:
        page.close()
        blocker.close()


def test_settings_round_trip(qapp, tmp_path) -> None:
    page, settings = _make_page(tmp_path, qapp)
    try:
        page.port_spin.setValue(9999)
        page.max_clients_spin.setValue(8)
        page.encoding_combo.setCurrentIndex(1)  # GBK
        page.hex_send_check.setChecked(True)
        page.close()

        reloaded = Settings(settings._path)
        reloaded.load()
        assert reloaded.get("server.port") == 9999
        assert reloaded.get("server.max_clients") == 8
        assert reloaded.get("server.encoding") == "gbk"
        assert reloaded.get("server.hex_send") is True
    finally:
        page.close()


def test_file_send_to_selected_client(qapp, tmp_path, monkeypatch) -> None:
    payload = bytes(range(256)) * 20  # 5120 B
    file_path = tmp_path / "server-payload.bin"
    file_path.write_bytes(payload)
    monkeypatch.setattr(
        net_server_page_module.QFileDialog,
        "getOpenFileName",
        lambda *args, **kwargs: (str(file_path), ""),
    )
    page, _settings = _make_page(tmp_path, qapp)
    clients = []
    try:
        port = _start_page_server(page)
        clients = [_connect_client(port)]
        assert process_events_until(
            lambda: page.client_table.rowCount() == 1, timeout_ms=5000
        )
        page.send_file_button.click()
        assert process_events_until(
            lambda: "完成" in page.file_progress_label.text(), timeout_ms=8000
        ), page.file_progress_label.text()
        assert page.log_pane.session().total_bytes("tx") == len(payload)
        assert process_events_until(
            lambda: clients[0].bytesAvailable() >= len(payload), timeout_ms=8000
        )
        assert bytes(clients[0].readAll()) == payload
    finally:
        page.close()
        _close_clients(clients)


def test_hex_validation_blocks_bad_server_input(qapp, tmp_path, monkeypatch) -> None:
    errors: list[tuple] = []
    monkeypatch.setattr(
        net_server_page_module, "show_error", lambda *args: errors.append(args)
    )
    page, _settings = _make_page(tmp_path, qapp)
    clients = []
    try:
        port = _start_page_server(page)
        clients = [_connect_client(port)]
        assert process_events_until(
            lambda: page.client_table.rowCount() == 1, timeout_ms=5000
        )
        page.hex_send_check.setChecked(True)
        page.send_editor.setPlainText("0G")
        page.send_button.click()
        assert errors
        assert "HEX" in str(errors[-1])
        assert page.log_pane.session().total_bytes("tx") == 0
    finally:
        page.close()
        _close_clients(clients)

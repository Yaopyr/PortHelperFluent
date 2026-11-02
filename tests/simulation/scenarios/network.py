"""网络客户端与服务器场景：127.0.0.1 回环 + Qt 真实套接字。"""

from __future__ import annotations

import socket

from PyQt6.QtCore import QTimer
from PyQt6.QtNetwork import QHostAddress, QHostInfo
from PyQt6.QtWidgets import QApplication

from app.io import net_client as net_client_module
from app.ui.dialogs.session_dialog import SessionDialog
from tests.simulation.harness.netloop import TcpEchoServer, TcpTestClient, UdpEchoServer


def _start_echo(sim) -> tuple[TcpEchoServer, int]:
    echo = TcpEchoServer()
    port = echo.start()
    sim._echo_servers = getattr(sim, "_echo_servers", [])
    sim._echo_servers.append(echo)
    return echo, port


def net_01_tcp_roundtrip(sim) -> None:
    echo, port = _start_echo(sim)
    page = sim.switch("net-client")
    page.host_edit.setText("127.0.0.1")
    page.port_spin.setValue(port)
    page.connect_button.click()
    sim.check_true("TCP 连接成功", sim.wait(page.controller.is_tcp_connected, 5000), page.status_chip.text())
    sim.check("连接后状态", page.status_chip.state, "ok")
    page.send_editor.setPlainText("hello-tcp")
    page.tcp_send_button.click()
    sim.check_true(
        "TCP 回环接收",
        sim.wait(lambda: page.tcp_log.session().total_bytes("rx") == len("hello-tcp"), 5000),
        page.counter_label.text(),
    )
    sim.check("TCP 发送计数", page.tcp_log.session().total_bytes("tx"), len("hello-tcp"))
    page.disconnect_button.click()
    sim.check_true("TCP 断开", sim.wait(lambda: not page.controller.is_tcp_connected(), 5000), page.status_chip.text())
    sim.check_true("TCP 断开无活动", not page.has_active_work(), "仍有活动连接")
    echo.stop()
    sim.screenshot("net-tcp")


def net_02_tcp_errors_and_invalid_hex(sim) -> None:
    page = sim.switch("net-client")
    page.host_edit.setText("   ")
    page.connect_button.click()
    sim.check_true("空地址报错", sim.wait(lambda: page.status_chip.state == "error", 3000), page.status_chip.text())
    sim.check_true("空地址没有连接", not page.has_active_work(), "错误后仍有活动")

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    page.host_edit.setText("127.0.0.1")
    page.port_spin.setValue(free_port)
    page.connect_button.click()
    sim.check_true("连接未监听端口报错", sim.wait(lambda: page.status_chip.state == "error", 5000), page.status_chip.text())

    echo, port = _start_echo(sim)
    page.port_spin.setValue(port)
    page.connect_button.click()
    sim.check_true("重新连接成功", sim.wait(page.controller.is_tcp_connected, 5000), page.status_chip.text())
    page.hex_send_check.setChecked(True)
    page.send_editor.setPlainText("0G")
    page.tcp_send_button.click()
    sim.wait(lambda: False, 150)
    sim.check("非法 HEX 不发送", page.tcp_log.session().total_bytes("tx"), 0)
    page.send_editor.setPlainText("0x01 02")
    page.tcp_send_button.click()
    sim.check_true("合法 HEX 发送", sim.wait(lambda: page.tcp_log.session().total_bytes("tx") == 2, 5000), page.tcp_log.log_view.toPlainText())
    page.disconnect_button.click()
    sim.wait(lambda: not page.controller.is_tcp_connected(), 5000)
    echo.stop()
    sim.screenshot("net-errors")


class _FakeHostInfo:
    def __init__(self, lookup_id: int, address: str = "127.0.0.1") -> None:
        self._lookup_id = lookup_id
        self._address = address

    def lookupId(self) -> int:
        return self._lookup_id

    def error(self):
        return QHostInfo.HostInfoError.NoError

    def addresses(self):
        return [QHostAddress(self._address)]


def net_03_udp_and_domain(sim) -> None:
    echo = UdpEchoServer()
    port = echo.start()
    page = sim.switch("net-client")
    page.mode_combo.setCurrentIndex(1)
    page.udp_local_spin.setValue(0)
    page.udp_port_spin.setValue(port)
    page.host_edit.setText("127.0.0.1")
    page.connect_button.click()
    sim.check_true("UDP 绑定", page.controller.is_udp_bound(), page.status_chip.text())
    page.send_editor.setPlainText("udp-hello")
    page.udp_send_button.click()
    sim.check_true(
        "UDP 回环接收",
        sim.wait(lambda: page.udp_log.session().total_bytes("rx") == len("udp-hello"), 5000),
        page.counter_label.text(),
    )
    page.disconnect_button.click()
    sim.check_true("UDP 解绑", sim.wait(lambda: not page.controller.is_udp_bound(), 3000), page.status_chip.text())
    echo.stop()
    page.udp_log.clear()

    def fake_lookup(host, callback):
        QTimer.singleShot(0, lambda: callback(_FakeHostInfo(4242)))
        return 4242

    sim.monkeypatch.setattr(net_client_module, "_lookup_host_name", fake_lookup)
    echo2 = UdpEchoServer()
    port2 = echo2.start()
    page.mode_combo.setCurrentIndex(1)
    page.host_edit.setText("sim.local")
    page.udp_port_spin.setValue(port2)
    page.connect_button.click()
    sim.check_true("UDP 域名模式绑定", page.controller.is_udp_bound(), page.status_chip.text())
    page.send_editor.setPlainText("domain-hello")
    page.udp_send_button.click()
    sim.check_true(
        "UDP 域名异步解析后发送",
        sim.wait(
            lambda: page.udp_log.session().total_bytes("rx") >= len("domain-hello"),
            5000,
        ),
        page.counter_label.text(),
    )
    page.disconnect_button.click()
    echo2.stop()
    sim.screenshot("net-udp")


def net_04_ping_dns_adapter(sim) -> None:
    page = sim.switch("net-client")
    page.host_edit.setText("127.0.0.1")
    page.ping_button.click()
    sim.check_true("Ping 结果窗口", sim.wait(lambda: bool(page._info_dialogs), 3000), "没有信息窗口")
    dialog = page._info_dialogs[-1]
    sim.check_in("Ping 注入结果", "模拟 ping 127.0.0.1 成功", dialog.text())
    sim.screenshot("net-ping")
    page._forget_dialog(dialog)
    dialog.close()

    page.domain_button.click()
    sim.check_true("域名解析窗口", sim.wait(lambda: bool(page._info_dialogs), 3000), "没有域名结果")
    domain_dialog = page._info_dialogs[-1]
    sim.check_in("域名解析注入结果", "127.0.0.1", domain_dialog.text())
    page._forget_dialog(domain_dialog)
    domain_dialog.close()

    page.adapter_button.click()
    sim.check_true("网卡信息窗口", sim.wait(lambda: bool(page._info_dialogs), 3000), "没有网卡结果")
    adapter_dialog = page._info_dialogs[-1]
    sim.check_in("网卡信息注入结果", "模拟网卡", adapter_dialog.text())
    adapter_dialog.copy_button.click()
    sim.check_in("网卡信息复制", "模拟网卡", QApplication.clipboard().text())
    page._forget_dialog(adapter_dialog)
    adapter_dialog.close()
    sim.screenshot("net-info")


def net_05_file_repeat_session(sim) -> None:
    echo, port = _start_echo(sim)
    page = sim.switch("net-client")
    page.host_edit.setText("127.0.0.1")
    page.port_spin.setValue(port)
    page.connect_button.click()
    sim.check_true("TCP 连接", sim.wait(page.controller.is_tcp_connected, 5000), page.status_chip.text())
    payload = bytes(range(256)) * 40
    source = sim.tmp_path / "net-payload.bin"
    source.write_bytes(payload)
    sim.modal.open_files.append(str(source))
    page.send_file_button.click()
    sim.check_true(
        "网络文件发送完成",
        sim.wait(lambda: "完成" in page.file_progress_label.text(), 8000),
        page.file_progress_label.text(),
    )
    sim.check("网络文件 TX 计数", page.tcp_log.session().total_bytes("tx"), len(payload))
    sim.check_true(
        "网络文件回环接收",
        sim.wait(lambda: page.tcp_log.session().total_bytes("rx") == len(payload), 8000),
        page.counter_label.text(),
    )
    sim.artifact(source, "net-file.bin")

    page.send_editor.setPlainText("repeat")
    page.interval_spin.setValue(20)
    page.repeat_check.setChecked(True)
    sim.check_true(
        "网络连续发送",
        sim.wait(lambda: page.tcp_log.session().total_bytes("tx") >= len(payload) + 12, 4000),
        page.counter_label.text(),
    )
    page.repeat_check.setChecked(False)

    csv_path = sim.tmp_path / "net-session.csv"
    txt_path = sim.tmp_path / "net-session.txt"

    def handler(dialog) -> int:
        sim.modal.save_files.extend([str(csv_path), str(txt_path)])
        dialog.export_csv_button.click()
        dialog.export_txt_button.click()
        dialog.close()
        return 0

    sim.modal.session_handler = handler
    page.session_button.click()
    sim.check_true("网络会话 CSV", csv_path.is_file(), str(csv_path))
    sim.check_true("网络会话 TXT", txt_path.is_file(), str(txt_path))
    sim.artifact(csv_path, "net-session.csv")
    sim.artifact(txt_path, "net-session.txt")
    page.disconnect_button.click()
    echo.stop()
    sim.screenshot("net-file")


def _client(port: int) -> TcpTestClient:
    client = TcpTestClient()
    assert client.connect_to(port)
    return client


def srv_01_start_and_stop(sim) -> None:
    page = sim.switch("net-server")
    page.port_spin.setValue(0)
    page.start_button.click()
    sim.check_true("服务器监听", page.controller.is_listening(), page.status_chip.text())
    port = page.controller.server_port()
    sim.check_true("服务器实际端口回填", port > 0, str(port))
    sim.check_true("端口控件回填", sim.wait(lambda: page.port_spin.value() == port, 3000), str(page.port_spin.value()))
    page.stop_button.click()
    sim.check_true("服务器停止", sim.wait(lambda: not page.controller.is_listening(), 3000), page.status_chip.text())
    sim.check("停止后客户端清零", page.client_table.rowCount(), 0)
    sim.screenshot("srv-start-stop")


def srv_02_clients_send_broadcast_disconnect(sim) -> None:
    page = sim.switch("net-server")
    page.port_spin.setValue(0)
    page.start_button.click()
    port = page.controller.server_port()
    clients = [_client(port), _client(port)]
    try:
        sim.check_true("两个客户端入表", sim.wait(lambda: page.client_table.rowCount() == 2, 5000), str(page.client_table.rowCount()))
        page.send_editor.setPlainText("selected")
        page.send_button.click()
        sim.check_true("单发只到选中客户端", clients[0].wait_for(b"selected", 5000) or clients[1].wait_for(b"selected", 5000), "没有客户端收到")
        page.broadcast_check.setChecked(True)
        page.send_editor.setPlainText("all")
        page.send_button.click()
        sim.check_true("广播到两个客户端", clients[0].wait_for(b"all", 5000) and clients[1].wait_for(b"all", 5000), "广播未全部到达")
        page.client_table.selectRow(0)
        page.disconnect_client_button.click()
        sim.check_true("断开选中客户端", sim.wait(lambda: page.client_table.rowCount() == 1, 5000), str(page.client_table.rowCount()))
        sim.check_in("客户端计数", "共 1 个客户端", page.client_count_label.text())
        sim.screenshot("srv-clients")
    finally:
        for client in clients:
            client.close()


def srv_03_file_and_session(sim) -> None:
    page = sim.switch("net-server")
    page.port_spin.setValue(0)
    page.start_button.click()
    port = page.controller.server_port()
    client = _client(port)
    try:
        sim.check_true("客户端入表", sim.wait(lambda: page.client_table.rowCount() == 1, 5000), str(page.client_table.rowCount()))
        payload = bytes(range(256)) * 20
        source = sim.tmp_path / "server-payload.bin"
        source.write_bytes(payload)
        sim.modal.open_files.append(str(source))
        page.send_file_button.click()
        sim.check_true(
            "服务器文件发送完成",
            sim.wait(lambda: "完成" in page.file_progress_label.text(), 8000),
            page.file_progress_label.text(),
        )
        sim.check("服务器文件写入字节", page.log_pane.session().total_bytes("tx"), len(payload))
        sim.check_true("客户端收到服务器文件", client.wait_for(payload, 8000), f"received={len(client.received)}")
        sim.artifact(source, "server-file.bin")

        csv_path = sim.tmp_path / "server-session.csv"
        txt_path = sim.tmp_path / "server-session.txt"

        def handler(dialog) -> int:
            sim.modal.save_files.extend([str(csv_path), str(txt_path)])
            dialog.export_csv_button.click()
            dialog.export_txt_button.click()
            dialog.close()
            return 0

        sim.modal.session_handler = handler
        page.session_button.click()
        sim.check_true("服务器会话 CSV", csv_path.is_file(), str(csv_path))
        sim.check_true("服务器会话 TXT", txt_path.is_file(), str(txt_path))
        sim.artifact(csv_path, "server-session.csv")
        sim.artifact(txt_path, "server-session.txt")
        sim.screenshot("srv-file")
    finally:
        client.close()


def srv_04_errors_and_max_clients(sim) -> None:
    page = sim.switch("net-server")
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    page.port_spin.setValue(port)
    page.start_button.click()
    sim.check_true("端口占用报错", sim.wait(lambda: page.status_chip.state == "error", 4000), page.status_chip.text())
    sim.check_true("端口占用未监听", not page.controller.is_listening(), "错误后仍在监听")
    blocker.close()

    page.port_spin.setValue(0)
    page.max_clients_spin.setValue(1)
    page.start_button.click()
    port2 = page.controller.server_port()
    client1 = _client(port2)
    client2 = TcpTestClient()
    client2.connect_to(port2, timeout_ms=2000)
    try:
        sim.wait(lambda: page.client_table.rowCount() >= 1, 5000)
        sim.check_true("最大客户端限制", page.client_table.rowCount() <= 1, str(page.client_table.rowCount()))
        page.send_editor.setPlainText("")
        page.send_button.click()
        sim.wait(lambda: False, 100)
        sim.check("空发送不增加 TX", page.log_pane.session().total_bytes("tx"), 0)
        sim.screenshot("srv-limits")
    finally:
        client1.close()
        client2.close()

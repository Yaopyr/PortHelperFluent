"""Serial page tests with an injected worker; no hardware or real port I/O."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtTest import QTest

from app.core.settings import Settings
from app.io.serial_worker import PortInfo, PyserialTransport, SerialWorker
from app.services.paths import resolve_paths
from app.ui.dialogs.multi_send_dialog import MultiSendDialog
from app.ui.pages import serial_page as serial_page_module
from app.ui.pages.serial_page import SerialPage, FILE_CHUNK
from app.ui.qt_utils import process_events_until


class FakeWorker(QObject):
    opened = pyqtSignal()
    closed = pyqtSignal()
    data_received = pyqtSignal(bytes)
    data_sent = pyqtSignal(bytes)
    error = pyqtSignal(str)
    modem_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.open_flag = False
        self.config = None
        self.sent: list[bytes] = []
        self.commands: list[tuple[str, object]] = []
        self.closed_count = 0

    def open_port(self, config):
        self.config = config
        self.open_flag = True
        self.opened.emit()
        return True

    def close_port(self):
        self.open_flag = False
        self.closed_count += 1
        self.closed.emit()

    def is_open(self):
        return self.open_flag

    def send(self, data):
        if not self.open_flag:
            self.error.emit("串口未打开")
            return False
        self.sent.append(bytes(data))
        self.data_sent.emit(bytes(data))
        return True

    def set_dtr(self, value):
        self.commands.append(("dtr", value))
        return True

    def set_rts(self, value):
        self.commands.append(("rts", value))
        return True

    def send_break(self):
        self.commands.append(("break", None))
        return True

    def wait(self, _timeout=0):
        return True


def _make_page(tmp_path, monkeypatch, ports=None):
    monkeypatch.setattr(
        serial_page_module,
        "list_ports",
        lambda: ports
        if ports is not None
        else [PortInfo("COM_TEST", "Fake Serial", "USB VID:PID=1234:5678", 0x1234, 0x5678, "SN1")],
    )
    settings = Settings(tmp_path / "config.json")
    paths = resolve_paths(exe_path=tmp_path / "app", appdata=tmp_path / "appdata")
    worker = FakeWorker()
    page = SerialPage(settings, paths, worker_factory=lambda _parent: worker)
    page.show()
    return page, worker, settings


def test_open_receive_send_close(qapp, tmp_path, monkeypatch):
    page, worker, settings = _make_page(tmp_path, monkeypatch)
    try:
        page.port_combo.setCurrentText("COM_TEST")
        page.open_button.click()
        assert worker.open_flag
        assert page.status_chip.state == "ok"
        assert page.open_button.text() == "关闭串口"
        assert worker.config.port == "COM_TEST"
        assert worker.config.baud == 115200
        assert page.has_active_work() is True

        worker.data_received.emit(b"hello")
        assert process_events_until(
            lambda: "hello" in page.rx_log.log_view.toPlainText(), timeout_ms=2000
        )
        assert page.rx_log.session().total_bytes("rx") == 5
        assert "5 B" in page.counter_label.text()

        page.send_editor.setPlainText("abc")
        page.send_button.click()
        assert worker.sent == [b"abc"]
        assert process_events_until(
            lambda: "abc" in page.tx_log.log_view.toPlainText(), timeout_ms=2000
        )
        assert page.tx_log.session().total_bytes("tx") == 3

        page.open_button.click()
        assert not worker.open_flag
        assert page.status_chip.state == "idle"
        assert page.has_active_work() is False
    finally:
        page.close()


def test_hex_send_validation_blocks_bad_input(qapp, tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(serial_page_module, "show_error", lambda *args: errors.append(args))
    page, worker, _settings = _make_page(tmp_path, monkeypatch)
    try:
        page.port_combo.setCurrentText("COM_TEST")
        page.open_button.click()
        page.hex_send_check.setChecked(True)
        page.send_editor.setPlainText("0G")
        page.send_button.click()
        assert worker.sent == []
        assert errors
        assert "HEX" in errors[-1][1] or "hex" in str(errors[-1]).lower()

        page.send_editor.setPlainText("0x01 02")
        page.send_button.click()
        assert worker.sent == [b"\x01\x02"]
    finally:
        page.close()


def test_encoding_and_newline_are_applied(qapp, tmp_path, monkeypatch):
    page, worker, _settings = _make_page(tmp_path, monkeypatch)
    try:
        page.port_combo.setCurrentText("COM_TEST")
        page.open_button.click()
        page.encoding_combo.setCurrentIndex(2)  # UTF-8
        page.newline_combo.setCurrentIndex(3)  # CRLF
        page.send_editor.setPlainText("你好")
        page.send_button.click()
        assert worker.sent == ["你好".encode("utf-8") + b"\r\n"]
    finally:
        page.close()


def test_repeat_send_starts_and_stops(qapp, tmp_path, monkeypatch):
    page, worker, _settings = _make_page(tmp_path, monkeypatch)
    try:
        page.port_combo.setCurrentText("COM_TEST")
        page.open_button.click()
        page.send_editor.setPlainText("x")
        page.interval_spin.setValue(20)
        page.repeat_check.setChecked(True)
        assert page._repeat_timer.isActive()
        assert page._repeat_timer.interval() == 20
        # 定时器间隔 20 ms，但首次触发由事件循环调度决定；固定 qWait(120) 在
        # 高负载下可能一次都没跑到，改为最多等 2 秒直到第 2 次发送发生。
        assert process_events_until(lambda: len(worker.sent) >= 2, timeout_ms=2000)
        assert len(worker.sent) >= 2
        page.repeat_check.setChecked(False)
        assert not page._repeat_timer.isActive()
    finally:
        page.close()


def test_line_control_and_modem_status(qapp, tmp_path, monkeypatch):
    page, worker, _settings = _make_page(tmp_path, monkeypatch)
    try:
        page.port_combo.setCurrentText("COM_TEST")
        page.open_button.click()
        page.dtr_switch.setChecked(True)
        page.rts_switch.setChecked(False)
        page.break_button.click()
        assert ("dtr", True) in worker.commands
        assert ("rts", False) in worker.commands
        assert any(name == "break" for name, _value in worker.commands)

        worker.modem_changed.emit({"cts": True, "dsr": False, "ri": True, "cd": False})
        QTest.qWait(20)
        assert page.line_chips["CTS"].state == "ok"
        assert page.line_chips["DSR"].state == "idle"
    finally:
        page.close()


def test_encoding_change_applies_to_logs(qapp, tmp_path, monkeypatch):
    page, worker, _settings = _make_page(tmp_path, monkeypatch)
    try:
        page.port_combo.setCurrentText("COM_TEST")
        page.open_button.click()
        page.encoding_combo.setCurrentIndex(1)  # GBK
        worker.data_received.emit(b"\xc4\xe3")
        assert process_events_until(
            lambda: "你" in page.rx_log.log_view.toPlainText(), timeout_ms=2000
        )
    finally:
        page.close()


def test_settings_round_trip(qapp, tmp_path, monkeypatch):
    page, _worker, settings = _make_page(tmp_path, monkeypatch)
    try:
        page.port_combo.setCurrentText("COM_TEST")
        page.baud_combo.setCurrentText("9600")
        page.encoding_combo.setCurrentIndex(1)  # GBK
        page.open_button.click()
        page.open_button.click()
        page.close()
        reloaded = Settings(settings._path)
        reloaded.load()
        assert reloaded.get("serial.port") == "COM_TEST"
        assert reloaded.get("serial.baud") == 9600
        assert reloaded.get("serial.encoding") == "gbk"
    finally:
        page.close()


def test_comment_combined_session(qapp, tmp_path, monkeypatch):
    page, worker, _settings = _make_page(tmp_path, monkeypatch)
    try:
        page.port_combo.setCurrentText("COM_TEST")
        page.open_button.click()
        worker.data_received.emit(b"rx")
        page.send_editor.setPlainText("tx")
        page.send_button.click()
        QTest.qWait(20)
        combined = page._combined_session()
        directions = [record.direction for record in combined.records()]
        assert "rx" in directions and "tx" in directions
    finally:
        page.close()


def test_multi_send_dialog_round_trip_and_callback(qapp):
    calls = []

    def send(text, hex_mode, newline):
        calls.append((text, hex_mode, newline))
        return True

    dialog = MultiSendDialog(send)
    try:
        dialog.text_edits[0].setText("AA")
        dialog.hex_checks[0].setChecked(True)
        dialog.newline_checks[0].setChecked(True)
        dialog.send_buttons[0].click()
        assert calls == [("AA", True, True)]

        dialog.text_edits[1].setText("hello")
        data = dialog.to_dict()
        assert data["slots"][0]["text"] == "AA"
        assert data["slots"][0]["hex_mode"] is True

        dialog.load_dict({"slots": [{"text": "restored", "hex_mode": False}]})
        assert dialog.text_edits[0].text() == "restored"
    finally:
        dialog.close()


def test_multi_send_loop_uses_timer(qapp):
    calls = []

    def send(text, hex_mode, newline):
        calls.append(text)
        return True

    dialog = MultiSendDialog(send)
    try:
        dialog.text_edits[0].setText("loop")
        dialog.intervals[0].setValue(20)
        dialog.loop_checks[0].setChecked(True)
        QTest.qWait(90)
        assert len(calls) >= 2
        dialog.stop_all_loops()
        assert not dialog._timers[0].isActive()
    finally:
        dialog.close()


def test_file_send_waits_for_write_ack_and_does_not_queue_whole_file(qapp, tmp_path, monkeypatch):
    class AckControlledWorker(FakeWorker):
        def send(self, data):
            if not self.open_flag:
                return False
            self.sent.append(bytes(data))
            return True

    monkeypatch.setattr(
        serial_page_module,
        "list_ports",
        lambda: [PortInfo("COM_TEST", "Fake", "hw", None, None, None)],
    )
    settings = Settings(tmp_path / "config.json")
    paths = resolve_paths(exe_path=tmp_path / "app", appdata=tmp_path / "appdata")
    worker = AckControlledWorker()
    page = SerialPage(settings, paths, worker_factory=lambda _parent: worker)
    path = tmp_path / "payload.bin"
    path.write_bytes(b"x" * (FILE_CHUNK + 100))
    monkeypatch.setattr(
        serial_page_module.QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(path), "")),
    )
    page.show()
    try:
        page.port_combo.setCurrentText("COM_TEST")
        page.open_button.click()
        page.send_file_button.click()
        assert len(worker.sent) == 1, "只应读取并排队第一块"
        assert page._file_awaiting_ack is True
        assert page.file_progress_label.text().startswith("payload.bin：0 /")

        worker.data_sent.emit(worker.sent[0])
        assert len(worker.sent) == 2
        assert page._file_sent == FILE_CHUNK
        worker.data_sent.emit(worker.sent[1])
        assert page._file_handle is None
        assert "完成" in page.file_progress_label.text()
    finally:
        page.close()


def test_default_worker_uses_pyserial_transport_not_page(qapp, tmp_path, monkeypatch):
    """默认构造路径不得把 page 当成 SerialWorker 的 transport 位置参数。"""
    monkeypatch.setattr(serial_page_module, "list_ports", lambda: [])
    settings = Settings(tmp_path / "config.json")
    paths = resolve_paths(exe_path=tmp_path / "app", appdata=tmp_path / "appdata")
    page = SerialPage(settings, paths)
    page.show()
    try:
        worker = page._worker
        assert isinstance(worker, SerialWorker)
        transport = worker._transport
        assert transport is not page
        assert isinstance(transport, PyserialTransport)
        for name in ("open", "close", "read", "write"):
            assert callable(getattr(transport, name))
        assert worker.parent() is page
    finally:
        page.stop_workers()
        page.close()

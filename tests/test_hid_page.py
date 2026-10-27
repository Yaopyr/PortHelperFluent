"""HID page tests with a fake hidapi backend; no hardware required."""

from __future__ import annotations

import queue

from PyQt6.QtTest import QTest

from app.core.settings import Settings
from app.io.hid_worker import HidDeviceInfo
from app.services.paths import resolve_paths
from app.ui.pages import hid_page as hid_page_module
from app.ui.pages.hid_page import HidPage
from app.ui.qt_utils import process_events_until

DEVICES = (
    HidDeviceInfo(
        path=b"fake-path-keyboard",
        vendor_id=0x1234,
        product_id=0x5678,
        product="Fake Keyboard",
        manufacturer="Acme",
        serial="KB-1",
        usage_page=0x01,
        usage=0x06,
        interface=0,
    ),
    HidDeviceInfo(
        path=b"fake-path-mouse",
        vendor_id=0x046D,
        product_id=0xC52B,
        product="Fake Mouse",
        manufacturer="Logi",
        serial="MS-1",
        usage_page=0x01,
        usage=0x02,
        interface=1,
    ),
)


class FakeHidBackend:
    def __init__(self, devices=DEVICES) -> None:
        self.devices = list(devices)
        self.reads: queue.Queue[bytes] = queue.Queue()
        self.writes: list[bytes] = []
        self.opened_paths: list[bytes] = []
        self.closed = 0
        self.fail_open = False
        self.fail_read = False
        self.fail_write = False

    def enumerate(self):
        return list(self.devices)

    def open(self, path):
        if self.fail_open:
            raise OSError("access denied")
        self.opened_paths.append(bytes(path))
        return object()

    def write(self, handle, data):
        if self.fail_write:
            raise OSError("device unplugged")
        self.writes.append(bytes(data))
        return len(data)

    def read(self, handle, size, timeout_ms):
        if self.fail_read:
            raise OSError("device removed")
        try:
            return self.reads.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return b""

    def close(self, handle):
        self.closed += 1


def _make_page(tmp_path, monkeypatch, *, backend=None):
    fake = backend if backend is not None else FakeHidBackend()
    settings = Settings(tmp_path / "config.json")
    paths = resolve_paths(exe_path=tmp_path / "app", appdata=tmp_path / "appdata")
    errors: list[tuple] = []
    monkeypatch.setattr(hid_page_module, "show_error", lambda *args: errors.append(args))
    page = HidPage(settings, paths, backend=fake)
    page.show()
    return page, fake, settings, errors


def _open_page(page):
    page.open_button.click()
    assert page.status_chip.state == "ok"
    assert page.open_button.text() == "关闭设备"


def _close_page(page):
    page.open_button.click()
    assert process_events_until(
        lambda: page.status_chip.state in ("idle", "error"), timeout_ms=3000
    )
    assert process_events_until(lambda: not page._worker.isRunning(), timeout_ms=3000)


def test_enumerate_select_and_filter(qapp, tmp_path, monkeypatch):
    page, _backend, _settings, _errors = _make_page(tmp_path, monkeypatch)
    try:
        assert page.device_table.rowCount() == 2
        assert page.device_table.currentRow() == 0
        assert page.open_button.isEnabled()
        assert "共 2 个 HID 接口" in page.device_count_label.text()
        assert "Fake Keyboard" in page.device_info_label.text()

        page.search_edit.setText("Mouse")
        assert page.device_table.isRowHidden(0)
        assert not page.device_table.isRowHidden(1)
        assert page.device_table.currentRow() == 1
        assert "Fake Mouse" in page.device_info_label.text()

        page.search_edit.setText("1234:5678")
        assert not page.device_table.isRowHidden(0)
        assert page.device_table.isRowHidden(1)
        assert page.device_table.currentRow() == 0

        page.search_edit.setText("no-such-device")
        assert page.device_table.isRowHidden(0) and page.device_table.isRowHidden(1)
        assert not page.open_button.isEnabled()
    finally:
        page.close()


def test_empty_device_list_is_normal(qapp, tmp_path, monkeypatch):
    page, _backend, _settings, _errors = _make_page(
        tmp_path, monkeypatch, backend=FakeHidBackend(devices=())
    )
    try:
        assert page.device_table.rowCount() == 0
        assert "未检测到标准 HID 设备" in page.device_count_label.text()
        assert not page.open_button.isEnabled()
        assert page.status_chip.state == "idle"
    finally:
        page.close()


def test_default_backend_uses_module_enumeration(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(hid_page_module, "list_devices", lambda: list(DEVICES))
    settings = Settings(tmp_path / "config.json")
    paths = resolve_paths(exe_path=tmp_path / "app", appdata=tmp_path / "appdata")
    page = HidPage(settings, paths)
    page.show()
    try:
        assert page.device_table.rowCount() == 2
        assert page.device_table.currentRow() == 0
    finally:
        page.stop_workers()
        page.close()


def test_open_close_selected_device(qapp, tmp_path, monkeypatch):
    page, backend, _settings, _errors = _make_page(tmp_path, monkeypatch)
    try:
        _open_page(page)
        assert backend.opened_paths == [DEVICES[0].path]
        assert page.has_active_work() is True
        assert not page.search_edit.isEnabled()
        assert page.send_button.isEnabled()
        assert "已打开 Fake Keyboard" in page.status_chip.text()

        _close_page(page)
        assert page.open_button.text() == "打开设备"
        assert page.has_active_work() is False
        assert page.search_edit.isEnabled()
        assert not page.send_button.isEnabled()
        assert backend.closed == 1
    finally:
        page.close()


def test_receive_report_updates_log_and_counters(qapp, tmp_path, monkeypatch):
    page, backend, _settings, _errors = _make_page(tmp_path, monkeypatch)
    try:
        _open_page(page)
        backend.reads.put(b"\x01\x02")
        assert process_events_until(
            lambda: page.rx_log.session().total_bytes("rx") == 2, timeout_ms=3000
        )
        assert process_events_until(
            lambda: "01 02" in page.rx_log.log_view.toPlainText(), timeout_ms=3000
        )
        assert "接收 2 B" in page.counter_label.text()
    finally:
        _close_page(page)
        page.close()


def test_send_without_report_id_pads_report(qapp, tmp_path, monkeypatch):
    page, backend, _settings, _errors = _make_page(tmp_path, monkeypatch)
    try:
        _open_page(page)
        page.report_id_spin.setValue(0)
        page.report_length_spin.setValue(8)
        page.send_editor.setPlainText("AA BB")
        page.send_button.click()
        assert process_events_until(lambda: backend.writes, timeout_ms=3000)
        assert backend.writes[0] == b"\xAA\xBB" + b"\x00" * 6
        assert page.tx_log.session().total_bytes("tx") == 8
        assert "发送 8 B" in page.counter_label.text()
    finally:
        _close_page(page)
        page.close()


def test_send_with_report_id_prepends_and_truncates(qapp, tmp_path, monkeypatch):
    page, backend, _settings, _errors = _make_page(tmp_path, monkeypatch)
    try:
        _open_page(page)
        page.report_id_spin.setValue(7)
        page.report_length_spin.setValue(4)
        page.send_editor.setPlainText("01 02 03 04 05")
        page.send_button.click()
        assert process_events_until(lambda: backend.writes, timeout_ms=3000)
        assert backend.writes[0] == b"\x07\x01\x02\x03"
        assert page.tx_log.session().total_bytes("tx") == 4
    finally:
        _close_page(page)
        page.close()


def test_invalid_hex_sends_nothing_and_reports_error(qapp, tmp_path, monkeypatch):
    page, backend, _settings, errors = _make_page(tmp_path, monkeypatch)
    try:
        _open_page(page)
        page.send_editor.setPlainText("GG")
        page.send_button.click()
        assert process_events_until(lambda: errors)
        QTest.qWait(50)
        assert backend.writes == []
        assert any("HEX" in str(item) for item in errors)
    finally:
        _close_page(page)
        page.close()


def test_read_error_auto_closes_with_readable_message(qapp, tmp_path, monkeypatch):
    page, backend, _settings, errors = _make_page(tmp_path, monkeypatch)
    try:
        _open_page(page)
        backend.fail_read = True
        assert process_events_until(
            lambda: page.status_chip.state == "error" and not page._worker.is_open(),
            timeout_ms=3000,
        )
        assert errors
        assert "设备可能已拔出" in errors[-1][2]
        assert page.open_button.text() == "打开设备"
        assert page.search_edit.isEnabled()
        assert not page.send_button.isEnabled()
    finally:
        page.close()


def test_send_error_auto_closes(qapp, tmp_path, monkeypatch):
    page, backend, _settings, errors = _make_page(tmp_path, monkeypatch)
    try:
        _open_page(page)
        backend.fail_write = True
        page.send_editor.setPlainText("01")
        page.send_button.click()
        assert process_events_until(
            lambda: page.status_chip.state == "error" and not page._worker.is_open(),
            timeout_ms=3000,
        )
        assert errors and "发送 HID Report 失败" in errors[-1][2]
    finally:
        page.close()


def test_repeat_send_uses_interval(qapp, tmp_path, monkeypatch):
    page, backend, _settings, _errors = _make_page(tmp_path, monkeypatch)
    try:
        _open_page(page)
        page.report_length_spin.setValue(2)
        page.send_editor.setPlainText("55")
        page.interval_spin.setValue(20)
        page.repeat_check.setChecked(True)
        assert page._repeat_timer.isActive()
        QTest.qWait(120)
        assert len(backend.writes) >= 2
        page.repeat_check.setChecked(False)
        assert not page._repeat_timer.isActive()
    finally:
        _close_page(page)
        page.close()


def test_file_send_is_split_into_reports(qapp, tmp_path, monkeypatch):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"ABCDEF")
    page, backend, _settings, _errors = _make_page(tmp_path, monkeypatch)
    try:
        _open_page(page)
        page.report_id_spin.setValue(0)
        page.report_length_spin.setValue(4)
        monkeypatch.setattr(
            hid_page_module.QFileDialog,
            "getOpenFileName",
            lambda *args, **kwargs: (str(source), ""),
        )
        page.send_file_button.click()
        assert process_events_until(lambda: len(backend.writes) >= 2, timeout_ms=3000)
        assert backend.writes == [b"ABCD", b"EF\x00\x00"]
        assert process_events_until(
            lambda: "文件发送完成" in page.file_progress_label.text(), timeout_ms=3000
        )
        assert page.tx_log.session().total_bytes("tx") == 8
    finally:
        _close_page(page)
        page.close()


def test_settings_round_trip_and_last_device(qapp, tmp_path, monkeypatch):
    page, _backend, settings, _errors = _make_page(tmp_path, monkeypatch)
    try:
        page.report_id_spin.setValue(9)
        page.report_length_spin.setValue(32)
        _open_page(page)
        _close_page(page)
        page.close()
        reloaded = Settings(settings._path)
        reloaded.load()
        assert reloaded.get("hid.report_id") == 9
        assert reloaded.get("hid.report_length") == 32
        assert bytes.fromhex(reloaded.get("hid.last_device_path")) == DEVICES[0].path
    finally:
        page.close()


def test_session_combined_and_counters_reset(qapp, tmp_path, monkeypatch):
    page, backend, _settings, _errors = _make_page(tmp_path, monkeypatch)
    try:
        _open_page(page)
        backend.reads.put(b"rx")
        assert process_events_until(
            lambda: page.rx_log.session().total_bytes("rx") == 2, timeout_ms=3000
        )
        page.report_length_spin.setValue(4)
        page.send_editor.setPlainText("74 78")
        page.send_button.click()
        assert process_events_until(
            lambda: page.tx_log.session().total_bytes("tx") == 4, timeout_ms=3000
        )
        combined = page._combined_session()
        directions = {record.direction for record in combined.records()}
        assert {"rx", "tx"} <= directions

        page._reset_counters()
        assert page.rx_log.session().total_bytes("rx") == 0
        assert page.tx_log.session().total_bytes("tx") == 0
        assert "接收 0 B · 发送 0 B" in page.counter_label.text()

        page.rx_log.clear()
        assert page.rx_log.log_view.toPlainText() == ""
    finally:
        _close_page(page)
        page.close()


def test_scope_notice_and_has_active_work(qapp, tmp_path, monkeypatch):
    page, _backend, _settings, _errors = _make_page(tmp_path, monkeypatch)
    try:
        assert "标准 HID" in page.scope_label.text()
        assert "CH372" in page.scope_label.text()
        assert page.has_active_work() is False
        _open_page(page)
        assert page.has_active_work() is True
        page.stop_workers()
        assert process_events_until(lambda: not page.has_active_work(), timeout_ms=3000)
        assert not page._worker.isRunning()
    finally:
        page.close()


def test_failed_open_shows_error_and_stays_closed(qapp, tmp_path, monkeypatch):
    backend = FakeHidBackend()
    backend.fail_open = True
    page, _backend, _settings, errors = _make_page(tmp_path, monkeypatch, backend=backend)
    try:
        page.open_button.click()
        assert process_events_until(lambda: errors, timeout_ms=3000)
        assert "打开 HID 设备失败" in errors[-1][2]
        assert page.status_chip.state == "error"
        assert page.open_button.text() == "打开设备"
        assert page.open_button.isEnabled()
        assert not page.has_active_work()
    finally:
        page.close()

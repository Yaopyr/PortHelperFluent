"""校验计算页离屏测试：文本/HEX/文件、取反、大小端、复制与源码预览。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest

from app.core.checksum import compute_all, format_value
from app.core.settings import Settings
from app.services.paths import resolve_paths
from app.ui.pages import checksum_page as checksum_page_module
from app.ui.pages.checksum_page import ChecksumPage, _FileChecksumTask
from app.ui.qt_utils import process_events_until

CRC32 = "CBF43926"
CRC32_DEC = str(0xCBF43926)
CRC32_BIN = "11001011111101000011100100100110"


def _make_page(tmp_path, monkeypatch):
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    settings = Settings(paths.config_file)
    errors: list[tuple] = []
    monkeypatch.setattr(
        checksum_page_module, "show_error", lambda *args: errors.append(args)
    )
    page = ChecksumPage(settings, paths)
    page.show()
    return page, settings, paths, errors


def _row_for(page: ChecksumPage, name: str) -> int:
    for row in range(page.results_table.rowCount()):
        item = page.results_table.item(row, 0)
        if item is not None and item.text() == name:
            return row
    return -1


def _hex_for(page: ChecksumPage, name: str) -> str:
    row = _row_for(page, name)
    assert row >= 0, name
    return page.results_table.item(row, 1).text()


def _dec_for(page: ChecksumPage, name: str) -> str:
    row = _row_for(page, name)
    assert row >= 0, name
    return page.results_table.item(row, 2).text()


def _compute_text(page: ChecksumPage, text: str) -> None:
    page.set_mode("text")
    page.text_editor.setPlainText(text)
    page.compute_button.click()


def test_page_metadata_and_source_preview(qapp, tmp_path, monkeypatch):
    page, _settings, _paths, _errors = _make_page(tmp_path, monkeypatch)
    try:
        assert page.objectName() == "checksum"
        assert "校验" in page.info_label.text()
        assert page.results_table.columnCount() == 5
        assert page.source_combo.currentData() == "CRC-32/ISO-HDLC"
        snippet = page.source_view.toPlainText()
        assert snippet.strip() and "def " in snippet
        assert page.source_view.isReadOnly()

        index = page.source_combo.findData("CRC-16/MODBUS")
        assert index >= 0
        page.source_combo.setCurrentIndex(index)
        text = page.source_view.toPlainText()
        assert "crc_16_modbus" in text
        assert "0x8005" in text
    finally:
        page.close()


def test_text_crc32_visible_with_all_columns(qapp, tmp_path, monkeypatch):
    page, _settings, _paths, errors = _make_page(tmp_path, monkeypatch)
    try:
        _compute_text(page, "123456789")
        assert errors == []
        assert page.results_table.rowCount() == 16
        assert _hex_for(page, "CRC-32/ISO-HDLC") == CRC32
        assert _dec_for(page, "CRC-32/ISO-HDLC") == CRC32_DEC
        row = _row_for(page, "CRC-32/ISO-HDLC")
        assert page.results_table.item(row, 3).text() == CRC32_BIN
        assert page.status_chip.state == "ok"
        assert "9 B" in page.status_chip.text()
        assert "16" in page.result_summary_label.text()
    finally:
        page.close()


def test_hex_input_matches_text(qapp, tmp_path, monkeypatch):
    page, _settings, _paths, errors = _make_page(tmp_path, monkeypatch)
    try:
        page.set_mode("hex")
        page.hex_editor.setPlainText("31 32 33 34 35 36 37 38 39")
        page.compute_button.click()
        assert errors == []
        assert page.mode() == "hex"
        assert _hex_for(page, "CRC-32/ISO-HDLC") == CRC32
    finally:
        page.close()


def test_invert_toggle_updates_table(qapp, tmp_path, monkeypatch):
    page, _settings, _paths, _errors = _make_page(tmp_path, monkeypatch)
    try:
        _compute_text(page, "123456789")
        assert _hex_for(page, "CRC-32/ISO-HDLC") == CRC32

        page.invert_check.setChecked(True)
        assert _hex_for(page, "CRC-32/ISO-HDLC") == "340BC6D9"
        assert _dec_for(page, "CRC-32/ISO-HDLC") == str(~0xCBF43926 & 0xFFFFFFFF)

        page.invert_check.setChecked(False)
        assert _hex_for(page, "CRC-32/ISO-HDLC") == CRC32
    finally:
        page.close()


def test_little_endian_display_for_16_and_32_bit(qapp, tmp_path, monkeypatch):
    page, _settings, _paths, _errors = _make_page(tmp_path, monkeypatch)
    try:
        _compute_text(page, "123456789")
        assert _hex_for(page, "CRC-16/MODBUS") == "4B37"
        assert _hex_for(page, "CRC-32/ISO-HDLC") == CRC32

        page.little_endian_check.setChecked(True)
        assert _hex_for(page, "CRC-16/MODBUS") == "374B"
        assert _hex_for(page, "CRC-32/ISO-HDLC") == "2639F4CB"
        assert _dec_for(page, "CRC-16/MODBUS") == str(0x4B37)
        row = _row_for(page, "CRC-16/MODBUS")
        assert page.results_table.item(row, 3).text() == format_value(0x374B, 16, 2)
    finally:
        page.close()


def test_copy_button_copies_hex(qapp, tmp_path, monkeypatch):
    page, _settings, _paths, _errors = _make_page(tmp_path, monkeypatch)
    try:
        _compute_text(page, "123456789")
        row = _row_for(page, "CRC-32/ISO-HDLC")
        button = page.results_table.cellWidget(row, 4)
        assert button is not None
        button.click()
        assert QApplication.clipboard().text() == CRC32
    finally:
        page.close()


def test_text_change_triggers_auto_compute(qapp, tmp_path, monkeypatch):
    page, _settings, _paths, _errors = _make_page(tmp_path, monkeypatch)
    try:
        page.set_mode("text")
        page.text_editor.setPlainText("123456789")
        assert process_events_until(
            lambda: page.results_table.rowCount() == 16, timeout_ms=3000
        )
        assert _hex_for(page, "CRC-32/ISO-HDLC") == CRC32
    finally:
        page.close()


def test_file_mode_streams_chunks_and_reports_progress(qapp, tmp_path, monkeypatch):
    data = (b"123456789" * 200000)[: 1024 * 1024 + 7]
    source = tmp_path / "payload.bin"
    source.write_bytes(data)
    expected = {
        result.name: result.value for result in compute_all(data)
    }
    page, _settings, _paths, errors = _make_page(tmp_path, monkeypatch)
    try:
        page.set_mode("file")
        page.file_picker.set_path(str(source))
        page.compute_button.click()
        assert process_events_until(
            lambda: page.status_chip.state in ("ok", "error")
            and not page.has_active_work(),
            timeout_ms=15000,
        )
        assert errors == []
        assert page.status_chip.state == "ok"
        assert page.progress_bar.value() == 100
        assert "已计算" in page.file_info_label.text()
        assert _hex_for(page, "CRC-32/ISO-HDLC") == format_value(
            expected["CRC-32/ISO-HDLC"], 32
        )
        assert _hex_for(page, "Sum-16") == format_value(expected["Sum-16"], 16)
        assert page.compute_button.isEnabled()
        assert page.file_picker.isEnabled()
    finally:
        page.close()


def test_file_task_cancel_before_run(qapp, tmp_path):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"data")
    canceled: list[bool] = []
    task = _FileChecksumTask(source)
    task.signals.canceled.connect(lambda: canceled.append(True))
    task.cancel()
    task.run()
    assert canceled == [True]


def test_cancel_button_resets_file_mode(qapp, tmp_path, monkeypatch):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"123456789" * 4096)
    page, _settings, _paths, _errors = _make_page(tmp_path, monkeypatch)
    try:
        page.set_mode("file")
        page.file_picker.set_path(str(source))
        page._start_file_compute(notify=False)
        assert page.has_active_work() is True
        page.cancel_button.click()
        assert page.has_active_work() is False
        assert page.status_chip.state == "idle"
        assert page.status_chip.text() == "已取消"
        assert page.progress_bar.value() == 0
        assert page._file_task is None
    finally:
        page.close()


def test_invalid_hex_shows_error_and_keeps_page_alive(qapp, tmp_path, monkeypatch):
    page, _settings, _paths, errors = _make_page(tmp_path, monkeypatch)
    try:
        page.set_mode("hex")
        page.hex_editor.setPlainText("GG")
        page.compute_button.click()
        QTest.qWait(50)
        assert errors
        assert errors[-1][1] == "校验输入有误"
        assert "十六进制" in errors[-1][2]
        assert page.results_table.rowCount() == 0
        assert page.status_chip.state == "error"

        page.hex_editor.setPlainText("01 02")
        page.compute_button.click()
        assert page.results_table.rowCount() == 16
        assert page.status_chip.state == "ok"
    finally:
        page.close()


def test_missing_file_reports_error(qapp, tmp_path, monkeypatch):
    page, _settings, _paths, errors = _make_page(tmp_path, monkeypatch)
    try:
        page.set_mode("file")
        page.file_picker.set_path(str(tmp_path / "not-there.bin"))
        page.compute_button.click()
        QTest.qWait(50)
        assert errors
        assert errors[-1][1] == "无法读取文件"
        assert page.status_chip.state == "error"
        assert page.results_table.rowCount() == 0
    finally:
        page.close()


def test_encoding_setting_round_trip(qapp, tmp_path, monkeypatch):
    page, settings, paths, _errors = _make_page(tmp_path, monkeypatch)
    try:
        index = page.encoding_combo.findData("gbk")
        assert index >= 0
        page.encoding_combo.setCurrentIndex(index)
        reloaded = Settings(paths.config_file)
        reloaded.load()
        assert reloaded.get("checksum.encoding") == "gbk"
    finally:
        page.close()


def test_text_encoding_changes_byte_input(qapp, tmp_path, monkeypatch):
    page, _settings, _paths, _errors = _make_page(tmp_path, monkeypatch)
    try:
        text = "你好"
        page.set_mode("text")
        gbk_index = page.encoding_combo.findData("gbk")
        page.encoding_combo.setCurrentIndex(gbk_index)
        page.text_editor.setPlainText(text)
        page.compute_button.click()
        gbk_crc = _hex_for(page, "CRC-32/ISO-HDLC")
        expected = {
            result.name: result.value
            for result in compute_all(text.encode("gbk"))
        }["CRC-32/ISO-HDLC"]
        assert gbk_crc == format_value(expected, 32)

        utf8_index = page.encoding_combo.findData("utf-8")
        page.encoding_combo.setCurrentIndex(utf8_index)
        page.compute_button.click()
        assert _hex_for(page, "CRC-32/ISO-HDLC") != gbk_crc
    finally:
        page.close()


def test_active_work_and_stop_workers_cleanup(qapp, tmp_path, monkeypatch):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"123456789" * 8192)
    page, _settings, _paths, _errors = _make_page(tmp_path, monkeypatch)
    try:
        assert page.has_active_work() is False
        page.set_mode("file")
        page.file_picker.set_path(str(source))
        page._start_file_compute(notify=False)
        assert page.has_active_work() is True
        page.stop_workers()
        assert page.has_active_work() is False
        assert process_events_until(lambda: page.status_chip.state != "busy", timeout_ms=3000)
    finally:
        page.close()

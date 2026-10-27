"""编码转换页离屏测试：工具切换、进制转换、数码管与文件转换。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from app.core.settings import Settings
from app.services.paths import resolve_paths
from app.ui.pages.convert import ConvertPage
from app.ui.pages.convert import data_tool as data_tool_module
from app.ui.pages.convert.data_tool import (
    OP_BIN_TO_TEXT,
    OP_HEX_TO_BIN,
    OP_HEX_TO_TEXT,
)


def _make_page(tmp_path, monkeypatch):
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    settings = Settings(paths.config_file)
    settings.load()
    errors: list[tuple] = []
    successes: list[tuple] = []
    monkeypatch.setattr(
        data_tool_module, "show_error", lambda *args: errors.append(args)
    )
    monkeypatch.setattr(
        data_tool_module, "show_success", lambda *args: successes.append(args)
    )
    page = ConvertPage(settings, paths)
    page.show()
    return page, settings, paths, errors, successes


def test_page_metadata_registry_and_lifecycle(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        assert page.objectName() == "convert"
        assert page.title_label.text() == "编码转换"
        assert "进制" in page.subtitle_label.text()
        assert page.tool_routes() == ("base", "data", "sevenseg")
        assert page.tool_stack.count() == 3
        assert page.current_tool_route() == "base"
        assert page.current_tool() is page.tools["base"]
        assert page.base_tool is page.tools["base"]
        assert page.data_tool is page.tools["data"]
        assert page.sevenseg_tool is page.tools["sevenseg"]
        assert page.has_active_work() is False
        page.stop_workers()
        assert QApplication.activeModalWidget() is None
    finally:
        page.close()


def test_switch_tool_updates_stack_and_setting(qapp, tmp_path, monkeypatch) -> None:
    page, settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        page.set_tool("sevenseg")
        assert page.current_tool_route() == "sevenseg"
        assert page.tool_stack.currentWidget() is page.tools["sevenseg"]
        assert settings.get("convert.last_tool") == "sevenseg"

        page.tool_switch.setCurrentItem("data")
        assert page.tool_stack.currentWidget() is page.tools["data"]
        assert settings.get("convert.last_tool") == "data"

        with pytest.raises(ValueError, match="未知"):
            page.set_tool("palette")
    finally:
        page.close()


def test_selected_tool_is_restored_from_settings(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        page.set_tool("sevenseg")
    finally:
        page.close()

    reloaded = Settings(paths.config_file)
    reloaded.load()
    assert reloaded.get("convert.last_tool") == "sevenseg"

    page2 = ConvertPage(reloaded, paths)
    page2.show()
    try:
        assert page2.current_tool_route() == "sevenseg"
        assert page2.tool_stack.currentWidget() is page2.tools["sevenseg"]
    finally:
        page2.close()


def test_base_tool_live_conversion(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        base = page.tools["base"]
        assert base.base() == 10
        assert base.bits() == 32

        base.input_edit.setText("255")
        assert base.output_values() == {
            2: "11111111",
            8: "377",
            10: "255",
            16: "FF",
        }
        assert base.error_label.text() == ""
        assert base.input_edit.isError() is False
    finally:
        page.close()


def test_base_tool_signed_two_complement_and_arbitrary_base(
    qapp, tmp_path, monkeypatch
) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        base = page.tools["base"]
        base.signed_check.setChecked(True)
        base.input_edit.setText("-1")
        assert base.output_values()[10] == "-1"
        assert base.output_values()[16] == "FFFFFFFF"
        assert base.output_values()[2] == "1" * 32
        assert base.output_values()[8] == "37777777777"

        bits64_index = base.bits_combo.findData(64)
        assert bits64_index >= 0
        base.bits_combo.setCurrentIndex(bits64_index)
        assert base.output_values()[2] == "1" * 64
        assert base.output_values()[16] == "FFFFFFFFFFFFFFFF"
        base.bits_combo.setCurrentIndex(base.bits_combo.findData(32))

        hex_index = base.from_base_combo.findData(16)
        assert hex_index >= 0
        base.from_base_combo.setCurrentIndex(hex_index)
        base.input_edit.setText("FFFFFFFF")
        assert base.output_values()[10] == "-1"

        base.signed_check.setChecked(False)
        base.input_edit.setText("FF")
        assert base.output_values()[10] == "255"

        z_index = base.from_base_combo.findData(36)
        base.from_base_combo.setCurrentIndex(z_index)
        base.input_edit.setText("Z")
        assert base.output_values()[10] == "35"
        assert base.output_values()[16] == "23"
    finally:
        page.close()


def test_base_tool_invalid_input_shows_inline_error(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        base = page.tools["base"]
        base.from_base_combo.setCurrentIndex(base.from_base_combo.findData(10))
        base.input_edit.setText("GG")
        assert base.error_label.text()
        assert "10" in base.error_label.text()
        assert base.input_edit.isError() is True
        assert "C42B1C" in base.input_edit.styleSheet()
        assert all(text == "" for text in base.output_values().values())

        base.input_edit.setText("16")
        assert base.error_label.text() == ""
        assert base.input_edit.isError() is False
        assert "C42B1C" not in base.input_edit.styleSheet()
        assert base.output_values()[16] == "10"

        base.input_edit.clear()
        assert base.error_label.text() == ""
        assert all(text == "" for text in base.output_values().values())
    finally:
        page.close()


def test_base_tool_copy_output(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        base = page.tools["base"]
        base.input_edit.setText("255")
        base.copy_buttons[16].click()
        assert QApplication.clipboard().text() == "FF"
        assert "已复制" in base.status_label.text()

        base.copy_buttons[2].click()
        assert QApplication.clipboard().text() == "11111111"
    finally:
        page.close()


def test_seven_segment_digit_to_code_and_back(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        seven = page.tools["sevenseg"]
        assert seven.common_cathode_code() == 0x3F
        assert seven.output_edits["cathode_hex"].text() == "3F"
        assert seven.output_edits["cathode_bin"].text() == "00111111"
        assert seven.output_edits["anode_hex"].text() == "C0"
        assert seven.output_edits["anode_bin"].text() == "11000000"
        assert [name for name, box in seven.segment_checks.items() if box.isChecked()] == [
            "a",
            "b",
            "c",
            "d",
            "e",
            "f",
        ]

        seven.digit_edit.setText("A")
        assert seven.common_cathode_code() == 0x77
        assert seven.output_edits["cathode_hex"].text() == "77"
        assert seven.output_edits["anode_hex"].text() == "88"
        assert [name for name, box in seven.segment_checks.items() if box.isChecked()] == [
            "a",
            "b",
            "c",
            "e",
            "f",
            "g",
        ]
    finally:
        page.close()


def test_seven_segment_toggle_segments_updates_code(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        seven = page.tools["sevenseg"]
        for box in seven.segment_checks.values():
            box.setChecked(False)
        assert seven.common_cathode_code() == 0
        assert seven.output_edits["cathode_hex"].text() == "00"
        assert seven.output_edits["anode_hex"].text() == "FF"

        seven.segment_checks["a"].setChecked(True)
        seven.segment_checks["b"].setChecked(True)
        seven.segment_checks["c"].setChecked(True)
        assert seven.common_cathode_code() == 0x07
        assert seven.common_anode_code() == 0xF8
        assert seven.output_edits["cathode_hex"].text() == "07"
        assert seven.output_edits["cathode_bin"].text() == "00000111"

        seven.segment_checks["dp"].setChecked(True)
        assert seven.common_cathode_code() == 0x87
        assert seven.output_edits["anode_hex"].text() == "78"
        assert "自定义" in seven.pattern_label.text()
    finally:
        page.close()


def test_seven_segment_invalid_character_and_copy(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        seven = page.tools["sevenseg"]
        seven.digit_edit.setText("G")
        assert seven.error_label.text()
        assert seven.digit_edit.isError() is True
        assert seven.output_edits["cathode_hex"].text() == ""

        seven.digit_edit.setText("9")
        assert seven.error_label.text() == ""
        assert seven.digit_edit.isError() is False

        seven.copy_buttons["cathode_hex"].click()
        assert QApplication.clipboard().text() == "6F"
    finally:
        page.close()


def test_data_tool_bin_to_text_with_temp_files(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["data"]
        source = tmp_path / "input.bin"
        target = tmp_path / "output_hex.txt"
        source.write_bytes(b"AB\x00\xff")

        result = tool.convert(source, target, OP_BIN_TO_TEXT)

        assert result == target
        assert target.read_text(encoding="utf-8") == "41 42 00 FF"
        assert tool.progress_bar.value() == 100
        assert "41 42" in tool.preview_edit.toPlainText()
        assert tool.status_label.text().startswith("Bin→Text")
        assert tool.error_label.text() == ""
    finally:
        page.close()


@pytest.mark.parametrize(
    ("hex_text", "codec", "expected"),
    [
        ("C4 E3", "gbk", "你"),
        ("E4 BD A0", "utf-8", "你"),
    ],
)
def test_data_tool_hex_to_text_uses_encoding(
    qapp, tmp_path, monkeypatch, hex_text: str, codec: str, expected: str
) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["data"]
        source = tmp_path / "input.txt"
        target = tmp_path / "decoded.txt"
        source.write_text(hex_text, encoding="utf-8")

        tool.convert(source, target, OP_HEX_TO_TEXT, codec)

        assert target.read_text(encoding=codec) == expected
        assert tool.preview_edit.toPlainText() == expected
    finally:
        page.close()


def test_data_tool_hex_to_bin_writes_raw_bytes(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["data"]
        source = tmp_path / "input.hex.txt"
        target = tmp_path / "output.bin"
        source.write_text("00 FF 0A\n41 42", encoding="utf-8")

        tool.convert(source, target, OP_HEX_TO_BIN)

        assert target.read_bytes() == b"\x00\xff\x0aAB"
        assert "00 FF 0A" in tool.preview_edit.toPlainText()
    finally:
        page.close()


def test_data_tool_default_encoding_is_utf8(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["data"]
        assert tool.codec() == "utf-8"

        source = tmp_path / "utf8.hex.txt"
        target = tmp_path / "utf8.txt"
        source.write_text("E4 BD A0", encoding="utf-8")
        tool.convert(source, target, OP_HEX_TO_TEXT)
        assert target.read_text(encoding="utf-8") == "你"
    finally:
        page.close()


def test_data_tool_invalid_hex_does_not_touch_existing_output(
    qapp, tmp_path, monkeypatch
) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["data"]
        source = tmp_path / "bad.txt"
        target = tmp_path / "output.bin"
        source.write_text("0x0G", encoding="utf-8")
        target.write_bytes(b"keep-me")

        with pytest.raises(ValueError, match="十六进制") as excinfo:
            tool.convert(source, target, OP_HEX_TO_BIN)

        message = str(excinfo.value)
        assert str(source) in message
        assert "G" in message
        assert target.read_bytes() == b"keep-me"
        assert tool.error_label.text()
        assert tool.progress_bar.value() == 0
    finally:
        page.close()


def test_data_tool_rejects_missing_input_and_same_path(
    qapp, tmp_path, monkeypatch
) -> None:
    page, _settings, _paths, errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["data"]
        missing = tmp_path / "missing.bin"
        with pytest.raises(ValueError, match="找不到输入文件"):
            tool.convert(missing, tmp_path / "out.txt", OP_BIN_TO_TEXT)

        source = tmp_path / "same.bin"
        source.write_bytes(b"1234")
        with pytest.raises(ValueError, match="不能与输入文件相同"):
            tool.convert(source, source, OP_BIN_TO_TEXT)
        assert errors == []
    finally:
        page.close()


def test_data_tool_uses_pickers_and_convert_button(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["data"]
        source = tmp_path / "sample.bin"
        source.write_bytes(b"Hi")
        tool.input_picker.set_path(source)
        assert tool.output_picker.path()
        assert Path(tool.output_picker.path()).name == "sample_hex.txt"

        tool.convert_button.click()
        assert errors == []
        assert Path(tool.output_picker.path()).read_text(encoding="utf-8") == "48 69"
        assert successes

        tool.set_operation(OP_HEX_TO_BIN)
        assert tool.operation() == OP_HEX_TO_BIN
        assert Path(tool.output_picker.path()).name == "sample_bin.bin"
    finally:
        page.close()


def test_data_tool_convert_button_reports_error_without_dialog(
    qapp, tmp_path, monkeypatch
) -> None:
    page, _settings, _paths, errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["data"]
        source = tmp_path / "bad.txt"
        source.write_text("GG", encoding="utf-8")
        tool.input_picker.set_path(source)
        tool.output_picker.set_path(tmp_path / "out.bin")
        tool.set_operation(OP_HEX_TO_BIN)

        tool.convert_button.click()

        assert errors
        assert "十六进制" in str(errors[0])
        assert QApplication.activeModalWidget() is None
    finally:
        page.close()

"""编码转换页离屏测试：七个工具的切换、转换、导出与预览。"""

from __future__ import annotations

import array
import math
import wave
from pathlib import Path

import pytest
from PIL import Image
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


_CREATED_PAGES: list = []


@pytest.fixture(autouse=True)
def _dispose_pages(qapp):
    """测试结束后通过 Qt 删除页面，避免控件环在后续主题切换时被 GC 打断。"""
    yield
    from PyQt6.QtCore import QCoreApplication, QEvent

    while _CREATED_PAGES:
        page = _CREATED_PAGES.pop()
        try:
            page.close()
            page.deleteLater()
        except RuntimeError:
            # 页面可能已在测试内被删除。
            pass
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


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
    _CREATED_PAGES.append(page)
    page.show()
    return page, settings, paths, errors, successes


def test_page_metadata_registry_and_lifecycle(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        assert page.objectName() == "convert"
        assert page.title_label.text() == "编码转换"
        assert "进制" in page.subtitle_label.text()
        assert page.tool_routes() == (
            "base",
            "data",
            "sevenseg",
            "palette",
            "gif",
            "audio",
            "qr",
        )
        assert page.tool_stack.count() == 7
        assert page.current_tool_route() == "base"
        assert page.current_tool() is page.tools["base"]
        assert page.base_tool is page.tools["base"]
        assert page.data_tool is page.tools["data"]
        assert page.sevenseg_tool is page.tools["sevenseg"]
        assert page.palette_tool is page.tools["palette"]
        assert page.gif_tool is page.tools["gif"]
        assert page.audio_tool is page.tools["audio"]
        assert page.qr_tool is page.tools["qr"]
        for route in ("palette", "gif", "audio", "qr"):
            page.set_tool(route)
            assert page.current_tool_route() == route
            assert page.tool_stack.currentWidget() is page.tools[route]
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

        page.set_tool("qr")
        assert page.current_tool_route() == "qr"
        assert page.tool_stack.currentWidget() is page.tools["qr"]
        assert settings.get("convert.last_tool") == "qr"

        with pytest.raises(ValueError, match="未知"):
            page.set_tool("missing")
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
    _CREATED_PAGES.append(page2)
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


# ---- Task 8：调色板、GIF、音频与二维码 ------------------------------------


def _has_pixmap(label) -> bool:
    pixmap = label.pixmap()
    return pixmap is not None and not pixmap.isNull()


def _write_page_gif(path: Path) -> None:
    first = Image.new("RGB", (20, 10), (255, 0, 0))
    second = Image.new("RGB", (20, 10), (0, 0, 255))
    first.save(
        path,
        save_all=True,
        append_images=[second],
        duration=[100, 200],
        loop=0,
        disposal=2,
    )


def _write_page_wav(path: Path, seconds: float = 0.1, rate: int = 8000) -> int:
    count = int(seconds * rate)
    samples = array.array(
        "h",
        (
            int(12000 * math.sin(2 * math.pi * 440 * index / rate))
            for index in range(count)
        ),
    )
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(samples.tobytes())
    return count


def test_palette_tool_live_conversion_errors_and_copy(
    qapp, tmp_path, monkeypatch
) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        palette = page.tools["palette"]
        palette.set_rgb(255, 0, 0)
        assert palette.hex_value() == "#FF0000"
        assert palette.output_values() == {
            "hex": "#FF0000",
            "rgb888": "0xFF0000",
            "rgb565": "0xF800",
            "rgb332": "0xE0",
        }
        assert "#FF0000" in palette.swatch_label.styleSheet()
        assert palette.error_label.text() == ""
        assert palette.hex_edit.isError() is False

        palette.hex_edit.setText("#00FF00")
        assert palette.current_rgb() == (0, 255, 0)
        assert palette.output_values()["rgb565"] == "0x07E0"
        assert palette.output_values()["rgb332"] == "0x1C"

        palette.red_spin.setValue(0)
        palette.green_spin.setValue(0)
        palette.blue_spin.setValue(255)
        assert palette.hex_value() == "#0000FF"
        assert palette.output_values()["rgb565"] == "0x001F"

        palette.copy_buttons["rgb565"].click()
        assert QApplication.clipboard().text() == "0x001F"
        assert "已复制" in palette.status_label.text()

        palette.hex_edit.setText("GG")
        assert palette.error_label.text()
        assert palette.hex_edit.isError() is True
        assert all(value == "" for value in palette.output_values().values())

        palette.hex_edit.setText("#123456")
        assert palette.error_label.text() == ""
        assert palette.hex_edit.isError() is False
        assert palette.output_values()["rgb888"] == "0x123456"
        assert QApplication.activeModalWidget() is None
    finally:
        page.close()


def test_gif_tool_load_preview_play_and_export(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["gif"]
        source = tmp_path / "clip.gif"
        _write_page_gif(source)

        frames = tool.load(source)

        assert len(frames) == 2
        assert tool.frame_count() == 2
        assert tool.frame_durations() == [100, 200]
        assert (tool.gif_width, tool.gif_height) == (20, 10)
        assert "2 帧" in tool.frame_info_label.text()
        assert _has_pixmap(tool.preview_label)
        assert "1 / 2" in tool.preview_caption.text()

        tool.set_preview_index(1)
        assert tool.current_frame_index() == 1
        assert "2 / 2" in tool.preview_caption.text()

        assert tool.play() is True
        assert tool.is_playing() is True
        tool.set_speed(200)
        assert tool.speed() == 200
        assert "200%" in tool.speed_label.text()
        tool.pause()
        assert tool.is_playing() is False

        output = tmp_path / "frames"
        written = tool.export(output, 10, 5)

        assert [path.name for path in written] == ["clip_0001.bmp", "clip_0002.bmp"]
        assert all(path.is_file() for path in written)
        assert all(Image.open(path).size == (10, 5) for path in written)
        assert tool.progress_bar.value() == 100
        assert tool.error_label.text() == ""
        assert "已导出 2" in tool.status_label.text()
        assert list(output.glob("*.tmp")) == []
        assert QApplication.activeModalWidget() is None
    finally:
        page.close()


def test_gif_tool_reports_missing_file_inline(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["gif"]
        with pytest.raises(ValueError, match="找不到"):
            tool.load(tmp_path / "missing.gif")
        assert tool.error_label.text()
        assert tool.frame_count() == 0
        with pytest.raises(ValueError, match="载入"):
            tool.export(tmp_path / "out")
        assert QApplication.activeModalWidget() is None
    finally:
        page.close()


def test_audio_tool_load_info_and_export_c(qapp, tmp_path, monkeypatch) -> None:
    page, settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["audio"]
        source = tmp_path / "tone.wav"
        samples = _write_page_wav(source)

        audio = tool.load(source)

        assert audio.sample_rate == 8000
        assert len(audio.frames) == samples * 2
        info = tool.format_info()
        assert "WAV" in info
        assert "8000 Hz" in info
        assert "0.100" in info
        assert tool.name_edit.text() == "tone"

        c_path = tmp_path / "tone.c"
        h_path = tmp_path / "tone.h"
        result_c, result_h = tool.export_c(c_path, h_path)

        assert result_c == c_path
        assert result_h == h_path
        source_text = c_path.read_text(encoding="utf-8")
        header_text = h_path.read_text(encoding="utf-8")
        assert "const unsigned char tone[]" in source_text
        assert "tone_length = 800;" in source_text
        assert "8000" in source_text
        assert "extern const unsigned char tone[]" in header_text
        assert tool.progress_bar.value() == 100
        assert "已生成" in tool.status_label.text()
        assert tool.error_label.text() == ""
        assert settings.get("convert.audio.last_export") == str(tmp_path)
        assert QApplication.activeModalWidget() is None
    finally:
        page.close()


def test_audio_tool_invalid_format_and_missing_file(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["audio"]
        unsupported = tmp_path / "sound.ogg"
        unsupported.write_bytes(b"OggS")
        with pytest.raises(ValueError, match="不支持"):
            tool.load(unsupported)
        assert tool.error_label.text()
        assert tool.audio is None

        with pytest.raises(ValueError, match="找不到"):
            tool.load(tmp_path / "missing.wav")
        with pytest.raises(ValueError, match="载入"):
            tool.export_c(tmp_path / "out.c", tmp_path / "out.h")
        assert QApplication.activeModalWidget() is None
    finally:
        page.close()


def test_qr_tool_preview_colors_and_save(qapp, tmp_path, monkeypatch) -> None:
    page, settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    try:
        tool = page.tools["qr"]
        assert tool.preview_label.text()
        assert not _has_pixmap(tool.preview_label)

        tool.set_text("https://example.com/porthelper")
        assert "已生成" in tool.status_label.text()
        assert _has_pixmap(tool.preview_label)
        image = tool.qr_image()
        assert image.width == image.height
        assert image.width >= (21 + 2 * 4) * 8

        tool.ec_combo.setCurrentIndex(tool.ec_combo.findData("H"))
        assert tool.ec() == "H"
        tool.box_spin.setValue(4)
        tool.border_spin.setValue(2)
        assert tool.box_size() == 4
        assert tool.border() == 2

        tool.set_colors("#FF0000", "#0000FF")
        assert tool.fg_color() == "#FF0000"
        assert tool.bg_color() == "#0000FF"
        assert tool.qr_image().getpixel((0, 0)) == (0, 0, 255, 255)

        png_path = tmp_path / "preview.png"
        bmp_path = tmp_path / "preview.bmp"
        assert tool.save(png_path) == png_path
        assert png_path.is_file()
        assert tool.save(bmp_path) == bmp_path
        assert bmp_path.is_file()
        assert settings.get("convert.qr.last_path") == str(bmp_path)
        with Image.open(png_path) as saved:
            assert saved.size == tool.qr_image().size

        tool.set_text("   ")
        assert not _has_pixmap(tool.preview_label)
        with pytest.raises(ValueError, match="不能为空"):
            tool.save(tmp_path / "empty.png")
        assert QApplication.activeModalWidget() is None
    finally:
        page.close()

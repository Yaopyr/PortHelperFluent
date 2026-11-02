"""工具页场景：校验、编码转换、图片、点阵与 C51。"""

from __future__ import annotations

import json
import re
import zlib
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from app.core.fontmatrix import FontParams
from app.core.imaging import ImageParams
from app.ui.dialogs.custom_crc_dialog import CustomCrcDialog
from app.ui.pages.convert.data_tool import (
    OP_BIN_TO_TEXT,
    OP_HEX_TO_BIN,
    OP_HEX_TO_TEXT,
)
from tests.simulation.harness import assets

DATA = b"123456789"


# ---- 校验计算 ---------------------------------------------------------


def _row_for(page, name: str) -> int:
    for row in range(page.results_table.rowCount()):
        item = page.results_table.item(row, 0)
        if item is not None and item.text() == name:
            return row
    return -1


def _hex_for(page, name: str) -> str:
    row = _row_for(page, name)
    assert row >= 0, name
    return page.results_table.item(row, 1).text()


def _compute_text(page, text: str) -> None:
    page.set_mode("text")
    page.text_editor.setPlainText(text)
    page.compute_button.click()


def chk_01_known_vectors(sim) -> None:
    page = sim.switch("checksum")
    _compute_text(page, DATA.decode())
    sim.check("校验结果行数", page.results_table.rowCount(), 16)
    sim.check("CRC-32 已知向量", _hex_for(page, "CRC-32/ISO-HDLC"), f"{zlib.crc32(DATA) & 0xFFFFFFFF:08X}")
    sim.check("CRC-16/MODBUS 已知向量", _hex_for(page, "CRC-16/MODBUS"), "4B37")
    sim.check("CRC-16/CCITT-FALSE 已知向量", _hex_for(page, "CRC-16/CCITT-FALSE"), "29B1")
    sim.check("CRC-8/MAXIM 已知向量", _hex_for(page, "CRC-8/MAXIM"), "A1")
    sim.check("Sum-8 独立计算", _hex_for(page, "Sum-8"), f"{sum(DATA) & 0xFF:02X}")
    sim.check("Sum-16 独立计算", _hex_for(page, "Sum-16"), f"{sum(DATA) & 0xFFFF:04X}")

    page.invert_check.setChecked(True)
    sim.check("取反显示", _hex_for(page, "CRC-32/ISO-HDLC"), "340BC6D9")
    page.invert_check.setChecked(False)
    page.little_endian_check.setChecked(True)
    sim.check("小端显示", _hex_for(page, "CRC-16/MODBUS"), "374B")
    page.little_endian_check.setChecked(False)
    sim.screenshot("checksum-vectors")


def chk_02_hex_text_encoding_invalid(sim) -> None:
    page = sim.switch("checksum")
    page.set_mode("hex")
    page.hex_editor.setPlainText("31 32 33 34 35 36 37 38 39")
    page.compute_button.click()
    sim.check("HEX 输入 CRC-32", _hex_for(page, "CRC-32/ISO-HDLC"), "CBF43926")
    page.hex_editor.setPlainText("GG")
    page.compute_button.click()
    sim.wait(lambda: False, 80)
    sim.check("非法 HEX 无结果", page.results_table.rowCount(), 0)
    sim.check("非法 HEX 状态", page.status_chip.state, "error")
    page.hex_editor.setPlainText("01 02")
    page.compute_button.click()
    sim.check("恢复合法 HEX", page.results_table.rowCount(), 16)

    page.set_mode("text")
    page.encoding_combo.setCurrentIndex(page.encoding_combo.findData("gbk"))
    page.text_editor.setPlainText("你好")
    page.compute_button.click()
    gbk = page.results_table.item(_row_for(page, "CRC-32/ISO-HDLC"), 1).text()
    page.encoding_combo.setCurrentIndex(page.encoding_combo.findData("utf-8"))
    page.compute_button.click()
    utf8 = page.results_table.item(_row_for(page, "CRC-32/ISO-HDLC"), 1).text()
    sim.check_true("编码改变 CRC 输入", gbk != utf8, f"{gbk} == {utf8}")
    sim.screenshot("checksum-inputs")


def chk_03_source_and_copy(sim) -> None:
    page = sim.switch("checksum")
    _compute_text(page, DATA.decode())
    for name in ("CRC-32/ISO-HDLC", "CRC-16/MODBUS", "CRC-8/MAXIM"):
        index = page.source_combo.findData(name)
        sim.check_true(f"源码下拉含 {name}", index >= 0, str(index))
        page.source_combo.setCurrentIndex(index)
        sim.check_true(f"{name} 源码非空", bool(page.source_view.toPlainText().strip()), "源码为空")
    page.source_combo.setCurrentIndex(page.source_combo.findData("CRC-32/ISO-HDLC"))
    row = _row_for(page, "CRC-32/ISO-HDLC")
    page.results_table.cellWidget(row, 4).click()
    sim.check("复制 CRC-32 HEX", QApplication.clipboard().text(), "CBF43926")
    sim.screenshot("checksum-source")


def _fill_ccitt(dialog: CustomCrcDialog) -> None:
    dialog.width_combo.setCurrentIndex(dialog.width_combo.findData(16))
    dialog.poly_edit.setText("1021")
    dialog.init_edit.setText("FFFF")
    dialog.xorout_edit.setText("0000")
    dialog.refin_check.setChecked(False)
    dialog.refout_check.setChecked(False)


def chk_04_custom_crc(sim) -> None:
    page = sim.switch("checksum")
    page.custom_crc_button.click()
    sim.qapp.processEvents()
    dialog = page._crc_dialog
    sim.check_true("自定义 CRC 对话框打开", isinstance(dialog, CustomCrcDialog) and dialog.isVisible(), "对话框未打开")
    _fill_ccitt(dialog)
    dialog.input_edit.setPlainText(DATA.decode())
    sim.check("自定义 CRC16-CCITT 计算", dialog.run_compute(), 0x29B1)
    sim.check("自定义 CRC 结果 HEX", dialog.result_hex(), "29B1")
    dialog.copy_result()
    sim.check("自定义 CRC 复制", QApplication.clipboard().text(), "29B1")
    dialog.poly_edit.setText("102G")
    sim.check("非法多项式被拒绝", dialog.run_compute(), None)
    sim.check_in("非法多项式提示", "十六进制", dialog.error_label.text())
    sim.screenshot("checksum-custom-crc")
    dialog.close()
    page._crc_dialog = None
    sim.qapp.processEvents()


def chk_05_file_stream_cancel_missing(sim) -> None:
    page = sim.switch("checksum")
    payload = bytes(index % 251 for index in range(2 * 1024 * 1024 + 7))
    source = sim.tmp_path / "checksum.bin"
    source.write_bytes(payload)
    page.set_mode("file")
    page.file_picker.set_path(str(source))
    page.compute_button.click()
    sim.check_true(
        "文件校验完成",
        sim.wait(lambda: page.status_chip.state in ("ok", "error") and not page.has_active_work(), 20000),
        page.status_chip.text(),
    )
    sim.check("文件校验状态", page.status_chip.state, "ok")
    sim.check("文件校验进度", page.progress_bar.value(), 100)
    sim.check("文件 CRC-32 独立计算", _hex_for(page, "CRC-32/ISO-HDLC"), f"{zlib.crc32(payload) & 0xFFFFFFFF:08X}")
    sim.check("文件 Sum-8 独立计算", _hex_for(page, "Sum-8"), f"{sum(payload) & 0xFF:02X}")
    sim.artifact(source, "checksum-file.bin")

    big = sim.tmp_path / "checksum-cancel.bin"
    big.write_bytes(b"Z" * (8 * 1024 * 1024))
    page.set_mode("file")
    page.file_picker.set_path(str(big))
    page._start_file_compute(notify=False)
    page.cancel_button.click()
    sim.check_true("文件校验可取消", sim.wait(lambda: not page.has_active_work(), 3000), str(sim.thread_states()))
    sim.check_in("取消状态文案", "已取消", page.status_chip.text())

    page.set_mode("file")
    page.file_picker.set_path(str(sim.tmp_path / "not-there.bin"))
    page.compute_button.click()
    sim.check_true("缺失文件报错", sim.wait(lambda: page.status_chip.state == "error", 3000), page.status_chip.text())
    sim.check("缺失文件无结果", page.results_table.rowCount(), 0)
    sim.screenshot("checksum-file")


# ---- 编码转换 ---------------------------------------------------------


def cnv_01_base_tool(sim) -> None:
    page = sim.switch("convert")
    page.set_tool("base")
    base = page.tools["base"]
    base.input_edit.setText("255")
    sim.check("十进制 255 的十六进制", base.output_values()[16], "FF")
    sim.check("十进制 255 的二进制", base.output_values()[2], "11111111")
    base.signed_check.setChecked(True)
    base.input_edit.setText("-1")
    sim.check("32 位补码十六进制", base.output_values()[16], "FFFFFFFF")
    base.bits_combo.setCurrentIndex(base.bits_combo.findData(64))
    sim.check("64 位补码二进制", base.output_values()[2], "1" * 64)
    base.copy_buttons[16].click()
    sim.check("复制进制结果", QApplication.clipboard().text(), "FFFFFFFFFFFFFFFF")
    base.input_edit.setText("GG")
    sim.check_true("非法进制输入提示", bool(base.error_label.text()), "无错误提示")
    sim.screenshot("convert-base")


def cnv_02_data_tool(sim) -> None:
    page = sim.switch("convert")
    page.set_tool("data")
    tool = page.tools["data"]
    source = sim.tmp_path / "data-input.bin"
    source.write_bytes(b"AB\x00\xff")
    target = sim.tmp_path / "data-hex.txt"
    result = tool.convert(source, target, OP_BIN_TO_TEXT)
    sim.check("Bin→Text 输出路径", result, target)
    sim.check("Bin→Text 内容", target.read_text(encoding="utf-8"), "41 42 00 FF")

    hex_source = sim.tmp_path / "data-hex-input.txt"
    hex_source.write_text("C4 E3", encoding="utf-8")
    text_target = sim.tmp_path / "data-text.txt"
    tool.convert(hex_source, text_target, OP_HEX_TO_TEXT, "gbk")
    sim.check("Hex→Text 编码", text_target.read_text(encoding="gbk"), "你")

    bin_target = sim.tmp_path / "data.bin"
    tool.convert(hex_source, bin_target, OP_HEX_TO_BIN)
    sim.check("Hex→Bin 内容", bin_target.read_bytes(), b"\xc4\xe3")
    sim.artifact(target, "data-hex.txt")
    sim.artifact(bin_target, "data.bin")
    sim.screenshot("convert-data")


def cnv_03_seven_segment(sim) -> None:
    page = sim.switch("convert")
    page.set_tool("sevenseg")
    tool = page.tools["sevenseg"]
    tool.digit_edit.setText("A")
    sim.check("七段 A 共阴", tool.common_cathode_code(), 0x77)
    sim.check("七段 A 共阳", tool.common_anode_code(), 0x88)
    tool.digit_edit.setText("G")
    sim.check_true("七段非法字符提示", bool(tool.error_label.text()), "无错误提示")
    tool.digit_edit.setText("9")
    tool.copy_buttons["cathode_hex"].click()
    sim.check("七段复制", QApplication.clipboard().text(), "6F")
    sim.screenshot("convert-sevenseg")


def cnv_04_palette(sim) -> None:
    page = sim.switch("convert")
    page.set_tool("palette")
    tool = page.tools["palette"]
    tool.set_rgb(255, 0, 0)
    sim.check("调色板 HEX", tool.output_values()["hex"], "#FF0000")
    sim.check("RGB565", tool.output_values()["rgb565"], "0xF800")
    sim.check("RGB332", tool.output_values()["rgb332"], "0xE0")
    tool.hex_edit.setText("GG")
    sim.check_true("非法颜色提示", bool(tool.error_label.text()), "无错误提示")
    tool.hex_edit.setText("#00FF00")
    sim.check("绿色 RGB565", tool.output_values()["rgb565"], "0x07E0")
    sim.screenshot("convert-palette")


def cnv_05_gif(sim) -> None:
    page = sim.switch("convert")
    page.set_tool("gif")
    tool = page.tools["gif"]
    source = assets.make_gif(sim.tmp_path / "clip.gif", frames=3, size=(8, 8))
    frames = tool.load(source)
    sim.check("GIF 帧数", len(frames), 3)
    sim.check("GIF 帧信息", tool.frame_count(), 3)
    sim.check_true("GIF 播放", tool.play() is True, "播放失败")
    tool.pause()
    out = sim.tmp_path / "gif-frames"
    written = tool.export(out, 4, 4)
    sim.check("GIF 导出帧数", len(written), 3)
    sim.check_true("GIF 导出 BMP 可读", all(path.is_file() and path.suffix == ".bmp" for path in written), str(written))
    for path in written:
        sim.artifact(path, path.name)
    sim.screenshot("convert-gif")


def cnv_06_audio(sim) -> None:
    page = sim.switch("convert")
    page.set_tool("audio")
    tool = page.tools["audio"]
    source = assets.make_wav(sim.tmp_path / "tone.wav", duration_s=0.1)
    audio = tool.load(source)
    sim.check("WAV 采样率", audio.sample_rate, 8000)
    c_path = sim.tmp_path / "tone.c"
    h_path = sim.tmp_path / "tone.h"
    result_c, result_h = tool.export_c(c_path, h_path)
    sim.check("音频 C 输出", result_c, c_path)
    sim.check_true("音频 C 文件存在", c_path.is_file() and "const unsigned char" in c_path.read_text(encoding="utf-8"), "C 文件无效")
    sim.check_true("音频 H 文件存在", h_path.is_file() and "extern const unsigned char" in h_path.read_text(encoding="utf-8"), "H 文件无效")
    sim.artifact(c_path, "tone.c")
    sim.artifact(h_path, "tone.h")
    sim.screenshot("convert-audio")


def cnv_07_qr(sim) -> None:
    page = sim.switch("convert")
    page.set_tool("qr")
    tool = page.tools["qr"]
    tool.set_text("https://example.com/porthelper")
    sim.check_true("二维码预览生成", tool.qr_image() is not None, "无二维码图像")
    tool.set_colors("#FF0000", "#0000FF")
    sim.check("二维码前景色", tool.fg_color(), "#FF0000")
    target = sim.tmp_path / "qr.png"
    sim.check("二维码保存", tool.save(target), target)
    sim.check_true("二维码 PNG 可读", target.is_file() and target.stat().st_size > 0, str(target))
    sim.artifact(target, "qr.png")
    sim.screenshot("convert-qr")


def cnv_08_tool_switch(sim) -> None:
    page = sim.switch("convert")
    routes = ("base", "data", "sevenseg", "palette", "gif", "audio", "qr")
    for route in routes:
        page.set_tool(route)
        sim.check(f"切换到 {route}", page.current_tool_route(), route)
    sim.check("工具切换持久化", sim.settings.get("convert.last_tool"), "qr")
    sim.screenshot("convert-tools")


# ---- 图片取模 ---------------------------------------------------------


def img_01_load(sim) -> None:
    page = sim.switch("image")
    source = assets.make_png(sim.tmp_path / "image.png", size=(4, 4))
    image = page.load(source)
    sim.check("图片尺寸", image.size, (4, 4))
    sim.check("图片路径", page.source_file, source)
    sim.check_true("原图预览非空", not page.original_preview_label.pixmap().isNull(), "原图预览为空")
    sim.check_true("输出预览非空", not page.output_preview_label.pixmap().isNull(), "输出预览为空")
    sim.check_true("代码预览非空", bool(page.code_preview.toPlainText().strip()), "代码为空")
    folder = assets.make_image_folder(sim.tmp_path / "images", count=3)
    files = page.load_folder(folder)
    sim.check("文件夹图片数", len(files), 3)
    sim.check("页面文件列表", len(page.files), 3)
    sim.artifact(source, "image.png")
    sim.screenshot("image-load")


def img_02_formats(sim) -> None:
    page = sim.switch("image")
    source = assets.make_png(sim.tmp_path / "format.png", size=(4, 4))
    page.load(source)
    page.set_params(ImageParams(output_type="bin", color_depth=1, width=4, height=4))
    sim.check("1 位 BIN 数据长度", len(page.last_result.data), 2)
    page.set_params(ImageParams(output_type="c", color_depth=16, scan="vertical", direction="btt"))
    sim.check_true("C 代码头", "const unsigned char" in page.code_preview.toPlainText(), "C 代码缺少数组声明")
    page.set_params(ImageParams(output_type="lvgl8", color_depth=16))
    sim.check_in("LVGL8 头", "lv_img_dsc_t", page.code_preview.toPlainText())
    page.set_params(ImageParams(output_type="lvgl9", color_depth=16))
    sim.check_in("LVGL9 头", "lv_image_dsc_t", page.code_preview.toPlainText())
    sim.screenshot("image-formats")


def img_03_params(sim) -> None:
    page = sim.switch("image")
    source = assets.make_png(sim.tmp_path / "params.png", size=(4, 2))
    page.load(source)
    sim.check("比例锁定默认", page.lock_ratio_check.isChecked(), True)
    page.width_spin.setValue(2)
    sim.check("宽度联动高度", page.height_spin.value(), 1)
    page.lock_ratio_check.setChecked(False)
    page.width_spin.setValue(3)
    sim.check("关闭比例锁定", page.height_spin.value(), 1)
    page.set_params(ImageParams(output_type="bin", color_depth=1, invert=True, background=(255, 0, 0)))
    sim.check_true("反色背景仍生成数据", len(page.last_result.data) > 0, "无输出数据")
    sim.screenshot("image-params")


def img_04_export(sim) -> None:
    page = sim.switch("image")
    source = assets.make_png(sim.tmp_path / "export.png", size=(4, 2))
    page.load(source)
    out = sim.tmp_path / "image-out"
    page.set_output_dir(out)
    page.set_output_name("icon")
    written = page.export_current()
    sim.check("单张导出文件数", len(written), 1)
    sim.check("单张导出文件名", written[0].name, "icon.c")
    sim.check_true("单张导出内容", "const unsigned char icon[]" in written[0].read_text(encoding="utf-8"), "数组名错误")
    sim.artifact(written[0], "icon.c")

    folder = assets.make_image_folder(sim.tmp_path / "batch-images", count=3)
    page.load_folder(folder)
    result = page.batch_export(output_dir=sim.tmp_path / "batch-out")
    sim.check("批量导出无错误", result.errors, {})
    names = sorted(path.name for outputs in result.values() for path in outputs)
    sim.check("批量导出文件", names, ["frame-0.c", "frame-1.c", "frame-2.c"])
    for outputs in result.values():
        for path in outputs:
            sim.artifact(path, path.name)
    sim.screenshot("image-export")


def img_05_errors_and_config(sim) -> None:
    page = sim.switch("image")
    try:
        page.load(sim.tmp_path / "missing.png")
        sim.check_true("缺失图片应报错", False, "未抛出异常")
    except ValueError as exc:
        sim.check_in("缺失图片错误", "找不到", str(exc))
    sim.check_true("错误标签可见", bool(page.error_label.text()), "无错误提示")

    page.set_params(ImageParams(output_type="c", direction="rtl", byte_order="big", width=10, height=5, invert=True))
    page.lock_ratio_check.setChecked(False)
    page.save_settings()
    sim.check("图片方向持久化", sim.settings.get("image.direction"), "rtl")
    sim.check("图片字节序持久化", sim.settings.get("image.byte_order"), "big")
    sim.check("图片宽度持久化", sim.settings.get("image.width"), 10)
    sim.check("图片反色持久化", sim.settings.get("image.invert"), True)
    sim.screenshot("image-errors")


# ---- 点阵生成 ---------------------------------------------------------


def _require_font(sim) -> None:
    if sim.font_path is None:
        pytest.skip("系统字体不可用，文本点阵场景跳过（BMP 场景仍执行）")


def dmx_01_text_matrix(sim) -> None:
    _require_font(sim)
    page = sim.switch("dotmatrix")
    page.set_text("A")
    page.set_size(16)
    page.set_params(FontParams(size=16, page_chars=8))
    sim.check_true("点阵页数", page.page_count() >= 1, str(page.page_count()))
    current = page.current_page()
    sim.check("点阵行数", current.rows, 16)
    sim.check_true("点阵列数", current.cols > 0, str(current.cols))
    sim.check_in("点阵代码数组", "const unsigned char", page.code_preview.toPlainText())
    sim.check_true("页码标签", "第 1 /" in page.page_label.text(), page.page_label.text())
    sim.screenshot("dotmatrix-text")


def dmx_02_bmp(sim) -> None:
    page = sim.switch("dotmatrix")
    source = assets.make_bmp(sim.tmp_path / "matrix.bmp", size=(2, 2), pixels=(0, 255, 128, 64))
    page.load_bmp(source)
    sim.check("BMP 模式", page.mode(), "bmp")
    sim.check("BMP 阈值矩阵", page.current_page().cells, [[True, False], [False, True]])
    page.invert_check.setChecked(True)
    sim.check("BMP 反色矩阵", page.current_page().cells, [[False, True], [True, False]])
    page.invert_check.setChecked(False)
    sim.screenshot("dotmatrix-bmp")


def dmx_03_edit_and_navigation(sim) -> None:
    _require_font(sim)
    page = sim.switch("dotmatrix")
    page.set_text("AAAAAAAAAA")
    page.set_params(FontParams(size=16, page_chars=1))
    sim.check_true("长文本分页", page.page_count() > 1, str(page.page_count()))
    sim.check("初始页码", page.current_page_index(), 0)
    page.next_button.click()
    sim.check("下一页", page.current_page_index(), 1)
    page.prev_button.click()
    sim.check("上一页", page.current_page_index(), 0)
    sim.check_true("单元格编辑", page.matrix_view.cellToggled(0, 0) is True, "编辑失败")
    sim.check("编辑后脏标记", page._dirty, True)
    page.reset_button.click()
    sim.check("重置后脏标记", page._dirty, False)
    text = page.copy_code()
    sim.check("点阵复制代码", QApplication.clipboard().text(), text)
    sim.screenshot("dotmatrix-edit")


def dmx_04_export(sim) -> None:
    _require_font(sim)
    page = sim.switch("dotmatrix")
    page.set_text("A")
    page.set_params(FontParams(size=16, page_chars=8))
    out = sim.tmp_path / "matrix-out"
    page.set_output_dir(out)
    page.set_output_name("font_a")
    c_path = page.export_c()
    bin_path = page.export_bin()
    sim.check("点阵 C 导出文件名", c_path.name, "font_a.c")
    sim.check("点阵 BIN 导出文件名", bin_path.name, "font_a.bin")
    sim.check_true("点阵 C 文件非空", c_path.is_file() and c_path.stat().st_size > 0, str(c_path))
    sim.check_true("点阵 BIN 文件非空", bin_path.is_file() and bin_path.stat().st_size > 0, str(bin_path))
    sim.artifact(c_path, "font_a.c")
    sim.artifact(bin_path, "font_a.bin")

    folder = sim.tmp_path / "bmp-folder"
    folder.mkdir(parents=True, exist_ok=True)
    assets.make_bmp(folder / "one.bmp", size=(2, 2))
    assets.make_bmp(folder / "two.bmp", size=(2, 2), pixels=(0, 255, 255, 0))
    files = page.load_bmp_folder(folder)
    result = page.batch_export(output_dir=sim.tmp_path / "bmp-out")
    sim.check("点阵批量文件数", len(files), 2)
    sim.check("点阵批量无错误", result.errors, {})
    names = sorted(path.name for outputs in result.values() for path in outputs)
    sim.check("点阵批量输出", names, ["one.c", "two.c"])
    sim.screenshot("dotmatrix-export")


def dmx_05_errors_and_config(sim) -> None:
    page = sim.switch("dotmatrix")
    try:
        page.load_bmp(sim.tmp_path / "missing.bmp")
        sim.check_true("缺失 BMP 应报错", False, "未抛出异常")
    except ValueError as exc:
        sim.check_in("缺失 BMP 错误", "找不到", str(exc))
    sim.check_true("BMP 错误标签", bool(page.error_label.text()), "无错误提示")
    page.set_params(FontParams(size=24, bold=True, italic=True, threshold=100, invert=True, scan="vertical", direction="btt", bit_order="lsb", page_chars=2))
    page.set_dot_color((255, 0, 0))
    page.set_background_color((0, 0, 255))
    page.save_settings()
    sim.check("点阵字号持久化", sim.settings.get("dotmatrix.size"), 24)
    sim.check("点阵扫描持久化", sim.settings.get("dotmatrix.scan"), "vertical")
    sim.check("点阵位序持久化", sim.settings.get("dotmatrix.bit_order"), "lsb")
    sim.check("点阵颜色持久化", sim.settings.get("dotmatrix.dot_color"), "#FF0000")
    sim.screenshot("dotmatrix-errors")


# ---- C51 向导 ---------------------------------------------------------


def c51_01_default_code(sim) -> None:
    page = sim.switch("c51")
    page.generate()
    text = page.code_preview.toPlainText()
    sim.check_true("C51 代码非空", bool(text.strip()), "代码为空")
    sim.check_in("C51 main 函数", "void main", text)
    sim.check_in("C51 寄存器", "TMOD", text)
    sim.check_true("C51 配置可读", page.current_config() is not None, "配置为空")
    sim.screenshot("c51-default")


def c51_02_timer_and_ports(sim) -> None:
    page = sim.switch("c51")
    page.crystal_spin.setValue(12.0)
    page.timer_mode_combos[0].setCurrentIndex(1)
    page.timer_time_spins[0].setValue(50_000)
    page.timer_run_checks[0].setChecked(True)
    page.generate()
    text = page.code_preview.toPlainText()
    sim.check_in("定时器 0 高字节", "TH0 = 0x3C", text)
    sim.check_in("定时器 0 低字节", "TL0 = 0xB0", text)
    sim.check_in("定时器标签", "0x3C", page.timer_labels[0].text())
    page.port_checks["P1"][0].setChecked(True)
    page.port_checks["P1"][2].setChecked(True)
    sim.check("端口 P1 值", page.port_value("P1"), 0x05)
    sim.check_in("端口代码", "P1 = 0x05", page.code_preview.toPlainText())
    page.crystal_spin.setValue(12.0)
    page.timer_time_spins[0].setValue(100_000)
    page.generate()
    sim.check_true("定时器溢出警告", "超过" in page.warning_view.toPlainText(), page.warning_view.toPlainText())
    sim.screenshot("c51-timer")


def c51_03_serial(sim) -> None:
    page = sim.switch("c51")
    page.crystal_spin.setValue(11.0592)
    page.serial_mode_combo.setCurrentIndex(1)
    page.baud_spin.setValue(9600)
    page.smod_check.setChecked(False)
    page.generate()
    sim.check_in("串口波特率标签", "9600", page.baud_label.text())
    sim.check_in("串口 TH1", "TH1=0xFD", page.baud_label.text())
    page.baud_spin.setValue(115200)
    page.smod_check.setChecked(True)
    page.generate()
    sim.check_true("高波特率误差警告", "误差" in page.warning_view.toPlainText(), page.warning_view.toPlainText())
    sim.screenshot("c51-serial")


def c51_04_interrupts_and_exports(sim) -> None:
    page = sim.switch("c51")
    page.interrupt_checks["et0"].setChecked(True)
    page.interrupt_checks["es"].setChecked(True)
    page.priority_combos["et0"].setCurrentIndex(1)
    page.generate()
    text = page.code_preview.toPlainText()
    sim.check_in("定时器 0 中断", "void timer0_isr(void) interrupt 1", text)
    sim.check_in("串口中断", "void uart_isr(void) interrupt 4", text)
    page.interrupt_checks["ex0"].setChecked(True)
    page.ext0_trigger_combo.setCurrentIndex(1)
    page.generate()
    sim.check_in("外部中断 0", "void ext0_isr(void) interrupt 0", page.code_preview.toPlainText())

    out = sim.tmp_path / "c51-out"
    page.set_output_dir(out)
    written = page.export_c()
    sim.check("C51 C 导出路径", written, out / "Main.c")
    sim.check_true("C51 C 文件存在", written.is_file(), str(written))
    keil = page.export_keil(output_dir=sim.tmp_path / "keil")
    sim.check("Keil 工程文件", [path.name for path in keil], ["Main.c", "M51Project.Uv2", "M51Project.Opt", "STARTUP.A51"])
    sim.check_true("Keil 工程文件全部存在", all(path.is_file() for path in keil), str(keil))
    sim.check_in("Keil 工程引用 Main.c", "Main.c", keil[1].read_text(encoding="ascii"))
    for path in keil:
        sim.artifact(path, path.name)
    sim.screenshot("c51-export")


def c51_05_errors_copy_and_config(sim) -> None:
    page = sim.switch("c51")
    _c51_config(page)
    page.generate()
    text = page.copy_code()
    sim.check("C51 复制代码", QApplication.clipboard().text(), text)
    page.copy_button.click()
    sim.check("C51 复制内容仍一致", QApplication.clipboard().text(), text)

    blocker = sim.tmp_path / "blocker.txt"
    blocker.write_text("x", encoding="utf-8")
    try:
        page.export_keil(output_dir=blocker / "sub")
        sim.check_true("不可写目录应报错", False, "未抛出异常")
    except OSError:
        pass
    sim.check_true("不可写目录错误标签", page.error_label.isVisible(), "错误标签不可见")

    page.crystal_spin.setValue(24.0)
    page.double_speed_switch.setChecked(True)
    page.set_port_value("P2", 0xA5)
    page.save_settings()
    sim.check("C51 晶振持久化", sim.settings.get("c51.crystal_mhz"), 24.0)
    sim.check("C51 端口持久化", sim.settings.get("c51.ports").get("P2"), 0xA5)
    sim.screenshot("c51-errors")


def _c51_config(page) -> None:
    page.crystal_spin.setValue(12.0)
    page.timer_mode_combos[0].setCurrentIndex(1)
    page.timer_time_spins[0].setValue(50_000)
    page.timer_run_checks[0].setChecked(True)
    page.generate()

"""图片取模页离屏测试：载入、预览、参数同步、导出与批量转换。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from PyQt6.QtWidgets import QApplication, QLabel

from app.core.imaging import ImageParams
from app.core.settings import Settings
from app.services.paths import resolve_paths
from app.ui.pages import image_page as image_page_module
from app.ui.pages.image_page import ImagePage

_CREATED_PAGES: list = []


@pytest.fixture(autouse=True)
def _dispose_pages(qapp):
    yield
    from PyQt6.QtCore import QCoreApplication, QEvent

    while _CREATED_PAGES:
        page = _CREATED_PAGES.pop()
        try:
            page.close()
            page.deleteLater()
        except RuntimeError:
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
        image_page_module, "show_error", lambda *args: errors.append(args)
    )
    monkeypatch.setattr(
        image_page_module, "show_success", lambda *args: successes.append(args)
    )
    page = ImagePage(settings, paths)
    _CREATED_PAGES.append(page)
    page.show()
    return page, settings, paths, errors, successes


def _make_png(path: Path, size=(4, 2)) -> Path:
    image = Image.new("RGB", size)
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
    image.putdata([colors[index % len(colors)] for index in range(size[0] * size[1])])
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


# ---- 基础 ---------------------------------------------------------------


def test_page_metadata_and_lifecycle(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    assert page.objectName() == "image"
    assert page.title_label.text() == "图片取模"
    assert "位图" in page.subtitle_label.text()
    assert isinstance(page.original_preview_label, QLabel)
    assert isinstance(page.output_preview_label, QLabel)
    assert page.has_active_work() is False
    page.stop_workers()
    assert QApplication.activeModalWidget() is None


def test_load_file_shows_preview_and_code(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    source = _make_png(tmp_path / "sample.png")

    image = page.load(source)
    assert image.size == (4, 2)
    assert page.image is image
    assert page.source_file == source
    assert page.original_preview_label.pixmap().isNull() is False
    assert "4×2" in page.original_info_label.text()
    assert page.output_preview_label.pixmap().isNull() is False
    assert page.last_result is not None
    assert (page.last_result.width, page.last_result.height) == (4, 2)
    assert "16 位" in page.output_info_label.text()
    assert "const unsigned char sample[]" in page.code_preview.toPlainText()
    assert "4x2" in page.code_preview.toPlainText()
    assert page.error_label.isVisible() is False


def test_parameter_changes_refresh_preview_and_code(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.load(_make_png(tmp_path / "sample.png"))

    page.set_params(
        ImageParams(output_type="bin", color_depth=1, width=2, height=1)
    )
    assert page.last_result is not None
    assert page.last_result.preview.size == (2, 1)
    assert len(page.last_result.data) == 1
    assert "1 位" in page.output_info_label.text()
    assert "BIN 原始数据" in page.code_preview.toPlainText()
    assert page.last_result.text is None

    page.set_params(ImageParams(output_type="lvgl9", color_depth=16))
    assert "lv_image_dsc_t" in page.code_preview.toPlainText()
    assert "LV_COLOR_FORMAT_RGB565" in page.code_preview.toPlainText()


def test_invalid_path_writes_inline_error_without_modal(
    qapp, tmp_path, monkeypatch
) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    missing = tmp_path / "missing.png"

    with pytest.raises(ValueError, match="找不到|无法"):
        page.load(missing)
    assert page.image is None
    assert page.error_label.text()
    assert QApplication.activeModalWidget() is None

    page.input_picker.set_path(str(missing))
    assert page.error_label.text()
    assert QApplication.activeModalWidget() is None


def test_width_height_lock_ratio(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.load(_make_png(tmp_path / "sample.png", size=(4, 2)))
    assert page.lock_ratio_check.isChecked() is True

    page.width_spin.setValue(2)
    assert page.height_spin.value() == 1
    page.height_spin.setValue(4)
    assert page.width_spin.value() == 8

    page.lock_ratio_check.setChecked(False)
    page.width_spin.setValue(3)
    assert page.height_spin.value() == 4


def test_scan_and_lvgl_controls_stay_consistent(
    qapp, tmp_path, monkeypatch
) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.load(_make_png(tmp_path / "scan.png"))
    page.set_params(ImageParams(output_type="c", scan="vertical", direction="btt"))
    assert page.scan() == "vertical"
    assert page.direction() == "btt"
    assert page.direction_combo.count() == 4

    page.set_params(ImageParams(output_type="lvgl9", scan="vertical", direction="ttb"))
    assert page.scan() == "vertical"
    assert page.direction() == "ttb"
    assert "lv_image_dsc_t" in page.code_preview.toPlainText()

    page.set_params(ImageParams(output_type="c", scan="horizontal", direction="ltr"))
    assert page.scan() == "horizontal"
    assert page.direction() == "ltr"

    # 直接选择纵向方向时扫描方式自动切换，方向选项始终完整。
    page.direction_combo.setCurrentIndex(page.direction_combo.findData("ttb"))
    assert page.direction() == "ttb"
    assert page.scan() == "vertical"
    page.direction_combo.setCurrentIndex(page.direction_combo.findData("rtl"))
    assert page.scan() == "horizontal"


# ---- 导出 ---------------------------------------------------------------


def test_export_current_writes_file(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, _successes = _make_page(tmp_path, monkeypatch)
    source = _make_png(tmp_path / "sample.png")
    page.load(source)
    output_dir = tmp_path / "output"
    page.set_output_dir(output_dir)
    page.set_output_name("my_array")

    written = page.export_current()
    assert len(written) == 1
    assert written[0].name == "my_array.c"
    assert written[0].is_file()
    text = written[0].read_text(encoding="utf-8")
    assert "const unsigned char my_array[]" in text
    assert page.progress_bar.value() == 100
    assert errors == []
    assert QApplication.activeModalWidget() is None


def test_export_button_reports_errors_without_modal(
    qapp, tmp_path, monkeypatch
) -> None:
    page, _settings, _paths, errors, _successes = _make_page(tmp_path, monkeypatch)
    page.convert_button.click()
    assert errors
    assert QApplication.activeModalWidget() is None


def test_batch_export_folder_writes_files(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, _successes = _make_page(tmp_path, monkeypatch)
    folder = tmp_path / "images"
    _make_png(folder / "first.png")
    _make_png(folder / "second.png")

    files = page.load_folder(folder)
    assert len(files) == 2
    assert len(page.files) == 2
    assert page.folder_picker.path() == str(folder)
    output_dir = tmp_path / "batch_out"

    result = page.batch_export(output_dir=output_dir)
    assert result.errors == {}
    assert set(result) == {str(path) for path in files}
    names = sorted(path.name for outputs in result.values() for path in outputs)
    assert names == ["first.c", "second.c"]
    assert page.progress_bar.value() == 100
    assert "成功 2" in page.status_label.text()
    assert errors == []


def test_batch_export_continues_after_bad_file(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, _successes = _make_page(tmp_path, monkeypatch)
    folder = tmp_path / "images"
    good = _make_png(folder / "good.png")
    bad = _make_png(folder / "bad.png")
    page.load_folder(folder)
    bad.unlink()

    result = page.batch_export(output_dir=tmp_path / "out")
    assert result.errors
    assert result[str(bad)] == []
    assert result[str(good)][0].is_file()
    assert page.error_label.isVisible() is True
    assert QApplication.activeModalWidget() is None
    assert errors == []


def test_batch_button_reports_partial_errors(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, _successes = _make_page(tmp_path, monkeypatch)
    folder = tmp_path / "images"
    _make_png(folder / "good.png")
    bad = _make_png(folder / "bad.png")
    page.load_folder(folder)
    page.set_output_dir(tmp_path / "out")
    bad.unlink()

    page.batch_button.click()
    assert errors
    assert QApplication.activeModalWidget() is None


# ---- 配置 ---------------------------------------------------------------


def test_parameters_are_persisted(qapp, tmp_path, monkeypatch) -> None:
    page, settings, paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.set_params(
        ImageParams(
            output_type="c",
            scan="horizontal",
            direction="rtl",
            color_depth=16,
            byte_order="big",
            width=10,
            height=5,
            background=(255, 0, 0),
            invert=True,
        )
    )
    page.lock_ratio_check.setChecked(False)
    page.set_output_dir(tmp_path / "saved_out")
    page.set_output_name("saved_name")
    page.save_settings()

    reloaded = Settings(paths.config_file)
    reloaded.load()
    assert reloaded.get("image.output_type") == "c"
    assert reloaded.get("image.direction") == "rtl"
    assert reloaded.get("image.byte_order") == "big"
    assert reloaded.get("image.width") == 10
    assert reloaded.get("image.height") == 5
    assert reloaded.get("image.background") == "#FF0000"
    assert reloaded.get("image.invert") is True
    assert reloaded.get("image.lock_ratio") is False
    assert str(reloaded.get("image.output_dir")).endswith("saved_out")
    assert reloaded.get("image.output_name") == "saved_name"

    second = ImagePage(reloaded, paths)
    _CREATED_PAGES.append(second)
    try:
        assert second.direction() == "rtl"
        assert second.byte_order() == "big"
        assert second.background() == (255, 0, 0)
        assert second.width_spin.value() == 10
        assert second.height_spin.value() == 5
        assert second.lock_ratio_check.isChecked() is False
        assert second.invert_check.isChecked() is True
        assert second.name_edit.text() == "saved_name"
    finally:
        second.close()

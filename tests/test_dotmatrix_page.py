"""点阵生成页与 MatrixView 的离屏测试：渲染、分页、编辑、导出与配置。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from app.core.fontmatrix import (
    FontParams,
    Matrix,
    matrix_to_bytes,
    matrix_to_c,
    matrices_to_bytes,
)
from app.core.settings import Settings
from app.services.paths import resolve_paths
from app.ui.pages import dotmatrix_page as dotmatrix_page_module
from app.ui.pages.dotmatrix_page import DotMatrixPage, resolve_font_file
from app.ui.widgets.matrix_view import ZOOM_MAX, ZOOM_MIN, MatrixView

_CREATED_WIDGETS: list = []


@pytest.fixture(autouse=True)
def _dispose_widgets(qapp):
    yield
    from PyQt6.QtCore import QCoreApplication, QEvent

    while _CREATED_WIDGETS:
        widget = _CREATED_WIDGETS.pop()
        try:
            widget.close()
            widget.deleteLater()
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
    monkeypatch.setattr(dotmatrix_page_module, "show_error", lambda *args: errors.append(args))
    monkeypatch.setattr(
        dotmatrix_page_module, "show_success", lambda *args: successes.append(args)
    )
    page = DotMatrixPage(settings, paths)
    _CREATED_WIDGETS.append(page)
    page.show()
    return page, settings, paths, errors, successes


def _make_bmp(path: Path, pixels=(0, 255, 128, 64), size=(2, 2)) -> Path:
    image = Image.new("L", size)
    image.putdata(list(pixels))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _make_view(matrix: Matrix) -> MatrixView:
    view = MatrixView()
    view.set_matrix(matrix)
    view.resize(view.sizeHint())
    view.show()
    _CREATED_WIDGETS.append(view)
    return view


# ---- 页面基础 -----------------------------------------------------------


def test_page_metadata_and_lifecycle(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    assert page.objectName() == "dotmatrix"
    assert page.title_label.text() == "点阵生成"
    assert "点阵" in page.subtitle_label.text()
    assert page.has_active_work() is False
    page.stop_workers()
    assert page.findChildren(MatrixView)
    assert QApplication.activeModalWidget() is None


def test_render_text_shows_preview_and_code(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, _successes = _make_page(tmp_path, monkeypatch)
    page.set_text("A")
    page.set_size(16)
    page.set_params(FontParams(size=16, page_chars=8))

    assert page.page_count() == 1
    current = page.current_page()
    assert current is not None
    assert current.rows == 16
    assert current.cols > 0
    assert page.matrix_view.matrix() is current
    assert page.matrix_view.sizeHint().width() > 0
    assert page.page_label.text() == "第 1 / 1 页"
    assert "const unsigned char" in page.code_preview.toPlainText()
    assert f"{current.cols} x {current.rows}" in page.code_preview.toPlainText()
    assert page.error_label.isVisible() is False
    assert errors == []


def test_page_navigation_for_long_text(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.set_text("AAAAAAAAAA")
    page.set_params(FontParams(size=16, page_chars=1))

    assert page.page_count() > 1
    assert page.current_page_index() == 0
    page.next_button.click()
    assert page.current_page_index() == 1
    assert "第 2 /" in page.page_label.text()
    page.prev_button.click()
    assert page.current_page_index() == 0
    page.set_page(999)
    assert page.current_page_index() == page.page_count() - 1
    page.set_page(-5)
    assert page.current_page_index() == 0
    assert page.next_button.isEnabled() is True


def test_toggle_cell_changes_export(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.set_text("A")
    page.set_params(FontParams(size=16, page_chars=8))
    before = matrix_to_bytes(page.current_page(), page.params())

    assert page.matrix_view.cellToggled(0, 0) is True
    assert page._dirty is True
    assert matrix_to_bytes(page.current_page(), page.params()) != before
    assert "已编辑" in page.status_label.text()

    target = tmp_path / "edited.c"
    page.export_c(target=target)
    expected = matrix_to_c(page.pages(), page.params(), "edited")
    assert target.read_text(encoding="utf-8") == expected


def test_reset_from_text_discards_manual_edits(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.set_text("A")
    page.matrix_view.cellToggled(0, 0)
    assert page._dirty is True
    page.reset_button.click()
    assert page._dirty is False
    assert "已编辑" not in page.preview_info_label.text()


# ---- 导出 ---------------------------------------------------------------


def test_export_c_and_bin_to_temp(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, _successes = _make_page(tmp_path, monkeypatch)
    page.set_text("A")
    output_dir = tmp_path / "out"
    page.set_output_dir(output_dir)
    page.set_output_name("font_test")

    written = page.export_c()
    assert written == output_dir / "font_test.c"
    assert written.is_file()
    text = written.read_text(encoding="utf-8")
    assert "const unsigned char font_test[]" in text
    assert "扫描方式" in text and "位序" in text
    assert page.progress_bar.value() == 100

    binary = page.export_bin()
    assert binary.name == "font_test.bin"
    assert binary.read_bytes() == matrices_to_bytes(page.pages(), page.params())

    page.set_output_name("")
    explicit = page.export_c(target=tmp_path / "custom.c")
    assert "const unsigned char custom[]" in explicit.read_text(encoding="utf-8")
    assert errors == []
    assert QApplication.activeModalWidget() is None


def test_export_without_data_reports_inline_error(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.set_text("")
    with pytest.raises(ValueError, match="没有可导出"):
        page.export_c(output_dir=tmp_path)
    assert page.error_label.isVisible() is True
    assert QApplication.activeModalWidget() is None


def test_copy_code_to_clipboard(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.set_text("A")
    text = page.copy_code()
    assert QApplication.clipboard().text() == text
    assert "const unsigned char" in text


# ---- BMP ----------------------------------------------------------------


def test_load_bmp_threshold_and_invert(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    source = _make_bmp(tmp_path / "sample.bmp")
    page.load_bmp(source)

    assert page.mode() == "bmp"
    assert page.bmp_picker.path() == str(source)
    assert page.page_count() == 1
    assert page.current_page().cells == [[True, False], [False, True]]
    assert page.bmp_info_label.text().startswith("BMP：sample.bmp")

    page.invert_check.setChecked(True)
    assert page.current_page().cells == [[False, True], [True, False]]


def test_load_bmp_missing_shows_inline_error(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="找不到"):
        page.load_bmp(tmp_path / "missing.bmp")
    assert page.error_label.isVisible() is True
    assert page.page_count() == 0
    assert QApplication.activeModalWidget() is None


def test_bmp_folder_batch_exports_one_c_per_file(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, _successes = _make_page(tmp_path, monkeypatch)
    folder = tmp_path / "bmp"
    _make_bmp(folder / "first.bmp")
    _make_bmp(folder / "second.bmp", pixels=(255, 0, 64, 128))

    files = page.load_bmp_folder(folder)
    assert len(files) == 2
    assert page.page_count() == 1
    assert page.folder_picker.path() == str(folder)
    assert "共 2 张" in page.bmp_info_label.text()

    result = page.batch_export(output_dir=tmp_path / "batch_out")
    assert result.errors == {}
    assert set(result) == {str(path) for path in files}
    names = sorted(path.name for outputs in result.values() for path in outputs)
    assert names == ["first.c", "second.c"]
    assert page.progress_bar.value() == 100
    assert "写出 2" in page.status_label.text()
    assert errors == []
    assert QApplication.activeModalWidget() is None


def test_batch_single_bmp_file(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    source = _make_bmp(tmp_path / "one.bmp")
    page.load_bmp(source)
    assert page.folder_picker.path() == ""
    assert page.batch_files() == [source]

    result = page.batch_export(output_dir=tmp_path / "single_out")
    assert result.errors == {}
    assert result[str(source)][0].name == "one.c"


def test_batch_continues_after_bad_file(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    folder = tmp_path / "bmp"
    good = _make_bmp(folder / "good.bmp")
    bad = _make_bmp(folder / "bad.bmp")
    page.load_bmp_folder(folder)
    bad.unlink()

    result = page.batch_export(output_dir=tmp_path / "out")
    assert result.errors
    assert result[str(bad)] == []
    assert result[str(good)][0].is_file()
    assert page.error_label.isVisible() is True


def test_mode_switch_bmp_without_file_clears_preview(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.set_mode("bmp")
    assert page.mode() == "bmp"
    assert page.page_count() == 0
    assert page.matrix_view.matrix() is None
    assert page.code_preview.toPlainText() == ""
    page.set_mode("text")
    assert page.mode() == "text"
    assert page.page_count() >= 1


# ---- 配置 ---------------------------------------------------------------


def test_parameters_are_persisted(qapp, tmp_path, monkeypatch) -> None:
    page, settings, paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.set_text("AB")
    page.set_params(
        FontParams(
            size=24,
            bold=True,
            italic=True,
            threshold=100,
            invert=True,
            scan="vertical",
            direction="btt",
            bit_order="lsb",
            page_chars=2,
        )
    )
    page.set_dot_color((255, 0, 0))
    page.set_background_color((0, 0, 255))
    page.grid_check.setChecked(False)
    page.zoom_spin.setValue(18)
    page.set_output_dir(tmp_path / "saved_out")
    page.set_output_name("saved_font")
    page.save_settings()

    reloaded = Settings(paths.config_file)
    reloaded.load()
    assert reloaded.get("dotmatrix.size") == 24
    assert reloaded.get("dotmatrix.bold") is True
    assert reloaded.get("dotmatrix.italic") is True
    assert reloaded.get("dotmatrix.threshold") == 100
    assert reloaded.get("dotmatrix.invert") is True
    assert reloaded.get("dotmatrix.scan") == "vertical"
    assert reloaded.get("dotmatrix.direction") == "btt"
    assert reloaded.get("dotmatrix.bit_order") == "lsb"
    assert reloaded.get("dotmatrix.page_chars") == 2
    assert reloaded.get("dotmatrix.dot_color") == "#FF0000"
    assert reloaded.get("dotmatrix.background") == "#0000FF"
    assert reloaded.get("dotmatrix.grid") is False
    assert reloaded.get("dotmatrix.zoom") == 18
    assert reloaded.get("dotmatrix.text") == "AB"
    assert str(reloaded.get("dotmatrix.output_dir")).endswith("saved_out")
    assert reloaded.get("dotmatrix.output_name") == "saved_font"

    second = DotMatrixPage(reloaded, paths)
    _CREATED_WIDGETS.append(second)
    assert second.size_spin.value() == 24
    assert second.bold_check.isChecked() is True
    assert second.italic_check.isChecked() is True
    assert second.threshold_spin.value() == 100
    assert second.invert_check.isChecked() is True
    assert second.scan() == "vertical"
    assert second.direction() == "btt"
    assert second.bit_order() == "lsb"
    assert second.page_chars() == 2
    assert second.dot_color() == (255, 0, 0)
    assert second.background_color() == (0, 0, 255)
    assert second.grid_check.isChecked() is False
    assert second.zoom_spin.value() == 18
    assert second.matrix_view.zoom() == 18
    assert second.text_edit.toPlainText() == "AB"
    assert second.name_edit.text() == "saved_font"


def test_mode_is_persisted(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.set_mode("bmp")
    page.save_settings()
    reloaded = Settings(paths.config_file)
    reloaded.load()
    assert reloaded.get("dotmatrix.mode") == "bmp"
    second = DotMatrixPage(reloaded, paths)
    _CREATED_WIDGETS.append(second)
    assert second.mode() == "bmp"
    assert second.mode_stack.currentIndex() == 1


def test_font_resolution_and_family_selector(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    assert page.font_combo.count() > 0
    assert page.set_font_family("Arial") is True
    assert page.font_family() == "Arial"

    font_file = resolve_font_file("Arial")
    if font_file is not None:
        assert Path(font_file).is_file()
    fallback = resolve_font_file("NoSuchFont12345")
    assert fallback is None or Path(fallback).is_file()
    page.set_text("A")
    assert page.page_count() >= 1
    assert page.params().size == page.size_spin.value()


# ---- MatrixView ---------------------------------------------------------


def test_matrix_view_set_matrix_zoom_and_size(qapp) -> None:
    matrix = Matrix(4, 4, [[(row + col) % 2 == 0 for col in range(4)] for row in range(4)])
    view = _make_view(matrix)
    assert view.matrix() is matrix
    assert view.minimumSize().width() >= 200
    assert view.minimumSize().height() >= 200
    assert view.sizeHint().width() > 0 and view.sizeHint().height() > 0

    assert view.set_zoom(1) == ZOOM_MIN
    assert view.set_zoom(999) == ZOOM_MAX
    assert view.set_zoom(9) == 9
    assert view.sizeHint().width() >= 4 * 9 + 16
    assert not view.grab().isNull()


def test_matrix_view_toggle_direct_click_and_wheel(qapp) -> None:
    matrix = Matrix(2, 2, [[False, False], [False, False]])
    view = _make_view(matrix)
    changed: list[tuple[int, int]] = []
    view.matrixChanged.connect(lambda row, col: changed.append((row, col)))

    assert view.cellToggled(1, 1) is True
    assert matrix.cells[1][1] is True
    assert changed == [(1, 1)]
    assert view.cellToggled(9, 9) is False

    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=view.cell_rect(0, 0).center())
    assert matrix.cells[0][0] is True

    before = view.zoom()
    event = QWheelEvent(
        QPointF(10, 10),
        QPointF(10, 10),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(view, event)
    assert view.zoom() == min(ZOOM_MAX, before + 1)

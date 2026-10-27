"""点阵核心测试：文字/BMP 渲染、分页、扫描方向、位序与 C 导出。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from app.core.fontmatrix import (
    FontParams,
    Matrix,
    matrix_from_image,
    matrix_from_text,
    matrix_to_bytes,
    matrix_to_c,
    matrices_to_bytes,
    paginate,
)

PATTERN = Matrix(
    2,
    8,
    [
        [True, True, False, False, False, False, False, False],
        [False, False, False, False, False, False, True, True],
    ],
)


def _params(**overrides) -> FontParams:
    return FontParams(**overrides)


# ---- 打包 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "bit_order", "expected"),
    [
        ("ltr", "msb", b"\xC0\x03"),
        ("ltr", "lsb", b"\x03\xC0"),
        ("rtl", "msb", b"\x03\xC0"),
        ("rtl", "lsb", b"\xC0\x03"),
    ],
)
def test_horizontal_packing_all_directions(direction, bit_order, expected) -> None:
    params = _params(scan="horizontal", direction=direction, bit_order=bit_order)
    assert matrix_to_bytes(PATTERN, params) == expected


@pytest.mark.parametrize(
    ("direction", "bit_order", "expected"),
    [
        ("ttb", "msb", b"\xA0\x05"),
        ("ttb", "lsb", b"\x05\xA0"),
        ("btt", "msb", b"\x50\x0A"),
        ("btt", "lsb", b"\x0A\x50"),
    ],
)
def test_vertical_packing_all_directions(direction, bit_order, expected) -> None:
    params = _params(scan="vertical", direction=direction, bit_order=bit_order)
    assert matrix_to_bytes(PATTERN, params) == expected


def test_direction_implies_scan_mode() -> None:
    assert matrix_to_bytes(PATTERN, _params(scan="horizontal", direction="ttb")) == b"\xA0\x05"
    assert matrix_to_bytes(PATTERN, _params(scan="vertical", direction="rtl")) == b"\x50\x0A"


def test_last_byte_is_padded_with_zeros() -> None:
    matrix = Matrix(1, 3, [[True, True, True]])
    assert matrix_to_bytes(matrix, _params(bit_order="msb")) == b"\xE0"
    assert matrix_to_bytes(matrix, _params(bit_order="lsb")) == b"\x07"


def test_vertical_packing_matches_transposed_horizontal() -> None:
    transposed = Matrix(
        PATTERN.cols, PATTERN.rows, [list(column) for column in zip(*PATTERN.cells)]
    )
    assert matrix_to_bytes(transposed, _params()) == matrix_to_bytes(
        PATTERN, _params(scan="vertical", direction="ttb")
    )


def test_matrices_to_bytes_concatenates_pages() -> None:
    pages = [PATTERN, PATTERN]
    assert matrices_to_bytes(pages, _params()) == matrix_to_bytes(PATTERN, _params()) * 2


# ---- 文字渲染 -----------------------------------------------------------


def test_render_default_font() -> None:
    pages = matrix_from_text("A", _params(size=16, page_chars=8))
    assert len(pages) == 1
    page = pages[0]
    assert page.rows == 16
    assert page.cols > 0
    assert any(any(row) for row in page.cells)
    assert len(matrix_to_bytes(page, _params())) == (page.rows * page.cols + 7) // 8


def test_render_multiline_stacks_rows() -> None:
    pages = matrix_from_text("A\nB", _params(size=16, page_chars=8))
    assert pages[0].rows == 32
    assert pages[0].cols > 0


def test_render_bold_and_italic_change_shape() -> None:
    normal = matrix_from_text("A", _params(size=20, page_chars=8))[0]
    bold = matrix_from_text("A", _params(size=20, bold=True, page_chars=8))[0]
    italic = matrix_from_text("A", _params(size=20, italic=True, page_chars=8))[0]
    assert bold.cells != normal.cells
    assert italic.cols > normal.cols


def test_render_with_explicit_windows_font() -> None:
    font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
    if not font_path.is_file():
        pytest.skip("当前系统没有 Arial 字体")
    page = matrix_from_text("A", _params(font_path=str(font_path), size=24))[0]
    assert page.rows == 24
    assert page.cols > 0
    assert any(any(row) for row in page.cells)


def test_render_empty_text_returns_no_pages() -> None:
    assert matrix_from_text("", _params()) == []


def test_long_text_pages_by_page_chars_times_size() -> None:
    pages = matrix_from_text("AAAAAAAAAA", _params(size=16, page_chars=1))
    assert len(pages) > 1
    assert all(page.cols == 16 for page in pages)
    assert all(page.rows == 16 for page in pages)


# ---- 图片与分页 ---------------------------------------------------------


def test_image_threshold_and_invert(tmp_path: Path) -> None:
    source = tmp_path / "sample.bmp"
    image = Image.new("L", (2, 2))
    image.putdata([0, 255, 128, 64])
    image.save(source)

    matrix = matrix_from_image(source, _params(threshold=128))
    assert (matrix.rows, matrix.cols) == (2, 2)
    assert matrix.cells == [[True, False], [False, True]]
    assert matrix_to_bytes(matrix, _params()) == b"\x90"

    inverted = matrix_from_image(source, _params(threshold=128, invert=True))
    assert inverted.cells == [[False, True], [True, False]]
    assert matrix_to_bytes(inverted, _params()) == b"\x60"


def test_image_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="找不到"):
        matrix_from_image(tmp_path / "missing.bmp", _params())


def test_paginate_pads_last_page() -> None:
    matrix = Matrix(2, 20, [[bool((row + col) % 2) for col in range(20)] for row in range(2)])
    pages = paginate(matrix, 1, char_width=7)
    assert len(pages) == 3
    assert all((page.rows, page.cols) == (2, 7) for page in pages)
    assert pages[0].cells == [row[:7] for row in matrix.cells]
    assert pages[1].cells == [row[7:14] for row in matrix.cells]
    last = pages[2].cells
    assert [row[:6] for row in last] == [row[14:] for row in matrix.cells]
    assert [row[6] for row in last] == [False, False]


def test_paginate_short_matrix_is_single_page() -> None:
    pages = paginate(PATTERN, 8, char_width=8)
    assert pages == [PATTERN]


def test_paginate_validates_arguments() -> None:
    with pytest.raises(ValueError):
        paginate(PATTERN, 0)
    with pytest.raises(ValueError):
        paginate(PATTERN, 1, char_width=0)


# ---- 矩阵与参数校验 -----------------------------------------------------


def test_matrix_cells_are_normalized_and_toggleable() -> None:
    matrix = Matrix(1, 2, [[0, 1]])  # type: ignore[list-item]
    assert matrix.cells == [[False, True]]
    assert matrix.toggle(0, 1) is False
    assert matrix.is_dot(0, 1) is False
    copied = matrix.copy()
    copied.toggle(0, 0)
    assert matrix.is_dot(0, 0) is False
    with pytest.raises(IndexError):
        matrix.toggle(1, 0)


def test_matrix_rejects_inconsistent_dimensions() -> None:
    with pytest.raises(ValueError, match="行数不一致"):
        Matrix(2, 2, [[False, False]])
    with pytest.raises(ValueError, match="列数不一致"):
        Matrix(1, 2, [[False]])


def test_font_params_validate_ranges() -> None:
    with pytest.raises(ValueError, match="字号"):
        FontParams(size=0)
    with pytest.raises(ValueError, match="阈值"):
        FontParams(threshold=256)
    with pytest.raises(ValueError, match="扫描方式"):
        FontParams(scan="diagonal")
    with pytest.raises(ValueError, match="每页字符数"):
        FontParams(page_chars=0)
    assert FontParams(font_path="  ").font_path is None


# ---- C 导出 -------------------------------------------------------------


def test_c_output_contains_metadata_and_sanitized_name() -> None:
    pages = [PATTERN, PATTERN]
    text = matrix_to_c(pages, _params(bit_order="lsb"), "my font-1")
    assert "const unsigned char my_font_1[]" in text
    assert "尺寸：8 x 2" in text
    assert "扫描方式：水平（逐行）" in text
    assert "位序：LSB 在前" in text
    assert "页数：2" in text
    assert "总字节数：4" in text
    assert text.count("0x") == 4
    assert text.endswith("};\n")


def test_c_output_rejects_empty_pages() -> None:
    with pytest.raises(ValueError, match="没有可导出"):
        matrix_to_c([], _params(), "font")


def test_c_identifier_avoids_leading_digits() -> None:
    text = matrix_to_c([PATTERN], _params(), "123 font")
    assert "const unsigned char _123_font[]" in text

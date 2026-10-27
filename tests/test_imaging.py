"""图片取模核心测试：色彩量化、扫描顺序、1 位打包与批量导出。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.core.imaging import (
    ImageParams,
    batch_convert,
    convert_file,
    convert_image,
    list_image_files,
    load_image,
)

# 2x2 测试图：红、绿 / 蓝、白。
COLORS = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))


def _image(colors=COLORS, size=(2, 2), mode="RGB") -> Image.Image:
    image = Image.new(mode, size)
    image.putdata(list(colors))
    return image


def _write_png(path: Path, colors=COLORS) -> Path:
    _image(colors).save(path)
    return path


# ---- 参数 ---------------------------------------------------------------


def test_params_defaults_and_normalization() -> None:
    params = ImageParams()
    assert params.output_type == "c"
    assert params.scan == "horizontal"
    assert params.direction == "ltr"
    assert params.color_depth == 16
    assert params.byte_order == "little"
    assert params.width is None
    assert params.height is None
    assert params.background == (0, 0, 0)
    assert params.invert is False

    normalized = ImageParams(
        output_type="BIN",
        scan="Vertical",
        direction="BTT",
        color_depth=32,
        byte_order="BIG",
        width=0,
        height="12",
        background="#102030",
        invert=1,
    )
    assert normalized.output_type == "bin"
    assert normalized.scan == "vertical"
    assert normalized.direction == "btt"
    assert normalized.byte_order == "big"
    assert normalized.width is None
    assert normalized.height == 12
    assert normalized.background == (0x10, 0x20, 0x30)
    assert normalized.invert is True


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"output_type": "png"}, "输出类型"),
        ({"scan": "diagonal"}, "扫描方式"),
        ({"direction": "up"}, "扫描方向"),
        ({"color_depth": 12}, "颜色深度"),
        ({"byte_order": "middle"}, "字节序"),
        ({"width": -1}, "宽度"),
        ({"height": 999999}, "高度"),
        ({"background": (300, 0, 0)}, "背景色"),
        ({"background": (1, 2)}, "背景色"),
    ],
)
def test_params_reject_invalid_values(kwargs, match) -> None:
    with pytest.raises(ValueError, match=match):
        ImageParams(**kwargs)


# ---- 读取 ---------------------------------------------------------------


def test_load_image_reads_independent_copy(tmp_path: Path) -> None:
    source = _write_png(tmp_path / "sample.png")
    image = load_image(source)
    assert image.size == (2, 2)
    assert image.mode == "RGB"
    source.unlink()
    # 副本不依赖源文件，删除后仍可读取像素。
    assert image.getpixel((0, 0)) == COLORS[0]


def test_load_image_rejects_missing_and_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="找不到"):
        load_image(tmp_path / "missing.png")

    invalid = tmp_path / "not-an-image.png"
    invalid.write_text("这不是图片", encoding="utf-8")
    with pytest.raises(ValueError, match="无法读取|读取图片失败"):
        load_image(invalid)


def test_list_image_files_filters_and_sorts(tmp_path: Path) -> None:
    _write_png(tmp_path / "b.png")
    _write_png(tmp_path / "a.png")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    _write_png(tmp_path / "sub" / "c.png")

    files = list_image_files(tmp_path)
    assert [item.name for item in files] == ["a.png", "b.png"]
    recursive = list_image_files(tmp_path, recursive=True)
    assert [item.name for item in recursive] == ["a.png", "b.png", "c.png"]
    with pytest.raises(ValueError, match="文件夹"):
        list_image_files(tmp_path / "missing")


# ---- 色彩与字节序 -------------------------------------------------------


def test_known_2x2_bytes_for_565_332_888_and_rgba() -> None:
    image = _image()

    little = convert_image(image, ImageParams(color_depth=16))
    assert little.data == bytes(
        (0x00, 0xF8, 0xE0, 0x07, 0x1F, 0x00, 0xFF, 0xFF)
    )
    big = convert_image(image, ImageParams(color_depth=16, byte_order="big"))
    assert big.data == bytes(
        (0xF8, 0x00, 0x07, 0xE0, 0x00, 0x1F, 0xFF, 0xFF)
    )

    rgb332 = convert_image(image, ImageParams(color_depth=8))
    assert rgb332.data == bytes((0xE0, 0x1C, 0x03, 0xFF))

    rgb888 = convert_image(image, ImageParams(color_depth=24))
    assert rgb888.data == bytes(
        (
            0xFF,
            0x00,
            0x00,
            0x00,
            0xFF,
            0x00,
            0x00,
            0x00,
            0xFF,
            0xFF,
            0xFF,
            0xFF,
        )
    )

    rgba = convert_image(image, ImageParams(color_depth=32))
    assert rgba.data == bytes(
        (
            0xFF,
            0x00,
            0x00,
            0xFF,
            0x00,
            0xFF,
            0x00,
            0xFF,
            0x00,
            0x00,
            0xFF,
            0xFF,
            0xFF,
            0xFF,
            0xFF,
            0xFF,
        )
    )


def test_byte_order_changes_only_16bit_values() -> None:
    image = _image()
    for depth in (8, 24, 32):
        little = convert_image(
            image, ImageParams(color_depth=depth, byte_order="little")
        )
        big = convert_image(
            image, ImageParams(color_depth=depth, byte_order="big")
        )
        assert little.data == big.data


# ---- 1 位 ---------------------------------------------------------------


def test_one_bit_packing_padding_and_invert() -> None:
    # 10 个像素：黑、白交替；白色编码为 1。
    colors = [(0, 0, 0) if index % 2 == 0 else (255, 255, 255) for index in range(10)]
    image = Image.new("RGB", (10, 1))
    image.putdata(colors)

    normal = convert_image(image, ImageParams(color_depth=1))
    assert normal.data == bytes((0x55, 0x40))  # 最后 6 位补 0
    assert normal.preview.size == (10, 1)

    inverted = convert_image(image, ImageParams(color_depth=1, invert=True))
    assert inverted.data == bytes((0xAA, 0x80))


def test_one_bit_uses_fixed_threshold() -> None:
    image = Image.new("RGB", (3, 1))
    image.putdata([(127, 127, 127), (128, 128, 128), (200, 200, 200)])
    result = convert_image(image, ImageParams(color_depth=1))
    # 127 -> 0，128/200 -> 1，MSB 在前 -> 0110 0000
    assert result.data == bytes((0x60,))


# ---- 扫描方向 -----------------------------------------------------------


def test_scan_directions_reorder_pixels() -> None:
    image = _image()
    horizontal = convert_image(
        image, ImageParams(scan="horizontal", direction="ltr", color_depth=24)
    ).data
    rtl = convert_image(
        image, ImageParams(scan="horizontal", direction="rtl", color_depth=24)
    ).data
    vertical = convert_image(
        image, ImageParams(scan="vertical", direction="ttb", color_depth=24)
    ).data
    btt = convert_image(
        image, ImageParams(scan="vertical", direction="btt", color_depth=24)
    ).data

    red = bytes((0xFF, 0x00, 0x00))
    green = bytes((0x00, 0xFF, 0x00))
    blue = bytes((0x00, 0x00, 0xFF))
    white = bytes((0xFF, 0xFF, 0xFF))

    assert horizontal == red + green + blue + white
    assert rtl == green + red + white + blue
    assert vertical == red + blue + green + white
    assert btt == blue + red + white + green


def test_direction_axis_selects_scan_axis() -> None:
    image = _image()
    explicit = convert_image(
        image,
        ImageParams(scan="horizontal", direction="ttb", color_depth=24),
    )
    vertical = convert_image(
        image,
        ImageParams(scan="vertical", direction="ttb", color_depth=24),
    )
    assert explicit.data == vertical.data

    vertical_with_ltr = convert_image(
        image,
        ImageParams(scan="vertical", direction="ltr", color_depth=24),
    )
    assert vertical_with_ltr.data == vertical.data


# ---- 缩放与透明 ---------------------------------------------------------


def test_resize_nearest_and_aspect_preservation() -> None:
    image = Image.new("RGB", (4, 2), (10, 20, 30))
    both = convert_image(image, ImageParams(width=2, height=3, color_depth=24))
    assert (both.width, both.height) == (2, 3)
    assert both.preview.size == (2, 3)
    assert len(both.data) == 2 * 3 * 3

    width_only = convert_image(image, ImageParams(width=2, color_depth=24))
    assert (width_only.width, width_only.height) == (2, 1)
    assert width_only.preview.size == (2, 1)

    height_only = convert_image(image, ImageParams(height=1, color_depth=24))
    assert (height_only.width, height_only.height) == (2, 1)

    original = convert_image(image, ImageParams(color_depth=24))
    assert (original.width, original.height) == (4, 2)


def test_resize_uses_nearest_neighbor() -> None:
    image = Image.new("RGB", (2, 2))
    image.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)])
    resized = convert_image(image, ImageParams(width=4, height=4, color_depth=24))
    assert (resized.width, resized.height) == (4, 4)
    data = resized.data
    pixels = {
        tuple(data[index : index + 3]) for index in range(0, len(data), 3)
    }
    # 最近邻只复制原像素，不会产生双线性插值的中间色。
    assert pixels == set(COLORS)


def test_transparent_pixels_use_background_color() -> None:
    image = Image.new("RGBA", (1, 1), (255, 0, 0, 0))
    over_blue = convert_image(
        image, ImageParams(color_depth=16, background=(0, 0, 255))
    )
    assert over_blue.data == bytes((0x1F, 0x00))

    # 透明像素合成到白色背景后，1 位输出为白色（1）。
    over_white = convert_image(
        image, ImageParams(color_depth=1, background=(255, 255, 255))
    )
    assert over_white.data == bytes((0x80,))


# ---- C / BIN / LVGL -----------------------------------------------------


def test_c_text_header_name_and_line_width() -> None:
    image = Image.new("RGB", (3, 3), (1, 2, 3))
    result = convert_image(
        image, ImageParams(output_type="c", color_depth=24), name="my image"
    )
    text = result.text or ""
    assert "/* my_image 3x3 depth=24 scan=horizontal direction=ltr" in text
    assert "byte_order=little */" in text
    assert "const unsigned char my_image[] = {" in text
    assert text.count("0x") == 27
    data_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("0x")
    ]
    assert [line.count("0x") for line in data_lines] == [12, 12, 3]
    assert text.endswith("};\n")


def test_bin_output_has_no_text() -> None:
    result = convert_image(_image(), ImageParams(output_type="bin"))
    assert result.text is None
    assert len(result.data) == 8


def test_lvgl8_and_lvgl9_descriptors() -> None:
    image = _image()
    lvgl8 = convert_image(
        image, ImageParams(output_type="lvgl8", color_depth=16)
    ).text or ""
    assert "LVGL 8 image" in lvgl8
    assert "const lv_img_dsc_t image = {" in lvgl8
    assert "LV_IMG_CF_TRUE_COLOR" in lvgl8
    assert ".header.w = 2" in lvgl8
    assert ".header.h = 2" in lvgl8
    assert ".data_size = 8" in lvgl8

    lvgl9 = convert_image(
        image, ImageParams(output_type="lvgl9", color_depth=16)
    ).text or ""
    assert "LVGL 9 image" in lvgl9
    assert "const lv_image_dsc_t image = {" in lvgl9
    assert "LV_COLOR_FORMAT_RGB565" in lvgl9
    assert ".header.stride = 4" in lvgl9
    assert ".data_size = 8" in lvgl9


def test_lvgl_output_allows_vertical_scan_with_warning() -> None:
    result = convert_image(
        _image(),
        ImageParams(output_type="lvgl9", scan="vertical", direction="ttb"),
    )
    text = result.text or ""
    assert "lv_image_dsc_t" in text
    assert "纵向扫描" in text


# ---- 单文件与批量 -------------------------------------------------------


def test_convert_file_writes_c_and_bin(tmp_path: Path) -> None:
    source = _write_png(tmp_path / "picture 1.png")
    out_dir = tmp_path / "out"
    c_paths = convert_file(source, ImageParams(output_type="c"), out_dir)
    assert len(c_paths) == 1
    assert c_paths[0].name == "picture 1.c"
    text = c_paths[0].read_text(encoding="utf-8")
    assert "const unsigned char picture_1[]" in text
    assert "2x2" in text

    bin_paths = convert_file(source, ImageParams(output_type="bin"), out_dir)
    assert bin_paths[0].suffix == ".bin"
    raw = convert_image(load_image(source), ImageParams(output_type="bin"))
    assert bin_paths[0].read_bytes() == raw.data


def test_convert_file_custom_name_and_collision(tmp_path: Path) -> None:
    source = _write_png(tmp_path / "source.png")
    out_dir = tmp_path / "out"
    first = convert_file(
        source, ImageParams(output_type="c"), out_dir, name="my_array.c"
    )
    assert first[0].name == "my_array.c"
    second = convert_file(
        source, ImageParams(output_type="c"), out_dir, name="my_array"
    )
    assert second[0].name == "my_array_1.c"


def test_batch_convert_two_files_and_collision(tmp_path: Path) -> None:
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _write_png(first_dir / "same.png")
    second = _write_png(second_dir / "same.png")
    out_dir = tmp_path / "out"
    progress: list[tuple[int, int]] = []

    result = batch_convert(
        [first, second],
        ImageParams(output_type="c"),
        out_dir,
        progress=lambda done, total: progress.append((done, total)),
    )
    assert set(result) == {str(first), str(second)}
    assert [path.name for path in result[str(first)]] == ["same.c"]
    assert [path.name for path in result[str(second)]] == ["same_1.c"]
    assert progress == [(1, 2), (2, 2)]
    assert result.errors == {}
    assert result.canceled is False

    again = batch_convert([first], ImageParams(output_type="c"), out_dir)
    assert again[str(first)][0].name == "same_2.c"


def test_batch_convert_collects_errors_and_continues(tmp_path: Path) -> None:
    valid = _write_png(tmp_path / "valid.png")
    missing = tmp_path / "missing.png"
    progress: list[tuple[int, int]] = []

    result = batch_convert(
        [missing, valid],
        ImageParams(output_type="bin"),
        tmp_path / "out",
        progress=lambda done, total: progress.append((done, total)),
    )
    assert result[str(missing)] == []
    assert str(missing) in result.errors
    assert result[str(valid)][0].is_file()
    assert progress == [(1, 2), (2, 2)]


def test_batch_convert_cancel_stops_before_next_file(tmp_path: Path) -> None:
    first = _write_png(tmp_path / "first.png")
    second = _write_png(tmp_path / "second.png")
    calls: list[int] = []

    def cancel() -> bool:
        calls.append(1)
        return len(calls) > 1

    result = batch_convert(
        [first, second],
        ImageParams(output_type="bin"),
        tmp_path / "out",
        cancel=cancel,
    )
    assert result.canceled is True
    assert result[str(first)][0].is_file()
    assert str(second) not in result


def test_batch_convert_empty_list(tmp_path: Path) -> None:
    result = batch_convert([], ImageParams(), tmp_path / "out")
    assert result == {}
    assert result.errors == {}
    assert result.canceled is False


# ---- 预览 ---------------------------------------------------------------


@pytest.mark.parametrize("depth", (1, 8, 16, 24, 32))
def test_preview_size_matches_output(depth: int) -> None:
    image = Image.new("RGB", (3, 2), (10, 20, 30))
    result = convert_image(image, ImageParams(color_depth=depth, width=2))
    assert result.preview.size == (2, round(2 * 2 / 3))
    assert result.preview.size == (result.width, result.height)


def test_preview_quantizes_colors() -> None:
    image = _image()
    preview_565 = convert_image(image, ImageParams(color_depth=16)).preview
    assert preview_565.getpixel((0, 0)) == (255, 0, 0)
    assert preview_565.getpixel((1, 1)) == (255, 255, 255)

    preview_332 = convert_image(image, ImageParams(color_depth=8)).preview
    assert preview_332.getpixel((0, 0)) == (255, 0, 0)

    preview_1 = convert_image(
        image, ImageParams(color_depth=1)
    ).preview.convert("RGB")
    # 红色亮度低于 128 -> 黑；白色亮度高于阈值 -> 白。
    assert preview_1.getpixel((0, 0)) == (0, 0, 0)
    assert preview_1.getpixel((1, 1)) == (255, 255, 255)


def test_one_bit_threshold_is_configurable() -> None:
    image = Image.new("RGB", (8, 1), (120, 120, 120))
    dark = convert_image(image, ImageParams(color_depth=1, threshold=200))
    light = convert_image(image, ImageParams(color_depth=1, threshold=100))
    assert dark.data == b"\x00"
    assert light.data == b"\xff"


def test_output_pixel_limit_is_enforced() -> None:
    image = Image.new("RGB", (2, 2), (0, 0, 0))
    with pytest.raises(ValueError, match="过大"):
        convert_image(image, ImageParams(width=5000, height=5000))

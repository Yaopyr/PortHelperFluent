"""``image`` 子命令：单图 ``convert`` 与文件夹 ``batch`` 取模。

像素编码、缩放与量化本身由 ``tests/test_imaging.py`` 覆盖；本文件只验证 CLI 契约：
参数映射、``data`` 字段形状、写出文件的位置与退出码。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

# 红色 RGB565 为 0xF800：小端两字节是 00 F8，大端是 F8 00。
RED_565_LE = b"\x00\xF8"
RED_565_BE = b"\xF8\x00"

RED = (255, 0, 0)


def _png(path: Path, size: tuple[int, int] = (4, 4), color=RED) -> Path:
    """写一张纯色 RGB PNG，返回路径（父目录由调用方保证存在）。"""
    Image.new("RGB", size, color).save(path)
    return path


def _out_dir(tmp_path: Path) -> Path:
    return tmp_path / "out"


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------


def test_convert_single_png_to_little_endian_16bit(cli, tmp_path) -> None:
    src = _png(tmp_path / "red.png")
    data = cli(
        [
            "image",
            "convert",
            "--file",
            str(src),
            "--out-dir",
            str(_out_dir(tmp_path)),
            "--type",
            "bin",
            "--depth",
            "16",
            "--width",
            "4",
            "--height",
            "4",
        ]
    )["data"]

    raw = (_out_dir(tmp_path) / "red.bin").read_bytes()
    assert len(raw) == 32 and raw[:2] == RED_565_LE
    assert data["width"] == 4 and data["height"] == 4
    assert data["bytes"] == 32
    assert data["files"] == [str(_out_dir(tmp_path) / "red.bin")]


def test_convert_c_contains_comment_and_array(cli, tmp_path) -> None:
    src = _png(tmp_path / "red.png")
    payload = cli(
        [
            "image",
            "convert",
            "--file",
            str(src),
            "--out-dir",
            str(_out_dir(tmp_path)),
            "--type",
            "c",
            "--width",
            "4",
            "--height",
            "4",
        ]
    )
    assert payload["exit_code"] == 0
    text = (_out_dir(tmp_path) / "red.c").read_text(encoding="utf-8")
    # 行首注释带尺寸与参数，数组声明是 const，标识符取输入文件名。
    assert "4x4" in text and "depth=16" in text
    assert "const unsigned char red[]" in text
    assert payload["data"]["files"] == [str(_out_dir(tmp_path) / "red.c")]


def test_convert_lvgl9_output(cli, tmp_path) -> None:
    src = _png(tmp_path / "icon.png", size=(8, 8))
    cli(
        [
            "image",
            "convert",
            "--file",
            str(src),
            "--out-dir",
            str(_out_dir(tmp_path)),
            "--type",
            "lvgl9",
            "--depth",
            "16",
        ]
    )
    text = (_out_dir(tmp_path) / "icon.c").read_text(encoding="utf-8")
    assert "lv_image_dsc_t" in text and "LV_COLOR_FORMAT_RGB565" in text


def test_convert_name_override(cli, tmp_path) -> None:
    src = _png(tmp_path / "red.png")
    data = cli(
        [
            "image",
            "convert",
            "--file",
            str(src),
            "--out-dir",
            str(_out_dir(tmp_path)),
            "--name",
            "logo",
        ]
    )["data"]
    assert data["files"] == [str(_out_dir(tmp_path) / "logo.c")]
    assert "const unsigned char logo[]" in (
        _out_dir(tmp_path) / "logo.c"
    ).read_text(encoding="utf-8")


def test_convert_envelope_and_data_shape(cli, tmp_path) -> None:
    src = _png(tmp_path / "red.png")
    payload = cli(
        ["image", "convert", "--file", str(src), "--out-dir", str(_out_dir(tmp_path))]
    )
    assert payload["ok"] is True
    assert payload["command"] == "image.convert"
    assert payload["exit_code"] == 0
    assert set(payload["data"]) == {"width", "height", "bytes", "files"}
    # 不给 --width/--height 时按原图尺寸输出。
    assert payload["data"]["width"] == 4 and payload["data"]["height"] == 4


def test_convert_bilevel_and_invert(cli, tmp_path) -> None:
    src = _png(tmp_path / "white.png", size=(8, 8), color=(255, 255, 255))
    cli(
        [
            "image",
            "convert",
            "--file",
            str(src),
            "--out-dir",
            str(tmp_path / "on"),
            "--type",
            "bin",
            "--depth",
            "1",
        ]
    )
    assert (tmp_path / "on" / "white.bin").read_bytes() == b"\xFF" * 8

    cli(
        [
            "image",
            "convert",
            "--file",
            str(src),
            "--out-dir",
            str(tmp_path / "inverted"),
            "--type",
            "bin",
            "--depth",
            "1",
            "--invert",
        ]
    )
    assert (tmp_path / "inverted" / "white.bin").read_bytes() == b"\x00" * 8


def test_convert_background_and_byte_order(cli, tmp_path) -> None:
    # 全透明像素合成到红色背景，再按大端两字节输出。
    src = tmp_path / "alpha.png"
    Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(src)
    cli(
        [
            "image",
            "convert",
            "--file",
            str(src),
            "--out-dir",
            str(tmp_path / "out"),
            "--type",
            "bin",
            "--depth",
            "16",
            "--byte-order",
            "big",
            "--background",
            "#FF0000",
        ]
    )
    assert (tmp_path / "out" / "alpha.bin").read_bytes()[:2] == RED_565_BE


def test_convert_error_paths(cli, tmp_path) -> None:
    src = _png(tmp_path / "red.png")
    out_dir = str(_out_dir(tmp_path))

    # 缺失文件：找不到（3）。
    payload = cli(
        ["image", "convert", "--file", str(tmp_path / "nope.png"), "--out-dir", out_dir]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"

    # 背景色无法解析：输入非法（3）。
    payload = cli(
        ["image", "convert", "--file", str(src), "--out-dir", out_dir, "--background", "zzz"]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    # 负的宽高由核心校验拒绝：输入非法（3）。
    payload = cli(
        ["image", "convert", "--file", str(src), "--out-dir", out_dir, "--width", "-4"]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    # 参数取值不在允许集合内、阈值越界、缺必填项：用法错误（2）。
    for extra in (
        ["--type", "bmp"],
        ["--depth", "12"],
        ["--scan", "diagonal"],
        ["--direction", "up"],
        ["--byte-order", "middle"],
        ["--threshold", "300"],
        ["--threshold", "-1"],
    ):
        payload = cli(["image", "convert", "--file", str(src), "--out-dir", out_dir, *extra])
        assert payload["exit_code"] == 2, extra
        assert payload["error"]["code"] == "usage", extra

    payload = cli(["image", "convert", "--file", str(src)])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


def test_batch_converts_all_images(cli, tmp_path) -> None:
    folder = tmp_path / "src"
    (folder / "nested").mkdir(parents=True)
    _png(folder / "red.png")
    _png(folder / "nested" / "green.png", color=(0, 255, 0))

    payload = cli(
        [
            "image",
            "batch",
            "--folder",
            str(folder),
            "--out-dir",
            str(_out_dir(tmp_path)),
            "--recursive",
            "--type",
            "bin",
            "--depth",
            "16",
        ]
    )

    assert payload["exit_code"] == 0
    assert payload["command"] == "image.batch"
    data = payload["data"]
    assert set(data) == {"files", "errors", "canceled"}
    assert len(data["files"]) == 2
    assert data["errors"] == {}
    assert data["canceled"] is False
    assert data["files"][str(folder / "red.png")] == [str(_out_dir(tmp_path) / "red.bin")]
    assert data["files"][str(folder / "nested" / "green.png")] == [
        str(_out_dir(tmp_path) / "green.bin")
    ]
    assert (_out_dir(tmp_path) / "green.bin").exists()


def test_batch_without_recursive_skips_subfolders(cli, tmp_path) -> None:
    folder = tmp_path / "src"
    (folder / "nested").mkdir(parents=True)
    _png(folder / "red.png")
    _png(folder / "nested" / "green.png", color=(0, 255, 0))

    data = cli(
        ["image", "batch", "--folder", str(folder), "--out-dir", str(_out_dir(tmp_path))]
    )["data"]
    assert len(data["files"]) == 1
    assert str(folder / "red.png") in data["files"]


def test_batch_collects_errors_without_aborting(cli, tmp_path) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    _png(folder / "good.png")
    bad = folder / "bad.png"
    bad.write_bytes(b"not an image")

    data = cli(
        ["image", "batch", "--folder", str(folder), "--out-dir", str(_out_dir(tmp_path))]
    )["data"]
    assert len(data["files"]) == 2
    assert str(bad) in data["errors"]
    assert data["files"][str(bad)] == []
    assert data["files"][str(folder / "good.png")] == [
        str(_out_dir(tmp_path) / "good.c")
    ]


def test_batch_error_paths(cli, tmp_path) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    _png(folder / "red.png")

    payload = cli(
        [
            "image",
            "batch",
            "--folder",
            str(tmp_path / "nope"),
            "--out-dir",
            str(_out_dir(tmp_path)),
        ]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"

    payload = cli(
        [
            "image",
            "batch",
            "--folder",
            str(folder),
            "--out-dir",
            str(_out_dir(tmp_path)),
            "--background",
            "#GGGGGG",
        ]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    payload = cli(["image", "batch", "--folder", str(folder)])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

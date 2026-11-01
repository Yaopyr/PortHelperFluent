"""``dotmatrix`` 子命令：文字、BMP 与文件夹批量点阵导出。

渲染、分页与打包本身由 ``tests/test_fontmatrix.py`` 覆盖；本文件只验证 CLI 契约：
参数映射、``--toggle`` 编辑、输出文件命名与格式、``data`` 字段形状与退出码。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_KEYS = {"pages", "width", "height", "bytes", "files"}
# 批量命令额外返回逐文件失败原因，单个坏图不影响其余文件。
BATCH_KEYS = DATA_KEYS | {"errors"}


def _bmp(path: Path, value: int = 0, size: tuple[int, int] = (8, 8)) -> Path:
    """写一张纯色灰度 BMP：``value=0`` 为黑（全部取为黑点），255 为白。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", size, value).save(path)
    return path


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------


def test_text_export_bin_and_c(cli, tmp_path) -> None:
    payload = cli(
        [
            "dotmatrix",
            "text",
            "--text",
            "A",
            "--size",
            "16",
            "--out-dir",
            str(tmp_path),
            "--export-format",
            "bin",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["command"] == "dotmatrix.text"
    data = payload["data"]
    assert set(data) == DATA_KEYS
    assert (tmp_path / "font_matrix.bin").exists()
    assert data["bytes"] > 0 and data["pages"] >= 1
    assert data["width"] > 0 and data["height"] > 0
    assert data["files"] == [str(tmp_path / "font_matrix.bin")]

    payload = cli(
        [
            "dotmatrix",
            "text",
            "--text",
            "A",
            "--size",
            "16",
            "--out-dir",
            str(tmp_path),
            "--export-format",
            "c",
            "--name",
            "letter",
        ]
    )
    assert payload["exit_code"] == 0
    text = (tmp_path / "letter.c").read_text(encoding="utf-8")
    assert "const unsigned char letter[]" in text
    # C 与 BIN 的点阵数据长度一致：``bytes`` 统计的是字模字节数，不是文件大小。
    assert payload["data"]["bytes"] == data["bytes"]
    assert payload["data"]["files"] == [str(tmp_path / "letter.c")]


def test_toggle_changes_first_byte(cli, tmp_path) -> None:
    base = cli(
        [
            "dotmatrix",
            "text",
            "--text",
            "A",
            "--size",
            "8",
            "--out-dir",
            str(tmp_path / "a"),
            "--export-format",
            "bin",
        ]
    )["data"]
    toggled = cli(
        [
            "dotmatrix",
            "text",
            "--text",
            "A",
            "--size",
            "8",
            "--out-dir",
            str(tmp_path / "b"),
            "--export-format",
            "bin",
            "--toggle",
            "0,0,0",
        ]
    )["data"]

    raw_base = (tmp_path / "a" / "font_matrix.bin").read_bytes()
    raw_toggled = (tmp_path / "b" / "font_matrix.bin").read_bytes()
    assert len(raw_base) == len(raw_toggled) == base["bytes"] == toggled["bytes"]
    assert raw_base[0] != raw_toggled[0]


def test_text_param_mapping(cli, tmp_path) -> None:
    """竖扫、LSB 与逐页字符数都透传到 ``FontParams``，并改变导出字节与页数。"""
    horizontal = cli(
        [
            "dotmatrix",
            "text",
            "--text",
            "AB",
            "--out-dir",
            str(tmp_path / "h"),
            "--export-format",
            "bin",
        ]
    )["data"]
    vertical = cli(
        [
            "dotmatrix",
            "text",
            "--text",
            "AB",
            "--out-dir",
            str(tmp_path / "v"),
            "--export-format",
            "bin",
            "--scan",
            "vertical",
            "--direction",
            "ttb",
            "--bit-order",
            "lsb",
        ]
    )["data"]
    raw_h = (tmp_path / "h" / "font_matrix.bin").read_bytes()
    raw_v = (tmp_path / "v" / "font_matrix.bin").read_bytes()
    # 同一块点阵换扫描方向后字节数不变、内容不同。
    assert len(raw_h) == len(raw_v) == horizontal["bytes"] == vertical["bytes"]
    assert raw_h != raw_v

    # 每页字符数决定页宽："AB" 在每页 1 个字符、字号 8 时被拆成多页，页宽 8。
    paged = cli(
        [
            "dotmatrix",
            "text",
            "--text",
            "AB",
            "--size",
            "8",
            "--page-chars",
            "1",
            "--out-dir",
            str(tmp_path / "paged"),
            "--export-format",
            "bin",
        ]
    )["data"]
    assert paged["pages"] >= 2
    assert paged["width"] == 8
    assert paged["pages"] > horizontal["pages"]


def test_output_format_and_pretty(cli, tmp_path) -> None:
    """``--format json|text`` 与 ``--pretty`` 是各叶子解析器共用的输出选项。"""
    out_dir = str(tmp_path / "out")

    payload = cli(
        ["dotmatrix", "text", "--text", "A", "--out-dir", out_dir, "--format", "text"]
    )
    assert payload["exit_code"] == 0
    assert "ok: true" in payload["raw"] and "dotmatrix.text" in payload["raw"]

    payload = cli(
        [
            "dotmatrix",
            "text",
            "--text",
            "A",
            "--out-dir",
            out_dir,
            "--format",
            "json",
            "--pretty",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["ok"] is True and payload["command"] == "dotmatrix.text"


def test_text_error_paths(cli, tmp_path) -> None:
    out_dir = str(tmp_path / "out")

    # 空文字没有可导出的点阵：输入非法（3）。
    payload = cli(["dotmatrix", "text", "--text", "", "--out-dir", out_dir])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    # 越界的数值参数、互斥字体参数、未知导出格式：用法错误（2）。
    for extra in (
        ["--size", "0"],
        ["--size", "257"],
        ["--threshold", "300"],
        ["--page-chars", "0"],
        ["--page-chars", "257"],
        ["--scan", "diagonal"],
        ["--direction", "up"],
        ["--bit-order", "middle"],
        ["--export-format", "png"],
        ["--font-family", "Arial", "--font-file", "a.ttf"],
    ):
        payload = cli(
            ["dotmatrix", "text", "--text", "A", "--out-dir", out_dir, *extra]
        )
        assert payload["exit_code"] == 2, extra
        assert payload["error"]["code"] == "usage", extra

    # 缺必填项：用法错误（2）。
    assert cli(["dotmatrix", "text", "--out-dir", out_dir])["exit_code"] == 2
    assert cli(["dotmatrix", "text", "--text", "A"])["exit_code"] == 2

    # 字体文件不存在：找不到（3）。
    payload = cli(
        [
            "dotmatrix",
            "text",
            "--text",
            "A",
            "--out-dir",
            out_dir,
            "--font-file",
            str(tmp_path / "nope.ttf"),
        ]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"

    # 无法解析的字体族回落 Pillow 内置字体，仍然成功（与 GUI 一致）。
    payload = cli(
        [
            "dotmatrix",
            "text",
            "--text",
            "A",
            "--out-dir",
            out_dir,
            "--font-family",
            "NoSuchFontFamily",
            "--export-format",
            "bin",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["bytes"] > 0


def test_toggle_error_paths(cli, tmp_path) -> None:
    out_dir = str(tmp_path / "out")

    # 页序号越界：输入非法（3）。
    payload = cli(
        ["dotmatrix", "text", "--text", "A", "--out-dir", out_dir, "--toggle", "9,0,0"]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    # 行列越界：输入非法（3）。
    payload = cli(
        ["dotmatrix", "text", "--text", "A", "--out-dir", out_dir, "--toggle", "0,99,0"]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    # 格式错误：用法错误（2）。
    for value in ("abc", "1,2", "1,2,3,4", "1,2,x"):
        payload = cli(
            ["dotmatrix", "text", "--text", "A", "--out-dir", out_dir, "--toggle", value]
        )
        assert payload["exit_code"] == 2, value
        assert payload["error"]["code"] == "usage", value


# ---------------------------------------------------------------------------
# bmp
# ---------------------------------------------------------------------------


def test_bmp_export_bin_and_c(cli, tmp_path) -> None:
    src = _bmp(tmp_path / "solid.bmp", 0)

    payload = cli(
        [
            "dotmatrix",
            "bmp",
            "--file",
            str(src),
            "--out-dir",
            str(tmp_path / "bin"),
            "--export-format",
            "bin",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["command"] == "dotmatrix.bmp"
    data = payload["data"]
    assert set(data) == DATA_KEYS
    assert data["pages"] == 1
    assert data["width"] == 8 and data["height"] == 8 and data["bytes"] == 8
    assert (tmp_path / "bin" / "solid.bin").read_bytes() == b"\xFF" * 8
    assert data["files"] == [str(tmp_path / "bin" / "solid.bin")]

    # 反色后同一张黑图变成全白点。
    payload = cli(
        [
            "dotmatrix",
            "bmp",
            "--file",
            str(src),
            "--out-dir",
            str(tmp_path / "inverted"),
            "--export-format",
            "bin",
            "--invert",
        ]
    )
    assert payload["exit_code"] == 0
    assert (tmp_path / "inverted" / "solid.bin").read_bytes() == b"\x00" * 8

    # 默认名取输入 stem，--name 可覆盖；C 文件里用同一个标识符。
    payload = cli(
        [
            "dotmatrix",
            "bmp",
            "--file",
            str(src),
            "--out-dir",
            str(tmp_path / "named"),
            "--export-format",
            "c",
            "--name",
            "glyph",
        ]
    )
    assert payload["exit_code"] == 0
    text = (tmp_path / "named" / "glyph.c").read_text(encoding="utf-8")
    assert "const unsigned char glyph[]" in text
    assert payload["data"]["files"] == [str(tmp_path / "named" / "glyph.c")]


def test_bmp_toggle_flips_one_cell(cli, tmp_path) -> None:
    src = _bmp(tmp_path / "white.bmp", 255)
    payload = cli(
        [
            "dotmatrix",
            "bmp",
            "--file",
            str(src),
            "--out-dir",
            str(tmp_path / "out"),
            "--export-format",
            "bin",
            "--toggle",
            "0,0,0",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["bytes"] == 8
    # 默认 msb 位序：翻转 (0, 0) 只影响第一字节的 bit7。
    assert (tmp_path / "out" / "white.bin").read_bytes() == b"\x80" + b"\x00" * 7


def test_bmp_error_paths(cli, tmp_path) -> None:
    out_dir = str(tmp_path / "out")

    # 图片不存在：找不到（3）。
    payload = cli(
        ["dotmatrix", "bmp", "--file", str(tmp_path / "nope.bmp"), "--out-dir", out_dir]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"

    # 不是有效图片：输入非法（3）。
    broken = tmp_path / "broken.bmp"
    broken.write_bytes(b"not an image")
    payload = cli(["dotmatrix", "bmp", "--file", str(broken), "--out-dir", out_dir])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    # 错误的导出格式、图片模式不适用分页参数、缺必填项：用法错误（2）。
    payload = cli(
        [
            "dotmatrix",
            "bmp",
            "--file",
            str(broken),
            "--out-dir",
            out_dir,
            "--export-format",
            "png",
        ]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    payload = cli(
        ["dotmatrix", "bmp", "--file", str(broken), "--out-dir", out_dir, "--page-chars", "4"]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert cli(["dotmatrix", "bmp", "--out-dir", out_dir])["exit_code"] == 2
    assert cli(["dotmatrix", "bmp", "--file", str(broken)])["exit_code"] == 2


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


def test_batch_generates_one_c_file_per_image(cli, tmp_path) -> None:
    folder = tmp_path / "src"
    (folder / "nested").mkdir(parents=True)
    _bmp(folder / "a.bmp", 0)
    _bmp(folder / "b.bmp", 255)
    _bmp(folder / "nested" / "a.bmp", 0)

    out_dir = tmp_path / "out"
    payload = cli(
        ["dotmatrix", "batch", "--folder", str(folder), "--out-dir", str(out_dir)]
    )
    assert payload["exit_code"] == 0
    assert payload["command"] == "dotmatrix.batch"
    data = payload["data"]
    assert set(data) == BATCH_KEYS
    assert data["errors"] == {}
    assert data["pages"] == 2
    assert data["width"] == 8 and data["height"] == 8 and data["bytes"] == 16
    assert sorted(Path(path).name for path in data["files"]) == ["a.c", "b.c"]
    assert "const unsigned char a[]" in (out_dir / "a.c").read_text(encoding="utf-8")
    assert "const unsigned char b[]" in (out_dir / "b.c").read_text(encoding="utf-8")

    # 递归时同名 stem 冲突，第二个文件自动加下标而不是覆盖。
    payload = cli(
        [
            "dotmatrix",
            "batch",
            "--folder",
            str(folder),
            "--out-dir",
            str(tmp_path / "rec"),
            "--recursive",
        ]
    )
    assert payload["exit_code"] == 0
    names = sorted(Path(path).name for path in payload["data"]["files"])
    assert names == ["a.c", "a_1.c", "b.c"]
    assert payload["data"]["pages"] == 3


def test_batch_collects_errors_without_aborting(cli, tmp_path) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    good = _bmp(folder / "good.bmp", 0)
    bad = folder / "bad.bmp"
    bad.write_bytes(b"not an image")

    payload = cli(
        ["dotmatrix", "batch", "--folder", str(folder), "--out-dir", str(tmp_path / "out")]
    )
    assert payload["exit_code"] == 0
    data = payload["data"]
    assert set(data) == BATCH_KEYS
    # 好图照常写出 C，坏图只记入 errors。
    assert (tmp_path / "out" / "good.c").exists()
    assert data["files"] == [str(tmp_path / "out" / "good.c")]
    assert data["pages"] == 1 and data["bytes"] == 8
    assert list(data["errors"]) == [str(bad)]
    assert "无法读取图片" in data["errors"][str(bad)]
    assert str(good) not in data["errors"]


def test_batch_error_paths(cli, tmp_path) -> None:
    out_dir = str(tmp_path / "out")

    # 文件夹不存在：找不到（3）。
    payload = cli(
        ["dotmatrix", "batch", "--folder", str(tmp_path / "nope"), "--out-dir", out_dir]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"

    # 文件夹里没有图片：输入非法（3）。
    empty = tmp_path / "empty"
    empty.mkdir()
    payload = cli(
        ["dotmatrix", "batch", "--folder", str(empty), "--out-dir", out_dir]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    # 全部图片都读不了：没有可用输出，仍是输入非法（3）。
    broken_dir = tmp_path / "broken"
    broken_dir.mkdir()
    (broken_dir / "bad.bmp").write_bytes(b"not an image")
    payload = cli(
        ["dotmatrix", "batch", "--folder", str(broken_dir), "--out-dir", out_dir]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    # 缺 --out-dir：用法错误（2）。
    assert cli(["dotmatrix", "batch", "--folder", str(empty)])["exit_code"] == 2

    # 批量只产出 C，没有 --export-format：未知参数仍是用法错误（2）。
    payload = cli(
        ["dotmatrix", "batch", "--folder", str(empty), "--out-dir", out_dir, "--export-format", "bin"]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# 导入开销
# ---------------------------------------------------------------------------


def test_dotmatrix_module_import_stays_light() -> None:
    """命令模块按需导入核心模块：导入它不能连带加载 Pillow/numpy/PyQt6。"""
    code = (
        "import sys\n"
        "import app.cli.commands.dotmatrix\n"
        "print(sorted(name for name in ('PIL', 'numpy', 'PyQt6') if name in sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"

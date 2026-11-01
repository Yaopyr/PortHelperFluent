"""``convert`` 数值与文本子命令：base/binhex/sevenseg/palette。

换算算法本身由 ``tests/test_encoding.py``、``tests/test_color.py`` 覆盖；本文件只
验证 CLI 契约：参数组合、``data`` 字段形状、错误码与退出码。
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 红：0xFF 转 RGB565 为 0xF800，转 RGB332 为 0xE0。
RED_565 = "F800"
RED_332 = "E0"


class _FakeStdin:
    """替身标准输入：只提供 ``buffer``，与 ``read_input`` 的读取方式一致。"""

    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


# ---------------------------------------------------------------------------
# base
# ---------------------------------------------------------------------------


def test_base_conversion(cli) -> None:
    data = cli(["convert", "base", "--value", "255", "--from-base", "10", "--bits", "8"])["data"]
    assert data["outputs"]["16"] == "FF" and data["outputs"]["2"] == "11111111"
    assert data["outputs"]["8"] == "377" and data["outputs"]["10"] == "255"


def test_base_envelope_and_data_shape(cli) -> None:
    payload = cli(["convert", "base", "--value", "255", "--from-base", "10"])
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["command"] == "convert.base"

    data = payload["data"]
    assert set(data) == {"input", "from_base", "bits", "signed", "outputs"}
    assert data["input"] == "255"
    assert data["from_base"] == 10
    assert data["bits"] == 32
    assert data["signed"] is False
    assert set(data["outputs"]) == {"2", "8", "10", "16"}


def test_base_signed_uses_twos_complement(cli) -> None:
    data = cli(
        ["convert", "base", "--value", "0xFF", "--from-base", "16", "--bits", "8", "--signed"]
    )["data"]
    assert data["signed"] is True
    assert data["outputs"]["10"] == "-1"
    assert data["outputs"]["16"] == "FF"


def test_base_error_paths(cli) -> None:
    # 数值超出位宽：输入非法（3/invalid_input）。
    payload = cli(["convert", "base", "--value", "511", "--from-base", "10", "--bits", "8"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    payload = cli(["convert", "base", "--value", "abc", "--from-base", "2"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    # 参数本身越界：用法错误（2/usage），不是输入内容问题。
    for argv in (
        ["--from-base", "40"],
        ["--from-base", "1"],
        ["--bits", "0"],
        ["--bits", "100"],
    ):
        payload = cli(["convert", "base", "--value", "1", *argv])
        assert payload["exit_code"] == 2
        assert payload["error"]["code"] == "usage"

    # 缺少 --value 由 argparse 报用法错误。
    payload = cli(["convert", "base", "--from-base", "10"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# binhex
# ---------------------------------------------------------------------------


def test_binhex_three_modes(cli, tmp_path) -> None:
    assert cli(["convert", "binhex", "--mode", "bin-to-hex", "--hex", "01 02"])["data"]["hex_text"] == "01 02"
    assert cli(["convert", "binhex", "--mode", "hex-to-text", "--hex", "E4 B8 AD"])["data"]["text"] == "中"
    out = tmp_path / "x.bin"
    assert cli(["convert", "binhex", "--mode", "hex-to-bytes", "--hex", "01 02", "--out", str(out)])["data"]["size"] == 2


def test_binhex_data_shape_per_mode(cli, tmp_path) -> None:
    data = cli(["convert", "binhex", "--mode", "bin-to-hex", "--hex", "01 02"])["data"]
    assert set(data) == {"mode", "size", "hex_text"}
    assert data["size"] == 2

    data = cli(["convert", "binhex", "--mode", "hex-to-text", "--hex", "E4 B8 AD"])["data"]
    assert set(data) == {"mode", "size", "encoding", "text"}
    assert data["encoding"] == "utf-8"
    assert data["size"] == 3

    data = cli(["convert", "binhex", "--mode", "hex-to-bytes", "--hex", "01 02"])["data"]
    assert set(data) == {"mode", "size", "hex", "out"}
    assert data["size"] == 2
    assert data["hex"] == "01 02"
    assert data["out"] is None

    out = tmp_path / "x.bin"
    data = cli(
        ["convert", "binhex", "--mode", "hex-to-bytes", "--hex", "01 02", "--out", str(out)]
    )["data"]
    assert data["out"] == str(out)
    assert out.read_bytes() == b"\x01\x02"


def test_binhex_hex_to_bytes_decodes_text_and_stdin(cli, tmp_path, monkeypatch) -> None:
    """--text/--stdin 给的是十六进制文本，hex-to-bytes 必须先解析再输出原始字节。"""
    out = tmp_path / "x.bin"
    text_data = cli(
        ["convert", "binhex", "--mode", "hex-to-bytes", "--text", "01 02", "--out", str(out)]
    )["data"]
    assert text_data["size"] == 2
    assert text_data["hex"] == "01 02"
    assert out.read_bytes() == b"\x01\x02"

    monkeypatch.setattr(sys, "stdin", _FakeStdin(b"01 02"))
    stdin_data = cli(["convert", "binhex", "--mode", "hex-to-bytes", "--stdin"])["data"]
    assert stdin_data["size"] == 2
    assert stdin_data["hex"] == "01 02"

    # 文本不是合法十六进制时按输入内容非法处理。
    payload = cli(["convert", "binhex", "--mode", "hex-to-bytes", "--text", "hello"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"


def test_binhex_writes_only_raw_bytes_for_hex_to_bytes(cli, tmp_path) -> None:
    """bin-to-hex 也给 --out 时不应写文件：--out 只属于 hex-to-bytes。"""
    out = tmp_path / "x.bin"
    payload = cli(
        ["convert", "binhex", "--mode", "bin-to-hex", "--hex", "01 02", "--out", str(out)]
    )
    assert payload["exit_code"] == 0
    assert not out.exists()


def test_binhex_formats_16_bytes_per_line(cli) -> None:
    data = cli(["convert", "binhex", "--mode", "bin-to-hex", "--hex", "00 " * 20])["data"]
    lines = data["hex_text"].split("\n")
    assert len(lines) == 2
    assert len(lines[0].split(" ")) == 16
    assert len(lines[1].split(" ")) == 4


def test_binhex_encoding_and_errors(cli) -> None:
    data = cli(
        ["convert", "binhex", "--mode", "hex-to-text", "--hex", "D6 D0", "--encoding", "gbk"]
    )["data"]
    assert data["text"] == "中"
    assert data["encoding"] == "gbk"

    payload = cli(["convert", "binhex", "--mode", "hex-to-text", "--hex", "0x0"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    payload = cli(
        ["convert", "binhex", "--mode", "hex-to-text", "--hex", "41", "--encoding", "no-such-codec"]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    payload = cli(["convert", "binhex", "--mode", "bin-to-hex"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    payload = cli(["convert", "binhex", "--hex", "01"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# sevenseg
# ---------------------------------------------------------------------------


def test_sevenseg_char_and_segments(cli) -> None:
    data = cli(["convert", "sevenseg", "--char", "0"])["data"]
    assert data["cathode"]["hex"] == "3F" and data["anode"]["hex"] == "C0"


def test_sevenseg_data_shape(cli) -> None:
    data = cli(["convert", "sevenseg", "--char", "0"])["data"]
    assert set(data) == {"char", "segments", "cathode", "anode"}
    assert data["char"] == "0"
    assert data["segments"] == ["a", "b", "c", "d", "e", "f"]
    for side in ("cathode", "anode"):
        assert set(data[side]) == {"code", "hex", "bin", "segments"}
    assert data["cathode"]["code"] == 0x3F
    assert data["cathode"]["bin"] == "00111111"
    assert data["anode"]["code"] == 0xC0
    assert data["anode"]["bin"] == "11000000"
    assert data["anode"]["segments"] == data["cathode"]["segments"]


def test_sevenseg_segments_input(cli) -> None:
    data = cli(["convert", "sevenseg", "--segments", "a,b,c,d,e,f"])["data"]
    assert data["char"] is None
    assert data["cathode"]["hex"] == "3F" and data["anode"]["hex"] == "C0"

    data = cli(["convert", "sevenseg", "--segments", "dp,a"])["data"]
    assert data["cathode"]["code"] == 0x81


def test_sevenseg_errors(cli) -> None:
    payload = cli(["convert", "sevenseg", "--char", "Z"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    # 段名不合法、段列表为空都按输入内容非法处理。
    for argv in (["--segments", "x"], ["--segments", "a z"], ["--segments", ","]):
        payload = cli(["convert", "sevenseg", *argv])
        assert payload["exit_code"] == 3
        assert payload["error"]["code"] == "invalid_input"

    # --char 与 --segments 互斥且必须给一个。
    payload = cli(["convert", "sevenseg"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    payload = cli(["convert", "sevenseg", "--char", "0", "--segments", "a"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# palette
# ---------------------------------------------------------------------------


def test_palette_forward_and_decode(cli) -> None:
    data = cli(["convert", "palette", "--color", "#FF0000"])["data"]
    assert data["rgb565"]["hex"] == "F800" and data["rgb332"]["hex"] == "E0"
    assert cli(["convert", "palette", "--decode", "0xF800", "--decode-format", "rgb565"])["data"]["rgb"] == [255, 0, 0]


def test_palette_forward_data_shape(cli) -> None:
    data = cli(["convert", "palette", "--color", "#FF0000"])["data"]
    assert set(data) == {"source", "rgb", "hex", "rgb888", "rgb565", "rgb332"}
    assert data["source"] == "color"
    assert data["rgb"] == [255, 0, 0]
    assert data["hex"] == "#FF0000"
    assert data["rgb888"] == {"value": 0xFF0000, "hex": "FF0000"}
    assert data["rgb565"] == {"value": 0xF800, "hex": RED_565}
    assert data["rgb332"] == {"value": 0xE0, "hex": RED_332}


def test_palette_rgb_input(cli) -> None:
    data = cli(["convert", "palette", "--rgb", "255, 0, 0"])["data"]
    assert data["source"] == "rgb"
    assert data["hex"] == "#FF0000"


def test_palette_decode_all_formats(cli) -> None:
    data = cli(["convert", "palette", "--decode", "0xF800", "--decode-format", "rgb565"])["data"]
    assert data["source"] == "decode"
    assert data["rgb"] == [255, 0, 0]
    assert data["hex"] == "#FF0000"
    assert data["rgb565"] == {"value": 0xF800, "hex": RED_565}

    data = cli(["convert", "palette", "--decode", "0xE0", "--decode-format", "rgb332"])["data"]
    assert data["rgb"] == [255, 0, 0]
    assert data["rgb332"] == {"value": 0xE0, "hex": RED_332}

    data = cli(["convert", "palette", "--decode", "0xFF0000", "--decode-format", "rgb888"])["data"]
    assert data["rgb"] == [255, 0, 0]
    assert data["rgb888"] == {"value": 0xFF0000, "hex": "FF0000"}


def test_palette_errors(cli) -> None:
    # --color/--rgb/--decode 必须三选一。
    payload = cli(["convert", "palette"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    payload = cli(["convert", "palette", "--color", "#FF0000", "--decode", "0xF800"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    # 解码方向必须给出源编码，源编码也只能给解码方向。
    payload = cli(["convert", "palette", "--decode", "0xF800"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    payload = cli(["convert", "palette", "--color", "#FF0000", "--decode-format", "rgb565"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    # --format 仍是全局输出格式（json/text），rgb565 不再是合法取值。
    payload = cli(["convert", "palette", "--color", "#FF0000", "--format", "rgb565"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    # 颜色与通道值非法都按输入内容处理。
    payload = cli(["convert", "palette", "--color", "#GG0000"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    payload = cli(["convert", "palette", "--rgb", "255, 0"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    payload = cli(["convert", "palette", "--decode", "0x1FFFF", "--decode-format", "rgb565"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"


def test_palette_text_format_renders_text(cli) -> None:
    payload = cli(["convert", "palette", "--color", "#FF0000", "--format", "text"])
    assert payload["exit_code"] == 0
    assert payload["raw"].startswith("ok: true")
    assert "#FF0000" in payload["raw"]
    assert "F800" in payload["raw"]


# ---------------------------------------------------------------------------
# 组与导入契约
# ---------------------------------------------------------------------------


def test_convert_group_requires_action(cli) -> None:
    payload = cli(["convert"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


def test_convert_commands_import_without_pyqt6() -> None:
    """convert 全路径只依赖标准库与 app.core，不能把 PyQt6 带进 CLI 进程。"""
    code = (
        "import sys\n"
        "import app.cli.commands.convert\n"
        "import app.cli.main\n"
        "print('PyQt6' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"

"""``convert`` 的数值与文本子命令：``base``、``binhex``、``sevenseg``、``palette``。

换算算法全部来自 :mod:`app.core.encoding` 与 :mod:`app.core.color`（与 GUI 同一套
实现），本模块只负责参数校验、方向选择与 JSON 字段：

- ``base``：按 ``--from-base`` 解析一次，输出 2/8/10/16 四种进制文本；
- ``binhex``：Bin→Hex、Hex→文本、Hex→原始字节三个方向，``--out`` 只属于最后一个；
- ``sevenseg``：给字符或点亮的段名，同时给出共阴/共阳的码值、十六进制与二进制；
- ``palette``：HEX/RGB 正向换算，以及 RGB565/RGB332/RGB888 反向解码。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.cli.errors import EXIT_OPERATION, EXIT_USAGE, CliError
from app.cli.inputs import add_input_options, read_input
from app.cli.output import add_output_options
from app.core.color import (
    RGB888_MAX,
    decode_332,
    decode_565,
    hex_to_rgb,
    rgb_to_332,
    rgb_to_565,
    rgb_to_888,
    rgb_to_hex,
)
from app.core.encoding import (
    SEVEN_SEGMENT_BITS,
    base_outputs,
    bin_file_to_hex_text,
    hex_text_to_text,
    seven_segment_code,
    seven_segment_segments,
)
from app.services.exports import write_bytes_atomic

__all__ = [
    "register",
    "run_base",
    "run_binhex",
    "run_palette",
    "run_sevenseg",
]

# 进制与位宽的取值范围；``base_outputs`` 本身对任意正整数位宽都能算，
# 这里只用范围拦住明显误用的参数（常用位宽是 8/16/32/64）。
BASE_MIN, BASE_MAX = 2, 36
BITS_MIN, BITS_MAX = 1, 64

# ``binhex --mode`` 的三个方向。
BINHEX_MODES = ("bin-to-hex", "hex-to-text", "hex-to-bytes")

# ``palette --format``：``json``/``text`` 是信封输出格式，其余是解码源编码。
# 同一选项承担两种语义，是为了让 ``--decode 0xF800 --format rgb565`` 与其它
# 叶子命令的 ``--format json|text`` 共存（argparse 不允许同名选项注册两次）。
PALETTE_FORMATS = ("json", "text", "rgb565", "rgb332", "rgb888")

_SEGMENT_SPLIT = re.compile(r"[,\s]+")


def register(subparsers) -> None:
    """把四个数值/文本子命令挂到 ``convert`` 的子命令解析器上。"""
    _register_base(subparsers)
    _register_binhex(subparsers)
    _register_sevenseg(subparsers)
    _register_palette(subparsers)


# ---------------------------------------------------------------------------
# base
# ---------------------------------------------------------------------------


def _register_base(subparsers) -> None:
    parser = subparsers.add_parser(
        "base",
        help="进制换算：一次给出 2/8/10/16 四种进制。",
        description="按 --from-base 解析 --value，输出 2/8/10/16 进制文本。",
    )
    parser.add_argument(
        "--value",
        required=True,
        metavar="VALUE",
        help="要转换的数值，可带符号与 0b/0o/0x 前缀。",
    )
    parser.add_argument(
        "--from-base",
        type=int,
        default=10,
        metavar="BASE",
        help="输入进制（2–36，默认 10）。",
    )
    parser.add_argument(
        "--bits",
        type=int,
        default=32,
        metavar="BITS",
        help="位宽（1–64，默认 32；常用 8/16/32/64）。",
    )
    parser.add_argument(
        "--signed",
        action="store_true",
        help="按二进制补码解释输入、显示十进制。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_base, command="convert.base")


def run_base(args) -> dict[str, Any]:
    """解析一次输入并返回四种进制输出；越界参数报 ``usage``，数值非法报 ``invalid_input``。"""
    text = getattr(args, "value", "")
    from_base = _range_arg("--from-base", getattr(args, "from_base", 10), BASE_MIN, BASE_MAX)
    bits = _range_arg("--bits", getattr(args, "bits", 32), BITS_MIN, BITS_MAX)
    signed = bool(getattr(args, "signed", False))
    try:
        outputs = base_outputs(text, from_base, bits=bits, signed=signed)
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
    return {
        "input": text,
        "from_base": from_base,
        "bits": bits,
        "signed": signed,
        "outputs": {str(base): rendered for base, rendered in outputs.items()},
    }


# ---------------------------------------------------------------------------
# binhex
# ---------------------------------------------------------------------------


def _register_binhex(subparsers) -> None:
    parser = subparsers.add_parser(
        "binhex",
        help="Bin/Hex/文本互转：bin-to-hex、hex-to-text、hex-to-bytes。",
        description="三种方向的互转；--out 只在 hex-to-bytes 时写出原始字节。",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=BINHEX_MODES,
        help="转换方向：bin-to-hex（字节→十六进制文本）、hex-to-text（十六进制→文本）、"
        "hex-to-bytes（十六进制→原始字节）。",
    )
    add_input_options(parser)
    parser.add_argument(
        "--encoding",
        default="utf-8",
        metavar="CODEC",
        help="hex-to-text 的解码编码，以及 --text/--file/--stdin 的编码（默认 utf-8）。",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="hex-to-bytes 的原始字节输出路径（先写临时文件再替换）。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_binhex, command="convert.binhex")


def run_binhex(args) -> dict[str, Any]:
    """按 ``--mode`` 做一次转换；``--out`` 只在 ``hex-to-bytes`` 时写文件。"""
    mode = getattr(args, "mode")
    source = read_input(args)

    if mode == "bin-to-hex":
        return {
            "mode": mode,
            "size": len(source.data),
            "hex_text": bin_file_to_hex_text(source.data),
        }

    if mode == "hex-to-text":
        try:
            text = hex_text_to_text(source.text, source.encoding)
        except ValueError as exc:
            raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
        return {
            "mode": mode,
            "size": len(source.data),
            "encoding": source.encoding,
            "text": text,
        }

    out_path = getattr(args, "out", None)
    written: str | None = None
    if out_path is not None:
        try:
            write_bytes_atomic(Path(out_path), source.data)
        except OSError as exc:
            raise CliError("permission_denied", f"写入文件失败：{exc}", EXIT_OPERATION) from exc
        written = str(out_path)
    return {"mode": mode, "size": len(source.data), "out": written}


# ---------------------------------------------------------------------------
# sevenseg
# ---------------------------------------------------------------------------


def _register_sevenseg(subparsers) -> None:
    parser = subparsers.add_parser(
        "sevenseg",
        help="七段数码管字型码：给字符或段名，输出共阴/共阳码值。",
        description="--char 与 --segments 二选一；同时输出共阴与共阳的码值、十六进制与二进制。",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--char", metavar="CHAR", help="单个字符 0–9 或 A–F。")
    group.add_argument(
        "--segments",
        metavar="SEGMENTS",
        help="点亮的段名，逗号/空白分隔，可选 a–g 与 dp（如 a,b,c,d,e,f）。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_sevenseg, command="convert.sevenseg")


def run_sevenseg(args) -> dict[str, Any]:
    """字符或段名 → 字型码；同时给出共阴（原码）与共阳（按位取反）的表示。"""
    char = getattr(args, "char", None)
    if char is not None:
        try:
            code = seven_segment_code(char, True)
        except ValueError as exc:
            raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
        label: str | None = char.strip().upper()
    else:
        code = _segments_code(getattr(args, "segments", "") or "")
        label = None

    anode_code = (~code) & 0xFF
    return {
        "char": label,
        "segments": seven_segment_segments(code, True),
        "cathode": _sevenseg_record(code),
        "anode": _sevenseg_record(anode_code, common_cathode=False),
    }


def _sevenseg_record(code: int, *, common_cathode: bool = True) -> dict[str, Any]:
    """一侧（共阴/共阳）的码值表示：``code``/``hex``/``bin``/``segments``。"""
    return {
        "code": code,
        "hex": f"{code:02X}",
        "bin": f"{code:08b}",
        "segments": seven_segment_segments(code, common_cathode),
    }


def _segments_code(text: str) -> int:
    """把 ``a,b,c`` 形式的段名按位或成字型码；段名非法或为空报 ``invalid_input``。"""
    names = [name for name in _SEGMENT_SPLIT.split(text) if name]
    if not names:
        raise CliError(
            "invalid_input",
            "--segments 至少需要一个段名（a–g 或 dp）。",
            EXIT_OPERATION,
        )
    code = 0
    for name in names:
        bit = SEVEN_SEGMENT_BITS.get(name.lower())
        if bit is None:
            raise CliError(
                "invalid_input",
                f"未知段名“{name}”，只支持 {', '.join(SEVEN_SEGMENT_BITS)}。",
                EXIT_OPERATION,
            )
        code |= bit
    return code


# ---------------------------------------------------------------------------
# palette
# ---------------------------------------------------------------------------


def _register_palette(subparsers) -> None:
    parser = subparsers.add_parser(
        "palette",
        help="调色板换算：HEX/RGB 正向换算与 RGB565/332/888 解码。",
        description="--color/--rgb/--decode 三选一；输出 rgb、hex 与三种编码的 value/hex。",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--color",
        metavar="#RRGGBB",
        help="颜色文本：支持 #RRGGBB、RRGGBB、0xRRGGBB 与 #RGB 简写。",
    )
    group.add_argument(
        "--rgb",
        metavar="R,G,B",
        help="颜色通道：三个 0–255 的整数，逗号分隔。",
    )
    group.add_argument(
        "--decode",
        metavar="VALUE",
        help="要解码的源值（十进制或 0x 前缀），必须与 --format rgb565/rgb332/rgb888 搭配。",
    )
    parser.add_argument(
        "--format",
        choices=PALETTE_FORMATS,
        default="json",
        help="输出格式：json（默认）或 text；配合 --decode 时表示源编码 "
        "rgb565/rgb332/rgb888，此时信封仍按 JSON 输出。",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="json 输出缩进 2 空格，便于人工阅读。",
    )
    parser.set_defaults(handler=run_palette, command="convert.palette")


def run_palette(args) -> dict[str, Any]:
    """三选一给出 RGB，再统一算出 hex 与 RGB888/565/332 的编码。"""
    color = getattr(args, "color", None)
    rgb_text = getattr(args, "rgb", None)
    decode_text = getattr(args, "decode", None)
    chosen_format = getattr(args, "format", "json") or "json"
    decode_format = chosen_format if chosen_format in _DECODERS else None

    if decode_text is not None:
        if decode_format is None:
            raise CliError(
                "usage",
                "--decode 需要 --format rgb565、rgb332 或 rgb888 指定源编码。",
                EXIT_USAGE,
            )
        rgb, origin = _decode_color(decode_text, decode_format), "decode"
    else:
        if decode_format is not None:
            raise CliError(
                "usage",
                "--format rgb565/rgb332/rgb888 只能与 --decode 一起使用。",
                EXIT_USAGE,
            )
        if color is not None:
            rgb, origin = _color_from_hex(color), "color"
        else:
            rgb, origin = _color_from_rgb(rgb_text), "rgb"

    return {
        "source": origin,
        "rgb": list(rgb),
        "hex": rgb_to_hex(rgb),
        "rgb888": _color_record(rgb_to_888(rgb), 24),
        "rgb565": _color_record(rgb_to_565(rgb), 16),
        "rgb332": _color_record(rgb_to_332(rgb), 8),
    }


def _decode_888(value: int) -> tuple[int, int, int]:
    """把 24 位 RGB888 拆成三个通道；``app.core.color`` 只有编码方向。"""
    if not 0 <= value <= RGB888_MAX:
        raise ValueError(f"RGB888 值必须在 0x000000–0x{RGB888_MAX:06X} 范围内。")
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


_DECODERS = {
    "rgb565": decode_565,
    "rgb332": decode_332,
    "rgb888": _decode_888,
}


def _decode_color(text: str, source_format: str) -> tuple[int, int, int]:
    """``--decode`` 数值 → RGB；数值写法非法或超出该编码范围都报 ``invalid_input``。"""
    try:
        value = int(str(text).strip(), 0)
    except ValueError as exc:
        raise CliError(
            "invalid_input",
            f"无法解析颜色值“{text}”：请使用十进制或 0x 前缀的十六进制。",
            EXIT_OPERATION,
        ) from exc
    try:
        return _DECODERS[source_format](value)
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc


def _color_from_hex(text: str) -> tuple[int, int, int]:
    try:
        return hex_to_rgb(text)
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc


def _color_from_rgb(text: str | None) -> tuple[int, int, int]:
    """把 ``R,G,B`` 解析成通道三元组；通道数与取值范围都借用 core 校验。"""
    parts = [part.strip() for part in (text or "").split(",")]
    if len(parts) != 3:
        raise CliError(
            "invalid_input",
            f"--rgb 需要三个逗号分隔的 0–255 整数，收到“{text}”。",
            EXIT_OPERATION,
        )
    try:
        channels = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise CliError(
            "invalid_input",
            f"--rgb 的通道必须是 0–255 的整数，收到“{text}”。",
            EXIT_OPERATION,
        ) from exc
    try:
        rgb_to_888(channels)
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
    return channels  # type: ignore[return-value]


def _color_record(value: int, width: int) -> dict[str, Any]:
    """一种编码的 ``value`` 与固定宽度的十六进制 ``hex``。"""
    return {"value": value, "hex": f"{value:0{width // 4}X}"}


# ---------------------------------------------------------------------------
# 公共
# ---------------------------------------------------------------------------


def _range_arg(name: str, value: int, minimum: int, maximum: int) -> int:
    """参数范围校验：越界说明参数写错（``usage``/2），不是输入内容问题。"""
    if not minimum <= value <= maximum:
        raise CliError(
            "usage",
            f"{name} 必须在 {minimum}–{maximum} 之间，收到 {value}。",
            EXIT_USAGE,
        )
    return value

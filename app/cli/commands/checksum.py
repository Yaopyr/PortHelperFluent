"""``checksum`` 命令：一次算出结果表里的全部校验值。

算法全部来自 :mod:`app.core.checksum`（与 GUI 同一套实现），本模块只负责参数、
过滤、展示格式与 JSON 字段：``value``/``dec`` 永远是掩码后的算术值，
``--byte-order little`` 只改变 ``hex``/``bin`` 的字节排列（对应 GUI 的「小端显示」）。
"""

from __future__ import annotations

from typing import Any

from app.cli.errors import EXIT_OPERATION, EXIT_USAGE, CliError
from app.cli.inputs import add_input_options, read_input
from app.cli.output import add_output_options
from app.core.checksum import (
    CRC_PRESETS,
    ChecksumResult,
    CrcAccumulator,
    CrcParams,
    compute_all,
    format_value,
    source_text,
)

__all__ = ["register", "run"]

# ``--byte-order`` 的可选值；只有这两种。
BYTE_ORDERS = ("big", "little")

# 自定义 CRC 在结果表里的固定名称。
CUSTOM_CRC_NAME = "Custom CRC"

# ``--custom-crc`` 的字段数与顺序：W,POLY,INIT,REFIN,REFOUT,XOROUT。
_CUSTOM_CRC_FIELDS = 6

_TRUE_WORDS = frozenset({"true", "1"})
_FALSE_WORDS = frozenset({"false", "0"})


def register(subparsers) -> None:
    """注册 ``checksum`` 子命令。"""
    parser = subparsers.add_parser(
        "checksum",
        help="计算累加和、XOR、奇偶、Intel HEX、TCP/IP 与 CRC 校验值。",
        description="计算结果表中的全部校验值；--only/--crc 过滤，--custom-crc 追加自定义 CRC。",
    )
    add_input_options(parser)
    parser.add_argument(
        "--encoding",
        default="utf-8",
        metavar="CODEC",
        help="--text/--file/--stdin 的文本编码（utf-8/gbk/ansi 等，默认 utf-8）。",
    )
    parser.add_argument(
        "--invert",
        action="store_true",
        help="按各项位宽取反（~value & mask）。",
    )
    parser.add_argument(
        "--byte-order",
        choices=BYTE_ORDERS,
        default="big",
        help="hex/bin 的展示字节序，默认 big；little 只影响展示。",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="NAME",
        help="按名称子串过滤结果（大小写不敏感，可多次）。",
    )
    parser.add_argument(
        "--crc",
        action="append",
        metavar="NAME",
        help="只在 CRC 预设中按名称子串过滤（大小写不敏感，可多次）。",
    )
    parser.add_argument(
        "--custom-crc",
        metavar="W,POLY,INIT,REFIN,REFOUT,XOROUT",
        help="追加一项自定义 CRC；REFIN/REFOUT 用 true/false/1/0。",
    )
    parser.add_argument(
        "--source",
        dest="source_name",
        metavar="NAME",
        help="额外返回该算法的 Python 实现示例（source_text）。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run, command="checksum")


def run(args) -> dict[str, Any]:
    """计算校验值并返回 ``data``；过滤无匹配时返回空 ``results``，不报错。"""
    source = read_input(args)
    byte_order = getattr(args, "byte_order", "big")
    invert = bool(getattr(args, "invert", False))

    results = list(compute_all(source.data, invert=invert))
    custom = getattr(args, "custom_crc", None)
    if custom is not None:
        results.append(_custom_result(custom, source.data, invert))
    results = _filter_results(results, getattr(args, "only", None), getattr(args, "crc", None))

    data: dict[str, Any] = {
        "size": len(source.data),
        "encoding": source.encoding,
        "inverted": invert,
        "byte_order": byte_order,
        "results": [_render_result(result, byte_order) for result in results],
    }
    source_name = getattr(args, "source_name", None)
    if source_name is not None:
        data["source"] = _source_preview(source_name)
    return data


def _custom_result(text: str, data: bytes, invert: bool) -> ChecksumResult:
    """按 ``--custom-crc`` 参数算一项，结果名固定为 ``Custom CRC``。"""
    params = _parse_custom_crc(text)
    accumulator = CrcAccumulator(params, CUSTOM_CRC_NAME)
    accumulator.update(data)
    value = accumulator.result()
    if invert:
        value = ~value & ((1 << params.width) - 1)
    return ChecksumResult(CUSTOM_CRC_NAME, value, params.width)


def _parse_custom_crc(text: str) -> CrcParams:
    """把 ``W,POLY,INIT,REFIN,REFOUT,XOROUT`` 解析成 :class:`CrcParams`。

    整数按 ``int(x, 0)`` 解析（支持 ``0x`` 前缀与十进制）；布尔只接受
    ``true/false/1/0``。字段数不对或数值非法一律转成 ``usage``/2。
    """
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != _CUSTOM_CRC_FIELDS:
        raise CliError(
            "usage",
            f"--custom-crc 需要 {_CUSTOM_CRC_FIELDS} 个逗号分隔的字段"
            f"（W,POLY,INIT,REFIN,REFOUT,XOROUT），收到 {len(parts)} 个。",
            EXIT_USAGE,
        )
    try:
        width = int(parts[0], 0)
        poly = int(parts[1], 0)
        init = int(parts[2], 0)
        refin = _parse_bool(parts[3], "REFIN")
        refout = _parse_bool(parts[4], "REFOUT")
        xorout = int(parts[5], 0)
    except ValueError as exc:
        raise CliError("usage", f"--custom-crc 参数无法解析：{exc}", EXIT_USAGE) from exc
    try:
        return CrcParams(width, poly, init, refin, refout, xorout)
    except ValueError as exc:
        raise CliError("usage", f"--custom-crc 参数非法：{exc}", EXIT_USAGE) from exc


def _parse_bool(text: str, field: str) -> bool:
    """``true/false/1/0``（大小写不敏感）转布尔；其他写法报参数错误。"""
    normalized = text.lower()
    if normalized in _TRUE_WORDS:
        return True
    if normalized in _FALSE_WORDS:
        return False
    raise ValueError(f"{field} 只接受 true/false/1/0，收到“{text}”")


def _filter_results(
    results: list[ChecksumResult],
    only: list[str] | None,
    crc: list[str] | None,
) -> list[ChecksumResult]:
    """``--only`` 过滤全部结果，``--crc`` 只保留匹配的 CRC 预设；都按子串匹配。"""
    only_patterns = [pattern.lower() for pattern in only or ()]
    crc_patterns = [pattern.lower() for pattern in crc or ()]

    def matches(name: str, patterns: list[str]) -> bool:
        lowered = name.lower()
        return any(pattern in lowered for pattern in patterns)

    kept: list[ChecksumResult] = []
    for result in results:
        if only_patterns and not matches(result.name, only_patterns):
            continue
        if crc_patterns and not (result.name in CRC_PRESETS and matches(result.name, crc_patterns)):
            continue
        kept.append(result)
    return kept


def _render_result(result: ChecksumResult, byte_order: str) -> dict[str, Any]:
    """一项结果的 JSON 表示；``hex``/``bin`` 按 ``byte_order`` 排列，``value`` 不变。"""
    display = _display_value(result.value, result.width, byte_order)
    return {
        "name": result.name,
        "width": result.width,
        "value": result.value,
        "dec": str(result.value),
        "hex": format_value(display, result.width, 16),
        "bin": format_value(display, result.width, 2),
    }


def _display_value(value: int, width: int, byte_order: str) -> int:
    """小端展示：位宽大于 8 时把值按字节反转；8 位及以下没有可反转的字节。"""
    if byte_order != "little" or width <= 8:
        return value
    raw = value.to_bytes(width // 8, "big")
    return int.from_bytes(raw, "little")


def _source_preview(name: str) -> str:
    """``--source``：取算法源码示例；未知名称转 ``not_found``/3。"""
    try:
        return source_text(name)
    except KeyError as exc:
        raise CliError(
            "not_found",
            f"没有算法“{name}”的源码示例。",
            EXIT_OPERATION,
        ) from exc

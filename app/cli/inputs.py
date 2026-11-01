"""CLI 输入源：``--text`` / ``--hex`` / ``--file`` / ``--stdin``。

四个选项互斥且必须给一个；解析结果统一成 :class:`InputData`，供命令模块拿字节、
文本与编码。``read_input`` 的失败一律转成 :class:`CliError`，不向外抛裸异常。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from app.cli.errors import EXIT_OPERATION, EXIT_USAGE, CliError
from app.core.encoding import decode_bytes, parse_hex, resolve_codec

__all__ = ["InputData", "add_input_options", "read_input"]


@dataclass(frozen=True)
class InputData:
    """一次输入解析结果。

    - ``data``：原始字节（``--text`` 按编码得到的字节，``--hex`` 解析后的字节）；
    - ``text``：``--text``/``--hex`` 保留原始文本，``--file``/``--stdin`` 按编码解码；
    - ``encoding``：解析后的 Python 编解码器名；
    - ``source``：``text``/``hex``/``file``/``stdin`` 之一。
    """

    data: bytes
    text: str
    encoding: str
    source: str


@dataclass(frozen=True)
class _Source:
    """互斥输入选项归一后的来源：``kind`` 是选项名，``value`` 是命令行给的原始值。"""

    kind: str
    value: str | None


def _source_type(kind: str):
    """生成 ``type=`` 转换器：把选项值包成带来源名的 :class:`_Source`。"""

    def _convert(value: str) -> _Source:
        return _Source(kind, value)

    return _convert


def add_input_options(parser, *, required: bool = True) -> None:
    """添加互斥输入选项，解析结果放在 ``args.source``。"""
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(
        "--text",
        dest="source",
        type=_source_type("text"),
        metavar="VALUE",
        help="文本输入，按 --encoding 编码。",
    )
    group.add_argument(
        "--hex",
        dest="source",
        type=_source_type("hex"),
        metavar="HEX",
        help="十六进制文本输入，支持空格/逗号/换行/0x/\\x。",
    )
    group.add_argument(
        "--file",
        dest="source",
        type=_source_type("file"),
        metavar="PATH",
        help="以原始字节读取文件。",
    )
    group.add_argument(
        "--stdin",
        dest="source",
        action="store_const",
        const=_Source("stdin", None),
        help="从标准输入读取原始字节。",
    )


def read_input(args) -> InputData:
    """按 ``args.source`` 读出输入数据；``--encoding`` 缺省为 utf-8。"""
    source = getattr(args, "source", None)
    if source is None:
        raise CliError(
            "usage",
            "必须提供 --text、--hex、--file 或 --stdin 之一。",
            EXIT_USAGE,
        )

    try:
        codec = resolve_codec(getattr(args, "encoding", None) or "utf-8")
    except ValueError as exc:
        raise CliError("usage", str(exc), EXIT_USAGE) from exc

    text_value = source.value or ""
    if source.kind == "text":
        try:
            data = text_value.encode(codec)
        except UnicodeEncodeError as exc:
            raise CliError(
                "invalid_input", f"文本无法按 {codec} 编码：{exc}", EXIT_OPERATION
            ) from exc
        return InputData(data=data, text=text_value, encoding=codec, source="text")

    if source.kind == "hex":
        try:
            data = parse_hex(text_value)
        except ValueError as exc:
            raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
        return InputData(data=data, text=text_value, encoding=codec, source="hex")

    if source.kind == "file":
        try:
            data = Path(text_value).read_bytes()
        except FileNotFoundError as exc:
            raise CliError(
                "not_found", f"找不到输入文件：{text_value}", EXIT_OPERATION
            ) from exc
        except OSError as exc:
            raise CliError(
                "invalid_input", f"读取输入文件 {text_value} 失败：{exc}", EXIT_OPERATION
            ) from exc
        return InputData(
            data=data, text=decode_bytes(data, codec), encoding=codec, source="file"
        )

    data = sys.stdin.buffer.read()
    return InputData(
        data=data, text=decode_bytes(data, codec), encoding=codec, source="stdin"
    )

"""文本编码与十六进制文本解析，供串口、网络与导出共用。"""

from __future__ import annotations

import codecs
import locale
import re

CODEC_CHOICES: tuple[str, ...] = ("ansi", "gbk", "utf-8")

_HEX_PREFIX_PATTERN = re.compile(r"0[xX]|\\[xX]")
_HEX_SEPARATOR_PATTERN = re.compile(r"[\s,;]+")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def resolve_codec(name: str) -> str:
    """把界面上的编码名解析为 Python 编解码器名。"""
    normalized = (name or "").strip().lower()
    if normalized == "ansi":
        # ANSI 表示系统当前代码页（简体中文 Windows 上为 cp936）。
        return locale.getpreferredencoding(False) or "utf-8"
    if normalized == "gbk":
        return "gbk"
    if normalized == "utf-8":
        return "utf-8"
    try:
        codecs.lookup(normalized)
    except LookupError as exc:
        raise ValueError(f"不支持的编码：{name}") from exc
    return normalized


def encode_text(text: str, codec: str) -> bytes:
    return text.encode(resolve_codec(codec))


def decode_bytes(data: bytes, codec: str, errors: str = "replace") -> str:
    return data.decode(resolve_codec(codec), errors=errors)


def parse_hex(text: str) -> bytes:
    """解析十六进制文本，支持 `0x`、`\\x` 前缀以及空格/逗号/分号/换行分隔。"""
    normalized = _HEX_PREFIX_PATTERN.sub(" ", text)
    result = bytearray()
    for token in _HEX_SEPARATOR_PATTERN.split(normalized):
        if not token:
            continue
        for char in token:
            if char not in _HEX_DIGITS:
                raise ValueError(f"十六进制内容包含非法字符“{char}”")
        if len(token) % 2:
            raise ValueError(f"十六进制内容“{token}”长度为奇数，请检查是否缺少半字节")
        result.extend(bytes.fromhex(token))
    return bytes(result)


def to_hex(data: bytes, sep: str = " ") -> str:
    return sep.join(f"{byte:02X}" for byte in data)

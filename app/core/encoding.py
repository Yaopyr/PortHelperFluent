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
    """解析十六进制文本，支持 `0x`、`\\x` 前缀以及空格/逗号/分号/换行分隔。

    以空白/逗号/分号为分隔符逐 token 解析：不含前缀的 token 必须整体是十六进制
    数字；含前缀的 token 可以是由多个 `\\x..` 片段拼接（如 `\\x01\\x02`）。
    前缀必须紧贴至少两位十六进制数字，因此 `0x`、`\\x`、`0x0` 等不完整输入，
    以及 `010x2` 这类把前缀嵌进数字中间的写法都会抛出中文 `ValueError`。
    `0x` 片段不能直接相邻拼接：`0x010x02` 中第二个前缀的 `0` 会被并入前一组
    数字，剩下孤立的 `x` 导致解析失败；多个 `0x` 片段请用空白/逗号分隔
    （如 `0x01 0x02`），而 `0x01\\x02` 这种 `\\x` 拼接是允许的。
    空字符串或纯分隔符返回 `b""`。
    """
    result = bytearray()
    for token in _HEX_SEPARATOR_PATTERN.split(text):
        if not token:
            continue
        if _HEX_PREFIX_PATTERN.match(token):
            result.extend(_parse_prefixed_token(token))
        else:
            result.extend(_parse_hex_token(token))
    return bytes(result)


def _parse_hex_token(token: str) -> bytes:
    """解析不含前缀的纯十六进制 token，错误信息带上 token 与非法字符。"""
    for char in token:
        if char not in _HEX_DIGITS:
            raise ValueError(f"十六进制内容“{token}”包含非法字符“{char}”")
    if len(token) % 2:
        raise ValueError(f"十六进制内容“{token}”长度为奇数，请检查是否缺少半字节")
    return bytes.fromhex(token)


def _parse_prefixed_token(token: str) -> bytes:
    """解析由一个或多个 `0x`/`\\x` 片段组成的 token。"""
    result = bytearray()
    position = 0
    while position < len(token):
        prefix = _HEX_PREFIX_PATTERN.match(token, position)
        if prefix is None:
            char = token[position]
            raise ValueError(f"十六进制内容“{token}”包含非法字符“{char}”")
        start = prefix.end()
        end = start
        while end < len(token) and token[end] in _HEX_DIGITS:
            end += 1
        if end < len(token) and not _HEX_PREFIX_PATTERN.match(token, end):
            # 数字后面紧跟非十六进制字符，直接指出这个字符，而不是笼统报奇数长度。
            char = token[end]
            raise ValueError(f"十六进制内容“{token}”包含非法字符“{char}”")
        digits = token[start:end]
        if not digits:
            prefix_text = token[position:start]
            raise ValueError(f"十六进制前缀“{prefix_text}”后缺少十六进制数字（内容：“{token}”）")
        if len(digits) % 2:
            raise ValueError(f"十六进制内容“{token}”长度为奇数，请检查是否缺少半字节")
        result.extend(bytes.fromhex(digits))
        position = end
    return bytes(result)


def to_hex(data: bytes, sep: str = " ") -> str:
    return sep.join(f"{byte:02X}" for byte in data)

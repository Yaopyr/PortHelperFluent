"""文本编码、十六进制文本、进制换算与七段数码管字型，供各页面共用。"""

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


# ---------------------------------------------------------------------------
# 进制转换
# ---------------------------------------------------------------------------

BASE_OUTPUT_BASES: tuple[int, ...] = (2, 8, 10, 16)
_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PREFIX_BY_BASE = {2: "0b", 8: "0o", 16: "0x"}


def _validate_base(base: int) -> int:
    if isinstance(base, bool) or not isinstance(base, int):
        raise ValueError(f"进制必须是 2–36 的整数，收到 {base!r}")
    if not 2 <= base <= 36:
        raise ValueError(f"进制必须在 2–36 之间，收到 {base}")
    return base


def _validate_bits(bits: int) -> int:
    if isinstance(bits, bool) or not isinstance(bits, int) or bits <= 0:
        raise ValueError(f"位宽必须是正整数，收到 {bits!r}")
    return bits


def parse_int(text: str, base: int, bits: int = 32, signed: bool = False) -> int:
    """按指定进制解析整数，并按位宽/符号语义返回数值。

    支持可选的 ``+``/``-`` 符号以及与本进制匹配的 ``0b``/``0o``/``0x`` 前缀
    （大小写均可）。无符号模式只接受 0 到 ``2**bits - 1``；有符号模式接受
    ``-2**(bits-1)`` 到 ``2**bits - 1``，其中大于等于 ``2**(bits-1)`` 的输入
    按二进制补码解释（例如 8 位的 ``0xFF`` 返回 ``-1``）。空输入、非法字符与
    超出位宽都会抛出中文 ``ValueError``。
    """
    base = _validate_base(base)
    bits = _validate_bits(bits)
    if not isinstance(text, str):
        raise ValueError("数值必须是文本")

    original = text.strip()
    if not original:
        raise ValueError("数值不能为空，请输入要转换的数字。")

    body = original
    negative = False
    if body[:1] in ("+", "-"):
        negative = body[0] == "-"
        body = body[1:].strip()
        if not body:
            raise ValueError(f"数值“{original}”缺少数字部分。")

    prefix = _PREFIX_BY_BASE.get(base)
    if prefix is not None and body[:2].lower() == prefix:
        body = body[2:].strip()
        if not body:
            raise ValueError(f"数值“{original}”缺少数字部分。")

    try:
        value = int(body, base)
    except ValueError as exc:
        raise ValueError(
            f"无法按 {base} 进制解析“{original}”，"
            "请检查字符是否属于该进制或是否含有多余空格。"
        ) from exc

    if negative:
        value = -value

    limit = 1 << bits
    if signed:
        minimum = -(1 << (bits - 1))
        if value < minimum or value > limit - 1:
            raise ValueError(
                f"数值 {value} 超出 {bits} 位有符号整数范围"
                f"（{minimum} ~ {limit - 1}）。"
            )
        if value >= (1 << (bits - 1)):
            value -= limit
        return value

    if value < 0 or value > limit - 1:
        raise ValueError(
            f"数值 {value} 超出 {bits} 位无符号整数范围（0 ~ {limit - 1}）。"
        )
    return value


def _format_unsigned(value: int, base: int) -> str:
    if value == 0:
        return "0"
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, base)
        digits.append(_DIGITS[remainder])
    return "".join(reversed(digits))


def format_int(value: int, base: int, bits: int = 32, signed: bool = False) -> str:
    """按位宽掩码后输出指定进制的文本。

    二进制/八进制/十六进制显示补码位模式并统一使用大写字母；十进制在有符号
    模式下显示补码对应的负值，无符号模式下显示掩码后的数值。
    """
    base = _validate_base(base)
    bits = _validate_bits(bits)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"数值必须是整数，收到 {value!r}") from exc

    mask = (1 << bits) - 1
    masked = number & mask
    if signed and base == 10 and masked >= (1 << (bits - 1)):
        return str(masked - (1 << bits))
    return _format_unsigned(masked, base)


def base_outputs(
    text: str, from_base: int, bits: int = 32, signed: bool = False
) -> dict[int, str]:
    """解析一次输入，返回二进制/八进制/十进制/十六进制四种输出。"""
    value = parse_int(text, from_base, bits=bits, signed=signed)
    return {
        base: format_int(value, base, bits=bits, signed=signed)
        for base in BASE_OUTPUT_BASES
    }


# ---------------------------------------------------------------------------
# 七段数码管
# ---------------------------------------------------------------------------

SEVEN_SEGMENT_BIT_ORDER: tuple[str, ...] = (
    "a",
    "b",
    "c",
    "d",
    "e",
    "f",
    "g",
    "dp",
)
SEVEN_SEGMENT_BITS: dict[str, int] = {
    "a": 0x01,
    "b": 0x02,
    "c": 0x04,
    "d": 0x08,
    "e": 0x10,
    "f": 0x20,
    "g": 0x40,
    "dp": 0x80,
}

# 段位 a=bit0、b=bit1……g=bit6、dp=bit7；0–9/A–F 不点亮小数点。
SEVEN_SEG_COMMON_CATHODE: dict[str, int] = {
    "0": 0x3F,
    "1": 0x06,
    "2": 0x5B,
    "3": 0x4F,
    "4": 0x66,
    "5": 0x6D,
    "6": 0x7D,
    "7": 0x07,
    "8": 0x7F,
    "9": 0x6F,
    "A": 0x77,
    "B": 0x7C,
    "C": 0x39,
    "D": 0x5E,
    "E": 0x79,
    "F": 0x71,
}


def seven_segment_code(char: str, common_cathode: bool = True) -> int:
    """返回单个字符 0–9/A–F 的七段字型码；共阳为共阴的 8 位按位取反。"""
    if not isinstance(char, str):
        raise ValueError("数码管输入必须是单个字符")
    text = char.strip()
    if len(text) != 1:
        raise ValueError(f"请输入单个字符（0–9 或 A–F），收到“{char}”")
    key = text.upper()
    try:
        code = SEVEN_SEG_COMMON_CATHODE[key]
    except KeyError as exc:
        raise ValueError(f"字符“{text}”没有七段字型码，只支持 0–9 与 A–F") from exc
    return code if common_cathode else (~code) & 0xFF


def seven_segment_segments(code: int, common_cathode: bool = True) -> list[str]:
    """把字型码拆成点亮的段名列表（a–g/dp）；共阳输入会先按位取反。"""
    try:
        value = int(code) & 0xFF
    except (TypeError, ValueError) as exc:
        raise ValueError(f"字型码必须是整数，收到 {code!r}") from exc
    if not common_cathode:
        value = (~value) & 0xFF
    return [
        name
        for name in SEVEN_SEGMENT_BIT_ORDER
        if value & SEVEN_SEGMENT_BITS[name]
    ]


# ---------------------------------------------------------------------------
# Bin/Hex/Text 文件辅助
# ---------------------------------------------------------------------------

HEX_TEXT_BYTES_PER_LINE = 16


def _bytes_to_hex_text(data: bytes) -> str:
    """把字节格式化为大写十六进制文本，每行 16 字节、以空格分隔。"""
    raw = bytes(data)
    if not raw:
        return ""
    lines = [
        " ".join(f"{byte:02X}" for byte in raw[start : start + HEX_TEXT_BYTES_PER_LINE])
        for start in range(0, len(raw), HEX_TEXT_BYTES_PER_LINE)
    ]
    return "\n".join(lines)


def bin_file_to_hex_text(data: bytes) -> str:
    """二进制文件内容转大写十六进制文本（每行 16 字节）。"""
    return _bytes_to_hex_text(data)


def hex_text_to_bytes(text: str) -> bytes:
    """十六进制文本转原始字节；语法与错误提示沿用 :func:`parse_hex`。"""
    return parse_hex(text)


def hex_text_to_text(text: str, codec: str) -> str:
    """十六进制文本按指定编码解码为文本（无法解码的字节替换为 U+FFFD）。"""
    return decode_bytes(hex_text_to_bytes(text), codec)


def text_to_hex_text(data: bytes) -> str:
    """字节内容转大写十六进制文本（每行 16 字节），与 Bin→Hex 格式一致。"""
    return _bytes_to_hex_text(data)

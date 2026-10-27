"""颜色编码转换：HEX、RGB888、RGB565 与 RGB332。

这些函数是纯 Python，供调色板工具与图片取模等模块共用；颜色一律用
``(r, g, b)`` 三元组表示，每个分量取值 0–255。
"""

from __future__ import annotations

import string
from typing import Sequence

RGB565_MAX = 0xFFFF
RGB332_MAX = 0xFF
RGB888_MAX = 0xFFFFFF
_HEX_DIGITS = frozenset(string.hexdigits)


def hex_to_rgb(text: str) -> tuple[int, int, int]:
    """把 ``#RRGGBB``/``RRGGBB``/``#RGB`` 解析为 RGB 三元组。

    允许省略 ``#``、使用 ``0x`` 前缀或使用大小写混合的十六进制数字；
    3 位简写按 ``#RGB`` 展开。格式非法时抛出中文 ``ValueError``。
    """
    original = str(text).strip()
    body = original
    if body.startswith("#"):
        body = body[1:]
    elif body[:2].lower() == "0x":
        body = body[2:]
    if len(body) == 3:
        body = "".join(char * 2 for char in body)
    if len(body) != 6 or any(char not in _HEX_DIGITS for char in body):
        raise ValueError(
            f"无法解析颜色“{original}”：请使用 #RRGGBB、RRGGBB 或 #RGB 格式。"
        )
    return int(body[0:2], 16), int(body[2:4], 16), int(body[4:6], 16)


def rgb_to_hex(rgb: Sequence[int]) -> str:
    """把 RGB 三元组格式化为大写 ``#RRGGBB``。"""
    red, green, blue = _validated_rgb(rgb)
    return f"#{red:02X}{green:02X}{blue:02X}"


def rgb_to_565(rgb: Sequence[int]) -> int:
    """把 RGB 编码为 16 位 RGB565（rrrrrggggggbbbbb）。"""
    red, green, blue = _validated_rgb(rgb)
    return ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)


def decode_565(value: int) -> tuple[int, int, int]:
    """把 16 位 RGB565 解码为 RGB 三元组。

    使用位复制补足低位，使解码后的颜色尽量接近原始颜色，并且重新编码后
    能得到同一个 565 值。
    """
    value = _validated_value(value, RGB565_MAX, "RGB565")
    red5 = (value >> 11) & 0x1F
    green6 = (value >> 5) & 0x3F
    blue5 = value & 0x1F
    red = (red5 << 3) | (red5 >> 2)
    green = (green6 << 2) | (green6 >> 4)
    blue = (blue5 << 3) | (blue5 >> 2)
    return red, green, blue


def rgb_to_332(rgb: Sequence[int]) -> int:
    """把 RGB 编码为 8 位 RGB332（rrrgggbb）。"""
    red, green, blue = _validated_rgb(rgb)
    return ((red >> 5) << 5) | ((green >> 5) << 2) | (blue >> 6)


def decode_332(value: int) -> tuple[int, int, int]:
    """把 8 位 RGB332 解码为 RGB 三元组（位复制补足低位）。"""
    value = _validated_value(value, RGB332_MAX, "RGB332")
    red3 = (value >> 5) & 0x07
    green3 = (value >> 2) & 0x07
    blue2 = value & 0x03
    red = (red3 << 5) | (red3 << 2) | (red3 >> 1)
    green = (green3 << 5) | (green3 << 2) | (green3 >> 1)
    blue = (blue2 << 6) | (blue2 << 4) | (blue2 << 2) | blue2
    return red, green, blue


def rgb_to_888(rgb: Sequence[int]) -> int:
    """把 RGB 编码为 24 位 RGB888（0xRRGGBB）。"""
    red, green, blue = _validated_rgb(rgb)
    return (red << 16) | (green << 8) | blue


def _validated_rgb(rgb: Sequence[int]) -> tuple[int, int, int]:
    try:
        values = tuple(int(component) for component in rgb)
    except (TypeError, ValueError) as exc:
        raise ValueError("RGB 必须是三个 0–255 的整数。") from exc
    if len(values) != 3:
        raise ValueError("RGB 必须是三个 0–255 的整数。")
    for component in values:
        if not 0 <= component <= 255:
            raise ValueError(f"RGB 分量 {component} 超出 0–255 范围。")
    return values  # type: ignore[return-value]


def _validated_value(value: int, maximum: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 值必须是整数。") from exc
    if not 0 <= number <= maximum:
        raise ValueError(f"{label} 值必须在 0x0000–0x{maximum:04X} 范围内。")
    return number

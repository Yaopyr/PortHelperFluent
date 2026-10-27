"""二维码生成与保存（不做扫码识别）。"""

from __future__ import annotations

from pathlib import Path

import qrcode
from PIL import Image
from qrcode.constants import (
    ERROR_CORRECT_H,
    ERROR_CORRECT_L,
    ERROR_CORRECT_M,
    ERROR_CORRECT_Q,
)

from app.core.color import hex_to_rgb

EC_LEVELS = {
    "L": ERROR_CORRECT_L,
    "M": ERROR_CORRECT_M,
    "Q": ERROR_CORRECT_Q,
    "H": ERROR_CORRECT_H,
}
SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".bmp"})
_MAX_BOX_SIZE = 64
_MAX_BORDER = 32


def make_qr(
    text: str,
    ec: str = "M",
    box_size: int = 8,
    border: int = 4,
    fg: str = "#000000",
    bg: str = "#FFFFFF",
) -> Image.Image:
    """根据文本生成 RGBA 二维码图像。

    ``ec`` 为纠错等级 L/M/Q/H；``box_size`` 是每个模块的像素数；``border``
    是静区宽度（模块数）。文本为空、参数非法或颜色无法解析时抛出中文
    ``ValueError``。
    """
    content = str(text)
    if not content.strip():
        raise ValueError("二维码内容不能为空：请输入要编码的文本。")
    level = _normalized_ec(ec)
    module_size = _bounded_int(box_size, "模块尺寸", minimum=1, maximum=_MAX_BOX_SIZE)
    quiet_zone = _bounded_int(border, "边框宽度", minimum=0, maximum=_MAX_BORDER)
    foreground = hex_to_rgb(fg)
    background = hex_to_rgb(bg)

    qr = qrcode.QRCode(
        version=None,
        error_correction=level,
        box_size=module_size,
        border=quiet_zone,
    )
    qr.add_data(content)
    qr.make(fit=True)
    try:
        base = qr.make_image(fill_color="black", back_color="white").get_image()
    except Exception as exc:  # qrcode/Pillow 的异常类型不稳定，统一转为中文提示
        raise ValueError(f"生成二维码失败：{exc}") from exc
    mask = base.convert("L").point(lambda value: 255 if value < 128 else 0).convert("1")
    image = Image.new("RGBA", base.size, (*background, 255))
    image.paste((*foreground, 255), (0, 0, base.width, base.height), mask)
    return image


def save_qr(image: Image.Image, path: Path) -> None:
    """把二维码保存为 PNG 或 BMP；BMP 不支持透明通道，保存前转为 RGB。"""
    destination = Path(path)
    suffix = destination.suffix.casefold()
    if suffix not in SUPPORTED_IMAGE_SUFFIXES:
        shown = suffix or "（无扩展名）"
        raise ValueError(
            f"不支持的图片格式：{shown}。请使用 .png 或 .bmp 扩展名。"
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".bmp":
            image.convert("RGB").save(destination, format="BMP")
        else:
            image.save(destination, format="PNG")
    except OSError as exc:
        raise OSError(f"保存二维码失败：{destination}（{exc}）。") from exc


def _normalized_ec(ec: str) -> int:
    key = str(ec).strip().upper()
    if key not in EC_LEVELS:
        raise ValueError("纠错等级必须是 L、M、Q 或 H。")
    return EC_LEVELS[key]


def _bounded_int(value: int, label: str, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数。") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{label}必须在 {minimum}–{maximum} 之间。")
    return number

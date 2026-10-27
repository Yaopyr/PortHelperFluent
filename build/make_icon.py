"""生成 PortHelper Fluent 的应用图标（build/icon.ico）。

图标完全由本脚本绘制：Win11 风格圆角方块、蓝色渐变、白色终端窗口与波形符号，
不使用原程序的任何图标或二进制资源。运行方式::

    python build/make_icon.py

脚本会输出 16/24/32/48/64/128/256 多尺寸 ICO，供窗口标题栏、任务栏与打包使用。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

CANVAS = 1024
SIZES = (16, 24, 32, 48, 64, 128, 256)
TOP_COLOR = (58, 140, 255)
BOTTOM_COLOR = (9, 71, 180)
WHITE = (255, 255, 255, 255)


def _vertical_gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    """生成自上而下的线性渐变；先画 1 像素宽再放大，避免逐像素循环整张图。"""
    column = Image.new("RGB", (1, size))
    for y in range(size):
        ratio = y / max(1, size - 1)
        column.putpixel(
            (0, y),
            tuple(round(start + (end - start) * ratio) for start, end in zip(top, bottom)),
        )
    return column.resize((size, size), Image.Resampling.BILINEAR)


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=radius, fill=255
    )
    return mask


def _draw_highlight(image: Image.Image, mask: Image.Image) -> None:
    """顶部一层很淡的高光，让图标更接近 Win11 的亚克力质感。"""
    highlight = Image.new("RGBA", image.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(highlight)
    for y in range(image.size[1] // 2):
        alpha = round(46 * (1 - y / (image.size[1] / 2)))
        draw.line((0, y, image.size[0], y), fill=(255, 255, 255, alpha))
    image.alpha_composite(Image.composite(highlight, Image.new("RGBA", image.size, (0, 0, 0, 0)), mask))


def _draw_glyph(image: Image.Image) -> None:
    """白色终端窗口、命令提示符与波形，整体按 1024 画布坐标绘制。"""
    draw = ImageDraw.Draw(image)
    scale = image.size[0] / CANVAS

    def point(x: int, y: int) -> tuple[int, int]:
        return (round(x * scale), round(y * scale))

    stroke = max(2, round(36 * scale))
    radius = round(88 * scale)
    draw.rounded_rectangle(
        (*point(196, 268), *point(828, 756)),
        radius=radius,
        outline=WHITE,
        width=stroke,
    )

    # 命令提示符 “>”
    prompt_width = max(2, round(46 * scale))
    draw.line(
        [point(330, 402), point(452, 480), point(330, 558)],
        fill=WHITE,
        width=prompt_width,
        joint="curve",
    )

    # 终端中的波形：与提示符同高，右侧留白均衡。
    draw.line(
        [
            point(520, 480),
            point(578, 480),
            point(612, 408),
            point(648, 552),
            point(682, 480),
            point(744, 480),
        ],
        fill=WHITE,
        width=max(2, round(32 * scale)),
        joint="curve",
    )

    # 底部两个短横线，暗示更多输出行；尺寸过小时自动省略。
    if image.size[0] >= 32:
        baseline = max(1, round(26 * scale))
        draw.line([point(332, 640), point(500, 640)], fill=WHITE, width=baseline)
        draw.line([point(540, 640), point(652, 640)], fill=WHITE, width=baseline)


def build_icon() -> Image.Image:
    """绘制 1024×1024 母版图标（RGBA）。"""
    base = _vertical_gradient(CANVAS, TOP_COLOR, BOTTOM_COLOR).convert("RGBA")
    mask = _rounded_mask(CANVAS, radius=232)
    icon = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    icon.paste(base, (0, 0), mask)
    _draw_highlight(icon, mask)
    _draw_glyph(icon)
    return icon


def main() -> int:
    output = Path(__file__).resolve().parent / "icon.ico"
    icon = build_icon()
    icon.save(output, format="ICO", sizes=[(size, size) for size in SIZES])
    size = output.stat().st_size
    with Image.open(output) as check:
        embedded = sorted(check.ico.sizes()) if hasattr(check, "ico") else []
    print(f"已生成 {output}（{size} 字节，尺寸：{embedded or list(SIZES)}）")
    return 0 if size > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

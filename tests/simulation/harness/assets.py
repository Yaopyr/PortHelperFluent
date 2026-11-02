"""生成验证所需的图片、音频、二进制与坏配置；全部写入临时目录。"""

from __future__ import annotations

import array
import json
import math
import wave
from pathlib import Path

from PIL import Image

KNOWN_COLORS = (
    (255, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
    (255, 255, 255),
)

FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\consola.ttf"),
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\simsun.ttc"),
)


def make_png(path: Path, size=(4, 4)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size)
    image.putdata(
        [KNOWN_COLORS[index % len(KNOWN_COLORS)] for index in range(size[0] * size[1])]
    )
    image.save(path)
    return path


def make_image_folder(folder: Path, count: int = 3, size=(4, 2)) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        image = Image.new("RGB", size)
        image.putdata(
            [
                KNOWN_COLORS[(index + offset) % len(KNOWN_COLORS)]
                for offset in range(size[0] * size[1])
            ]
        )
        image.save(folder / f"frame-{index}.png")
    return folder


def make_bmp(path: Path, size=(8, 8), pixels=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("L", size)
    if pixels is None:
        pixels = [255 if (row + col) % 2 else 0 for row in range(size[1]) for col in range(size[0])]
    image.putdata(list(pixels))
    image.save(path)
    return path


def make_gif(path: Path, frames: int = 3, size=(4, 4)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    images = [
        Image.new("RGB", size, color=(index * 40, 0, 0)) for index in range(frames)
    ]
    images[0].save(
        path,
        save_all=True,
        append_images=images[1:],
        duration=80,
        loop=0,
        format="GIF",
    )
    return path


def make_wav(path: Path, duration_s: float = 0.1, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = max(1, int(rate * duration_s))
    data = array.array("h")
    for index in range(samples):
        data.append(int(12000 * math.sin(2 * math.pi * 440 * index / rate)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data.tobytes())
    return path


def make_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_corrupt_config(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json", encoding="utf-8")
    return path


def find_font() -> Path | None:
    for candidate in FONT_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def independent_png_bytes(path: Path) -> bytes:
    """读取测试图片的 RGB 像素，供独立期望值使用。"""
    with Image.open(path) as image:
        return image.convert("RGB").tobytes()


def config_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))

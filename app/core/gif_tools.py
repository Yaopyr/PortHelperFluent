"""GIF 帧读取与批量 BMP 导出（纯核心逻辑，不依赖 Qt）。"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageSequence, UnidentifiedImageError

_MAX_STEM_LENGTH = 120
_INVALID_NAME_CHARS = frozenset('<>:"/\\|?*')
_MAX_DIMENSION = 16384


@dataclass(frozen=True)
class GifFrame:
    """GIF 的一帧：原图、从 0 开始的序号与显示时长（毫秒）。"""

    index: int
    image: Image.Image
    duration_ms: int


def load_gif(path: Path) -> tuple[list[GifFrame], int, int]:
    """读取 GIF 的所有帧，返回 ``(frames, width, height)``。

    每一帧都转换为独立的 RGBA 图像，因此可以安全地脱离 GIF 文件使用。
    文件缺失、不是有效图像或没有任何帧时抛出中文 ``ValueError``。
    """
    source = Path(path)
    if not source.is_file():
        raise ValueError(
            f"找不到 GIF 文件：{source}。请确认路径是否正确、文件是否已被移动。"
        )
    frames: list[GifFrame] = []
    try:
        with Image.open(source) as image:
            width, height = image.size
            for index, frame in enumerate(ImageSequence.Iterator(image)):
                duration = _frame_duration(frame, image)
                frames.append(
                    GifFrame(index=index, image=frame.convert("RGBA"), duration_ms=duration)
                )
    except UnidentifiedImageError as exc:
        raise ValueError(
            f"无法读取 GIF 文件“{source}”：文件不是有效的图像，或格式不受支持。"
        ) from exc
    except OSError as exc:
        raise ValueError(f"读取 GIF 文件失败：{source}（{exc}）。") from exc
    if not frames:
        raise ValueError(f"GIF 文件不包含任何帧：{source}。")
    return frames, width, height


def export_frames(
    frames: Iterable[GifFrame],
    output_dir: Path,
    stem: str,
    width: int | None = None,
    height: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> list[Path]:
    """把每一帧导出为 ``stem_0001.bmp`` 形式的 BMP，返回写出的路径列表。

    ``width``/``height`` 只给一个时按原图比例缩放，都给时精确缩放，都为
    ``None`` 时保持原尺寸。``progress`` 在每个文件写完后以 ``(已完成, 总数)``
    调用；``cancel`` 返回 True 时停止，已写出的文件保留。写入使用同目录临时
    文件再替换，失败不会留下半截 BMP 或临时文件。
    """
    frame_list = list(frames)
    if not frame_list:
        return []
    target_dir = Path(output_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(
            f"无法创建导出目录：{target_dir}（{exc}）。请检查路径与写入权限。"
        ) from exc

    safe_stem = _safe_stem(stem)
    total = len(frame_list)
    written: list[Path] = []
    for position, frame in enumerate(frame_list, start=1):
        if cancel is not None and cancel():
            break
        target_size = _target_size(frame.image.size, width, height)
        image = frame.image
        if target_size != image.size:
            image = image.resize(target_size, Image.Resampling.LANCZOS)
        rgb_image = _flatten_to_rgb(image)
        destination = target_dir / f"{safe_stem}_{position:04d}.bmp"
        _save_bmp_atomic(rgb_image, destination)
        written.append(destination)
        if progress is not None:
            progress(position, total)
    return written


def _frame_duration(frame: Image.Image, container: Image.Image) -> int:
    raw = frame.info.get("duration", container.info.get("duration", 0))
    try:
        duration = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(duration, 0)


def _target_size(
    original: tuple[int, int], width: int | None, height: int | None
) -> tuple[int, int]:
    if width is None and height is None:
        return original
    original_width, original_height = original
    if original_width <= 0 or original_height <= 0:
        raise ValueError("GIF 帧尺寸无效，无法缩放导出。")
    target_width = _dimension(width, "宽度") if width is not None else None
    target_height = _dimension(height, "高度") if height is not None else None
    if target_width is not None and target_height is not None:
        return target_width, target_height
    if target_width is not None:
        derived = round(original_height * target_width / original_width)
        return target_width, max(derived, 1)
    assert target_height is not None
    derived = round(original_width * target_height / original_height)
    return max(derived, 1), target_height


def _dimension(value: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"输出{label}必须是整数。") from exc
    if number <= 0:
        raise ValueError(f"输出{label}必须大于 0。")
    if number > _MAX_DIMENSION:
        raise ValueError(f"输出{label}不能超过 {_MAX_DIMENSION} 像素。")
    return number


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """BMP 不支持透明通道：把透明像素合成到白色背景。"""
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _save_bmp_atomic(image: Image.Image, destination: Path) -> None:
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        image.save(temp_path, format="BMP")
        os.replace(temp_path, destination)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _safe_stem(stem: str) -> str:
    """清洗导出文件名前缀，保留中文并阻止路径分隔符逃逸。"""
    text = Path(str(stem)).name
    cleaned = "".join(
        char
        for char in text
        if char not in _INVALID_NAME_CHARS and ord(char) >= 32 and ord(char) != 127
    )
    cleaned = cleaned.strip(" .")[: _MAX_STEM_LENGTH].rstrip(" .")
    return cleaned or "frame"

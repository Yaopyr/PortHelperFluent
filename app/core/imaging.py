"""图片取模核心：缩放、色彩量化、扫描排列与 C/BIN/LVGL 导出。

本模块不依赖 Qt，可被单元测试直接导入。图像统一先转换为 RGBA，再把透明
像素合成到指定背景色，最后按颜色深度量化。1 位输出使用固定阈值 128：
亮度 >= 128 记为 1（白），低于 128 记为 0（黑），``invert`` 会把结果取反。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, UnidentifiedImageError

from app.core.logging_setup import get_logger
from app.services.exports import (
    sanitize_name,
    unique_path,
    write_bytes_atomic,
    write_text_atomic,
)

OUTPUT_TYPES = ("c", "bin", "lvgl8", "lvgl9")
SCANS = ("horizontal", "vertical")
DIRECTIONS = ("ltr", "rtl", "ttb", "btt")
BYTE_ORDERS = ("little", "big")
COLOR_DEPTHS = (1, 8, 16, 24, 32)
IMAGE_SUFFIXES = frozenset(
    {
        ".png",
        ".bmp",
        ".dib",
        ".jpg",
        ".jpeg",
        ".jfif",
        ".gif",
        ".webp",
        ".tif",
        ".tiff",
        ".ico",
        ".ppm",
        ".pgm",
        ".pbm",
        ".tga",
    }
)
MAX_DIMENSION = 16384
VALUES_PER_LINE = 12

_LVGL8_FORMATS = {
    1: "LV_IMG_CF_ALPHA_1BIT",
    8: "LV_IMG_CF_TRUE_COLOR",
    16: "LV_IMG_CF_TRUE_COLOR",
    24: "LV_IMG_CF_TRUE_COLOR",
    32: "LV_IMG_CF_TRUE_COLOR_ALPHA",
}
_LVGL9_FORMATS = {
    1: "LV_COLOR_FORMAT_I1",
    8: "LV_COLOR_FORMAT_RGB332",
    16: "LV_COLOR_FORMAT_RGB565",
    24: "LV_COLOR_FORMAT_RGB888",
    32: "LV_COLOR_FORMAT_ARGB8888",
}
_C_KEYWORDS = frozenset(
    {
        "auto",
        "break",
        "case",
        "char",
        "const",
        "continue",
        "default",
        "do",
        "double",
        "else",
        "enum",
        "extern",
        "float",
        "for",
        "goto",
        "if",
        "inline",
        "int",
        "long",
        "register",
        "restrict",
        "return",
        "short",
        "signed",
        "sizeof",
        "static",
        "struct",
        "switch",
        "typedef",
        "union",
        "unsigned",
        "void",
        "volatile",
        "while",
        "_Bool",
        "_Complex",
        "_Imaginary",
    }
)


@dataclass(frozen=True)
class ImageParams:
    """图片转换参数；宽度或高度为 ``None``/0 时按比例自动推导。"""

    output_type: str = "c"
    scan: str = "horizontal"
    direction: str = "ltr"
    color_depth: int = 16
    byte_order: str = "little"
    width: int | None = None
    height: int | None = None
    background: tuple[int, int, int] = (0, 0, 0)
    invert: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "output_type",
            _choice(self.output_type, OUTPUT_TYPES, "输出类型"),
        )
        object.__setattr__(self, "scan", _choice(self.scan, SCANS, "扫描方式"))
        object.__setattr__(
            self, "direction", _choice(self.direction, DIRECTIONS, "扫描方向")
        )
        object.__setattr__(
            self, "byte_order", _choice(self.byte_order, BYTE_ORDERS, "字节序")
        )
        object.__setattr__(
            self,
            "color_depth",
            _depth(self.color_depth),
        )
        object.__setattr__(self, "width", _optional_dimension(self.width, "输出宽度"))
        object.__setattr__(
            self, "height", _optional_dimension(self.height, "输出高度")
        )
        object.__setattr__(
            self, "background", _background(self.background)
        )
        object.__setattr__(self, "invert", bool(self.invert))


@dataclass(frozen=True)
class ConversionResult:
    """一次转换的结果：原始数据、实际尺寸、预览图与可选的 C 代码。"""

    data: bytes
    width: int
    height: int
    preview: Image.Image
    text: str | None


class BatchResult(dict):
    """批量转换结果：输入路径 -> 输出文件列表。

    ``errors`` 保存失败文件的中文原因；``canceled`` 表示是否被用户取消。
    """

    def __init__(self) -> None:
        super().__init__()
        self.errors: dict[str, str] = {}
        self.canceled = False


def load_image(path: Path) -> Image.Image:
    """读取图片并返回脱离文件的副本；失败时抛出中文 ``ValueError``。"""
    source = Path(path)
    if not source.is_file():
        raise ValueError(
            f"找不到图片文件：{source}。请确认路径是否正确、文件是否已被移动。"
        )
    try:
        with Image.open(source) as image:
            image.load()
            return image.copy()
    except UnidentifiedImageError as exc:
        raise ValueError(
            f"无法读取图片“{source}”：文件不是有效的图像，或格式不受支持。"
        ) from exc
    except Exception as exc:
        raise ValueError(f"读取图片失败：{source}（{exc}）。") from exc


def list_image_files(folder: Path, recursive: bool = False) -> list[Path]:
    """列出文件夹中的图片文件（默认不递归），按文件名排序。"""
    source = Path(folder)
    if not source.is_dir():
        raise ValueError(f"找不到图片文件夹：{source}。请确认路径是否正确。")
    try:
        iterator = source.rglob("*") if recursive else source.iterdir()
        files = [
            path
            for path in iterator
            if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES
        ]
    except OSError as exc:
        raise ValueError(f"读取图片文件夹失败：{source}（{exc}）。") from exc
    return sorted(files, key=lambda item: item.name.casefold())


def convert_image(
    image: Image.Image,
    params: ImageParams,
    name: str = "image",
) -> ConversionResult:
    """把 Pillow 图像转换为目标数据、预览与可选的 C/LVGL 文本。"""
    if not isinstance(image, Image.Image):
        raise TypeError("image 必须是 PIL.Image.Image 实例。")

    scan, direction = _effective_scan(params.scan, params.direction)
    target_width, target_height = _target_size(
        image.size, params.width, params.height
    )

    rgba = image.convert("RGBA")
    if rgba.size != (target_width, target_height):
        rgba = rgba.resize(
            (target_width, target_height), Image.Resampling.NEAREST
        )
    flattened = _flatten_rgba(rgba, params.background)
    pixels = np.asarray(flattened, dtype=np.uint8)
    if params.color_depth == 1:
        gray = np.asarray(flattened.convert("L"), dtype=np.uint8)
        data = _encode_bilevel(
            _scan_order(gray, scan, direction), params.invert
        )
    else:
        data = _encode_pixels(
            _scan_order(pixels, scan, direction),
            params.color_depth,
            params.byte_order,
        )
    preview = _make_preview(flattened, params.color_depth, params.invert)

    text: str | None = None
    if params.output_type == "c":
        text = _c_text(
            data,
            name,
            target_width,
            target_height,
            params.color_depth,
            scan,
            direction,
            params.byte_order,
        )
    elif params.output_type in ("lvgl8", "lvgl9"):
        text = _lvgl_text(
            data,
            name,
            target_width,
            target_height,
            params.color_depth,
            scan,
            direction,
            params.byte_order,
            params.output_type,
        )
    return ConversionResult(
        data=data,
        width=target_width,
        height=target_height,
        preview=preview,
        text=text,
    )


def convert_file(
    path: Path,
    params: ImageParams,
    output_dir: Path,
    name: str | None = None,
) -> list[Path]:
    """转换单张图片并写出 ``.c`` 或 ``.bin``，返回写出的路径列表。

    输出文件名来自 ``name`` 或原文件名，经过 Windows 文件名清洗；同名文件
    自动追加 ``_1``、``_2`` 后缀。写入使用同目录临时文件再替换，不会留下
    半截文件。
    """
    source = Path(path)
    image = load_image(source)
    requested = str(name).strip() if name is not None else ""
    if not requested:
        requested = source.stem
    requested = _strip_known_suffix(requested) or source.stem
    safe_stem = sanitize_name(requested, fallback="image")

    result = convert_image(image, params, name=requested or source.stem)
    target_dir = _ensure_output_dir(output_dir)
    suffix = ".bin" if params.output_type == "bin" else ".c"
    target = unique_path(target_dir / f"{safe_stem}{suffix}")
    try:
        if params.output_type == "bin":
            write_bytes_atomic(target, result.data)
        else:
            write_text_atomic(target, result.text or "", encoding="utf-8")
    except OSError as exc:
        raise OSError(
            f"写入文件失败：{target}（{exc}）。"
            "请检查磁盘空间、目录权限或文件是否被其他程序占用。"
        ) from exc
    return [target]


def batch_convert(
    paths: list[Path],
    params: ImageParams,
    output_dir: Path,
    progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> BatchResult:
    """批量转换图片；单个文件失败不会中断其余文件。

    返回 ``BatchResult``（``dict[str, list[Path]]``）：键为输入路径字符串，
    值为输出文件列表；失败原因记录在 ``errors`` 中。``progress`` 在每个文件
    处理完后以 ``(已完成, 总数)`` 调用，``cancel`` 返回 True 时在下一个文件
    前停止。已写出的文件都是完整的原子文件。
    """
    file_list = [Path(item) for item in paths]
    result = BatchResult()
    if not file_list:
        return result
    target_dir = _ensure_output_dir(output_dir)
    total = len(file_list)
    for index, source in enumerate(file_list, start=1):
        if cancel is not None and cancel():
            result.canceled = True
            break
        key = str(source)
        try:
            outputs = convert_file(source, params, target_dir)
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            result.errors[key] = message
            result.setdefault(key, [])
            get_logger().warning("批量图片转换失败：%s（%s）", source, message)
        else:
            result.setdefault(key, []).extend(outputs)
        if progress is not None:
            progress(index, total)
    return result


def _choice(value: object, allowed: tuple[str, ...], label: str) -> str:
    text = str(value).strip().lower()
    if text not in allowed:
        options = "、".join(allowed)
        raise ValueError(f"{label}必须是 {options} 之一，当前为“{value}”。")
    return text


def _depth(value: object) -> int:
    try:
        depth = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"颜色深度必须是整数，当前为“{value}”。") from exc
    if depth not in COLOR_DEPTHS:
        options = "、".join(str(item) for item in COLOR_DEPTHS)
        raise ValueError(f"颜色深度必须是 {options} 位之一，当前为 {depth}。")
    return depth


def _optional_dimension(value: object, label: str) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数或留空。") from exc
    if number == 0:
        return None
    if number < 0:
        raise ValueError(f"{label}不能为负数。")
    if number > MAX_DIMENSION:
        raise ValueError(f"{label}不能超过 {MAX_DIMENSION} 像素。")
    return number


def _background(value: object) -> tuple[int, int, int]:
    if value is None:
        return (0, 0, 0)
    if isinstance(value, str):
        from app.core.color import hex_to_rgb

        return hex_to_rgb(value)
    try:
        components = tuple(int(component) for component in value)  # type: ignore[union-attr]
    except (TypeError, ValueError) as exc:
        raise ValueError("背景色必须是三个 0–255 的整数。") from exc
    if len(components) != 3:
        raise ValueError("背景色必须是三个 0–255 的整数。")
    for component in components:
        if not 0 <= component <= 255:
            raise ValueError(f"背景色分量 {component} 超出 0–255 范围。")
    return components  # type: ignore[return-value]


def _effective_scan(scan: str, direction: str) -> tuple[str, str]:
    """归一化扫描组合，保证返回的 (方式, 方向) 始终是有效组合。

    纵向扫描或方向为 ttb/btt 时使用逐列排列；此时 ltr/rtl 分别映射为
    从上到下/从下到上，避免界面上的组合产生歧义。
    """
    if scan == "vertical" or direction in ("ttb", "btt"):
        if direction in ("ttb", "btt"):
            return "vertical", direction
        return "vertical", "ttb" if direction == "ltr" else "btt"
    return "horizontal", direction if direction in ("ltr", "rtl") else "ltr"


def _target_size(
    original: tuple[int, int],
    width: int | None,
    height: int | None,
) -> tuple[int, int]:
    original_width, original_height = int(original[0]), int(original[1])
    if original_width <= 0 or original_height <= 0:
        raise ValueError("图片尺寸无效，无法转换。")
    if width is None and height is None:
        return original_width, original_height
    if width is not None and height is not None:
        return width, height
    if width is not None:
        return width, max(1, round(original_height * width / original_width))
    assert height is not None
    return max(1, round(original_width * height / original_height)), height


def _flatten_rgba(
    image: Image.Image, background: tuple[int, int, int]
) -> Image.Image:
    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    canvas = Image.new("RGBA", rgba.size, (*background, 255))
    canvas.alpha_composite(rgba)
    return canvas.convert("RGB")


def _scan_order(
    pixels: np.ndarray, scan: str, direction: str
) -> np.ndarray:
    """把 (高, 宽, ...) 像素数组按扫描方式与方向展平成像素序列。"""
    if scan == "horizontal":
        ordered = pixels if direction == "ltr" else pixels[:, ::-1]
    else:
        ordered = np.swapaxes(pixels, 0, 1)
        if direction == "btt":
            ordered = ordered[:, ::-1]
    return ordered.reshape(-1, *pixels.shape[2:])


def _encode_bilevel(ordered_gray: np.ndarray, invert: bool) -> bytes:
    white = (ordered_gray >= 128) != bool(invert)
    return np.packbits(white.astype(np.uint8), bitorder="big").tobytes()


def _encode_pixels(
    ordered: np.ndarray, depth: int, byte_order: str
) -> bytes:
    red = ordered[:, 0].astype(np.uint16)
    green = ordered[:, 1].astype(np.uint16)
    blue = ordered[:, 2].astype(np.uint16)
    if depth == 8:
        values = ((red >> 5) << 5) | ((green >> 5) << 2) | (blue >> 6)
        return values.astype(np.uint8).tobytes()
    if depth == 16:
        values = ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
        dtype = "<u2" if byte_order == "little" else ">u2"
        return values.astype(dtype).tobytes()
    if depth == 24:
        return ordered.astype(np.uint8).tobytes()
    # 32 位 RGBA8888：alpha 固定为不透明。
    alpha = np.full((ordered.shape[0], 1), 255, dtype=np.uint8)
    return np.concatenate(
        [ordered.astype(np.uint8), alpha], axis=1
    ).tobytes()


def _make_preview(
    flattened: Image.Image,
    depth: int,
    invert: bool,
) -> Image.Image:
    if depth == 1:
        gray = np.asarray(flattened.convert("L"), dtype=np.uint8)
        white = (gray >= 128) != bool(invert)
        return Image.fromarray(
            np.where(white, 255, 0).astype(np.uint8), mode="L"
        ).convert("RGB")

    pixels = np.asarray(flattened, dtype=np.uint8)
    red = pixels[..., 0].astype(np.uint16)
    green = pixels[..., 1].astype(np.uint16)
    blue = pixels[..., 2].astype(np.uint16)
    if depth == 8:
        red3 = red >> 5
        green3 = green >> 5
        blue2 = blue >> 6
        preview = np.stack(
            [
                (red3 << 5) | (red3 << 2) | (red3 >> 1),
                (green3 << 5) | (green3 << 2) | (green3 >> 1),
                (blue2 << 6) | (blue2 << 4) | (blue2 << 2) | blue2,
            ],
            axis=-1,
        )
    elif depth == 16:
        red5 = red >> 3
        green6 = green >> 2
        blue5 = blue >> 3
        preview = np.stack(
            [
                (red5 << 3) | (red5 >> 2),
                (green6 << 2) | (green6 >> 4),
                (blue5 << 3) | (blue5 >> 2),
            ],
            axis=-1,
        )
    else:
        preview = pixels
    return Image.fromarray(preview.astype(np.uint8), mode="RGB")


def _c_identifier(name: object, fallback: str = "image") -> str:
    text = re.sub(r"[^0-9A-Za-z_]", "_", str(name))
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"_{text}"
    if text in _C_KEYWORDS:
        text = f"{text}_"
    return text[:60]


def _strip_known_suffix(name: str) -> str:
    text = name.strip()
    lowered = text.casefold()
    for suffix in (".c", ".bin", ".h"):
        if lowered.endswith(suffix):
            return text[: -len(suffix)].rstrip(" .")
    return text


def _ensure_output_dir(output_dir: Path) -> Path:
    target_dir = Path(output_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(
            f"无法创建输出目录：{target_dir}（{exc}）。请检查路径与写入权限。"
        ) from exc
    if not target_dir.is_dir():
        raise OSError(f"输出目录不可用：{target_dir}。请选择一个文件夹。")
    return target_dir


def _format_bytes(data: bytes) -> str:
    if not data:
        return ""
    lines: list[str] = []
    for start in range(0, len(data), VALUES_PER_LINE):
        chunk = data[start : start + VALUES_PER_LINE]
        lines.append(
            "    " + ", ".join(f"0x{value:02X}" for value in chunk) + ","
        )
    return "\n".join(lines)


def _header_comment(
    prefix: str,
    name: str,
    width: int,
    height: int,
    depth: int,
    scan: str,
    direction: str,
    byte_order: str,
) -> str:
    return (
        f"/* {prefix}{name} {width}x{height} depth={depth} "
        f"scan={scan} direction={direction} "
        f"endian={byte_order} byte_order={byte_order} */"
    )


def _c_text(
    data: bytes,
    name: str,
    width: int,
    height: int,
    depth: int,
    scan: str,
    direction: str,
    byte_order: str,
) -> str:
    identifier = _c_identifier(name)
    comment = _header_comment(
        "",
        identifier,
        width,
        height,
        depth,
        scan,
        direction,
        byte_order,
    )
    return (
        f"{comment}\n"
        f"const unsigned char {identifier}[] = {{\n"
        f"{_format_bytes(data)}\n"
        "};\n"
    )


def _lvgl_text(
    data: bytes,
    name: str,
    width: int,
    height: int,
    depth: int,
    scan: str,
    direction: str,
    byte_order: str,
    output_type: str,
) -> str:
    identifier = _c_identifier(name)
    version = 8 if output_type == "lvgl8" else 9
    comment = _header_comment(
        f"LVGL {version} image: ",
        identifier,
        width,
        height,
        depth,
        scan,
        direction,
        byte_order,
    )
    map_name = f"{identifier}_map"
    lines = [
        comment,
        '#include "lvgl.h"',
        "",
        f"static const uint8_t {map_name}[] = {{",
        _format_bytes(data),
        "};",
        "",
    ]
    notes: list[str] = []
    if scan != "horizontal":
        notes.append(
            "/* 纵向扫描数据不能直接按 LVGL 的 stride 描述符显示，"
            "请改用水平扫描。 */"
        )
    if depth == 32:
        notes.append(
            "/* 32 位数据按 RGBA8888（R,G,B,A）排列，alpha 固定 255；"
            "LVGL 使用 ARGB8888 时请按平台字节序调整。 */"
        )
    if notes:
        lines[1:1] = notes
    if output_type == "lvgl8":
        lines.extend(
            [
                f"const lv_img_dsc_t {identifier} = {{",
                f"    .header.cf = {_LVGL8_FORMATS[depth]},",
                "    .header.always_zero = 0,",
                "    .header.reserved = 0,",
                f"    .header.w = {width},",
                f"    .header.h = {height},",
                f"    .data_size = {len(data)},",
                f"    .data = {map_name},",
                "};",
            ]
        )
    else:
        stride = _lvgl_stride(width, depth)
        lines.extend(
            [
                f"const lv_image_dsc_t {identifier} = {{",
                "    .header.magic = LV_IMAGE_HEADER_MAGIC,",
                f"    .header.cf = {_LVGL9_FORMATS[depth]},",
                "    .header.flags = 0,",
                f"    .header.w = {width},",
                f"    .header.h = {height},",
                f"    .header.stride = {stride},",
                f"    .data_size = {len(data)},",
                f"    .data = {map_name},",
                "};",
            ]
        )
    return "\n".join(lines) + "\n"


def _lvgl_stride(width: int, depth: int) -> int:
    if depth == 1:
        return (width + 7) // 8
    return width * (depth // 8)

"""``image`` 命令组：单图取模与文件夹批量取模。

- ``convert``：一张图 → 一个 ``.c``（C 或 LVGL）或 ``.bin``，返回实际输出尺寸、
  数据字节数与写出的文件；
- ``batch``：文件夹内每张图片各自转换，失败文件记入 ``errors`` 但不中断其余文件。

尺寸推导、色彩量化、扫描排列与文件命名都来自 :mod:`app.core.imaging`（与 GUI
同一套实现）：``convert_image`` 给出尺寸与数据长度，``convert_file`` 负责命名与
原子写出。本模块只做参数映射与结果组装，不做像素处理。

核心模块会连带加载 numpy 与 Pillow，因此按需局部导入：``import app.cli.main``
时只 import 轻量模块，真正跑 ``image`` 命令时才付出这部分开销。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.cli.errors import EXIT_OPERATION, EXIT_USAGE, CliError
from app.cli.output import add_output_options
from app.core.color import hex_to_rgb

if TYPE_CHECKING:
    from app.core.imaging import ImageParams

__all__ = ["batch", "build_params", "convert", "register"]

THRESHOLD_MIN, THRESHOLD_MAX = 0, 255

# 与 ``convert_file`` 一致：``--name`` 带这些后缀时先去掉，保证文件主名与 C 标识符相同。
_NAME_SUFFIXES = (".c", ".bin", ".h")


def register(subparsers) -> None:
    """注册 ``image`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "image",
        help="图片取模：单图与文件夹批量转 C/BIN/LVGL8/LVGL9。",
        description="把图片缩放、量化后导出为 C 数组、BIN 或 LVGL 图像；"
        "可用子命令：convert、batch。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    _register_convert(group)
    _register_batch(group)


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------


def _register_convert(subparsers) -> None:
    parser = subparsers.add_parser(
        "convert",
        help="单图取模：在 --out-dir 写出 .c 或 .bin。",
        description="转换一张图片，输出文件名取 --name 或输入文件名。",
    )
    parser.add_argument("--file", required=True, metavar="IMAGE", help="输入图片路径。")
    parser.add_argument(
        "--out-dir",
        required=True,
        metavar="DIR",
        help="输出目录，不存在时自动创建。",
    )
    parser.add_argument(
        "--name",
        metavar="NAME",
        help="输出文件名主名（不含扩展名），默认取输入文件名。",
    )
    _add_params(parser)
    add_output_options(parser)
    parser.set_defaults(handler=convert, command="image.convert")


def convert(args) -> dict[str, Any]:
    """转换单张图片并返回 ``width``/``height``/``bytes``/``files``。"""
    from app.core.imaging import convert_file, convert_image, load_image

    params = build_params(args)
    source = Path(getattr(args, "file"))
    if not source.is_file():
        raise CliError(
            "not_found",
            f"找不到图片文件：{source}。请确认路径是否正确、文件是否已被移动。",
            EXIT_OPERATION,
        )
    name = getattr(args, "name", None)
    requested = _requested_name(source, name)
    try:
        # convert_image 给出尺寸与数据长度；convert_file 负责命名与原子写出。
        result = convert_image(load_image(source), params, name=requested)
        files = convert_file(source, params, Path(getattr(args, "out_dir")), name=name)
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
    except OSError as exc:
        raise CliError("permission_denied", f"写出图片失败：{exc}", EXIT_OPERATION) from exc
    return {
        "width": result.width,
        "height": result.height,
        "bytes": len(result.data),
        "files": [str(path) for path in files],
    }


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


def _register_batch(subparsers) -> None:
    parser = subparsers.add_parser(
        "batch",
        help="批量取模：转换文件夹内的所有图片。",
        description="对文件夹内每张图片各生成一个输出文件；单个文件失败不影响其余文件。",
    )
    parser.add_argument(
        "--folder", required=True, metavar="DIR", help="输入图片文件夹。"
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        metavar="DIR",
        help="输出目录，不存在时自动创建。",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="递归处理子文件夹（默认只看当前文件夹）。",
    )
    _add_params(parser)
    add_output_options(parser)
    parser.set_defaults(handler=batch, command="image.batch")


def batch(args) -> dict[str, Any]:
    """批量转换文件夹内的图片，返回 ``files``/``errors``/``canceled``。"""
    from app.core.imaging import batch_convert, list_image_files

    params = build_params(args)
    folder = Path(getattr(args, "folder"))
    if not folder.is_dir():
        raise CliError(
            "not_found",
            f"找不到图片文件夹：{folder}。请确认路径是否正确。",
            EXIT_OPERATION,
        )
    try:
        paths = list_image_files(
            folder, recursive=bool(getattr(args, "recursive", False))
        )
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
    try:
        result = batch_convert(paths, params, Path(getattr(args, "out_dir")))
    except OSError as exc:
        raise CliError("permission_denied", f"写出图片失败：{exc}", EXIT_OPERATION) from exc
    return {
        "files": {
            key: [str(path) for path in outputs] for key, outputs in result.items()
        },
        "errors": dict(result.errors),
        "canceled": bool(result.canceled),
    }


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def _add_params(parser) -> None:
    """添加 ``convert`` 与 ``batch`` 共用的一组取模参数。"""
    # 允许集合来自核心模块，避免与 ``ImageParams`` 的校验标准漂移。
    from app.core.imaging import (
        BYTE_ORDERS,
        COLOR_DEPTHS,
        DIRECTIONS,
        OUTPUT_TYPES,
        SCANS,
    )

    parser.add_argument(
        "--type",
        dest="output_type",
        choices=OUTPUT_TYPES,
        default="c",
        metavar="TYPE",
        help="输出类型：c（C 数组）、bin（原始字节）、lvgl8 或 lvgl9（默认 c）。",
    )
    parser.add_argument(
        "--scan",
        choices=SCANS,
        default="horizontal",
        metavar="SCAN",
        help="扫描方式：horizontal（逐行）或 vertical（逐列），默认 horizontal。",
    )
    parser.add_argument(
        "--direction",
        choices=DIRECTIONS,
        default="ltr",
        metavar="DIR",
        help="扫描方向：ltr、rtl、ttb、btt，默认 ltr。",
    )
    parser.add_argument(
        "--depth",
        dest="color_depth",
        type=int,
        choices=COLOR_DEPTHS,
        default=16,
        metavar="BITS",
        help="颜色深度：1、8、16、24、32 位，默认 16。",
    )
    parser.add_argument(
        "--byte-order",
        choices=BYTE_ORDERS,
        default="little",
        metavar="ORDER",
        help="多字节像素的字节序：little 或 big，默认 little。",
    )
    parser.add_argument(
        "--width",
        type=int,
        metavar="N",
        help="输出宽度（像素）；省略或 0 时按原图比例推导。",
    )
    parser.add_argument(
        "--height",
        type=int,
        metavar="N",
        help="输出高度（像素）；省略或 0 时按原图比例推导。",
    )
    parser.add_argument(
        "--background",
        metavar="#RRGGBB",
        help="透明像素合成到该背景色（默认 #000000）。",
    )
    parser.add_argument(
        "--invert",
        action="store_true",
        help="1 位输出取反（默认亮像素记 1）。",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=128,
        metavar="0-255",
        help="1 位输出的亮度阈值：>= 阈值为亮（默认 128）。",
    )


def build_params(args) -> ImageParams:
    """把命令行参数映射成 :class:`ImageParams`。

    阈值越界属于参数写错（``usage``/2）；其余取值交给 ``ImageParams`` 校验，
    它的中文 ``ValueError``（如无法解析的背景色、负的宽高）按输入非法（3）上报。
    """
    from app.core.imaging import ImageParams

    threshold = _threshold_arg(getattr(args, "threshold", 128))
    try:
        return ImageParams(
            output_type=getattr(args, "output_type", "c"),
            scan=getattr(args, "scan", "horizontal"),
            direction=getattr(args, "direction", "ltr"),
            color_depth=getattr(args, "color_depth", 16),
            byte_order=getattr(args, "byte_order", "little"),
            width=getattr(args, "width", None),
            height=getattr(args, "height", None),
            background=_parse_background(getattr(args, "background", None)),
            invert=bool(getattr(args, "invert", False)),
            threshold=threshold,
        )
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc


def _parse_background(text: str | None) -> tuple[int, int, int]:
    """``--background #RRGGBB`` → RGB 三元组；格式非法由 ``hex_to_rgb`` 报中文 ValueError。"""
    if not text:
        return (0, 0, 0)
    return hex_to_rgb(text)


def _threshold_arg(value: int) -> int:
    if not THRESHOLD_MIN <= value <= THRESHOLD_MAX:
        raise CliError(
            "usage",
            f"--threshold 必须在 {THRESHOLD_MIN}–{THRESHOLD_MAX} 之间，收到 {value}。",
            EXIT_USAGE,
        )
    return value


def _requested_name(source: Path, name: str | None) -> str:
    """``convert_image`` 用的名字：与 ``convert_file`` 一样回落到输入文件名并去掉后缀。"""
    requested = str(name).strip() if name is not None else ""
    lowered = requested.casefold()
    for suffix in _NAME_SUFFIXES:
        if lowered.endswith(suffix):
            requested = requested[: -len(suffix)].rstrip(" .")
            break
    return requested or source.stem

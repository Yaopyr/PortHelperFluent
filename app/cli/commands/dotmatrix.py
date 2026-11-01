"""``dotmatrix`` 命令组：文字、BMP 与文件夹批量点阵导出。

- ``text``：文字 → 分页点阵 → 一个 ``.c`` 或 ``.bin``，可用 ``--toggle`` 手工翻转单元格；
- ``bmp``：一张图片 → 单页点阵 → 一个 ``.c`` 或 ``.bin``；
- ``batch``：文件夹内每张图片各自生成一个独立 ``.c``（文件名取输入 stem）。

渲染、分页、扫描排列与位序打包都来自 :mod:`app.core.fontmatrix`（与 GUI 同一套
实现），本模块只做参数映射、编辑与结果组装。

核心模块会连带加载 Pillow，因此按需局部导入：``import app.cli.main`` 与导入本模块
都只拉轻量依赖，真正跑 ``dotmatrix`` 命令时才付出这部分开销。解析器里的允许集合
使用本模块的常量，避免构造解析器时导入核心模块。

注意：``--format`` 在本组表示导出格式（``c``/``bin``），因此不提供 ``--format
json|text``；stdout 始终是 JSON 信封。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.cli.errors import EXIT_OPERATION, EXIT_USAGE, CliError
from app.services.exports import sanitize_name, unique_path, write_bytes_atomic, write_text_atomic

if TYPE_CHECKING:
    from app.core.fontmatrix import FontParams, Matrix

__all__ = ["batch", "bmp", "build_params", "register", "text"]

# 允许集合与 ``app.core.fontmatrix`` 的校验标准保持一致（此处硬编码以保持导入轻量）。
EXPORT_FORMATS = ("c", "bin")
SCANS = ("horizontal", "vertical")
DIRECTIONS = ("ltr", "rtl", "ttb", "btt")
BIT_ORDERS = ("msb", "lsb")
SIZE_MIN, SIZE_MAX = 1, 256
PAGE_CHARS_MIN, PAGE_CHARS_MAX = 1, 256
THRESHOLD_MIN, THRESHOLD_MAX = 0, 255
DEFAULT_NAME = "font_matrix"

_SUFFIXES = {"c": ".c", "bin": ".bin"}
# ``--name`` 带这些后缀时先去掉，保证文件主名与 C 标识符一致。
_NAME_SUFFIXES = (".c", ".bin", ".h")


def register(subparsers) -> None:
    """注册 ``dotmatrix`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "dotmatrix",
        help="点阵字模：文字、图片与文件夹批量导出 C/BIN。",
        description="把文字渲染或把图片二值化为点阵，导出 C 数组或 BIN；"
        "可用子命令：text、bmp、batch。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    _register_text(group)
    _register_bmp(group)
    _register_batch(group)


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------


def _register_text(subparsers) -> None:
    parser = subparsers.add_parser(
        "text",
        help="文字取模：按字号分页，导出 C 数组或 BIN。",
        description="把文字渲染为可分页编辑的点阵；--toggle 可在导出前翻转单元格。",
    )
    parser.add_argument("--text", required=True, metavar="TEXT", help="要取模的文字（支持多行）。")
    _add_output_args(parser)
    _add_font_params(parser)
    _add_image_params(parser)
    _add_page_args(parser)
    parser.set_defaults(handler=text, command="dotmatrix.text")


def text(args) -> dict[str, Any]:
    """渲染文字、应用 ``--toggle`` 后写出单个文件，返回点阵统计与文件列表。"""
    from app.core.fontmatrix import matrix_from_text

    params = build_params(args)
    content = str(getattr(args, "text") or "")
    if content == "":
        raise CliError(
            "invalid_input",
            "文字为空，没有可导出的点阵。请用 --text 传入要取模的文字。",
            EXIT_OPERATION,
        )
    try:
        pages = matrix_from_text(content, params)
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
    if not pages:
        raise CliError(
            "invalid_input",
            f"文字“{content}”没有生成任何点阵，请确认内容与字号。",
            EXIT_OPERATION,
        )
    _apply_toggles(pages, getattr(args, "toggles", ()))
    return _export(args, pages, params, default_name=DEFAULT_NAME)


# ---------------------------------------------------------------------------
# bmp
# ---------------------------------------------------------------------------


def _register_bmp(subparsers) -> None:
    parser = subparsers.add_parser(
        "bmp",
        help="图片取模：单张图片导出 C 数组或 BIN。",
        description="按阈值把图片二值化为单页点阵；--toggle 可在导出前翻转单元格。",
    )
    parser.add_argument("--file", required=True, metavar="IMAGE", help="输入图片路径（BMP/PNG 等）。")
    _add_output_args(parser)
    _add_image_params(parser)
    _add_toggle_arg(parser)
    parser.set_defaults(handler=bmp, command="dotmatrix.bmp")


def bmp(args) -> dict[str, Any]:
    """把一张图片取模为单页点阵并写出，返回点阵统计与文件列表。"""
    from app.core.fontmatrix import matrix_from_image

    params = build_params(args)
    source = Path(getattr(args, "file"))
    if not source.is_file():
        raise CliError(
            "not_found",
            f"找不到图片文件：{source}。请确认路径是否正确、文件是否已被移动。",
            EXIT_OPERATION,
        )
    try:
        matrix = matrix_from_image(source, params)
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
    _apply_toggles([matrix], getattr(args, "toggles", ()))
    return _export(args, [matrix], params, default_name=source.stem or DEFAULT_NAME)


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------


def _register_batch(subparsers) -> None:
    parser = subparsers.add_parser(
        "batch",
        help="批量取模：文件夹内每张图片各生成一个 C 文件。",
        description="对文件夹内每张图片生成独立 C 数组，文件名取输入 stem；重名自动加下标。",
    )
    parser.add_argument("--folder", required=True, metavar="DIR", help="输入图片文件夹。")
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
    _add_image_params(parser)
    parser.set_defaults(handler=batch, command="dotmatrix.batch")


def batch(args) -> dict[str, Any]:
    """把文件夹内每张图片导出为独立 C 文件，返回累计统计与文件列表。"""
    from app.core.fontmatrix import matrix_from_image, matrix_to_bytes, matrix_to_c
    from app.core.imaging import list_image_files

    params = build_params(args)
    folder = Path(getattr(args, "folder"))
    if not folder.is_dir():
        raise CliError(
            "not_found",
            f"找不到图片文件夹：{folder}。请确认路径是否正确。",
            EXIT_OPERATION,
        )
    try:
        paths = list_image_files(folder, recursive=bool(getattr(args, "recursive", False)))
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
    if not paths:
        raise CliError(
            "invalid_input",
            f"文件夹中没有可转换的图片：{folder}。请确认其中包含 BMP/PNG 等图片。",
            EXIT_OPERATION,
        )

    out_dir = Path(getattr(args, "out_dir"))
    files: list[str] = []
    pages_count = width = height = total_bytes = 0
    for source in paths:
        try:
            matrix = matrix_from_image(source, params)
        except ValueError as exc:
            raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc
        name = source.stem or DEFAULT_NAME
        target = unique_path(
            out_dir / f"{sanitize_name(name, fallback=DEFAULT_NAME)}{_SUFFIXES['c']}"
        )
        try:
            write_text_atomic(target, matrix_to_c([matrix], params, name))
        except OSError as exc:
            raise CliError(
                "permission_denied",
                f"写出点阵失败：{target}（{exc}）。请检查磁盘空间或目录权限。",
                EXIT_OPERATION,
            ) from exc
        files.append(str(target))
        pages_count += 1
        width = max(width, matrix.cols)
        height = max(height, matrix.rows)
        total_bytes += len(matrix_to_bytes(matrix, params))
    return {
        "pages": pages_count,
        "width": width,
        "height": height,
        "bytes": total_bytes,
        "files": files,
    }


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def _add_output_args(parser) -> None:
    """``text``/``bmp`` 的输出参数：目录、导出格式与数组名。"""
    parser.add_argument(
        "--out-dir",
        required=True,
        metavar="DIR",
        help="输出目录，不存在时自动创建。",
    )
    parser.add_argument(
        "--format",
        dest="export_format",
        choices=EXPORT_FORMATS,
        default="c",
        metavar="FORMAT",
        help="导出格式：c（C 数组）或 bin（原始字节），默认 c。",
    )
    parser.add_argument(
        "--name",
        metavar="NAME",
        help="输出文件名主名（不含扩展名）；默认 font_matrix，BMP 模式取输入文件名。",
    )


def _add_font_params(parser) -> None:
    """文字模式的字体与字号参数；字体族与字体文件互斥。"""
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--font-family",
        metavar="NAME",
        help="系统字体族名；无法解析时使用 Pillow 内置字体。",
    )
    source.add_argument(
        "--font-file",
        metavar="PATH",
        help="字体文件路径（.ttf/.ttc/.otf）。",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=16,
        metavar="N",
        help=f"字号（像素），{SIZE_MIN}–{SIZE_MAX}，默认 16。",
    )
    parser.add_argument("--bold", action="store_true", help="加粗渲染（描边 1 像素）。")
    parser.add_argument("--italic", action="store_true", help="倾斜渲染。")


def _add_image_params(parser) -> None:
    """三种子命令共用：二值化阈值、反色与扫描排列。"""
    parser.add_argument(
        "--threshold",
        type=int,
        default=128,
        metavar="0-255",
        help="灰度阈值：低于阈值的像素取为黑点（默认 128）。",
    )
    parser.add_argument("--invert", action="store_true", help="取反（黑底白字的图片请开启）。")
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
        help="取模走向：ltr、rtl、ttb、btt，默认 ltr。",
    )
    parser.add_argument(
        "--bit-order",
        dest="bit_order",
        choices=BIT_ORDERS,
        default="msb",
        metavar="ORDER",
        help="位序：msb（先出现的像素在 bit7）或 lsb，默认 msb。",
    )


def _add_page_args(parser) -> None:
    """分页参数（页宽 = 每页字符数 × 字号）与手工翻转单元格。"""
    parser.add_argument(
        "--page-chars",
        dest="page_chars",
        type=int,
        default=8,
        metavar="N",
        help=f"每页字符数，{PAGE_CHARS_MIN}–{PAGE_CHARS_MAX}，默认 8；页宽为字符数 × 字号。",
    )
    _add_toggle_arg(parser)


def _add_toggle_arg(parser) -> None:
    """导出前翻转单元格；图片模式不分页，因此只加这一项编辑参数。"""
    parser.add_argument(
        "--toggle",
        dest="toggles",
        action="append",
        metavar="P,R,C",
        help="导出前翻转单元格，“页,行,列”均为 0 基，可重复使用。",
    )


def build_params(args) -> FontParams:
    """把命令行参数映射成 :class:`FontParams`。

    数值越界属于参数写错（``usage``/2），字体文件不存在按找不到输入（``not_found``/3）；
    其余取值交给 ``FontParams`` 校验，它的中文 ``ValueError`` 按输入非法（3）上报。
    """
    from app.core.fontmatrix import FontParams

    _bounded(getattr(args, "size", 16), SIZE_MIN, SIZE_MAX, "--size")
    _bounded(getattr(args, "threshold", 128), THRESHOLD_MIN, THRESHOLD_MAX, "--threshold")
    _bounded(
        getattr(args, "page_chars", 8), PAGE_CHARS_MIN, PAGE_CHARS_MAX, "--page-chars"
    )
    try:
        return FontParams(
            font_path=_font_path(args),
            size=int(getattr(args, "size", 16)),
            bold=bool(getattr(args, "bold", False)),
            italic=bool(getattr(args, "italic", False)),
            threshold=int(getattr(args, "threshold", 128)),
            invert=bool(getattr(args, "invert", False)),
            scan=getattr(args, "scan", "horizontal"),
            direction=getattr(args, "direction", "ltr"),
            bit_order=getattr(args, "bit_order", "msb"),
            page_chars=int(getattr(args, "page_chars", 8)),
        )
    except ValueError as exc:
        raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc


def _font_path(args) -> str | None:
    """``--font-file`` 原样使用；``--font-family`` 用 ``resolve_font_file`` 解析（None 走内置字体）。"""
    from app.core.fontmatrix import resolve_font_file

    font_file = getattr(args, "font_file", None)
    if font_file:
        path = Path(str(font_file)).expanduser()
        if not path.is_file():
            raise CliError(
                "not_found",
                f"找不到字体文件：{path}。请确认路径是否正确，或改用 --font-family。",
                EXIT_OPERATION,
            )
        return str(path)
    family = getattr(args, "font_family", None)
    if family:
        return resolve_font_file(str(family))
    return None


def _bounded(value: int, low: int, high: int, label: str) -> int:
    if not low <= value <= high:
        raise CliError(
            "usage",
            f"{label} 必须在 {low}–{high} 之间，收到 {value}。",
            EXIT_USAGE,
        )
    return value


def _apply_toggles(pages: list[Matrix], values: list[str] | None) -> None:
    """按 ``页,行,列``（0 基）翻转单元格；格式错误转 ``usage``，越界转 ``invalid_input``。"""
    for raw in values or ():
        page, row, col = _parse_toggle(raw)
        if not 0 <= page < len(pages):
            raise CliError(
                "invalid_input",
                f"--toggle 的页序号 {page} 超出范围：当前共 {len(pages)} 页（0 基）。",
                EXIT_OPERATION,
            )
        try:
            pages[page].toggle(row, col)
        except IndexError as exc:
            raise CliError("invalid_input", str(exc), EXIT_OPERATION) from exc


def _parse_toggle(value: str) -> tuple[int, int, int]:
    parts = [part.strip() for part in str(value).split(",")]
    if len(parts) != 3:
        raise CliError(
            "usage",
            f"--toggle 需要“页,行,列”三个 0 基整数，收到“{value}”。",
            EXIT_USAGE,
        )
    try:
        page, row, col = (int(part) for part in parts)
    except ValueError as exc:
        raise CliError(
            "usage",
            f"--toggle 需要三个整数，收到“{value}”。",
            EXIT_USAGE,
        ) from exc
    return page, row, col


# ---------------------------------------------------------------------------
# 写出
# ---------------------------------------------------------------------------


def _export(args, pages: list[Matrix], params: FontParams, *, default_name: str) -> dict[str, Any]:
    """按 ``--format`` 写出单文件并返回 ``pages``/``width``/``height``/``bytes``/``files``。"""
    from app.core.fontmatrix import matrix_to_c, matrices_to_bytes

    fmt = str(getattr(args, "export_format", "c"))
    if fmt not in _SUFFIXES:
        raise CliError("usage", f"未知导出格式：{fmt}。", EXIT_USAGE)
    name = _strip_name_suffixes(getattr(args, "name", None)) or default_name
    out_dir = Path(getattr(args, "out_dir"))
    target = unique_path(
        out_dir / f"{sanitize_name(name, fallback=default_name)}{_SUFFIXES[fmt]}"
    )
    data = matrices_to_bytes(pages, params)
    try:
        if fmt == "bin":
            write_bytes_atomic(target, data)
        else:
            write_text_atomic(target, matrix_to_c(pages, params, name))
    except OSError as exc:
        raise CliError(
            "permission_denied",
            f"写出点阵失败：{target}（{exc}）。请检查磁盘空间或目录权限。",
            EXIT_OPERATION,
        ) from exc
    return {
        "pages": len(pages),
        "width": max(page.cols for page in pages),
        "height": max(page.rows for page in pages),
        "bytes": len(data),
        "files": [str(target)],
    }


def _strip_name_suffixes(name: str | None) -> str:
    """``--name`` 去掉常见的导出后缀，避免出现 ``letter.c.c``。"""
    requested = str(name).strip() if name is not None else ""
    lowered = requested.casefold()
    for suffix in _NAME_SUFFIXES:
        if lowered.endswith(suffix):
            requested = requested[: -len(suffix)].rstrip(" .")
            break
    return requested

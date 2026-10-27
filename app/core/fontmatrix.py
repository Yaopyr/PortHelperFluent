"""点阵字模核心：文字/图片二值化、扫描排序、位序打包与 C 数组导出。

本模块不依赖 Qt，可被单元测试直接导入。约定 ``Matrix.cells`` 中 ``True`` 表示
“点亮”（黑点），``False`` 表示背景。文字先用 Pillow 渲染为白底黑字，再按阈值
二值化；BMP 图片同理，``invert`` 会把结果取反。

扫描排序：

- ``horizontal`` + ``ltr``：逐行，从左到右；
- ``horizontal`` + ``rtl``：逐行，从右到左；
- ``vertical`` + ``ttb``：逐列，从上到下；
- ``vertical`` + ``btt``：逐列，从下到上。

方向为 ``ttb``/``btt`` 时自动使用纵向扫描，``vertical`` 搭配 ``ltr``/``rtl`` 时
分别映射为 ``ttb``/``btt``，避免界面组合产生歧义。像素按每字节 8 点打包，最后
一个字节不足 8 位时用 0 补齐；``msb`` 表示先出现的像素放在 bit7。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.core.logging_setup import get_logger
from app.services.exports import sanitize_name

SCANS = ("horizontal", "vertical")
DIRECTIONS = ("ltr", "rtl", "ttb", "btt")
BIT_ORDERS = ("msb", "lsb")
MAX_SIZE = 256
MAX_PAGE_CHARS = 256
MAX_DIMENSION = 16384
MAX_PIXELS = 4_000_000
VALUES_PER_LINE = 12
ITALIC_SHEAR = 0.2
_C_KEYWORDS = frozenset(
    {
        "auto", "break", "case", "char", "const", "continue", "default", "do",
        "double", "else", "enum", "extern", "float", "for", "goto", "if",
        "inline", "int", "long", "register", "restrict", "return", "short",
        "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
        "unsigned", "void", "volatile", "while", "_Bool",
    }
)
_SCAN_LABELS = {"horizontal": "水平（逐行）", "vertical": "垂直（逐列）"}
_DIRECTION_LABELS = {
    "ltr": "自左到右",
    "rtl": "自右到左",
    "ttb": "自上到下",
    "btt": "自下到上",
}
_BIT_LABELS = {"msb": "MSB 在前", "lsb": "LSB 在前"}
_FONT_SUFFIX = re.compile(r"\s*\((TrueType|OpenType|All res|PostScript)\)\s*$", re.I)


@dataclass(frozen=True)
class FontParams:
    """文字渲染与取模参数；构造时校验取值范围。"""

    font_path: str | None = None
    size: int = 16
    bold: bool = False
    italic: bool = False
    threshold: int = 128
    invert: bool = False
    scan: str = "horizontal"
    direction: str = "ltr"
    bit_order: str = "msb"
    page_chars: int = 8

    def __post_init__(self) -> None:
        text = None if self.font_path is None else str(self.font_path).strip()
        object.__setattr__(self, "font_path", text or None)
        object.__setattr__(self, "size", _bounded_int(self.size, 1, MAX_SIZE, "字号"))
        object.__setattr__(
            self, "threshold", _bounded_int(self.threshold, 0, 255, "阈值")
        )
        object.__setattr__(
            self, "page_chars", _bounded_int(self.page_chars, 1, MAX_PAGE_CHARS, "每页字符数")
        )
        object.__setattr__(self, "scan", _choice(self.scan, SCANS, "扫描方式"))
        object.__setattr__(
            self, "direction", _choice(self.direction, DIRECTIONS, "取模走向")
        )
        object.__setattr__(
            self, "bit_order", _choice(self.bit_order, BIT_ORDERS, "位序")
        )
        object.__setattr__(self, "bold", bool(self.bold))
        object.__setattr__(self, "italic", bool(self.italic))
        object.__setattr__(self, "invert", bool(self.invert))


@dataclass
class Matrix:
    """二维点阵：``rows`` 行、``cols`` 列，``True`` 表示黑点。"""

    rows: int
    cols: int
    cells: list[list[bool]]

    def __post_init__(self) -> None:
        if not isinstance(self.rows, int) or not isinstance(self.cols, int):
            raise ValueError("点阵行数和列数必须是整数。")
        if self.rows < 0 or self.cols < 0:
            raise ValueError("点阵行数和列数不能为负数。")
        if len(self.cells) != self.rows:
            raise ValueError(
                f"点阵行数不一致：声明 {self.rows} 行，实际 {len(self.cells)} 行。"
            )
        normalized: list[list[bool]] = []
        for index, row in enumerate(self.cells):
            if len(row) != self.cols:
                raise ValueError(
                    f"点阵第 {index + 1} 行列数不一致：应为 {self.cols} 列，实际 {len(row)} 列。"
                )
            normalized.append([bool(cell) for cell in row])
        self.cells = normalized

    def copy(self) -> "Matrix":
        return Matrix(self.rows, self.cols, [list(row) for row in self.cells])

    def toggle(self, row: int, col: int) -> bool:
        """切换一个单元格并返回新状态；越界时抛出 ``IndexError``。"""
        if not 0 <= row < self.rows or not 0 <= col < self.cols:
            raise IndexError(f"单元格 ({row}, {col}) 超出 {self.rows}×{self.cols} 点阵范围。")
        self.cells[row][col] = not self.cells[row][col]
        return self.cells[row][col]

    def is_dot(self, row: int, col: int) -> bool:
        return bool(self.cells[row][col])


def matrix_from_text(text: str, params: FontParams) -> list[Matrix]:
    """把文字渲染为分页点阵；空文字返回空列表。

    多行文字按 ``params.size`` 为基线逐行堆叠；每页宽度为
    ``page_chars * size`` 列，最后一页不足时用白点补齐，便于统一导出尺寸。
    """
    _require_params(params)
    content = str(text).replace("\r\n", "\n").replace("\r", "\n")
    if content == "":
        return []

    font, stroke_width = _load_font(params)
    lines = content.split("\n")
    probe = ImageDraw.Draw(Image.new("L", (1, 1), 255))
    boxes = [
        probe.textbbox((0, 0), line or " ", font=font, stroke_width=stroke_width)
        for line in lines
    ]
    line_heights = [max(1, box[3] - box[1]) for box in boxes]
    line_height = max(params.size, max(line_heights))
    line_widths = [max(1, box[2] - box[0]) for box in boxes]
    width = max(line_widths)
    height = line_height * len(lines)
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise ValueError("文字过长或字号过大，无法生成点阵；请缩短文字或减小字号。")

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        left, top, right, bottom = boxes[index]
        draw.text(
            (-left, index * line_height + (line_height - (bottom - top)) // 2 - top),
            line,
            font=font,
            fill=0,
            stroke_width=stroke_width,
            stroke_fill=0,
        )
    if params.italic:
        image = _italic_shear(image)

    matrix = _matrix_from_gray(image, params.threshold, params.invert)
    return paginate(matrix, params.page_chars, params.size)


def matrix_from_image(path: Path | str, params: FontParams) -> Matrix:
    """读取 BMP/常见位图，按阈值二值化为单页点阵。"""
    _require_params(params)
    source = Path(path)
    if not source.is_file():
        raise ValueError(
            f"找不到图片文件：{source}。请确认路径是否正确、文件是否已被移动。"
        )
    try:
        with Image.open(source) as image:
            image.load()
            gray = image.convert("L")
    except Exception as exc:
        raise ValueError(
            f"无法读取图片“{source}”：文件不是有效的图像，或格式不受支持（{exc}）。"
        ) from exc
    if gray.width * gray.height > MAX_PIXELS:
        raise ValueError(
            f"图片过大（{gray.width}×{gray.height}），最多支持 {MAX_PIXELS} 像素的点阵。"
            "请先缩小图片再取模。"
        )
    return _matrix_from_gray(gray, params.threshold, params.invert)


def paginate(matrix: Matrix, page_chars: int, char_width: int = 8) -> list[Matrix]:
    """按每页字符数水平切分点阵。

    ``char_width`` 是单个字符占用的点数（文字模式取字号）。页面宽度为
    ``page_chars * char_width`` 列；最后一页会用 ``False`` 补齐到相同宽度。
    """
    pages = int(page_chars)
    width = int(char_width)
    if pages < 1 or pages > MAX_PAGE_CHARS:
        raise ValueError(f"每页字符数必须是 1–{MAX_PAGE_CHARS}，当前为 {page_chars}。")
    if width < 1:
        raise ValueError(f"字符宽度必须大于 0，当前为 {char_width}。")
    page_width = pages * width
    if matrix.cols <= page_width or matrix.cols == 0:
        return [matrix]
    result: list[Matrix] = []
    for start in range(0, matrix.cols, page_width):
        width_here = min(page_width, matrix.cols - start)
        cells = [
            row[start : start + width_here] + [False] * (page_width - width_here)
            for row in matrix.cells
        ]
        result.append(Matrix(matrix.rows, page_width, cells))
    return result


def matrix_to_bytes(matrix: Matrix, params: FontParams) -> bytes:
    """按扫描方式、走向与位序把单页点阵打包为字节。"""
    _require_params(params)
    scan, direction = _effective_scan(params.scan, params.direction)
    if scan == "horizontal":
        rows = range(matrix.rows)
        cols = range(matrix.cols) if direction == "ltr" else range(matrix.cols - 1, -1, -1)
        cells = (matrix.cells[row][col] for row in rows for col in cols)
    else:
        cols = range(matrix.cols)
        rows = range(matrix.rows) if direction == "ttb" else range(matrix.rows - 1, -1, -1)
        cells = (matrix.cells[row][col] for col in cols for row in rows)
    return _pack_cells(cells, params.bit_order)


def matrices_to_bytes(matrices: list[Matrix], params: FontParams) -> bytes:
    """按页顺序拼接多页点阵的字节数据。"""
    _require_params(params)
    return b"".join(matrix_to_bytes(matrix, params) for matrix in matrices)


def matrix_to_c(matrices: list[Matrix], params: FontParams, name: str) -> str:
    """生成带尺寸/扫描/位序/页数注释的 C 数组文本。"""
    _require_params(params)
    if not matrices:
        raise ValueError("没有可导出的点阵数据，请先生成点阵。")
    data = matrices_to_bytes(matrices, params)
    width = max(matrix.cols for matrix in matrices)
    height = max(matrix.rows for matrix in matrices)
    identifier = _c_identifier(name)
    display_name = sanitize_name(str(name), fallback="font_matrix")
    scan, direction = _effective_scan(params.scan, params.direction)
    lines = [
        "/*",
        f" * 点阵字模：{display_name}",
        f" * 尺寸：{width} x {height}（宽 x 高，每页 {params.page_chars} 个字符）",
        f" * 扫描方式：{_SCAN_LABELS[scan]}",
        f" * 取模走向：{_DIRECTION_LABELS[direction]}",
        f" * 位序：{_BIT_LABELS[params.bit_order]}",
        f" * 页数：{len(matrices)}",
        f" * 总字节数：{len(data)}",
        " */",
        f"const unsigned char {identifier}[] = {{",
    ]
    for offset in range(0, len(data), VALUES_PER_LINE):
        values = data[offset : offset + VALUES_PER_LINE]
        lines.append("    " + " ".join(f"0x{value:02X}," for value in values))
    lines.append("};")
    return "\n".join(lines) + "\n"


def _require_params(params: FontParams) -> None:
    if not isinstance(params, FontParams):
        raise TypeError("params 必须是 FontParams 实例。")


def _load_font(params: FontParams):
    stroke_width = 1 if params.bold else 0
    if params.font_path:
        candidate = Path(params.font_path)
        try:
            if candidate.is_file():
                return ImageFont.truetype(str(candidate), params.size), stroke_width
        except OSError:
            get_logger().warning("字体文件不可用，改用 Pillow 内置字体：%s", candidate)
    try:
        # Pillow 10.1+ 的 load_default(size=...) 返回可缩放的 FreeType 字体。
        return ImageFont.load_default(size=params.size), stroke_width
    except (TypeError, OSError):
        return ImageFont.load_default(), stroke_width


def _italic_shear(image: Image.Image) -> Image.Image:
    """向右倾斜合成斜体：x' = x + 0.2 * y，并扩展画布避免右侧被裁掉。"""
    extra = int(round(ITALIC_SHEAR * image.height)) + 1
    return image.transform(
        (image.width + extra, image.height),
        Image.Transform.AFFINE,
        (1, -ITALIC_SHEAR, 0, 0, 1, 0),
        resample=Image.Resampling.BICUBIC,
        fillcolor=255,
    )


def _matrix_from_gray(image: Image.Image, threshold: int, invert: bool) -> Matrix:
    width, height = image.size
    pixels = image.tobytes()
    cells = [
        [(pixels[row * width + col] < threshold) != bool(invert) for col in range(width)]
        for row in range(height)
    ]
    return Matrix(height, width, cells)


def _effective_scan(scan: str, direction: str) -> tuple[str, str]:
    if scan == "vertical" or direction in ("ttb", "btt"):
        if direction in ("ttb", "btt"):
            return "vertical", direction
        return "vertical", "ttb" if direction == "ltr" else "btt"
    return "horizontal", direction if direction in ("ltr", "rtl") else "ltr"


def _pack_cells(cells, bit_order: str) -> bytes:
    data = bytearray()
    value = 0
    count = 0
    for cell in cells:
        if cell:
            if bit_order == "msb":
                value |= 1 << (7 - count)
            else:
                value |= 1 << count
        count += 1
        if count == 8:
            data.append(value)
            value = 0
            count = 0
    if count:
        data.append(value)
    return bytes(data)


def _c_identifier(name: str) -> str:
    cleaned = sanitize_name(str(name), fallback="font_matrix")
    chars = [char if (char.isascii() and (char.isalnum() or char == "_")) else "_" for char in cleaned]
    identifier = "".join(chars).strip("_") or "font_matrix"
    if identifier[0].isdigit() or identifier.lower() in _C_KEYWORDS:
        identifier = f"_{identifier}"
    return identifier


@lru_cache(maxsize=1)
def _font_file_map() -> dict[str, str]:
    """字体族/文件名（小写）到字体文件路径的映射；只读系统目录与注册表。"""
    mapping: dict[str, str] = {}
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    try:
        for entry in fonts_dir.iterdir():
            if entry.is_file() and entry.suffix.casefold() in (".ttf", ".ttc", ".otf"):
                mapping.setdefault(entry.name.casefold(), str(entry))
    except OSError:
        pass
    try:
        import winreg
    except ImportError:
        return mapping
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        except OSError:
            continue
        try:
            for index in range(winreg.QueryInfoKey(key)[1]):
                try:
                    label, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    continue
                filename = str(value or "").strip()
                if not filename:
                    continue
                candidate = Path(filename)
                path = candidate if candidate.is_absolute() else fonts_dir / candidate
                if not path.is_file():
                    continue
                base = _FONT_SUFFIX.sub("", str(label)).strip()
                for alias in re.split(r"[&,]", base):
                    key_text = alias.strip().casefold()
                    if key_text:
                        mapping.setdefault(key_text, str(path))
        finally:
            key.Close()
    return mapping


def resolve_font_file(family: str) -> str | None:
    """把字体族名解析为字体文件；无法解析时返回 None（由 Pillow 内置字体兜底）。"""
    text = str(family).strip()
    if not text:
        return None
    mapping = _font_file_map()
    found = mapping.get(text.casefold())
    if found:
        return found
    for suffix in (".ttf", ".ttc", ".otf"):
        found = mapping.get((text + suffix).casefold())
        if found:
            return found
    return None


def _choice(value: object, allowed: tuple[str, ...], label: str) -> str:
    text = str(value).strip().lower()
    if text not in allowed:
        options = "、".join(allowed)
        raise ValueError(f"{label}必须是 {options} 之一，当前为“{value}”。")
    return text


def _bounded_int(value: object, low: int, high: int, label: str) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数，当前为“{value}”。") from exc
    if not low <= number <= high:
        raise ValueError(f"{label}必须在 {low}–{high} 之间，当前为 {number}。")
    return number

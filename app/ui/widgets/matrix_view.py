"""可编辑点阵控件：绘制黑白单元格、网格、点击切换与滚轮缩放。"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPen, QWheelEvent
from PyQt6.QtWidgets import QWidget

from app.core.fontmatrix import Matrix

ZOOM_MIN = 2
ZOOM_MAX = 40
ZOOM_DEFAULT = 12
MARGIN = 8
GRID_COLOR = QColor(208, 208, 208)
EMPTY_TEXT = "尚未生成点阵"


class MatrixView(QWidget):
    """点阵预览/编辑器；左键切换单元格，滚轮缩放，绘制网格。"""

    matrixChanged = pyqtSignal(int, int)
    zoomChanged = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._matrix: Matrix | None = None
        self._zoom = ZOOM_DEFAULT
        self._dot_color = QColor(0, 0, 0)
        self._background = QColor(255, 255, 255)
        self._grid_visible = True
        self.setMinimumSize(200, 200)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("点阵预览")
        self._update_tooltip()

    # ---- 数据 ---------------------------------------------------------

    def set_matrix(self, matrix: Matrix | None) -> None:
        self._matrix = matrix
        self._update_tooltip()
        self.updateGeometry()
        self.update()

    def matrix(self) -> Matrix | None:
        return self._matrix

    def set_zoom(self, zoom: int) -> int:
        """设置每点像素数，自动限制在 2–40；返回实际生效值。"""
        try:
            value = int(zoom)
        except (TypeError, ValueError):
            value = self._zoom
        value = max(ZOOM_MIN, min(ZOOM_MAX, value))
        if value != self._zoom:
            self._zoom = value
            self.updateGeometry()
            self.update()
            self.zoomChanged.emit(value)
        return self._zoom

    def zoom(self) -> int:
        return self._zoom

    def set_colors(self, dot, background) -> None:
        self._dot_color = _to_qcolor(dot)
        self._background = _to_qcolor(background)
        self.update()

    def dot_color(self) -> QColor:
        return QColor(self._dot_color)

    def background_color(self) -> QColor:
        return QColor(self._background)

    def set_grid_visible(self, visible: bool) -> None:
        self._grid_visible = bool(visible)
        self.update()

    def is_grid_visible(self) -> bool:
        return self._grid_visible

    # ---- 编辑 ---------------------------------------------------------

    def cellToggled(self, row: int, col: int) -> bool:
        """切换 (row, col) 单元格；成功时发出 ``matrixChanged``。"""
        matrix = self._matrix
        if matrix is None or not (0 <= row < matrix.rows and 0 <= col < matrix.cols):
            return False
        matrix.toggle(row, col)
        self.update(self.cell_rect(row, col).adjusted(-1, -1, 1, 1))
        self.matrixChanged.emit(row, col)
        return True

    def toggle_cell(self, row: int, col: int) -> bool:
        return self.cellToggled(row, col)

    def cell_rect(self, row: int, col: int) -> QRect:
        origin = self._origin()
        return QRect(origin.x() + col * self._zoom, origin.y() + row * self._zoom,
                     self._zoom, self._zoom)

    def _origin(self) -> QPoint:
        matrix = self._matrix
        if matrix is None:
            return QPoint(MARGIN, MARGIN)
        width = matrix.cols * self._zoom
        height = matrix.rows * self._zoom
        return QPoint(max(MARGIN, (self.width() - width) // 2),
                      max(MARGIN, (self.height() - height) // 2))

    def _cell_at(self, point: QPoint) -> tuple[int, int] | None:
        matrix = self._matrix
        if matrix is None:
            return None
        origin = self._origin()
        col = (point.x() - origin.x()) // self._zoom
        row = (point.y() - origin.y()) // self._zoom
        if 0 <= row < matrix.rows and 0 <= col < matrix.cols:
            return int(row), int(col)
        return None

    # ---- 尺寸与绘制 ---------------------------------------------------

    def sizeHint(self) -> QSize:
        matrix = self._matrix
        if matrix is None:
            return QSize(200, 200)
        width = matrix.cols * self._zoom + 2 * MARGIN
        height = matrix.rows * self._zoom + 2 * MARGIN
        return QSize(max(200, width), max(200, height))

    def minimumSizeHint(self) -> QSize:
        return QSize(200, 200)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background)
        matrix = self._matrix
        if matrix is None or matrix.rows == 0 or matrix.cols == 0:
            painter.setPen(QColor(120, 120, 120))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, EMPTY_TEXT)
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._dot_color)
        origin = self._origin()
        for row in range(matrix.rows):
            cells = matrix.cells[row]
            y = origin.y() + row * self._zoom
            for col in range(matrix.cols):
                if cells[col]:
                    painter.drawRect(
                        origin.x() + col * self._zoom, y, self._zoom, self._zoom
                    )
        if self._grid_visible:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(GRID_COLOR, 1))
            width = matrix.cols * self._zoom
            height = matrix.rows * self._zoom
            for col in range(matrix.cols + 1):
                x = origin.x() + col * self._zoom
                painter.drawLine(x, origin.y(), x, origin.y() + height)
            for row in range(matrix.rows + 1):
                y = origin.y() + row * self._zoom
                painter.drawLine(origin.x(), y, origin.x() + width, y)

    # ---- 交互 ---------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            cell = self._cell_at(event.position().toPoint())
            if cell is not None:
                self.cellToggled(*cell)
                event.accept()
                return
        super().mousePressEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if self._matrix is not None and delta:
            self.set_zoom(self._zoom + (1 if delta > 0 else -1))
            event.accept()
            return
        super().wheelEvent(event)

    def _update_tooltip(self) -> None:
        if self._matrix is None:
            text = "点阵预览：生成后显示；左键切换黑白，滚轮缩放。"
        else:
            text = (
                f"点阵编辑：{self._matrix.cols} 列 × {self._matrix.rows} 行，"
                f"每点 {self._zoom} 像素。左键切换黑白，滚轮缩放。"
            )
        self.setToolTip(text)
        self.setAccessibleDescription(text)


def _to_qcolor(value) -> QColor:
    if isinstance(value, QColor):
        return QColor(value)
    if isinstance(value, str):
        color = QColor(value)
        return color if color.isValid() else QColor(0, 0, 0)
    try:
        red, green, blue = (int(component) for component in value)
    except (TypeError, ValueError):
        return QColor(0, 0, 0)
    return QColor(red, green, blue)

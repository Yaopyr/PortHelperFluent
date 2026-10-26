"""单文件选择控件：文本框 + 浏览按钮 + 拖放。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QWidget
from qfluentwidgets import FluentIcon, LineEdit, ToolButton

from app.ui.qt_utils import safe_slot


class FilePicker(QWidget):
    """文件路径输入；支持浏览与拖入单个文件。"""

    pathChanged = pyqtSignal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        save_mode: bool = False,
        placeholder: str = "选择文件，或把文件拖到这里",
    ) -> None:
        super().__init__(parent)
        self._save_mode = save_mode

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.line_edit = LineEdit(self)
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setClearButtonEnabled(True)
        # 让拖放事件落到 FilePicker 而不是被内部文本框吞掉。
        self.line_edit.setAcceptDrops(False)
        self.line_edit.textChanged.connect(self.pathChanged.emit)
        layout.addWidget(self.line_edit, 1)

        self.browse_button = ToolButton(self)
        self.browse_button.setIcon(FluentIcon.FOLDER)
        self.browse_button.setToolTip("浏览…")
        self.browse_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.browse_button.clicked.connect(self._browse)
        layout.addWidget(self.browse_button)

        self.setAcceptDrops(True)

    def path(self) -> str:
        return self.line_edit.text().strip()

    def set_path(self, path: str | Path) -> None:
        self.line_edit.setText(str(path))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if _single_local_file(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        path = _single_local_file(event)
        if path is None:
            event.ignore()
            return
        self.set_path(path)
        event.acceptProposedAction()

    @safe_slot
    def _browse(self) -> None:
        if self._save_mode:
            path, _selected = QFileDialog.getSaveFileName(
                self, "选择保存位置", self.path(), "所有文件 (*.*)"
            )
        else:
            path, _selected = QFileDialog.getOpenFileName(
                self, "选择文件", self.path(), "所有文件 (*.*)"
            )
        if path:
            self.set_path(path)


def _single_local_file(event: QDragEnterEvent | QDropEvent) -> str | None:
    """只接受恰好一个本地文件；多文件、目录与远程 URL 一律忽略。"""
    mime = event.mimeData()
    if not mime.hasUrls():
        return None
    urls = mime.urls()
    if len(urls) != 1 or not urls[0].isLocalFile():
        return None
    path = Path(urls[0].toLocalFile())
    if not path.is_file():
        return None
    return str(path)

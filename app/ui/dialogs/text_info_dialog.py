"""只读文本信息对话框：用于 Ping 输出、域名解析结果与网卡信息。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CaptionLabel, PrimaryPushButton, PushButton, SubtitleLabel

from app.ui.feedback import show_success
from app.ui.qt_utils import safe_slot


class TextInfoDialog(QDialog):
    """展示一段只读文本，支持一键复制全部内容；由调用方决定 modal 或非 modal。"""

    def __init__(
        self,
        title: str,
        text: str,
        parent: QWidget | None = None,
        *,
        note: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("textInfoDialog")
        self.setWindowTitle(title)
        self.resize(720, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.title_label = SubtitleLabel(title, self)
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        if note:
            self.note_label = CaptionLabel(note, self)
            self.note_label.setWordWrap(True)
            layout.addWidget(self.note_label)
        else:
            self.note_label = None

        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setReadOnly(True)
        self.text_edit.setUndoRedoEnabled(False)
        self.text_edit.setPlainText(text)
        self.text_edit.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.text_edit.setFont(font)
        layout.addWidget(self.text_edit, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.copy_button = PushButton("复制全部", self)
        self.copy_button.clicked.connect(self.copy_all)
        buttons.addWidget(self.copy_button)
        self.close_button = PrimaryPushButton("关闭", self)
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def text(self) -> str:
        return self.text_edit.toPlainText()

    @safe_slot
    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.text_edit.toPlainText())
        show_success(self, "已复制", "文本已复制到剪贴板。")

"""十六进制输入框：即时校验、非法标红，Ctrl+Enter 提交。"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QWidget
from qfluentwidgets import LineEdit

from app.core.encoding import parse_hex
from app.ui.qt_utils import safe_slot

_INVALID_STYLE = "border: 1px solid #C42B1C; border-radius: 4px;"


class HexLineEdit(LineEdit):
    """按 `parse_hex` 规则校验的十六进制输入框。"""

    submit = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._valid = True
        self._error = ""
        self.setPlaceholderText("例如：01 02 0A FF")
        self.textChanged.connect(self._validate)
        self._validate(self.text())

    def is_valid(self) -> bool:
        return self._valid

    def error_message(self) -> str:
        return self._error

    def hex_bytes(self) -> bytes:
        """返回解析后的字节；内容非法时返回空字节，调用方无需再捕获异常。"""
        if not self._valid:
            return b""
        try:
            return parse_hex(self.text())
        except ValueError:
            return b""

    def set_hex_text(self, text: str) -> None:
        self.setText(text)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        is_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if is_enter and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            event.accept()
            self.submit.emit()
            return
        super().keyPressEvent(event)

    @safe_slot
    def _validate(self, text: str) -> None:
        try:
            parse_hex(text)
        except ValueError as exc:
            self._valid = False
            self._error = str(exc)
            self.setStyleSheet(_INVALID_STYLE)
            self.setToolTip(f"十六进制格式错误：{exc}")
            self.setAccessibleDescription(f"十六进制格式错误：{exc}")
            return
        self._valid = True
        self._error = ""
        self.setStyleSheet("")
        self.setToolTip("按 Ctrl+Enter 提交")
        self.setAccessibleDescription("十六进制输入，格式有效")

"""状态胶囊：彩色圆点 + 可读状态文字。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel

CHIP_HEIGHT = 28
_DOT_SIZE = 9

_STATE_COLORS = {
    "idle": "#8A8A8A",
    "ok": "#0F7B0F",
    "busy": "#0A84FF",
    "error": "#C42B1C",
}


class StatusChip(QWidget):
    """固定高度的状态指示：idle / ok / busy / error。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._text = "未连接"

        self.setFixedHeight(CHIP_HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(6)

        self._dot = QWidget(self)
        self._dot.setFixedSize(_DOT_SIZE, _DOT_SIZE)
        layout.addWidget(self._dot)

        self.label = BodyLabel(self._text, self)
        layout.addWidget(self.label)

        self._apply_state()

    @property
    def state(self) -> str:
        return self._state

    def text(self) -> str:
        """当前状态文字（同时用于无障碍名称）。"""
        return self._text

    def set_state(self, state: str, text: str) -> None:
        """更新状态与文字；未知状态按 idle 显示，避免调用方拼错后无提示。"""
        self._state = state if state in _STATE_COLORS else "idle"
        self._text = text
        self.label.setText(text)
        self._apply_state()

    def _apply_state(self) -> None:
        color = _STATE_COLORS[self._state]
        # 圆点用样式表着色，边框透明保证高 DPI 下仍是圆形。
        self._dot.setStyleSheet(
            f"background-color: {color}; border: none; border-radius: {_DOT_SIZE // 2}px;"
        )
        self.setAccessibleName(f"状态：{self._text}")
        self.setAccessibleDescription(f"当前状态代码：{self._state}")
        self.setToolTip(self._text)

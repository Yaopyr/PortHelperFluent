"""空状态：居中图标 + 标题 + 提示，用于无设备、无结果等场景。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon, IconWidget, SubtitleLabel


class EmptyState(QWidget):
    """居中显示的空状态占位。"""

    def __init__(
        self,
        title: str,
        hint: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)
        layout.addStretch(1)

        icon = IconWidget(FluentIcon.INFO, self)
        icon.setFixedSize(32, 32)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)

        title_label = SubtitleLabel(title, self)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        if hint:
            hint_label = CaptionLabel(hint, self)
            hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)

        layout.addStretch(1)
        self.setAccessibleName(title)
        if hint:
            self.setAccessibleDescription(hint)

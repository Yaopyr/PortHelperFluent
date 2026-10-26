"""参数卡片：统一的标题/副标题与内边距。"""

from __future__ import annotations

from PyQt6.QtWidgets import QVBoxLayout
from qfluentwidgets import CaptionLabel, CardWidget, SubtitleLabel

CARD_MARGIN = 16
CARD_SPACING = 10


def section_card(title: str, subtitle: str | None = None) -> tuple[CardWidget, QVBoxLayout]:
    """创建一张参数卡片，返回卡片与已包含标题的纵向布局。"""
    card = CardWidget()
    layout = QVBoxLayout(card)
    layout.setContentsMargins(CARD_MARGIN, CARD_MARGIN, CARD_MARGIN, CARD_MARGIN)
    layout.setSpacing(CARD_SPACING)

    title_label = SubtitleLabel(title, card)
    title_label.setWordWrap(True)
    layout.addWidget(title_label)

    if subtitle:
        subtitle_label = CaptionLabel(subtitle, card)
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)

    return card, layout

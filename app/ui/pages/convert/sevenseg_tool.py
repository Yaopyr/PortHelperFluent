"""七段数码管字型码：输入字符或勾选段，实时生成共阴/共阳编码。"""

from __future__ import annotations

from PyQt6.QtCore import QRegularExpression
from PyQt6.QtGui import QFont, QRegularExpressionValidator
from PyQt6.QtWidgets import QApplication, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    FluentIcon,
    LineEdit,
    ToolButton,
)

from app.core.encoding import (
    SEVEN_SEG_COMMON_CATHODE,
    SEVEN_SEGMENT_BITS,
    SEVEN_SEGMENT_BIT_ORDER,
    seven_segment_code,
)
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card

OUTPUT_ORDER = (
    ("cathode_hex", "共阴 HEX"),
    ("cathode_bin", "共阴 BIN"),
    ("anode_hex", "共阳 HEX"),
    ("anode_bin", "共阳 BIN"),
)
SEGMENT_DESCRIPTIONS = {
    "a": "bit0 · 0x01",
    "b": "bit1 · 0x02",
    "c": "bit2 · 0x04",
    "d": "bit3 · 0x08",
    "e": "bit4 · 0x10",
    "f": "bit5 · 0x20",
    "g": "bit6 · 0x40",
    "dp": "bit7 · 0x80",
}
ERROR_BORDER_RULE = "LineEdit { border: 1px solid #C42B1C; }"
ERROR_COLOR = "#C42B1C"
SEVEN_SEG_HINT = (
    "输入 0–9 或 A–F 自动点亮对应段；也可以直接勾选 a–g/dp 反向生成字型码。"
    "共阳编码等于共阴编码按位取反（保留 8 位）。"
)


class SevenSegTool(QWidget):
    """数码管字型工具；字符与段选双向联动。"""

    title = "数码管"
    route = "sevenseg"

    def __init__(self, settings=None, paths=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.paths = paths
        self.setObjectName("convertSevenSegTool")
        self.segment_checks: dict[str, CheckBox] = {}
        self.output_edits: dict[str, LineEdit] = {}
        self.copy_buttons: dict[str, ToolButton] = {}
        self._normal_stylesheet = ""
        self._syncing = False
        self._cathode_code = 0
        self._build_ui()
        self.digit_edit.setText("0")

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        card, layout = section_card("数码管字型", SEVEN_SEG_HINT)

        digit_row = QHBoxLayout()
        digit_row.setSpacing(10)
        digit_row.addWidget(BodyLabel("字符", card))
        self.digit_edit = LineEdit(card)
        self.digit_edit.setPlaceholderText("0–9 / A–F")
        self.digit_edit.setFixedWidth(110)
        self.digit_edit.setMaxLength(1)
        self.digit_edit.setValidator(
            QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]?"), self)
        )
        self.digit_edit.textChanged.connect(self._on_digit_changed)
        self._normal_stylesheet = self.digit_edit.styleSheet()
        digit_row.addWidget(self.digit_edit)

        self.pattern_label = CaptionLabel("", card)
        self.pattern_label.setWordWrap(True)
        digit_row.addWidget(self.pattern_label, 1)
        layout.addLayout(digit_row)

        segments_label = BodyLabel("段选", card)
        layout.addWidget(segments_label)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        for index, name in enumerate(SEVEN_SEGMENT_BIT_ORDER):
            check = CheckBox(name, card)
            check.setToolTip(SEGMENT_DESCRIPTIONS[name])
            check.toggled.connect(self._on_segment_toggled)
            self.segment_checks[name] = check
            grid.addWidget(check, index // 4, index % 4)
        layout.addLayout(grid)

        self.error_label = CaptionLabel("", card)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {ERROR_COLOR};")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        mono = _mono_font()
        for key, label_text in OUTPUT_ORDER:
            row = QHBoxLayout()
            row.setSpacing(10)
            label = BodyLabel(label_text, card)
            label.setMinimumWidth(64)
            row.addWidget(label)

            edit = LineEdit(card)
            edit.setReadOnly(True)
            edit.setFont(mono)
            edit.setPlaceholderText("—")
            self.output_edits[key] = edit
            row.addWidget(edit, 1)

            button = ToolButton(card)
            button.setIcon(FluentIcon.COPY)
            button.setToolTip(f"复制{label_text}")
            button.clicked.connect(lambda _checked=False, k=key: self.copy_output(k))
            self.copy_buttons[key] = button
            row.addWidget(button)
            layout.addLayout(row)

        self.cathode_hex_edit = self.output_edits["cathode_hex"]
        self.cathode_bin_edit = self.output_edits["cathode_bin"]
        self.anode_hex_edit = self.output_edits["anode_hex"]
        self.anode_bin_edit = self.output_edits["anode_bin"]

        caption = CaptionLabel(
            "段位：a=bit0（0x01）、b=bit1（0x02）、c=0x04、d=0x08、"
            "e=0x10、f=0x20、g=0x40、dp=0x80；共阳 = 共阴按位取反。",
            card,
        )
        caption.setWordWrap(True)
        layout.addWidget(caption)

        root.addWidget(card)
        root.addStretch(1)

    # ---- 联动 ---------------------------------------------------------

    def common_cathode_code(self) -> int:
        return self._cathode_code & 0xFF

    def common_anode_code(self) -> int:
        return (~self._cathode_code) & 0xFF

    @safe_slot
    def _on_digit_changed(self, _text: str, *_args) -> None:
        text = self.digit_edit.text().strip()
        if not text:
            self._set_error("")
            self._clear_segments()
            self._set_outputs(None)
            self.pattern_label.setText("输入字符或勾选段。")
            return
        try:
            code = seven_segment_code(text, common_cathode=True)
        except ValueError as exc:
            self._set_error(str(exc))
            self._clear_segments()
            self._set_outputs(None)
            self.pattern_label.setText("字符无效。")
            return
        self._set_error("")
        self._set_segments_from_code(code)
        self._apply_code(code)

    @safe_slot
    def _on_segment_toggled(self, *_args) -> None:
        if self._syncing:
            return
        code = 0
        for name in SEVEN_SEGMENT_BIT_ORDER:
            if self.segment_checks[name].isChecked():
                code |= SEVEN_SEGMENT_BITS[name]
        self._set_error("")
        self._apply_code(code)

    def _set_segments_from_code(self, code: int) -> None:
        self._syncing = True
        try:
            for name in SEVEN_SEGMENT_BIT_ORDER:
                self.segment_checks[name].setChecked(
                    bool(code & SEVEN_SEGMENT_BITS[name])
                )
        finally:
            self._syncing = False

    def _clear_segments(self) -> None:
        self._set_segments_from_code(0)

    def _apply_code(self, code: int) -> None:
        self._cathode_code = int(code) & 0xFF
        self._set_outputs(self._cathode_code)
        matched = next(
            (
                char
                for char, value in SEVEN_SEG_COMMON_CATHODE.items()
                if value == self._cathode_code
            ),
            None,
        )
        if matched:
            self.pattern_label.setText(f"当前字型：{matched}")
        else:
            self.pattern_label.setText("自定义字型（不对应 0–9/A–F）")

    def _set_outputs(self, code: int | None) -> None:
        if code is None:
            for edit in self.output_edits.values():
                edit.clear()
            return
        cathode = int(code) & 0xFF
        anode = (~cathode) & 0xFF
        self.output_edits["cathode_hex"].setText(f"{cathode:02X}")
        self.output_edits["cathode_bin"].setText(f"{cathode:08b}")
        self.output_edits["anode_hex"].setText(f"{anode:02X}")
        self.output_edits["anode_bin"].setText(f"{anode:08b}")

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
        self.digit_edit.setError(bool(message))
        self.digit_edit.setStyleSheet(
            self._normal_stylesheet + ("\n" + ERROR_BORDER_RULE if message else "")
        )

    @safe_slot
    def copy_output(self, key: str) -> None:
        text = self.output_edits[key].text()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.pattern_label.setText(f"已复制 {text}")


def _mono_font() -> QFont:
    font = QFont("Cascadia Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(10)
    return font

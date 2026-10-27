"""进制转换工具：2–36 进制输入，按位宽/补码输出四种进制。"""

from __future__ import annotations

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon,
    LineEdit,
    ToolButton,
)

from app.core.encoding import BASE_OUTPUT_BASES, base_outputs
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card

OUTPUT_LABELS: dict[int, str] = {
    2: "二进制",
    8: "八进制",
    10: "十进制",
    16: "十六进制",
}
ERROR_BORDER_RULE = "LineEdit { border: 1px solid #C42B1C; }"
ERROR_COLOR = "#C42B1C"
INPUT_HINT = (
    "支持 2–36 进制；可带与进制匹配的 0b/0o/0x 前缀。"
    "有符号模式按二进制补码解释：8 位的 FF 等价于 -1，"
    "输入 -1 会得到对应位宽的全 1 位模式。"
)


class BaseTool(QWidget):
    """进制转换工具页；所有输入变化实时刷新四种输出。"""

    title = "进制转换"
    route = "base"

    def __init__(self, settings=None, paths=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.paths = paths
        self.setObjectName("convertBaseTool")
        self.output_edits: dict[int, LineEdit] = {}
        self.copy_buttons: dict[int, ToolButton] = {}
        self._normal_stylesheet = ""
        self._build_ui()
        self._on_input_changed()

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        card, layout = section_card("进制转换", INPUT_HINT)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        input_row.addWidget(BodyLabel("输入数值", card))
        self.input_edit = LineEdit(card)
        self.input_edit.setPlaceholderText("例如 255、0x1F、-1（有符号模式）")
        self.input_edit.setClearButtonEnabled(True)
        self.input_edit.setMinimumWidth(260)
        self.input_edit.textChanged.connect(self._on_input_changed)
        self._normal_stylesheet = self.input_edit.styleSheet()
        input_row.addWidget(self.input_edit, 1)
        layout.addLayout(input_row)

        options_row = QHBoxLayout()
        options_row.setSpacing(10)
        options_row.addWidget(BodyLabel("输入进制", card))
        self.from_base_combo = ComboBox(card)
        for base in range(2, 37):
            self.from_base_combo.addItem(f"{base} 进制", userData=base)
        decimal_index = self.from_base_combo.findData(10)
        self.from_base_combo.setCurrentIndex(max(0, decimal_index))
        self.from_base_combo.setToolTip("2–36 进制；11 以上使用 A–Z 表示数字")
        self.from_base_combo.currentIndexChanged.connect(self._on_input_changed)
        options_row.addWidget(self.from_base_combo)

        options_row.addSpacing(12)
        options_row.addWidget(BodyLabel("位宽", card))
        self.bits_combo = ComboBox(card)
        self.bits_combo.addItem("32 位", userData=32)
        self.bits_combo.addItem("64 位", userData=64)
        self.bits_combo.setToolTip("决定掩码宽度与有符号十进制范围")
        self.bits_combo.currentIndexChanged.connect(self._on_input_changed)
        options_row.addWidget(self.bits_combo)

        options_row.addSpacing(12)
        self.signed_check = CheckBox("有符号（二进制补码）", card)
        self.signed_check.setToolTip("勾选后十进制显示补码对应的负值")
        self.signed_check.toggled.connect(self._on_input_changed)
        options_row.addWidget(self.signed_check)
        options_row.addStretch(1)
        layout.addLayout(options_row)

        self.error_label = CaptionLabel("", card)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {ERROR_COLOR};")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        mono = _mono_font()
        for base in BASE_OUTPUT_BASES:
            row = QHBoxLayout()
            row.setSpacing(10)
            label = BodyLabel(OUTPUT_LABELS[base], card)
            label.setMinimumWidth(56)
            row.addWidget(label)

            edit = LineEdit(card)
            edit.setReadOnly(True)
            edit.setFont(mono)
            edit.setPlaceholderText("—")
            edit.setMinimumWidth(260)
            self.output_edits[base] = edit
            row.addWidget(edit, 1)

            button = ToolButton(card)
            button.setIcon(FluentIcon.COPY)
            button.setToolTip(f"复制{OUTPUT_LABELS[base]}结果")
            button.clicked.connect(lambda _checked=False, b=base: self.copy_output(b))
            self.copy_buttons[base] = button
            row.addWidget(button)
            layout.addLayout(row)

        self.status_label = CaptionLabel("输入数值后实时显示转换结果。", card)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        caption = CaptionLabel(
            "有符号模式使用二进制补码：负数为对应位宽的反码加一；"
            "二进制/八进制/十六进制始终显示补码位模式。",
            card,
        )
        caption.setWordWrap(True)
        layout.addWidget(caption)

        root.addWidget(card)
        root.addStretch(1)

    # ---- 转换 ---------------------------------------------------------

    def base(self) -> int:
        value = self.from_base_combo.currentData()
        return int(value) if value is not None else 10

    def bits(self) -> int:
        value = self.bits_combo.currentData()
        return int(value) if value is not None else 32

    def signed(self) -> bool:
        return self.signed_check.isChecked()

    def output_values(self) -> dict[int, str]:
        return {base: edit.text() for base, edit in self.output_edits.items()}

    @safe_slot
    def _on_input_changed(self, *_args) -> None:
        text = self.input_edit.text().strip()
        if not text:
            self._clear_outputs()
            self._set_error("")
            self.status_label.setText("输入数值后实时显示转换结果。")
            return
        try:
            values = base_outputs(
                text, self.base(), bits=self.bits(), signed=self.signed()
            )
        except ValueError as exc:
            self._clear_outputs()
            self._set_error(str(exc))
            self.status_label.setText("转换失败，请修正输入。")
            return
        self._set_error("")
        for base, value in values.items():
            self.output_edits[base].setText(value)
        self.status_label.setText("已按输入进制解析并转换。")

    def _clear_outputs(self) -> None:
        for edit in self.output_edits.values():
            edit.clear()

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
        self.input_edit.setError(bool(message))
        self.input_edit.setStyleSheet(
            self._normal_stylesheet + ("\n" + ERROR_BORDER_RULE if message else "")
        )

    @safe_slot
    def copy_output(self, base: int) -> None:
        """把指定进制的输出复制到剪贴板。"""
        text = self.output_edits[base].text()
        if not text:
            self.status_label.setText(f"没有可复制的{OUTPUT_LABELS[base]}结果。")
            return
        QApplication.clipboard().setText(text)
        self.status_label.setText(f"已复制{OUTPUT_LABELS[base]}：{text}")


def _mono_font() -> QFont:
    font = QFont("Cascadia Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(10)
    return font

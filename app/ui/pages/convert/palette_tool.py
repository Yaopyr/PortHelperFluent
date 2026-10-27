"""调色板工具：颜色选择器、HEX/RGB 输入与 RGB888/565/332 输出。"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    LineEdit,
    PushButton,
    SpinBox,
    ToolButton,
)

from app.core.color import (
    decode_332,
    decode_565,
    hex_to_rgb,
    rgb_to_332,
    rgb_to_565,
    rgb_to_888,
    rgb_to_hex,
)
from app.core.logging_setup import get_logger
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card

DEFAULT_COLOR = "#0A84FF"
SETTINGS_KEY = "convert.palette.color"
OUTPUT_ORDER = (
    ("hex", "24 位 HEX"),
    ("rgb888", "RGB888"),
    ("rgb565", "RGB565"),
    ("rgb332", "RGB332"),
)
OUTPUT_LABELS = dict(OUTPUT_ORDER)
ERROR_BORDER_RULE = "LineEdit { border: 1px solid #C42B1C; }"
ERROR_COLOR = "#C42B1C"
PALETTE_HINT = "用于彩屏与二维码位图调色"


class PaletteTool(QWidget):
    """颜色编码工具；HEX、RGB 输入与三种硬件色深输出实时联动。"""

    title = "调色板"
    route = "palette"

    def __init__(self, settings=None, paths=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.paths = paths
        self.setObjectName("convertPaletteTool")
        self.output_edits: dict[str, LineEdit] = {}
        self.copy_buttons: dict[str, ToolButton] = {}
        self._syncing = False
        self._normal_stylesheet = ""
        self._build_ui()
        self._load_saved_color()

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        card, layout = section_card("调色板", PALETTE_HINT)

        color_row = QHBoxLayout()
        color_row.setSpacing(14)
        self.swatch_label = QLabel(card)
        self.swatch_label.setFixedSize(76, 76)
        self.swatch_label.setToolTip("当前颜色")
        color_row.addWidget(self.swatch_label)

        picker_column = QVBoxLayout()
        picker_column.setSpacing(8)
        self.choose_button = PushButton("选择颜色…", card)
        self.choose_button.setIcon(FluentIcon.PALETTE)
        self.choose_button.setToolTip("打开系统颜色选择器")
        self.choose_button.clicked.connect(self._choose_color)
        picker_column.addWidget(self.choose_button)

        hex_row = QHBoxLayout()
        hex_row.setSpacing(8)
        hex_row.addWidget(BodyLabel("HEX", card))
        self.hex_edit = LineEdit(card)
        self.hex_edit.setPlaceholderText("#RRGGBB")
        self.hex_edit.setClearButtonEnabled(True)
        self.hex_edit.setFixedWidth(150)
        self.hex_edit.textChanged.connect(self._on_hex_changed)
        self._normal_stylesheet = self.hex_edit.styleSheet()
        hex_row.addWidget(self.hex_edit)
        picker_column.addLayout(hex_row)

        rgb_row = QHBoxLayout()
        rgb_row.setSpacing(8)
        rgb_row.addWidget(BodyLabel("R", card))
        self.red_spin = _channel_spin(card, self)
        rgb_row.addWidget(self.red_spin)
        rgb_row.addWidget(BodyLabel("G", card))
        self.green_spin = _channel_spin(card, self)
        rgb_row.addWidget(self.green_spin)
        rgb_row.addWidget(BodyLabel("B", card))
        self.blue_spin = _channel_spin(card, self)
        rgb_row.addWidget(self.blue_spin)
        rgb_row.addStretch(1)
        picker_column.addLayout(rgb_row)

        color_row.addLayout(picker_column, 1)
        layout.addLayout(color_row)

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
            label.setMinimumWidth(80)
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

        self.detail_label = CaptionLabel("", card)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.status_label = CaptionLabel("输入 HEX 或 RGB 后实时显示编码结果。", card)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        caption = CaptionLabel(
            "RGB565 为 rrrrrggggggbbbbb（16 位），RGB332 为 rrrgggbb（8 位）；"
            "可直接用于彩屏驱动与二维码位图调色。",
            card,
        )
        caption.setWordWrap(True)
        layout.addWidget(caption)

        root.addWidget(card)
        root.addStretch(1)

    # ---- 状态 ---------------------------------------------------------

    def current_rgb(self) -> tuple[int, int, int]:
        return (
            self.red_spin.value(),
            self.green_spin.value(),
            self.blue_spin.value(),
        )

    def hex_value(self) -> str:
        return rgb_to_hex(self.current_rgb())

    def output_values(self) -> dict[str, str]:
        return {key: edit.text() for key, edit in self.output_edits.items()}

    def set_rgb(self, red: int, green: int, blue: int) -> None:
        """设置当前颜色并刷新全部输出；非法分量抛出中文 ``ValueError``。"""
        color = rgb_to_hex((red, green, blue))
        self._syncing = True
        try:
            self.hex_edit.setText(color)
            self.red_spin.setValue(int(red))
            self.green_spin.setValue(int(green))
            self.blue_spin.setValue(int(blue))
        finally:
            self._syncing = False
        self._set_error("")
        self._apply_color((int(red), int(green), int(blue)), persist=False)

    def set_hex(self, text: str) -> None:
        """按 HEX 文本设置颜色；解析失败抛出中文 ``ValueError``。"""
        self.set_rgb(*hex_to_rgb(text))

    def copy_output(self, key: str) -> None:
        """把指定编码结果复制到剪贴板。"""
        text = self.output_edits[key].text()
        if not text:
            self.status_label.setText(f"没有可复制的{OUTPUT_LABELS[key]}结果。")
            return
        QApplication.clipboard().setText(text)
        self.status_label.setText(f"已复制{OUTPUT_LABELS[key]}：{text}")

    # ---- 联动 ---------------------------------------------------------

    def _load_saved_color(self) -> None:
        saved = DEFAULT_COLOR
        if self.settings is not None:
            try:
                saved = str(self.settings.get(SETTINGS_KEY, DEFAULT_COLOR) or DEFAULT_COLOR)
            except Exception:
                get_logger().exception("读取调色板上次颜色失败")
        try:
            rgb = hex_to_rgb(saved)
        except ValueError:
            rgb = hex_to_rgb(DEFAULT_COLOR)
        self.set_rgb(*rgb)

    @safe_slot
    def _on_hex_changed(self, _text: str, *_args) -> None:
        if self._syncing:
            return
        text = self.hex_edit.text().strip()
        if not text:
            self._syncing = True
            try:
                self.red_spin.setValue(0)
                self.green_spin.setValue(0)
                self.blue_spin.setValue(0)
            finally:
                self._syncing = False
            self._set_outputs(None)
            self.detail_label.setText("")
            self._set_error("")
            self.status_label.setText("输入 HEX 或 RGB 后实时显示编码结果。")
            return
        try:
            rgb = hex_to_rgb(text)
        except ValueError as exc:
            self._set_outputs(None)
            self.detail_label.setText("")
            self._set_error(str(exc))
            self.status_label.setText("HEX 格式无效，请修正输入。")
            return
        self._syncing = True
        try:
            self.red_spin.setValue(rgb[0])
            self.green_spin.setValue(rgb[1])
            self.blue_spin.setValue(rgb[2])
        finally:
            self._syncing = False
        self._set_error("")
        self._apply_color(rgb, persist=False)

    @safe_slot
    def _on_channel_changed(self, *_args) -> None:
        if self._syncing:
            return
        rgb = self.current_rgb()
        self._syncing = True
        try:
            self.hex_edit.setText(rgb_to_hex(rgb))
        finally:
            self._syncing = False
        self._set_error("")
        self._apply_color(rgb, persist=False)

    def _apply_color(
        self, rgb: tuple[int, int, int], *, persist: bool
    ) -> None:
        red, green, blue = rgb
        hex_text = rgb_to_hex(rgb)
        value_565 = rgb_to_565(rgb)
        value_332 = rgb_to_332(rgb)
        self.swatch_label.setStyleSheet(
            f"background-color: {hex_text}; border: 1px solid #8A8A8A; border-radius: 8px;"
        )
        self.output_edits["hex"].setText(hex_text)
        self.output_edits["rgb888"].setText(f"0x{rgb_to_888(rgb):06X}")
        self.output_edits["rgb565"].setText(f"0x{value_565:04X}")
        self.output_edits["rgb332"].setText(f"0x{value_332:02X}")
        decoded_565 = decode_565(value_565)
        decoded_332 = decode_332(value_332)
        self.detail_label.setText(
            f"RGB({red}, {green}, {blue}) · RGB888 {rgb_to_888(rgb)} · "
            f"RGB565 {value_565}（还原 {decoded_565[0]},{decoded_565[1]},{decoded_565[2]}） · "
            f"RGB332 {value_332}（还原 {decoded_332[0]},{decoded_332[1]},{decoded_332[2]}）"
        )
        self.status_label.setText(f"当前颜色：{hex_text}")
        if persist:
            self._remember_color(hex_text)

    def _set_outputs(self, rgb: tuple[int, int, int] | None) -> None:
        if rgb is not None:
            self._apply_color(rgb, persist=False)
            return
        for edit in self.output_edits.values():
            edit.clear()

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
        self.hex_edit.setError(bool(message))
        self.hex_edit.setStyleSheet(
            self._normal_stylesheet + ("\n" + ERROR_BORDER_RULE if message else "")
        )

    @safe_slot
    def _choose_color(self) -> None:
        color = QColorDialog.getColor(
            QColor(self.hex_value()), self, "选择颜色"
        )
        if not color.isValid():
            return
        self.set_rgb(color.red(), color.green(), color.blue())
        self._remember_color(self.hex_value())

    def _remember_color(self, hex_text: str) -> None:
        if self.settings is None:
            return
        try:
            self.settings.set(SETTINGS_KEY, hex_text)
            self.settings.save()
        except Exception:
            get_logger().exception("保存调色板上次颜色失败")


def _channel_spin(parent: QWidget, owner: "PaletteTool") -> SpinBox:
    spin = SpinBox(parent)
    spin.setRange(0, 255)
    spin.setFixedWidth(72)
    spin.valueChanged.connect(owner._on_channel_changed)
    return spin


def _mono_font() -> QFont:
    font = QFont("Cascadia Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(10)
    return font

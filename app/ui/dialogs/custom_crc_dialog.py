"""自定义 CRC 对话框：按 Rocksoft/RevEng 参数计算任意 8/16/32 位 CRC。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
)

from app.core.checksum import CrcAccumulator, CrcParams, format_value
from app.core.encoding import encode_text, parse_hex
from app.ui.feedback import show_success
from app.ui.qt_utils import safe_slot

WIDTH_CHOICES = ((8, "8 位"), (16, "16 位"), (32, "32 位"))
# 位宽 -> (poly, init, xorout) 的十六进制默认值；16 位默认即 CCITT-FALSE。
DEFAULT_PARAMS: dict[int, tuple[str, str, str]] = {
    8: ("07", "00", "00"),
    16: ("1021", "FFFF", "0000"),
    32: ("04C11DB7", "FFFFFFFF", "FFFFFFFF"),
}
_ERROR_STYLE = "color: #C42B1C;"


def build_crc_params(
    width: int,
    poly_text: str,
    init_text: str,
    xorout_text: str,
    refin: bool,
    refout: bool,
) -> CrcParams:
    """校验界面输入并构造 `CrcParams`；错误信息为可直接展示的中文。

    位宽只允许 8/16/32；poly/init/xorout 接受 `0x` 前缀与下划线分隔，必须落在
    对应位宽范围内。参数错误统一抛 `ValueError`，不产生半计算的 CRC。
    """
    if isinstance(width, bool) or width not in (8, 16, 32):
        raise ValueError(f"CRC 位宽只支持 8/16/32 位，收到 {width!r}。")
    poly = _parse_hex_parameter(poly_text, "多项式 poly", width)
    init = _parse_hex_parameter(init_text, "初始值 init", width)
    xorout = _parse_hex_parameter(xorout_text, "结果异或 xorout", width)
    try:
        return CrcParams(
            width=width,
            poly=poly,
            init=init,
            refin=bool(refin),
            refout=bool(refout),
            xorout=xorout,
        )
    except ValueError as exc:
        raise ValueError(f"CRC 参数无效：{exc}") from None


def _parse_hex_parameter(text: str, label: str, width: int) -> int:
    raw = str(text or "").strip().replace("_", "").replace(" ", "")
    if raw.lower().startswith("0x"):
        raw = raw[2:]
    if not raw:
        raise ValueError(f"{label}不能为空，请输入不超过 {width} 位的十六进制数。")
    try:
        value = int(raw, 16)
    except ValueError:
        raise ValueError(
            f"{label} 必须是十六进制数（例如 0x1021），收到“{text}”。"
        ) from None
    limit = (1 << width) - 1
    if value < 0:
        raise ValueError(f"{label} 不能为负数。")
    if value > limit:
        raise ValueError(
            f"{label}=0x{value:X} 超出 {width} 位范围（最大 0x{limit:X}）。"
        )
    return value


class CustomCrcDialog(QDialog):
    """自定义 CRC 参数与数据输入；所有计算在 GUI 线程内进行（数据量通常很小）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("customCrcDialog")
        self.setWindowTitle("自定义 CRC")
        self.resize(560, 560)
        self._last_result: tuple[CrcParams, int] | None = None
        self._loading = True

        self._build_ui()
        default_index = self.width_combo.findData(16)
        if default_index >= 0:
            self.width_combo.setCurrentIndex(default_index)
        self._load_defaults(16)
        self._loading = False

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(SubtitleLabel("自定义 CRC", self))
        hint = CaptionLabel(
            "按 Rocksoft/RevEng 模型计算；文本框内容按 UTF-8 编码为字节，"
            "勾选 HEX 时按十六进制字节解析。",
            self,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        grid.addWidget(BodyLabel("位宽", self), 0, 0)
        self.width_combo = ComboBox(self)
        for width, label in WIDTH_CHOICES:
            self.width_combo.addItem(label, userData=width)
        self.width_combo.currentIndexChanged.connect(self._on_width_changed)
        grid.addWidget(self.width_combo, 0, 1)

        self.poly_edit = self._parameter_edit(grid, 1, "多项式 poly")
        self.init_edit = self._parameter_edit(grid, 2, "初始值 init")
        self.xorout_edit = self._parameter_edit(grid, 3, "结果异或 xorout")

        grid.addWidget(BodyLabel("反射", self), 4, 0)
        reflect_row = QHBoxLayout()
        reflect_row.setSpacing(14)
        self.refin_check = CheckBox("输入反射 refin", self)
        self.refin_check.setToolTip("每个输入字节按位反转后再参与计算")
        reflect_row.addWidget(self.refin_check)
        self.refout_check = CheckBox("输出反射 refout", self)
        self.refout_check.setToolTip("输出前把结果按位反转")
        reflect_row.addWidget(self.refout_check)
        reflect_row.addStretch(1)
        grid.addLayout(reflect_row, 4, 1)
        layout.addLayout(grid)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        input_row.addWidget(BodyLabel("输入数据", self))
        self.hex_check = CheckBox("HEX 输入", self)
        self.hex_check.setToolTip("勾选后按空格/逗号分隔的十六进制字节解析")
        input_row.addWidget(self.hex_check)
        input_row.addStretch(1)
        layout.addLayout(input_row)

        self.input_edit = PlainTextEdit(self)
        self.input_edit.setPlaceholderText("输入要计算 CRC 的数据，例如：123456789")
        self.input_edit.setMinimumHeight(96)
        layout.addWidget(self.input_edit, 1)

        self.error_label = CaptionLabel("", self)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(_ERROR_STYLE)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.result_label = BodyLabel("结果：—", self)
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.result_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.compute_button = PrimaryPushButton("计算", self)
        self.compute_button.setToolTip("按当前参数计算 CRC（输入为空时提示）")
        self.compute_button.clicked.connect(self.run_compute)
        buttons.addWidget(self.compute_button)
        buttons.addStretch(1)
        self.copy_button = PushButton("复制结果", self)
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_result)
        buttons.addWidget(self.copy_button)
        self.close_button = PushButton("关闭", self)
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def _parameter_edit(self, grid: QGridLayout, row: int, label: str) -> LineEdit:
        grid.addWidget(BodyLabel(label, self), row, 0)
        edit = LineEdit(self)
        edit.setPlaceholderText("十六进制，例如 0x1021")
        edit.setToolTip(f"{label}：十六进制数，可加 0x 前缀")
        grid.addWidget(edit, row, 1)
        return edit

    # ---- 参数与计算 ---------------------------------------------------

    def current_width(self) -> int:
        width = self.width_combo.currentData()
        return int(width) if width in (8, 16, 32) else 16

    def parse_params(self) -> CrcParams:
        return build_crc_params(
            self.current_width(),
            self.poly_edit.text(),
            self.init_edit.text(),
            self.xorout_edit.text(),
            self.refin_check.isChecked(),
            self.refout_check.isChecked(),
        )

    def read_data(self) -> bytes:
        text = self.input_edit.toPlainText()
        if self.hex_check.isChecked():
            return parse_hex(text)
        if not text:
            raise ValueError("请输入要计算 CRC 的数据。")
        try:
            return encode_text(text, "utf-8")
        except UnicodeError as exc:
            raise ValueError(f"文本无法按 UTF-8 编码：{exc}") from None

    @safe_slot
    def run_compute(self, *_args) -> int | None:
        """计算并展示结果；输入有误时在对话框内就地提示并返回 None。"""
        try:
            params = self.parse_params()
            data = self.read_data()
        except ValueError as exc:
            self._show_error(str(exc))
            return None
        accumulator = CrcAccumulator(params)
        accumulator.update(data)
        value = accumulator.result()
        self._last_result = (params, value)
        self.error_label.hide()
        self.result_label.setText(
            f"结果：0x{format_value(value, params.width, 16)}"
            f"（{value}，{params.width} 位）"
        )
        self.copy_button.setEnabled(True)
        return value

    def result_value(self) -> int | None:
        return self._last_result[1] if self._last_result is not None else None

    def result_hex(self) -> str:
        """返回不带 0x 前缀的大写十六进制结果；尚未计算时返回空串。"""
        if self._last_result is None:
            return ""
        params, value = self._last_result
        return format_value(value, params.width, 16)

    @safe_slot
    def copy_result(self, *_args) -> None:
        text = self.result_hex()
        if not text:
            return
        QApplication.clipboard().setText(text)
        show_success(self, "已复制 CRC 结果", text)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()
        self.result_label.setText("结果：—")
        self.copy_button.setEnabled(False)
        self._last_result = None

    # ---- 槽 -----------------------------------------------------------

    @safe_slot
    def _on_width_changed(self, *_args) -> None:
        if self._loading:
            return
        self._load_defaults(self.current_width())

    def _load_defaults(self, width: int) -> None:
        poly, init, xorout = DEFAULT_PARAMS.get(width, DEFAULT_PARAMS[16])
        self.poly_edit.setText(poly)
        self.init_edit.setText(init)
        self.xorout_edit.setText(xorout)
        self._last_result = None
        self.result_label.setText("结果：—")
        self.copy_button.setEnabled(False)
        self.error_label.hide()

"""二维码工具：文本、纠错等级、尺寸、边框与前景/背景色，预览并保存。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import (
    QColorDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
)

from app.core.color import hex_to_rgb, rgb_to_hex
from app.core.logging_setup import get_logger
from app.core.qr_tools import EC_LEVELS, make_qr, save_qr
from app.ui.feedback import show_error, show_success
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.file_picker import FilePicker

ERROR_COLOR = "#C42B1C"
SETTINGS_LAST_PATH = "convert.qr.last_path"
DEFAULT_FG = "#000000"
DEFAULT_BG = "#FFFFFF"
PREVIEW_SIZE = 220
QR_HINT = (
    "输入文本生成二维码，可选纠错等级、模块尺寸、静区边框与前景/背景色；"
    "预览后保存为 PNG/BMP。仅生成二维码，不提供扫码识别。"
)


class QrTool(QWidget):
    """二维码生成工具；预览与保存逻辑可直接在测试中调用。"""

    title = "二维码"
    route = "qr"

    def __init__(self, settings=None, paths=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.paths = paths
        self.setObjectName("convertQrTool")
        self._image: Image.Image | None = None
        self._fg = DEFAULT_FG
        self._bg = DEFAULT_BG
        self._build_ui()
        self._restore_last_path()
        self.refresh_preview()

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        card, layout = section_card("二维码", QR_HINT)

        layout.addWidget(BodyLabel("文本内容", card))
        self.text_edit = PlainTextEdit(card)
        self.text_edit.setPlaceholderText("输入要编码的文本；支持中文与多行内容")
        self.text_edit.setMinimumHeight(70)
        self.text_edit.setMaximumHeight(92)
        self.text_edit.textChanged.connect(self._on_content_changed)
        layout.addWidget(self.text_edit)

        params_row = QHBoxLayout()
        params_row.setSpacing(10)
        params_row.addWidget(BodyLabel("纠错等级", card))
        self.ec_combo = ComboBox(card)
        for level in EC_LEVELS:
            self.ec_combo.addItem(level, userData=level)
        self.ec_combo.setCurrentIndex(max(0, self.ec_combo.findData("M")))
        self.ec_combo.setToolTip("L 约 7%、M 约 15%、Q 约 25%、H 约 30% 的容错能力")
        self.ec_combo.currentIndexChanged.connect(self._on_parameter_changed)
        params_row.addWidget(self.ec_combo)

        params_row.addSpacing(8)
        params_row.addWidget(BodyLabel("模块尺寸", card))
        self.box_spin = SpinBox(card)
        self.box_spin.setRange(1, 40)
        self.box_spin.setValue(8)
        self.box_spin.setSuffix(" px")
        self.box_spin.setFixedWidth(110)
        self.box_spin.valueChanged.connect(self._on_parameter_changed)
        params_row.addWidget(self.box_spin)

        params_row.addSpacing(8)
        params_row.addWidget(BodyLabel("边框", card))
        self.border_spin = SpinBox(card)
        self.border_spin.setRange(0, 20)
        self.border_spin.setValue(4)
        self.border_spin.setSuffix(" 模块")
        self.border_spin.setFixedWidth(120)
        self.border_spin.valueChanged.connect(self._on_parameter_changed)
        params_row.addWidget(self.border_spin)
        params_row.addStretch(1)
        layout.addLayout(params_row)

        color_row = QHBoxLayout()
        color_row.setSpacing(10)
        color_row.addWidget(BodyLabel("颜色", card))
        self.fg_button = PushButton("", card)
        self.fg_button.setToolTip("选择二维码模块（前景）颜色")
        self.fg_button.clicked.connect(self._choose_fg)
        color_row.addWidget(self.fg_button)
        self.bg_button = PushButton("", card)
        self.bg_button.setToolTip("选择静区（背景）颜色")
        self.bg_button.clicked.connect(self._choose_bg)
        color_row.addWidget(self.bg_button)
        color_row.addStretch(1)
        layout.addLayout(color_row)
        self._update_color_buttons()

        self.preview_label = QLabel(card)
        self.preview_label.setFixedSize(PREVIEW_SIZE, PREVIEW_SIZE)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet(
            "background-color: #FFFFFF; border: 1px solid #8A8A8A; color: #666666;"
        )
        self.preview_label.setText("输入内容后生成预览")
        layout.addWidget(self.preview_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        output_row = QHBoxLayout()
        output_row.setSpacing(10)
        output_label = BodyLabel("保存路径", card)
        output_label.setMinimumWidth(64)
        output_row.addWidget(output_label)
        self.output_picker = FilePicker(
            card, save_mode=True, placeholder="选择 .png 或 .bmp 保存路径"
        )
        output_row.addWidget(self.output_picker, 1)
        layout.addLayout(output_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(12)
        self.save_button = PrimaryPushButton("保存图片", card)
        self.save_button.clicked.connect(self._on_save_clicked)
        action_row.addWidget(self.save_button)
        self.status_label = CaptionLabel("输入文本后实时生成预览。", card)
        self.status_label.setWordWrap(True)
        action_row.addWidget(self.status_label, 1)
        layout.addLayout(action_row)

        self.error_label = CaptionLabel("", card)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {ERROR_COLOR};")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        root.addWidget(card)
        root.addStretch(1)

    # ---- 参数 ---------------------------------------------------------

    def text(self) -> str:
        return self.text_edit.toPlainText()

    def set_text(self, text: str) -> None:
        self.text_edit.setPlainText(str(text))

    def ec(self) -> str:
        value = self.ec_combo.currentData()
        return str(value) if value else "M"

    def box_size(self) -> int:
        return int(self.box_spin.value())

    def border(self) -> int:
        return int(self.border_spin.value())

    def fg_color(self) -> str:
        return self._fg

    def bg_color(self) -> str:
        return self._bg

    def set_colors(self, fg: str, bg: str) -> None:
        """设置前景/背景色；颜色无法解析时抛出中文 ``ValueError``。"""
        foreground = rgb_to_hex(hex_to_rgb(fg))
        background = rgb_to_hex(hex_to_rgb(bg))
        self._fg = foreground
        self._bg = background
        self._update_color_buttons()
        self.refresh_preview()

    def qr_image(self) -> Image.Image:
        """按当前参数生成二维码；文本为空等情况抛出中文 ``ValueError``。"""
        return make_qr(
            self.text(),
            ec=self.ec(),
            box_size=self.box_size(),
            border=self.border(),
            fg=self._fg,
            bg=self._bg,
        )

    def _update_color_buttons(self) -> None:
        for button, label, color in (
            (self.fg_button, "前景色", self._fg),
            (self.bg_button, "背景色", self._bg),
        ):
            button.setText(f"{label} {color}")

    @safe_slot
    def _choose_fg(self) -> None:
        color = QColorDialog.getColor(QColor(self._fg), self, "选择前景色")
        if color.isValid():
            self.set_colors(rgb_to_hex((color.red(), color.green(), color.blue())), self._bg)

    @safe_slot
    def _choose_bg(self) -> None:
        color = QColorDialog.getColor(QColor(self._bg), self, "选择背景色")
        if color.isValid():
            self.set_colors(self._fg, rgb_to_hex((color.red(), color.green(), color.blue())))

    @safe_slot
    def _on_content_changed(self, *_args) -> None:
        self.refresh_preview()

    @safe_slot
    def _on_parameter_changed(self, *_args) -> None:
        self.refresh_preview()

    # ---- 预览与保存 ---------------------------------------------------

    def refresh_preview(self) -> Image.Image | None:
        """重新生成预览；空文本或参数错误时清空预览并显示内联提示。"""
        if not self.text().strip():
            self._image = None
            self.preview_label.clear()
            self.preview_label.setText("输入内容后生成预览")
            self._set_error("")
            self.status_label.setText("输入文本后实时生成预览。")
            return None
        try:
            image = self.qr_image()
        except ValueError as exc:
            self._image = None
            self.preview_label.clear()
            self.preview_label.setText("预览生成失败")
            self._set_error(str(exc))
            self.status_label.setText("请修正参数后重试。")
            return None
        self._image = image
        pixmap = _pil_to_pixmap(image)
        self.preview_label.setPixmap(
            pixmap.scaled(
                self.preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._set_error("")
        self.status_label.setText(
            f"已生成 {image.width}×{image.height} 像素二维码（纠错 {self.ec()}）。"
        )
        return image

    def save(self, path: str | Path | None = None) -> Path:
        """保存当前二维码；不弹出对话框，失败抛出中文异常。"""
        image = self.qr_image()
        path_text = str(path) if path is not None else self.output_picker.path().strip()
        if not path_text.strip():
            raise ValueError("请选择二维码保存路径。")
        target = Path(path_text.strip())
        if not target.suffix:
            target = target.with_suffix(".png")
        save_qr(image, target)
        self._image = image
        self.output_picker.set_path(target)
        self._set_error("")
        self.status_label.setText(f"已保存二维码：{target}")
        self._remember_last_path(target)
        return target

    def _default_save_path(self) -> Path:
        text = self.output_picker.path().strip()
        if text:
            return Path(text)
        if self.settings is not None:
            try:
                saved = str(self.settings.get(SETTINGS_LAST_PATH, "") or "")
            except Exception:
                saved = ""
            if saved:
                return Path(saved)
        return Path.home() / "qrcode.png"

    @safe_slot
    def _on_save_clicked(self, *_args) -> None:
        if not self.output_picker.path().strip():
            selected, _filter = QFileDialog.getSaveFileName(
                self,
                "保存二维码",
                str(self._default_save_path()),
                "PNG 图像 (*.png);;BMP 图像 (*.bmp)",
            )
            if not selected:
                return
            self.output_picker.set_path(selected)
        try:
            target = self.save()
        except Exception as exc:
            self._set_error(str(exc))
            show_error(self, "保存失败", str(exc))
            return
        show_success(self, "保存完成", str(target))

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _remember_last_path(self, path: Path) -> None:
        if self.settings is None:
            return
        try:
            self.settings.set(SETTINGS_LAST_PATH, str(path))
            self.settings.save()
        except Exception:
            get_logger().exception("保存二维码上次路径失败")

    def _restore_last_path(self) -> None:
        if self.settings is None:
            return
        try:
            saved = str(self.settings.get(SETTINGS_LAST_PATH, "") or "")
        except Exception:
            get_logger().exception("读取二维码上次路径失败")
            return
        if saved:
            self.output_picker.set_path(saved)


def _pil_to_pixmap(image: Image.Image) -> QPixmap:
    rgba = image.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimage = QImage(
        data,
        rgba.width,
        rgba.height,
        rgba.width * 4,
        QImage.Format.Format_RGBA8888,
    ).copy()
    return QPixmap.fromImage(qimage)

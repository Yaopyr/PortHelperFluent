"""点阵生成页：文字/BMP 取模、可分页编辑预览与 C/BIN/批量导出。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QFontInfo, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SegmentedWidget,
    SpinBox,
    TitleLabel,
    ToolButton,
)

from app.core.color import hex_to_rgb, rgb_to_hex
from app.core.fontmatrix import (
    FontParams,
    Matrix,
    matrix_from_image,
    matrix_from_text,
    matrix_to_c,
    matrices_to_bytes,
    paginate,
    resolve_font_file,
)
from app.core.imaging import BatchResult, list_image_files
from app.core.logging_setup import get_logger
from app.services.exports import sanitize_name, unique_path, write_bytes_atomic, write_text_atomic
from app.ui.feedback import show_error, show_success
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.file_picker import FilePicker
from app.ui.widgets.matrix_view import ZOOM_DEFAULT, ZOOM_MAX, ZOOM_MIN, MatrixView

__all__ = ["DotMatrixPage", "resolve_font_file"]
ERROR_COLOR = "#C42B1C"
SETTINGS = "dotmatrix."
MODE_TEXT, MODE_BMP = "text", "bmp"
MODE_ITEMS = (("文字", MODE_TEXT), ("BMP 图片", MODE_BMP))
SCAN_ITEMS = (("水平（逐行）", "horizontal"), ("垂直（逐列）", "vertical"))
DIRECTION_ITEMS = (("自左到右", "ltr"), ("自右到左", "rtl"), ("自上到下", "ttb"), ("自下到上", "btt"))
BIT_ORDER_ITEMS = (("MSB 在前", "msb"), ("LSB 在前", "lsb"))
DEFAULT_DOT_COLOR, DEFAULT_BACKGROUND = (0, 0, 0), (255, 255, 255)
DEFAULT_TEXT = "点阵"
MAX_CODE_LINES = 80
FALLBACK_FAMILIES = ("Microsoft YaHei UI", "Microsoft YaHei", "SimSun", "Arial", "Consolas", "Courier New")
INPUT_HINT = (
    "文字模式使用系统字体渲染，字号 8–64，可加粗/倾斜；"
    "BMP 模式按阈值二值化，反色适用于黑底白字的图片。"
)
PREVIEW_HINT = (
    "预览按“每页字符数 × 字号”分页。左键单击单元格可切换黑白，"
    "编辑结果直接参与 C/BIN 导出；“从源重新生成”会丢弃手动修改。"
)


class _FolderPicker(FilePicker):
    """文件夹选择控件；复用 FilePicker 的输入框与拖放区域。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, placeholder="选择 BMP 文件夹，或把文件夹拖到这里")

    @safe_slot
    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 BMP 文件夹", self.path() or str(Path.home()))
        if selected:
            self.set_path(selected)

    def dragEnterEvent(self, event) -> None:
        try:
            if _single_local_path(event) is not None:
                event.acceptProposedAction()
            else:
                event.ignore()
        except Exception:
            get_logger().exception("处理文件夹拖入失败")
            event.ignore()

    def dropEvent(self, event) -> None:
        try:
            path = _single_local_path(event)
            if path is None:
                event.ignore()
                return
            self.set_path(path)
            event.acceptProposedAction()
        except Exception:
            get_logger().exception("处理文件夹拖放失败")
            event.ignore()


class DotMatrixPage(QWidget):
    """点阵生成工作台；预览、编辑与导出方法可在测试中直接调用。"""

    title = "点阵生成"

    def __init__(self, settings=None, paths=None, parent: QWidget | None = None, **kwargs) -> None:
        super().__init__(parent)
        self.setObjectName("dotmatrix")
        self.settings, self.paths, self.extra_options = settings, paths, dict(kwargs)
        self._pages: list[Matrix] = []
        self._page_index = 0
        self._dirty = False
        self._mode = MODE_TEXT
        self._dot_rgb, self._background_rgb = DEFAULT_DOT_COLOR, DEFAULT_BACKGROUND
        self._bmp_path: Path | None = None
        self._folder_path: Path | None = None
        self._files: list[Path] = []
        self._loading, self._setting_input, self._syncing_zoom = True, False, False
        self._cancel_requested = self._batch_running = False
        self._batch_done = 0
        self._build_ui()
        self._restore_settings()
        self._loading = False
        self.render()

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            " QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        content = QWidget(scroll)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(12)
        self.title_label = TitleLabel("点阵生成", self)
        self.subtitle_label = CaptionLabel(
            "把文字或 BMP 转为可分页编辑的点阵，导出 C 数组、BIN 或批量 C 文件", self
        )
        self.subtitle_label.setWordWrap(True)
        root.addWidget(self.title_label)
        root.addWidget(self.subtitle_label)
        root.addWidget(self._build_input_card())
        root.addWidget(self._build_preview_card(), 1)
        root.addWidget(self._build_params_card())
        self.save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        self.save_shortcut.activated.connect(self._on_save_shortcut)

    def _build_input_card(self) -> QWidget:
        card, layout = section_card("输入", INPUT_HINT)
        self.mode_switch = SegmentedWidget(card)
        for text, key in MODE_ITEMS:
            self.mode_switch.addItem(key, text)
        self.mode_switch.setCurrentItem(MODE_TEXT)
        self.mode_switch.currentItemChanged.connect(self._on_mode_changed)
        layout.addWidget(self.mode_switch)
        self.mode_stack = QStackedWidget(card)
        self.mode_stack.addWidget(self._build_text_panel())
        self.mode_stack.addWidget(self._build_bmp_panel())
        layout.addWidget(self.mode_stack)
        return card

    def _build_text_panel(self) -> QWidget:
        panel = QWidget(self.mode_stack)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(BodyLabel("输入文字（支持多行）", panel))
        self.text_edit = PlainTextEdit(panel)
        self.text_edit.setPlaceholderText("输入要取模的文字，例如：点阵")
        self.text_edit.setPlainText(DEFAULT_TEXT)
        self.text_edit.setMaximumHeight(90)
        layout.addWidget(self.text_edit)
        self.font_combo = ComboBox(panel)
        for family in _font_families():
            self.font_combo.addItem(family)
        for preferred in FALLBACK_FAMILIES[:3]:
            index = self.font_combo.findText(preferred)
            if index >= 0:
                self.font_combo.setCurrentIndex(index)
                break
        self.font_combo.setFixedWidth(210)
        self.font_combo.setToolTip("选择系统字体；字体文件无法定位时回退到 Pillow 内置字体")
        self.size_spin = _spin(panel, 8, 64, 16, tip="字号（像素），同时决定每页字符宽度")
        self.bold_check = CheckBox("粗体", panel)
        self.italic_check = CheckBox("斜体", panel)
        layout.addLayout(_control_row(panel, [("字体", self.font_combo), ("字号", self.size_spin),
                                             ("", self.bold_check), ("", self.italic_check)]))
        return panel

    def _build_bmp_panel(self) -> QWidget:
        panel = QWidget(self.mode_stack)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.bmp_picker = FilePicker(panel, placeholder="选择 BMP/PNG 图片，或把图片拖到这里")
        self.folder_picker = _FolderPicker(panel)
        layout.addLayout(_picker_row(panel, "BMP 文件", self.bmp_picker))
        layout.addLayout(_picker_row(panel, "BMP 文件夹", self.folder_picker))
        self.threshold_spin = _spin(panel, 0, 255, 128, tip="灰度低于阈值的像素取为黑点")
        self.invert_check = CheckBox("反色", panel)
        self.invert_check.setToolTip("黑底白字的图片请勾选反色")
        layout.addLayout(_control_row(panel, [("阈值", self.threshold_spin), ("", self.invert_check)]))
        self.bmp_info_label = CaptionLabel("尚未选择 BMP 图片。", panel)
        self.bmp_info_label.setWordWrap(True)
        layout.addWidget(self.bmp_info_label)
        return panel

    def _build_preview_card(self) -> QWidget:
        card, layout = section_card("点阵预览", PREVIEW_HINT)
        nav = QHBoxLayout()
        nav.setSpacing(8)
        self.prev_button, self.next_button = PushButton("上一页", card), PushButton("下一页", card)
        self.page_label = BodyLabel("第 0 / 0 页", card)
        self.reset_button = PushButton("从源重新生成", card)
        self.reset_button.setToolTip("丢弃手动修改，按当前文字或 BMP 重新生成")
        for widget in (self.prev_button, self.page_label, self.next_button):
            nav.addWidget(widget)
        nav.addSpacing(12)
        nav.addWidget(self.reset_button)
        nav.addStretch(1)
        layout.addLayout(nav)

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(6)
        self.matrix_view = MatrixView(card)
        self.view = self.matrix_view
        self.preview_scroll = QScrollArea(card)
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setFrameShape(QFrame.Shape.StyledPanel)
        self.preview_scroll.setMinimumHeight(250)
        self.preview_scroll.setWidget(self.matrix_view)
        self.preview_info_label = CaptionLabel("尚未生成点阵。", card)
        self.preview_info_label.setWordWrap(True)
        left.addWidget(self.preview_scroll, 1)
        left.addWidget(self.preview_info_label)
        body.addLayout(left, 2)

        right = QVBoxLayout()
        right.setSpacing(6)
        code_header = QHBoxLayout()
        code_header.addWidget(BodyLabel("C 代码预览", card))
        self.copy_button = PushButton("复制", card)
        code_header.addWidget(self.copy_button)
        code_header.addStretch(1)
        right.addLayout(code_header)
        self.code_preview = PlainTextEdit(card)
        self.code_preview.setReadOnly(True)
        self.code_preview.setFont(_mono_font())
        self.code_preview.setMinimumHeight(230)
        self.code_preview.setPlaceholderText("生成点阵后在此显示 C 数组")
        self.code_edit = self.code_preview
        right.addWidget(self.code_preview, 1)
        body.addLayout(right, 3)
        layout.addLayout(body)
        return card

    def _build_params_card(self) -> QWidget:
        card, layout = section_card("取模参数", "参数变化会刷新预览并自动保存。")
        self.scan_combo = _combo(card, SCAN_ITEMS, 140)
        self.direction_combo = _combo(card, DIRECTION_ITEMS, 130)
        self.bit_order_combo = _combo(card, BIT_ORDER_ITEMS, 120)
        self.page_chars_spin = _spin(card, 1, 64, 8, tip="每页按“字符数 × 字号”列宽分页")
        self.zoom_spin = _spin(card, ZOOM_MIN, ZOOM_MAX, ZOOM_DEFAULT, tip="每个点占用的像素数，也可在预览区滚轮缩放")
        layout.addLayout(_control_row(card, [
            ("扫描方式", self.scan_combo), ("取模走向", self.direction_combo), ("位序", self.bit_order_combo),
            ("每页字符数", self.page_chars_spin), ("缩放", self.zoom_spin)]))
        self.dot_color_button = PushButton("#000000", card)
        self.background_button = PushButton("#FFFFFF", card)
        for button in (self.dot_color_button, self.background_button):
            button.setFixedWidth(110)
        self.grid_check = CheckBox("显示网格", card)
        self.grid_check.setChecked(True)
        layout.addLayout(_control_row(card, [("点颜色", self.dot_color_button),
                                             ("背景色", self.background_button), ("", self.grid_check)]))

        output_row = QHBoxLayout()
        output_row.setSpacing(8)
        output_row.addWidget(BodyLabel("输出目录", card))
        self.output_dir_edit = LineEdit(card)
        self.output_dir_edit.setPlaceholderText("留空则保存到 BMP 所在目录")
        self.output_dir_edit.setClearButtonEnabled(True)
        output_row.addWidget(self.output_dir_edit, 1)
        self.output_dir_button = ToolButton(card)
        self.output_dir_button.setIcon(FluentIcon.FOLDER)
        self.output_dir_button.setToolTip("选择输出目录")
        output_row.addWidget(self.output_dir_button)
        output_row.addSpacing(10)
        output_row.addWidget(BodyLabel("输出名称", card))
        self.name_edit = LineEdit(card)
        self.name_edit.setPlaceholderText("留空使用 font_matrix 或 BMP 文件名")
        self.name_edit.setClearButtonEnabled(True)
        self.name_edit.setFixedWidth(200)
        output_row.addWidget(self.name_edit)
        output_row.addStretch(1)
        layout.addLayout(output_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        self.export_c_button = PrimaryPushButton("导出 C", card)
        self.export_bin_button = PushButton("导出 BIN", card)
        self.batch_button = PushButton("批量导出 BMP", card)
        self.batch_button.setToolTip("把当前 BMP 文件或文件夹中的每张图片导出为 C 文件")
        self.cancel_button = PushButton("取消", card)
        self.cancel_button.setEnabled(False)
        self.progress_bar = ProgressBar(card)
        self.progress_bar.setValue(0)
        for widget in (self.export_c_button, self.export_bin_button, self.batch_button, self.cancel_button):
            action_row.addWidget(widget)
        action_row.addWidget(self.progress_bar, 1)
        layout.addLayout(action_row)
        self.status_label = CaptionLabel("输入文字或选择 BMP 后可预览并导出。", card)
        self.status_label.setWordWrap(True)
        self.error_label = CaptionLabel("", card)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {ERROR_COLOR};")
        self.error_label.setVisible(False)
        layout.addWidget(self.status_label)
        layout.addWidget(self.error_label)
        self._render_color_buttons()

        connections = (
            (self.text_edit.textChanged, self._on_text_changed),
            (self.font_combo.currentTextChanged, self._on_font_changed),
            (self.size_spin.valueChanged, self._on_font_changed),
            (self.bold_check.toggled, self._on_font_changed),
            (self.italic_check.toggled, self._on_font_changed),
            (self.bmp_picker.pathChanged, self._on_bmp_path_changed),
            (self.folder_picker.pathChanged, self._on_bmp_folder_changed),
            (self.threshold_spin.valueChanged, self._on_bmp_option_changed),
            (self.invert_check.toggled, self._on_bmp_option_changed),
            (self.scan_combo.currentIndexChanged, self._on_layout_changed),
            (self.direction_combo.currentIndexChanged, self._on_layout_changed),
            (self.bit_order_combo.currentIndexChanged, self._on_layout_changed),
            (self.page_chars_spin.valueChanged, self._on_page_chars_changed),
            (self.zoom_spin.valueChanged, self._on_zoom_changed),
            (self.grid_check.toggled, self._on_grid_toggled),
            (self.dot_color_button.clicked, lambda: self._choose_color("dot")),
            (self.background_button.clicked, lambda: self._choose_color("background")),
            (self.output_dir_edit.textChanged, self._on_setting_text_changed),
            (self.name_edit.textChanged, self._on_setting_text_changed),
            (self.output_dir_button.clicked, self._browse_output_dir),
            (self.prev_button.clicked, self._on_prev_clicked),
            (self.next_button.clicked, self._on_next_clicked),
            (self.reset_button.clicked, self._on_reset_clicked),
            (self.copy_button.clicked, self._on_copy_clicked),
            (self.export_c_button.clicked, self._on_export_c_clicked),
            (self.export_bin_button.clicked, self._on_export_bin_clicked),
            (self.batch_button.clicked, self._on_batch_clicked),
            (self.cancel_button.clicked, self._on_cancel_clicked),
            (self.matrix_view.zoomChanged, self._on_view_zoom_changed),
            (self.matrix_view.matrixChanged, self._on_cell_changed),
        )
        for signal, slot in connections:
            signal.connect(slot)
        return card

    # ---- 参数 ---------------------------------------------------------

    def mode(self) -> str:
        return self._mode

    def scan(self) -> str:
        return str(self.scan_combo.currentData() or "horizontal")

    def direction(self) -> str:
        return str(self.direction_combo.currentData() or "ltr")

    def bit_order(self) -> str:
        return str(self.bit_order_combo.currentData() or "msb")

    def page_chars(self) -> int:
        return int(self.page_chars_spin.value())

    def font_family(self) -> str:
        return self.font_combo.currentText().strip()

    def dot_color(self) -> tuple[int, int, int]:
        return tuple(self._dot_rgb)  # type: ignore[return-value]

    def background_color(self) -> tuple[int, int, int]:
        return tuple(self._background_rgb)  # type: ignore[return-value]

    def current_params(self) -> FontParams:
        return FontParams(
            font_path=self._font_path(), size=int(self.size_spin.value()),
            bold=self.bold_check.isChecked(), italic=self.italic_check.isChecked(),
            threshold=int(self.threshold_spin.value()), invert=self.invert_check.isChecked(),
            scan=self.scan(), direction=self.direction(), bit_order=self.bit_order(),
            page_chars=self.page_chars(),
        )

    def params(self) -> FontParams:
        return self.current_params()

    def _font_path(self) -> str | None:
        family = self.font_family()
        path = resolve_font_file(family)
        if path is None:
            try:
                path = resolve_font_file(QFontInfo(QFont(family)).family())
            except Exception:
                path = None
        return path

    def set_params(self, params: FontParams) -> None:
        self._loading = True
        try:
            self.size_spin.setValue(params.size)
            self.bold_check.setChecked(params.bold)
            self.italic_check.setChecked(params.italic)
            self.threshold_spin.setValue(params.threshold)
            self.invert_check.setChecked(params.invert)
            _set_combo_data(self.scan_combo, params.scan)
            _set_combo_data(self.direction_combo, params.direction)
            _set_combo_data(self.bit_order_combo, params.bit_order)
            self.page_chars_spin.setValue(params.page_chars)
        finally:
            self._loading = False
        self.render()
        self.save_settings()

    def set_text(self, text: str) -> None:
        blocked = self.text_edit.blockSignals(True)
        try:
            self.text_edit.setPlainText(str(text))
        finally:
            self.text_edit.blockSignals(blocked)
        if not self._loading and self._mode == MODE_TEXT:
            self.render()

    def set_font_family(self, family: str) -> bool:
        text = str(family).strip()
        if not text:
            return False
        index = self.font_combo.findText(text)
        if index < 0:
            self.font_combo.addItem(text)
            index = self.font_combo.count() - 1
        self.font_combo.setCurrentIndex(index)
        return True

    def set_size(self, size: int) -> None:
        self.size_spin.setValue(int(size))

    def set_mode(self, mode: str, refresh: bool = True) -> None:
        target = MODE_BMP if str(mode).lower() == MODE_BMP else MODE_TEXT
        blocked = self.mode_switch.blockSignals(True)
        try:
            self.mode_switch.setCurrentItem(target)
        finally:
            self.mode_switch.blockSignals(blocked)
        self._apply_mode(target)
        if refresh and not self._loading:
            self.render()

    def set_dot_color(self, rgb: tuple[int, int, int]) -> None:
        self._dot_rgb = _validate_rgb(rgb)
        self._render_color_buttons()
        self.matrix_view.set_colors(self._dot_rgb, self._background_rgb)
        self.save_settings()

    def set_background_color(self, rgb: tuple[int, int, int]) -> None:
        self._background_rgb = _validate_rgb(rgb)
        self._render_color_buttons()
        self.matrix_view.set_colors(self._dot_rgb, self._background_rgb)
        self.save_settings()

    def set_output_dir(self, directory: str | Path) -> None:
        self.output_dir_edit.setText(str(directory))

    def output_dir(self) -> Path | None:
        text = self.output_dir_edit.text().strip()
        if text:
            return Path(text)
        if self._bmp_path is not None:
            return self._bmp_path.parent
        return self._files[0].parent if self._files else None

    def output_name(self) -> str | None:
        return self.name_edit.text().strip() or None

    def set_output_name(self, name: str) -> None:
        self.name_edit.setText(str(name))

    # ---- 配置 ---------------------------------------------------------

    def save_settings(self) -> None:
        if self.settings is None or self._loading:
            return
        try:
            self.settings.update({
                SETTINGS + "mode": self.mode(), SETTINGS + "text": self.text_edit.toPlainText(),
                SETTINGS + "font_family": self.font_family(), SETTINGS + "size": int(self.size_spin.value()),
                SETTINGS + "bold": self.bold_check.isChecked(), SETTINGS + "italic": self.italic_check.isChecked(),
                SETTINGS + "threshold": int(self.threshold_spin.value()),
                SETTINGS + "invert": self.invert_check.isChecked(), SETTINGS + "scan": self.scan(),
                SETTINGS + "direction": self.direction(), SETTINGS + "bit_order": self.bit_order(),
                SETTINGS + "page_chars": self.page_chars(), SETTINGS + "dot_color": rgb_to_hex(self._dot_rgb),
                SETTINGS + "background": rgb_to_hex(self._background_rgb),
                SETTINGS + "grid": self.grid_check.isChecked(), SETTINGS + "zoom": int(self.zoom_spin.value()),
                SETTINGS + "output_dir": self.output_dir_edit.text().strip(),
                SETTINGS + "output_name": self.name_edit.text().strip(),
            })
            self.settings.save()
        except Exception:
            get_logger().exception("保存点阵参数失败")

    def _restore_settings(self) -> None:
        if self.settings is None:
            return
        get = lambda key, default: self.settings.get(SETTINGS + key, default)  # noqa: E731
        try:
            text = str(get("text", DEFAULT_TEXT) or "")
            if text:
                self.text_edit.setPlainText(text)
            self.set_font_family(str(get("font_family", "") or ""))
            self.size_spin.setValue(max(8, min(64, _safe_int(get("size", 16), 16))))
            self.bold_check.setChecked(bool(get("bold", False)))
            self.italic_check.setChecked(bool(get("italic", False)))
            self.threshold_spin.setValue(max(0, min(255, _safe_int(get("threshold", 128), 128))))
            self.invert_check.setChecked(bool(get("invert", False)))
            for combo, key, default in (
                (self.scan_combo, "scan", "horizontal"), (self.direction_combo, "direction", "ltr"),
                (self.bit_order_combo, "bit_order", "msb")):
                _set_combo_data(combo, str(get(key, default) or default))
            self.page_chars_spin.setValue(max(1, min(64, _safe_int(get("page_chars", 8), 8))))
            self.zoom_spin.setValue(max(ZOOM_MIN, min(ZOOM_MAX, _safe_int(get("zoom", ZOOM_DEFAULT), ZOOM_DEFAULT))))
            self.grid_check.setChecked(bool(get("grid", True)))
            self.output_dir_edit.setText(str(get("output_dir", "") or ""))
            self.name_edit.setText(str(get("output_name", "") or ""))
            self._dot_rgb = _safe_color(get("dot_color", "#000000"), DEFAULT_DOT_COLOR)
            self._background_rgb = _safe_color(get("background", "#FFFFFF"), DEFAULT_BACKGROUND)
        except Exception:
            get_logger().exception("读取点阵参数失败")
            self._dot_rgb, self._background_rgb = DEFAULT_DOT_COLOR, DEFAULT_BACKGROUND
        self._render_color_buttons()
        self.matrix_view.set_colors(self._dot_rgb, self._background_rgb)
        self.matrix_view.set_zoom(self.zoom_spin.value())
        self.matrix_view.set_grid_visible(self.grid_check.isChecked())
        self._apply_mode(str(get("mode", MODE_TEXT) or MODE_TEXT))

    # ---- 生成与分页 ---------------------------------------------------

    def render(self) -> list[Matrix]:
        """按当前输入源重新生成点阵并刷新预览；返回分页列表。"""
        if self._mode == MODE_BMP:
            pages = self._render_bmp()
        else:
            try:
                pages = matrix_from_text(self.text_edit.toPlainText(), self.params())
            except Exception as exc:
                self._fail(str(exc))
                return []
        if pages:
            self._set_error("")
        self._pages, self._dirty = list(pages), False
        self._page_index = min(self._page_index, len(self._pages) - 1) if self._pages else 0
        self._refresh_views()
        if self._pages:
            total = len(matrices_to_bytes(self._pages, self.params()))
            self.status_label.setText(f"已生成 {len(self._pages)} 页，共 {total} 字节。")
        else:
            self.status_label.setText("请选择 BMP 图片或输入文字以生成点阵。")
        return self._pages

    def _render_bmp(self) -> list[Matrix]:
        if self._bmp_path is None:
            return []
        try:
            matrix = matrix_from_image(self._bmp_path, self.params())
        except Exception as exc:
            self._fail(str(exc))
            return []
        return paginate(matrix, self.params().page_chars, self.params().size)

    def _fail(self, message: str) -> None:
        self._pages, self._page_index, self._dirty = [], 0, False
        self._refresh_views()
        self._set_error(message)
        self.status_label.setText("生成失败。")

    def page_count(self) -> int:
        return len(self._pages)

    def pages(self) -> list[Matrix]:
        return list(self._pages)

    def current_page(self) -> Matrix | None:
        return self._pages[self._page_index] if self._pages else None

    def current_page_index(self) -> int:
        return self._page_index

    def set_page(self, index: int) -> None:
        self._page_index = max(0, min(int(index), len(self._pages) - 1)) if self._pages else 0
        self._refresh_views()

    def next_page(self) -> None:
        self.set_page(self._page_index + 1)

    def previous_page(self) -> None:
        self.set_page(self._page_index - 1)

    def reset_from_text(self) -> list[Matrix]:
        """丢弃手动修改，按当前文字或 BMP 重新生成。"""
        return self.render()

    def reset(self) -> list[Matrix]:
        return self.reset_from_text()

    def _refresh_views(self) -> None:
        current = self.current_page()
        self.matrix_view.set_matrix(current)
        self.matrix_view.resize(self.matrix_view.sizeHint())
        self.page_label.setText(f"第 {self._page_index + 1 if self._pages else 0} / {len(self._pages)} 页")
        self.prev_button.setEnabled(self._page_index > 0)
        self.next_button.setEnabled(self._page_index + 1 < len(self._pages))
        self.reset_button.setEnabled(bool(self._pages))
        if current is None:
            self.preview_info_label.setText("尚未生成点阵。")
            self.code_preview.clear()
            return
        total = len(matrices_to_bytes(self._pages, self.params()))
        edited = " · 已手动编辑" if self._dirty else ""
        self.preview_info_label.setText(
            f"第 {self._page_index + 1}/{len(self._pages)} 页 · {current.cols} × {current.rows} 点 · "
            f"每点 {self.matrix_view.zoom()} 像素 · 共 {total} 字节{edited}"
        )
        self.code_preview.setPlainText(self._code_preview_text())

    def _code_preview_text(self) -> str:
        if not self._pages:
            return ""
        try:
            text = matrix_to_c(self._pages, self.params(), self._array_name())
        except Exception as exc:
            self._set_error(str(exc))
            return ""
        lines = text.splitlines()
        shown = "\n".join(lines[:MAX_CODE_LINES])
        if len(lines) > MAX_CODE_LINES:
            shown += f"\n/* … 已截断，共 {len(lines)} 行；导出后查看完整文件 */"
        return shown

    def _array_name(self, name: str | None = None) -> str:
        if name:
            return str(name)
        if self.output_name():
            return str(self.output_name())
        return self._bmp_path.stem if self._bmp_path is not None else "font_matrix"

    # ---- 事件 ---------------------------------------------------------

    @safe_slot
    def _on_mode_changed(self, *_args) -> None:
        if not self._loading:
            self._apply_mode(str(self.mode_switch.currentRouteKey()))
            self.save_settings()
            self.render()

    def _apply_mode(self, mode: str) -> None:
        self._mode = MODE_BMP if str(mode).lower() == MODE_BMP else MODE_TEXT
        self.mode_stack.setCurrentIndex(1 if self._mode == MODE_BMP else 0)
        if self.mode_switch.currentRouteKey() != self._mode:
            blocked = self.mode_switch.blockSignals(True)
            try:
                self.mode_switch.setCurrentItem(self._mode)
            finally:
                self.mode_switch.blockSignals(blocked)

    @safe_slot
    def _on_text_changed(self, *_args) -> None:
        if not self._loading and self._mode == MODE_TEXT:
            self.render()

    @safe_slot
    def _on_font_changed(self, *_args) -> None:
        if not self._loading:
            self.save_settings()
            if self._mode == MODE_TEXT:
                self.render()

    @safe_slot
    def _on_bmp_option_changed(self, *_args) -> None:
        if not self._loading:
            self.save_settings()
            if self._mode == MODE_BMP:
                self.render()

    @safe_slot
    def _on_layout_changed(self, *_args) -> None:
        if not self._loading:
            self.save_settings()
            self._refresh_views()

    @safe_slot
    def _on_page_chars_changed(self, *_args) -> None:
        if not self._loading:
            self.save_settings()
            self.render()

    @safe_slot
    def _on_zoom_changed(self, value: int) -> None:
        if self._loading or self._syncing_zoom:
            return
        self.matrix_view.set_zoom(int(value))
        self.matrix_view.resize(self.matrix_view.sizeHint())
        self._refresh_views()
        self.save_settings()

    @safe_slot
    def _on_view_zoom_changed(self, value: int) -> None:
        self._syncing_zoom = True
        try:
            self.zoom_spin.setValue(int(value))
        finally:
            self._syncing_zoom = False
        self.save_settings()

    @safe_slot
    def _on_grid_toggled(self, checked: bool) -> None:
        self.matrix_view.set_grid_visible(bool(checked))
        if not self._loading:
            self.save_settings()

    @safe_slot
    def _on_setting_text_changed(self, _text: str = "") -> None:
        if not self._loading:
            self.save_settings()

    @safe_slot
    def _on_cell_changed(self, row: int, col: int) -> None:
        self._dirty = True
        self._refresh_views()
        self.status_label.setText(
            f"已编辑第 {self._page_index + 1} 页第 {row + 1} 行、第 {col + 1} 列；导出使用当前点阵。"
        )

    @safe_slot
    def _on_prev_clicked(self, *_args) -> None:
        self.previous_page()

    @safe_slot
    def _on_next_clicked(self, *_args) -> None:
        self.next_page()

    @safe_slot
    def _on_reset_clicked(self, *_args) -> None:
        self.reset_from_text()

    @safe_slot
    def _on_copy_clicked(self, *_args) -> None:
        try:
            self.copy_code()
        except Exception as exc:
            show_error(self, "复制失败", str(exc))
            return
        show_success(self, "已复制", "C 数组已复制到剪贴板。")

    def copy_code(self) -> str:
        if not self._pages:
            raise ValueError("当前没有可复制的点阵，请先生成。")
        text = matrix_to_c(self._pages, self.params(), self._array_name())
        QApplication.clipboard().setText(text)
        return text

    def _choose_color(self, which: str) -> None:
        current = self._dot_rgb if which == "dot" else self._background_rgb
        color = QColorDialog.getColor(QColor(*current), self, "选择点阵颜色")
        if color.isValid():
            rgb = (color.red(), color.green(), color.blue())
            self.set_dot_color(rgb) if which == "dot" else self.set_background_color(rgb)

    def _render_color_buttons(self) -> None:
        for button, rgb in ((self.dot_color_button, self._dot_rgb),
                            (self.background_button, self._background_rgb)):
            hex_text = rgb_to_hex(rgb)
            button.setText(hex_text)
            button.setStyleSheet(
                f"background-color: {hex_text}; color: "
                f"{'#000000' if sum(rgb) > 382 else '#FFFFFF'}; border: 1px solid #8A8A8A;"
            )

    def _browse_output_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择点阵输出目录", str(self.output_dir() or ""))
        if selected:
            self.output_dir_edit.setText(selected)

    # ---- BMP 载入 -----------------------------------------------------

    def load_bmp(self, path: str | Path) -> Matrix:
        """载入单张 BMP/图片并切换到 BMP 模式；失败时写入内联错误并抛出。"""
        source = Path(path)
        try:
            matrix = matrix_from_image(source, self.params())
        except Exception as exc:
            self._fail(str(exc))
            raise
        self._bmp_path, self._files = source, [source]
        self._folder_path = None
        self._set_input_paths(file_path=source, folder_path=None)
        self.set_mode(MODE_BMP, refresh=False)
        self._pages = paginate(matrix, self.params().page_chars, self.params().size)
        self._page_index, self._dirty = 0, False
        self.bmp_info_label.setText(f"BMP：{source.name} · {matrix.cols}×{matrix.rows} 点。")
        self.status_label.setText(f"已载入 {source.name}。")
        self._set_error("")
        self._refresh_views()
        self.save_settings()
        return matrix

    def load_bmp_folder(self, folder: str | Path) -> list[Path]:
        """载入 BMP 文件夹并预览第一张；返回可批量导出的文件列表。"""
        source = Path(folder)
        try:
            files = list_image_files(source)
        except Exception as exc:
            self._fail(str(exc))
            raise
        if not files:
            error = ValueError(f"文件夹中没有可转换的图片：{source}。请确认其中包含 BMP/PNG 等图片。")
            self._fail(str(error))
            raise error
        matrix = preview = None
        last_error: Exception | None = None
        for candidate in files:
            try:
                matrix = matrix_from_image(candidate, self.params())
            except Exception as exc:
                last_error = exc
                continue
            preview = candidate
            break
        if matrix is None or preview is None:
            error = ValueError(f"文件夹中的图片都无法读取：{source}"
                               + (f"（{last_error}）" if last_error else ""))
            self._fail(str(error))
            raise error
        self._bmp_path, self._files = preview, files
        self._folder_path = source
        self._set_input_paths(file_path=preview, folder_path=source)
        self.set_mode(MODE_BMP, refresh=False)
        self._pages = paginate(matrix, self.params().page_chars, self.params().size)
        self._page_index, self._dirty = 0, False
        self.bmp_info_label.setText(
            f"文件夹：{source} · 共 {len(files)} 张，预览 {preview.name}（{matrix.cols}×{matrix.rows} 点）。"
        )
        self.status_label.setText(f"已载入 {len(files)} 张图片，可批量导出 C 文件。")
        self._set_error("")
        self._refresh_views()
        self.save_settings()
        return files

    def _set_input_paths(self, file_path: Path | None, folder_path: Path | None) -> None:
        self._setting_input = True
        try:
            self.bmp_picker.set_path(file_path or "")
            self.folder_picker.set_path(folder_path or "")
        finally:
            self._setting_input = False

    @safe_slot
    def _on_bmp_path_changed(self, _path: str = "") -> None:
        if not self._setting_input and not self._loading and self.bmp_picker.path():
            try:
                self.load_bmp(self.bmp_picker.path())
            except Exception:
                pass  # load_bmp 已写入可读错误。

    @safe_slot
    def _on_bmp_folder_changed(self, _path: str = "") -> None:
        if not self._setting_input and not self._loading and self.folder_picker.path():
            try:
                self.load_bmp_folder(self.folder_picker.path())
            except Exception:
                pass

    # ---- 导出 ---------------------------------------------------------

    def export_c(self, target: str | Path | None = None, output_dir: str | Path | None = None,
                 name: str | None = None) -> Path:
        return self._export("c", target, output_dir, name)

    def export_bin(self, target: str | Path | None = None, output_dir: str | Path | None = None,
                   name: str | None = None) -> Path:
        return self._export("bin", target, output_dir, name)

    def export_current(self, kind: str = "c", target: str | Path | None = None,
                       output_dir: str | Path | None = None, name: str | None = None) -> Path:
        return self._export(kind, target, output_dir, name)

    def _export(self, kind: str, target, output_dir, name) -> Path:
        if not self._pages:
            error = ValueError("当前没有可导出的点阵，请先生成或载入图片。")
            self._set_error(str(error))
            raise error
        params = self.params()
        array_name = name or self.output_name()
        if not array_name and target is not None:
            array_name = Path(target).stem
        array_name = array_name or self._array_name()
        if kind == "bin":
            payload, suffix = matrices_to_bytes(self._pages, params), ".bin"
        else:
            payload, suffix = matrix_to_c(self._pages, params, array_name), ".c"
        if target is not None:
            path = Path(target)
            if not path.suffix:
                path = path.with_suffix(suffix)
        else:
            directory = Path(output_dir) if output_dir is not None else self.output_dir()
            if directory is None:
                error = ValueError("请先选择输出目录，或在“输出目录”中填写保存位置。")
                self._set_error(str(error))
                raise error
            stem = sanitize_name(
                str(name or self.output_name() or (self._bmp_path.stem if self._bmp_path else "font_matrix")),
                fallback="font_matrix",
            )
            path = unique_path(Path(directory) / f"{stem}{suffix}")
        try:
            if kind == "bin":
                write_bytes_atomic(path, payload)
            else:
                write_text_atomic(path, payload)
        except OSError as exc:
            message = f"写入文件失败：{path}（{exc}）。请检查磁盘空间、目录权限或文件是否被占用。"
            self._set_error(message)
            raise OSError(message) from exc
        self.progress_bar.setValue(100)
        self.status_label.setText(f"已导出 {path.name} 到 {path.parent}。")
        self._set_error("")
        self.save_settings()
        return path

    def batch_files(self) -> list[Path]:
        folder = self.folder_picker.path().strip()
        if folder and Path(folder).is_dir():
            if self._folder_path is not None and self._folder_path == Path(folder):
                return list(self._files)
            try:
                return list_image_files(Path(folder))
            except ValueError:
                return []
        if self._bmp_path is not None:
            return [self._bmp_path]
        picker = self.bmp_picker.path().strip()
        return [Path(picker)] if picker else list(self._files)

    def batch_export(self, paths: list[str | Path] | None = None,
                     output_dir: str | Path | None = None) -> BatchResult:
        """把 BMP 文件夹（或单张图片）中每张图片导出为独立 C 文件。"""
        file_list = [Path(item) for item in paths] if paths is not None else self.batch_files()
        if not file_list:
            error = ValueError("请先选择 BMP 文件或文件夹。")
            self._set_error(str(error))
            raise error
        directory = Path(output_dir) if output_dir is not None else (self.output_dir() or file_list[0].parent)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            message = f"无法创建输出目录：{directory}（{exc}）。请检查目录权限。"
            self._set_error(message)
            raise OSError(message) from exc
        params, result, total = self.params(), BatchResult(), len(file_list)
        self._cancel_requested = False
        self._batch_running, self._batch_done = True, 0
        self.cancel_button.setEnabled(True)
        self.export_c_button.setEnabled(False)
        self.batch_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.status_label.setText(f"正在批量导出 {total} 张图片…")
        self._set_error("")
        try:
            for index, source in enumerate(file_list, start=1):
                if self._cancel_requested:
                    result.canceled = True
                    break
                try:
                    matrix = matrix_from_image(source, params)
                    pages = paginate(matrix, params.page_chars, params.size)
                    target = unique_path(
                        directory / f"{sanitize_name(source.stem, fallback='font_matrix')}.c"
                    )
                    write_text_atomic(target, matrix_to_c(pages, params, source.stem))
                    result[str(source)] = [target]
                except Exception as exc:
                    message = str(exc).strip() or type(exc).__name__
                    result[str(source)] = []
                    result.errors[str(source)] = message
                    get_logger().warning("批量点阵导出失败：%s（%s）", source, message)
                self._on_batch_progress(index, total)
        finally:
            self._batch_running = False
            self.cancel_button.setEnabled(False)
            self.export_c_button.setEnabled(True)
            self.batch_button.setEnabled(True)
        written = sum(len(outputs) for outputs in result.values())
        if result.canceled:
            self.status_label.setText(f"已取消：已处理 {self._batch_done} / {total} 张，写出 {written} 个文件。")
        else:
            self.progress_bar.setValue(100)
            self.status_label.setText(f"批量完成：写出 {written} 个 C 文件，失败 {len(result.errors)} 个。")
        if result.errors:
            self._set_error(_batch_error_summary(result))
        self.save_settings()
        return result

    def _on_batch_progress(self, done: int, total: int) -> None:
        self._batch_done = done
        self.progress_bar.setValue(int(done * 100 / total) if total else 0)
        self.status_label.setText(f"正在批量导出 {done} / {total} 张…")
        QApplication.processEvents()

    def cancel_batch(self) -> None:
        if self._batch_running:
            self._cancel_requested = True
            self.status_label.setText("正在取消批量导出…")

    def _on_cancel_clicked(self, *_args) -> None:
        self.cancel_batch()

    def _default_export_path(self, suffix: str) -> str:
        stem = sanitize_name(
            str(self.output_name() or (self._bmp_path.stem if self._bmp_path else "font_matrix")),
            fallback="font_matrix",
        )
        return str(Path(self.output_dir() or Path.home()) / f"{stem}{suffix}")

    def _save_via_dialog(self, kind: str) -> None:
        suffix, title = (".bin", "导出 BIN") if kind == "bin" else (".c", "导出 C 数组")
        path, _selected = QFileDialog.getSaveFileName(
            self, title, self._default_export_path(suffix), f"{title} (*{suffix});;所有文件 (*.*)"
        )
        if not path:
            return
        try:
            written = self.export_bin(target=path) if kind == "bin" else self.export_c(target=path)
        except Exception as exc:
            show_error(self, "导出失败", str(exc))
            return
        show_success(self, "导出完成", f"已保存：{written}")

    @safe_slot
    def _on_export_c_clicked(self, *_args) -> None:
        self._save_via_dialog("c")

    @safe_slot
    def _on_export_bin_clicked(self, *_args) -> None:
        self._save_via_dialog("bin")

    @safe_slot
    def _on_batch_clicked(self, *_args) -> None:
        try:
            result = self.batch_export()
        except Exception as exc:
            show_error(self, "批量导出失败", str(exc))
            return
        if result.errors:
            show_error(self, "部分图片导出失败", _batch_error_summary(result))
            return
        show_success(self, "批量导出完成", f"已写出 {sum(len(v) for v in result.values())} 个文件。")

    @safe_slot
    def _on_save_shortcut(self, *_args) -> None:
        try:
            written = self.export_c()
        except Exception as exc:
            show_error(self, "导出失败", str(exc))
            return
        show_success(self, "导出完成", f"已保存：{written}")

    # ---- 生命周期 -----------------------------------------------------

    def has_active_work(self) -> bool:
        """点阵页不保持连接或线程；批量导出是同步的。"""
        return False

    def stop_workers(self) -> None:
        """请求取消正在进行的批量导出；没有后台线程需要 join。"""
        self._cancel_requested = True

    def closeEvent(self, event) -> None:
        self._cancel_requested = True
        self.save_settings()
        super().closeEvent(event)

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))


# ---- 小工具 -----------------------------------------------------------


def _font_families() -> list[str]:
    try:
        families = [str(item) for item in QFontDatabase.families()]
    except Exception:
        get_logger().exception("读取系统字体列表失败")
        families = []
    for family in FALLBACK_FAMILIES:
        if family not in families:
            families.append(family)
    return families


def _spin(parent: QWidget, low: int, high: int, value: int, tip: str = "") -> SpinBox:
    spin = SpinBox(parent)
    spin.setRange(low, high)
    spin.setValue(value)
    # qfluentwidgets 的 SpinBox 最小宽度约 133；更窄时数字输入框会被压缩到 0 宽。
    spin.setFixedWidth(130)
    if tip:
        spin.setToolTip(tip)
    return spin


def _picker_row(parent: QWidget, title: str, picker: FilePicker) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(10)
    label = BodyLabel(title, parent)
    label.setMinimumWidth(70)
    row.addWidget(label)
    row.addWidget(picker, 1)
    return row


def _control_row(parent: QWidget, items: list[tuple[str, QWidget]]) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(8)
    for title, widget in items:
        if title:
            row.addWidget(BodyLabel(title, parent))
        row.addWidget(widget)
        row.addSpacing(8)
    row.addStretch(1)
    return row


def _combo(parent: QWidget, items, width: int) -> ComboBox:
    combo = ComboBox(parent)
    for label, value in items:
        combo.addItem(label, userData=value)
    combo.setFixedWidth(width)
    return combo


def _set_combo_data(combo: ComboBox, value) -> bool:
    index = combo.findData(value)
    if index < 0 and isinstance(value, str):
        index = combo.findData(value.lower())
    if index < 0:
        return False
    blocked = combo.blockSignals(True)
    try:
        combo.setCurrentIndex(index)
    finally:
        combo.blockSignals(blocked)
    return True


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _safe_color(value, default: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        return hex_to_rgb(str(value))
    except (ValueError, TypeError):
        return default


def _validate_rgb(rgb) -> tuple[int, int, int]:
    components = tuple(int(value) for value in rgb)
    if len(components) != 3 or any(not 0 <= value <= 255 for value in components):
        raise ValueError("颜色必须是三个 0–255 的整数。")
    return components  # type: ignore[return-value]


def _single_local_path(event) -> str | None:
    mime = event.mimeData()
    if not mime.hasUrls():
        return None
    urls = mime.urls()
    if len(urls) != 1 or not urls[0].isLocalFile():
        return None
    local_path = urls[0].toLocalFile().strip()
    return str(Path(local_path)) if local_path else None


def _batch_error_summary(result: BatchResult) -> str:
    items = list(result.errors.items())
    lines = [f"{Path(path).name}：{message}" for path, message in items[:5]]
    if len(items) > 5:
        lines.append(f"另有 {len(items) - 5} 个文件失败。")
    return "\n".join(lines)


def _mono_font() -> QFont:
    font = QFont("Cascadia Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setFamilies(["Cascadia Mono", "Consolas", "Courier New"])
    return font

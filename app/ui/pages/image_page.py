"""图片取模页：单张/文件夹导入、实时预览、参数保存与批量导出。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QLabel,
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
    SpinBox,
    TitleLabel,
    ToolButton,
)

from app.core.color import hex_to_rgb, rgb_to_hex
from app.core.imaging import (
    MAX_DIMENSION,
    BatchResult,
    ConversionResult,
    ImageParams,
    batch_convert,
    convert_file,
    convert_image,
    list_image_files,
    load_image,
)
from app.core.logging_setup import get_logger
from app.ui.feedback import show_error, show_success
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.file_picker import FilePicker

ERROR_COLOR = "#C42B1C"
SETTINGS = "image."
PREVIEW_SIZE = (250, 170)
MAX_CODE_LINES = 90
MAX_BIN_PREVIEW = 256
DEFAULT_BACKGROUND = (0, 0, 0)
OUTPUT_TYPE_ITEMS = (("C 数组", "c"), ("BIN 原始数据", "bin"), ("LVGL 8", "lvgl8"), ("LVGL 9", "lvgl9"))
SCAN_ITEMS = (("水平（逐行）", "horizontal"), ("垂直（逐列）", "vertical"))
DIRECTION_ITEMS = (
    ("自左到右", "ltr"),
    ("自右到左", "rtl"),
    ("自上到下", "ttb"),
    ("自下到上", "btt"),
)
DEPTH_ITEMS = (("1 位", 1), ("8 位（RGB332）", 8), ("16 位（RGB565）", 16),
               ("24 位（RGB888）", 24), ("32 位（RGBA8888）", 32))
BYTE_ORDER_ITEMS = (("小端（Little Endian）", "little"), ("大端（Big Endian）", "big"))
IMAGE_HINT = (
    "支持 Pillow 可读取的常见位图。透明像素按背景色合成；"
    "输出宽高填 0 表示保持原尺寸，只填一个时按比例缩放。"
)
PREVIEW_HINT = (
    "左侧为原图，中间为按当前参数生成的效果预览，右侧为导出代码的前几行。"
    "16 位预览会展示 RGB565 量化后的颜色。"
)


class _FolderPicker(FilePicker):
    """文件夹选择控件；复用 FilePicker 的输入框与拖放区域。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, placeholder="选择图片文件夹，或把文件夹拖到这里")

    @safe_slot
    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择图片文件夹", self.path() or str(Path.home())
        )
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


class ImagePage(QWidget):
    """图片取模工作台；预览与导出逻辑可在测试中直接调用。"""

    title = "图片取模"

    def __init__(self, settings=None, paths=None, parent: QWidget | None = None, **kwargs) -> None:
        super().__init__(parent)
        self.setObjectName("image")
        self.settings = settings
        self.paths = paths
        self.extra_options = dict(kwargs)
        self.image: Image.Image | None = None
        self.source: Path | None = None
        self.source_file: Path | None = None
        self.files: list[Path] = []
        self.last_result: ConversionResult | None = None
        self.generated_text = ""
        self._background = DEFAULT_BACKGROUND
        self._loading = True
        self._setting_input = False
        self._syncing_size = False
        self._cancel_requested = False
        self._batch_running = False
        self._batch_done = 0
        self._build_ui()
        self._restore_settings()
        self._loading = False
        self._refresh_control_state()

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
        self.title_label = TitleLabel("图片取模", self)
        self.subtitle_label = CaptionLabel(
            "把位图转换为 C 数组、BIN 或 LVGL 图像描述，支持单张与文件夹批量导出", self
        )
        self.subtitle_label.setWordWrap(True)
        root.addWidget(self.title_label)
        root.addWidget(self.subtitle_label)

        input_card, input_layout = section_card("输入图片", IMAGE_HINT)
        self.input_picker = FilePicker(input_card, placeholder="选择图片文件，或把图片拖到这里")
        self.input_picker.pathChanged.connect(self._on_file_path_changed)
        self.folder_picker = _FolderPicker(input_card)
        self.folder_picker.pathChanged.connect(self._on_folder_path_changed)
        self.file_picker = self.input_picker
        input_layout.addLayout(_picker_row(input_card, "图片文件", self.input_picker))
        input_layout.addLayout(_picker_row(input_card, "图片文件夹", self.folder_picker))
        self.input_info_label = CaptionLabel("尚未选择图片。", input_card)
        self.input_info_label.setWordWrap(True)
        input_layout.addWidget(self.input_info_label)
        root.addWidget(input_card)

        preview_card, preview_layout = section_card("预览与代码", PREVIEW_HINT)
        preview_row = QHBoxLayout()
        preview_row.setSpacing(12)
        self.original_preview_label = _preview_label(preview_card, "选择图片后在此预览")
        self.output_preview_label = _preview_label(preview_card, "生成后在此预览输出效果")
        self.original_info_label = CaptionLabel("原图：未载入", preview_card)
        self.output_info_label = CaptionLabel("输出：未生成", preview_card)
        self.original_preview = self.original_preview_label
        self.output_preview = self.output_preview_label
        self.preview_label = self.output_preview_label
        preview_row.addLayout(
            _preview_column(preview_card, "原图", self.original_preview_label, self.original_info_label), 1
        )
        preview_row.addLayout(
            _preview_column(preview_card, "输出", self.output_preview_label, self.output_info_label), 1
        )
        code_column = QVBoxLayout()
        code_column.setSpacing(6)
        code_column.addWidget(BodyLabel("生成代码", preview_card))
        self.code_preview = PlainTextEdit(preview_card)
        self.code_preview.setReadOnly(True)
        self.code_preview.setFont(_mono_font())
        self.code_preview.setMinimumHeight(150)
        self.code_preview.setPlaceholderText("转换后在此显示 C 或 LVGL 代码的前几行")
        self.code_edit = self.code_preview
        code_column.addWidget(self.code_preview, 1)
        preview_row.addLayout(code_column, 1)
        preview_layout.addLayout(preview_row)
        root.addWidget(preview_card, 1)

        params_card, params_layout = section_card("转换参数", "参数变化会实时刷新预览，并自动保存。")
        self.output_type_combo = _combo(params_card, OUTPUT_TYPE_ITEMS, 140)
        self.scan_combo = _combo(params_card, SCAN_ITEMS, 140)
        self.direction_combo = _combo(params_card, DIRECTION_ITEMS, 130)
        self.depth_combo = _combo(params_card, DEPTH_ITEMS, 160)
        self.byte_order_combo = _combo(
            params_card, BYTE_ORDER_ITEMS, 180, "只影响 16 位 RGB565 的字节排列"
        )
        self.width_spin = _dimension_spin(params_card)
        self.height_spin = _dimension_spin(params_card)
        self.lock_ratio_check = CheckBox("锁定宽高比", params_card)
        self.lock_ratio_check.setChecked(True)
        self.background_button = PushButton("#000000", params_card)
        self.background_button.setFixedWidth(110)
        self.background_button.setToolTip("透明像素合成到该背景色")
        self.invert_check = CheckBox("反色（1 位）", params_card)
        self.threshold_spin = SpinBox(params_card)
        self.threshold_spin.setRange(0, 255)
        self.threshold_spin.setValue(128)
        self.threshold_spin.setFixedWidth(110)
        self.threshold_spin.setToolTip("1 位输出：亮度 >= 阈值记为白，低于阈值记为黑")
        self.threshold_spin.setEnabled(False)
        self.threshold_spin.valueChanged.connect(self._on_parameter_changed)
        params_layout.addLayout(
            _control_row(
                params_card,
                [
                    ("输出类型", self.output_type_combo),
                    ("扫描方式", self.scan_combo),
                    ("扫描方向", self.direction_combo),
                    ("颜色深度", self.depth_combo),
                ],
            )
        )
        params_layout.addLayout(
            _control_row(
                params_card,
                [
                    ("字节序", self.byte_order_combo),
                    ("输出宽", self.width_spin),
                    ("输出高", self.height_spin),
                    ("", self.lock_ratio_check),
                    ("背景色", self.background_button),
                    ("1 位阈值", self.threshold_spin),
                    ("", self.invert_check),
                ],
            )
        )

        output_row = QHBoxLayout()
        output_row.setSpacing(8)
        output_row.addWidget(BodyLabel("输出目录", params_card))
        self.output_dir_edit = LineEdit(params_card)
        self.output_dir_edit.setPlaceholderText("留空则保存到图片所在目录")
        self.output_dir_edit.setClearButtonEnabled(True)
        output_row.addWidget(self.output_dir_edit, 1)
        self.output_dir_button = ToolButton(params_card)
        self.output_dir_button.setIcon(FluentIcon.FOLDER)
        self.output_dir_button.setToolTip("选择输出目录")
        output_row.addWidget(self.output_dir_button)
        output_row.addSpacing(10)
        output_row.addWidget(BodyLabel("输出名称", params_card))
        self.name_edit = LineEdit(params_card)
        self.name_edit.setPlaceholderText("留空使用原文件名")
        self.name_edit.setClearButtonEnabled(True)
        self.name_edit.setFixedWidth(180)
        output_row.addWidget(self.name_edit)
        output_row.addStretch(1)
        params_layout.addLayout(output_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        self.convert_button = PrimaryPushButton("转换并保存", params_card)
        self.batch_button = PushButton("批量转换", params_card)
        self.batch_button.setToolTip("把当前文件夹（或单张图片）批量导出到输出目录")
        self.cancel_button = PushButton("取消", params_card)
        self.cancel_button.setEnabled(False)
        self.progress_bar = ProgressBar(params_card)
        self.progress_bar.setValue(0)
        for widget in (self.convert_button, self.batch_button, self.cancel_button):
            action_row.addWidget(widget)
        action_row.addWidget(self.progress_bar, 1)
        params_layout.addLayout(action_row)
        self.status_label = CaptionLabel("选择图片后可预览并导出。", params_card)
        self.status_label.setWordWrap(True)
        self.error_label = CaptionLabel("", params_card)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {ERROR_COLOR};")
        self.error_label.setVisible(False)
        params_layout.addWidget(self.status_label)
        params_layout.addWidget(self.error_label)
        root.addWidget(params_card)

        self.output_type_combo.currentIndexChanged.connect(self._on_output_type_changed)
        self.scan_combo.currentIndexChanged.connect(self._on_scan_changed)
        self.direction_combo.currentIndexChanged.connect(self._on_direction_changed)
        self.depth_combo.currentIndexChanged.connect(self._on_depth_changed)
        self.byte_order_combo.currentIndexChanged.connect(self._on_parameter_changed)
        self.width_spin.valueChanged.connect(self._on_width_changed)
        self.height_spin.valueChanged.connect(self._on_height_changed)
        self.lock_ratio_check.toggled.connect(self._on_parameter_changed)
        self.invert_check.toggled.connect(self._on_parameter_changed)
        self.background_button.clicked.connect(self._choose_background)
        self.output_dir_edit.textChanged.connect(self._on_setting_text_changed)
        self.name_edit.textChanged.connect(self._on_setting_text_changed)
        self.output_dir_button.clicked.connect(self._browse_output_dir)
        self.convert_button.clicked.connect(self._on_convert_clicked)
        self.batch_button.clicked.connect(self._on_batch_clicked)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self._update_background_button()

    # ---- 设置存取 -----------------------------------------------------

    def _restore_settings(self) -> None:
        if self.settings is None:
            return
        get = lambda key, default: self.settings.get(SETTINGS + key, default)  # noqa: E731
        try:
            _set_combo_data(self.output_type_combo, str(get("output_type", "c") or "c"))
            _set_combo_data(self.scan_combo, str(get("scan", "horizontal") or "horizontal"))
            _set_combo_data(self.depth_combo, int(get("color_depth", 16) or 16))
            _set_combo_data(self.byte_order_combo, str(get("byte_order", "little") or "little"))
            _set_combo_data(self.direction_combo, str(get("direction", "ltr") or "ltr"))
            self.width_spin.setValue(_safe_int(get("width", 0)))
            self.height_spin.setValue(_safe_int(get("height", 0)))
            self.lock_ratio_check.setChecked(bool(get("lock_ratio", True)))
            self.invert_check.setChecked(bool(get("invert", False)))
            self.threshold_spin.setValue(_safe_int(get("threshold", 128)))
            try:
                self._background = hex_to_rgb(str(get("background", "#000000") or "#000000"))
            except ValueError:
                self._background = DEFAULT_BACKGROUND
            self.output_dir_edit.setText(str(get("output_dir", "") or ""))
            self.name_edit.setText(str(get("output_name", "") or ""))
        except Exception:
            get_logger().exception("读取图片取模参数失败")
            self._background = DEFAULT_BACKGROUND
        self._update_background_button()

    def save_settings(self) -> None:
        """把当前参数写入配置；配置不可写时只记录日志。"""
        if self.settings is None or self._loading:
            return
        try:
            self.settings.update(
                {
                    SETTINGS + "output_type": self.output_type(),
                    SETTINGS + "scan": self.scan(),
                    SETTINGS + "direction": self.direction(),
                    SETTINGS + "color_depth": self.depth(),
                    SETTINGS + "byte_order": self.byte_order(),
                    SETTINGS + "width": int(self.width_spin.value()),
                    SETTINGS + "height": int(self.height_spin.value()),
                    SETTINGS + "lock_ratio": bool(self.lock_ratio_check.isChecked()),
                    SETTINGS + "background": rgb_to_hex(self._background),
                    SETTINGS + "invert": bool(self.invert_check.isChecked()),
                    SETTINGS + "threshold": int(self.threshold_spin.value()),
                    SETTINGS + "output_dir": self.output_dir_edit.text().strip(),
                    SETTINGS + "output_name": self.name_edit.text().strip(),
                }
            )
            self.settings.save()
        except Exception:
            get_logger().exception("保存图片取模参数失败")

    # ---- 参数读取与同步 -----------------------------------------------

    def output_type(self) -> str:
        return str(self.output_type_combo.currentData() or "c")

    def scan(self) -> str:
        return str(self.scan_combo.currentData() or "horizontal")

    def direction(self) -> str:
        return str(self.direction_combo.currentData() or "ltr")

    def depth(self) -> int:
        try:
            return int(self.depth_combo.currentData())
        except (TypeError, ValueError):
            return 16

    def byte_order(self) -> str:
        return str(self.byte_order_combo.currentData() or "little")

    def background(self) -> tuple[int, int, int]:
        return tuple(self._background)  # type: ignore[return-value]

    def background_color(self) -> tuple[int, int, int]:
        return self.background()

    def is_lvgl(self) -> bool:
        return self.output_type() in ("lvgl8", "lvgl9")

    def current_params(self) -> ImageParams:
        return ImageParams(
            output_type=self.output_type(),
            scan=self.scan(),
            direction=self.direction(),
            color_depth=self.depth(),
            byte_order=self.byte_order(),
            width=self.width_spin.value() or None,
            height=self.height_spin.value() or None,
            background=self._background,
            invert=bool(self.invert_check.isChecked()),
            threshold=int(self.threshold_spin.value()),
        )

    def params(self) -> ImageParams:
        return self.current_params()

    def set_params(self, params: ImageParams) -> None:
        """设置全部转换参数（测试与恢复配置共用）。"""
        self._loading = True
        try:
            _set_combo_data(self.output_type_combo, params.output_type)
            _set_combo_data(self.depth_combo, params.color_depth)
            _set_combo_data(self.byte_order_combo, params.byte_order)
            _set_combo_data(self.scan_combo, params.scan)
            _set_combo_data(self.direction_combo, params.direction)
            self.width_spin.setValue(params.width or 0)
            self.height_spin.setValue(params.height or 0)
            self._background = tuple(params.background)
            self.invert_check.setChecked(bool(params.invert))
            self.threshold_spin.setValue(int(params.threshold))
        finally:
            self._loading = False
        self._update_background_button()
        self._refresh_control_state()
        self._on_parameter_changed()

    def set_background(self, rgb: tuple[int, int, int]) -> None:
        components = tuple(int(value) for value in rgb)
        if len(components) != 3 or any(not 0 <= value <= 255 for value in components):
            raise ValueError("背景色必须是三个 0–255 的整数。")
        self._background = components  # type: ignore[assignment]
        self._update_background_button()
        self._on_parameter_changed()

    def set_background_color(self, rgb: tuple[int, int, int]) -> None:
        self.set_background(rgb)

    def _update_background_button(self) -> None:
        hex_text = rgb_to_hex(self._background)
        text_color = "#000000" if sum(self._background) > 382 else "#FFFFFF"
        self.background_button.setText(hex_text)
        self.background_button.setStyleSheet(
            f"background-color: {hex_text}; color: {text_color}; border: 1px solid #8A8A8A;"
        )

    def _refresh_control_state(self) -> None:
        self.byte_order_combo.setEnabled(self.depth() == 16)
        self.threshold_spin.setEnabled(self.depth() == 1)
        self.cancel_button.setEnabled(self._batch_running)

    def _on_output_type_changed(self, *_args) -> None:
        if self._loading:
            return
        self._refresh_control_state()
        self._on_parameter_changed()

    def _on_scan_changed(self, *_args) -> None:
        if self._loading:
            return
        direction = self.direction()
        if self.scan() == "horizontal" and direction in ("ttb", "btt"):
            _set_combo_data(self.direction_combo, "ltr" if direction == "ttb" else "rtl")
        elif self.scan() == "vertical" and direction in ("ltr", "rtl"):
            _set_combo_data(self.direction_combo, "ttb" if direction == "ltr" else "btt")
        self._refresh_control_state()
        self._on_parameter_changed()

    def _on_direction_changed(self, *_args) -> None:
        if self._loading:
            return
        _set_combo_data(
            self.scan_combo,
            "horizontal" if self.direction() in ("ltr", "rtl") else "vertical",
        )
        self._refresh_control_state()
        self._on_parameter_changed()

    def _on_depth_changed(self, *_args) -> None:
        if self._loading:
            return
        self._refresh_control_state()
        self._on_parameter_changed()

    def _on_width_changed(self, value: int) -> None:
        if self._loading or self._syncing_size:
            return
        if self.lock_ratio_check.isChecked() and self.image is not None and int(value) > 0:
            original_width, original_height = self.image.size
            self._set_dimension(self.height_spin, round(original_height * int(value) / original_width))
        self._on_parameter_changed()

    def _on_height_changed(self, value: int) -> None:
        if self._loading or self._syncing_size:
            return
        if self.lock_ratio_check.isChecked() and self.image is not None and int(value) > 0:
            original_width, original_height = self.image.size
            self._set_dimension(self.width_spin, round(original_width * int(value) / original_height))
        self._on_parameter_changed()

    def _set_dimension(self, spin: SpinBox, value: int) -> None:
        self._syncing_size = True
        try:
            spin.setValue(max(0, min(int(value), MAX_DIMENSION)))
        finally:
            self._syncing_size = False

    def _sync_size_for_image(self) -> None:
        if not self.lock_ratio_check.isChecked() or self.image is None:
            return
        original_width, original_height = self.image.size
        if self.width_spin.value() > 0:
            self._set_dimension(self.height_spin, round(original_height * self.width_spin.value() / original_width))
        elif self.height_spin.value() > 0:
            self._set_dimension(self.width_spin, round(original_width * self.height_spin.value() / original_height))

    @safe_slot
    def _on_parameter_changed(self, *_args) -> None:
        if self._loading:
            return
        self.save_settings()
        self.refresh_preview()

    @safe_slot
    def _on_setting_text_changed(self, _text: str = "") -> None:
        if not self._loading:
            self.save_settings()

    # ---- 输入与预览 ---------------------------------------------------

    def load(self, path: str | Path | None = None):
        """按路径载入图片或文件夹；为空时读取当前输入框。"""
        text = str(path) if path is not None else (self.input_picker.path() or self.folder_picker.path())
        text = text.strip()
        if not text:
            error = ValueError("请选择图片文件或文件夹。")
            self._fail_load(error)
            raise error
        source = Path(text)
        return self.load_folder(source) if source.is_dir() else self.load_file(source)

    def load_file(self, path: str | Path) -> Image.Image:
        """载入单张图片；失败时写入内联错误并抛出中文异常。"""
        source = Path(path)
        try:
            image = load_image(source)
        except Exception as exc:
            self._fail_load(exc)
            raise
        self.image, self.source, self.source_file = image, source, source
        self.files = [source]
        self._set_input_paths(file_path=source, folder_path=None)
        self._show_image(self.original_preview_label, image)
        self.original_info_label.setText(f"原图：{source.name} · {image.width}×{image.height} · {image.mode}")
        self.input_info_label.setText(f"单张图片：{source.name} · {image.width}×{image.height} 像素。")
        self.status_label.setText(f"已载入 {source.name}。")
        self._set_error("")
        self._sync_size_for_image()
        self.refresh_preview()
        self.save_settings()
        return image

    def load_folder(self, path: str | Path) -> list[Path]:
        """载入文件夹中的所有图片，预览第一张可读图片。"""
        source = Path(path)
        try:
            files = list_image_files(source)
        except Exception as exc:
            self._fail_load(exc)
            raise
        if not files:
            error = ValueError(
                f"文件夹中没有可转换的图片：{source}。请确认文件夹中包含 PNG/BMP/JPG 等常见图片。"
            )
            self._fail_load(error)
            raise error
        image = preview_file = None
        last_error: Exception | None = None
        for candidate in files:
            try:
                image = load_image(candidate)
            except Exception as exc:
                last_error = exc
                continue
            preview_file = candidate
            break
        if image is None or preview_file is None:
            error = ValueError(f"文件夹中的图片都无法读取：{source}" + (f"（{last_error}）" if last_error else ""))
            self._fail_load(error)
            raise error
        self.image, self.source, self.source_file = image, preview_file, preview_file
        self.files = files
        self._set_input_paths(file_path=None, folder_path=source)
        self._show_image(self.original_preview_label, image)
        self.original_info_label.setText(
            f"原图：{preview_file.name} · {image.width}×{image.height} · {image.mode}"
        )
        self.input_info_label.setText(
            f"文件夹：{source} · 共 {len(files)} 张图片，预览 {preview_file.name}。"
        )
        self.status_label.setText(f"已载入 {len(files)} 张图片，可批量转换。")
        self._set_error("")
        self._sync_size_for_image()
        self.refresh_preview()
        self.save_settings()
        return files

    def _set_input_paths(self, file_path: Path | None, folder_path: Path | None) -> None:
        self._setting_input = True
        try:
            self.input_picker.set_path(file_path or "")
            self.folder_picker.set_path(folder_path or "")
        finally:
            self._setting_input = False

    def _fail_load(self, error: Exception) -> None:
        self.image = self.source = self.source_file = None
        self.files = []
        self.last_result = None
        self.generated_text = ""
        self.original_preview_label.clear()
        self.original_preview_label.setText("选择图片后在此预览")
        self.output_preview_label.clear()
        self.output_preview_label.setText("生成后在此预览输出效果")
        self.original_info_label.setText("原图：未载入")
        self.output_info_label.setText("输出：未生成")
        self.code_preview.clear()
        self.progress_bar.setValue(0)
        self.status_label.setText("载入失败。")
        self._set_error(str(error))

    @safe_slot
    def _on_file_path_changed(self, _path: str = "") -> None:
        if self._setting_input or self._loading:
            return
        text = self.input_picker.path()
        if not text:
            return
        path = Path(text)
        if not path.exists():
            self._set_error(f"找不到图片：{path}。请确认路径是否正确。")
            return
        try:
            self.load_folder(path) if path.is_dir() else self.load_file(path)
        except Exception:
            pass  # load_* 已把可读错误写入页面。

    @safe_slot
    def _on_folder_path_changed(self, _path: str = "") -> None:
        if self._setting_input or self._loading:
            return
        text = self.folder_picker.path()
        if not text:
            return
        path = Path(text)
        if not path.exists():
            self._set_error(f"找不到文件夹：{path}。请确认路径是否正确。")
            return
        try:
            self.load_folder(path) if path.is_dir() else self.load_file(path)
        except Exception:
            pass

    def refresh_preview(self) -> ConversionResult | None:
        """按当前参数生成输出预览与代码预览；失败时写入内联错误。"""
        if self.image is None:
            self.last_result = None
            self.generated_text = ""
            self.output_preview_label.clear()
            self.output_preview_label.setText("生成后在此预览输出效果")
            self.output_info_label.setText("输出：未生成")
            self.code_preview.clear()
            return None
        try:
            result = convert_image(self.image, self.current_params(), name=self._array_name())
        except Exception as exc:
            self.last_result = None
            self.generated_text = ""
            self.output_preview_label.clear()
            self.output_preview_label.setText("参数无效")
            self.output_info_label.setText("输出：参数错误")
            self.code_preview.clear()
            self._set_error(str(exc))
            return None
        self.last_result = result
        self.generated_text = result.text or ""
        self._show_image(self.output_preview_label, result.preview)
        self.output_info_label.setText(
            f"输出：{result.width}×{result.height} · {self.depth()} 位 · {len(result.data)} 字节"
        )
        self.code_preview.setPlainText(self._code_preview_text(result))
        self._set_error("")
        return result

    def _array_name(self) -> str:
        return self.source_file.stem if self.source_file is not None else "image"

    def _code_preview_text(self, result: ConversionResult) -> str:
        if result.text:
            lines = result.text.splitlines()
            shown = "\n".join(lines[:MAX_CODE_LINES])
            if len(lines) > MAX_CODE_LINES:
                shown += f"\n/* … 已截断，共 {len(lines)} 行；导出后查看完整文件 */"
            return shown
        data = result.data
        preview = data[:MAX_BIN_PREVIEW]
        lines = [f"/* BIN 原始数据：{len(data)} 字节；以下为前 {len(preview)} 字节 */"]
        for offset in range(0, len(preview), 16):
            chunk = preview[offset : offset + 16]
            lines.append(f"{offset:08X}  " + " ".join(f"{value:02X}" for value in chunk))
        if len(data) > len(preview):
            lines.append(f"/* … 已截断，共 {len(data)} 字节 */")
        return "\n".join(lines)

    def _show_image(self, label: QLabel, image: Image.Image) -> None:
        pixmap = _pil_to_pixmap(image)
        label.setPixmap(
            pixmap.scaled(
                label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # ---- 导出 ---------------------------------------------------------

    def output_dir(self) -> Path | None:
        text = self.output_dir_edit.text().strip()
        if text:
            return Path(text)
        if self.source_file is not None:
            return self.source_file.parent
        return self.files[0].parent if self.files else None

    def set_output_dir(self, directory: str | Path) -> None:
        self.output_dir_edit.setText(str(directory))

    def output_name(self) -> str | None:
        return self.name_edit.text().strip() or None

    def set_output_name(self, name: str) -> None:
        self.name_edit.setText(str(name))

    def batch_files(self) -> list[Path]:
        if self.files:
            return list(self.files)
        return [self.source_file] if self.source_file is not None else []

    def export_current(self, output_dir: str | Path | None = None, name: str | None = None) -> list[Path]:
        """转换当前图片并保存；不弹出对话框，失败抛出中文异常。"""
        if self.image is None or self.source_file is None:
            error = ValueError("请先选择图片文件。")
            self._set_error(str(error))
            raise error
        directory = Path(output_dir) if output_dir is not None else self.output_dir()
        if directory is None:
            error = ValueError("请选择输出目录。")
            self._set_error(str(error))
            raise error
        self._cancel_requested = False
        self.progress_bar.setValue(10)
        self.status_label.setText("正在转换并保存…")
        try:
            written = convert_file(
                self.source_file,
                self.current_params(),
                directory,
                name=name if name is not None else self.output_name(),
            )
        except Exception as exc:
            self.progress_bar.setValue(0)
            self.status_label.setText("转换失败。")
            self._set_error(str(exc))
            raise
        self.progress_bar.setValue(100)
        self.status_label.setText(f"已保存 {written[0].name} 到 {directory}。")
        self._set_error("")
        self.save_settings()
        return written

    def convert(self, output_dir: str | Path | None = None, name: str | None = None) -> list[Path]:
        return self.export_current(output_dir=output_dir, name=name)

    def save(self, output_dir: str | Path | None = None) -> list[Path]:
        return self.export_current(output_dir=output_dir)

    def batch_export(
        self, paths: list[str | Path] | None = None, output_dir: str | Path | None = None
    ) -> BatchResult:
        """批量转换当前文件夹或指定文件；返回每个输入对应的输出列表。"""
        file_list = [Path(item) for item in paths] if paths is not None else self.batch_files()
        if not file_list:
            error = ValueError("请先选择图片文件或文件夹。")
            self._set_error(str(error))
            raise error
        directory = Path(output_dir) if output_dir is not None else self.output_dir()
        if directory is None:
            error = ValueError("请选择输出目录。")
            self._set_error(str(error))
            raise error
        self._cancel_requested = False
        self._batch_running = True
        self._batch_done = 0
        self.cancel_button.setEnabled(True)
        self.convert_button.setEnabled(False)
        self.batch_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.status_label.setText(f"正在批量转换 {len(file_list)} 张图片…")
        self._set_error("")
        try:
            result = batch_convert(
                file_list,
                self.current_params(),
                directory,
                progress=self._on_batch_progress,
                cancel=lambda: self._cancel_requested,
            )
        except Exception as exc:
            self.status_label.setText("批量转换失败。")
            self.progress_bar.setValue(0)
            self._set_error(str(exc))
            raise
        finally:
            self._batch_running = False
            self.cancel_button.setEnabled(False)
            self.convert_button.setEnabled(True)
            self.batch_button.setEnabled(True)
        success = sum(len(outputs) for outputs in result.values())
        failed = len(result.errors)
        if result.canceled:
            self.status_label.setText(
                f"已取消：已处理 {self._batch_done} / {len(file_list)} 张，成功 {success} 个，失败 {failed} 个。"
            )
        else:
            self.progress_bar.setValue(100)
            self.status_label.setText(f"批量完成：成功 {success} 个，失败 {failed} 个。")
        if result.errors:
            self._set_error(_batch_error_summary(result))
        self.save_settings()
        return result

    def batch_convert(
        self, paths: list[str | Path] | None = None, output_dir: str | Path | None = None
    ) -> BatchResult:
        return self.batch_export(paths=paths, output_dir=output_dir)

    def _on_batch_progress(self, done: int, total: int) -> None:
        self._batch_done = done
        self.progress_bar.setValue(int(done * 100 / total) if total else 0)
        self.status_label.setText(f"正在批量转换 {done} / {total} 张…")
        QApplication.processEvents()  # 让取消按钮与进度条在大批量转换时保持响应。

    def cancel_batch(self) -> None:
        if self._batch_running:
            self._cancel_requested = True
            self.status_label.setText("正在取消批量转换…")

    def _choose_background(self) -> None:
        color = QColorDialog.getColor(QColor(*self._background), self, "选择背景色")
        if color.isValid():
            self.set_background((color.red(), color.green(), color.blue()))

    def _browse_output_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择图片输出目录", str(self.output_dir() or ""))
        if selected:
            self.output_dir_edit.setText(selected)

    @safe_slot
    def _on_convert_clicked(self, *_args) -> None:
        try:
            written = self.export_current()
        except Exception as exc:
            show_error(self, "转换失败", str(exc))
            return
        show_success(self, "转换完成", f"已保存：{written[0]}")

    @safe_slot
    def _on_batch_clicked(self, *_args) -> None:
        try:
            result = self.batch_export()
        except Exception as exc:
            show_error(self, "批量转换失败", str(exc))
            return
        if result.errors:
            show_error(self, "部分图片转换失败", _batch_error_summary(result))
            return
        show_success(self, "批量转换完成", f"已写出 {sum(len(v) for v in result.values())} 个文件。")

    @safe_slot
    def _on_cancel_clicked(self, *_args) -> None:
        self.cancel_batch()

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    # ---- 生命周期 -----------------------------------------------------

    def has_active_work(self) -> bool:
        """图片页不保持连接或线程；批量转换是同步的。"""
        return False

    def stop_workers(self) -> None:
        """请求取消正在进行的批量转换；没有后台线程需要 join。"""
        self._cancel_requested = True

    def closeEvent(self, event) -> None:
        self._cancel_requested = True
        self.save_settings()
        super().closeEvent(event)


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


def _preview_label(parent: QWidget, text: str) -> QLabel:
    label = QLabel(parent)
    label.setFixedSize(*PREVIEW_SIZE)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet("background-color: #FFFFFF; border: 1px solid #8A8A8A; color: #666666;")
    label.setText(text)
    return label


def _preview_column(parent: QWidget, title: str, label: QLabel, caption: CaptionLabel) -> QVBoxLayout:
    column = QVBoxLayout()
    column.setSpacing(6)
    column.addWidget(BodyLabel(title, parent))
    column.addWidget(label, 0, Qt.AlignmentFlag.AlignHCenter)
    caption.setWordWrap(True)
    column.addWidget(caption)
    column.addStretch(1)
    return column


def _combo(parent: QWidget, items, width: int, tooltip: str | None = None) -> ComboBox:
    combo = ComboBox(parent)
    _fill_combo(combo, items)
    combo.setFixedWidth(width)
    if tooltip:
        combo.setToolTip(tooltip)
    return combo


def _fill_combo(combo: ComboBox, items) -> None:
    for label, value in items:
        combo.addItem(label, userData=value)


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


def _dimension_spin(parent: QWidget) -> SpinBox:
    spin = SpinBox(parent)
    spin.setRange(0, MAX_DIMENSION)
    spin.setValue(0)
    spin.setSpecialValueText("原始")
    spin.setFixedWidth(100)
    spin.setToolTip("0 表示保持原尺寸；锁定比例时只需修改一个方向")
    return spin


def _safe_int(value) -> int:
    try:
        return max(0, min(int(value or 0), MAX_DIMENSION))
    except (TypeError, ValueError):
        return 0


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


def _pil_to_pixmap(image: Image.Image) -> QPixmap:
    rgba = image.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimage = QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888).copy()
    return QPixmap.fromImage(qimage)

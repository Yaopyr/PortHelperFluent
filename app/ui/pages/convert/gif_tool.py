"""GIF→BMP 工具：逐帧预览、调速播放、缩放导出与进度反馈。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    LineEdit,
    ListWidget,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    Slider,
    SpinBox,
    ToolButton,
)

from app.core.gif_tools import GifFrame, export_frames, load_gif
from app.core.logging_setup import get_logger
from app.ui.feedback import show_error, show_success
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.file_picker import FilePicker

ERROR_COLOR = "#C42B1C"
SETTINGS_DIR_KEY = "convert.gif.output_dir"
MIN_SPEED = 10
MAX_SPEED = 400
PREVIEW_SIZE = (260, 200)
GIF_HINT = (
    "载入 GIF 后逐帧预览并调速；导出为 名称_0001.bmp、名称_0002.bmp…。"
    "输出宽高填 0 表示保持原始尺寸；只填一个时按比例缩放。"
)


class GifTool(QWidget):
    """GIF 拆帧工具；导出逻辑集中在 :meth:`export`，可脱离对话框直接测试。"""

    title = "GIF→BMP"
    route = "gif"

    def __init__(self, settings=None, paths=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.paths = paths
        self.setObjectName("convertGifTool")
        self.frames: list[GifFrame] = []
        self.gif_width = 0
        self.gif_height = 0
        self.source: Path | None = None
        self._setting_input = False
        self._loading = False
        self._cancel_requested = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance_frame)
        self._build_ui()
        self._restore_output_dir()

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        card, layout = section_card("GIF→BMP", GIF_HINT)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        label = BodyLabel("GIF 文件", card)
        label.setMinimumWidth(64)
        input_row.addWidget(label)
        self.input_picker = FilePicker(card, placeholder="选择 GIF 文件，或把文件拖到这里")
        self.input_picker.pathChanged.connect(self._on_path_changed)
        input_row.addWidget(self.input_picker, 1)
        layout.addLayout(input_row)

        self.frame_info_label = CaptionLabel("尚未载入 GIF 文件。", card)
        self.frame_info_label.setWordWrap(True)
        layout.addWidget(self.frame_info_label)

        preview_row = QHBoxLayout()
        preview_row.setSpacing(12)
        self.frame_list = ListWidget(card)
        self.frame_list.setFixedWidth(180)
        self.frame_list.setMinimumHeight(200)
        self.frame_list.setToolTip("选择帧进行预览")
        self.frame_list.currentRowChanged.connect(self._on_frame_selected)
        preview_row.addWidget(self.frame_list)

        preview_column = QVBoxLayout()
        preview_column.setSpacing(6)
        self.preview_label = QLabel(card)
        self.preview_label.setFixedSize(*PREVIEW_SIZE)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet(
            "background-color: #FFFFFF; border: 1px solid #8A8A8A; color: #666666;"
        )
        self.preview_label.setText("载入 GIF 后在此预览")
        preview_column.addWidget(self.preview_label)
        self.preview_caption = CaptionLabel("", card)
        self.preview_caption.setWordWrap(True)
        preview_column.addWidget(self.preview_caption)
        preview_row.addLayout(preview_column, 1)
        layout.addLayout(preview_row)

        control_row = QHBoxLayout()
        control_row.setSpacing(10)
        self.play_button = PushButton("播放", card)
        self.play_button.setIcon(FluentIcon.PLAY)
        self.play_button.setToolTip("按当前帧时长循环预览")
        self.play_button.clicked.connect(self.toggle_playback)
        control_row.addWidget(self.play_button)

        control_row.addWidget(BodyLabel("速度", card))
        self.speed_slider = Slider(card)
        self.speed_slider.setOrientation(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(MIN_SPEED, MAX_SPEED)
        self.speed_slider.setValue(100)
        self.speed_slider.setFixedWidth(180)
        self.speed_slider.setToolTip("预览播放速度（百分比）")
        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        control_row.addWidget(self.speed_slider)
        self.speed_label = CaptionLabel("100%", card)
        self.speed_label.setFixedWidth(52)
        control_row.addWidget(self.speed_label)
        control_row.addStretch(1)
        layout.addLayout(control_row)

        size_row = QHBoxLayout()
        size_row.setSpacing(10)
        size_row.addWidget(BodyLabel("输出宽", card))
        self.width_spin = SpinBox(card)
        self.width_spin.setRange(0, 4096)
        self.width_spin.setValue(0)
        self.width_spin.setSpecialValueText("原始")
        self.width_spin.setFixedWidth(96)
        size_row.addWidget(self.width_spin)
        size_row.addWidget(BodyLabel("输出高", card))
        self.height_spin = SpinBox(card)
        self.height_spin.setRange(0, 4096)
        self.height_spin.setValue(0)
        self.height_spin.setSpecialValueText("原始")
        self.height_spin.setFixedWidth(96)
        size_row.addWidget(self.height_spin)
        size_row.addWidget(
            CaptionLabel("0 = 原始尺寸；只填一个时按比例缩放。", card), 1
        )
        layout.addLayout(size_row)

        dir_row = QHBoxLayout()
        dir_row.setSpacing(10)
        dir_label = BodyLabel("导出目录", card)
        dir_label.setMinimumWidth(64)
        dir_row.addWidget(dir_label)
        self.output_dir_edit = LineEdit(card)
        self.output_dir_edit.setPlaceholderText("选择 BMP 导出目录")
        self.output_dir_edit.setClearButtonEnabled(True)
        dir_row.addWidget(self.output_dir_edit, 1)
        self.browse_dir_button = ToolButton(card)
        self.browse_dir_button.setIcon(FluentIcon.FOLDER)
        self.browse_dir_button.setToolTip("选择导出目录")
        self.browse_dir_button.clicked.connect(self._browse_output_dir)
        dir_row.addWidget(self.browse_dir_button)
        layout.addLayout(dir_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(12)
        self.export_button = PrimaryPushButton("导出 BMP", card)
        self.export_button.clicked.connect(self._on_export_clicked)
        action_row.addWidget(self.export_button)
        self.cancel_button = PushButton("取消", card)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_export)
        action_row.addWidget(self.cancel_button)
        self.progress_bar = ProgressBar(card)
        self.progress_bar.setValue(0)
        action_row.addWidget(self.progress_bar, 1)
        layout.addLayout(action_row)

        self.error_label = CaptionLabel("", card)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {ERROR_COLOR};")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        self.status_label = CaptionLabel("载入 GIF 后可预览并导出 BMP。", card)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        root.addWidget(card)
        root.addStretch(1)

    # ---- 载入与预览 ---------------------------------------------------

    def load(self, path: str | Path | None = None) -> list[GifFrame]:
        """载入 GIF；失败时写入内联错误并抛出中文异常。"""
        source_text = str(path) if path is not None else self.input_picker.path()
        source_text = source_text.strip()
        self.stop_playback()
        self._cancel_requested = False
        if not source_text:
            error = ValueError("请选择 GIF 文件。")
            self._fail_load(error)
            raise error
        source = Path(source_text)
        self._loading = True
        try:
            frames, width, height = load_gif(source)
        except Exception as exc:
            self._fail_load(exc)
            raise
        finally:
            self._loading = False

        self.source = source
        self.frames = frames
        self.gif_width = width
        self.gif_height = height
        self._fill_frame_list()
        self.progress_bar.setValue(0)
        self.cancel_button.setEnabled(False)
        self._set_error("")
        self.frame_info_label.setText(
            f"{len(frames)} 帧 · {width}×{height} 像素 · "
            f"总时长 {sum(frame.duration_ms for frame in frames)} ms"
        )
        self.status_label.setText(f"已载入 {source.name}。")
        if self.input_picker.path() != str(source):
            self._setting_input = True
            try:
                self.input_picker.set_path(source)
            finally:
                self._setting_input = False
        if not self.output_dir_edit.text().strip():
            self.output_dir_edit.setText(str(source.parent))
        return frames

    def frame_count(self) -> int:
        return len(self.frames)

    def frame_durations(self) -> list[int]:
        return [frame.duration_ms for frame in self.frames]

    def _fail_load(self, error: Exception) -> None:
        self.frames = []
        self.source = None
        self.frame_list.clear()
        self.preview_label.clear()
        self.preview_label.setText("载入 GIF 后在此预览")
        self.preview_caption.clear()
        self.frame_info_label.setText("载入失败。")
        self.progress_bar.setValue(0)
        self.status_label.setText("载入 GIF 失败。")
        self._set_error(str(error))

    def _fill_frame_list(self) -> None:
        self.frame_list.blockSignals(True)
        try:
            self.frame_list.clear()
            for index, frame in enumerate(self.frames):
                item = QListWidgetItem(
                    f"帧 {index + 1:02d} · {frame.duration_ms} ms"
                )
                self.frame_list.addItem(item)
            if self.frames:
                self.frame_list.setCurrentRow(0)
        finally:
            self.frame_list.blockSignals(False)
        if self.frames:
            self._show_frame(0)

    def current_frame_index(self) -> int:
        return max(self.frame_list.currentRow(), 0)

    def set_preview_index(self, index: int) -> None:
        if not self.frames:
            return
        index = max(0, min(int(index), len(self.frames) - 1))
        self.frame_list.setCurrentRow(index)
        self._show_frame(index)

    @safe_slot
    def _on_frame_selected(self, row: int, *_args) -> None:
        if 0 <= row < len(self.frames):
            self._show_frame(row)
            if self._timer.isActive():
                self._timer.setInterval(self._interval_for(row))

    def _show_frame(self, index: int) -> None:
        frame = self.frames[index]
        pixmap = _pil_to_pixmap(frame.image)
        self.preview_label.setPixmap(
            pixmap.scaled(
                self.preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.preview_caption.setText(
            f"第 {index + 1} / {len(self.frames)} 帧 · {frame.duration_ms} ms · "
            f"{frame.image.width}×{frame.image.height}"
        )

    # ---- 播放与调速 ---------------------------------------------------

    def speed(self) -> int:
        return int(self.speed_slider.value())

    def set_speed(self, percent: int) -> None:
        self.speed_slider.setValue(
            max(MIN_SPEED, min(int(percent), MAX_SPEED))
        )

    @safe_slot
    def _on_speed_changed(self, value: int, *_args) -> None:
        self.speed_label.setText(f"{value}%")
        if self._timer.isActive():
            self._timer.setInterval(self._interval_for(self.current_frame_index()))

    def _interval_for(self, index: int) -> int:
        if not self.frames:
            return 100
        duration = max(self.frames[index].duration_ms, 20)
        interval = duration * 100 / max(self.speed(), 1)
        return max(10, int(interval))

    def is_playing(self) -> bool:
        return self._timer.isActive()

    def toggle_playback(self) -> bool:
        if self._timer.isActive():
            self.pause()
            return False
        return self.play()

    def play(self) -> bool:
        if not self.frames:
            self.status_label.setText("请先载入 GIF 文件。")
            return False
        self._timer.start(self._interval_for(self.current_frame_index()))
        self.play_button.setText("暂停")
        self.play_button.setIcon(FluentIcon.PAUSE)
        return True

    def pause(self) -> None:
        self._timer.stop()
        self.play_button.setText("播放")
        self.play_button.setIcon(FluentIcon.PLAY)

    def stop_playback(self) -> None:
        self.pause()

    def _advance_frame(self) -> None:
        if not self.frames:
            self.pause()
            return
        self.set_preview_index((self.current_frame_index() + 1) % len(self.frames))

    # ---- 导出 ---------------------------------------------------------

    def output_dir(self) -> Path | None:
        text = self.output_dir_edit.text().strip()
        if text:
            return Path(text)
        if self.source is not None:
            return self.source.parent
        return None

    def set_output_dir(self, directory: str | Path) -> None:
        self.output_dir_edit.setText(str(directory))

    def _browse_output_dir(self) -> None:
        current = str(self.output_dir())
        selected = QFileDialog.getExistingDirectory(self, "选择导出目录", current)
        if selected:
            self.output_dir_edit.setText(selected)

    def export(
        self,
        output_dir: str | Path | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> list[Path]:
        """导出全部帧为 BMP；不弹出对话框，失败抛出中文异常。"""
        if not self.frames:
            error = ValueError("请先载入 GIF 文件。")
            self._set_error(str(error))
            raise error
        directory = Path(output_dir) if output_dir is not None else self.output_dir()
        if directory is None or not str(directory).strip():
            error = ValueError("请选择 BMP 导出目录。")
            self._set_error(str(error))
            raise error
        target_width = self.width_spin.value() if width is None else int(width)
        target_height = self.height_spin.value() if height is None else int(height)
        target_width = target_width or None
        target_height = target_height or None
        stem = self.source.stem if self.source is not None else "frame"

        self.pause()
        self._cancel_requested = False
        self.cancel_button.setEnabled(True)
        self.progress_bar.setValue(0)
        self._set_error("")
        self.status_label.setText(f"正在导出 {len(self.frames)} 帧…")
        try:
            written = export_frames(
                self.frames,
                directory,
                stem,
                width=target_width,
                height=target_height,
                progress=self._on_export_progress,
                cancel=lambda: self._cancel_requested,
            )
        except Exception as exc:
            self.progress_bar.setValue(0)
            self.cancel_button.setEnabled(False)
            self.status_label.setText("导出失败。")
            self._set_error(str(exc))
            raise
        self.cancel_button.setEnabled(False)
        if self._cancel_requested:
            self.progress_bar.setValue(
                int(len(written) * 100 / len(self.frames)) if self.frames else 0
            )
            self.status_label.setText(
                f"已取消导出，已写出 {len(written)} / {len(self.frames)} 个 BMP。"
            )
        else:
            self.progress_bar.setValue(100)
            self.status_label.setText(
                f"已导出 {len(written)} 个 BMP 到 {directory}。"
            )
        self._remember_output_dir(directory)
        return written

    def cancel_export(self) -> None:
        self._cancel_requested = True
        self.status_label.setText("正在取消导出…")

    def _on_export_progress(self, done: int, total: int) -> None:
        self.progress_bar.setValue(int(done * 100 / total) if total else 0)
        self.status_label.setText(f"正在导出 {done} / {total} 帧…")
        # 让进度条与“取消”按钮在大批量导出时保持响应。
        QApplication.processEvents()

    @safe_slot
    def _on_export_clicked(self, *_args) -> None:
        try:
            written = self.export()
        except Exception as exc:
            show_error(self, "导出失败", str(exc))
            return
        show_success(self, "导出完成", f"已写出 {len(written)} 个 BMP 文件。")

    @safe_slot
    def _on_path_changed(self, _path: str) -> None:
        if self._setting_input or self._loading:
            return
        text = self.input_picker.path()
        if not text:
            self.frames = []
            self.source = None
            self.frame_list.clear()
            self.preview_label.clear()
            self.preview_label.setText("载入 GIF 后在此预览")
            self.preview_caption.clear()
            self.frame_info_label.setText("尚未载入 GIF 文件。")
            self._set_error("")
            return
        if not Path(text).is_file():
            return
        try:
            self.load(text)
        except Exception:
            # load() 已把可读错误写入页面，这里不再弹窗或抛出。
            pass

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _remember_output_dir(self, directory: Path) -> None:
        if self.settings is None:
            return
        try:
            self.settings.set(SETTINGS_DIR_KEY, str(directory))
            self.settings.save()
        except Exception:
            get_logger().exception("保存 GIF 导出目录失败")

    def _restore_output_dir(self) -> None:
        if self.settings is None:
            return
        try:
            saved = str(self.settings.get(SETTINGS_DIR_KEY, "") or "")
        except Exception:
            get_logger().exception("读取 GIF 导出目录失败")
            return
        if saved:
            self.output_dir_edit.setText(saved)

    def stop_workers(self) -> None:
        """关闭页面时停止预览定时器与导出。"""
        self._cancel_requested = True
        self.stop_playback()

    def hideEvent(self, event) -> None:
        # 切换到其他工具时停止预览，避免定时器在后台空转。
        self.stop_playback()
        super().hideEvent(event)


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

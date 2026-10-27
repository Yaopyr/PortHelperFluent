"""音频→C 工具：WAV/MP3 载入、异步播放与 C 数组导出。

WAV 使用标准库 ``winsound`` 异步播放，MP3 使用 Windows MCI
（``mciSendStringW``，通过 ctypes 调用），不引入额外依赖；两者都不会阻塞
界面，关闭页面时会停止播放并释放 MCI 句柄。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
)

from app.core.audio_tools import (
    AudioData,
    audio_duration_s,
    audio_to_c,
    audio_to_h,
    c_identifier,
    load_audio,
)
from app.core.logging_setup import get_logger
from app.services.exports import write_text_atomic
from app.ui.feedback import show_error, show_success
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.file_picker import FilePicker

try:  # pragma: no cover - 非 Windows 环境只用于导入时不崩溃
    import winsound
except ImportError:  # pragma: no cover
    winsound = None  # type: ignore[assignment]

ERROR_COLOR = "#C42B1C"
SETTINGS_EXPORT_KEY = "convert.audio.last_export"
MCI_LENGTH = 1024
AUDIO_HINT = (
    "载入 WAV/MP3 后查看格式与时长；WAV 用 winsound 播放，MP3 用 Windows MCI "
    "解码播放（需要本机 ffmpeg 读取）。导出生成配对的 .c 与 .h 文件。"
)
C_HINT = (
    "数组名会被清洗为合法 C 标识符；最大采样点数按每声道计算，"
    "留空或填 0 表示导出全部采样。"
)


class AudioTool(QWidget):
    """音频查看与 C 数组工具；播放与导出逻辑均可脱离对话框调用。"""

    title = "音频→C"
    route = "audio"

    def __init__(self, settings=None, paths=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.paths = paths
        self.setObjectName("convertAudioTool")
        self.audio: AudioData | None = None
        self.source: Path | None = None
        self._setting_input = False
        self._setting_output = False
        self._auto_output = True
        self._playing = False
        self._mci_alias: str | None = None
        self._mci_opened = False
        self._build_ui()

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        source_card, source_layout = section_card("音频文件", AUDIO_HINT)
        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        label = BodyLabel("音频文件", source_card)
        label.setMinimumWidth(64)
        input_row.addWidget(label)
        self.input_picker = FilePicker(
            source_card, placeholder="选择 WAV/MP3 文件，或把文件拖到这里"
        )
        self.input_picker.pathChanged.connect(self._on_path_changed)
        input_row.addWidget(self.input_picker, 1)
        source_layout.addLayout(input_row)

        self.info_label = CaptionLabel("尚未载入音频文件。", source_card)
        self.info_label.setWordWrap(True)
        source_layout.addWidget(self.info_label)

        playback_row = QHBoxLayout()
        playback_row.setSpacing(10)
        self.play_button = PushButton("播放", source_card)
        self.play_button.setIcon(FluentIcon.PLAY)
        self.play_button.clicked.connect(self._on_play_clicked)
        playback_row.addWidget(self.play_button)
        self.stop_button = PushButton("停止", source_card)
        self.stop_button.setIcon(FluentIcon.PAUSE)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        playback_row.addWidget(self.stop_button)
        playback_row.addWidget(
            CaptionLabel(
                "异步播放，不阻塞界面；关闭页面自动停止。", source_card
            ),
            1,
        )
        source_layout.addLayout(playback_row)

        self.error_label = CaptionLabel("", source_card)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {ERROR_COLOR};")
        self.error_label.setVisible(False)
        source_layout.addWidget(self.error_label)
        root.addWidget(source_card)

        export_card, export_layout = section_card("C 数组导出", C_HINT)
        name_row = QHBoxLayout()
        name_row.setSpacing(10)
        name_row.addWidget(BodyLabel("数组名", export_card))
        self.name_edit = LineEdit(export_card)
        self.name_edit.setPlaceholderText("例如 audio")
        self.name_edit.setFixedWidth(180)
        name_row.addWidget(self.name_edit)
        name_row.addSpacing(8)
        name_row.addWidget(BodyLabel("最大采样点数", export_card))
        self.max_samples_edit = LineEdit(export_card)
        self.max_samples_edit.setPlaceholderText("留空或 0 = 全部")
        self.max_samples_edit.setFixedWidth(170)
        self.max_samples_edit.setValidator(QIntValidator(0, 1_000_000_000, self))
        name_row.addWidget(self.max_samples_edit)
        name_row.addStretch(1)
        export_layout.addLayout(name_row)

        c_row = QHBoxLayout()
        c_row.setSpacing(10)
        c_label = BodyLabel("C 源文件", export_card)
        c_label.setMinimumWidth(64)
        c_row.addWidget(c_label)
        self.c_picker = FilePicker(
            export_card, save_mode=True, placeholder="选择 .c 输出文件"
        )
        self.c_picker.pathChanged.connect(self._on_output_changed)
        c_row.addWidget(self.c_picker, 1)
        export_layout.addLayout(c_row)

        h_row = QHBoxLayout()
        h_row.setSpacing(10)
        h_label = BodyLabel("头文件", export_card)
        h_label.setMinimumWidth(64)
        h_row.addWidget(h_label)
        self.h_picker = FilePicker(
            export_card, save_mode=True, placeholder="选择 .h 输出文件"
        )
        self.h_picker.pathChanged.connect(self._on_output_changed)
        h_row.addWidget(self.h_picker, 1)
        export_layout.addLayout(h_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(12)
        self.convert_button = PrimaryPushButton("转换为 C 并保存", export_card)
        self.convert_button.clicked.connect(self._on_convert_clicked)
        action_row.addWidget(self.convert_button)
        self.progress_bar = ProgressBar(export_card)
        self.progress_bar.setValue(0)
        action_row.addWidget(self.progress_bar, 1)
        export_layout.addLayout(action_row)

        self.status_label = CaptionLabel("载入音频后可生成 C 数组。", export_card)
        self.status_label.setWordWrap(True)
        export_layout.addWidget(self.status_label)
        root.addWidget(export_card)
        root.addStretch(1)

    # ---- 载入 ---------------------------------------------------------

    def load(self, path: str | Path | None = None) -> AudioData:
        """载入 WAV/MP3；失败时写入内联错误并抛出中文异常。"""
        source_text = str(path) if path is not None else self.input_picker.path()
        source_text = source_text.strip()
        if not source_text:
            error = ValueError("请选择音频文件。")
            self._fail_load(error)
            raise error
        source = Path(source_text)
        self.stop()
        try:
            audio = load_audio(source)
        except Exception as exc:
            self._fail_load(exc)
            raise

        self.audio = audio
        self.source = source
        duration = audio_duration_s(audio)
        suffix = source.suffix.lstrip(".").upper()
        self.info_label.setText(
            f"格式：{suffix} · 采样率 {audio.sample_rate} Hz · "
            f"声道 {audio.channels} · 位宽 {audio.sample_width * 8} 位 · "
            f"时长 {duration:.3f} 秒（{len(audio.frames)} 字节）"
        )
        self.name_edit.setText(c_identifier(source.stem))
        if self._auto_output or not self.c_picker.path():
            self._refresh_suggested_outputs(source)
        self.progress_bar.setValue(0)
        self._set_error("")
        self.status_label.setText(f"已载入 {source.name}。")
        if self.input_picker.path() != str(source):
            self._setting_input = True
            try:
                self.input_picker.set_path(source)
            finally:
                self._setting_input = False
        return audio

    def _fail_load(self, error: Exception) -> None:
        self.audio = None
        self.source = None
        self.info_label.setText("载入失败。")
        self.progress_bar.setValue(0)
        self.status_label.setText("载入音频失败。")
        self._set_error(str(error))

    def format_info(self) -> str:
        return self.info_label.text()

    def _refresh_suggested_outputs(self, source: Path) -> None:
        c_path = source.with_suffix(".c")
        h_path = source.with_suffix(".h")
        self._setting_output = True
        try:
            self.c_picker.set_path(c_path)
            self.h_picker.set_path(h_path)
        finally:
            self._setting_output = False
        self._auto_output = True

    @safe_slot
    def _on_output_changed(self, _path: str) -> None:
        if self._setting_output:
            return
        self._auto_output = False

    @safe_slot
    def _on_path_changed(self, _path: str) -> None:
        if self._setting_input:
            return
        text = self.input_picker.path()
        if not text:
            self.audio = None
            self.source = None
            self.info_label.setText("尚未载入音频文件。")
            self._set_error("")
            return
        path = Path(text)
        if not path.is_file() or path.suffix.casefold() not in (".wav", ".mp3"):
            return
        try:
            self.load(path)
        except Exception:
            # load() 已显示可读错误；自动载入失败不弹窗。
            pass

    # ---- 播放 ---------------------------------------------------------

    def is_playing(self) -> bool:
        return self._playing

    def play(self) -> bool:
        """异步播放当前音频；失败时返回 False 并写入内联错误。"""
        self._set_error("")
        if self.audio is None or self.source is None:
            self._set_error("请先载入音频文件。")
            return False
        self.stop()
        try:
            if self.source.suffix.casefold() == ".mp3":
                self._play_mci(self.source)
            else:
                self._play_wav(self.source)
        except Exception as exc:
            self._playing = False
            self._set_error(f"播放失败：{exc}")
            self.status_label.setText("播放失败。")
            return False
        self._playing = True
        self.status_label.setText(f"正在播放 {self.source.name}…")
        return True

    def _play_wav(self, source: Path) -> None:
        if winsound is None:
            raise RuntimeError("当前系统不支持 winsound 播放，请改用 Windows。")
        try:
            winsound.PlaySound(
                str(source), winsound.SND_FILENAME | winsound.SND_ASYNC
            )
        except Exception as exc:
            raise RuntimeError(
                f"无法播放 WAV 文件：{source}（{exc}）。请确认音频设备可用。"
            ) from exc

    def _play_mci(self, source: Path) -> None:
        if not _MCI_AVAILABLE:
            raise RuntimeError("当前系统不支持 MCI 音频播放，请改用 Windows。")
        self._close_mci()
        alias = f"porthelper_audio_{id(self):x}"
        commands = (
            f'open "{source}" type mpegvideo alias {alias}',
            f'open "{source}" alias {alias}',
        )
        last_error: Exception | None = None
        for open_command in commands:
            try:
                _mci_send(open_command)
            except RuntimeError as exc:
                last_error = exc
                continue
            self._mci_alias = alias
            self._mci_opened = True
            break
        if not self._mci_opened:
            raise RuntimeError(
                f"无法打开 MP3 文件进行播放：{source}"
                + (f"（{last_error}）" if last_error else "")
            )
        try:
            _mci_send(f"play {alias} from 0")
        except Exception:
            self._close_mci()
            raise

    def stop(self) -> None:
        """停止当前播放并释放 MCI 资源；没有播放时也安全。"""
        if winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                try:
                    winsound.PlaySound(None, 0)
                except Exception:
                    pass
        if self._mci_opened and self._mci_alias:
            try:
                _mci_send(f"stop {self._mci_alias}")
                _mci_send(f"seek {self._mci_alias} to start")
            except Exception:
                pass
        self._playing = False

    def _close_mci(self) -> None:
        if self._mci_opened and self._mci_alias:
            try:
                _mci_send(f"close {self._mci_alias}")
            except Exception:
                get_logger().debug("关闭 MCI 音频别名失败：%s", self._mci_alias)
        self._mci_alias = None
        self._mci_opened = False

    def stop_workers(self) -> None:
        self.stop()
        self._close_mci()

    def closeEvent(self, event) -> None:
        self.stop_workers()
        super().closeEvent(event)

    @safe_slot
    def _on_play_clicked(self, *_args) -> None:
        self.play()

    @safe_slot
    def _on_stop_clicked(self, *_args) -> None:
        self.stop()
        self.status_label.setText("已停止播放。")

    # ---- 导出 ---------------------------------------------------------

    def _max_samples_value(self) -> int | None:
        text = self.max_samples_edit.text().strip()
        if not text:
            return None
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError("最大采样点数必须是整数。") from exc
        if value < 0:
            raise ValueError("最大采样点数不能为负数。")
        return value or None

    def suggested_paths(self) -> tuple[Path, Path] | None:
        if self.source is None:
            return None
        return self.source.with_suffix(".c"), self.source.with_suffix(".h")

    def export_c(
        self,
        c_path: str | Path | None = None,
        h_path: str | Path | None = None,
        name: str | None = None,
        max_samples: int | None = None,
    ) -> tuple[Path, Path]:
        """生成并保存 ``.c``/``.h``；不弹出对话框，失败抛出中文异常。"""
        if self.audio is None or self.source is None:
            error = ValueError("请先载入音频文件。")
            self._set_error(str(error))
            raise error
        try:
            target_c, target_h = self._resolve_output_paths(c_path, h_path)
            if _same_path(target_c, target_h):
                raise ValueError("C 源文件与头文件不能是同一个路径。")
            raw_name = (name or self.name_edit.text()).strip() or self.source.stem
            identifier = c_identifier(raw_name)
            limit = self._max_samples_value() if max_samples is None else max_samples
        except ValueError as exc:
            self._set_error(str(exc))
            raise
        if self.name_edit.text().strip() != identifier:
            self.name_edit.setText(identifier)

        self._set_error("")
        self.progress_bar.setValue(5)
        self.status_label.setText("正在生成 C 数组…")
        try:
            source_text = audio_to_c(self.audio, identifier, max_samples=limit)
            header_text = audio_to_h(self.audio, identifier, max_samples=limit)
        except Exception as exc:
            self.progress_bar.setValue(0)
            self.status_label.setText("生成 C 数组失败。")
            self._set_error(str(exc))
            raise
        self.progress_bar.setValue(50)
        try:
            write_text_atomic(target_c, source_text, encoding="utf-8")
            write_text_atomic(target_h, header_text, encoding="utf-8")
        except OSError as exc:
            self.progress_bar.setValue(0)
            self.status_label.setText("写入文件失败。")
            message = (
                f"写入 C 文件失败：{target_c} / {target_h}（{exc}）。"
                "请检查目录权限或文件是否被占用。"
            )
            self._set_error(message)
            raise OSError(message) from exc

        self.progress_bar.setValue(100)
        sample_count = _sample_count(self.audio, limit)
        self.status_label.setText(
            f"已生成 {target_c.name} 与 {target_h.name}（{sample_count} 个采样点）。"
        )
        self._setting_output = True
        try:
            self.c_picker.set_path(target_c)
            self.h_picker.set_path(target_h)
        finally:
            self._setting_output = False
        self._remember_export_dir(target_c.parent)
        return target_c, target_h

    def _resolve_output_paths(
        self, c_path: str | Path | None, h_path: str | Path | None
    ) -> tuple[Path, Path]:
        if c_path is not None and h_path is not None:
            return Path(c_path), Path(h_path)
        if c_path is not None:
            target_c = Path(c_path)
            return target_c, target_c.with_suffix(".h")
        if h_path is not None:
            target_h = Path(h_path)
            return target_h.with_suffix(".c"), target_h
        c_text = self.c_picker.path().strip()
        h_text = self.h_picker.path().strip()
        if c_text and h_text:
            return Path(c_text), Path(h_text)
        suggested = self.suggested_paths()
        if suggested is not None:
            target_c, target_h = suggested
            return (
                Path(c_text) if c_text else target_c,
                Path(h_text) if h_text else target_h,
            )
        raise ValueError("请选择 .c 与 .h 输出路径。")

    @safe_slot
    def _on_convert_clicked(self, *_args) -> None:
        try:
            c_path, h_path = self.export_c()
        except Exception as exc:
            show_error(self, "导出失败", str(exc))
            return
        show_success(self, "导出完成", f"{c_path}\n{h_path}")

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _remember_export_dir(self, directory: Path) -> None:
        if self.settings is None:
            return
        try:
            self.settings.set(SETTINGS_EXPORT_KEY, str(directory))
            self.settings.save()
        except Exception:
            get_logger().exception("保存音频导出目录失败")


def _sample_count(audio: AudioData, max_samples: int | None) -> int:
    frame_size = max(audio.channels * audio.sample_width, 1)
    total = len(audio.frames) // frame_size
    if max_samples is None:
        return total
    return min(total, max(int(max_samples), 0))


def _same_path(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return str(first).casefold() == str(second).casefold()


_MCI_AVAILABLE = sys.platform == "win32"


def _mci_send(command: str) -> str:
    """执行一条 MCI 命令；失败时抛出带 MCI 错误文本的中文异常。"""
    if not _MCI_AVAILABLE:
        raise RuntimeError("当前系统不支持 MCI 音频播放。")
    buffer = ctypes.create_unicode_buffer(MCI_LENGTH)
    code = ctypes.windll.winmm.mciSendStringW(
        ctypes.c_wchar_p(command), buffer, MCI_LENGTH, None
    )
    if code != 0:
        error_buffer = ctypes.create_unicode_buffer(MCI_LENGTH)
        ctypes.windll.winmm.mciGetErrorStringW(
            code, error_buffer, MCI_LENGTH
        )
        detail = error_buffer.value.strip() or f"错误码 {code}"
        raise RuntimeError(detail)
    return buffer.value

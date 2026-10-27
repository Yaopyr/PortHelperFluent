"""Bin/Hex/Text 文件转换：二进制、十六进制文本与按编码解码的文本互转。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    SegmentedWidget,
)

from app.core.encoding import (
    bin_file_to_hex_text,
    decode_bytes,
    hex_text_to_bytes,
    hex_text_to_text,
    resolve_codec,
    to_hex,
)
from app.core.logging_setup import get_logger
from app.services.exports import write_bytes_atomic, write_text_atomic
from app.ui.feedback import show_error, show_success
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.file_picker import FilePicker

OP_BIN_TO_TEXT = "bin_to_text"
OP_HEX_TO_TEXT = "hex_to_text"
OP_HEX_TO_BIN = "hex_to_bin"
OPERATION_ITEMS = (
    (OP_BIN_TO_TEXT, "Bin→Text"),
    (OP_HEX_TO_TEXT, "Hex→Text"),
    (OP_HEX_TO_BIN, "Hex→Bin"),
)
OPERATION_LABELS = {
    OP_BIN_TO_TEXT: "Bin→Text",
    OP_HEX_TO_TEXT: "Hex→Text",
    OP_HEX_TO_BIN: "Hex→Bin",
}
OPERATION_INDEX = {key: index for index, (key, _text) in enumerate(OPERATION_ITEMS)}
ENCODING_ITEMS = (("ANSI（系统）", "ansi"), ("GBK", "gbk"), ("UTF-8", "utf-8"))
PREVIEW_BYTES = 256
ERROR_COLOR = "#C42B1C"
DATA_HINT = (
    "Bin→Text：二进制文件转大写十六进制文本；Hex→Text：十六进制文本按所选编码解码为文本；"
    "Hex→Bin：十六进制文本转原始字节。输出使用原子写入，失败不会留下半截文件。"
)


class DataTool(QWidget):
    """文件转换工具；转换逻辑在 :meth:`convert` 中，可脱离对话框直接测试。"""

    title = "Bin/Hex/文本"
    route = "data"

    def __init__(self, settings=None, paths=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.paths = paths
        self.setObjectName("convertDataTool")
        self.last_input_path: Path | None = None
        self.last_output_path: Path | None = None
        self.last_input_size = 0
        self.last_output_size = 0
        self._setting_output = False
        self._auto_output = True
        self._build_ui()
        self._on_operation_changed(self.operation())

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        card, layout = section_card("文件转换", DATA_HINT)

        self.operation_switch = SegmentedWidget(card)
        for key, text in OPERATION_ITEMS:
            self.operation_switch.addItem(key, text)
        self.operation_switch.setCurrentItem(OP_BIN_TO_TEXT)
        self.operation_switch.currentItemChanged.connect(self._on_operation_changed)
        layout.addWidget(self.operation_switch)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)
        label = BodyLabel("输入文件", card)
        label.setMinimumWidth(64)
        input_row.addWidget(label)
        self.input_picker = FilePicker(card, placeholder="选择输入文件，或把文件拖到这里")
        self.input_picker.pathChanged.connect(self._on_input_changed)
        input_row.addWidget(self.input_picker, 1)
        layout.addLayout(input_row)

        output_row = QHBoxLayout()
        output_row.setSpacing(10)
        label = BodyLabel("输出文件", card)
        label.setMinimumWidth(64)
        output_row.addWidget(label)
        self.output_picker = FilePicker(
            card, save_mode=True, placeholder="选择输出文件，或把文件拖到这里"
        )
        self.output_picker.pathChanged.connect(self._on_output_changed)
        output_row.addWidget(self.output_picker, 1)
        layout.addLayout(output_row)

        encoding_row = QHBoxLayout()
        encoding_row.setSpacing(10)
        encoding_row.addWidget(BodyLabel("文本编码", card))
        self.encoding_combo = ComboBox(card)
        for text, codec in ENCODING_ITEMS:
            self.encoding_combo.addItem(text, userData=codec)
        utf8_index = self.encoding_combo.findData("utf-8")
        self.encoding_combo.setCurrentIndex(max(0, utf8_index))
        self.encoding_combo.setToolTip("仅 Hex→Text 使用；ANSI 表示系统当前代码页")
        encoding_row.addWidget(self.encoding_combo)
        encoding_row.addWidget(
            CaptionLabel("Hex→Text 按此编码生成文本文件。", card), 1
        )
        layout.addLayout(encoding_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(12)
        self.convert_button = PrimaryPushButton("转换", card)
        self.convert_button.clicked.connect(self._on_convert_clicked)
        action_row.addWidget(self.convert_button)
        self.progress_bar = ProgressBar(card)
        self.progress_bar.setValue(0)
        action_row.addWidget(self.progress_bar, 1)
        layout.addLayout(action_row)

        self.error_label = CaptionLabel("", card)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {ERROR_COLOR};")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        self.status_label = CaptionLabel("选择输入/输出文件后点击“转换”。", card)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        root.addWidget(card)

        preview_card, preview_layout = section_card(
            "输出预览", f"转换完成后显示输出文件的前 {PREVIEW_BYTES} 个字节/字符。"
        )
        self.preview_edit = PlainTextEdit(preview_card)
        self.preview_edit.setReadOnly(True)
        self.preview_edit.setFont(_mono_font())
        self.preview_edit.setPlaceholderText("转换完成后在这里显示输出内容预览。")
        self.preview_edit.setMinimumHeight(150)
        preview_layout.addWidget(self.preview_edit, 1)
        root.addWidget(preview_card, 1)

    # ---- 操作与路径 ---------------------------------------------------

    def operation(self) -> str:
        route = self.operation_switch.currentRouteKey()
        return route if route in OPERATION_INDEX else OP_BIN_TO_TEXT

    def set_operation(self, operation: str) -> None:
        """切换转换方向；未知方向抛出中文错误。"""
        if operation not in OPERATION_INDEX:
            raise ValueError(f"未知转换类型：{operation}")
        self.operation_switch.setCurrentItem(operation)
        # setCurrentItem 在已经是当前项时不会发信号，这里显式刷新一次。
        self._on_operation_changed(operation)

    def codec(self) -> str:
        value = self.encoding_combo.currentData()
        return str(value) if value else "utf-8"

    def encoding_label(self) -> str:
        return self.encoding_combo.currentText()

    @staticmethod
    def suggested_output(path: str | Path, operation: str) -> Path:
        """按输入路径与转换方向给出默认输出文件名。"""
        source = Path(path)
        if operation == OP_BIN_TO_TEXT:
            return source.with_name(f"{source.stem}_hex.txt")
        if operation == OP_HEX_TO_TEXT:
            return source.with_name(f"{source.stem}_text.txt")
        return source.with_name(f"{source.stem}_bin.bin")

    @safe_slot
    def _on_operation_changed(self, route: str, *_args) -> None:
        text = OPERATION_LABELS.get(str(route), OPERATION_LABELS[OP_BIN_TO_TEXT])
        self.preview_edit.clear()
        self.progress_bar.setValue(0)
        self.status_label.setText(f"当前模式：{text}。选择输入/输出文件后点击“转换”。")
        self._set_error("")
        if self._auto_output or not self.output_picker.path():
            self._refresh_suggested_output()

    @safe_slot
    def _on_input_changed(self, _path: str) -> None:
        if self._auto_output or not self.output_picker.path():
            self._refresh_suggested_output()

    @safe_slot
    def _on_output_changed(self, _path: str) -> None:
        if self._setting_output:
            return
        self._auto_output = False

    def _refresh_suggested_output(self) -> None:
        source = self.input_picker.path()
        if not source:
            return
        target = self.suggested_output(source, self.operation())
        self._setting_output = True
        try:
            self.output_picker.set_path(target)
        finally:
            self._setting_output = False
        self._auto_output = True

    # ---- 转换 ---------------------------------------------------------

    def convert(
        self,
        input_path: str | Path | None = None,
        output_path: str | Path | None = None,
        operation: str | None = None,
        codec: str | None = None,
    ) -> Path:
        """执行一次转换并返回输出路径；不弹出任何对话框。

        未传入的参数从界面控件读取。校验失败、十六进制语法错误与读写失败都会
        抛出带路径/片段的中文异常，同时把错误写入页面内联提示。
        """
        try:
            target = self._perform_conversion(
                input_path, output_path, operation, codec
            )
        except Exception as exc:
            self._set_error(str(exc))
            self.progress_bar.setValue(0)
            self.status_label.setText("转换失败。")
            raise
        self._set_error("")
        return target

    def Convert(
        self,
        input_path: str | Path | None = None,
        output_path: str | Path | None = None,
        operation: str | None = None,
        codec: str | None = None,
    ) -> Path:
        """任务约定的大写别名，行为与 :meth:`convert` 完全一致。"""
        return self.convert(input_path, output_path, operation, codec)

    def _perform_conversion(
        self,
        input_path: str | Path | None,
        output_path: str | Path | None,
        operation: str | None,
        codec: str | None,
    ) -> Path:
        operation = operation or self.operation()
        if operation not in OPERATION_INDEX:
            raise ValueError(f"未知转换类型：{operation}")
        label = OPERATION_LABELS[operation]
        codec = codec or self.codec()

        source_text = str(input_path) if input_path is not None else self.input_picker.path()
        if not source_text.strip():
            raise ValueError("请选择输入文件。")
        source = Path(source_text.strip())
        if not source.is_file():
            raise ValueError(f"找不到输入文件：{source}。请确认路径是否正确、文件是否已被移动。")

        target_text = (
            str(output_path) if output_path is not None else self.output_picker.path()
        )
        if not target_text.strip():
            raise ValueError("请选择输出文件。")
        target = Path(target_text.strip())
        if _same_path(source, target):
            raise ValueError(f"输出文件不能与输入文件相同：{source}")

        self.progress_bar.setValue(5)
        self.status_label.setText(f"正在读取 {source.name}…")
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise OSError(f"读取输入文件失败：{source}（{exc}）") from exc
        self.last_input_path = source
        self.last_input_size = len(data)
        self.progress_bar.setValue(35)

        if operation == OP_BIN_TO_TEXT:
            output_text = bin_file_to_hex_text(data)
            self.progress_bar.setValue(70)
            try:
                write_text_atomic(target, output_text, encoding="utf-8")
            except OSError as exc:
                raise OSError(f"写入输出文件失败：{target}（{exc}）") from exc
        elif operation == OP_HEX_TO_TEXT:
            input_text = data.decode("utf-8-sig", errors="replace")
            try:
                output_text = hex_text_to_text(input_text, codec)
            except ValueError as exc:
                raise ValueError(
                    f"输入文件“{source}”不是有效的十六进制文本：{exc}"
                ) from exc
            resolved_codec = resolve_codec(codec)
            self.progress_bar.setValue(70)
            try:
                write_text_atomic(target, output_text, encoding=resolved_codec)
            except UnicodeError as exc:
                raise ValueError(
                    f"无法按“{self.encoding_label()}”编码写入输出文件：{target}（{exc}）"
                ) from exc
            except OSError as exc:
                raise OSError(f"写入输出文件失败：{target}（{exc}）") from exc
        else:
            input_text = data.decode("utf-8-sig", errors="replace")
            try:
                raw = hex_text_to_bytes(input_text)
            except ValueError as exc:
                raise ValueError(
                    f"输入文件“{source}”不是有效的十六进制文本：{exc}"
                ) from exc
            self.progress_bar.setValue(70)
            try:
                write_bytes_atomic(target, raw)
            except OSError as exc:
                raise OSError(f"写入输出文件失败：{target}（{exc}）") from exc

        self.progress_bar.setValue(100)
        self.last_output_path = target
        self.last_output_size = _file_size(target)
        self.status_label.setText(
            f"{label} 完成：{source.name}（{self.last_input_size} B）→ "
            f"{target.name}（{self.last_output_size} B）"
        )
        self._update_preview(target, operation, codec)
        return target

    def _update_preview(self, target: Path, operation: str, codec: str) -> None:
        try:
            with target.open("rb") as handle:
                head = handle.read(PREVIEW_BYTES)
        except OSError:
            get_logger().exception("读取输出预览失败：%s", target)
            self.preview_edit.clear()
            return
        if not head:
            self.preview_edit.setPlainText("（输出文件为空）")
            return
        if operation == OP_HEX_TO_BIN:
            text = to_hex(head)
        elif operation == OP_HEX_TO_TEXT:
            text = decode_bytes(head, codec)
        else:
            text = decode_bytes(head, "utf-8")
        if len(head) >= PREVIEW_BYTES:
            text += "\n…（预览已截断）"
        self.preview_edit.setPlainText(text)

    @safe_slot
    def _on_convert_clicked(self, *_args) -> None:
        try:
            target = self.convert()
        except Exception as exc:
            show_error(self, "转换失败", str(exc))
            return
        show_success(self, "转换完成", str(target))

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))


def _same_path(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return str(first).casefold() == str(second).casefold()


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mono_font() -> QFont:
    font = QFont("Cascadia Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(10)
    return font

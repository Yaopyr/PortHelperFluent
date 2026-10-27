"""校验计算页：文本/HEX/文件输入，流式校验、结果表、大小端与源码预览。"""

from __future__ import annotations

import threading
from functools import partial
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QSplitter,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SegmentedWidget,
    TableWidget,
    TitleLabel,
    ToolButton,
)

from app.core.checksum import (
    RESULT_NAMES,
    ChecksumResult,
    compute_all,
    create_accumulators,
    format_value,
    source_text,
)
from app.core.encoding import encode_text, parse_hex
from app.core.logging_setup import get_logger
from app.core.settings import Settings
from app.services.paths import AppPaths
from app.ui.dialogs.custom_crc_dialog import CustomCrcDialog
from app.ui.feedback import show_error
from app.ui.qt_utils import safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.file_picker import FilePicker
from app.ui.widgets.status_chip import StatusChip

MODE_TEXT = "text"
MODE_HEX = "hex"
MODE_FILE = "file"
MODE_LABELS = ((MODE_TEXT, "文本"), (MODE_HEX, "HEX"), (MODE_FILE, "文件"))
MODE_INDEX = {MODE_TEXT: 0, MODE_HEX: 1, MODE_FILE: 2}
ENCODING_ITEMS = (("ANSI（系统）", "ansi"), ("GBK", "gbk"), ("UTF-8", "utf-8"))
RESULT_COLUMNS = ("算法", "HEX", "DEC", "BIN", "复制")
DEFAULT_SOURCE = "CRC-32/ISO-HDLC"
FILE_CHUNK = 1024 * 1024
AUTO_DELAY_MS = 250

INPUT_HINT = (
    "文本按所选编码转为字节；HEX 支持空格、逗号与 0x 前缀；"
    "文件按 1 MiB 分块流式读取，不整体载入内存。"
)
RESULT_HINT = "HEX/BIN 默认按大端显示；勾选小端只调整 16/32 位的字节排列，DEC 仍为数值本身。"
SOURCE_HINT = "展示本程序对应算法的独立 Python 实现，供移植参考，不是原 PortHelper 源码。"


class _TaskSignals(QObject):
    # 所有信号都携带代际 token：页面取消/关闭后旧任务的迟到信号会被忽略。
    progress = pyqtSignal(int, int, int)  # token, read, total
    finished = pyqtSignal(int, object)  # token, results
    failed = pyqtSignal(int, str)  # token, message
    canceled = pyqtSignal(int)  # token


class _FileChecksumTask(QRunnable):
    """在 QThreadPool 中分块读取文件并累加校验，支持协作式取消。"""

    def __init__(self, path: Path, token: int = 0, chunk_size: int = FILE_CHUNK) -> None:
        super().__init__()
        self.signals = _TaskSignals()
        self.path = Path(path)
        self.token = int(token)
        self.chunk_size = max(1, int(chunk_size))
        self.total_size = 0
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        if self._cancelled.is_set():
            self.signals.canceled.emit(self.token)
            return
        accumulators = create_accumulators()
        read = 0
        try:
            self.total_size = self.path.stat().st_size
            self.signals.progress.emit(self.token, 0, self.total_size)
            with self.path.open("rb") as handle:
                while True:
                    if self._cancelled.is_set():
                        self.signals.canceled.emit(self.token)
                        return
                    chunk = handle.read(self.chunk_size)
                    if not chunk:
                        break
                    for accumulator in accumulators:
                        accumulator.update(chunk)
                    read += len(chunk)
                    self.signals.progress.emit(self.token, read, self.total_size)
        except OSError as exc:
            self.signals.failed.emit(self.token, f"读取文件失败：{exc}")
            return
        except Exception as exc:  # 后台线程不能把异常抛给线程池
            self.signals.failed.emit(self.token, f"计算文件校验失败：{exc}")
            return
        if self._cancelled.is_set():
            self.signals.canceled.emit(self.token)
            return
        results = [
            ChecksumResult(accumulator.name, accumulator.result(), accumulator.width)
            for accumulator in accumulators
        ]
        self.signals.finished.emit(self.token, results)


class ChecksumPage(QWidget):
    """校验计算工作台；文本/HEX 在 GUI 线程计算，文件在后台线程分块计算。"""

    def __init__(
        self,
        settings: Settings,
        paths: AppPaths,
        parent: QWidget | None = None,
        **kwargs,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("checksum")
        self.settings = settings
        self.paths = paths
        self.extra_options = dict(kwargs)
        self._loading = True
        self._closing = False
        self._results: list[ChecksumResult] = []
        self._file_task: _FileChecksumTask | None = None
        self._file_token = 0
        self._crc_dialog: CustomCrcDialog | None = None
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(AUTO_DELAY_MS)
        self._auto_timer.timeout.connect(self._auto_compute)

        self._build_ui()
        self._restore_settings()
        self._update_source()
        self._loading = False

    # ---- 装配 ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(TitleLabel("校验计算", self))
        self.info_label = CaptionLabel("求和、XOR/BCC、奇偶校验、Intel HEX、TCP/IP 与 8 种 CRC 预设", self)
        title_box.addWidget(self.info_label)
        header.addLayout(title_box)
        header.addStretch(1)
        self.status_chip = StatusChip(self)
        self.status_chip.set_state("idle", "就绪")
        header.addWidget(self.status_chip)
        root.addLayout(header)

        input_card, input_layout = section_card("输入", INPUT_HINT)
        self.mode_switch = SegmentedWidget(input_card)
        for route, text in MODE_LABELS:
            self.mode_switch.addItem(route, text)
        self.mode_switch.setCurrentItem(MODE_TEXT)
        self.mode_switch.currentItemChanged.connect(self._on_mode_changed)
        input_layout.addWidget(self.mode_switch)

        self.mode_stack = QStackedWidget(input_card)
        self.mode_stack.addWidget(self._build_text_panel())
        self.mode_stack.addWidget(self._build_hex_panel())
        self.mode_stack.addWidget(self._build_file_panel())
        input_layout.addWidget(self.mode_stack)

        options = QHBoxLayout()
        options.setSpacing(12)
        self.invert_check = CheckBox("计算后取反", input_card)
        self.invert_check.setToolTip("每项结果按自身位宽执行 ~value & mask")
        self.invert_check.toggled.connect(self._render_results)
        options.addWidget(self.invert_check)
        self.little_endian_check = CheckBox("小端显示 16/32 位", input_card)
        self.little_endian_check.setToolTip("只影响 HEX 与 BIN 列的字节顺序；DEC 始终显示数值本身")
        self.little_endian_check.toggled.connect(self._render_results)
        options.addWidget(self.little_endian_check)
        options.addStretch(1)
        self.custom_crc_button = PushButton("自定义 CRC…", input_card)
        self.custom_crc_button.setToolTip(
            "按位宽、poly、init、xorout 与反射参数计算任意 8/16/32 位 CRC"
        )
        self.custom_crc_button.clicked.connect(self.open_custom_crc)
        options.addWidget(self.custom_crc_button)
        self.compute_button = PrimaryPushButton("计算", input_card)
        self.compute_button.setToolTip("立即计算当前输入；文本/HEX 变化时也会自动计算")
        self.compute_button.clicked.connect(self._compute_clicked)
        options.addWidget(self.compute_button)
        input_layout.addLayout(options)
        root.addWidget(input_card)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_results_card())
        splitter.addWidget(self._build_source_card())
        splitter.setSizes([380, 250])
        root.addWidget(splitter, 1)

    def _build_text_panel(self) -> QWidget:
        panel = QWidget(self.mode_stack)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.text_editor = PlainTextEdit(panel)
        self.text_editor.setPlaceholderText("输入要计算校验的文本，例如：123456789")
        self.text_editor.setFont(_mono_font())
        self.text_editor.setMinimumHeight(88)
        self.text_editor.setMaximumHeight(140)
        self.text_editor.textChanged.connect(self._schedule_auto)
        layout.addWidget(self.text_editor)

        encoding_row = QHBoxLayout()
        encoding_row.setSpacing(8)
        encoding_row.addWidget(BodyLabel("文本编码", panel))
        self.encoding_combo = ComboBox(panel)
        for label, codec in ENCODING_ITEMS:
            self.encoding_combo.addItem(label, userData=codec)
        self.encoding_combo.setToolTip(
            "ANSI 使用系统当前代码页（简体中文为 cp936），GBK 固定为 cp936"
        )
        self.encoding_combo.currentIndexChanged.connect(self._on_encoding_changed)
        encoding_row.addWidget(self.encoding_combo)
        encoding_row.addStretch(1)
        layout.addLayout(encoding_row)
        return panel

    def _build_hex_panel(self) -> QWidget:
        panel = QWidget(self.mode_stack)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.hex_editor = PlainTextEdit(panel)
        self.hex_editor.setPlaceholderText("输入 HEX 字节，例如：31 32 33 34 35 36 37 38 39")
        self.hex_editor.setFont(_mono_font())
        self.hex_editor.setMinimumHeight(88)
        self.hex_editor.setMaximumHeight(140)
        self.hex_editor.textChanged.connect(self._schedule_auto)
        layout.addWidget(self.hex_editor)
        hint = CaptionLabel("非法字符或奇数长度会提示错误，不会计算半截数据。", panel)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return panel

    def _build_file_panel(self) -> QWidget:
        panel = QWidget(self.mode_stack)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.file_picker = FilePicker(panel)
        self.file_picker.pathChanged.connect(self._on_file_path_changed)
        layout.addWidget(self.file_picker)
        progress_row = QHBoxLayout()
        progress_row.setSpacing(8)
        self.progress_bar = ProgressBar(panel)
        self.progress_bar.setValue(0)
        progress_row.addWidget(self.progress_bar, 1)
        self.cancel_button = PushButton("取消", panel)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_file_compute)
        progress_row.addWidget(self.cancel_button)
        layout.addLayout(progress_row)
        self.file_info_label = CaptionLabel("未选择文件；选择后会自动按块计算。", panel)
        self.file_info_label.setWordWrap(True)
        layout.addWidget(self.file_info_label)
        return panel

    def _build_results_card(self) -> QWidget:
        card, layout = section_card("计算结果", RESULT_HINT)
        self.results_table = TableWidget(card)
        self.results_table.setColumnCount(len(RESULT_COLUMNS))
        self.results_table.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.verticalHeader().setDefaultSectionSize(36)
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.results_table.setColumnWidth(4, 56)
        layout.addWidget(self.results_table, 1)
        self.result_summary_label = CaptionLabel("尚未计算；输入内容后自动更新。", card)
        self.result_summary_label.setWordWrap(True)
        layout.addWidget(self.result_summary_label)
        return card

    def _build_source_card(self) -> QWidget:
        card, layout = section_card("源码预览", SOURCE_HINT)
        row = QHBoxLayout()
        row.addWidget(BodyLabel("算法", card))
        self.source_combo = ComboBox(card)
        for name in RESULT_NAMES:
            self.source_combo.addItem(name, userData=name)
        index = self.source_combo.findData(DEFAULT_SOURCE)
        if index >= 0:
            self.source_combo.setCurrentIndex(index)
        row.addWidget(self.source_combo, 1)
        layout.addLayout(row)

        self.source_view = PlainTextEdit(card)
        self.source_view.setReadOnly(True)
        self.source_view.setFont(_mono_font())
        self.source_view.setPlaceholderText("选择算法后显示对应的 Python 实现")
        self.source_view.setMinimumHeight(150)
        layout.addWidget(self.source_view, 1)
        self.source_combo.currentIndexChanged.connect(self._update_source)
        return card

    def _restore_settings(self) -> None:
        saved = str(self.settings.get("checksum.encoding", "utf-8"))
        index = self.encoding_combo.findData(saved)
        if index < 0:
            index = self.encoding_combo.findData("utf-8")
        blocked = self.encoding_combo.blockSignals(True)
        try:
            self.encoding_combo.setCurrentIndex(max(0, index))
        finally:
            self.encoding_combo.blockSignals(blocked)

    def _save_encoding(self) -> None:
        if self._loading:
            return
        try:
            self.settings.set("checksum.encoding", self._encoding())
            self.settings.save()
        except Exception:
            get_logger().exception("保存校验页编码设置失败")

    # ---- 输入与计算 ---------------------------------------------------

    def mode(self) -> str:
        """当前输入模式：text / hex / file。"""
        route = self.mode_switch.currentRouteKey()
        return route if route in MODE_INDEX else MODE_TEXT

    def set_mode(self, route: str) -> None:
        """切换输入模式（测试与快捷键可复用）。"""
        if route not in MODE_INDEX:
            raise ValueError(f"未知输入模式：{route}")
        self.mode_switch.setCurrentItem(route)
        self._on_mode_changed(route)

    def _encoding(self) -> str:
        codec = self.encoding_combo.currentData()
        return str(codec) if codec else "utf-8"

    @safe_slot
    def _on_mode_changed(self, route: str, *_args) -> None:
        self.mode_stack.setCurrentIndex(MODE_INDEX.get(str(route), 0))
        if str(route) != MODE_FILE:
            self._cancel_file_compute(silent=True)
            self._schedule_auto()
        elif not self._loading:
            self._schedule_auto()

    @safe_slot
    def _on_encoding_changed(self, *_args) -> None:
        self._save_encoding()
        if self.mode() == MODE_TEXT:
            self._schedule_auto()

    @safe_slot
    def _schedule_auto(self, *_args) -> None:
        if self._loading or self._closing:
            return
        self._auto_timer.start()

    @safe_slot
    def _auto_compute(self) -> None:
        if self.mode() == MODE_FILE:
            self._start_file_compute(notify=False)
        else:
            self._compute_now(notify=False)

    @safe_slot
    def _compute_clicked(self, *_args) -> None:
        self._auto_timer.stop()
        if self.mode() == MODE_FILE:
            self._start_file_compute(notify=True)
        else:
            self._compute_now(notify=True)

    def _read_input(self) -> bytes:
        mode = self.mode()
        if mode == MODE_HEX:
            return parse_hex(self.hex_editor.toPlainText())
        if mode == MODE_TEXT:
            return encode_text(self.text_editor.toPlainText(), self._encoding())
        raise ValueError("文件模式请使用文件计算。")

    def _compute_now(self, *, notify: bool) -> None:
        try:
            data = self._read_input()
        except UnicodeError as exc:
            self._handle_input_error(
                f"文本无法按“{self.encoding_combo.currentText()}”编码：{exc}", notify
            )
            return
        except ValueError as exc:
            self._handle_input_error(str(exc), notify)
            return
        except Exception as exc:
            get_logger().exception("读取校验输入失败")
            self._handle_input_error(f"读取输入失败：{exc}", notify)
            return
        self._apply_results(compute_all(data), len(data))

    def _handle_input_error(self, message: str, notify: bool) -> None:
        self._clear_results(message)
        self.status_chip.set_state("error", "输入有误")
        if notify:
            show_error(self, "校验输入有误", message)

    # ---- 结果展示 -----------------------------------------------------

    def _apply_results(self, results: list[ChecksumResult], byte_count: int) -> None:
        self._results = list(results)
        self._render_results()
        self.result_summary_label.setText(
            f"共 {len(self._results)} 项 · 输入 {_format_bytes(byte_count)} · "
            "勾选“计算后取反”可按位取反"
        )
        self.status_chip.set_state("ok", f"计算完成 · {_format_bytes(byte_count)}")

    def _clear_results(self, message: str) -> None:
        self._results = []
        self._render_results()
        self.result_summary_label.setText(message)

    def _result_values(self, result: ChecksumResult) -> tuple[int, int]:
        """返回 (数值, 显示值)：取反后若为小端，显示值再按字节反转。"""
        value = result.value
        if self.invert_check.isChecked():
            value = ~value & ((1 << result.width) - 1)
        display = value
        if self.little_endian_check.isChecked() and result.width > 8:
            size = result.width // 8
            display = int.from_bytes(value.to_bytes(size, "big"), "little")
        return value, display

    @safe_slot
    def _render_results(self, *_args) -> None:
        table = self.results_table
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            table.setRowCount(0)
            table.setRowCount(len(self._results))
            for row, result in enumerate(self._results):
                value, display = self._result_values(result)
                cells = (
                    result.name,
                    format_value(display, result.width, 16),
                    format_value(value, result.width, 10),
                    format_value(display, result.width, 2),
                )
                for column, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    if column:
                        item.setTextAlignment(
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                        )
                    item.setToolTip(f"{result.name} · {result.width} 位")
                    table.setItem(row, column, item)
                button = ToolButton(table)
                button.setIcon(FluentIcon.COPY)
                button.setToolTip(f"复制 {result.name} 的 HEX 值")
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                button.clicked.connect(partial(self._copy_value, result))
                table.setCellWidget(row, 4, button)
        finally:
            table.blockSignals(False)
            table.setUpdatesEnabled(True)

    @safe_slot
    def _copy_value(self, result: ChecksumResult, *_args) -> None:
        _value, display = self._result_values(result)
        text = format_value(display, result.width, 16)
        try:
            QApplication.clipboard().setText(text)
        except Exception:
            get_logger().exception("复制校验结果失败")
            return
        self.status_chip.set_state("ok", f"已复制 {result.name}")

    @safe_slot
    def _update_source(self, *_args) -> None:
        name = str(self.source_combo.currentData() or self.source_combo.currentText())
        if not name:
            self.source_view.clear()
            return
        try:
            self.source_view.setPlainText(source_text(name))
        except KeyError:
            self.source_view.setPlainText(f"# 没有算法“{name}”的源码示例")

    # ---- 自定义 CRC ---------------------------------------------------

    @safe_slot
    def open_custom_crc(self, *_args) -> CustomCrcDialog:
        """打开（或复用）非模态的自定义 CRC 对话框，避免阻塞主窗口与测试。"""
        dialog = self._crc_dialog
        if dialog is None:
            dialog = CustomCrcDialog(self)
            self._crc_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    # ---- 文件模式 -----------------------------------------------------

    @safe_slot
    def _on_file_path_changed(self, _path: str) -> None:
        if self._loading or self._closing:
            return
        path_text = self.file_picker.path()
        if not path_text:
            self._auto_timer.stop()
            self.progress_bar.setValue(0)
            self.file_info_label.setText("未选择文件；选择后会自动按块计算。")
            self._clear_results("未选择文件")
            self.status_chip.set_state("idle", "就绪")
            return
        path = Path(path_text)
        size = _file_size(path)
        if size is None:
            self.file_info_label.setText(f"{path.name}：文件不存在或不可访问")
        else:
            self.file_info_label.setText(f"{path.name}：{_format_bytes(size)}，准备计算…")
        self._schedule_auto()

    def _start_file_compute(self, *, notify: bool) -> None:
        self._cancel_file_compute(silent=True)
        path_text = self.file_picker.path()
        if not path_text:
            if notify:
                show_error(self, "未选择文件", "请选择要计算校验的文件。")
            else:
                self.status_chip.set_state("idle", "就绪")
            return
        path = Path(path_text)
        if not path.is_file():
            self._clear_results("文件不存在或不可访问")
            self.status_chip.set_state("error", "文件不可用")
            if notify:
                show_error(
                    self,
                    "无法读取文件",
                    f"找不到文件：{path}\n请确认路径是否正确、文件是否已被移动。",
                )
            return
        task = _FileChecksumTask(path, self._bump_file_token())
        self._file_task = task
        task.signals.progress.connect(self._on_file_progress)
        task.signals.finished.connect(self._on_file_finished)
        task.signals.failed.connect(self._on_file_failed)
        self._clear_results(f"正在读取 {path.name}…")
        self.progress_bar.setValue(0)
        self.cancel_button.setEnabled(True)
        self.compute_button.setEnabled(False)
        self.mode_switch.setEnabled(False)
        self.file_picker.setEnabled(False)
        self.status_chip.set_state("busy", "正在读取文件…")
        self._pool.start(task)

    def _bump_file_token(self) -> int:
        """代际 token：取消/完成后递增，迟到信号因 token 不匹配而被忽略。"""
        self._file_token += 1
        return self._file_token

    @safe_slot
    def _on_file_progress(self, token: int, read: int, total: int) -> None:
        task = self._file_task
        if task is None or token != self._file_token or self._closing:
            return
        percent = 100 if total <= 0 else min(100, max(0, read * 100 // total))
        self.progress_bar.setValue(percent)
        self.file_info_label.setText(
            f"{task.path.name}：{_format_bytes(read)} / {_format_bytes(total)}"
        )

    @safe_slot
    def _on_file_finished(self, token: int, results: object) -> None:
        task = self._file_task
        if task is None or token != self._file_token or self._closing:
            return
        self._bump_file_token()
        self._file_task = None
        self._reset_file_ui()
        self.progress_bar.setValue(100)
        items = [item for item in results if isinstance(item, ChecksumResult)]
        self.file_info_label.setText(
            f"{task.path.name}：{_format_bytes(task.total_size)} 已计算"
        )
        self._apply_results(items, task.total_size)

    @safe_slot
    def _on_file_failed(self, token: int, message: str) -> None:
        task = self._file_task
        if task is None or token != self._file_token or self._closing:
            return
        self._bump_file_token()
        self._file_task = None
        self._reset_file_ui()
        self.progress_bar.setValue(0)
        self.file_info_label.setText(f"{task.path.name}：读取失败")
        self._clear_results("文件读取失败")
        self.status_chip.set_state("error", "文件错误")
        show_error(self, "文件校验失败", str(message))

    @safe_slot
    def _cancel_file_compute(self, *_args, silent: bool = False) -> None:
        task = self._file_task
        if task is None:
            return
        task.cancel()
        self._bump_file_token()
        self._file_task = None
        self._reset_file_ui()
        if not silent:
            self.progress_bar.setValue(0)
            self.file_info_label.setText("已取消文件计算。")
            self._clear_results("已取消文件计算")
            self.status_chip.set_state("idle", "已取消")

    def _reset_file_ui(self) -> None:
        self.cancel_button.setEnabled(False)
        self.compute_button.setEnabled(True)
        self.mode_switch.setEnabled(True)
        self.file_picker.setEnabled(True)

    # ---- 生命周期 -----------------------------------------------------

    def has_active_work(self) -> bool:
        return self._file_task is not None

    def stop_workers(self) -> None:
        self._closing = True
        self._auto_timer.stop()
        task = self._file_task
        if task is not None:
            task.cancel()
            self._bump_file_token()
            self._file_task = None
            self._reset_file_ui()
            self.progress_bar.setValue(0)
            self.status_chip.set_state("idle", "已取消")
        try:
            self._pool.waitForDone(3000)
        except Exception:
            get_logger().exception("停止校验后台任务失败")

    def closeEvent(self, event) -> None:
        self._save_encoding()
        self.stop_workers()
        if self._crc_dialog is not None:
            self._crc_dialog.close()
        super().closeEvent(event)


def _mono_font() -> QFont:
    font = QFont("Cascadia Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(10)
    return font


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.2f} MB"

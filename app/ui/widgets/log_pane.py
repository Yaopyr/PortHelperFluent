"""收发日志面板：批量渲染、暂停/自动滚动、HEX/时间戳与导出。"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CheckBox, FluentIcon, SubtitleLabel, SwitchButton, ToolButton

from app.core.encoding import decode_bytes, to_hex
from app.core.settings import DEFAULT_SETTINGS
from app.services.session_log import LogRecord, SessionLog
from app.ui.feedback import show_success
from app.ui.qt_utils import safe_slot

BATCH_INTERVAL_MS = 33
RERENDER_LIMIT = 2000
PENDING_LIMIT = 20_000


class LogPane(QWidget):
    """会话记录的内存模型 + 批量渲染视图；暂停只停渲染，不停记录与计数。"""

    def __init__(
        self,
        title: str,
        *,
        show_direction: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._show_direction = show_direction
        self._session = SessionLog()
        self._pending: deque[LogRecord] = deque(maxlen=PENDING_LIMIT)
        self._hex_mode = False
        self._timestamp_visible = True
        self._codec = "utf-8"
        self._paused = False
        self._auto_scroll = True

        self._build_ui(title)
        self._timer = QTimer(self)
        self._timer.setInterval(BATCH_INTERVAL_MS)
        self._timer.timeout.connect(self._flush_pending)
        self._timer.start()

    # ---- 公开 API -----------------------------------------------------

    def append_bytes(self, data: bytes, direction: str, source: str = "") -> None:
        if not data:
            return
        record = self._session.add_bytes(bytes(data), direction, source)
        self._pending.append(record)

    def append_info(self, text: str) -> None:
        if not text:
            return
        record = self._session.add_bytes(text.encode("utf-8"), "info")
        self._pending.append(record)

    def set_hex_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._hex_mode:
            return
        self._hex_mode = enabled
        self._set_checked(self.hex_check, enabled)
        self._rerender()

    def set_timestamp_visible(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._timestamp_visible:
            return
        self._timestamp_visible = enabled
        self._set_checked(self.timestamp_check, enabled)
        self._rerender()

    def set_encoding(self, codec: str) -> None:
        codec = (codec or "utf-8").strip() or "utf-8"
        if codec == self._codec:
            return
        self._codec = codec
        self._rerender()

    def set_paused(self, paused: bool) -> None:
        paused = bool(paused)
        if paused == self._paused:
            return
        self._paused = paused
        self._set_checked(self.pause_switch, paused)
        if not paused:
            self._flush_pending()

    def clear(self) -> None:
        self._session.clear()
        self._pending.clear()
        self.log_view.clear()

    def save_txt(self, path: str | Path) -> None:
        self._session.export_txt(Path(path), codec=self._codec, hex_mode=self._hex_mode)

    def save_csv(self, path: str | Path) -> None:
        self._session.export_csv(Path(path), codec=self._codec, hex_mode=self._hex_mode)

    def session(self) -> SessionLog:
        return self._session

    # ---- 界面 ---------------------------------------------------------

    def _build_ui(self, title: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        self.title_label = SubtitleLabel(title, self)
        header.addWidget(self.title_label)
        header.addStretch(1)

        self.hex_check = CheckBox("HEX", self)
        self.hex_check.setToolTip("以十六进制显示收发数据")
        self.hex_check.toggled.connect(self._on_hex_toggled)
        header.addWidget(self.hex_check)

        self.timestamp_check = CheckBox("时间戳", self)
        self.timestamp_check.setChecked(self._timestamp_visible)
        self.timestamp_check.setToolTip("在每行前显示接收时间")
        self.timestamp_check.toggled.connect(self._on_timestamp_toggled)
        header.addWidget(self.timestamp_check)

        self.autoscroll_check = CheckBox("自动滚动", self)
        self.autoscroll_check.setChecked(self._auto_scroll)
        self.autoscroll_check.setToolTip("有新数据时自动滚动到底部")
        self.autoscroll_check.toggled.connect(self._on_autoscroll_toggled)
        header.addWidget(self.autoscroll_check)

        self.pause_switch = SwitchButton(self)
        self.pause_switch.setText("暂停")
        self.pause_switch.setToolTip("暂停只停止刷新，仍会继续记录与计数")
        self.pause_switch.checkedChanged.connect(self._on_pause_toggled)
        header.addWidget(self.pause_switch)

        self.copy_button = self._tool_button(FluentIcon.COPY, "复制全部日志", self._copy)
        self.save_button = self._tool_button(FluentIcon.SAVE, "保存为 TXT 或 CSV", self._save)
        self.clear_button = self._tool_button(FluentIcon.DELETE, "清空显示与会话记录", self._clear_clicked)
        for button in (self.copy_button, self.save_button, self.clear_button):
            header.addWidget(button)

        root.addLayout(header)

        self.log_view = QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setUndoRedoEnabled(False)
        self.log_view.setPlaceholderText("暂无数据")
        self.log_view.setMaximumBlockCount(int(DEFAULT_SETTINGS["ui.max_display_lines"]))
        # 日志区不抢发送框的键盘焦点。
        self.log_view.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.log_view.setFont(font)
        root.addWidget(self.log_view, 1)

    def _tool_button(self, icon, tooltip: str, slot) -> ToolButton:
        button = ToolButton(self)
        button.setIcon(icon)
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(slot)
        return button

    # ---- 渲染 ---------------------------------------------------------

    @safe_slot
    def _flush_pending(self) -> None:
        if self._paused or not self._pending:
            return
        lines = [self._format_record(self._pending.popleft()) for _ in range(len(self._pending))]
        if not lines:
            return
        self.log_view.appendPlainText("\n".join(lines))
        self._scroll_to_bottom()

    @safe_slot
    def _rerender(self) -> None:
        """按当前 HEX/时间戳/编码设置重绘最近记录（最多 2000 条）。"""
        self._pending.clear()
        records = self._session.records()[-RERENDER_LIMIT:]
        self.log_view.clear()
        if not records:
            return
        self.log_view.setPlainText("\n".join(self._format_record(record) for record in records))
        self._scroll_to_bottom()

    def _format_record(self, record: LogRecord) -> str:
        parts: list[str] = []
        if self._timestamp_visible:
            stamp = f"{record.timestamp:%H:%M:%S}.{record.timestamp.microsecond // 1000:03d}"
            parts.append(f"[{stamp}]")
        if self._show_direction:
            parts.append(f"[{_direction_text(record.direction)}]")
        if record.source:
            parts.append(f"{record.source}:")
        parts.append(self._payload_text(record))
        return " ".join(parts)

    def _payload_text(self, record: LogRecord) -> str:
        if record.direction == "info":
            # 信息行始终按文本显示，避免被 HEX 模式变成字节串。
            return record.data.decode("utf-8", errors="replace")
        if self._hex_mode:
            return to_hex(record.data)
        try:
            return decode_bytes(record.data, self._codec)
        except Exception:
            return record.data.decode("utf-8", errors="replace")

    def _scroll_to_bottom(self) -> None:
        if not self._auto_scroll:
            return
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ---- 槽 -----------------------------------------------------------

    @safe_slot
    def _on_hex_toggled(self, checked: bool) -> None:
        self.set_hex_mode(checked)

    @safe_slot
    def _on_timestamp_toggled(self, checked: bool) -> None:
        self.set_timestamp_visible(checked)

    @safe_slot
    def _on_autoscroll_toggled(self, checked: bool) -> None:
        self._auto_scroll = bool(checked)
        if checked:
            self._scroll_to_bottom()

    @safe_slot
    def _on_pause_toggled(self, checked: bool) -> None:
        self.set_paused(checked)

    @safe_slot
    def _copy(self) -> None:
        QApplication.clipboard().setText(self.log_view.toPlainText())

    @safe_slot
    def _save(self) -> None:
        default_name = f"session-{datetime.now():%Y%m%d-%H%M%S}.txt"
        path_text, _selected = QFileDialog.getSaveFileName(
            self,
            "保存会话日志",
            default_name,
            "文本文件 (*.txt);;CSV 文件 (*.csv)",
        )
        if not path_text:
            return
        path = Path(path_text)
        if path.suffix.lower() == ".csv":
            self.save_csv(path)
        else:
            if path.suffix.lower() != ".txt":
                path = path.with_suffix(".txt")
            self.save_txt(path)
        show_success(self, "日志已保存", str(path))

    @safe_slot
    def _clear_clicked(self) -> None:
        self.clear()

    @staticmethod
    def _set_checked(widget, checked: bool) -> None:
        """程序化更新开关，避免再次触发切换槽。"""
        blocked = widget.blockSignals(True)
        try:
            widget.setChecked(checked)
        finally:
            widget.blockSignals(blocked)


def _direction_text(direction: str) -> str:
    return {"rx": "RX", "tx": "TX", "info": "信息"}.get(direction, direction.upper())

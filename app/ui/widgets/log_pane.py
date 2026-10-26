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
from app.core.settings import DEFAULT_SETTINGS, Settings
from app.services.session_log import LogRecord, SessionLog, direction_label
from app.ui.feedback import show_success
from app.ui.qt_utils import safe_slot

BATCH_INTERVAL_MS = 33
RERENDER_LIMIT = 2000
# 每次 33 ms 刷新最多写入的记录数与字节数；剩余的留到下一个 tick，避免长会话卡顿。
FLUSH_MAX_RECORDS = 2000
FLUSH_MAX_BYTES = 256 * 1024
PENDING_LIMIT = 20_000
MIN_DISPLAY_LINES = 1000
MAX_DISPLAY_LINES = 100_000


class LogPane(QWidget):
    """会话记录的内存模型 + 批量渲染视图；暂停只停渲染，不停记录与计数。

    用户设置接入（Task 3 页面直接使用）::

        pane = LogPane("RX", settings=settings)       # 读取 ui.max_display_lines / ui.auto_scroll
        pane = LogPane("RX", max_display_lines=5000, auto_scroll=False)  # 显式值优先
        pane.configure(max_display_lines=8000, auto_scroll=True)         # 运行期更新

    显示行数限制在 1000–100000；只有未提供的参数才回退到设置或默认值。
    会话记录本身不受显示行数限制，导出始终使用完整会话。
    """

    def __init__(
        self,
        title: str,
        *,
        show_direction: bool = True,
        max_display_lines: int | None = None,
        auto_scroll: bool | None = None,
        settings: Settings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._show_direction = show_direction
        self._session = SessionLog()
        self._pending: deque[LogRecord] = deque()
        self._pending_limit = PENDING_LIMIT
        self._display_dropped = 0
        self._hex_mode = False
        self._timestamp_visible = True
        self._codec = "utf-8"
        self._paused = False
        self._max_display_lines = _resolve_display_lines(settings, max_display_lines)
        self._auto_scroll = _resolve_auto_scroll(settings, auto_scroll)

        self._build_ui(title)
        self._timer = QTimer(self)
        self._timer.setInterval(BATCH_INTERVAL_MS)
        self._timer.timeout.connect(self._flush_pending)
        self._timer.start()

    # ---- 公开 API -----------------------------------------------------

    @property
    def display_dropped(self) -> int:
        """因待显示队列超过上限而被丢弃的最旧记录数（会话记录仍完整保留）。"""
        return self._display_dropped

    def max_display_lines(self) -> int:
        """当前生效的显示行数上限（1000–100000）。"""
        return self._max_display_lines

    def auto_scroll(self) -> bool:
        """有新数据时是否自动滚动到底部。"""
        return self._auto_scroll

    def configure(
        self,
        *,
        max_display_lines: int | None = None,
        auto_scroll: bool | None = None,
    ) -> None:
        """应用用户设置；参数为 None 时保持当前值，行数超界时自动收敛。"""
        if max_display_lines is not None:
            self._max_display_lines = _clamp_display_lines(
                max_display_lines, self._max_display_lines
            )
            self.log_view.setMaximumBlockCount(self._max_display_lines)
        if auto_scroll is not None:
            self._auto_scroll = bool(auto_scroll)
            self._set_checked(self.autoscroll_check, self._auto_scroll)
            if self._auto_scroll:
                self._scroll_to_bottom()

    def append_bytes(self, data: bytes, direction: str, source: str = "") -> None:
        if not data:
            return
        record = self._session.add_bytes(bytes(data), direction, source)
        self._enqueue(record)

    def append_info(self, text: str) -> None:
        if not text:
            return
        record = self._session.add_bytes(text.encode("utf-8"), "info")
        self._enqueue(record)

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

        self.copy_button = self._tool_button(FluentIcon.COPY, "复制当前显示", self._copy)
        self.save_button = self._tool_button(FluentIcon.SAVE, "保存为 TXT 或 CSV", self._save)
        self.clear_button = self._tool_button(FluentIcon.DELETE, "清空显示与会话记录", self._clear_clicked)
        for button in (self.copy_button, self.save_button, self.clear_button):
            header.addWidget(button)

        root.addLayout(header)

        self.log_view = QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setUndoRedoEnabled(False)
        self.log_view.setPlaceholderText("暂无数据")
        self.log_view.setMaximumBlockCount(self._max_display_lines)
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

    def _enqueue(self, record: LogRecord) -> None:
        """加入待显示队列；超限时丢弃最旧待显示记录并计数（会话仍保留全部）。"""
        self._pending.append(record)
        while len(self._pending) > self._pending_limit:
            self._pending.popleft()
            self._display_dropped += 1

    @safe_slot
    def _flush_pending(self) -> None:
        if self._paused or not self._pending:
            return
        records: list[LogRecord] = []
        total_bytes = 0
        while self._pending:
            record = self._pending[0]
            limit_reached = len(records) >= FLUSH_MAX_RECORDS or (
                total_bytes + len(record.data) > FLUSH_MAX_BYTES
            )
            # 单条记录再大也要写出去，否则队列会永远卡在第一条。
            if records and limit_reached:
                break
            self._pending.popleft()
            records.append(record)
            total_bytes += len(record.data)
        if not records:
            return
        text = "\n".join(self._format_record(record) for record in records)
        if self._auto_scroll:
            self.log_view.appendPlainText(text)
            self._scroll_to_bottom()
            return
        # appendPlainText 会把光标移到末尾并跟随滚动；关闭自动滚动时恢复原位置。
        scroll_bar = self.log_view.verticalScrollBar()
        previous = scroll_bar.value()
        self.log_view.appendPlainText(text)
        scroll_bar.setValue(min(previous, scroll_bar.maximum()))

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
            parts.append(f"[{direction_label(record.direction)}]")
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
        """复制当前显示内容（不是完整会话）。"""
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

    # ---- 生命周期 -----------------------------------------------------

    def closeEvent(self, event) -> None:
        """关闭时停止批量渲染定时器，避免窗口销毁后仍有回调。"""
        self._timer.stop()
        super().closeEvent(event)


def _resolve_display_lines(settings: Settings | None, explicit: int | None) -> int:
    """显式参数 > Settings > 默认值；结果始终收敛在 1000–100000。"""
    default = int(DEFAULT_SETTINGS["ui.max_display_lines"])
    if explicit is not None:
        return _clamp_display_lines(explicit, default)
    configured = _settings_value(settings, "ui.max_display_lines")
    if configured is not None:
        return _clamp_display_lines(configured, default)
    return _clamp_display_lines(default, default)


def _resolve_auto_scroll(settings: Settings | None, explicit: bool | None) -> bool:
    """显式参数 > Settings > 默认值。"""
    if explicit is not None:
        return bool(explicit)
    configured = _settings_value(settings, "ui.auto_scroll")
    if configured is not None:
        return bool(configured)
    return bool(DEFAULT_SETTINGS["ui.auto_scroll"])


def _settings_value(settings: Settings | None, key: str):
    if settings is None:
        return None
    try:
        return settings.get(key)
    except Exception:
        return None


def _clamp_display_lines(value, fallback: int) -> int:
    try:
        lines = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(MIN_DISPLAY_LINES, min(MAX_DISPLAY_LINES, lines))

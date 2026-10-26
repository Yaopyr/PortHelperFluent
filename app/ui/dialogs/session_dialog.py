"""会话记录对话框：表格快照 + 导出 CSV/TXT。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CaptionLabel, PrimaryPushButton, PushButton

from app.core.encoding import decode_bytes, to_hex
from app.services.session_log import SessionLog
from app.ui.feedback import show_success
from app.ui.qt_utils import safe_slot

ROW_CAP = 5000
_COLUMNS = ("时间", "方向", "来源", "长度", "HEX", "文本")


class SessionDialog(QDialog):
    """展示 `SessionLog` 的最近快照；导出始终包含完整记录。"""

    def __init__(
        self,
        session: SessionLog,
        parent: QWidget | None = None,
        *,
        codec: str = "utf-8",
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._codec = codec or "utf-8"

        self.setObjectName("sessionDialog")
        self.setWindowTitle("会话记录")
        self.resize(960, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        records = session.records()
        shown = records[-ROW_CAP:]
        self.cap_label = CaptionLabel(self._cap_text(len(records), len(shown)), self)
        layout.addWidget(self.cap_label)

        self.table = QTableWidget(len(shown), len(_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setUpdatesEnabled(False)
        try:
            self._fill_table(shown)
        finally:
            self.table.setUpdatesEnabled(True)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.export_csv_button = PushButton("导出 CSV", self)
        self.export_csv_button.clicked.connect(self._export_csv)
        buttons.addWidget(self.export_csv_button)
        self.export_txt_button = PushButton("导出 TXT", self)
        self.export_txt_button.clicked.connect(self._export_txt)
        buttons.addWidget(self.export_txt_button)
        self.close_button = PrimaryPushButton("关闭", self)
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def _cap_text(self, total: int, shown: int) -> str:
        if total > shown:
            return (
                f"共 {total} 条记录，表格显示最近 {shown} 条（上限 {ROW_CAP} 条）；"
                "导出文件包含全部记录。"
            )
        return f"共 {total} 条记录，导出文件包含全部记录。"

    def _fill_table(self, records) -> None:
        for row, record in enumerate(records):
            values = (
                _format_time(record),
                {"rx": "RX", "tx": "TX", "info": "信息"}.get(
                    record.direction, record.direction.upper()
                ),
                record.source,
                str(len(record.data)),
                to_hex(record.data),
                self._record_text(record),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 3:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row, column, item)

    def _record_text(self, record) -> str:
        if record.direction == "info":
            return record.data.decode("utf-8", errors="replace")
        try:
            return decode_bytes(record.data, self._codec)
        except Exception:
            return record.data.decode("utf-8", errors="replace")

    @safe_slot
    def _export_csv(self) -> None:
        path = self._ask_path("CSV 文件 (*.csv)", ".csv")
        if path is None:
            return
        self._session.export_csv(path, codec=self._codec)
        show_success(self, "已导出", str(path))

    @safe_slot
    def _export_txt(self) -> None:
        path = self._ask_path("文本文件 (*.txt)", ".txt")
        if path is None:
            return
        self._session.export_txt(path, codec=self._codec)
        show_success(self, "已导出", str(path))

    def _ask_path(self, filter_text: str, suffix: str) -> Path | None:
        default_name = f"session-{datetime.now():%Y%m%d-%H%M%S}{suffix}"
        path_text, _selected = QFileDialog.getSaveFileName(
            self, "导出会话记录", default_name, filter_text
        )
        if not path_text:
            return None
        path = Path(path_text)
        if path.suffix.lower() != suffix:
            path = path.with_suffix(suffix)
        return path


def _format_time(record) -> str:
    timestamp = record.timestamp
    return f"{timestamp:%Y-%m-%d %H:%M:%S}.{timestamp.microsecond // 1000:03d}"

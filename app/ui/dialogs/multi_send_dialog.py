"""多项发送对话框：8 个独立槽位，支持 HEX、换行与循环发送。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
)

from app.core.logging_setup import get_logger
from app.ui.feedback import show_error, show_success
from app.ui.qt_utils import safe_slot
from app.services.exports import write_text_atomic

SLOT_COUNT = 8
MIN_INTERVAL_MS = 10
MAX_INTERVAL_MS = 60000


@dataclass
class MultiSendSlot:
    enabled: bool = True
    hex_mode: bool = False
    text: str = ""
    newline: bool = False
    loop: bool = False
    interval_ms: int = 1000


class MultiSendDialog(QDialog):
    """8 槽多项发送；发送动作由页面注入，对话框不直接持有串口 worker。"""

    def __init__(
        self,
        send_callback: Callable[[str, bool, bool], bool],
        parent: QWidget | None = None,
        *,
        slots: list[MultiSendSlot] | None = None,
    ) -> None:
        super().__init__(parent)
        self._send_callback = send_callback
        self._timers: list[QTimer] = []
        self._build_ui()
        self.from_slots(slots or [MultiSendSlot() for _ in range(SLOT_COUNT)])

    # ---- UI -----------------------------------------------------------

    def _build_ui(self) -> None:
        self.setObjectName("multiSendDialog")
        self.setWindowTitle("多项发送")
        self.resize(980, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)
        root.addWidget(
            CaptionLabel(
                "每行可独立发送；勾选“循环”后按设定间隔重复发送。HEX 文本在发送前校验。",
                self,
            )
        )

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        headers = ("启用", "HEX", "数据", "发送", "循环", "间隔 (ms)", "换行")
        for column, text in enumerate(headers):
            grid.addWidget(CaptionLabel(text, self), 0, column)

        self.enabled_checks: list[CheckBox] = []
        self.hex_checks: list[CheckBox] = []
        self.text_edits: list[LineEdit] = []
        self.send_buttons: list[PushButton] = []
        self.loop_checks: list[CheckBox] = []
        self.intervals: list[SpinBox] = []
        self.newline_checks: list[CheckBox] = []

        for row in range(SLOT_COUNT):
            enabled = CheckBox(self)
            enabled.setChecked(True)
            self.enabled_checks.append(enabled)
            grid.addWidget(enabled, row + 1, 0)

            hex_check = CheckBox("HEX", self)
            self.hex_checks.append(hex_check)
            grid.addWidget(hex_check, row + 1, 1)

            edit = LineEdit(self)
            edit.setPlaceholderText(f"第 {row + 1} 项数据")
            self.text_edits.append(edit)
            grid.addWidget(edit, row + 1, 2)

            send = PushButton("发送", self)
            send.clicked.connect(lambda _checked=False, index=row: self._send_slot(index))
            self.send_buttons.append(send)
            grid.addWidget(send, row + 1, 3)

            loop = CheckBox("循环", self)
            loop.toggled.connect(
                lambda checked, index=row: self._on_loop_toggled(index, checked)
            )
            self.loop_checks.append(loop)
            grid.addWidget(loop, row + 1, 4)

            interval = SpinBox(self)
            interval.setRange(MIN_INTERVAL_MS, MAX_INTERVAL_MS)
            interval.setValue(1000)
            interval.setSuffix(" ms")
            self.intervals.append(interval)
            grid.addWidget(interval, row + 1, 5)

            newline = CheckBox("换行", self)
            self.newline_checks.append(newline)
            grid.addWidget(newline, row + 1, 6)

            timer = QTimer(self)
            timer.timeout.connect(lambda index=row: self._send_slot(index))
            self._timers.append(timer)

        root.addLayout(grid)
        root.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.send_all_button = PrimaryPushButton("发送全部启用项", self)
        self.send_all_button.clicked.connect(self._send_all)
        buttons.addWidget(self.send_all_button)
        self.stop_button = PushButton("停止全部循环", self)
        self.stop_button.clicked.connect(self.stop_all_loops)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        self.save_button = PushButton("保存数据", self)
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        self.load_button = PushButton("载入数据", self)
        self.load_button.clicked.connect(self._load)
        buttons.addWidget(self.load_button)
        self.close_button = PushButton("关闭", self)
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        root.addLayout(buttons)

    # ---- 数据 ---------------------------------------------------------

    def slots(self) -> list[MultiSendSlot]:
        return [
            MultiSendSlot(
                enabled=self.enabled_checks[i].isChecked(),
                hex_mode=self.hex_checks[i].isChecked(),
                text=self.text_edits[i].text(),
                newline=self.newline_checks[i].isChecked(),
                loop=self.loop_checks[i].isChecked(),
                interval_ms=self.intervals[i].value(),
            )
            for i in range(SLOT_COUNT)
        ]

    def from_slots(self, slots: list[MultiSendSlot]) -> None:
        for index in range(SLOT_COUNT):
            value = slots[index] if index < len(slots) else MultiSendSlot()
            self.enabled_checks[index].setChecked(value.enabled)
            self.hex_checks[index].setChecked(value.hex_mode)
            self.text_edits[index].setText(value.text)
            self.newline_checks[index].setChecked(value.newline)
            self.loop_checks[index].setChecked(value.loop)
            self.intervals[index].setValue(
                max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, int(value.interval_ms)))
            )
            self._on_loop_toggled(index, value.loop)

    def to_dict(self) -> dict:
        return {"slots": [slot.__dict__ for slot in self.slots()]}

    def load_dict(self, data: dict) -> None:
        raw = data.get("slots", [])
        slots = []
        for item in raw[:SLOT_COUNT]:
            if not isinstance(item, dict):
                continue
            slots.append(
                MultiSendSlot(
                    enabled=bool(item.get("enabled", True)),
                    hex_mode=bool(item.get("hex_mode", False)),
                    text=str(item.get("text", "")),
                    newline=bool(item.get("newline", False)),
                    loop=bool(item.get("loop", False)),
                    interval_ms=int(item.get("interval_ms", 1000)),
                )
            )
        self.from_slots(slots)

    # ---- 发送 ---------------------------------------------------------

    @safe_slot
    def _send_slot(self, index: int) -> None:
        if not self.enabled_checks[index].isChecked():
            return
        try:
            ok = self._send_callback(
                self.text_edits[index].text(),
                self.hex_checks[index].isChecked(),
                self.newline_checks[index].isChecked(),
            )
        except ValueError as exc:
            self._stop_row(index)
            show_error(self, "发送失败", str(exc))
            return
        except Exception as exc:
            self._stop_row(index)
            get_logger().exception("多项发送失败")
            show_error(self, "发送失败", str(exc))
            return
        if not ok:
            self._stop_row(index)

    @safe_slot
    def _send_all(self) -> None:
        for index in range(SLOT_COUNT):
            if self.enabled_checks[index].isChecked():
                self._send_slot(index)

    @safe_slot
    def _on_loop_toggled(self, index: int, checked: bool) -> None:
        timer = self._timers[index]
        if checked:
            timer.start(self.intervals[index].value())
        else:
            timer.stop()

    def _stop_row(self, index: int) -> None:
        self.loop_checks[index].setChecked(False)
        self._timers[index].stop()

    @safe_slot
    def stop_all_loops(self) -> None:
        for index in range(SLOT_COUNT):
            self._stop_row(index)

    # ---- 文件 ---------------------------------------------------------

    @safe_slot
    def _save(self) -> None:
        path_text, _selected = QFileDialog.getSaveFileName(
            self, "保存多项发送数据", "multi-send.json", "JSON 文件 (*.json)"
        )
        if not path_text:
            return
        path = Path(path_text)
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        write_text_atomic(
            path,
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        show_success(self, "已保存", str(path))

    @safe_slot
    def _load(self) -> None:
        path_text, _selected = QFileDialog.getOpenFileName(
            self, "载入多项发送数据", "", "JSON 文件 (*.json)"
        )
        if not path_text:
            return
        try:
            data = json.loads(Path(path_text).read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("文件内容不是 JSON 对象")
            self.load_dict(data)
        except Exception as exc:
            get_logger().exception("载入多项发送数据失败")
            show_error(self, "载入失败", str(exc))
            return
        show_success(self, "已载入", path_text)

    def closeEvent(self, event) -> None:
        self.stop_all_loops()
        super().closeEvent(event)

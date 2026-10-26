"""共享控件的离屏测试：日志面板、文件选择、HEX 输入、状态胶囊与反馈。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QWidget
from qfluentwidgets import CardWidget, InfoBarPosition

from app.ui import feedback
from app.ui.qt_utils import process_events_until, readable_message, safe_slot
from app.ui.widgets.cards import section_card
from app.ui.widgets.empty_state import EmptyState
from app.ui.widgets.file_picker import FilePicker
from app.ui.widgets.hex_editor import HexLineEdit
from app.ui.widgets.log_pane import LogPane
from app.ui.widgets.status_chip import StatusChip


def _text(pane: LogPane) -> str:
    return pane.log_view.toPlainText()


def test_log_pane_renders_hex_and_counts_bytes(qapp) -> None:
    pane = LogPane("RX")
    pane.set_hex_mode(True)
    pane.set_timestamp_visible(False)

    pane.append_bytes(b"\x01\x02", "rx")

    assert process_events_until(lambda: "01 02" in _text(pane))
    assert "[RX]" in _text(pane)
    assert pane.session().total_bytes() == 2
    assert pane.session().total_bytes("rx") == 2
    pane.close()


def test_log_pane_pause_stops_rendering_but_keeps_recording(qapp) -> None:
    pane = LogPane("RX")
    pane.set_hex_mode(True)
    pane.set_timestamp_visible(False)
    pane.append_bytes(b"\x01", "rx")
    assert process_events_until(lambda: "01" in _text(pane))

    pane.set_paused(True)
    before = _text(pane)
    pane.append_bytes(b"\x02", "rx")
    for _ in range(5):
        qapp.processEvents()

    assert _text(pane) == before
    assert pane.session().total_bytes("rx") == 2

    pane.set_paused(False)
    assert process_events_until(lambda: "02" in _text(pane))
    pane.close()


def test_log_pane_clear_resets_display_and_records(qapp) -> None:
    pane = LogPane("RX")
    pane.append_bytes(b"\x41", "rx")
    assert process_events_until(lambda: _text(pane) != "")

    pane.clear()

    assert _text(pane) == ""
    assert pane.session().records() == ()
    pane.close()


def test_log_pane_info_line_does_not_count_as_rx_or_tx(qapp) -> None:
    pane = LogPane("RX")
    pane.set_timestamp_visible(False)

    pane.append_info("模拟提示")

    assert process_events_until(lambda: "模拟提示" in _text(pane))
    assert pane.session().total_bytes() == 0
    assert len(pane.session().records()) == 1
    pane.close()


def test_log_pane_hex_toggle_rerenders_recent_records(qapp) -> None:
    pane = LogPane("RX")
    pane.set_timestamp_visible(False)
    pane.append_bytes(b"A", "rx")
    assert process_events_until(lambda: _text(pane).endswith("A"))

    pane.set_hex_mode(True)

    assert process_events_until(lambda: "41" in _text(pane))
    assert not _text(pane).endswith("A")
    pane.close()


def test_log_pane_ignore_empty_payload(qapp) -> None:
    pane = LogPane("RX")
    pane.append_bytes(b"", "rx")
    assert pane.session().records() == ()
    pane.close()


def test_log_pane_exports_info_text_as_utf8(qapp, tmp_path: Path) -> None:
    pane = LogPane("RX")
    pane.set_encoding("gbk")
    pane.append_info("提示信息")

    txt_path = tmp_path / "log.txt"
    csv_path = tmp_path / "log.csv"
    pane.save_txt(txt_path)
    pane.save_csv(csv_path)

    assert "提示信息" in txt_path.read_text(encoding="utf-8")
    assert "提示信息" in csv_path.read_text(encoding="utf-8-sig")
    pane.close()


def test_file_picker_set_path_emits_signal(qapp, tmp_path: Path) -> None:
    picker = FilePicker()
    received: list[str] = []
    picker.pathChanged.connect(received.append)
    target = tmp_path / "data.bin"

    picker.set_path(target)

    assert picker.path() == str(target)
    assert received == [str(target)]
    picker.close()


def test_hex_editor_rejects_invalid_and_accepts_valid(qapp) -> None:
    editor = HexLineEdit()

    editor.setText("0G")
    assert editor.is_valid() is False
    assert editor.hex_bytes() == b""
    assert "非法字符" in editor.toolTip()
    assert editor.styleSheet()

    editor.setText("0A ff")
    assert editor.is_valid() is True
    assert editor.hex_bytes() == b"\x0a\xff"
    assert editor.styleSheet() == ""

    editor.set_hex_text("01 02")
    assert editor.hex_bytes() == b"\x01\x02"
    editor.close()


def test_status_chip_exposes_error_text(qapp) -> None:
    chip = StatusChip()

    chip.set_state("error", "失败")

    assert chip.state == "error"
    assert chip.text() == "失败"
    assert chip.label.text() == "失败"
    assert "失败" in chip.accessibleName()
    assert chip.minimumHeight() == chip.maximumHeight() == 28
    chip.close()


def test_section_card_contains_title_and_subtitle(qapp) -> None:
    card, layout = section_card("参数", "说明文字")

    assert isinstance(card, CardWidget)
    assert layout.count() == 2
    assert card.layout().contentsMargins().left() == 16
    card.close()


def test_empty_state_exposes_title_and_hint(qapp) -> None:
    state = EmptyState("暂无设备", "请连接设备后点击刷新")

    assert state.accessibleName() == "暂无设备"
    assert "刷新" in state.accessibleDescription()
    state.close()


def test_feedback_uses_info_bar_for_visible_parent(qapp, monkeypatch) -> None:
    parent = QWidget()
    parent.show()
    qapp.processEvents()
    calls: list[dict] = []

    class FakeInfoBar:
        @staticmethod
        def info(**kwargs) -> object:
            calls.append(kwargs)
            return object()

    monkeypatch.setattr(feedback, "InfoBar", FakeInfoBar)

    feedback.show_info(parent, "提示", "内容")

    assert len(calls) == 1
    assert calls[0]["title"] == "提示"
    assert calls[0]["content"] == "内容"
    assert calls[0]["duration"] == feedback.INFO_DURATION_MS
    assert calls[0]["position"] == InfoBarPosition.TOP_RIGHT
    assert calls[0]["parent"] is parent
    parent.close()


def test_feedback_error_uses_longer_duration(qapp, monkeypatch) -> None:
    parent = QWidget()
    parent.show()
    qapp.processEvents()
    calls: list[dict] = []

    class FakeInfoBar:
        @staticmethod
        def error(**kwargs) -> object:
            calls.append(kwargs)
            return object()

    monkeypatch.setattr(feedback, "InfoBar", FakeInfoBar)

    feedback.show_error(parent, "失败", "细节")

    assert calls[0]["duration"] == feedback.ERROR_DURATION_MS
    parent.close()


def test_feedback_info_is_noop_for_hidden_parent(qapp, monkeypatch) -> None:
    parent = QWidget()
    calls: list[dict] = []

    class FakeInfoBar:
        @staticmethod
        def info(**kwargs) -> object:
            calls.append(kwargs)
            return object()

    monkeypatch.setattr(feedback, "InfoBar", FakeInfoBar)

    feedback.show_info(parent, "提示", "不应出现")

    assert calls == []
    parent.close()


def test_safe_slot_catches_exception_and_reports(qapp, monkeypatch) -> None:
    reported: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        feedback,
        "show_error",
        lambda parent, title, content: reported.append((parent, title, content)),
    )

    @safe_slot
    def broken(widget: QWidget) -> None:
        raise FileNotFoundError("missing.txt")

    parent = QWidget()

    assert broken(parent) is None
    assert len(reported) == 1
    assert reported[0][0] is parent
    assert reported[0][1] == "操作失败"
    assert "找不到" in reported[0][2]
    parent.close()


def test_readable_message_maps_common_errors() -> None:
    import serial

    assert "权限" in readable_message(PermissionError("denied"))
    assert "找不到" in readable_message(FileNotFoundError("missing"))
    assert "串口" in readable_message(serial.SerialException("closed"))
    assert "访问失败" in readable_message(OSError("io"))
    assert readable_message(ValueError("自定义错误")) == "自定义错误"


def test_process_events_until_returns_false_on_timeout(qapp) -> None:
    assert process_events_until(lambda: False, timeout_ms=10) is False
    assert process_events_until(lambda: True, timeout_ms=0) is True


def test_session_dialog_shows_last_records_and_explains_cap(qapp, monkeypatch) -> None:
    from app.services.session_log import SessionLog
    from app.ui.dialogs import session_dialog as session_dialog_module
    from app.ui.dialogs.session_dialog import SessionDialog

    session = SessionLog()
    session.add_bytes(b"\x01", "rx", "COM1")
    session.add_bytes(b"OK", "tx")
    session.add_bytes("提示".encode("utf-8"), "info")
    monkeypatch.setattr(session_dialog_module, "ROW_CAP", 2)

    dialog = SessionDialog(session)

    assert dialog.table.rowCount() == 2
    assert "上限 2" in dialog.cap_label.text()
    assert dialog.table.item(0, 1).text() == "TX"
    assert dialog.table.item(1, 1).text() == "信息"
    assert dialog.table.item(0, 3).text() == "2"
    dialog.close()

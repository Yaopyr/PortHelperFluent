"""共享控件的离屏测试：日志面板、文件选择、HEX 输入、状态胶囊与反馈。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QApplication, QWidget
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


@pytest.fixture(autouse=True)
def _stop_lingering_log_panes(qapp):
    """即使测试中途失败也关闭 LogPane，确保 33 ms 定时器不残留到后续测试。"""
    yield
    for widget in list(QApplication.allWidgets()):
        try:
            if isinstance(widget, LogPane):
                widget.close()
        except RuntimeError:
            # Qt 已经销毁的包装对象，无需处理。
            continue


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


def test_safe_slot_trims_extra_signal_arguments(qapp, monkeypatch) -> None:
    """Qt 的 clicked 会附带 bool；无参槽与绑定方法都必须照常执行。"""
    reported: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        feedback,
        "show_error",
        lambda parent, title, content: reported.append((parent, title, content)),
    )
    calls: list[object] = []

    @safe_slot
    def no_args() -> None:
        calls.append("no_args")

    @safe_slot
    def with_default(value: str, checked: bool = False) -> None:
        calls.append((value, checked))

    @safe_slot
    def variadic(*args) -> None:
        calls.append(args)

    class Receiver:
        def __init__(self) -> None:
            self.called = False

        @safe_slot
        def method(self) -> None:
            self.called = True

    receiver = Receiver()
    no_args(False)
    with_default("x", True, "extra")
    variadic(1, 2, False)
    receiver.method(False)

    assert calls == ["no_args", ("x", True), (1, 2, False)]
    assert receiver.called is True
    assert reported == []


def test_safe_slot_reports_internal_type_error_instead_of_hiding_it(qapp, monkeypatch) -> None:
    """参数裁剪只发生在调用前；函数体里的 TypeError 必须进入统一错误反馈。"""
    reported: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        feedback,
        "show_error",
        lambda parent, title, content: reported.append((parent, title, content)),
    )

    @safe_slot
    def broken() -> None:
        raise TypeError("内部类型错误")

    assert broken(False) is None
    assert len(reported) == 1
    assert "内部类型错误" in reported[0][2]


def test_log_pane_copy_save_and_clear_buttons_with_real_clicks(
    qapp, tmp_path: Path, monkeypatch, caplog
) -> None:
    """真实 .click() 会带 bool 参数；三个按钮都必须真正执行动作且不报槽错误。"""
    from app.ui.widgets import log_pane as log_pane_module

    caplog.set_level(logging.ERROR, logger="porthelper")
    reported: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        feedback,
        "show_error",
        lambda parent, title, content: reported.append((parent, title, content)),
    )
    target = tmp_path / "saved.txt"
    monkeypatch.setattr(
        log_pane_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(target), "文本文件 (*.txt)")),
    )
    pane = LogPane("RX")
    pane.set_timestamp_visible(False)
    pane.append_bytes(b"hello", "rx")
    assert process_events_until(lambda: "hello" in _text(pane))

    pane.copy_button.click()

    assert pane.copy_button.toolTip() == "复制当前显示"
    assert QApplication.clipboard().text() == _text(pane)
    assert "hello" in QApplication.clipboard().text()

    pane.save_button.click()

    assert target.is_file()
    assert "hello" in target.read_text(encoding="utf-8")

    pane.clear_button.click()

    assert _text(pane) == ""
    assert pane.session().records() == ()
    assert reported == []
    assert "slot failed" not in caplog.text
    pane.close()


def test_file_picker_browse_button_with_real_click(
    qapp, tmp_path: Path, monkeypatch, caplog
) -> None:
    """真实 .click() 必须调用 QFileDialog 并写入选中路径。"""
    from app.ui.widgets import file_picker as file_picker_module

    caplog.set_level(logging.ERROR, logger="porthelper")
    target = tmp_path / "firmware.bin"
    calls: list[tuple] = []

    def fake_dialog(*args, **kwargs):
        calls.append((args, kwargs))
        return str(target), "所有文件 (*.*)"

    monkeypatch.setattr(
        file_picker_module.QFileDialog, "getOpenFileName", staticmethod(fake_dialog)
    )
    picker = FilePicker()

    picker.browse_button.click()

    assert calls, "浏览按钮没有打开文件选择框"
    assert picker.path() == str(target)
    assert "slot failed" not in caplog.text
    picker.close()


def test_session_dialog_export_buttons_with_real_clicks(
    qapp, tmp_path: Path, monkeypatch, caplog
) -> None:
    """导出按钮真实 .click() 后必须写出 CSV/TXT；槽参数不能被误当成数据。"""
    from app.services.session_log import SessionLog
    from app.ui.dialogs import session_dialog as session_dialog_module
    from app.ui.dialogs.session_dialog import SessionDialog

    caplog.set_level(logging.ERROR, logger="porthelper")
    reported: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        feedback,
        "show_error",
        lambda parent, title, content: reported.append((parent, title, content)),
    )
    csv_path = tmp_path / "export.csv"
    txt_path = tmp_path / "export.txt"
    answers = iter([(str(csv_path), "CSV 文件 (*.csv)"), (str(txt_path), "文本文件 (*.txt)")])
    monkeypatch.setattr(
        session_dialog_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: next(answers)),
    )
    session = SessionLog()
    session.add_bytes(b"AB", "rx", "COM1")
    dialog = SessionDialog(session)

    dialog.export_csv_button.click()
    qapp.processEvents()
    dialog.export_txt_button.click()

    assert csv_path.is_file()
    assert "COM1" in csv_path.read_text(encoding="utf-8-sig")
    assert txt_path.is_file()
    assert "[RX]" in txt_path.read_text(encoding="utf-8")
    assert reported == []
    assert "slot failed" not in caplog.text
    dialog.close()


def test_file_picker_accepts_single_local_drop(qapp, tmp_path: Path) -> None:
    """拖入恰好一个本地文件时写入路径并接受事件。"""
    target = tmp_path / "drag.bin"
    target.write_bytes(b"data")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(target))])
    picker = FilePicker()
    enter = QDragEnterEvent(
        QPoint(1, 1),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    picker.dragEnterEvent(enter)

    assert enter.isAccepted() is True

    drop = QDropEvent(
        QPointF(1, 1),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    picker.dropEvent(drop)

    assert drop.isAccepted() is True
    assert picker.path() == str(target)
    picker.close()


def test_file_picker_ignores_multiple_and_remote_drops(qapp, tmp_path: Path) -> None:
    picker = FilePicker()
    mime = QMimeData()
    mime.setUrls(
        [QUrl.fromLocalFile(str(tmp_path / "a.bin")), QUrl.fromLocalFile(str(tmp_path / "b.bin"))]
    )
    drop = QDropEvent(
        QPointF(1, 1),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    picker.dropEvent(drop)

    assert drop.isAccepted() is False
    assert picker.path() == ""
    picker.close()


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


def test_log_pane_honors_settings_max_lines_and_auto_scroll(qapp, tmp_path: Path) -> None:
    from app.core.settings import Settings

    settings = Settings(tmp_path / "config.json")
    settings.load()
    settings.set("ui.max_display_lines", 12345)
    settings.set("ui.auto_scroll", False)

    pane = LogPane("RX", settings=settings)

    assert pane.max_display_lines() == 12345
    assert pane.log_view.maximumBlockCount() == 12345
    assert pane.auto_scroll() is False
    assert pane.autoscroll_check.isChecked() is False

    pane.configure(max_display_lines=999999, auto_scroll=True)

    assert pane.max_display_lines() == 100000
    assert pane.log_view.maximumBlockCount() == 100000
    assert pane.autoscroll_check.isChecked() is True

    pane.configure(max_display_lines=10)

    assert pane.max_display_lines() == 1000
    assert pane.log_view.maximumBlockCount() == 1000
    pane.close()

    # 显式参数优先于设置，但非法值仍被收敛到合法区间。
    override = LogPane("RX", settings=settings, max_display_lines=5000, auto_scroll=True)
    assert override.max_display_lines() == 5000
    assert override.auto_scroll() is True
    override.close()

    clamped = LogPane("RX", max_display_lines=50, auto_scroll=False)
    assert clamped.max_display_lines() == 1000
    assert clamped.auto_scroll() is False
    clamped.close()


def test_log_pane_auto_scroll_off_keeps_view_position(qapp) -> None:
    pane = LogPane("RX", auto_scroll=False, max_display_lines=1000)
    pane.set_timestamp_visible(False)
    pane.resize(400, 200)
    pane.show()
    qapp.processEvents()

    for index in range(300):
        pane.append_info(f"line-{index}")

    assert process_events_until(lambda: "line-299" in _text(pane))
    qapp.processEvents()
    bar = pane.log_view.verticalScrollBar()
    assert bar.maximum() > 0
    assert bar.value() == 0
    pane.close()


def test_log_pane_flush_caps_records_per_tick(qapp) -> None:
    from app.ui.widgets import log_pane as log_pane_module

    pane = LogPane("RX")
    pane._timer.stop()
    pane.set_timestamp_visible(False)
    for index in range(5000):
        pane.append_bytes(str(index).encode("ascii"), "rx")

    assert len(pane._pending) == 5000

    pane._flush_pending()

    assert pane.log_view.blockCount() == log_pane_module.FLUSH_MAX_RECORDS
    assert len(pane._pending) == 5000 - log_pane_module.FLUSH_MAX_RECORDS

    pane._flush_pending()

    assert pane.log_view.blockCount() == 2 * log_pane_module.FLUSH_MAX_RECORDS
    assert len(pane._pending) == 5000 - 2 * log_pane_module.FLUSH_MAX_RECORDS
    # 渲染是分批的，但会话记录一条都不能少。
    assert len(pane.session().records()) == 5000
    pane.close()


def test_log_pane_flush_caps_bytes_per_tick(qapp) -> None:
    from app.ui.widgets import log_pane as log_pane_module

    pane = LogPane("RX")
    pane._timer.stop()
    pane.set_timestamp_visible(False)
    payload = b"x" * 1024
    for _ in range(400):
        pane.append_bytes(payload, "rx")

    pane._flush_pending()

    per_tick = log_pane_module.FLUSH_MAX_BYTES // len(payload)
    assert pane.log_view.blockCount() == per_tick
    assert len(pane._pending) == 400 - per_tick
    pane.close()


def test_log_pane_pending_overflow_drops_oldest_and_counts(qapp) -> None:
    from app.ui.widgets import log_pane as log_pane_module

    pane = LogPane("RX")
    pane._timer.stop()
    total = log_pane_module.PENDING_LIMIT + 25
    for index in range(total):
        pane.append_bytes(str(index).encode("ascii"), "rx")

    records = pane.session().records()
    assert pane.display_dropped == 25
    assert len(pane._pending) == log_pane_module.PENDING_LIMIT
    # 待显示队列丢的是最旧的，会话仍然保留全部记录。
    assert len(records) == total
    assert pane._pending[0] is records[25]
    pane.close()


def test_log_pane_close_stops_render_timer(qapp) -> None:
    pane = LogPane("RX")
    assert pane._timer.isActive() is True

    pane.close()

    assert pane._timer.isActive() is False


def test_log_pane_counts_tx_bytes_and_direction_label(qapp) -> None:
    pane = LogPane("TX")
    pane.set_timestamp_visible(False)

    pane.append_bytes(b"AB", "tx", "COM3")

    assert process_events_until(lambda: "[TX]" in _text(pane))
    assert "COM3: AB" in _text(pane)
    assert pane.session().total_bytes("tx") == 2
    assert pane.session().total_bytes("rx") == 0
    pane.close()


def test_session_dialog_exports_displayed_snapshot_only(
    qapp, tmp_path: Path, monkeypatch
) -> None:
    from app.services.session_log import SessionLog
    from app.ui.dialogs import session_dialog as session_dialog_module
    from app.ui.dialogs.session_dialog import SessionDialog

    monkeypatch.setattr(session_dialog_module, "ROW_CAP", 2)
    session = SessionLog()
    session.add_bytes(b"one", "rx", "COM1")
    session.add_bytes(b"two", "rx", "COM1")
    session.add_bytes(b"three", "rx", "COM1")
    dialog = SessionDialog(session)
    # 对话框打开后实时会话继续增长，但导出应是打开时的表格快照。
    session.add_bytes(b"after-open", "rx", "COM1")
    target = tmp_path / "snapshot.txt"
    monkeypatch.setattr(
        session_dialog_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(target), "文本文件 (*.txt)")),
    )

    dialog.export_txt_button.click()

    text = target.read_text(encoding="utf-8")
    assert "two" in text
    assert "three" in text
    assert "one" not in text
    assert "after-open" not in text
    assert "当前显示" in dialog.cap_label.text()
    dialog.close()


def test_session_dialog_default_export_dir_hook(qapp, tmp_path: Path, monkeypatch) -> None:
    from app.services.session_log import SessionLog
    from app.ui.dialogs import session_dialog as session_dialog_module
    from app.ui.dialogs.session_dialog import SessionDialog

    captured: list[str] = []
    export_dir = tmp_path / "exports"

    def fake_dialog(*args, **kwargs):
        captured.append(args[2])
        return "", ""

    monkeypatch.setattr(
        session_dialog_module.QFileDialog, "getSaveFileName", staticmethod(fake_dialog)
    )
    dialog = SessionDialog(SessionLog(), default_export_dir=export_dir)

    dialog.export_txt_button.click()

    assert captured
    assert captured[0].startswith(str(export_dir))
    assert captured[0].endswith(".txt")
    dialog.close()


def test_session_dialog_header_modes_avoid_resize_to_contents(qapp) -> None:
    from PyQt6.QtWidgets import QHeaderView

    from app.services.session_log import SessionLog
    from app.ui.dialogs.session_dialog import SessionDialog

    dialog = SessionDialog(SessionLog())
    header = dialog.table.horizontalHeader()

    for column in range(4):
        assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
    for column in (4, 5):
        assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Stretch
    dialog.close()


def test_file_picker_drop_handler_never_raises(qapp, monkeypatch, caplog) -> None:
    from app.ui.widgets import file_picker as file_picker_module

    caplog.set_level(logging.ERROR, logger="porthelper")

    def broken(event):
        raise RuntimeError("无法解析拖放数据")

    monkeypatch.setattr(file_picker_module, "_single_local_file", broken)
    picker = FilePicker()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("C:/tmp/x.bin")])
    drop = QDropEvent(
        QPointF(1, 1),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    picker.dropEvent(drop)  # 不抛异常即为通过

    assert drop.isAccepted() is False
    assert "处理文件拖放失败" in caplog.text
    picker.close()


def test_log_pane_uses_settings_font_size(qapp, tmp_path):
    from app.core.settings import Settings
    from app.ui.widgets.log_pane import LogPane

    settings = Settings(tmp_path / "config.json")
    settings.set("ui.log_font_size", 14)
    pane = LogPane("font", settings=settings)
    try:
        assert pane.log_view.font().pointSize() == 14
        pane.configure(font_size=12)
        assert pane.log_view.font().pointSize() == 12
        pane.configure(font_size=99)
        assert pane.log_view.font().pointSize() == 24
    finally:
        pane.close()


def test_log_pane_surfaces_session_truncation(qapp, tmp_path):
    from app.services.session_log import SessionLog
    from app.ui.widgets.log_pane import LogPane

    pane = LogPane("truncate", session=SessionLog(max_bytes=8, max_records=100))
    try:
        pane.append_bytes(b"x" * 32, "rx")
        assert pane.drop_label.isVisible() or pane.drop_label.text()
        assert "已截断" in pane.drop_label.text()
        out = tmp_path / "session.txt"
        pane.save_txt(out)
        assert "已丢弃" in out.read_text(encoding="utf-8")
    finally:
        pane.close()

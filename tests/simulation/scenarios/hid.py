"""USB HID 页场景：真实 HidWorker + VirtualHidBackend。"""

from __future__ import annotations

from pathlib import Path

from app.ui.dialogs.session_dialog import SessionDialog


def _open(sim):
    page = sim.switch("hid")
    page.open_button.click()
    sim.check_true("HID 进入打开状态", sim.wait(lambda: page._worker.is_open(), 3000), page.status_chip.text())
    return page


def hid_01_enumerate_and_filter(sim) -> None:
    page = sim.switch("hid")
    sim.check("HID 设备行数", page.device_table.rowCount(), 2)
    sim.check_in("设备计数", "共 2 个 HID 接口", page.device_count_label.text())
    sim.check_in("首行 VID:PID", "1234:5678", page.device_table.item(0, 0).text())
    sim.check_in("首行产品名", "模拟键盘", page.device_info_label.text())
    sim.check_in("Usage 显示", "0x0001 / 0x0006", page.device_table.item(0, 4).text())

    page.search_edit.setText("厂商")
    sim.check_true("搜索隐藏键盘行", page.device_table.isRowHidden(0), "键盘行未隐藏")
    sim.check_true("搜索保留厂商行", not page.device_table.isRowHidden(1), "厂商行被隐藏")
    page.search_edit.setText("1234:5678")
    sim.check_true("按 VID:PID 搜索", not page.device_table.isRowHidden(0), "键盘行被隐藏")
    page.search_edit.setText("")
    sim.screenshot("hid-enumerate")


def hid_02_open_close_and_failure(sim) -> None:
    page = _open(sim)
    sim.check("打开后状态", page.status_chip.state, "ok")
    sim.check_in("打开描述", "模拟键盘", page.status_chip.text())
    page.report_id_spin.setValue(7)
    page.report_length_spin.setValue(4)
    sim.check("报告 ID 设置", page.report_id_spin.value(), 7)
    sim.check("报告长度设置", page.report_length_spin.value(), 4)
    page.open_button.click()
    sim.check_true("关闭后线程停止", sim.wait_threads(4000), str(sim.thread_states()))
    sim.check_true("关闭后状态回到 idle", sim.wait(lambda: page.status_chip.state == "idle", 3000), page.status_chip.text())

    sim.hub.hid.fail_open = True
    page.open_button.click()
    sim.check_true("HID 打开失败进入错误状态", sim.wait(lambda: page.status_chip.state == "error", 3000), page.status_chip.text())
    sim.check_true("HID 打开失败没有线程", not page._worker.isRunning(), "worker 仍在运行")
    sim.hub.hid.fail_open = False
    sim.screenshot("hid-open-failure")


def hid_03_read_write_pad_truncate_unplug_and_repeat(sim) -> None:
    page = _open(sim)
    backend = sim.hub.hid
    page.report_id_spin.setValue(0)
    page.report_length_spin.setValue(8)
    page.hex_send_check.setChecked(True)
    page.send_editor.setPlainText("AA BB")
    page.send_button.click()
    sim.check_true("HID 报告补零", sim.wait(lambda: bool(backend.writes), 3000), str(backend.writes))
    sim.check("补零后报告", backend.writes[0][1], b"\xAA\xBB" + b"\x00" * 6)

    page.report_id_spin.setValue(7)
    page.report_length_spin.setValue(4)
    page.send_editor.setPlainText("01 02 03 04 05")
    page.send_button.click()
    sim.check_true("HID 报告带 ID 截断", sim.wait(lambda: len(backend.writes) >= 2, 3000), str(backend.writes))
    sim.check("带 ID 报告内容", backend.writes[-1][1], b"\x07\x01\x02\x03")

    backend.push_input(b"\x10\x20")
    sim.check_true("HID 接收进入日志", sim.wait(lambda: "10 20" in page.rx_log.log_view.toPlainText(), 3000), page.rx_log.log_view.toPlainText())
    sim.check("HID 接收计数", page.rx_log.session().total_bytes("rx"), 2)

    page.report_length_spin.setValue(2)
    page.send_editor.setPlainText("55")
    page.interval_spin.setValue(20)
    page.repeat_check.setChecked(True)
    sim.check_true("HID 连续发送", sim.wait(lambda: len(backend.writes) >= 4, 3000), str(len(backend.writes)))
    page.repeat_check.setChecked(False)

    backend.unplug()
    sim.check_true("HID 拔出后自动关闭", sim.wait(lambda: page.status_chip.state == "error" and not page._worker.is_open(), 3000), page.status_chip.text())
    sim.screenshot("hid-read-write")


def hid_04_file_send_and_cancel(sim) -> None:
    page = _open(sim)
    backend = sim.hub.hid
    source = sim.tmp_path / "hid-payload.bin"
    source.write_bytes(b"ABCDEF")
    page.report_id_spin.setValue(0)
    page.report_length_spin.setValue(4)
    sim.modal.open_files.append(str(source))
    page.send_file_button.click()
    sim.check_true(
        "HID 文件发送完成",
        sim.wait(lambda: "完成" in page.file_progress_label.text(), 5000),
        page.file_progress_label.text(),
    )
    sim.check_true("HID 文件报告到达设备", sim.wait(lambda: bool(backend.writes), 3000), str(backend.writes))
    sim.check("HID 文件分报告", [item[1] for item in backend.writes], [b"ABCD", b"EF\x00\x00"])
    sim.artifact(source, "hid-file.bin")

    big = sim.tmp_path / "hid-cancel.bin"
    big.write_bytes(b"Q" * 4096)
    page.report_length_spin.setValue(64)
    sim.modal.open_files.append(str(big))
    page.send_file_button.click()
    sim.wait(lambda: False, 80)
    if page.cancel_file_button.isEnabled():
        page.cancel_file_button.click()
    sim.check_true("HID 取消后未报告完成", "完成" not in page.file_progress_label.text(), page.file_progress_label.text())
    sim.screenshot("hid-file")


def hid_05_session_counters_and_errors(sim) -> None:
    page = _open(sim)
    backend = sim.hub.hid
    backend.push_input(b"rx")
    sim.check_true("HID 接收记录", sim.wait(lambda: page.rx_log.session().total_bytes("rx") == 2, 3000), page.counter_label.text())
    page.report_length_spin.setValue(4)
    page.send_editor.setPlainText("74 78")
    page.send_button.click()
    sim.check_true("HID 发送记录", sim.wait(lambda: page.tx_log.session().total_bytes("tx") == 4, 3000), page.counter_label.text())
    combined = page._combined_session()
    directions = {record.direction for record in combined.records()}
    sim.check_true("HID 合并会话包含收发", {"rx", "tx"} <= directions, str(directions))

    csv_path = sim.tmp_path / "hid-session.csv"
    txt_path = sim.tmp_path / "hid-session.txt"
    dialog = SessionDialog(combined)
    try:
        sim.modal.save_files.extend([str(csv_path), str(txt_path)])
        dialog.export_csv_button.click()
        dialog.export_txt_button.click()
        sim.check_true("HID 会话 CSV", csv_path.is_file(), str(csv_path))
        sim.check_true("HID 会话 TXT", txt_path.is_file(), str(txt_path))
        sim.artifact(csv_path, "hid-session.csv")
        sim.artifact(txt_path, "hid-session.txt")
    finally:
        dialog.close()

    page._reset_counters()
    sim.check_in("HID 计数清零", "接收 0 B · 发送 0 B", page.counter_label.text())
    page.rx_log.clear()
    sim.check("HID 日志清空", page.rx_log.log_view.toPlainText(), "")
    sim.screenshot("hid-session")

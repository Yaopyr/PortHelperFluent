"""串口页场景：真实 SerialWorker + VirtualSerialTransport。"""

from __future__ import annotations

import time
from pathlib import Path

from app.ui.dialogs.multi_send_dialog import MultiSendDialog


def _open(sim):
    page = sim.switch("serial")
    page.port_combo.setCurrentText("COM7")
    page.open_button.click()
    sim.check_true("串口进入打开状态", sim.wait(lambda: page._worker is not None and page._worker.is_open(), 3000), page.status_chip.text())
    return page


def _writes(sim) -> list[bytes]:
    return list(sim.hub.serial.device("COM7").writes)


def ser_01_enumerate_and_persist(sim) -> None:
    page = sim.switch("serial")
    items = [page.port_combo.itemText(index) for index in range(page.port_combo.count())]
    sim.check_true("枚举到 COM7", any("COM7" in item for item in items), str(items))
    sim.check_true("枚举到蓝牙 COM8", any("COM8" in item for item in items), str(items))
    page.port_combo.setCurrentText("COM7")
    page.baud_combo.setCurrentText("9600")
    page.parity_combo.setCurrentIndex(1)
    page.flow_combo.setCurrentIndex(1)
    page.encoding_combo.setCurrentIndex(1)
    page.open_button.click()
    sim.check_true("参数变化后仍可打开", sim.wait(lambda: page._worker is not None and page._worker.is_open(), 3000), page.status_chip.text())
    device = sim.hub.serial.device("COM7")
    sim.check("打开配置波特率", device.open_config.baud, 9600)
    sim.check("打开配置校验", device.open_config.parity, "odd")
    sim.check("打开配置流控", device.open_config.flow, "rtscts")
    page.open_button.click()
    sim.wait(lambda: not page._worker.is_open(), 3000)
    sim.check("串口配置持久化", sim.settings.get("serial.baud"), 9600)
    sim.check("串口编码持久化", sim.settings.get("serial.encoding"), "gbk")
    sim.check("串口端口持久化", sim.settings.get("serial.port"), "COM7")
    sim.screenshot("serial-params")


def ser_02_open_close_and_failure(sim) -> None:
    page = _open(sim)
    sim.check("打开后状态 ok", page.status_chip.state, "ok")
    sim.check_true("打开后按钮为关闭", page.open_button.text().startswith("关闭"), page.open_button.text())
    page.open_button.click()
    sim.check_true("关闭后状态回到 idle", sim.wait(lambda: page.status_chip.state == "idle", 3000), page.status_chip.text())
    sim.check_true("关闭后线程停止", sim.wait_threads(4000), str(sim.thread_states()))

    device = sim.hub.serial.device("COM7")
    device.fail_open = True
    page.port_combo.setCurrentText("COM7")
    page.open_button.click()
    sim.check_true("打开失败进入错误状态", sim.wait(lambda: page.status_chip.state == "error", 3000), page.status_chip.text())
    sim.check_true("打开失败没有线程", page._worker is None or not page._worker.isRunning(), "worker 仍在运行")
    device.fail_open = False
    sim.screenshot("serial-open-failure")


def ser_03_send_text_hex_newline_encoding(sim) -> None:
    page = _open(sim)
    device = sim.hub.serial.device("COM7")
    page.hex_send_check.setChecked(True)
    page.send_editor.setPlainText("0x01 02")
    page.send_button.click()
    sim.check_true("HEX 发送写入正确", sim.wait(lambda: b"\x01\x02" in device.writes, 3000), str(device.writes))
    before = len(device.writes)

    page.send_editor.setPlainText("0G")
    page.send_button.click()
    sim.wait(lambda: False, 150)
    sim.check("非法 HEX 不发送", len(device.writes), before)

    page.hex_send_check.setChecked(False)
    page.encoding_combo.setCurrentIndex(2)
    page.newline_combo.setCurrentIndex(3)
    page.send_editor.setPlainText("你好")
    page.send_button.click()
    expected = "你好".encode("utf-8") + b"\r\n"
    sim.check_true("UTF-8 + CRLF 写入正确", sim.wait(lambda: expected in device.writes, 3000), str(device.writes))
    sim.check_true("TX 日志记录发送内容", sim.wait_text(page.tx_log.log_view, "你好", 3000), page.tx_log.log_view.toPlainText())
    sim.screenshot("serial-send")


def ser_04_receive_log_pause_clear(sim) -> None:
    page = _open(sim)
    device = sim.hub.serial.device("COM7")
    device.push_rx(b"hello")
    sim.check_true("接收文本进入日志", sim.wait(lambda: "hello" in page.rx_log.log_view.toPlainText(), 3000), page.rx_log.log_view.toPlainText())
    sim.check("接收计数", page.rx_log.session().total_bytes("rx"), 5)
    sim.check_in("计数器显示接收字节", "接收 5 B", page.counter_label.text())

    page.rx_log.hex_check.setChecked(True)
    sim.check_true("HEX 模式重绘", sim.wait(lambda: "68 65 6C 6C 6F" in page.rx_log.log_view.toPlainText(), 3000), page.rx_log.log_view.toPlainText())
    page.rx_log.timestamp_check.setChecked(False)
    page.rx_log.pause_switch.setChecked(True)
    before = page.rx_log.log_view.toPlainText()
    device.push_rx(b"world")
    sim.wait(lambda: False, 120)
    sim.check("暂停时不刷新显示", page.rx_log.log_view.toPlainText(), before)
    sim.check("暂停时仍记录会话", page.rx_log.session().total_bytes("rx"), 10)
    page.rx_log.pause_switch.setChecked(False)
    sim.check_true("恢复后补显示", sim.wait(lambda: "77 6F 72 6C 64" in page.rx_log.log_view.toPlainText(), 3000), page.rx_log.log_view.toPlainText())
    page.rx_log.clear_button.click()
    sim.check("清空日志", page.rx_log.log_view.toPlainText(), "")
    sim.screenshot("serial-rx")


def ser_05_line_control_and_modem(sim) -> None:
    page = _open(sim)
    device = sim.hub.serial.device("COM7")
    page.dtr_switch.setChecked(True)
    page.rts_switch.setChecked(True)
    page.break_button.click()
    sim.check_true("DTR 命令到达设备", sim.wait(lambda: ("dtr", True) in device.commands, 3000), str(device.commands))
    sim.check_true("RTS 命令到达设备", ("rts", True) in device.commands, str(device.commands))
    sim.check_true("BREAK 命令到达设备", any(name == "break" for name, _ in device.commands), str(device.commands))

    device.set_modem(cts=True, dsr=True, ri=False, cd=True)
    sim.check_true("CTS 状态灯变绿", sim.wait(lambda: page.line_chips["CTS"].state == "ok", 3000), page.line_chips["CTS"].text())
    sim.check_true("DSR 状态灯变绿", page.line_chips["DSR"].state == "ok", page.line_chips["DSR"].text())
    sim.check_true("RLSD 状态灯变绿", page.line_chips["RLSD"].state == "ok", page.line_chips["RLSD"].text())
    sim.screenshot("serial-line-control")


def ser_06_repeat_send(sim) -> None:
    page = _open(sim)
    device = sim.hub.serial.device("COM7")
    page.send_editor.setPlainText("x")
    page.interval_spin.setValue(20)
    page.repeat_check.setChecked(True)
    sim.check_true("连续发送启动", sim.wait(lambda: len(device.writes) >= 3, 3000), str(len(device.writes)))
    page.repeat_check.setChecked(False)
    sim.check_true("停止后定时器关闭", not page._repeat_timer.isActive(), "定时器仍在运行")
    count = len(device.writes)
    sim.wait(lambda: False, 200)
    sim.check_true("停止后不再追加写入", len(device.writes) <= count + 1, f"{count} -> {len(device.writes)}")
    page.open_button.click()
    sim.check_true("关闭串口后线程停止", sim.wait_threads(4000), str(sim.thread_states()))


def ser_07_file_send_ack_and_cancel(sim) -> None:
    page = _open(sim)
    device = sim.hub.serial.device("COM7")
    payload = bytes(index % 251 for index in range(20 * 1024))
    source = sim.tmp_path / "serial-payload.bin"
    source.write_bytes(payload)
    device.short_write_once = 4096
    sim.modal.open_files.append(str(source))
    page.send_file_button.click()
    sim.check_true(
        "文件发送完成",
        sim.wait(lambda: page._file_handle is None and "完成" in page.file_progress_label.text(), 8000),
        page.file_progress_label.text(),
    )
    sim.check("短写后仍发送完整文件", b"".join(device.writes), payload)
    sim.check("文件进度字节数", page._file_sent, len(payload))
    sim.artifact(source, "serial-file.bin")
    sim.screenshot("serial-file-done")

    big = sim.tmp_path / "serial-cancel.bin"
    big.write_bytes(b"Z" * (100 * 1024))
    sim.modal.open_files.append(str(big))
    device.writes.clear()
    page.send_file_button.click()
    sim.check_true("文件发送进入等待确认", sim.wait(lambda: page._file_handle is not None, 3000), "未开始")
    page.cancel_file_button.click()
    sim.check_true("取消后句柄关闭", sim.wait(lambda: page._file_handle is None, 3000), "句柄未关闭")
    sim.check_true("取消后不再完成", "完成" not in page.file_progress_label.text(), page.file_progress_label.text())
    sim.check_true("取消后写入少于文件", sum(len(chunk) for chunk in device.writes) < len(big.read_bytes()), str(sum(len(chunk) for chunk in device.writes)))


def ser_08_multi_send_session_and_log(sim) -> None:
    page = _open(sim)
    device = sim.hub.serial.device("COM7")
    calls: list[tuple] = []

    def multi_handler(dialog: MultiSendDialog) -> int:
        dialog.text_edits[0].setText("01 02")
        dialog.hex_checks[0].setChecked(True)
        dialog.send_buttons[0].click()
        dialog.text_edits[1].setText("loop")
        dialog.intervals[1].setValue(20)
        dialog.loop_checks[1].setChecked(True)
        sim.wait(lambda: b"loop" in device.writes, 2500)
        sim.modal.dialog_screenshot(dialog, "serial-multi-send")
        calls.append(("interacted",))
        dialog.stop_all_loops()
        dialog.close()
        return 0

    sim.modal.multi_send_handler = multi_handler
    page.multi_send_button.click()
    sim.check_true("多项发送 HEX 写入", sim.wait(lambda: b"\x01\x02" in device.writes, 3000), str(device.writes))
    sim.check_true("多项发送对话框已交互", bool(calls), "handler 未被调用")
    sim.check_true("循环发送产生多次写入", sim.wait(lambda: device.writes.count(b"loop") >= 2, 3000), str(device.writes))

    dialog = MultiSendDialog(page._send_text, page)
    try:
        dialog.text_edits[2].setText("persisted")
        data = dialog.to_dict()
        sim.check("多项发送方案槽位数", len(data["slots"]), 8)
        dialog.load_dict({"slots": [{"text": "restored", "hex_mode": False}]})
        sim.check("多项发送方案可恢复", dialog.text_edits[0].text(), "restored")
    finally:
        dialog.close()

    csv_path = sim.tmp_path / "serial-session.csv"
    txt_path = sim.tmp_path / "serial-session.txt"

    def session_handler(dialog) -> int:
        sim.modal.save_files.extend([str(csv_path), str(txt_path)])
        dialog.export_csv_button.click()
        dialog.export_txt_button.click()
        sim.modal.dialog_screenshot(dialog, "serial-session")
        dialog.close()
        return 0

    sim.modal.session_handler = session_handler
    page.session_button.click()
    sim.check_true("会话 CSV 导出", csv_path.is_file(), str(csv_path))
    sim.check_true("会话 TXT 导出", txt_path.is_file(), str(txt_path))
    sim.artifact(csv_path, "serial-session.csv")
    sim.artifact(txt_path, "serial-session.txt")

    log_path = sim.tmp_path / "serial-log.txt"
    sim.modal.save_files.append(str(log_path))
    page.rx_log.save_button.click()
    sim.check_true("日志保存", log_path.is_file(), str(log_path))
    sim.screenshot("serial-multi-session")

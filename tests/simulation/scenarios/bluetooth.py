"""蓝牙页场景：真实 BleWorker/SppWorker + 有状态虚拟后端。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication

from app.io.ble_worker import BleDeviceInfo
from app.io import spp_worker as spp_worker_module
from app.io.spp_worker import SppWorker
from tests.simulation.devices.ble import READ_UUID, SERVICE_UUID, WRITE_UUID


def _warm_ble(sim):
    page = sim.switch("bluetooth")
    page.mode_segment.setCurrentItem("ble")
    page.scan_button.click()
    sim.check_true(
        "BLE 扫描出两个设备",
        sim.wait(lambda: page.device_table.rowCount() >= 2, 5000),
        f"rows={page.device_table.rowCount()}",
    )
    return page


def _connect_ble(sim, page):
    page.device_table.selectRow(0)
    page.connect_button.click()
    sim.check_true(
        "BLE 连接完成",
        sim.wait(lambda: page._ble_worker is not None and page._ble_worker.is_connected(), 6000),
        page.status_chip.text(),
    )
    sim.check_true(
        "BLE 服务树就绪",
        sim.wait(lambda: page.service_tree.topLevelItemCount() >= 1, 6000),
        f"topLevel={page.service_tree.topLevelItemCount()}",
    )
    sim.check_true(
        "BLE MTU 已更新",
        sim.wait(lambda: "未知" not in page.mtu_label.text(), 6000),
        page.mtu_label.text(),
    )
    page.log_pane.set_hex_mode(True)
    return page


def _tree_item(tree, needle: str):
    for service_index in range(tree.topLevelItemCount()):
        service = tree.topLevelItem(service_index)
        for child_index in range(service.childCount()):
            item = service.child(child_index)
            if needle in item.text(0):
                return item
    return None


def _row_address(table, row: int) -> str:
    item = table.item(row, 1)
    return item.text() if item is not None else ""


def _select_write_char(page) -> None:
    item = _tree_item(page.service_tree, "FFE1")
    sim_page = page
    assert item is not None
    sim_page.service_tree.setCurrentItem(item)


def bt_01_scan_filter_favorite_rename_copy(sim) -> None:
    page = _warm_ble(sim)
    sim.check("BLE 设备数", page.device_table.rowCount(), 2)

    page.filter_edit.setText("模拟")
    sim.check("名称过滤只剩有名称设备", page.device_table.rowCount(), 1)
    page.filter_edit.setText("")
    page.only_named_switch.setChecked(True)
    sim.check("仅显示有名称", page.device_table.rowCount(), 1)
    page.only_named_switch.setChecked(False)
    sim.check("取消过滤恢复两台设备", page.device_table.rowCount(), 2)

    page._toggle_favorite("AA:BB:CC:DD:EE:FF")
    sim.check("收藏持久化", sim.settings.get("bluetooth.favorites"), ["AA:BB:CC:DD:EE:FF"])
    sim.modal.texts.append(("别名设备", True))
    page._rename_device("AA:BB:CC:DD:EE:FF")
    sim.check("别名持久化", sim.settings.get("bluetooth.aliases").get("AA:BB:CC:DD:EE:FF"), "别名设备")
    sim.check_true("别名显示在表格", "别名设备" in page.device_table.item(0, 0).text(), page.device_table.item(0, 0).text())

    sim.modal.menu_items.append("复制地址")
    page.device_table.selectRow(0)
    page._show_device_menu(QPoint(0, 0))
    sim.check("复制蓝牙地址", QApplication.clipboard().text(), "AA:BB:CC:DD:EE:FF")
    sim.screenshot("ble-scan")


def bt_02_connect_mtu_rssi_disconnect_and_failure(sim) -> None:
    page = _warm_ble(sim)
    sim.hub.ble.mtu_value = 247
    _connect_ble(sim, page)
    sim.check_in("MTU 显示", "247", page.mtu_label.text())
    sim.check_true("RSSI 显示", "未知" not in page.rssi_label.text(), page.rssi_label.text())

    sim.hub.ble.drop("模拟设备主动断开连接")
    sim.check_true("对端断开后复位", sim.wait(lambda: page.connect_button.text() == "连接", 4000), page.status_chip.text())
    sim.check_in("断开原因写入日志", "模拟设备主动断开连接", sim.log_text(page))

    page.device_table.selectRow(0)
    sim.hub.ble.fail_on = "connect"
    page.connect_button.click()
    sim.check_true("BLE 连接失败显示错误", sim.wait(lambda: page.status_chip.state == "error", 4000), page.status_chip.text())
    sim.hub.ble.fail_on = None
    sim.screenshot("ble-connect")


def bt_03_service_tree_read_and_copy_uuid(sim) -> None:
    page = _warm_ble(sim)
    _connect_ble(sim, page)
    sim.check_true("服务树非空", page.service_tree.topLevelItemCount() >= 1, "无服务节点")
    _select_write_char(page)
    sim.check_in("写入目标标签", "FFE1", page.target_label.text())
    page.read_button.click()
    sim.check_true("特征值读取进入日志", sim.wait(lambda: "01 02" in sim.log_text(page), 4000), sim.log_text(page))

    service_node = page.service_tree.topLevelItem(0)
    page.service_tree.setCurrentItem(service_node)
    QApplication.clipboard().clear()
    sim.modal.menu_items.append("复制服务 UUID")
    page._show_service_menu(QPoint(5000, 5000))
    sim.check("复制服务 UUID", QApplication.clipboard().text(), SERVICE_UUID)
    page.service_tree.setCurrentItem(_tree_item(page.service_tree, "FFE1"))
    sim.modal.menu_items.append("复制特征值 UUID")
    page._show_service_menu(QPoint(5000, 5000))
    sim.check("复制特征值 UUID", QApplication.clipboard().text(), WRITE_UUID)
    sim.screenshot("ble-service-tree")


def bt_04_write_notify_and_repeat(sim) -> None:
    page = _warm_ble(sim)
    _connect_ble(sim, page)
    _select_write_char(page)
    sim.check_true("有写入目标后发送按钮可用", page.send_button.isEnabled(), "发送按钮仍禁用")
    page.hex_send_check.setChecked(True)
    page.send_editor.setPlainText("01 02")
    page.send_button.click()
    sim.check_true(
        "BLE 写入到达后端",
        sim.wait(lambda: any(call[0] == "write" and call[2] == b"\x01\x02" for call in sim.hub.ble.calls), 4000),
        str(sim.hub.ble.calls),
    )
    responses = [call[3] for call in sim.hub.ble.calls if call[0] == "write"]
    sim.check("默认写入需应答", responses[-1], True)
    page.write_with_response_switch.setChecked(False)
    page.send_button.click()
    sim.wait(lambda: len([call for call in sim.hub.ble.calls if call[0] == "write"]) >= 2, 3000)
    responses = [call[3] for call in sim.hub.ble.calls if call[0] == "write"]
    sim.check("关闭应答后无响应写", responses[-1], False)

    page.subscribe_button.click()
    sim.check_true("订阅回调就绪", sim.wait(lambda: sim.hub.ble.notify_ready.is_set(), 4000), "未收到订阅")
    sim.hub.ble.emit_notification(WRITE_UUID, b"")
    sim.check_in("空通知计数", "忽略空通知", page.counter_label.text())
    sim.hub.ble.emit_notification(WRITE_UUID, b"\x10\x20")
    sim.check_true("通知进入日志", sim.wait(lambda: "10 20" in sim.log_text(page), 4000), sim.log_text(page))

    page.interval_spin.setValue(20)
    page.repeat_check.setChecked(True)
    sim.check_true(
        "BLE 连续发送",
        sim.wait(lambda: len([call for call in sim.hub.ble.calls if call[0] == "write"]) >= 4, 4000),
        str(sim.hub.ble.calls),
    )
    page.repeat_check.setChecked(False)
    sim.screenshot("ble-write-notify")


def bt_05_mtu_chunk_file(sim) -> None:
    page = _warm_ble(sim)
    sim.hub.ble.mtu_value = 23
    _connect_ble(sim, page)
    _select_write_char(page)
    payload = bytes(range(60))
    source = sim.tmp_path / "ble-payload.bin"
    source.write_bytes(payload)
    sim.modal.open_files.append(str(source))
    page.send_file_button.click()
    sim.check_true(
        "BLE 文件发送完成",
        sim.wait(lambda: "完成" in page.file_progress_label.text(), 6000),
        page.file_progress_label.text(),
    )
    writes = [call[2] for call in sim.hub.ble.calls if call[0] == "write"]
    sim.check("按 MTU-3 分片", [len(data) for data in writes], [20, 20, 20])
    sim.check("分片内容拼接", b"".join(writes), payload)
    sim.artifact(source, "ble-file.bin")

    sim.hub.ble.calls.clear()
    page.mtu_chunk_switch.setChecked(False)
    sim.modal.open_files.append(str(source))
    page.send_file_button.click()
    sim.check_true("关闭分片后一次发送", sim.wait(lambda: any(call[0] == "write" for call in sim.hub.ble.calls), 4000), str(sim.hub.ble.calls))
    writes = [call[2] for call in sim.hub.ble.calls if call[0] == "write"]
    sim.check("不分片写入长度", [len(data) for data in writes], [60])
    sim.screenshot("ble-file")


def _warm_spp(sim):
    page = sim.switch("bluetooth")
    page.mode_segment.setCurrentItem("spp")
    page.spp_scan_button.click()
    sim.check_true(
        "SPP 扫描出两个设备",
        sim.wait(lambda: page.spp_table.rowCount() >= 2, 5000),
        f"rows={page.spp_table.rowCount()}",
    )
    return page


def _visible_spp_rows(page) -> list[int]:
    return [
        row
        for row in range(page.spp_table.rowCount())
        if not page.spp_table.isRowHidden(row)
    ]


def bt_06_spp_scan_filter(sim) -> None:
    page = _warm_spp(sim)
    sim.check("SPP 设备数", page.spp_table.rowCount(), 2)
    page.spp_filter_edit.setText("已配对")
    sim.check("SPP 名称过滤可见行", len(_visible_spp_rows(page)), 1)
    page.spp_filter_edit.setText("")
    page.only_paired_switch.setChecked(True)
    sim.check("SPP 只看已配对可见行", len(_visible_spp_rows(page)), 1)
    page.only_paired_switch.setChecked(False)
    sim.check_true("SPP 配对状态列", "已配对" in page.spp_table.item(0, 2).text(), page.spp_table.item(0, 2).text())
    sim.screenshot("spp-scan")


def bt_07_spp_pair_pin_reject_failure_timeout(sim) -> None:
    page = _warm_spp(sim)
    page.spp_table.selectRow(1)
    sim.hub.spp.prompt_kind = "pin"
    sim.modal.texts.append(("1234", True))
    page.spp_connect_button.click()
    sim.check_true(
        "SPP PIN 配对连接",
        sim.wait(lambda: "RFCOMM" in page.spp_info_label.text(), 6000),
        page.status_chip.text(),
    )
    sim.check_true("配对调用到达后端", any(call[0] == "pair" for call in sim.hub.spp.calls), str(sim.hub.spp.calls))
    page.spp_connect_button.click()
    sim.check_true("RFCOMM 断开", sim.wait(lambda: page.spp_connect_button.text() == "一键连接", 4000), page.spp_connect_button.text())
    sim.check_true("SPP worker 空闲", sim.wait(lambda: not page._spp_worker._connecting, 4000), "worker 仍忙")

    page.spp_table.selectRow(1)
    sim.modal.texts.append(("", False))
    page.spp_connect_button.click()
    sim.check_true("拒绝 PIN 后报错", sim.wait(lambda: page.status_chip.state == "error", 6000), page.status_chip.text())
    sim.check_true("拒绝后 worker 空闲", sim.wait(lambda: not page._spp_worker._connecting, 4000), "worker 仍忙")

    sim.hub.spp.fail_on = "pair"
    page.spp_table.selectRow(1)
    page.spp_connect_button.click()
    sim.check_true("配对异常报错", sim.wait(lambda: page.status_chip.state == "error", 6000), page.status_chip.text())
    sim.hub.spp.fail_on = None
    sim.check_true("失败后 worker 空闲", sim.wait(lambda: not page._spp_worker._connecting, 4000), "worker 仍忙")

    sim.hub.spp.pair_delay = 0.5
    sim.monkeypatch.setattr(SppWorker, "PROMPT_TIMEOUT", 0.1)
    sim.monkeypatch.setattr(spp_worker_module, "PAIR_TIMEOUT", 0.2)
    page.spp_table.selectRow(1)
    page.spp_connect_button.click()
    sim.check_true("配对超时报错", sim.wait(lambda: page.status_chip.state == "error", 6000), page.status_chip.text())
    sim.hub.spp.pair_delay = 0.0
    sim.screenshot("spp-pair")


def bt_08_spp_rfcomm_com_file_and_session(sim) -> None:
    page = _warm_spp(sim)
    page.spp_table.selectRow(0)
    page.spp_connect_button.click()
    sim.check_true("SPP RFCOMM 连接", sim.wait(lambda: "RFCOMM" in page.spp_info_label.text(), 6000), page.status_chip.text())
    page.send_editor.setPlainText("hello-spp")
    page.send_button.click()
    sim.check_true(
        "SPP 发送到达后端",
        sim.wait(lambda: any(call[0] == "send" and call[1] == b"hello-spp" for call in sim.hub.spp.calls), 4000),
        str(sim.hub.spp.calls),
    )
    sim.hub.spp.deliver(b"rx-spp")
    sim.check_true("SPP 接收进入日志", sim.wait(lambda: "rx-spp" in sim.log_text(page), 4000), sim.log_text(page))

    payload = bytes(range(256)) * 40
    source = sim.tmp_path / "spp-payload.bin"
    source.write_bytes(payload)
    sim.modal.open_files.append(str(source))
    page.send_file_button.click()
    sim.check_true(
        "SPP 文件发送完成",
        sim.wait(lambda: "完成" in page.file_progress_label.text(), 8000),
        page.file_progress_label.text(),
    )
    sends = [call[1] for call in sim.hub.spp.calls if call[0] == "send"]
    file_sends = sends[1:]
    sim.check("SPP 文件按 4 KiB 分片", [len(data) for data in file_sends], [4096, 4096, 2048])
    sim.check("SPP 文件总字节", sum(len(data) for data in sends), len(b"hello-spp") + len(payload))
    sim.artifact(source, "spp-file.bin")

    log_path = sim.tmp_path / "spp-session.txt"
    sim.modal.save_files.append(str(log_path))
    page.log_pane.save_button.click()
    sim.check_true("SPP 会话导出", log_path.is_file(), str(log_path))
    sim.artifact(log_path, "spp-session.txt")

    sim.hub.spp.drop_peer("模拟对端主动断开")
    sim.check_true("SPP 对端断开后复位", sim.wait(lambda: page.spp_connect_button.text() == "一键连接", 4000), page.status_chip.text())

    page.com_combo.setCurrentText("COM8")
    page.baud_combo.setCurrentText("9600")
    page.com_connect_button.click()
    sim.check_true("SPP COM 回退连接", sim.wait(lambda: page.status_chip.state == "ok", 6000), page.status_chip.text())
    page.send_editor.setPlainText("com-data")
    page.send_button.click()
    sim.check_true(
        "COM 回退写入虚拟串口",
        sim.wait(lambda: b"com-data" in sim.hub.serial.device("COM8").writes, 4000),
        str(sim.hub.serial.device("COM8").writes),
    )
    sim.screenshot("spp-com")

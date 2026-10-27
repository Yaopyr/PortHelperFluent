"""HidWorker tests with an in-memory fake backend; no hardware required."""

from __future__ import annotations

import queue

from app.io.hid_worker import (
    HidDeviceInfo,
    HidWorker,
    HidapiBackend,
    build_report,
    list_devices,
)
from app.ui.qt_utils import process_events_until

DEVICES = (
    HidDeviceInfo(
        path=b"fake-path-keyboard",
        vendor_id=0x1234,
        product_id=0x5678,
        product="Fake Keyboard",
        manufacturer="Acme",
        serial="KB-1",
        usage_page=0x01,
        usage=0x06,
        interface=0,
    ),
    HidDeviceInfo(
        path=b"fake-path-mouse",
        vendor_id=0x046D,
        product_id=0xC52B,
        product="Fake Mouse",
        manufacturer="Logi",
        serial="MS-1",
        usage_page=0x01,
        usage=0x02,
        interface=1,
    ),
)


class FakeHidBackend:
    def __init__(self, devices=DEVICES) -> None:
        self.devices = list(devices)
        self.reads: queue.Queue[bytes] = queue.Queue()
        self.writes: list[bytes] = []
        self.opened_paths: list[bytes] = []
        self.closed = 0
        self.fail_open = False
        self.fail_read = False
        self.fail_write = False

    def enumerate(self):
        return list(self.devices)

    def open(self, path):
        if self.fail_open:
            raise OSError("access denied")
        self.opened_paths.append(bytes(path))
        return object()

    def write(self, handle, data):
        if self.fail_write:
            raise OSError("device unplugged")
        self.writes.append(bytes(data))
        return len(data)

    def read(self, handle, size, timeout_ms):
        if self.fail_read:
            raise OSError("device removed")
        try:
            return self.reads.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return b""

    def close(self, handle):
        self.closed += 1


def _wait(predicate, timeout_ms=2000):
    return process_events_until(predicate, timeout_ms=timeout_ms)


def _open_worker(backend):
    worker = HidWorker(backend)
    worker.open_device(DEVICES[0])
    assert _wait(worker.is_open)
    return worker


def test_enumerate_two_devices(qapp):
    backend = FakeHidBackend()
    devices = backend.enumerate()
    assert len(devices) == 2
    assert devices[0].id_text == "1234:5678"
    assert devices[0].product == "Fake Keyboard"
    assert devices[0].usage_text == "0x0001 / 0x0006"
    assert devices[1].serial == "MS-1"


def test_open_and_close_device(qapp):
    backend = FakeHidBackend()
    worker = HidWorker(backend)
    opened = []
    closed = []
    worker.opened.connect(opened.append)
    worker.closed.connect(lambda: closed.append(True))
    try:
        worker.open_device(DEVICES[0])
        assert _wait(lambda: opened and worker.is_open())
        assert backend.opened_paths == [DEVICES[0].path]
        assert "Fake Keyboard" in opened[0]
        assert "1234:5678" in opened[0]

        worker.close_device()
        assert _wait(lambda: closed)
        assert not worker.is_open()
        assert not worker.isRunning()
        assert backend.closed == 1
    finally:
        worker.close_device()
        worker.wait(2000)


def test_send_pads_and_prepends_report_id(qapp):
    backend = FakeHidBackend()
    worker = _open_worker(backend)
    try:
        assert worker.send(b"\xAA\xBB", report_id=0, report_length=8) is True
        assert worker.send(b"\xCC", report_id=5, report_length=4) is True
        assert worker.send(b"abcdef", report_id=0, report_length=4) is True
        assert _wait(lambda: len(backend.writes) == 3)
        assert backend.writes[0] == b"\xAA\xBB" + b"\x00" * 6
        assert backend.writes[1] == b"\x05\xCC\x00\x00"
        assert backend.writes[2] == b"abcd"
    finally:
        worker.close_device()
        worker.wait(2000)


def test_read_loop_emits_received_bytes(qapp):
    backend = FakeHidBackend()
    worker = _open_worker(backend)
    received = []
    worker.data_received.connect(received.append)
    try:
        backend.reads.put(b"\x01\x02\x03")
        assert _wait(lambda: received)
        assert received == [b"\x01\x02\x03"]
    finally:
        worker.close_device()
        worker.wait(2000)


def test_read_error_emits_error_and_closes(qapp):
    backend = FakeHidBackend()
    backend.fail_read = True
    worker = HidWorker(backend)
    errors = []
    closed = []
    worker.error.connect(errors.append)
    worker.closed.connect(lambda: closed.append(True))
    try:
        worker.open_device(DEVICES[0])
        assert _wait(lambda: errors and closed, timeout_ms=3000)
        assert "读取 HID Report 失败" in errors[0]
        assert not worker.is_open()
        assert backend.closed == 1
    finally:
        worker.close_device()
        worker.wait(2000)


def test_write_error_emits_error_and_closes(qapp):
    backend = FakeHidBackend()
    worker = _open_worker(backend)
    errors = []
    closed = []
    worker.error.connect(errors.append)
    worker.closed.connect(lambda: closed.append(True))
    try:
        backend.fail_write = True
        assert worker.send(b"x", report_id=0, report_length=4)
        assert _wait(lambda: errors and closed, timeout_ms=3000)
        assert "发送 HID Report 失败" in errors[0]
        assert not worker.is_open()
    finally:
        worker.close_device()
        worker.wait(2000)


def test_send_without_open_reports_error(qapp):
    worker = HidWorker(FakeHidBackend())
    errors = []
    worker.error.connect(errors.append)
    assert worker.send(b"x") is False
    assert _wait(lambda: errors)
    assert "未打开" in errors[0]
    assert not worker.is_open()


def test_failed_open_reports_error_without_thread(qapp):
    backend = FakeHidBackend()
    backend.fail_open = True
    worker = HidWorker(backend)
    errors = []
    worker.error.connect(errors.append)
    worker.open_device(DEVICES[0])
    assert _wait(lambda: errors)
    assert "打开 HID 设备失败" in errors[0]
    assert not worker.is_open()
    assert not worker.isRunning()
    worker.close_device()


def test_build_report_edge_cases():
    assert build_report(b"", report_id=0, report_length=4) == b"\x00\x00\x00\x00"
    assert build_report(b"AB", report_id=3, report_length=1) == b"\x03"
    assert build_report(b"AB", report_id=3, report_length=4) == b"\x03AB\x00"
    try:
        build_report(b"x", report_id=256, report_length=8)
    except ValueError as exc:
        assert "0–255" in str(exc)
    else:
        raise AssertionError("报告 ID 超界应报错")


def test_enumeration_failure_returns_empty_list():
    class BrokenBackend(HidapiBackend):
        def _hid(self):
            raise OSError("hidapi DLL missing")

    assert BrokenBackend().enumerate() == []


def test_real_enumeration_returns_a_list_without_hardware():
    devices = list_devices()
    assert isinstance(devices, list)
    for info in devices:
        assert isinstance(info, HidDeviceInfo)
        assert isinstance(info.path, bytes)

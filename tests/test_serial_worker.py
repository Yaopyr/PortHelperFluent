"""SerialWorker/transport tests using an in-memory fake; no hardware required."""

from __future__ import annotations

import queue
import time

from app.io.serial_worker import SerialConfig, SerialWorker, list_ports
from app.ui.qt_utils import process_events_until


class FakeTransport:
    def __init__(self) -> None:
        self.reads: queue.Queue[bytes] = queue.Queue()
        self.writes: list[bytes] = []
        self.commands: list[tuple[str, object]] = []
        self.closed = 0
        self.fail_read = False
        self.config = None
        self.status = {"cts": False, "dsr": False, "ri": False, "cd": False}

    def open(self, config):
        self.config = config

    def close(self):
        self.closed += 1

    def read(self, size, timeout):
        if self.fail_read:
            raise OSError("device removed")
        try:
            return self.reads.get(timeout=timeout)
        except queue.Empty:
            return b""

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def set_dtr(self, value):
        self.commands.append(("dtr", value))

    def set_rts(self, value):
        self.commands.append(("rts", value))

    def send_break(self, duration=0.25):
        self.commands.append(("break", duration))

    def modem_status(self):
        return dict(self.status)


def _wait(predicate, timeout_ms=2000):
    return process_events_until(predicate, timeout_ms=timeout_ms)


def test_list_ports_is_a_list_without_hardware():
    ports = list_ports()
    assert isinstance(ports, list)


def test_open_send_and_receive(qapp):
    transport = FakeTransport()
    worker = SerialWorker(transport)
    opened = []
    received = []
    worker.opened.connect(lambda: opened.append(True))
    worker.data_received.connect(received.append)
    try:
        assert worker.open_port(SerialConfig("COM_TEST", baud=9600)) is True
        assert _wait(lambda: opened)
        assert worker.is_open()

        assert worker.send(b"AB") is True
        assert _wait(lambda: transport.writes)
        assert transport.writes == [b"AB"]

        transport.reads.put(b"hello")
        assert _wait(lambda: received)
        assert received == [b"hello"]
    finally:
        worker.close_port()
        worker.wait(2000)
    assert transport.closed == 1


def test_close_emits_closed_and_stops_thread(qapp):
    transport = FakeTransport()
    worker = SerialWorker(transport)
    closed = []
    worker.closed.connect(lambda: closed.append(True))
    try:
        assert worker.open_port(SerialConfig("COM_TEST"))
        assert _wait(lambda: worker.is_open())
        worker.close_port()
        assert _wait(lambda: closed)
    finally:
        worker.close_port()
        worker.wait(2000)
    assert not worker.is_open()


def test_read_error_emits_error_and_closes(qapp):
    transport = FakeTransport()
    transport.fail_read = True
    worker = SerialWorker(transport)
    errors = []
    closed = []
    worker.error.connect(errors.append)
    worker.closed.connect(lambda: closed.append(True))
    try:
        assert worker.open_port(SerialConfig("COM_TEST"))
        assert _wait(lambda: errors and closed, timeout_ms=3000)
    finally:
        worker.close_port()
        worker.wait(2000)
    assert any("读取失败" in item for item in errors)
    assert transport.closed >= 1


def test_line_control_commands_are_queued(qapp):
    transport = FakeTransport()
    worker = SerialWorker(transport)
    try:
        assert worker.open_port(SerialConfig("COM_TEST"))
        assert worker.set_dtr(True)
        assert worker.set_rts(False)
        assert worker.send_break()
        assert _wait(lambda: len(transport.commands) >= 3)
        assert ("dtr", True) in transport.commands
        assert ("rts", False) in transport.commands
        assert any(name == "break" for name, _value in transport.commands)
    finally:
        worker.close_port()
        worker.wait(2000)


def test_send_without_open_reports_error(qapp):
    worker = SerialWorker(FakeTransport())
    errors = []
    worker.error.connect(errors.append)
    assert worker.send(b"x") is False
    assert _wait(lambda: errors)
    assert "未打开" in errors[-1]


def test_failed_open_reports_error(qapp):
    class BrokenTransport(FakeTransport):
        def open(self, config):
            raise OSError("access denied")

    worker = SerialWorker(BrokenTransport())
    errors = []
    worker.error.connect(errors.append)
    assert worker.open_port(SerialConfig("COM_TEST")) is False
    assert _wait(lambda: errors)
    assert "打开串口失败" in errors[-1]
    assert not worker.isRunning()

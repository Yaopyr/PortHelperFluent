"""``serial`` 子命令：端口枚举、send 与 modem 的 CLI 契约。

真实串口不在测试里打开：``serial_io.open_transport`` / ``serial_io.list_ports`` 被
monkeypatch 成内存 transport，验证参数映射、读写顺序、退出码与信封字段。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.cli.transports import serial_io

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _port_info(device, description, hwid, vid=None, pid=None, serial_number=None):
    """惰性导入 ``PortInfo``：测试模块级导入不能把 PyQt6/pyserial 拉进进程。"""
    from app.io.serial_worker import PortInfo

    return PortInfo(device, description, hwid, vid, pid, serial_number)


class FakeTransport:
    """实现 ``SerialTransport`` 协议的内存替身，按顺序记录全部调用。"""

    def __init__(
        self,
        chunks=(),
        *,
        modem=None,
        open_error=None,
        write_error=None,
        read_error=None,
    ):
        self.chunks = list(chunks)
        self.closed = False
        self.writes = []
        self.reads = []
        self.calls = []
        self.config = None
        self.dtr = None
        self.rts = None
        self.broke = False
        self._modem = (
            modem
            if modem is not None
            else {"cts": True, "dsr": False, "ri": False, "cd": True}
        )
        self._open_error = open_error
        self._write_error = write_error
        self._read_error = read_error

    def open(self, config):
        self.calls.append(("open", config))
        if self._open_error is not None:
            raise self._open_error
        self.config = config

    def close(self):
        self.calls.append(("close",))
        self.closed = True

    def read(self, size, timeout):
        self.calls.append(("read", size, timeout))
        self.reads.append(timeout)
        if self._read_error is not None:
            raise self._read_error
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        self.calls.append(("write", bytes(data)))
        if self._write_error is not None:
            raise self._write_error
        self.writes.append(bytes(data))
        return len(data)

    def set_dtr(self, value):
        self.calls.append(("dtr", value))
        self.dtr = value

    def set_rts(self, value):
        self.calls.append(("rts", value))
        self.rts = value

    def send_break(self, duration=0.25):
        self.calls.append(("break",))
        self.broke = True

    def modem_status(self):
        self.calls.append(("modem",))
        return dict(self._modem)


def _prevent_hanging_read(monkeypatch) -> None:
    """把 read_until 换成即时返回的桩，避免非法时间参数让红灯测试真的挂起。"""
    from app.cli.reads import ReadOutcome

    monkeypatch.setattr(
        "app.cli.commands.serial.read_until",
        lambda fetch, **kwargs: ReadOutcome(b"", False, False, 0, 0.0),
    )


def _install(monkeypatch, transport: FakeTransport) -> FakeTransport:
    monkeypatch.setattr(serial_io, "open_transport", lambda: transport)
    return transport


def _install_ports(monkeypatch, ports) -> None:
    monkeypatch.setattr(serial_io, "list_ports", lambda: list(ports))


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_reports_ports(cli, monkeypatch) -> None:
    _install_ports(
        monkeypatch,
        [
            _port_info("COM9", "USB Serial", "USB VID:PID=1234:5678", 0x1234, 0x5678, "SN1"),
            _port_info("COM3", "Bluetooth Link", "BTHENUM"),
        ],
    )
    payload = cli(["serial", "list"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "serial.list"

    data = payload["data"]
    assert data["count"] == 2
    assert [item["device"] for item in data["ports"]] == ["COM9", "COM3"]
    assert set(data["ports"][0]) == {
        "device",
        "description",
        "hwid",
        "vid",
        "pid",
        "serial_number",
    }
    assert data["ports"][0]["vid"] == 0x1234
    assert data["ports"][1]["serial_number"] is None


def test_list_filter_matches_device_description_and_hwid(cli, monkeypatch) -> None:
    _install_ports(
        monkeypatch,
        [
            _port_info("COM9", "USB Serial", "USB VID:PID=1234:5678"),
            _port_info("COM3", "Bluetooth Link", "BTHENUM"),
        ],
    )
    payload = cli(["serial", "list", "--filter", "usb"])
    assert payload["exit_code"] == 0
    assert payload["data"]["count"] == 1
    assert payload["data"]["ports"][0]["device"] == "COM9"

    by_hwid = cli(["serial", "list", "--filter", "bthenum"])
    assert by_hwid["data"]["count"] == 1
    assert by_hwid["data"]["ports"][0]["device"] == "COM3"

    missing = cli(["serial", "list", "--filter", "nothing-here"])
    assert missing["exit_code"] == 0
    assert missing["data"] == {"ports": [], "count": 0}


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def test_send_expect_match(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport([b"O", b"K"]))
    payload = cli(
        ["serial", "send", "--port", "COM9", "--text", "AT", "--expect", "OK"]
    )
    assert payload["exit_code"] == 0
    assert payload["command"] == "serial.send"

    data = payload["data"]
    assert data["matched"] is True
    assert data["sent"]["bytes"] == 2
    assert data["sent"]["writes"] == 1
    assert data["received"]["hex"] == "4f4b"
    assert data["received"]["text"] == "OK"
    assert data["received"]["size"] == 2
    assert data["modem"] == {"cts": True, "dsr": False, "ring": False, "rlsd": True}
    assert transport.writes == [b"AT"]
    assert transport.closed is True


def test_send_expect_hex_matches_raw_bytes(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport([b"\x01\x02"]))
    payload = cli(
        ["serial", "send", "--port", "COM9", "--hex", "0x41", "--expect-hex", "0102"]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["matched"] is True
    assert transport.writes == [b"A"]


def test_expect_mismatch_exits_4(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport([b"NO"]))
    payload = cli(
        [
            "serial",
            "send",
            "--port",
            "COM9",
            "--text",
            "AT",
            "--expect",
            "OK",
            "--timeout",
            "0.1",
        ]
    )
    assert payload["exit_code"] == 4
    assert payload["ok"] is False
    assert payload["error"]["code"] == "expect_mismatch"
    assert transport.closed is True


def test_send_default_connection_config(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport())
    payload = cli(["serial", "send", "--port", "COM9", "--text", "AT", "--read-for", "0"])
    assert payload["exit_code"] == 0

    config = transport.config
    assert config.port == "COM9"
    assert config.baud == 115200
    assert config.bytesize == 8
    assert config.stopbits == 1
    assert config.parity == "none"
    assert config.flow == "none"
    assert config.encoding == "utf-8"


def test_send_maps_connection_options(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport())
    cli(
        [
            "serial",
            "send",
            "--port",
            "COM9",
            "--text",
            "AT",
            "--baud",
            "9600",
            "--data-bits",
            "7",
            "--stop-bits",
            "1.5",
            "--parity",
            "even",
            "--flow",
            "rtscts",
            "--encoding",
            "gbk",
            "--read-for",
            "0",
        ]
    )
    config = transport.config
    assert config.baud == 9600
    assert config.bytesize == 7
    assert config.stopbits == 1.5
    assert config.parity == "even"
    assert config.flow == "rtscts"
    assert config.encoding == "gbk"


def test_read_for_zero_reads_nothing_and_matched_false(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport([b"RX"]))
    payload = cli(["serial", "send", "--port", "COM9", "--text", "AT", "--read-for", "0"])
    data = payload["data"]
    assert data["matched"] is False
    assert data["received"] == {"hex": "", "text": "", "size": 0}
    assert transport.reads == []


def test_no_send_break_by_default(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport())
    cli(["serial", "send", "--port", "COM9", "--text", "AT", "--read-for", "0"])
    assert transport.broke is False


def test_dtr_rts_break_line_ending_and_file_input(cli, monkeypatch, tmp_path) -> None:
    transport = _install(monkeypatch, FakeTransport())
    payload = cli(
        [
            "serial",
            "send",
            "--port",
            "COM9",
            "--text",
            "AT",
            "--line-ending",
            "crlf",
            "--dtr",
            "on",
            "--rts",
            "off",
            "--send-break",
            "--read-for",
            "0",
        ]
    )
    assert payload["exit_code"] == 0
    assert transport.writes == [b"AT\r\n"]
    assert transport.dtr is True
    assert transport.rts is False
    assert transport.broke is True
    assert transport.closed is True

    source = tmp_path / "payload.bin"
    source.write_bytes(b"FILE-DATA")
    from_file = _install(monkeypatch, FakeTransport())
    payload = cli(["serial", "send", "--port", "COM9", "--file", str(source), "--read-for", "0"])
    assert payload["exit_code"] == 0
    assert from_file.writes == [b"FILE-DATA"]
    assert from_file.dtr is None
    assert from_file.rts is None
    assert from_file.broke is False
    assert from_file.closed is True


def test_send_call_order(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport([b"O", b"K"]))
    payload = cli(
        [
            "serial",
            "send",
            "--port",
            "COM9",
            "--text",
            "AT",
            "--dtr",
            "on",
            "--rts",
            "off",
            "--send-break",
            "--repeat",
            "2",
            "--interval-ms",
            "0",
            "--expect",
            "OK",
        ]
    )
    assert payload["exit_code"] == 0
    assert [call[0] for call in transport.calls] == [
        "open",
        "dtr",
        "rts",
        "break",
        "write",
        "write",
        "read",
        "read",
        "modem",
        "close",
    ]
    assert transport.calls[0] == ("open", transport.config)
    assert transport.calls[1] == ("dtr", True)
    assert transport.calls[2] == ("rts", False)
    assert transport.calls[3] == ("break",)
    assert transport.calls[4] == ("write", b"AT")
    assert transport.calls[5] == ("write", b"AT")
    assert transport.calls[6][0] == "read"
    assert transport.calls[7][0] == "read"
    assert transport.calls[8] == ("modem",)
    assert transport.calls[9] == ("close",)


def test_line_ending_applied_once_before_repeat(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport())
    payload = cli(
        [
            "serial",
            "send",
            "--port",
            "COM9",
            "--text",
            "AT",
            "--line-ending",
            "crlf",
            "--repeat",
            "3",
            "--interval-ms",
            "0",
            "--read-for",
            "0",
        ]
    )
    assert payload["exit_code"] == 0
    assert transport.writes == [b"AT\r\n", b"AT\r\n", b"AT\r\n"]
    assert payload["data"]["sent"]["bytes"] == 12
    assert payload["data"]["sent"]["writes"] == 3


def test_repeat_must_be_positive(cli, monkeypatch) -> None:
    _install(monkeypatch, FakeTransport())
    payload = cli(
        ["serial", "send", "--port", "COM9", "--text", "AT", "--repeat", "0", "--read-for", "0"]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


def test_negative_interval_is_rejected(cli, monkeypatch) -> None:
    _install(monkeypatch, FakeTransport())
    payload = cli(
        [
            "serial",
            "send",
            "--port",
            "COM9",
            "--text",
            "AT",
            "--interval-ms",
            "-5",
            "--read-for",
            "0",
        ]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


def test_non_finite_timeout_is_rejected(cli, monkeypatch) -> None:
    _prevent_hanging_read(monkeypatch)
    _install(monkeypatch, FakeTransport())
    for value in ("nan", "inf"):
        payload = cli(
            ["serial", "send", "--port", "COM9", "--text", "AT", "--timeout", value]
        )
        assert payload["exit_code"] == 2
        assert payload["error"]["code"] == "usage"


def test_non_finite_read_for_is_rejected(cli, monkeypatch) -> None:
    _prevent_hanging_read(monkeypatch)
    _install(monkeypatch, FakeTransport())
    payload = cli(["serial", "send", "--port", "COM9", "--text", "AT", "--read-for", "nan"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


def test_timeout_zero_still_allowed(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport())
    payload = cli(
        ["serial", "send", "--port", "COM9", "--text", "AT", "--expect", "OK", "--timeout", "0"]
    )
    assert payload["exit_code"] == 4
    assert payload["error"]["code"] == "expect_mismatch"
    assert transport.closed is True


def test_open_failure_maps_to_device_unavailable(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport(open_error=OSError("拒绝访问")))
    payload = cli(["serial", "send", "--port", "COM9", "--text", "AT", "--read-for", "0"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert "COM9" in payload["error"]["message"]
    assert transport.closed is True


def test_write_failure_maps_to_device_unavailable_and_closes(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport(write_error=OSError("写失败")))
    payload = cli(["serial", "send", "--port", "COM9", "--text", "AT", "--read-for", "0"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert transport.closed is True


def test_read_failure_maps_to_device_unavailable_and_closes(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport(read_error=OSError("读失败")))
    payload = cli(
        ["serial", "send", "--port", "COM9", "--text", "AT", "--expect", "OK", "--timeout", "0.1"]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert transport.closed is True


# ---------------------------------------------------------------------------
# modem
# ---------------------------------------------------------------------------


def test_modem_reports_normalized_lines(cli, monkeypatch) -> None:
    transport = _install(
        monkeypatch,
        FakeTransport(modem={"cts": True, "dsr": False, "ri": True, "cd": False}),
    )
    payload = cli(["serial", "modem", "--port", "COM9"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "serial.modem"
    assert payload["data"] == {"cts": True, "dsr": False, "ring": True, "rlsd": False}
    assert transport.config.port == "COM9"
    assert transport.closed is True


def test_open_transport_construction_failure_maps_to_device_unavailable(
    cli, monkeypatch
) -> None:
    def _broken_factory():
        raise OSError("驱动不可用")

    monkeypatch.setattr(serial_io, "open_transport", _broken_factory)
    payload = cli(["serial", "send", "--port", "COM9", "--text", "AT", "--read-for", "0"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"


def test_modem_open_failure_maps_to_device_unavailable(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport(open_error=OSError("占用")))
    payload = cli(["serial", "modem", "--port", "COM9"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert transport.closed is True


# ---------------------------------------------------------------------------
# 架构回归：CLI 导入不拉进 GUI/pyserial
# ---------------------------------------------------------------------------


def test_cli_imports_do_not_load_gui_or_pyserial() -> None:
    modules = ("app.io.serial_worker", "serial", "PyQt6")
    for module in ("app.cli.main", "app.cli.commands.serial"):
        code = (
            f"import sys; import {module}; "
            f"bad = [m for m in {modules!r} if m in sys.modules]; "
            "print(bad); raise SystemExit(1 if bad else 0)"
        )
        process = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert process.returncode == 0, process.stdout + process.stderr
        assert process.stdout.strip() == "[]"


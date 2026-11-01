"""``serial`` 子命令：端口枚举、send 与 modem 的 CLI 契约。

真实串口不在测试里打开：``serial_io.open_transport`` / ``serial_io.list_ports`` 被
monkeypatch 成内存 transport，验证参数映射、读写顺序、退出码与信封字段。
"""

from __future__ import annotations

from app.cli.transports import serial_io
from app.io.serial_worker import PortInfo


class FakeTransport:
    """实现 ``SerialTransport`` 协议的内存替身，记录全部调用。"""

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
        if self._open_error is not None:
            raise self._open_error
        self.config = config

    def close(self):
        self.closed = True

    def read(self, size, timeout):
        self.reads.append(timeout)
        if self._read_error is not None:
            raise self._read_error
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        if self._write_error is not None:
            raise self._write_error
        self.writes.append(bytes(data))
        return len(data)

    def set_dtr(self, value):
        self.dtr = value

    def set_rts(self, value):
        self.rts = value

    def send_break(self, duration=0.25):
        self.broke = True

    def modem_status(self):
        return dict(self._modem)


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
            PortInfo("COM9", "USB Serial", "USB VID:PID=1234:5678", 0x1234, 0x5678, "SN1"),
            PortInfo("COM3", "Bluetooth Link", "BTHENUM", None, None, None),
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
            PortInfo("COM9", "USB Serial", "USB VID:PID=1234:5678", None, None, None),
            PortInfo("COM3", "Bluetooth Link", "BTHENUM", None, None, None),
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


def test_modem_open_failure_maps_to_device_unavailable(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport(open_error=OSError("占用")))
    payload = cli(["serial", "modem", "--port", "COM9"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert transport.closed is True

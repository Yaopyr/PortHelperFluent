"""``spp`` 子命令：经典蓝牙扫描、配对感知发送与 COM 备用通道的 CLI 契约。

真实蓝牙/串口都不参与测试：``spp_io.new_backend`` 换成自包含内存后端，
``serial_io.open_transport`` 换成 Task 11 风格的 ``FakeTransport``。
扫描/读取的等待都用短时长（0.1 秒级）收敛，不触碰任何真实硬件。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from app.cli.errors import CliError
from app.cli.transports import serial_io, spp_io

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_ADDRESS = "AA:BB:CC:DD:EE:FF"
_OTHER_ADDRESS = "11:22:33:44:55:66"


# ---------------------------------------------------------------------------
# 内存后端与设备
# ---------------------------------------------------------------------------


def _device(name="Phone", address=_ADDRESS, paired=False, device_id=None):
    """构造真实的 ``ClassicDeviceInfo``（惰性导入，模块级不加载 PyQt6/WinRT）。"""
    from app.io.spp_worker import ClassicDeviceInfo

    return ClassicDeviceInfo(
        name=name,
        address=address,
        paired=paired,
        device_id=device_id if device_id is not None else f"id-{address}",
    )


class FakeSppBackend:
    """实现 ``SppBackend`` 协议的内存替身，按顺序记录全部调用。"""

    def __init__(
        self,
        *,
        devices=(),
        pair_status="PAIRED",
        prompt_kind="pin",
        scan_error=None,
        pair_error=None,
        connect_error=None,
        send_error=None,
        reply_chunks=(),
        disconnect_after_send=False,
    ) -> None:
        self.devices = list(devices)
        self.calls: list[tuple] = []
        self.prompts: list[tuple[str, str | None]] = []
        self.sent: list[bytes] = []
        self.stop_calls = 0
        self.data_callback = None
        self.prompt_callback = None
        self.disconnect_callback = None
        self._pair_status = pair_status
        self._prompt_kind = prompt_kind
        self._scan_error = scan_error
        self._pair_error = pair_error
        self._connect_error = connect_error
        self._send_error = send_error
        self._reply_chunks = [bytes(chunk) for chunk in reply_chunks]
        self._disconnect_after_send = disconnect_after_send

    def set_data_callback(self, callback):
        self.calls.append(("set_data_callback",))
        self.data_callback = callback

    def set_prompt_callback(self, callback):
        self.calls.append(("set_prompt_callback",))
        self.prompt_callback = callback

    def set_disconnect_callback(self, callback):
        self.calls.append(("set_disconnect_callback",))
        self.disconnect_callback = callback

    async def scan(self, duration, on_device, stop):
        self.calls.append(("scan", duration))
        if self._scan_error is not None:
            raise self._scan_error
        for info in self.devices:
            on_device(info)
            if stop():
                self.stop_calls += 1
                return

    async def pair(self, device):
        self.calls.append(("pair", device.device_id))
        if self._pair_error is not None:
            raise self._pair_error
        if device.paired or self._pair_status == "ALREADY_PAIRED":
            return "ALREADY_PAIRED"
        if self._pair_status == "PAIRED":
            answer = None
            if self.prompt_callback is not None:
                answer = self.prompt_callback(self._prompt_kind, "配对提示")
            self.prompts.append((self._prompt_kind, answer))
        return self._pair_status

    async def connect(self, device):
        self.calls.append(("connect", device.device_id))
        if self._connect_error is not None:
            raise self._connect_error

    async def send(self, data):
        self.calls.append(("send", bytes(data)))
        self.sent.append(bytes(data))
        if self._send_error is not None:
            raise self._send_error
        if self.data_callback is not None:
            for chunk in self._reply_chunks:
                self.data_callback(chunk)
        if self._disconnect_after_send and self.disconnect_callback is not None:
            asyncio.get_running_loop().call_soon(
                self.disconnect_callback, "设备断电"
            )
        return len(data)

    async def close(self):
        self.calls.append(("close",))


class FakeTransport:
    """``SerialTransport`` 协议的内存替身，按顺序记录全部调用。"""

    def __init__(self, chunks=(), *, modem=None, open_error=None):
        self.chunks = list(chunks)
        self.closed = False
        self.writes: list[bytes] = []
        self.calls: list[tuple] = []
        self.config = None
        self._modem = (
            modem if modem is not None else {"cts": True, "dsr": False, "ri": False, "cd": True}
        )
        self._open_error = open_error

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
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        self.calls.append(("write", bytes(data)))
        self.writes.append(bytes(data))
        return len(data)

    def modem_status(self):
        self.calls.append(("modem",))
        return dict(self._modem)


def _install(monkeypatch, backend: FakeSppBackend) -> FakeSppBackend:
    """所有 ``spp`` 命令都通过 ``spp_io.new_backend`` 拿后端。"""
    monkeypatch.setattr(spp_io, "new_backend", lambda: backend)
    return backend


def _install_transport(monkeypatch, transport: FakeTransport) -> FakeTransport:
    monkeypatch.setattr(serial_io, "open_transport", lambda: transport)
    return transport


def _count(backend: FakeSppBackend, name: str) -> int:
    return sum(1 for call in backend.calls if call[0] == name)


# ---------------------------------------------------------------------------
# scan：传输层
# ---------------------------------------------------------------------------


def test_scan_returns_classic_devices() -> None:
    fake = FakeSppBackend(
        devices=[_device("Phone", _ADDRESS, True, "id-1"), _device("Thermo", _OTHER_ADDRESS)]
    )

    devices = asyncio.run(spp_io.scan(fake, 0.1))

    assert [device.address for device in devices] == [_ADDRESS, _OTHER_ADDRESS]
    assert devices[0].name == "Phone"
    assert devices[0].paired is True
    assert devices[0].device_id == "id-1"
    assert fake.calls == [("scan", 0.1)]


def test_scan_filters_case_insensitively_and_dedupes_keeping_first() -> None:
    fake = FakeSppBackend(
        devices=[
            _device("Phone", _ADDRESS, True, "id-1"),
            _device("Thermo", _OTHER_ADDRESS, False, "id-2"),
            _device("Phone-v2", _ADDRESS, True, "id-1"),
        ]
    )

    devices = asyncio.run(spp_io.scan(fake, 0.1))
    assert [device.name for device in devices] == ["Phone", "Thermo"]

    by_name = asyncio.run(spp_io.scan(fake, 0.1, filter_text="THERMO"))
    assert [device.address for device in by_name] == [_OTHER_ADDRESS]

    by_address = asyncio.run(spp_io.scan(fake, 0.1, filter_text="aa:bb"))
    assert [device.name for device in by_address] == ["Phone"]

    paired_only = asyncio.run(spp_io.scan(fake, 0.1, paired_only=True))
    assert [device.name for device in paired_only] == ["Phone"]


def test_scan_backend_error_is_device_unavailable() -> None:
    fake = FakeSppBackend(scan_error=OSError("适配器关闭"))

    with pytest.raises(CliError) as excinfo:
        asyncio.run(spp_io.scan(fake, 0.1))
    assert excinfo.value.code == "device_unavailable"
    assert excinfo.value.exit_code == 3


# ---------------------------------------------------------------------------
# scan：CLI
# ---------------------------------------------------------------------------


def test_scan_command_reports_devices_count_and_text_format(cli, monkeypatch) -> None:
    _install(
        monkeypatch,
        FakeSppBackend(
            devices=[_device("Phone", _ADDRESS, True, "id-1"), _device("Thermo", _OTHER_ADDRESS)]
        ),
    )

    payload = cli(["spp", "scan", "--duration", "0.1"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "spp.scan"
    assert payload["data"]["count"] == 2
    assert payload["data"]["devices"] == [
        {
            "name": "Phone",
            "address": _ADDRESS,
            "paired": True,
            "device_id": "id-1",
        },
        {
            "name": "Thermo",
            "address": _OTHER_ADDRESS,
            "paired": False,
            "device_id": f"id-{_OTHER_ADDRESS}",
        },
    ]

    text = cli(["spp", "scan", "--duration", "0.1", "--format", "text"])
    assert text["exit_code"] == 0
    assert '"count": 2' in text["raw"]


def test_scan_paired_only_and_filter(cli, monkeypatch) -> None:
    _install(
        monkeypatch,
        FakeSppBackend(
            devices=[_device("Phone", _ADDRESS, True, "id-1"), _device("Thermo", _OTHER_ADDRESS)]
        ),
    )

    payload = cli(
        ["spp", "scan", "--duration", "0.1", "--filter", "thermo", "--paired-only"]
    )
    assert payload["exit_code"] == 0
    assert payload["data"] == {"devices": [], "count": 0}

    payload = cli(["spp", "scan", "--duration", "0.1", "--paired-only"])
    assert payload["data"]["count"] == 1
    assert payload["data"]["devices"][0]["name"] == "Phone"


def test_scan_rejects_invalid_duration_and_blank_filter(cli) -> None:
    for duration in ("0", "nan", "3601"):
        payload = cli(["spp", "scan", "--duration", duration])
        assert payload["exit_code"] == 2
        assert payload["error"]["code"] == "usage"

    payload = cli(["spp", "scan", "--duration", "0.1", "--filter", "   "])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# send：配对、收发与断开
# ---------------------------------------------------------------------------


def test_send_pairs_with_pin_and_matches_reply(cli, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        FakeSppBackend(
            devices=[_device("Phone", _ADDRESS, False, "id-1")],
            prompt_kind="pin",
            reply_chunks=(b"O", b"K"),
        ),
    )

    payload = cli(
        [
            "spp",
            "send",
            "--address",
            _ADDRESS,
            "--scan-duration",
            "0.1",
            "--text",
            "AT",
            "--pin",
            "1234",
            "--expect",
            "OK",
            "--timeout",
            "0.1",
            "--read-for",
            "0",
        ]
    )

    assert payload["exit_code"] == 0
    assert payload["command"] == "spp.send"
    data = payload["data"]
    assert data["matched"] is True
    assert data["sent"] == {"bytes": 2, "writes": 1}
    assert data["received"] == {"hex": "4f4b", "text": "OK", "size": 2, "chunks": 2}
    assert data["device"] == {
        "name": "Phone",
        "address": _ADDRESS,
        "paired": False,
        "device_id": "id-1",
    }
    # PIN 提示回调收到 kind="pin"，回答就是 --pin 的值。
    assert fake.prompts == [("pin", "1234")]
    assert fake.sent == [b"AT"]
    assert _count(fake, "pair") == 1
    assert _count(fake, "connect") == 1
    assert _count(fake, "close") == 1


def test_send_confirmation_prompt_answers_yes(cli, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        FakeSppBackend(devices=[_device("Phone", _ADDRESS)], prompt_kind="confirm"),
    )

    payload = cli(
        [
            "spp",
            "send",
            "--device-id",
            "id-1",
            "--text",
            "AT",
            "--confirm",
            "--read-for",
            "0",
        ]
    )

    assert payload["exit_code"] == 0
    assert fake.prompts == [("confirm", "yes")]
    assert _count(fake, "pair") == 1


def test_send_paired_device_skips_pairing_without_prompt() -> None:
    fake = FakeSppBackend()
    device = _device("Phone", _ADDRESS, True, "id-1")

    result = asyncio.run(spp_io.send(fake, device, b"AT", read_for=0))

    assert result["matched"] is False
    assert result["device"]["paired"] is True
    assert fake.prompts == []
    assert _count(fake, "pair") == 0
    assert _count(fake, "connect") == 1
    assert _count(fake, "send") == 1
    assert _count(fake, "close") == 1


def test_send_already_paired_status_does_not_prompt(cli, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        FakeSppBackend(devices=[_device("Phone", _ADDRESS)], pair_status="ALREADY_PAIRED"),
    )

    payload = cli(
        [
            "spp",
            "send",
            "--device-id",
            "id-1",
            "--text",
            "AT",
            "--pin",
            "1234",
            "--read-for",
            "0",
        ]
    )

    assert payload["exit_code"] == 0
    assert fake.prompts == []
    assert _count(fake, "pair") == 1
    assert _count(fake, "connect") == 1


def test_send_requires_pin_or_confirm_for_unpaired_address(cli, monkeypatch) -> None:
    fake = _install(
        monkeypatch, FakeSppBackend(devices=[_device("Phone", _ADDRESS, False, "id-1")])
    )

    payload = cli(
        ["spp", "send", "--address", _ADDRESS, "--scan-duration", "0.1", "--text", "AT"]
    )

    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert "--pin/--confirm" in payload["error"]["message"]
    assert _count(fake, "scan") == 1
    assert _count(fake, "pair") == 0
    assert _count(fake, "connect") == 0
    assert _count(fake, "send") == 0


def test_device_id_pair_unknown_resolves_already_paired(cli, monkeypatch) -> None:
    fake = _install(
        monkeypatch, FakeSppBackend(pair_status="ALREADY_PAIRED")
    )

    payload = cli(["spp", "send", "--device-id", "id-1", "--text", "AT", "--read-for", "0"])

    assert payload["exit_code"] == 0
    assert fake.prompts == []
    assert _count(fake, "pair") == 1
    assert _count(fake, "connect") == 1
    assert _count(fake, "send") == 1
    assert _count(fake, "close") == 1


def test_device_id_pair_unknown_rejection_hints_pin_confirm(cli, monkeypatch) -> None:
    fake = _install(
        monkeypatch, FakeSppBackend(pair_status="AUTHENTICATIONREJECTED")
    )

    payload = cli(["spp", "send", "--device-id", "id-1", "--text", "AT"])

    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert "--pin/--confirm" in payload["error"]["message"]
    assert _count(fake, "pair") == 1
    assert _count(fake, "connect") == 0
    assert _count(fake, "close") == 1


def test_send_pair_rejection_maps_to_device_unavailable(cli, monkeypatch) -> None:
    fake = _install(
        monkeypatch, FakeSppBackend(pair_status="AUTHENTICATIONREJECTED")
    )

    payload = cli(
        ["spp", "send", "--device-id", "id-1", "--text", "AT", "--pin", "1234"]
    )

    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert payload["error"]["message"] == "配对失败（设备拒绝配对）"
    assert _count(fake, "connect") == 0
    assert _count(fake, "close") == 1


def test_send_disconnect_during_read_exits_with_reason(cli, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        FakeSppBackend(devices=[_device("Phone", _ADDRESS, True)], disconnect_after_send=True),
    )

    payload = cli(
        [
            "spp",
            "send",
            "--address",
            _ADDRESS,
            "--scan-duration",
            "0.1",
            "--text",
            "AT",
            "--read-for",
            "0.2",
            "--timeout",
            "0.2",
        ]
    )

    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert "断开" in payload["error"]["message"]
    assert "设备断电" in payload["error"]["message"]
    assert _count(fake, "close") == 1


def test_send_repeat_writes_and_reports_totals(cli, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        FakeSppBackend(devices=[_device("Phone", _ADDRESS, True)]),
    )

    payload = cli(
        [
            "spp",
            "send",
            "--address",
            _ADDRESS,
            "--scan-duration",
            "0.1",
            "--text",
            "AT",
            "--repeat",
            "2",
            "--interval-ms",
            "0",
            "--read-for",
            "0",
        ]
    )

    assert payload["exit_code"] == 0
    assert payload["data"]["sent"] == {"bytes": 4, "writes": 2}
    assert fake.sent == [b"AT", b"AT"]


def test_send_expect_hex_matches_raw_bytes(cli, monkeypatch) -> None:
    _install(
        monkeypatch,
        FakeSppBackend(
            devices=[_device("Phone", _ADDRESS, True)], reply_chunks=(b"\x01\x02",)
        ),
    )

    payload = cli(
        [
            "spp",
            "send",
            "--address",
            _ADDRESS,
            "--scan-duration",
            "0.1",
            "--text",
            "AT",
            "--expect-hex",
            "0102",
            "--timeout",
            "0.1",
            "--read-for",
            "0",
        ]
    )

    assert payload["exit_code"] == 0
    assert payload["data"]["matched"] is True
    assert payload["data"]["received"]["hex"] == "0102"


def test_send_format_text_is_readable(cli, monkeypatch) -> None:
    _install(
        monkeypatch,
        FakeSppBackend(devices=[_device("Phone", _ADDRESS, True)]),
    )

    payload = cli(
        [
            "spp",
            "send",
            "--address",
            _ADDRESS,
            "--scan-duration",
            "0.1",
            "--text",
            "AT",
            "--read-for",
            "0",
            "--format",
            "text",
        ]
    )

    assert payload["exit_code"] == 0
    assert "matched" in payload["raw"]


def test_send_expect_timeout_exits_4_and_closes(cli, monkeypatch) -> None:
    fake = _install(monkeypatch, FakeSppBackend())

    payload = cli(
        [
            "spp",
            "send",
            "--device-id",
            "id-1",
            "--text",
            "AT",
            "--pin",
            "1234",
            "--expect",
            "OK",
            "--timeout",
            "0.1",
        ]
    )

    assert payload["exit_code"] == 4
    assert payload["error"]["code"] == "expect_mismatch"
    assert _count(fake, "close") == 1


# ---------------------------------------------------------------------------
# send：地址解析
# ---------------------------------------------------------------------------


def test_address_resolution_stops_scan_early() -> None:
    target = _device("Phone", _ADDRESS, True, "id-target")
    fake = FakeSppBackend(devices=[_device("Other", _OTHER_ADDRESS), target])

    device = asyncio.run(spp_io.resolve_address(fake, _ADDRESS.lower(), 0.1))

    assert device.device_id == "id-target"
    assert fake.stop_calls == 1


def test_address_not_found_exits_3(cli, monkeypatch) -> None:
    fake = _install(monkeypatch, FakeSppBackend(devices=[_device("Other", _OTHER_ADDRESS)]))

    payload = cli(
        [
            "spp",
            "send",
            "--address",
            _ADDRESS,
            "--scan-duration",
            "0.1",
            "--text",
            "AT",
            "--pin",
            "1234",
        ]
    )

    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"
    assert _ADDRESS in payload["error"]["message"]
    assert _count(fake, "pair") == 0
    assert _count(fake, "connect") == 0


def test_device_id_skips_scan(cli, monkeypatch) -> None:
    fake = _install(monkeypatch, FakeSppBackend(reply_chunks=(b"OK",)))

    payload = cli(
        [
            "spp",
            "send",
            "--device-id",
            "id-1",
            "--text",
            "AT",
            "--pin",
            "1234",
            "--expect",
            "OK",
            "--timeout",
            "0.1",
            "--read-for",
            "0",
        ]
    )

    assert payload["exit_code"] == 0
    assert payload["data"]["device"] == {
        "name": "",
        "address": "",
        "paired": False,
        "device_id": "id-1",
    }
    assert _count(fake, "scan") == 0


# ---------------------------------------------------------------------------
# send：参数校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["spp", "send", "--device-id", "id-1"],  # 缺输入
        ["spp", "send", "--address", _ADDRESS, "--device-id", "id-1", "--text", "AT"],
        ["spp", "send", "--device-id", "id-1", "--text", "AT", "--pin", "12ab"],
        ["spp", "send", "--device-id", "id-1", "--text", "AT", "--timeout", "0"],
        ["spp", "send", "--device-id", "id-1", "--text", "AT", "--timeout", "nan"],
        ["spp", "send", "--device-id", "id-1", "--text", "AT", "--timeout", "3601"],
        ["spp", "send", "--device-id", "id-1", "--text", "AT", "--read-for", "-1"],
        ["spp", "send", "--device-id", "id-1", "--text", "AT", "--read-for", "nan"],
        ["spp", "send", "--device-id", "id-1", "--text", "AT", "--repeat", "0"],
        ["spp", "send", "--device-id", "id-1", "--text", "AT", "--interval-ms", "-1"],
        ["spp", "send", "--device-id", "id-1", "--text", "AT", "--scan-duration", "0"],
        ["spp", "send", "--device-id", "id-1", "--text", "AT", "--scan-duration", "nan"],
        ["spp", "send", "--device-id", " ", "--text", "AT"],
    ],
)
def test_send_argument_validation_is_usage_error(cli, monkeypatch, argv) -> None:
    _install(monkeypatch, FakeSppBackend())

    payload = cli(argv)

    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# com：复用串口 transport
# ---------------------------------------------------------------------------


def test_com_channel_uses_serial_transport(cli, monkeypatch) -> None:
    transport = _install_transport(monkeypatch, FakeTransport([b"OK"]))

    payload = cli(["spp", "com", "--port", "COM9", "--text", "AT", "--expect", "OK"])

    assert payload["exit_code"] == 0
    assert payload["command"] == "spp.com"
    data = payload["data"]
    assert data["matched"] is True
    assert data["sent"] == {"bytes": 2, "writes": 1}
    assert data["received"] == {"hex": "4f4b", "text": "OK", "size": 2}
    assert data["modem"] == {"cts": True, "dsr": False, "ring": False, "rlsd": True}
    assert transport.writes == [b"AT"]
    assert transport.config.port == "COM9"
    assert transport.closed is True


def test_com_format_text_is_readable(cli, monkeypatch) -> None:
    transport = _install_transport(monkeypatch, FakeTransport([b"OK"]))

    payload = cli(
        [
            "spp",
            "com",
            "--port",
            "COM9",
            "--text",
            "AT",
            "--expect",
            "OK",
            "--format",
            "text",
        ]
    )

    assert payload["exit_code"] == 0
    assert "matched" in payload["raw"]
    assert transport.closed is True


def test_com_open_failure_maps_to_device_unavailable(cli, monkeypatch) -> None:
    transport = _install_transport(
        monkeypatch, FakeTransport(open_error=OSError("拒绝访问"))
    )

    payload = cli(["spp", "com", "--port", "COM9", "--text", "AT", "--expect", "OK"])

    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert "COM9" in payload["error"]["message"]
    assert transport.closed is True


def test_com_expect_timeout_exits_4_and_closes(cli, monkeypatch) -> None:
    transport = _install_transport(monkeypatch, FakeTransport())

    payload = cli(
        [
            "spp",
            "com",
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
    assert payload["error"]["code"] == "expect_mismatch"
    assert transport.closed is True


def test_com_reads_without_expect_for_fixed_window(cli, monkeypatch) -> None:
    transport = _install_transport(monkeypatch, FakeTransport([b"RX"]))

    payload = cli(
        ["spp", "com", "--port", "COM9", "--text", "AT", "--read-for", "0"]
    )

    assert payload["exit_code"] == 0
    assert payload["data"]["matched"] is False
    assert payload["data"]["received"] == {"hex": "", "text": "", "size": 0}
    assert transport.writes == [b"AT"]
    assert transport.closed is True


# ---------------------------------------------------------------------------
# 架构回归：CLI 导入不拉进 WinRT/PyQt6
# ---------------------------------------------------------------------------


def test_cli_import_does_not_import_spp_stack() -> None:
    code = (
        "import sys\n"
        "import app.cli.main, app.cli.commands.spp, app.cli.transports.spp_io\n"
        "print('app.io.spp_worker' in sys.modules, 'winrt' in sys.modules,"
        " 'PyQt6' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False False False"

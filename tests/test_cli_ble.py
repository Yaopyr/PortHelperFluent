"""``ble`` 子命令：扫描、服务树、特征值读写与通知流的 CLI 契约。

真实蓝牙与本机时钟都不参与测试：``ble_io.new_backend`` 换成自包含内存后端；
通知流用 ``--max-events``/``--until-hex`` 快速收敛，只有直调用例保留
``duration=0.1`` 的真实等待，用来验证截止时间与汇总字段。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.cli.errors import CliError
from app.cli.transports import ble_io

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_ADDRESS = "AA:BB:CC:DD:EE:FF"
_CHAR = "FFE1"


# ---------------------------------------------------------------------------
# 内存后端
# ---------------------------------------------------------------------------


def _device(name="Sensor", address="AA:BB", rssi=-50):
    """构造真实的 ``BleDeviceInfo``（惰性导入，模块级不加载 PyQt6）。"""
    from app.io.ble_worker import BleDeviceInfo

    return BleDeviceInfo(name=name, address=address, rssi=rssi, seen_at=0.0)


def _service(
    uuid="0000ffe0-0000-1000-8000-00805f9b34fb",
    characteristics=None,
):
    """构造真实的 ``BleServiceInfo``；默认是 FFE0 + FFE1（read/write/notify）。"""
    from app.io.ble_worker import BleCharInfo, BleServiceInfo

    if characteristics is None:
        characteristics = (
            ("0000ffe1-0000-1000-8000-00805f9b34fb", ("read", "write", "notify")),
        )
    return BleServiceInfo(
        uuid=uuid,
        characteristics=tuple(
            BleCharInfo(uuid=char_uuid, properties=tuple(props))
            for char_uuid, props in characteristics
        ),
    )


class FakeBleBackend:
    """实现 ``BleBackend`` 协议的内存替身，按顺序记录全部调用。"""

    def __init__(
        self,
        *,
        devices=(),
        services=(),
        mtu=23,
        read_data=b"",
        notifications=(),
        scan_error=None,
        connect_error=None,
        connect_delay=0.0,
        services_error=None,
        read_error=None,
        write_error=None,
        subscribe_error=None,
        unsubscribe_error=None,
    ) -> None:
        self.devices = list(devices)
        self.services_list = list(services)
        self.notifications = list(notifications)
        self._mtu = mtu
        self._read_data = bytes(read_data)
        self._scan_error = scan_error
        self._connect_error = connect_error
        self._connect_delay = connect_delay
        self._services_error = services_error
        self._read_error = read_error
        self._write_error = write_error
        self._subscribe_error = subscribe_error
        self._unsubscribe_error = unsubscribe_error
        self.calls: list[tuple] = []
        self.disconnected = False
        self.connected = False
        self.disconnect_callback = None

    def set_disconnect_callback(self, callback):
        self.calls.append(("set_disconnect_callback",))
        self.disconnect_callback = callback

    async def scan(self, duration, on_device, stop):
        self.calls.append(("scan", duration))
        if self._scan_error is not None:
            raise self._scan_error
        for info in self.devices:
            on_device(info)

    async def connect(self, address):
        self.calls.append(("connect", address))
        if self._connect_delay:
            await asyncio.sleep(self._connect_delay)
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def disconnect(self):
        self.calls.append(("disconnect",))
        self.connected = False
        self.disconnected = True

    async def services(self):
        self.calls.append(("services",))
        if self._services_error is not None:
            raise self._services_error
        return list(self.services_list)

    def mtu(self):
        self.calls.append(("mtu",))
        return self._mtu

    async def read(self, char_uuid):
        self.calls.append(("read", char_uuid))
        if self._read_error is not None:
            raise self._read_error
        return bytes(self._read_data)

    async def write(self, char_uuid, data, response):
        self.calls.append(("write", char_uuid, bytes(data), response))
        if self._write_error is not None:
            raise self._write_error

    async def set_notify(self, char_uuid, enabled, on_data):
        self.calls.append(("set_notify", char_uuid, enabled))
        if not enabled:
            if self._unsubscribe_error is not None:
                raise self._unsubscribe_error
            return
        if self._subscribe_error is not None:
            raise self._subscribe_error
        for char, data in self.notifications:
            on_data(char, data)


def _install(monkeypatch, backend: FakeBleBackend) -> FakeBleBackend:
    """所有 ``ble`` 命令都通过 ``ble_io.new_backend`` 拿后端。"""
    monkeypatch.setattr(ble_io, "new_backend", lambda: backend)
    return backend


def _count(backend: FakeBleBackend, name: str) -> int:
    return sum(1 for call in backend.calls if call[0] == name)


def _events(payload: dict) -> list[dict]:
    """把 ``raw`` 里的 NDJSON 行解析成事件列表（最后一项是标准信封）。"""
    lines = [line for line in payload["raw"].splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


_FORTY_BYTES = " ".join(f"{index:02X}" for index in range(40))


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def test_scan_returns_devices_in_scan_order() -> None:
    fake = FakeBleBackend(
        devices=[_device("Sensor", "AA:BB", -50), _device("Thermo", "11:22", -70)]
    )

    devices = asyncio.run(ble_io.scan(fake, 0.1))
    assert [device.address for device in devices] == ["AA:BB", "11:22"]
    assert devices[0].name == "Sensor"
    assert devices[0].rssi == -50
    assert fake.calls == [("scan", 0.1)]
    assert fake.connected is False


def test_scan_filters_by_name_or_address_case_insensitively() -> None:
    fake = FakeBleBackend(
        devices=[
            _device("Sensor", "AA:BB", -50),
            _device("Thermo", "11:22", -70),
            _device("sensor-2", "33:44", -60),
        ]
    )

    assert [d.address for d in asyncio.run(ble_io.scan(fake, 0.1, "aa"))] == ["AA:BB"]
    assert [d.address for d in asyncio.run(ble_io.scan(fake, 0.1, "SENSOR"))] == [
        "AA:BB",
        "33:44",
    ]


def test_scan_dedupes_same_address_keeping_last_info() -> None:
    fake = FakeBleBackend(
        devices=[
            _device("Sensor", "AA:BB", -50),
            _device("Thermo", "11:22", -70),
            _device("Sensor-v2", "AA:BB", -40),
        ]
    )

    devices = asyncio.run(ble_io.scan(fake, 0.1))
    assert [device.address for device in devices] == ["AA:BB", "11:22"]
    assert devices[0].name == "Sensor-v2"
    assert devices[0].rssi == -40
    assert devices[1].name == "Thermo"


def test_scan_backend_error_is_device_unavailable() -> None:
    fake = FakeBleBackend(scan_error=OSError("适配器关闭"))

    with pytest.raises(CliError) as excinfo:
        asyncio.run(ble_io.scan(fake, 0.1))
    assert excinfo.value.code == "device_unavailable"
    assert excinfo.value.exit_code == 3


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------


def test_services_maps_labels_properties_and_mtu() -> None:
    fake = FakeBleBackend(mtu=23, services=[_service()])

    result = asyncio.run(ble_io.services(fake, _ADDRESS))

    assert result["mtu"] == 23
    assert result["services"] == [
        {
            "uuid": "0000ffe0-0000-1000-8000-00805f9b34fb",
            "label": "FFE0（自定义透传）",
            "characteristics": [
                {
                    "uuid": "0000ffe1-0000-1000-8000-00805f9b34fb",
                    "label": "FFE1（FFE0 数据）",
                    "properties": ["read", "write", "notify"],
                    "subscribed": False,
                }
            ],
        }
    ]
    assert ("connect", _ADDRESS) in fake.calls
    assert fake.disconnected is True
    assert _count(fake, "mtu") == 1


def test_services_failure_maps_and_disconnects() -> None:
    fake = FakeBleBackend(services_error=OSError("读取服务失败"))

    with pytest.raises(CliError) as excinfo:
        asyncio.run(ble_io.services(fake, _ADDRESS))
    assert excinfo.value.code == "device_unavailable"
    assert excinfo.value.exit_code == 3
    assert fake.disconnected is True


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read_returns_hex_text_size_and_char() -> None:
    fake = FakeBleBackend(read_data=b"\x01\x02")

    result = asyncio.run(ble_io.read(fake, _ADDRESS, _CHAR))

    assert result == {"char": _CHAR, "hex": "0102", "text": "\x01\x02", "size": 2}
    assert ("read", _CHAR) in fake.calls
    assert fake.disconnected is True


def test_read_connect_timeout_is_device_unavailable() -> None:
    fake = FakeBleBackend(connect_error=TimeoutError())

    with pytest.raises(CliError) as excinfo:
        asyncio.run(ble_io.read(fake, _ADDRESS, _CHAR))
    assert excinfo.value.code == "device_unavailable"
    assert excinfo.value.exit_code == 3
    assert _count(fake, "read") == 0
    assert fake.disconnected is True


def test_read_connect_exception_is_device_unavailable() -> None:
    fake = FakeBleBackend(connect_error=OSError("设备不在范围内"))

    with pytest.raises(CliError) as excinfo:
        asyncio.run(ble_io.read(fake, _ADDRESS, _CHAR))
    assert excinfo.value.code == "device_unavailable"
    assert excinfo.value.exit_code == 3
    assert "设备不在范围内" in excinfo.value.message


def test_read_failure_maps_and_disconnects() -> None:
    fake = FakeBleBackend(read_error=OSError("读失败"))

    with pytest.raises(CliError) as excinfo:
        asyncio.run(ble_io.read(fake, _ADDRESS, _CHAR))
    assert excinfo.value.code == "device_unavailable"
    assert fake.disconnected is True


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def test_write_chunks_by_mtu_and_reports_totals() -> None:
    fake = FakeBleBackend(mtu=23)

    result = asyncio.run(ble_io.write(fake, _ADDRESS, _CHAR, bytes(range(40))))

    assert result == {"chunks": 2, "bytes": 40, "mtu": 23, "response": True}
    writes = [call for call in fake.calls if call[0] == "write"]
    assert writes == [
        ("write", _CHAR, bytes(range(40))[:20], True),
        ("write", _CHAR, bytes(range(40))[20:], True),
    ]
    assert fake.disconnected is True


def test_write_is_not_chunked_when_mtu_chunk_is_off() -> None:
    fake = FakeBleBackend(mtu=23)

    result = asyncio.run(
        ble_io.write(fake, _ADDRESS, _CHAR, bytes(range(40)), mtu_chunk=False)
    )

    assert result == {"chunks": 1, "bytes": 40, "mtu": None, "response": True}
    assert [call[1:] for call in fake.calls if call[0] == "write"] == [
        (_CHAR, bytes(range(40)), True)
    ]
    assert _count(fake, "mtu") == 0


def test_write_repeat_multiplies_chunks_and_bytes() -> None:
    fake = FakeBleBackend(mtu=23)

    result = asyncio.run(
        ble_io.write(fake, _ADDRESS, _CHAR, bytes(range(40)), repeat=2)
    )

    assert result == {"chunks": 4, "bytes": 80, "mtu": 23, "response": True}
    assert _count(fake, "write") == 4


def test_write_repeat_sleeps_only_between_writes(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ble_io.asyncio, "sleep", fake_sleep)
    fake = FakeBleBackend(mtu=23)

    asyncio.run(
        ble_io.write(fake, _ADDRESS, _CHAR, b"AB", repeat=3, interval_ms=100)
    )

    assert sleeps == [0.1, 0.1]
    assert _count(fake, "write") == 3


def test_write_failure_maps_and_disconnects() -> None:
    fake = FakeBleBackend(write_error=OSError("写入被拒绝"))

    with pytest.raises(CliError) as excinfo:
        asyncio.run(ble_io.write(fake, _ADDRESS, _CHAR, b"AB"))
    assert excinfo.value.code == "device_unavailable"
    assert excinfo.value.exit_code == 3
    assert fake.disconnected is True


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------


def test_notify_emits_notifications_and_end() -> None:
    fake = FakeBleBackend(notifications=[(_CHAR, b"\x01"), (_CHAR, b"OK")])
    events: list[dict] = []

    result = asyncio.run(
        ble_io.notify(fake, _ADDRESS, _CHAR, duration=0.1, emit=events.append)
    )

    assert [event["event"] for event in events] == ["notification", "notification", "end"]
    assert events[0]["char"] == _CHAR
    assert events[0]["hex"] == "01"
    assert events[0]["text"] == "\x01"
    assert events[0]["size"] == 1
    assert isinstance(events[0]["at"], str)
    assert events[1]["text"] == "OK"
    assert set(events[-1]) == {
        "event",
        "duration_s",
        "bytes",
        "events",
        "matched",
        "at",
    }
    assert result["bytes"] == 3
    assert result["events"] == 2
    assert result["matched"] is False
    assert 0 <= result["duration_s"] < 1.0


def test_notify_until_hex_match_ends_early() -> None:
    fake = FakeBleBackend(notifications=[(_CHAR, b"\x01"), (_CHAR, b"OK")])
    events: list[dict] = []

    result = asyncio.run(
        ble_io.notify(
            fake,
            _ADDRESS,
            _CHAR,
            duration=0.1,
            emit=events.append,
            matcher=lambda data: b"OK" in data,
        )
    )

    assert result["matched"] is True
    assert result["events"] == 2
    assert result["bytes"] == 3
    assert events[-1]["event"] == "end"


def test_notify_max_events_ends_early() -> None:
    fake = FakeBleBackend(
        notifications=[(_CHAR, b"1"), (_CHAR, b"2"), (_CHAR, b"3")]
    )
    events: list[dict] = []

    result = asyncio.run(
        ble_io.notify(fake, _ADDRESS, _CHAR, duration=0.1, emit=events.append, max_events=1)
    )

    assert result["bytes"] == 1
    assert result["events"] == 1
    assert result["matched"] is False
    assert [event["event"] for event in events] == ["notification", "end"]


def test_notify_max_bytes_truncates_last_notification() -> None:
    fake = FakeBleBackend(notifications=[(_CHAR, b"ABCD"), (_CHAR, b"EF")])
    events: list[dict] = []

    result = asyncio.run(
        ble_io.notify(fake, _ADDRESS, _CHAR, duration=0.1, emit=events.append, max_bytes=3)
    )

    assert result["bytes"] == 3
    assert result["events"] == 1
    assert events[0]["hex"] == "414243"
    assert events[0]["size"] == 3
    assert [event["event"] for event in events] == ["notification", "end"]


def test_notify_unsubscribes_and_disconnects() -> None:
    fake = FakeBleBackend(notifications=[(_CHAR, b"1")])

    asyncio.run(ble_io.notify(fake, _ADDRESS, _CHAR, duration=0.1, emit=lambda event: None))

    assert fake.calls[-2:] == [("set_notify", _CHAR, False), ("disconnect",)]
    assert fake.disconnected is True


def test_notify_subscribe_failure_maps_without_unsubscribe() -> None:
    fake = FakeBleBackend(subscribe_error=OSError("特征值不支持通知"))

    with pytest.raises(CliError) as excinfo:
        asyncio.run(
            ble_io.notify(fake, _ADDRESS, _CHAR, duration=0.1, emit=lambda event: None)
        )
    assert excinfo.value.code == "device_unavailable"
    assert excinfo.value.exit_code == 3
    assert _count(fake, "set_notify") == 1
    assert fake.disconnected is True


def test_notify_unsubscribe_failure_does_not_mask_result() -> None:
    fake = FakeBleBackend(
        notifications=[(_CHAR, b"1")], unsubscribe_error=OSError("取消订阅失败")
    )

    result = asyncio.run(
        ble_io.notify(fake, _ADDRESS, _CHAR, duration=0.1, emit=lambda event: None)
    )

    assert result["events"] == 1
    assert fake.disconnected is True


# ---------------------------------------------------------------------------
# CLI：scan / services / read
# ---------------------------------------------------------------------------


def test_scan_command_outputs_devices_and_count(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch,
        FakeBleBackend(
            devices=[_device("Sensor", "AA:BB", -50), _device("Thermo", "11:22", -70)]
        ),
    )

    payload = cli(["ble", "scan", "--duration", "0.1"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "ble.scan"
    assert payload["data"] == {
        "devices": [
            {"name": "Sensor", "address": "AA:BB", "rssi": -50},
            {"name": "Thermo", "address": "11:22", "rssi": -70},
        ],
        "count": 2,
    }
    assert backend.calls == [("scan", 0.1)]


def test_scan_command_filter_and_text_format(cli, monkeypatch) -> None:
    _install(
        monkeypatch,
        FakeBleBackend(
            devices=[_device("Sensor", "AA:BB", -50), _device("Thermo", "11:22", -70)]
        ),
    )

    payload = cli(["ble", "scan", "--duration", "0.1", "--filter", "thermo"])
    assert payload["data"]["count"] == 1
    assert payload["data"]["devices"][0]["address"] == "11:22"

    payload = cli(["ble", "scan", "--duration", "0.1", "--format", "text"])
    assert payload["exit_code"] == 0
    assert '"count": 2' in payload["raw"]


def test_services_command_reports_tree(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBleBackend(mtu=23, services=[_service()]))

    payload = cli(["ble", "services", "--address", _ADDRESS])
    assert payload["exit_code"] == 0
    assert payload["command"] == "ble.services"

    data = payload["data"]
    assert data["mtu"] == 23
    service = data["services"][0]
    assert service["label"] == "FFE0（自定义透传）"
    character = service["characteristics"][0]
    assert character["properties"] == ["read", "write", "notify"]
    assert character["subscribed"] is False
    assert ("connect", _ADDRESS) in backend.calls
    assert backend.disconnected is True


def test_read_command_returns_payload(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBleBackend(read_data=b"\x01\x02"))

    payload = cli(["ble", "read", "--address", _ADDRESS, "--char", _CHAR])
    assert payload["exit_code"] == 0
    assert payload["command"] == "ble.read"
    assert payload["data"] == {"char": _CHAR, "hex": "0102", "text": "\x01\x02", "size": 2}
    assert backend.disconnected is True


# ---------------------------------------------------------------------------
# CLI：write
# ---------------------------------------------------------------------------


def test_write_command_writes_whole_payload_without_mtu_chunk(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBleBackend(mtu=23))

    payload = cli(
        ["ble", "write", "--address", _ADDRESS, "--char", _CHAR, "--hex", _FORTY_BYTES]
    )
    assert payload["exit_code"] == 0
    assert payload["command"] == "ble.write"
    assert payload["data"] == {
        "address": _ADDRESS,
        "char": _CHAR,
        "chunks": 1,
        "bytes": 40,
        "mtu": None,
        "response": True,
    }
    assert _count(backend, "write") == 1
    assert backend.disconnected is True


def test_write_command_mtu_chunk_and_no_response(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBleBackend(mtu=23))

    payload = cli(
        [
            "ble",
            "write",
            "--address",
            _ADDRESS,
            "--char",
            _CHAR,
            "--hex",
            _FORTY_BYTES,
            "--mtu-chunk",
            "--no-response",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["chunks"] == 2
    assert payload["data"]["mtu"] == 23
    assert payload["data"]["response"] is False
    writes = [call for call in backend.calls if call[0] == "write"]
    assert [call[3] for call in writes] == [False, False]


def test_write_command_repeat_doubles_chunks(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBleBackend(mtu=23))

    payload = cli(
        [
            "ble",
            "write",
            "--address",
            _ADDRESS,
            "--char",
            _CHAR,
            "--text",
            "x",
            "--mtu-chunk",
            "--repeat",
            "2",
            "--interval-ms",
            "0",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["chunks"] == 2
    assert payload["data"]["bytes"] == 2
    assert _count(backend, "write") == 2
    assert backend.disconnected is True


def test_write_command_failure_exits_three_and_disconnects(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBleBackend(write_error=OSError("写入被拒绝")))

    payload = cli(
        ["ble", "write", "--address", _ADDRESS, "--char", _CHAR, "--hex", "01"]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert backend.disconnected is True


# ---------------------------------------------------------------------------
# CLI：notify
# ---------------------------------------------------------------------------


def test_notify_command_streams_events_and_summary(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch, FakeBleBackend(notifications=[(_CHAR, b"\x01"), (_CHAR, b"OK")])
    )

    payload = cli(
        [
            "ble",
            "notify",
            "--address",
            _ADDRESS,
            "--char",
            _CHAR,
            "--duration",
            "1",
            "--max-events",
            "1",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["command"] == "ble.notify"

    events = _events(payload)
    assert [event.get("event") for event in events[:-1]] == ["notification", "end"]
    assert events[0]["hex"] == "01"
    assert payload["data"]["events"] == 1
    assert payload["data"]["bytes"] == 1
    assert payload["data"]["matched"] is False
    assert ("set_notify", _CHAR, False) in backend.calls
    assert backend.disconnected is True


def test_notify_command_until_hex_ends_early(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch, FakeBleBackend(notifications=[(_CHAR, b"\x01"), (_CHAR, b"OK")])
    )

    payload = cli(
        [
            "ble",
            "notify",
            "--address",
            _ADDRESS,
            "--char",
            _CHAR,
            "--duration",
            "1",
            "--until-hex",
            "4F4B",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["matched"] is True
    assert payload["data"]["events"] == 2
    assert payload["data"]["bytes"] == 3
    assert backend.disconnected is True


def test_notify_command_max_bytes(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch, FakeBleBackend(notifications=[(_CHAR, b"ABCD"), (_CHAR, b"EF")])
    )

    payload = cli(
        [
            "ble",
            "notify",
            "--address",
            _ADDRESS,
            "--char",
            _CHAR,
            "--duration",
            "1",
            "--max-bytes",
            "3",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["bytes"] == 3
    assert payload["data"]["events"] == 1
    assert payload["data"]["matched"] is False
    assert backend.disconnected is True


def test_notify_command_rejects_text_format(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBleBackend())

    payload = cli(
        [
            "ble",
            "notify",
            "--address",
            _ADDRESS,
            "--char",
            _CHAR,
            "--duration",
            "1",
            "--format",
            "text",
        ]
    )
    assert payload["exit_code"] == 2
    assert "usage" in payload["raw"]
    assert backend.calls == []


def test_notify_command_rejects_pretty(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBleBackend())

    payload = cli(
        [
            "ble",
            "notify",
            "--address",
            _ADDRESS,
            "--char",
            _CHAR,
            "--duration",
            "1",
            "--pretty",
        ]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


def test_notify_command_rejects_until(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBleBackend())

    payload = cli(
        [
            "ble",
            "notify",
            "--address",
            _ADDRESS,
            "--char",
            _CHAR,
            "--duration",
            "1",
            "--until",
            "OK",
        ]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


# ---------------------------------------------------------------------------
# CLI：参数校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["ble", "scan"],
        ["ble", "scan", "--duration", "0"],
        ["ble", "scan", "--duration", "-1"],
        ["ble", "scan", "--duration", "nan"],
        ["ble", "scan", "--duration", "inf"],
        ["ble", "scan", "--duration", "3601"],
    ],
)
def test_scan_rejects_invalid_duration(cli, monkeypatch, argv) -> None:
    backend = _install(monkeypatch, FakeBleBackend())

    payload = cli(argv)
    assert payload["exit_code"] == 2
    assert backend.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["ble", "services"],
        ["ble", "services", "--address", "   "],
        ["ble", "services", "--address", _ADDRESS, "--timeout", "0"],
        ["ble", "services", "--address", _ADDRESS, "--timeout", "-1"],
        ["ble", "services", "--address", _ADDRESS, "--timeout", "nan"],
        ["ble", "services", "--address", _ADDRESS, "--timeout", "inf"],
        ["ble", "services", "--address", _ADDRESS, "--timeout", "3601"],
    ],
)
def test_services_rejects_invalid_arguments(cli, monkeypatch, argv) -> None:
    backend = _install(monkeypatch, FakeBleBackend())

    payload = cli(argv)
    assert payload["exit_code"] == 2
    assert backend.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["ble", "read", "--char", _CHAR],
        ["ble", "read", "--address", _ADDRESS],
        ["ble", "read", "--address", " ", "--char", _CHAR],
        ["ble", "read", "--address", _ADDRESS, "--char", "  "],
        ["ble", "read", "--address", _ADDRESS, "--char", _CHAR, "--timeout", "0"],
    ],
)
def test_read_rejects_invalid_arguments(cli, monkeypatch, argv) -> None:
    backend = _install(monkeypatch, FakeBleBackend())

    payload = cli(argv)
    assert payload["exit_code"] == 2
    assert backend.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--repeat", "0"],
        ["--repeat", "-1"],
        ["--interval-ms", "-1"],
        ["--timeout", "0"],
        ["--timeout", "nan"],
    ],
)
def test_write_rejects_invalid_numbers(cli, monkeypatch, extra) -> None:
    backend = _install(monkeypatch, FakeBleBackend())

    payload = cli(
        ["ble", "write", "--address", _ADDRESS, "--char", _CHAR, "--hex", "01", *extra]
    )
    assert payload["exit_code"] == 2
    assert backend.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["ble", "write", "--address", _ADDRESS, "--char", _CHAR],
        ["ble", "write", "--char", _CHAR, "--hex", "01"],
        ["ble", "write", "--address", _ADDRESS, "--hex", "01"],
    ],
)
def test_write_requires_input_and_target(cli, monkeypatch, argv) -> None:
    backend = _install(monkeypatch, FakeBleBackend())

    payload = cli(argv)
    assert payload["exit_code"] == 2
    assert backend.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["ble", "notify", "--address", _ADDRESS, "--char", _CHAR],
        ["ble", "notify", "--address", _ADDRESS, "--char", _CHAR, "--duration", "0"],
        ["ble", "notify", "--address", _ADDRESS, "--char", _CHAR, "--duration", "3601"],
        ["ble", "notify", "--char", _CHAR, "--duration", "1"],
        ["ble", "notify", "--address", _ADDRESS, "--duration", "1"],
    ],
)
def test_notify_rejects_invalid_arguments(cli, monkeypatch, argv) -> None:
    backend = _install(monkeypatch, FakeBleBackend())

    payload = cli(argv)
    assert payload["exit_code"] == 2
    assert backend.calls == []


# ---------------------------------------------------------------------------
# 架构回归
# ---------------------------------------------------------------------------


def test_cli_import_does_not_import_ble_stack() -> None:
    code = (
        "import sys\n"
        "import app.cli.main, app.cli.commands.ble, app.cli.transports.ble_io\n"
        "print('app.io.ble_worker' in sys.modules, 'bleak' in sys.modules,"
        " 'PyQt6' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False False False"

"""``serial monitor`` 流式命令：NDJSON 事件序列、有界结束条件与错误映射。

真实串口与真实时钟都不参与测试：``serial_io.open_transport`` 换成内存 transport，
``time.monotonic`` 换成只有 ``read`` 才推进的假时钟，因此 ``--duration 1`` 的用例
能在毫秒级结束，同时仍能验证截止时间、轮询窗口与字节上界。
"""

from __future__ import annotations

import json

import pytest

from app.cli.commands import serial
from app.cli.transports import serial_io


class FakeClock:
    """由 :class:`FakeTransport` 的 ``read`` 推进的单调时钟。"""

    def __init__(self, step: float = 0.05, start: float = 1000.0) -> None:
        self.value = start
        self.step = step

    def monotonic(self) -> float:
        return self.value

    def tick(self) -> None:
        self.value += self.step


class FakeTransport:
    """``SerialTransport`` 协议的内存替身；每次 ``read`` 推进假时钟。"""

    def __init__(
        self,
        chunks=(),
        *,
        clock: FakeClock | None = None,
        open_error=None,
        read_error=None,
        fail_after: int | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self.calls = []
        self.closed = False
        self.config = None
        self.dtr = None
        self.rts = None
        self.broke = False
        self._clock = clock
        self._open_error = open_error
        self._read_error = read_error
        self._fail_after = fail_after
        self._read_count = 0

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
        if self._clock is not None:
            self._clock.tick()
        self._read_count += 1
        if self._read_error is not None and (
            self._fail_after is None or self._read_count > self._fail_after
        ):
            raise self._read_error
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        self.calls.append(("write", bytes(data)))
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
        return {"cts": False, "dsr": False, "ri": False, "cd": False}


def _install(monkeypatch, transport: FakeTransport) -> FakeTransport:
    monkeypatch.setattr(serial_io, "open_transport", lambda: transport)
    return transport


def _install_clock(monkeypatch, clock: FakeClock) -> None:
    """替换命令模块里的 ``time.monotonic``，时钟只由 transport 的 read 推进。"""
    monkeypatch.setattr(serial.time, "monotonic", clock.monotonic)


def _events(payload: dict) -> list[dict]:
    """把 ``raw`` 里的 NDJSON 行解析成事件列表（最后一项是标准信封）。"""
    lines = [line for line in payload["raw"].splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _read_count(transport: FakeTransport) -> int:
    return sum(1 for call in transport.calls if call[0] == "read")


# ---------------------------------------------------------------------------
# 正常结束
# ---------------------------------------------------------------------------


def test_monitor_emits_ndjson_and_stops_at_duration(cli, monkeypatch) -> None:
    clock = FakeClock()
    transport = _install(monkeypatch, FakeTransport([b"AB", b"CD"], clock=clock))
    _install_clock(monkeypatch, clock)

    payload = cli(["serial", "monitor", "--port", "COM9", "--duration", "1"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "serial.monitor"

    events = _events(payload)
    assert [event.get("event") for event in events[:-1]] == [
        "start",
        "data",
        "data",
        "end",
    ]
    envelope = events[-1]
    assert envelope["ok"] is True
    assert envelope["command"] == "serial.monitor"
    assert payload["data"] == envelope["data"]
    # 假时钟每轮固定推进 0.05 秒，循环在截止后的第一次检查处退出，
    # 因此 duration_s 落在 [1.0, 1.0 + 一个轮询窗口]。
    assert 1.0 <= payload["data"]["duration_s"] <= 1.05
    assert payload["data"]["bytes"] == 4
    assert payload["data"]["events"] == 2
    assert payload["data"]["matched"] is False

    start = events[0]
    assert start["port"] == "COM9"
    assert start["config"]["baud"] == 115200
    assert start["config"]["data_bits"] == 8
    assert start["config"]["encoding"] == "utf-8"
    assert isinstance(start["at"], str)

    first, second = events[1], events[2]
    assert first["hex"] == "4142"
    assert first["text"] == "AB"
    assert first["size"] == 2
    assert second["hex"] == "4344"
    assert second["text"] == "CD"
    assert isinstance(second["at"], str)

    assert events[3]["bytes"] == 4
    assert events[3]["events"] == 2

    assert transport.calls[0][0] == "open"
    assert transport.calls[1] == ("read", 256, 0.05)
    assert _read_count(transport) >= 2
    assert transport.calls[-1] == ("close",)
    assert transport.closed is True


def test_monitor_data_text_follows_encoding(cli, monkeypatch) -> None:
    clock = FakeClock()
    transport = _install(monkeypatch, FakeTransport([b"\xc4\xe3"], clock=clock))
    _install_clock(monkeypatch, clock)

    payload = cli(
        [
            "serial",
            "monitor",
            "--port",
            "COM9",
            "--duration",
            "1",
            "--encoding",
            "gbk",
            "--max-events",
            "1",
        ]
    )
    assert payload["exit_code"] == 0
    events = _events(payload)
    assert events[1]["hex"] == "c4e3"
    assert events[1]["text"] == "你"
    assert events[1]["size"] == 2
    assert transport.closed is True


# ---------------------------------------------------------------------------
# 提前结束条件
# ---------------------------------------------------------------------------


def test_monitor_until_ends_early(cli, monkeypatch) -> None:
    clock = FakeClock()
    transport = _install(
        monkeypatch, FakeTransport([b"O", b"K"], clock=clock)
    )
    _install_clock(monkeypatch, clock)

    payload = cli(
        ["serial", "monitor", "--port", "COM9", "--duration", "1", "--until", "OK"]
    )
    assert payload["exit_code"] == 0
    assert payload["data"] == {
        "duration_s": pytest.approx(0.1),
        "bytes": 2,
        "events": 2,
        "matched": True,
    }

    events = _events(payload)
    assert events[-2]["matched"] is True
    assert events[-1]["ok"] is True
    assert [event["hex"] for event in events[1:3]] == ["4f", "4b"]
    assert _read_count(transport) == 2
    assert transport.closed is True


def test_monitor_until_hex_matches_cumulative_bytes(cli, monkeypatch) -> None:
    clock = FakeClock()
    transport = _install(
        monkeypatch, FakeTransport([b"\x01", b"\x02"], clock=clock)
    )
    _install_clock(monkeypatch, clock)

    payload = cli(
        [
            "serial",
            "monitor",
            "--port",
            "COM9",
            "--duration",
            "1",
            "--until-hex",
            "0102",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["matched"] is True
    assert payload["data"]["events"] == 2
    assert _read_count(transport) == 2


def test_monitor_max_events_ends_early(cli, monkeypatch) -> None:
    clock = FakeClock()
    transport = _install(
        monkeypatch, FakeTransport([b"1", b"2", b"3"], clock=clock)
    )
    _install_clock(monkeypatch, clock)

    payload = cli(
        [
            "serial",
            "monitor",
            "--port",
            "COM9",
            "--duration",
            "1",
            "--max-events",
            "2",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["events"] == 2
    assert payload["data"]["bytes"] == 2
    assert payload["data"]["matched"] is False

    events = _events(payload)
    assert [event["hex"] for event in events[1:3]] == ["31", "32"]
    assert _read_count(transport) == 2
    assert transport.closed is True


def test_monitor_max_bytes_truncates_last_chunk(cli, monkeypatch) -> None:
    clock = FakeClock()
    transport = _install(
        monkeypatch, FakeTransport([b"ABCD", b"EF"], clock=clock)
    )
    _install_clock(monkeypatch, clock)

    payload = cli(
        [
            "serial",
            "monitor",
            "--port",
            "COM9",
            "--duration",
            "1",
            "--max-bytes",
            "5",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["data"] == {
        "duration_s": pytest.approx(0.1),
        "bytes": 5,
        "events": 2,
        "matched": False,
    }

    events = _events(payload)
    assert events[1]["hex"] == "41424344"
    assert events[1]["size"] == 4
    assert events[2]["hex"] == "45"
    assert events[2]["text"] == "E"
    assert events[2]["size"] == 1
    assert _read_count(transport) == 2
    assert transport.closed is True


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


def test_monitor_read_error_emits_error_event_and_exit_3(cli, monkeypatch) -> None:
    clock = FakeClock()
    transport = _install(
        monkeypatch,
        FakeTransport(
            [b"AB"], clock=clock, read_error=OSError("读失败"), fail_after=1
        ),
    )
    _install_clock(monkeypatch, clock)

    payload = cli(["serial", "monitor", "--port", "COM9", "--duration", "1"])
    assert payload["exit_code"] == 3
    assert payload["command"] == "serial.monitor"
    assert payload["error"]["code"] == "device_unavailable"
    assert "COM9" in payload["error"]["message"]

    events = _events(payload)
    assert [event.get("event") for event in events[:-1]] == ["start", "data", "error"]
    assert events[2]["code"] == "device_unavailable"
    assert "COM9" in events[2]["message"]
    assert transport.closed is True


def test_monitor_open_failure_emits_no_start_event(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport(open_error=OSError("端口被占用")))
    emitted: list[dict] = []
    monkeypatch.setattr(serial, "emit_event", emitted.append)

    payload = cli(["serial", "monitor", "--port", "COM9", "--duration", "1"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert "COM9" in payload["error"]["message"]
    assert emitted == []
    assert transport.closed is True


# ---------------------------------------------------------------------------
# 参数校验：流式命令固定 NDJSON，且所有上界都必须合法
# ---------------------------------------------------------------------------


def test_monitor_rejects_text_format(cli, monkeypatch) -> None:
    clock = FakeClock()
    transport = _install(monkeypatch, FakeTransport([b"AB"], clock=clock))
    _install_clock(monkeypatch, clock)

    payload = cli(
        [
            "serial",
            "monitor",
            "--port",
            "COM9",
            "--duration",
            "1",
            "--format",
            "text",
        ]
    )
    assert payload["exit_code"] == 2
    assert "usage" in payload["raw"]
    assert transport.calls == []


def test_monitor_rejects_pretty(cli, monkeypatch) -> None:
    clock = FakeClock()
    transport = _install(monkeypatch, FakeTransport([b"AB"], clock=clock))
    _install_clock(monkeypatch, clock)

    payload = cli(
        ["serial", "monitor", "--port", "COM9", "--duration", "1", "--pretty"]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert transport.calls == []


def test_monitor_requires_duration(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport())
    payload = cli(["serial", "monitor", "--port", "COM9"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert transport.calls == []


def test_monitor_rejects_out_of_range_bounds(cli, monkeypatch) -> None:
    transport = _install(monkeypatch, FakeTransport())
    for extra in (
        ["--duration", "0.5"],
        ["--duration", "3601"],
        ["--duration", "1", "--max-bytes", "0"],
        ["--duration", "1", "--max-events", "0"],
        ["--duration", "1", "--until", "", "--until-hex", "00"],
    ):
        payload = cli(["serial", "monitor", "--port", "COM9", *extra])
        assert payload["exit_code"] == 2
        assert payload["error"]["code"] == "usage"
    assert transport.calls == []

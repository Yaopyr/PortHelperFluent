"""``hid`` 子命令：列表、报告发送与流式监控的 CLI 契约。

真实 HID 硬件与本机时钟都不参与测试：``hid_io.new_backend`` 换成自包含内存后端，
``hid_io._clock`` 换成由 ``read`` 推进的假时钟，因此 ``--duration 1`` 的用例能在
毫秒级结束，同时仍能验证截止时间、轮询窗口与字节/事件上界。
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.cli.commands import hid
from app.cli.transports import hid_io

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_DETAIL_FIELDS = {"index", "path_b64", "serial", "interface", "product", "vid", "pid"}
_LIST_FIELDS = {
    "index",
    "path_b64",
    "vid",
    "pid",
    "product",
    "manufacturer",
    "serial",
    "usage_page",
    "usage",
    "interface",
}


# ---------------------------------------------------------------------------
# 内存后端与假时钟
# ---------------------------------------------------------------------------


def _device(
    path=b"path-1",
    vid=0x1234,
    pid=0x5678,
    *,
    product="Widget",
    manufacturer="Acme",
    serial="SN1",
    usage_page=0x0001,
    usage=0x0006,
    interface=0,
):
    """构造真实 ``HidDeviceInfo``（惰性导入，模块级不加载 PyQt6）。"""
    from app.io.hid_worker import HidDeviceInfo

    return HidDeviceInfo(
        path=path,
        vendor_id=vid,
        product_id=pid,
        product=product,
        manufacturer=manufacturer,
        serial=serial,
        usage_page=usage_page,
        usage=usage,
        interface=interface,
    )


class FakeClock:
    """由 :class:`FakeBackend` 的 ``read`` 推进的单调时钟。"""

    def __init__(self, step: float = 0.1, start: float = 1000.0) -> None:
        self.value = start
        self.step = step

    def monotonic(self) -> float:
        return self.value

    def tick(self) -> None:
        self.value += self.step


class FakeHandle:
    """假的句柄：记录写入内容与是否关闭。"""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False


class FakeBackend:
    """实现 ``HidBackend`` 协议的内存替身，按顺序记录全部调用。"""

    def __init__(
        self,
        devices=(),
        *,
        chunks=(),
        clock: FakeClock | None = None,
        open_error=None,
        write_error=None,
        write_result: int | None = None,
        read_error=None,
        read_error_after: int | None = None,
        close_error=None,
    ) -> None:
        self.devices = list(devices)
        self.chunks = list(chunks)
        self.calls: list[tuple] = []
        self.handle: FakeHandle | None = None
        self.opened_path: bytes | None = None
        self._clock = clock
        self._open_error = open_error
        self._write_error = write_error
        self._write_result = write_result
        self._read_error = read_error
        self._read_error_after = read_error_after
        self._read_count = 0
        self._close_error = close_error

    def enumerate(self):
        self.calls.append(("enumerate",))
        return list(self.devices)

    def open(self, path):
        self.calls.append(("open", bytes(path)))
        if self._open_error is not None:
            raise self._open_error
        self.opened_path = bytes(path)
        self.handle = FakeHandle()
        return self.handle

    def write(self, handle, data):
        self.calls.append(("write", bytes(data)))
        if self._write_error is not None:
            raise self._write_error
        result = len(data) if self._write_result is None else self._write_result
        if result >= 0:
            handle.writes.append(bytes(data))
        return result

    def read(self, handle, size, timeout_ms):
        self.calls.append(("read", size, timeout_ms))
        if self._clock is not None:
            self._clock.tick()
        self._read_count += 1
        if self._read_error is not None and (
            self._read_error_after is None or self._read_count > self._read_error_after
        ):
            raise self._read_error
        return self.chunks.pop(0) if self.chunks else b""

    def close(self, handle):
        self.calls.append(("close",))
        handle.closed = True
        if self._close_error is not None:
            raise self._close_error


def _install(monkeypatch, backend: FakeBackend) -> FakeBackend:
    """``hid list/send/monitor`` 都通过 ``hid_io.new_backend`` 拿后端。"""
    monkeypatch.setattr(hid_io, "new_backend", lambda: backend)
    return backend


def _install_clock(monkeypatch, clock: FakeClock) -> None:
    monkeypatch.setattr(hid_io, "_clock", clock.monotonic)


def _events(payload: dict) -> list[dict]:
    """把 ``raw`` 里的 NDJSON 行解析成事件列表（最后一项是标准信封）。"""
    lines = [line for line in payload["raw"].splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _count(backend: FakeBackend, name: str) -> int:
    return sum(1 for call in backend.calls if call[0] == name)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_reports_devices_and_fields(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch,
        FakeBackend(
            [
                _device(b"p1", 0xABCD, 0x0001, product="Widget", serial="SN-1"),
                _device(b"p2", 0x0F0F, 0x0002, product="Keyboard", serial="SN-2"),
            ]
        ),
    )

    payload = cli(["hid", "list"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "hid.list"

    data = payload["data"]
    assert data["count"] == 2
    first = data["devices"][0]
    assert set(first) == _LIST_FIELDS
    assert first["index"] == 0
    assert first["path_b64"] == base64.standard_b64encode(b"p1").decode("ascii")
    assert first["vid"] == "ABCD"
    assert first["pid"] == "0001"
    assert first["product"] == "Widget"
    assert first["manufacturer"] == "Acme"
    assert first["serial"] == "SN-1"
    assert first["usage_page"] == 0x0001
    assert first["usage"] == 0x0006
    assert first["interface"] == 0
    assert data["devices"][1]["index"] == 1
    assert data["devices"][1]["vid"] == "0F0F"
    assert _count(backend, "open") == 0
    assert _count(backend, "write") == 0


def test_list_filter_matches_product_manufacturer_and_serial(cli, monkeypatch) -> None:
    _install(
        monkeypatch,
        FakeBackend(
            [
                _device(b"p1", 0x1111, 0x0001, product="Widget", serial="SN-1"),
                _device(
                    b"p2",
                    0x2222,
                    0x0002,
                    product="Keyboard",
                    manufacturer="Acme",
                    serial="SN-2",
                ),
            ]
        ),
    )

    for needle, expected in (
        ("wid", [0]),
        ("acme", [0, 1]),
        ("sn-2", [1]),
        ("2222:0002", [1]),
    ):
        payload = cli(["hid", "list", "--filter", needle])
        assert payload["exit_code"] == 0
        assert [item["index"] for item in payload["data"]["devices"]] == expected


def test_list_filters_by_vid_and_pid(cli, monkeypatch) -> None:
    _install(
        monkeypatch,
        FakeBackend(
            [
                _device(b"p1", 0x1234, 0x0001),
                _device(b"p2", 0x4321, 0x0002),
            ]
        ),
    )

    payload = cli(["hid", "list", "--vid", "0x1234"])
    assert [item["index"] for item in payload["data"]["devices"]] == [0]

    payload = cli(["hid", "list", "--pid", "0x0002"])
    assert [item["index"] for item in payload["data"]["devices"]] == [1]
    assert payload["data"]["count"] == 1


def test_list_rejects_invalid_vid(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))
    payload = cli(["hid", "list", "--vid", "xyz"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


# ---------------------------------------------------------------------------
# send：报告构造、读取与设备选择
# ---------------------------------------------------------------------------


def test_path_b64_round_trips_non_utf8_bytes(cli, monkeypatch) -> None:
    path = b"\xff\x00abc"
    backend = _install(monkeypatch, FakeBackend([_device(path)]))

    payload = cli(["hid", "list"])
    encoded = payload["data"]["devices"][0]["path_b64"]
    assert encoded == base64.standard_b64encode(path).decode("ascii")

    payload = cli(["hid", "send", "--path-b64", encoded, "--hex", "00"])
    assert payload["exit_code"] == 0
    assert backend.opened_path == path


def test_send_builds_report_and_writes_once(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device(b"p1")]))

    payload = cli(
        [
            "hid",
            "send",
            "--index",
            "0",
            "--hex",
            "01 02",
            "--report-id",
            "1",
            "--report-length",
            "4",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["command"] == "hid.send"

    data = payload["data"]
    assert data["written"] == {"writes": 1, "bytes": 4}
    assert data["report"] == "01010200"
    assert "input_report" not in data
    assert set(data["device"]) == _LIST_FIELDS
    assert data["device"]["index"] == 0
    assert data["device"]["path_b64"] == base64.b64encode(b"p1").decode("ascii")
    assert data["device"]["vid"] == "1234"
    assert data["device"]["pid"] == "5678"

    writes = [call[1] for call in backend.calls if call[0] == "write"]
    assert writes == [b"\x01\x01\x02\x00"]
    assert backend.opened_path == b"p1"
    assert _count(backend, "read") == 0
    assert backend.calls[-1] == ("close",)
    assert backend.handle.closed is True


def test_send_without_report_id_keeps_payload_first(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device(b"p1")]))

    payload = cli(
        ["hid", "send", "--index", "0", "--hex", "01 02", "--report-length", "4"]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["report"] == "01020000"
    assert [call[1] for call in backend.calls if call[0] == "write"] == [
        b"\x01\x02\x00\x00"
    ]


def test_send_reads_input_report_once(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device(b"p1")], chunks=[b"\x05"]))

    payload = cli(
        [
            "hid",
            "send",
            "--index",
            "0",
            "--hex",
            "00",
            "--read",
            "--read-length",
            "4",
            "--timeout",
            "0.5",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["written"] == {"writes": 1, "bytes": 64}
    assert payload["data"]["input_report"] == {
        "hex": "05",
        "text": "\x05",
        "size": 1,
    }
    assert [call for call in backend.calls if call[0] == "read"] == [
        ("read", 4, 500)
    ]
    assert _count(backend, "write") == 1
    assert backend.handle.closed is True


def test_send_input_report_text_uses_input_encoding(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch, FakeBackend([_device(b"p1")], chunks=["中".encode("gbk")])
    )

    payload = cli(
        [
            "hid",
            "send",
            "--index",
            "0",
            "--text",
            "x",
            "--encoding",
            "gbk",
            "--read",
        ]
    )
    assert payload["exit_code"] == 0
    assert payload["data"]["input_report"] == {
        "hex": "d6d0",
        "text": "中",
        "size": 2,
    }
    assert [call[1] for call in backend.calls if call[0] == "write"] == [b"x" + b"\x00" * 63]


def test_send_repeat_writes_with_interval_and_reads_once(cli, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(hid.time, "sleep", sleeps.append)
    backend = _install(
        monkeypatch, FakeBackend([_device(b"p1")], chunks=[b"\x01"])
    )

    payload = cli(
        [
            "hid",
            "send",
            "--index",
            "0",
            "--hex",
            "00",
            "--repeat",
            "2",
            "--interval-ms",
            "100",
            "--read",
        ]
    )
    assert payload["exit_code"] == 0
    # 默认报告长度 64：两次写入共 128 字节。
    assert payload["data"]["written"] == {"writes": 2, "bytes": 128}
    assert _count(backend, "write") == 2
    # 间隔只出现在两次写入之间：第一次写之前不 sleep。
    assert sleeps == [0.1]
    assert _count(backend, "read") == 1
    assert backend.handle.closed is True


def test_send_selects_device_by_index(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch, FakeBackend([_device(b"p1"), _device(b"p2", serial="SN2")])
    )

    payload = cli(["hid", "send", "--index", "1", "--hex", "00"])
    assert payload["exit_code"] == 0
    assert backend.opened_path == b"p2"


def test_send_selects_device_by_vid_pid_and_serial(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch,
        FakeBackend(
            [
                _device(b"p1", serial="AAA"),
                _device(b"p2", serial="BBB"),
            ]
        ),
    )

    payload = cli(
        ["hid", "send", "--vid", "1234", "--pid", "5678", "--serial", "BBB", "--hex", "00"]
    )
    assert payload["exit_code"] == 0
    assert backend.opened_path == b"p2"


def test_send_selects_device_by_interface(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch,
        FakeBackend(
            [
                _device(b"p1", interface=0),
                _device(b"p2", interface=1),
            ]
        ),
    )

    payload = cli(
        ["hid", "send", "--vid", "1234", "--pid", "5678", "--interface", "1", "--hex", "00"]
    )
    assert payload["exit_code"] == 0
    assert backend.opened_path == b"p2"


def test_send_unknown_vid_pid_is_not_found(cli, monkeypatch) -> None:
    _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(["hid", "send", "--vid", "9999", "--pid", "0000", "--hex", "00"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"


def test_send_unknown_path_is_not_found(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device(b"p1")]))

    payload = cli(
        [
            "hid",
            "send",
            "--path-b64",
            base64.b64encode(b"missing").decode("ascii"),
            "--hex",
            "00",
        ]
    )
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"
    assert _count(backend, "open") == 0


@pytest.mark.parametrize("index", ["5", "-1"])
def test_send_index_out_of_range_is_not_found(cli, monkeypatch, index) -> None:
    _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(["hid", "send", "--index", index, "--hex", "00"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"


def test_send_ambiguous_vid_pid_returns_candidates(cli, monkeypatch) -> None:
    _install(
        monkeypatch,
        FakeBackend(
            [
                _device(b"p1", serial="AAA", interface=0),
                _device(b"p2", serial="BBB", interface=1),
            ]
        ),
    )

    payload = cli(["hid", "send", "--vid", "1234", "--pid", "5678", "--hex", "00"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "ambiguous_device"
    details = payload["error"]["details"]
    assert len(details) == 2
    for item in details:
        assert set(item) == _DETAIL_FIELDS
    assert [item["index"] for item in details] == [0, 1]
    assert details[0]["path_b64"] == base64.standard_b64encode(b"p1").decode("ascii")
    assert details[1]["serial"] == "BBB"
    assert details[1]["interface"] == 1
    assert details[0]["vid"] == "1234"
    assert details[0]["pid"] == "5678"


def test_send_rejects_invalid_base64_path(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(["hid", "send", "--path-b64", "%%%%", "--hex", "00"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


def test_send_rejects_vid_without_pid(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(["hid", "send", "--vid", "1234", "--hex", "00"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


def test_send_rejects_multiple_device_selectors(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(
        ["hid", "send", "--index", "0", "--vid", "1234", "--pid", "5678", "--hex", "00"]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


def test_send_rejects_index_and_path_together(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(
        [
            "hid",
            "send",
            "--index",
            "0",
            "--path-b64",
            base64.b64encode(b"p1").decode("ascii"),
            "--hex",
            "00",
        ]
    )
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


def test_send_rejects_serial_without_vid_pid(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(["hid", "send", "--index", "0", "--serial", "SN1", "--hex", "00"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


def test_send_rejects_missing_device_selector(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(["hid", "send", "--hex", "00"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--report-id", "256"],
        ["--report-id", "-1"],
        ["--report-length", "0"],
        ["--report-length", "1025"],
        ["--read-length", "0"],
        ["--read-length", "1025"],
        ["--timeout", "nan"],
        ["--timeout", "inf"],
        ["--timeout", "-1"],
        ["--repeat", "0"],
        ["--interval-ms", "-1"],
    ],
)
def test_send_rejects_invalid_numbers(cli, monkeypatch, extra) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(["hid", "send", "--index", "0", "--hex", "00", *extra])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


def test_send_maps_build_report_error_to_usage(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    def _boom(*args, **kwargs):
        raise ValueError("报告参数无效")

    monkeypatch.setattr(hid_io, "build_report", _boom)
    payload = cli(["hid", "send", "--index", "0", "--hex", "00"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert "报告参数无效" in payload["error"]["message"]
    assert backend.calls == []


def test_send_write_failure_is_device_unavailable(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch,
        FakeBackend([_device(b"p1")], write_error=OSError("已拔出")),
    )

    payload = cli(["hid", "send", "--index", "0", "--hex", "00"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert "1234:5678" in payload["error"]["message"]
    assert backend.handle.closed is True


def test_send_negative_write_result_is_device_unavailable(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch, FakeBackend([_device(b"p1")], write_result=-1)
    )

    payload = cli(["hid", "send", "--index", "0", "--hex", "00"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert backend.handle.writes == []
    assert backend.calls[-1] == ("close",)
    assert backend.handle.closed is True


def test_send_open_failure_is_device_unavailable(cli, monkeypatch) -> None:
    backend = _install(
        monkeypatch, FakeBackend([_device(b"p1")], open_error=OSError("被占用"))
    )

    payload = cli(["hid", "send", "--index", "0", "--hex", "00"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert "打开 HID 设备失败" in payload["error"]["message"]
    assert "1234:5678" in payload["error"]["message"]
    assert _count(backend, "write") == 0


def test_send_close_failure_writes_stderr_only(capsys, monkeypatch) -> None:
    backend = _install(
        monkeypatch, FakeBackend([_device(b"p1")], close_error=OSError("关闭失败"))
    )
    from app.cli.main import run

    code = run(["hid", "send", "--index", "0", "--hex", "00"])
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["data"]["report"] == "00" + "00" * 63
    assert backend.handle.closed is True
    assert captured.err.strip().count("\n") == 0
    assert captured.err.strip().endswith("关闭 HID 设备失败：关闭失败")
    assert "警告：" in captured.err


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------


def test_monitor_emits_data_events_and_summary(cli, monkeypatch) -> None:
    clock = FakeClock()
    backend = _install(
        monkeypatch, FakeBackend([_device(b"p1")], chunks=[b"\x01"], clock=clock)
    )
    _install_clock(monkeypatch, clock)

    payload = cli(["hid", "monitor", "--index", "0", "--duration", "1"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "hid.monitor"

    events = _events(payload)
    assert [event.get("event") for event in events[:-1]] == ["data"]
    data = events[0]
    assert set(data) == {"event", "hex", "text", "size", "at"}
    assert data["hex"] == "01"
    assert data["text"] == "\x01"
    assert data["size"] == 1
    assert isinstance(data["at"], str)

    # 假时钟每轮推进 0.1 秒：1 秒窗口内正好 10 次 read。
    assert _count(backend, "read") == 10
    assert [call for call in backend.calls if call[0] == "read"][0] == (
        "read",
        64,
        100,
    )
    assert payload["data"] == {
        "duration_s": pytest.approx(1.0),
        "bytes": 1,
        "events": 1,
        "report_length": 64,
    }
    assert backend.calls[-1] == ("close",)
    assert backend.handle.closed is True


def test_monitor_read_timeout_shrinks_to_remaining(cli, monkeypatch) -> None:
    clock = FakeClock(step=0.95)
    backend = _install(monkeypatch, FakeBackend([_device(b"p1")], clock=clock))
    _install_clock(monkeypatch, clock)

    payload = cli(["hid", "monitor", "--index", "0", "--duration", "1"])
    assert payload["exit_code"] == 0
    reads = [call for call in backend.calls if call[0] == "read"]
    # 第二轮只剩不足 100 ms，单次 read 的超时窗口随之收缩。
    assert reads[0] == ("read", 64, 100)
    assert 1 <= reads[1][2] <= 50
    assert payload["data"]["bytes"] == 0
    assert payload["data"]["events"] == 0
    assert payload["data"]["report_length"] == 64


def test_monitor_max_events_ends_early(cli, monkeypatch) -> None:
    clock = FakeClock()
    backend = _install(
        monkeypatch,
        FakeBackend([_device(b"p1")], chunks=[b"1", b"2", b"3"], clock=clock),
    )
    _install_clock(monkeypatch, clock)

    payload = cli(
        ["hid", "monitor", "--index", "0", "--duration", "1", "--max-events", "1"]
    )
    assert payload["exit_code"] == 0
    assert payload["data"] == {
        "duration_s": pytest.approx(0.1),
        "bytes": 1,
        "events": 1,
        "report_length": 64,
    }
    events = _events(payload)
    assert events[0]["hex"] == "31"
    assert _count(backend, "read") == 1
    assert backend.handle.closed is True


def test_monitor_max_bytes_truncates_last_chunk(cli, monkeypatch) -> None:
    clock = FakeClock()
    backend = _install(
        monkeypatch,
        FakeBackend([_device(b"p1")], chunks=[b"ABCD", b"EF"], clock=clock),
    )
    _install_clock(monkeypatch, clock)

    payload = cli(
        ["hid", "monitor", "--index", "0", "--duration", "1", "--max-bytes", "1"]
    )
    assert payload["exit_code"] == 0
    assert payload["data"] == {
        "duration_s": pytest.approx(0.1),
        "bytes": 1,
        "events": 1,
        "report_length": 64,
    }
    events = _events(payload)
    assert events[0]["hex"] == "41"
    assert events[0]["size"] == 1
    assert _count(backend, "read") == 1
    assert backend.handle.closed is True


def test_monitor_rejects_text_format(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(
        ["hid", "monitor", "--index", "0", "--duration", "1", "--format", "text"]
    )
    assert payload["exit_code"] == 2
    # --format text 同时作用于错误信封，只能从原始输出里确认错误码。
    assert "usage" in payload["raw"]
    assert backend.calls == []


def test_monitor_rejects_pretty(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(["hid", "monitor", "--index", "0", "--duration", "1", "--pretty"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


@pytest.mark.parametrize("extra", [["--until", "OK"], ["--until-hex", "4F4B"]])
def test_monitor_rejects_until(cli, monkeypatch, extra) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(["hid", "monitor", "--index", "0", "--duration", "1", *extra])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


def test_monitor_requires_duration(cli, monkeypatch) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(["hid", "monitor", "--index", "0"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


@pytest.mark.parametrize("extra", [["--report-length", "0"], ["--report-length", "1025"]])
def test_monitor_rejects_invalid_report_length(cli, monkeypatch, extra) -> None:
    backend = _install(monkeypatch, FakeBackend([_device()]))

    payload = cli(["hid", "monitor", "--index", "0", "--duration", "1", *extra])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"
    assert backend.calls == []


def test_monitor_read_error_emits_error_event_before_envelope(cli, monkeypatch) -> None:
    clock = FakeClock()
    backend = _install(
        monkeypatch,
        FakeBackend([_device(b"p1")], clock=clock, read_error=OSError("读失败")),
    )
    _install_clock(monkeypatch, clock)

    payload = cli(["hid", "monitor", "--index", "0", "--duration", "1"])
    assert payload["exit_code"] == 3
    assert payload["command"] == "hid.monitor"
    assert payload["error"]["code"] == "device_unavailable"
    assert "1234:5678" in payload["error"]["message"]

    events = _events(payload)
    assert len(events) == 2
    error_event, envelope = events
    assert error_event["event"] == "error"
    assert error_event["code"] == "device_unavailable"
    assert "1234:5678" in error_event["message"]
    assert isinstance(error_event["at"], str)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "device_unavailable"

    assert backend.handle.closed is True
    assert backend.calls[-1] == ("close",)


def test_monitor_read_error_after_data_keeps_prior_event(cli, monkeypatch) -> None:
    clock = FakeClock()
    backend = _install(
        monkeypatch,
        FakeBackend(
            [_device(b"p1")],
            chunks=[b"\x01"],
            clock=clock,
            read_error=OSError("读失败"),
            read_error_after=1,
        ),
    )
    _install_clock(monkeypatch, clock)

    payload = cli(["hid", "monitor", "--index", "0", "--duration", "1"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert "1234:5678" in payload["error"]["message"]

    events = _events(payload)
    assert [event.get("event") for event in events] == ["data", "error", None]
    assert events[0]["hex"] == "01"
    assert events[1]["event"] == "error"
    assert events[1]["code"] == "device_unavailable"
    assert "1234:5678" in events[1]["message"]
    assert events[-1]["ok"] is False
    assert events[-1]["command"] == "hid.monitor"

    assert backend.handle.closed is True
    assert backend.calls[-1] == ("close",)


def test_monitor_open_failure_has_no_events(cli, monkeypatch) -> None:
    clock = FakeClock()
    backend = _install(
        monkeypatch,
        FakeBackend([_device(b"p1")], clock=clock, open_error=OSError("被占用")),
    )
    _install_clock(monkeypatch, clock)
    emitted: list[dict] = []
    monkeypatch.setattr(hid, "emit_event", emitted.append)

    payload = cli(["hid", "monitor", "--index", "0", "--duration", "1"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "device_unavailable"
    assert "打开 HID 设备失败" in payload["error"]["message"]
    assert "1234:5678" in payload["error"]["message"]
    assert emitted == []
    assert _count(backend, "read") == 0
    assert backend.opened_path is None


# ---------------------------------------------------------------------------
# 架构回归
# ---------------------------------------------------------------------------


def test_cli_import_does_not_import_hid_stack() -> None:
    code = (
        "import sys\n"
        "import app.cli.main, app.cli.commands.hid, app.cli.transports.hid_io\n"
        "print('app.io.hid_worker' in sys.modules, 'hid' in sys.modules,"
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

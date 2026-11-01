"""CLI 基础契约：错误码、信封、输入解析与有界读取。

本文件只覆盖 Qt 无关的基础模块（``app.cli.errors``/``output``/``inputs``/``reads``）；
具体命令的测试在各自的任务文件里。
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import APP_VERSION
from app.cli.errors import (
    EXIT_EXPECT,
    EXIT_INTERNAL,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_OPERATION,
    EXIT_USAGE,
    CliError,
)
from app.cli.inputs import InputData, add_input_options, read_input
from app.cli.output import (
    add_output_options,
    configure_stdio,
    emit_event,
    envelope_error,
    envelope_ok,
    write_payload,
)
from app.cli.reads import (
    StreamOptions,
    add_stream_options,
    make_matcher,
    read_stream_options,
    read_until,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _input_parser(*, required: bool = True) -> argparse.ArgumentParser:
    """构造一个只带输入选项的解析器；``--encoding`` 由命令自己声明，测试里显式补上。"""
    parser = argparse.ArgumentParser(prog="test", add_help=False)
    add_input_options(parser, required=required)
    parser.add_argument("--encoding", default="utf-8")
    return parser


class _FakeStdin:
    """替身标准输入：只提供 ``buffer``，与真实 ``sys.stdin`` 的读取方式一致。"""

    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


# ---------------------------------------------------------------------------
# 退出码
# ---------------------------------------------------------------------------


def test_exit_code_constants() -> None:
    assert EXIT_OK == 0
    assert EXIT_INTERNAL == 1
    assert EXIT_USAGE == 2
    assert EXIT_OPERATION == 3
    assert EXIT_EXPECT == 4
    assert EXIT_INTERRUPTED == 130


def test_cli_error_defaults_to_operation_exit_code() -> None:
    error = CliError("device_unavailable", "打开串口 COM3 失败：...")
    assert error.code == "device_unavailable"
    assert error.message == "打开串口 COM3 失败：..."
    assert error.exit_code == EXIT_OPERATION
    assert error.details is None
    assert str(error) == "打开串口 COM3 失败：..."


# ---------------------------------------------------------------------------
# 信封与输出
# ---------------------------------------------------------------------------


def test_envelope_ok_shape() -> None:
    payload = envelope_ok("checksum", {"size": 9}, 12)
    assert payload == {
        "ok": True,
        "command": "checksum",
        "version": APP_VERSION,
        "elapsed_ms": 12,
        "data": {"size": 9},
    }
    assert "error" not in payload


def test_envelope_error_includes_details_and_exit_code() -> None:
    details = [{"index": 0, "path_b64": "AA=="}, {"index": 1, "path_b64": "AQ=="}]
    error = CliError("ambiguous_device", "命中多台设备", 3, details=details)
    payload = envelope_error("hid.send", error, 42)
    assert payload == {
        "ok": False,
        "command": "hid.send",
        "version": APP_VERSION,
        "elapsed_ms": 42,
        "error": {"code": "ambiguous_device", "message": "命中多台设备", "details": details},
    }


def test_envelope_error_omits_details_when_absent() -> None:
    error = CliError("not_found", "找不到设备")
    payload = envelope_error("serial.send", error, 5)
    assert payload["ok"] is False
    assert payload["error"] == {"code": "not_found", "message": "找不到设备"}
    assert set(payload) == {"ok", "command", "version", "elapsed_ms", "error"}


def test_write_payload_json_default_and_pretty(capsys) -> None:
    payload = {"ok": True, "command": "checksum", "data": {"name": "校验"}}
    write_payload(payload)
    out = capsys.readouterr().out
    assert json.loads(out) == payload
    # ensure_ascii=False：中文原样输出，便于 Agent 直接读。
    assert "校验" in out and "\\u" not in out
    assert out.count("\n") == 1

    write_payload(payload, pretty=True)
    pretty_out = capsys.readouterr().out
    assert json.loads(pretty_out) == payload
    assert pretty_out.splitlines()[1].startswith('  "')


def test_write_payload_text_renderer(capsys) -> None:
    payload = {
        "ok": True,
        "command": "checksum",
        "elapsed_ms": 7,
        "data": {"size": 3, "hex": "01 02 03"},
    }
    write_payload(payload, fmt="text")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "ok: true"
    assert lines[1] == "command: checksum"
    assert lines[2] == "elapsed_ms: 7"
    # 嵌套对象压成一行 JSON，避免人工查看时被无限缩进淹没。
    assert lines[3] == 'data: {"size": 3, "hex": "01 02 03"}'


def test_emit_event_writes_one_json_line(capsys) -> None:
    emit_event({"event": "data", "hex": "41", "text": "A"})
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"event": "data", "hex": "41", "text": "A"}


def test_configure_stdio_keeps_utf8_output(capsys) -> None:
    configure_stdio()
    print("中文")
    assert capsys.readouterr().out.strip() == "中文"


class _RecordingStream:
    """记录 ``reconfigure`` 调用的替身标准流；``encoding`` 可为 None（缺失）。"""

    def __init__(self, encoding: str | None) -> None:
        self.encoding = encoding
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.mark.parametrize("encoding", ["utf-8", "UTF-8", "utf8", "utf_8"])
def test_configure_stdio_leaves_utf8_streams_alone(monkeypatch, encoding: str) -> None:
    """已经是 UTF-8 的流不调用 reconfigure：pytest 捕获流会因此丢掉汇总行。"""
    fake_stdout = _RecordingStream(encoding)
    fake_stderr = _RecordingStream(None)  # encoding 缺失的替身同样不动
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    configure_stdio()

    assert fake_stdout.calls == []
    assert fake_stderr.calls == []


def test_configure_stdio_reconfigures_non_utf8_streams(monkeypatch) -> None:
    fake_stdout = _RecordingStream("cp936")
    fake_stderr = _RecordingStream(None)
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    configure_stdio()

    assert fake_stdout.calls == [
        {"encoding": "utf-8", "errors": "replace", "newline": "\n"}
    ]
    assert fake_stderr.calls == []


def test_add_output_options_defaults_and_choices() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    add_output_options(parser)

    args = parser.parse_args([])
    assert args.format == "json"
    assert args.pretty is False

    args = parser.parse_args(["--format", "text", "--pretty"])
    assert args.format == "text" and args.pretty is True

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["--format", "xml"])
    assert exit_info.value.code == EXIT_USAGE


def test_cli_primitives_import_without_pyqt6() -> None:
    code = (
        "import sys\n"
        "import app.cli.errors, app.cli.inputs, app.cli.output, app.cli.reads\n"
        "print('PyQt6' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------


def test_read_input_text_hex_file_stdin(tmp_path, monkeypatch) -> None:
    parser = _input_parser()

    # --text：按 --encoding 编码；text 字段保存原始文本。
    result = read_input(parser.parse_args(["--text", "中文"]))
    assert isinstance(result, InputData)
    assert result.data == b"\xe4\xb8\xad\xe6\x96\x87"
    assert result.text == "中文"
    assert result.encoding == "utf-8"
    assert result.source == "text"

    result = read_input(parser.parse_args(["--text", "中文", "--encoding", "gbk"]))
    assert result.data == b"\xd6\xd0\xce\xc4"
    assert result.text == "中文"
    assert result.encoding == "gbk"

    # --hex：复用 parse_hex，text 保留原始十六进制字符串。
    result = read_input(parser.parse_args(["--hex", "E4 B8 AD"]))
    assert result.data == b"\xe4\xb8\xad"
    assert result.text == "E4 B8 AD"
    assert result.source == "hex"

    # --file：读原始字节，text 按编码解码。
    raw_path = tmp_path / "raw.bin"
    raw_path.write_bytes(b"\xe4\xb8\xad")
    result = read_input(parser.parse_args(["--file", str(raw_path)]))
    assert result.data == b"\xe4\xb8\xad"
    assert result.text == "中"
    assert result.source == "file"

    # --stdin：读 buffer 的原始字节，text 按编码解码。
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b"\xe4\xb8\xad"))
    result = read_input(parser.parse_args(["--stdin"]))
    assert result.data == b"\xe4\xb8\xad"
    assert result.text == "中"
    assert result.source == "stdin"


def test_read_input_requires_exactly_one_source() -> None:
    # required=False 的解析器允许空输入，此时由 read_input 报 usage。
    optional_parser = _input_parser(required=False)
    with pytest.raises(CliError) as excinfo:
        read_input(optional_parser.parse_args([]))
    error = excinfo.value
    assert error.code == "usage" and error.exit_code == EXIT_USAGE

    # required=True 时 argparse 自己拦住空输入；互斥组拦住同时给两个来源。
    required_parser = _input_parser(required=True)
    with pytest.raises(SystemExit) as exit_info:
        required_parser.parse_args([])
    assert exit_info.value.code == EXIT_USAGE

    with pytest.raises(SystemExit) as exit_info:
        required_parser.parse_args(["--text", "x", "--file", "y"])
    assert exit_info.value.code == EXIT_USAGE


def test_read_input_reports_bad_hex_missing_file_and_bad_codec(tmp_path) -> None:
    parser = _input_parser()

    with pytest.raises(CliError) as excinfo:
        read_input(parser.parse_args(["--hex", "0x0"]))
    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.exit_code == EXIT_OPERATION
    assert "十六进制" in excinfo.value.message

    with pytest.raises(CliError) as excinfo:
        read_input(parser.parse_args(["--file", str(tmp_path / "missing.bin")]))
    assert excinfo.value.code == "not_found"
    assert excinfo.value.exit_code == EXIT_OPERATION

    with pytest.raises(CliError) as excinfo:
        read_input(parser.parse_args(["--text", "x", "--encoding", "no-such-codec"]))
    assert excinfo.value.code == "usage"
    assert excinfo.value.exit_code == EXIT_USAGE
    assert "编码" in excinfo.value.message


def test_read_input_gbk_file_decode_replaces_invalid_bytes(tmp_path) -> None:
    parser = _input_parser()
    raw_path = tmp_path / "gbk.bin"
    raw_path.write_bytes(b"\xd6\xd0\xff")
    result = read_input(parser.parse_args(["--file", str(raw_path), "--encoding", "gbk"]))
    # errors="replace"：无法解码的 0xFF 变成 U+FFFD，不抛异常。
    assert result.text == "中\ufffd"
    assert result.data == b"\xd6\xd0\xff"


# ---------------------------------------------------------------------------
# 有界读取
# ---------------------------------------------------------------------------


def test_read_until_matches_chunks_split_across_reads() -> None:
    chunks = [b"O", b"K"]

    def fetch(seconds: float) -> bytes:
        return chunks.pop(0) if chunks else b""

    outcome = read_until(fetch, timeout=0.2, match=lambda data: b"OK" in data, poll=0.01)
    assert outcome.data == b"OK"
    assert outcome.matched is True
    assert outcome.timed_out is False
    assert outcome.chunks == 2


def test_read_until_reports_timeout_when_match_not_reached() -> None:
    def fetch(seconds: float) -> bytes:
        return b""

    outcome = read_until(fetch, timeout=0.05, match=lambda data: b"OK" in data, poll=0.01)
    assert outcome.matched is False
    assert outcome.timed_out is True
    assert outcome.chunks == 0
    assert outcome.elapsed_s >= 0.05


def test_read_until_stops_at_max_bytes_and_read_for() -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    def fetch(seconds: float) -> bytes:
        now[0] += seconds
        return b"0123456789"

    # max_bytes：3 次 fetch 得到 30 字节，只保留前 25 字节。
    outcome = read_until(fetch, timeout=10, read_for=0.2, max_bytes=25, poll=0.05, clock=clock)
    assert outcome.data == b"0123456789012345678901234"
    assert outcome.chunks == 3
    assert outcome.matched is False
    assert outcome.timed_out is False

    # read_for 到点结束：每轮推进 poll，第 4 轮刚好到截止时间。
    now[0] = 0.0
    outcome = read_until(fetch, timeout=10, read_for=0.2, poll=0.05, clock=clock)
    assert len(outcome.data) == 40
    assert outcome.chunks == 4
    assert outcome.elapsed_s == pytest.approx(0.2)

    # 单次 fetch 就跨越截止时间时不再循环：poll 收缩到剩余时间（0.2 秒），不会越界。
    now[0] = 0.0
    calls: list[float] = []

    def slow_fetch(seconds: float) -> bytes:
        calls.append(seconds)
        now[0] += seconds
        return b"x"

    outcome = read_until(slow_fetch, timeout=10, read_for=0.2, poll=0.5, clock=clock)
    assert calls == [0.2]
    assert outcome.data == b"x" and outcome.chunks == 1
    assert outcome.elapsed_s == pytest.approx(0.2)


def test_make_matcher_hex_and_text() -> None:
    hex_matcher = make_matcher(expect_hex="4F 4B")
    assert hex_matcher is not None
    assert hex_matcher(b"..OK..") is True
    assert hex_matcher(b"NO") is False

    text_matcher = make_matcher(expect="OK")
    assert text_matcher is not None
    assert text_matcher(b"..OK..") is True
    assert text_matcher(b"NO") is False

    gbk_matcher = make_matcher(expect="中文", encoding="gbk")
    assert gbk_matcher is not None
    assert gbk_matcher(b"\xd6\xd0\xce\xc4") is True

    assert make_matcher() is None

    with pytest.raises(CliError) as excinfo:
        make_matcher(expect="OK", expect_hex="4F 4B")
    assert excinfo.value.code == "usage"
    assert excinfo.value.exit_code == EXIT_USAGE


def test_make_matcher_rejects_empty_patterns() -> None:
    # 空模式会让任何数据立即「命中」，必须当参数错误拦下。
    with pytest.raises(CliError) as excinfo:
        make_matcher(expect="")
    assert excinfo.value.code == "usage"
    assert excinfo.value.exit_code == EXIT_USAGE

    for empty_hex in ("", "   ", " , \n\t"):
        with pytest.raises(CliError) as excinfo:
            make_matcher(expect_hex=empty_hex)
        assert excinfo.value.code == "usage"
        assert excinfo.value.exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------
# 流式选项
# ---------------------------------------------------------------------------


def test_stream_options_parsing_and_defaults() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    add_stream_options(parser)

    options = read_stream_options(parser.parse_args(["--duration", "2"]))
    assert isinstance(options, StreamOptions)
    assert options.duration == 2.0
    assert options.max_bytes is None
    assert options.max_events is None
    assert options.until is None
    assert options.until_hex is None

    options = read_stream_options(
        parser.parse_args(
            [
                "--duration",
                "1.5",
                "--max-bytes",
                "10",
                "--max-events",
                "3",
                "--until-hex",
                "4F 4B",
            ]
        )
    )
    assert options.duration == 1.5
    assert options.max_bytes == 10
    assert options.max_events == 3
    assert options.until_hex == "4F 4B"


def test_stream_options_validation() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    add_stream_options(parser)
    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args([])
    assert exit_info.value.code == EXIT_USAGE

    for argv in (
        ["--duration", "0.5"],  # 小于 1 秒
        ["--duration", "10000"],  # 大于 3600 秒
        ["--duration", "2", "--max-bytes", "0"],
        ["--duration", "2", "--max-events", "-1"],
        ["--duration", "2", "--until", "OK", "--until-hex", "4F 4B"],
    ):
        with pytest.raises(CliError) as excinfo:
            read_stream_options(parser.parse_args(argv))
        assert excinfo.value.code == "usage"
        assert excinfo.value.exit_code == EXIT_USAGE

    optional_parser = argparse.ArgumentParser(add_help=False)
    add_stream_options(optional_parser, require_duration=False)
    with pytest.raises(CliError) as excinfo:
        read_stream_options(optional_parser.parse_args([]))
    assert excinfo.value.code == "usage"
    assert excinfo.value.exit_code == EXIT_USAGE

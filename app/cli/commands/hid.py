"""``hid`` 命令组：USB HID 设备枚举、报告发送与有界流式监控。

- ``list``：枚举设备，按 product/manufacturer/serial 与 vid/pid 过滤；
- ``send``：按 ``--index``/``--path-b64``/``--vid+--pid`` 选设备 → 用 ``build_report``
  构造报告 → 按 ``--repeat`` 写入 → 可选读一次输入报告 → 关闭；
- ``monitor``：打开后循环读取，只逐条输出 ``data`` 事件，返回时长/字节/事件汇总，
  由 ``--duration``/``--max-bytes``/``--max-events`` 保证有界。

复用 :mod:`app.io.hid_worker` 的 ``HidDeviceInfo``/``HidapiBackend``/``build_report``，
但不启动 ``HidWorker``；这些导入都由 :mod:`app.cli.transports.hid_io` 在函数内部
完成，模块级只依赖标准库与 CLI 原语，不会把 ``hid``/PyQt6 带进 CLI 进程。
"""

from __future__ import annotations

import math
import sys
import time
from typing import Any

from app.cli.errors import EXIT_OPERATION, EXIT_USAGE, CliError
from app.cli.inputs import add_input_options, read_input
from app.cli.output import add_output_options, emit_event
from app.cli.reads import add_stream_options, read_stream_options
from app.cli.transports import hid_io
from app.core.encoding import decode_bytes

__all__ = ["register", "run_list", "run_monitor", "run_send"]

_DEFAULT_REPORT_ID = 0
_DEFAULT_REPORT_LENGTH = 64
_DEFAULT_READ_LENGTH = 64
_DEFAULT_TIMEOUT = 1.0
_DEFAULT_REPEAT = 1
_DEFAULT_INTERVAL_MS = 100
_MAX_REPORT_LENGTH = 1024
_MAX_REPORT_ID = 255
_MAX_ID = 0xFFFF

# 空块后的短暂让步：真实 hidapi 的 read 会阻塞，但后端若立即返回空块，
# 这个停顿可避免监控循环空转；上限仅数毫秒，且不越过 --duration。
EMPTY_READ_PAUSE = 0.005


def register(subparsers) -> None:
    """注册 ``hid`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "hid",
        help="USB HID：枚举设备、发送报告与流式读取输入报告。",
        description="直接使用 hidapi 适配层，不启动 GUI 工作线程；"
        "可用子命令：list、send、monitor。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    _register_list(group)
    _register_send(group)
    _register_monitor(group)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _register_list(subparsers) -> None:
    parser = subparsers.add_parser(
        "list",
        help="枚举 USB HID 设备，可按关键词或 vid/pid 过滤。",
        description="返回 index、path_b64、vid、pid 等字段；--index 指本次枚举顺序（0 基），"
        "跨调用定位请用 list 输出的 path_b64 配合 send/monitor 的 --path-b64。",
    )
    parser.add_argument(
        "--filter",
        metavar="TEXT",
        help="按 product、manufacturer 或 serial 的子串过滤（大小写不敏感）。",
    )
    parser.add_argument("--vid", metavar="HEX", help="只看该厂商 ID（0–FFFF）。")
    parser.add_argument("--pid", metavar="HEX", help="只看该产品 ID（0–FFFF）。")
    add_output_options(parser)
    parser.set_defaults(handler=run_list, command="hid.list")


def run_list(args) -> dict[str, Any]:
    """返回 ``devices`` 列表与 ``count``。"""
    needle = str(getattr(args, "filter", None) or "").strip().lower()
    vid = _hex_id(getattr(args, "vid", None), "--vid")
    pid = _hex_id(getattr(args, "pid", None), "--pid")

    devices = hid_io.enumerate_devices(hid_io.new_backend())
    rows: list[dict[str, Any]] = []
    for index, info in enumerate(devices):
        haystack = (
            f"{info.product} {info.manufacturer} {info.serial} {info.id_text}".lower()
        )
        if needle and needle not in haystack:
            continue
        if vid is not None and info.vendor_id != vid:
            continue
        if pid is not None and info.product_id != pid:
            continue
        rows.append(hid_io.device_summary(info, index))
    return {"devices": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def _add_device_options(parser) -> None:
    """添加设备选择参数（send/monitor 共用）。"""
    parser.add_argument(
        "--index", type=int, metavar="N", help="本次 hid list 顺序（0 基）。"
    )
    parser.add_argument(
        "--path-b64",
        metavar="B64",
        help="设备路径的标准 base64（见 hid list 的 path_b64）。",
    )
    parser.add_argument("--vid", metavar="HEX", help="厂商 ID（0–FFFF，支持 0x 前缀）。")
    parser.add_argument("--pid", metavar="HEX", help="产品 ID（0–FFFF，支持 0x 前缀）。")
    parser.add_argument(
        "--serial", metavar="S", help="序列号精确匹配，只能配合 --vid/--pid。"
    )
    parser.add_argument(
        "--interface",
        type=int,
        metavar="N",
        help="接口编号精确匹配，只能配合 --vid/--pid。",
    )


def _register_send(subparsers) -> None:
    parser = subparsers.add_parser(
        "send",
        help="向 USB HID 设备写入一个 Output Report，可选读取输入报告。",
        description="报告按 --report-id/--report-length 填充或截断后写入；"
        "--read 时只读取一次输入报告，--timeout 0 表示读不等待。",
    )
    _add_device_options(parser)
    add_input_options(parser, required=True)
    parser.add_argument(
        "--encoding",
        default="utf-8",
        metavar="CODEC",
        help="输入文本与 input_report.text 的编码，默认 utf-8。",
    )
    parser.add_argument(
        "--report-id",
        type=int,
        default=_DEFAULT_REPORT_ID,
        metavar="N",
        help=f"报告 ID（0–255），0 表示无编号报告，默认 {_DEFAULT_REPORT_ID}。",
    )
    parser.add_argument(
        "--report-length",
        type=int,
        default=_DEFAULT_REPORT_LENGTH,
        metavar="N",
        help=f"报告总长度（含报告 ID，1–1024），默认 {_DEFAULT_REPORT_LENGTH}。",
    )
    parser.add_argument(
        "--read",
        action="store_true",
        help="写入后读取一次输入报告。",
    )
    parser.add_argument(
        "--read-length",
        type=int,
        default=_DEFAULT_READ_LENGTH,
        metavar="N",
        help=f"读取输入报告的最大字节数（1–1024），默认 {_DEFAULT_READ_LENGTH}。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"读取输入报告的等待秒数，默认 {_DEFAULT_TIMEOUT:g}；0 表示不等待。",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=_DEFAULT_REPEAT,
        metavar="N",
        help=f"写入次数，默认 {_DEFAULT_REPEAT}。",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=_DEFAULT_INTERVAL_MS,
        metavar="N",
        help=f"两次写入之间的间隔毫秒数，默认 {_DEFAULT_INTERVAL_MS}。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_send, command="hid.send")


def run_send(args) -> dict[str, Any]:
    """写入报告并按需读一次输入报告，返回 ``written``/``report``/``device``。"""
    data = read_input(args)
    report_id = _bounded_int(
        getattr(args, "report_id", _DEFAULT_REPORT_ID),
        "--report-id",
        0,
        _MAX_REPORT_ID,
    )
    report_length = _bounded_int(
        getattr(args, "report_length", _DEFAULT_REPORT_LENGTH),
        "--report-length",
        1,
        _MAX_REPORT_LENGTH,
    )
    read_length = _bounded_int(
        getattr(args, "read_length", _DEFAULT_READ_LENGTH),
        "--read-length",
        1,
        _MAX_REPORT_LENGTH,
    )
    timeout = _finite(getattr(args, "timeout", _DEFAULT_TIMEOUT), "--timeout")
    repeat = _positive_int(getattr(args, "repeat", _DEFAULT_REPEAT), "--repeat")
    interval_ms = _non_negative_int(
        getattr(args, "interval_ms", _DEFAULT_INTERVAL_MS), "--interval-ms"
    )

    try:
        report = hid_io.build_report(data.data, report_id, report_length)
    except ValueError as exc:
        raise CliError("usage", f"发送参数无效：{exc}", EXIT_USAGE) from exc

    backend, info, index, handle = _open_device(args)
    writes = 0
    total = 0
    input_report: dict[str, Any] | None = None
    try:
        for position in range(repeat):
            if position and interval_ms:
                time.sleep(interval_ms / 1000.0)
            try:
                written = int(backend.write(handle, report))
            except CliError:
                raise
            except Exception as exc:
                raise CliError(
                    "device_unavailable",
                    f"向 HID 设备 {_ident(info)} 写入报告失败：{exc}",
                    EXIT_OPERATION,
                ) from exc
            if written < 0:
                raise CliError(
                    "device_unavailable",
                    f"向 HID 设备 {_ident(info)} 写入报告失败：设备返回 {written}。",
                    EXIT_OPERATION,
                )
            writes += 1
            total += written
        if bool(getattr(args, "read", False)):
            try:
                raw = backend.read(handle, read_length, int(timeout * 1000))
            except CliError:
                raise
            except Exception as exc:
                raise CliError(
                    "device_unavailable",
                    f"从 HID 设备 {_ident(info)} 读取输入报告失败：{exc}",
                    EXIT_OPERATION,
                ) from exc
            raw = bytes(raw) if raw else b""
            input_report = {
                "hex": raw.hex(),
                "text": decode_bytes(raw, data.encoding),
                "size": len(raw),
            }
    finally:
        _close(backend, handle)

    result: dict[str, Any] = {
        "written": {"writes": writes, "bytes": total},
        "report": report.hex(),
        "device": hid_io.device_summary(info, index),
    }
    if input_report is not None:
        result["input_report"] = input_report
    return result


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------


def _register_monitor(subparsers) -> None:
    parser = subparsers.add_parser(
        "monitor",
        help="持续读取 HID 输入报告，逐行输出 data 事件并返回汇总（有界，固定 NDJSON）。",
        description="打开设备后循环读取，只逐行输出 data 事件，结束时返回时长/字节/"
        "事件数/报告长度汇总；必须用 --duration 限定运行时长，可用 "
        "--max-bytes/--max-events 提前结束。输出固定为 NDJSON，"
        "--format text、--pretty 与 --until/--until-hex 都视为参数错误（退出码 2）。",
    )
    _add_device_options(parser)
    parser.add_argument(
        "--report-length",
        type=int,
        default=_DEFAULT_REPORT_LENGTH,
        metavar="N",
        help=f"单次读取的最大字节数（1–1024），默认 {_DEFAULT_REPORT_LENGTH}。",
    )
    add_stream_options(parser)
    add_output_options(parser)
    parser.set_defaults(handler=run_monitor, command="hid.monitor")


def run_monitor(args) -> dict[str, Any]:
    """流式读取 HID 输入报告，返回 ``duration_s``/``bytes``/``events``/``report_length``。"""
    _require_ndjson(args)
    if getattr(args, "until", None) is not None or getattr(args, "until_hex", None) is not None:
        raise CliError(
            "usage",
            "hid monitor 不支持 --until/--until-hex，请用 --duration/--max-bytes/"
            "--max-events 结束。",
            EXIT_USAGE,
        )
    options = read_stream_options(args)
    report_length = _bounded_int(
        getattr(args, "report_length", _DEFAULT_REPORT_LENGTH),
        "--report-length",
        1,
        _MAX_REPORT_LENGTH,
    )

    backend, info, _index, handle = _open_device(args)
    started = hid_io._clock()
    deadline = started + options.duration
    total = 0
    events = 0
    try:
        while True:
            # 每一轮（包括空块）都重新检查截止时间，保证不会越界等待。
            remaining = deadline - hid_io._clock()
            if remaining <= 0:
                break
            # 单次 read 的等待窗口收缩到剩余时间，避免读超时越过 --duration。
            timeout_ms = max(1, int(min(hid_io.READ_TIMEOUT_MS, remaining * 1000)))
            try:
                chunk = backend.read(handle, report_length, timeout_ms)
            except CliError:
                raise
            except Exception as exc:
                message = f"读取 HID 设备 {_ident(info)} 失败：{exc}"
                # 流式命令运行中失败：先输出一行 error 事件，再由入口输出标准错误信封。
                emit_event(
                    {
                        "event": "error",
                        "code": "device_unavailable",
                        "message": message,
                        "at": hid_io._timestamp(),
                    }
                )
                raise CliError(
                    "device_unavailable",
                    message,
                    EXIT_OPERATION,
                ) from exc
            chunk = bytes(chunk) if chunk else b""

            if options.max_bytes is not None:
                budget = options.max_bytes - total
                if budget <= 0:
                    break
                if len(chunk) > budget:
                    chunk = chunk[:budget]
            if not chunk:
                pause = min(EMPTY_READ_PAUSE, deadline - hid_io._clock())
                if pause > 0:
                    time.sleep(pause)
                continue

            total += len(chunk)
            events += 1
            emit_event(
                {
                    "event": "data",
                    "hex": chunk.hex(),
                    "text": decode_bytes(chunk, "utf-8"),
                    "size": len(chunk),
                    "at": hid_io._timestamp(),
                }
            )
            if options.max_bytes is not None and total >= options.max_bytes:
                break
            if options.max_events is not None and events >= options.max_events:
                break
    finally:
        _close(backend, handle)

    return {
        "duration_s": round(max(hid_io._clock() - started, 0.0), 3),
        "bytes": total,
        "events": events,
        "report_length": report_length,
    }


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


def _require_ndjson(args) -> None:
    """流式命令固定输出 NDJSON：拒绝 ``--format text`` 与 ``--pretty``。"""
    if str(getattr(args, "format", "json")) == "text":
        raise CliError(
            "usage",
            "流式命令固定输出 NDJSON，不支持 --format text。",
            EXIT_USAGE,
        )
    if bool(getattr(args, "pretty", False)):
        raise CliError(
            "usage",
            "流式命令固定输出 NDJSON，不支持 --pretty。",
            EXIT_USAGE,
        )


def _selection(args) -> dict[str, Any]:
    """校验设备选择参数，返回 :func:`hid_io.resolve_device` 的关键字参数。"""
    index = getattr(args, "index", None)
    path_b64 = getattr(args, "path_b64", None)
    vid_raw = getattr(args, "vid", None)
    pid_raw = getattr(args, "pid", None)
    serial = getattr(args, "serial", None)
    interface = getattr(args, "interface", None)
    vid = _hex_id(vid_raw, "--vid")
    pid = _hex_id(pid_raw, "--pid")

    modes: list[str] = []
    if index is not None:
        modes.append("--index")
    if path_b64 is not None:
        modes.append("--path-b64")
    if vid_raw is not None or pid_raw is not None:
        modes.append("--vid/--pid")
    if len(modes) > 1:
        raise CliError(
            "usage",
            "设备只能用 --index、--path-b64 或 --vid/--pid 三者之一指定，"
            f"当前同时给了 {'、'.join(modes)}。",
            EXIT_USAGE,
        )
    if not modes:
        raise CliError(
            "usage",
            "必须用 --index、--path-b64 或 --vid/--pid 指定 HID 设备。",
            EXIT_USAGE,
        )
    if (vid_raw is not None) != (pid_raw is not None):
        raise CliError("usage", "--vid 与 --pid 必须同时提供。", EXIT_USAGE)
    if (serial is not None or interface is not None) and vid_raw is None:
        raise CliError(
            "usage", "--serial/--interface 只能配合 --vid 与 --pid 使用。", EXIT_USAGE
        )
    return {
        "index": index,
        "path_b64": path_b64,
        "vid": vid,
        "pid": pid,
        "serial": serial,
        "interface": interface,
    }


def _open_device(args) -> tuple[Any, Any, int, object]:
    """解析并打开设备；打开失败收敛成 ``device_unavailable``。"""
    selection = _selection(args)
    backend = hid_io.new_backend()
    try:
        info, index = hid_io.resolve_device_entry(backend=backend, **selection)
    except ValueError as exc:
        raise CliError("usage", str(exc), EXIT_USAGE) from exc
    try:
        backend, handle = hid_io.open_device(info, backend=backend)
    except CliError:
        raise
    except Exception as exc:
        raise CliError(
            "device_unavailable",
            f"打开 HID 设备失败：{exc}（{_ident(info)}）",
            EXIT_OPERATION,
        ) from exc
    return backend, info, index, handle


def _ident(info: Any) -> str:
    """设备标识：``vid:pid`` 加 ``path_b64``，让错误消息能直接定位设备。"""
    return (
        f"{info.vendor_id:04X}:{info.product_id:04X}"
        f"（path {hid_io.encode_path(info.path)}）"
    )


def _close(backend, handle) -> None:
    """关闭句柄；关闭失败只向 stderr 写一行摘要，不覆盖已有结果或错误。"""
    try:
        backend.close(handle)
    except Exception as exc:
        _write_stderr(f"警告：关闭 HID 设备失败：{exc}")


def _write_stderr(message: str) -> None:
    """窗口化 exe 的 stderr 可能不可用：写摘要失败时静默跳过。"""
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(message + "\n")
        stream.flush()
    except Exception:
        pass


def _hex_id(value, name: str) -> int | None:
    """把 ``--vid``/``--pid`` 解析成 0–FFFF 的整数（支持 ``0x`` 前缀）。"""
    if value is None:
        return None
    text = str(value).strip()
    try:
        number = int(text, 16)
    except ValueError as exc:
        raise CliError(
            "usage",
            f"{name} 必须是 0–FFFF 的十六进制数，收到 {value}。",
            EXIT_USAGE,
        ) from exc
    if not 0 <= number <= _MAX_ID:
        raise CliError(
            "usage", f"{name} 必须在 0–{_MAX_ID} 之间，收到 {value}。", EXIT_USAGE
        )
    return number


def _bounded_int(value, name: str, minimum: int, maximum: int) -> int:
    """整数且必须落在 ``minimum``–``maximum`` 之间。"""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CliError("usage", f"{name} 必须是整数。", EXIT_USAGE) from exc
    if not minimum <= number <= maximum:
        raise CliError(
            "usage",
            f"{name} 必须在 {minimum}–{maximum} 之间，收到 {value}。",
            EXIT_USAGE,
        )
    return number


def _finite(value, name: str) -> float:
    """有限非负数；nan/inf/负数一律报参数错误。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CliError("usage", f"{name} 必须是数字。", EXIT_USAGE) from exc
    if not math.isfinite(number) or number < 0:
        raise CliError(
            "usage", f"{name} 必须是有限的非负数，收到 {value}。", EXIT_USAGE
        )
    return number


def _positive_int(value, name: str) -> int:
    """正整数。"""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CliError("usage", f"{name} 必须是整数。", EXIT_USAGE) from exc
    if number < 1:
        raise CliError("usage", f"{name} 必须是正整数，收到 {value}。", EXIT_USAGE)
    return number


def _non_negative_int(value, name: str) -> int:
    """非负整数。"""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CliError("usage", f"{name} 必须是整数。", EXIT_USAGE) from exc
    if number < 0:
        raise CliError("usage", f"{name} 不能为负数，收到 {value}。", EXIT_USAGE)
    return number

"""``ble`` 命令组：BLE 扫描、服务树、特征值读写与有界通知流。

- ``scan``：按 ``--duration`` 扫描，按名称/地址子串过滤，返回 ``name``/``address``/``rssi``；
- ``services``：连接 → 取服务树与 MTU → 断开，UUID 附 ``service_label``/
  ``characteristic_label`` 中文标签；
- ``read``：连接 → 读一个特征值 → 断开，返回 ``char``/``hex``/``text``/``size``；
- ``write``：连接 → 按 ``--mtu-chunk`` 分片、按 ``--repeat`` 写入 → 断开；
- ``notify``：订阅 → 逐条输出 ``notification`` 事件与最终 ``end`` 事件 → 取消订阅并断开，
  固定 NDJSON，由 ``--duration``/``--max-bytes``/``--max-events``/``--until-hex`` 保证有界。

复用 :mod:`app.cli.transports.ble_io` 驱动的 ``BleakBackend``，但不启动 ``BleWorker``；
``ble_io`` 的导入不触碰 ``app.io.ble_worker``、``bleak`` 与 PyQt6。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from app.cli.errors import EXIT_USAGE, CliError
from app.cli.inputs import add_input_options, read_input
from app.cli.output import add_output_options, emit_event
from app.cli.reads import add_stream_options, make_matcher, read_stream_options
from app.cli.transports import ble_io

__all__ = [
    "register",
    "run_notify",
    "run_read",
    "run_scan",
    "run_services",
    "run_write",
]

_DEFAULT_TIMEOUT = ble_io.DEFAULT_TIMEOUT
_DEFAULT_REPEAT = 1
_DEFAULT_INTERVAL_MS = 100
_MAX_SECONDS = 3600.0


def register(subparsers) -> None:
    """注册 ``ble`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "ble",
        help="蓝牙低功耗：扫描、服务树、特征值读写与通知订阅。",
        description="直接使用 bleak 后端，每条命令独立「连接→操作→断开」；"
        "可用子命令：scan、services、read、write、notify。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    _register_scan(group)
    _register_services(group)
    _register_read(group)
    _register_write(group)
    _register_notify(group)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def _register_scan(subparsers) -> None:
    parser = subparsers.add_parser(
        "scan",
        help="扫描 BLE 广播，返回设备名称、地址与信号强度。",
        description="扫描 --duration 秒后返回 devices 列表；--filter 按设备名称或地址"
        "做大小写不敏感的子串过滤；同一地址只保留最后一次信息。",
    )
    parser.add_argument(
        "--duration",
        type=float,
        required=True,
        metavar="SECONDS",
        help="扫描时长（秒，大于 0 且不超过 3600）。",
    )
    parser.add_argument(
        "--filter",
        metavar="TEXT",
        help="按设备名称或地址的子串过滤（大小写不敏感）。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_scan, command="ble.scan")


def run_scan(args) -> dict[str, Any]:
    """扫描并返回 ``devices`` 列表与 ``count``。"""
    duration = _duration(getattr(args, "duration", None), "--duration")
    devices = asyncio.run(
        ble_io.scan(ble_io.new_backend(), duration, getattr(args, "filter", None))
    )
    return {
        "devices": [ble_io.device_summary(info) for info in devices],
        "count": len(devices),
    }


# ---------------------------------------------------------------------------
# services / read
# ---------------------------------------------------------------------------


def _add_target_options(parser) -> None:
    """添加 ``--address``/``--char`` 与蓝牙连接超时（services/read/write/notify 共用）。"""
    parser.add_argument(
        "--address",
        required=True,
        metavar="ADDR",
        help="BLE 设备地址，如 AA:BB:CC:DD:EE:FF。",
    )
    parser.add_argument(
        "--char",
        required=True,
        metavar="UUID",
        help="特征值 UUID，支持 4 位短 UUID（如 FFE1）或完整 128 位 UUID。",
    )
    _add_timeout_option(parser)


def _add_timeout_option(parser) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"连接超时秒数（大于 0 且不超过 3600），默认 {_DEFAULT_TIMEOUT:g}。",
    )


def _register_services(subparsers) -> None:
    parser = subparsers.add_parser(
        "services",
        help="列出 BLE 设备的服务、特征值、属性与 MTU。",
        description="连接后读取 GATT 服务树，UUID 附中文标签，"
        "characteristics 里的 subscribed 固定为 false。",
    )
    parser.add_argument(
        "--address", required=True, metavar="ADDR", help="BLE 设备地址。"
    )
    _add_timeout_option(parser)
    add_output_options(parser)
    parser.set_defaults(handler=run_services, command="ble.services")


def run_services(args) -> dict[str, Any]:
    """返回 ``mtu`` 与 ``services``。"""
    address = _required_text(getattr(args, "address", None), "--address")
    timeout = _timeout(getattr(args, "timeout", None))
    return asyncio.run(ble_io.services(ble_io.new_backend(), address, timeout))


def _register_read(subparsers) -> None:
    parser = subparsers.add_parser(
        "read",
        help="读取一个 BLE 特征值，返回 hex、text 与字节数。",
        description="连接 → 读取 → 断开；读取失败按 device_unavailable（退出码 3）。",
    )
    _add_target_options(parser)
    add_output_options(parser)
    parser.set_defaults(handler=run_read, command="ble.read")


def run_read(args) -> dict[str, Any]:
    """返回 ``char``/``hex``/``text``/``size``。"""
    address = _required_text(getattr(args, "address", None), "--address")
    char = _required_text(getattr(args, "char", None), "--char")
    timeout = _timeout(getattr(args, "timeout", None))
    return asyncio.run(ble_io.read(ble_io.new_backend(), address, char, timeout))


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _register_write(subparsers) -> None:
    parser = subparsers.add_parser(
        "write",
        help="向 BLE 特征值写入数据，可选按 MTU 分片与无响应写入。",
        description="写入失败按 device_unavailable（退出码 3）；--mtu-chunk 按 MTU-3 "
        "分片写入，不给时整块写入；--repeat 次之间按 --interval-ms 停顿。",
    )
    _add_target_options(parser)
    add_input_options(parser, required=True)
    parser.add_argument(
        "--encoding",
        default="utf-8",
        metavar="CODEC",
        help="--text 输入的文本编码，默认 utf-8。",
    )
    parser.add_argument(
        "--no-response",
        action="store_true",
        help="使用无响应写入（write-without-response），默认需要设备应答。",
    )
    parser.add_argument(
        "--mtu-chunk",
        action="store_true",
        help="按已协商的 MTU 分片写入（单片上限 MTU-3）。",
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
    parser.set_defaults(handler=run_write, command="ble.write")


def run_write(args) -> dict[str, Any]:
    """写入数据，返回 ``address``/``char``/``chunks``/``bytes``/``mtu``/``response``。"""
    address = _required_text(getattr(args, "address", None), "--address")
    char = _required_text(getattr(args, "char", None), "--char")
    timeout = _timeout(getattr(args, "timeout", None))
    repeat = _positive_int(getattr(args, "repeat", _DEFAULT_REPEAT), "--repeat")
    interval_ms = _non_negative_int(
        getattr(args, "interval_ms", _DEFAULT_INTERVAL_MS), "--interval-ms"
    )
    data = read_input(args)
    result = asyncio.run(
        ble_io.write(
            ble_io.new_backend(),
            address,
            char,
            data.data,
            response=not bool(getattr(args, "no_response", False)),
            mtu_chunk=bool(getattr(args, "mtu_chunk", False)),
            timeout=timeout,
            repeat=repeat,
            interval_ms=interval_ms,
        )
    )
    return {"address": address, "char": char, **result}


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------


def _register_notify(subparsers) -> None:
    parser = subparsers.add_parser(
        "notify",
        help="订阅 BLE 通知，按 NDJSON 输出 notification/end 事件（有界）。",
        description="连接并订阅后逐行输出 notification 事件，结束时输出 end 事件；"
        "必须用 --duration 限定运行时长，可用 --max-bytes/--max-events/--until-hex "
        "提前结束。输出固定为 NDJSON，--format text、--pretty 与 --until 都视为"
        "参数错误（退出码 2）。",
    )
    _add_target_options(parser)
    add_stream_options(parser)
    add_output_options(parser)
    parser.set_defaults(handler=run_notify, command="ble.notify")


def run_notify(args) -> dict[str, Any]:
    """订阅通知并输出事件，返回 ``duration_s``/``bytes``/``events``/``matched``。"""
    _require_ndjson(args)
    if getattr(args, "until", None) is not None:
        raise CliError(
            "usage",
            "ble notify 不支持 --until，请改用 --until-hex。",
            EXIT_USAGE,
        )
    address = _required_text(getattr(args, "address", None), "--address")
    char = _required_text(getattr(args, "char", None), "--char")
    timeout = _timeout(getattr(args, "timeout", None))
    options = read_stream_options(args)
    matcher = make_matcher(expect_hex=options.until_hex)
    return asyncio.run(
        ble_io.notify(
            ble_io.new_backend(),
            address,
            char,
            duration=options.duration,
            emit=emit_event,
            timeout=timeout,
            matcher=matcher,
            max_bytes=options.max_bytes,
            max_events=options.max_events,
        )
    )


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


def _required_text(value, name: str) -> str:
    """去掉首尾空白后必须非空；空串按参数错误处理。"""
    text = str(value or "").strip()
    if not text:
        raise CliError("usage", f"{name} 不能为空。", EXIT_USAGE)
    return text


def _duration(value, name: str) -> float:
    """扫描时长：有限正数，且不超过 3600 秒（允许 0.1 这类短扫描）。"""
    number = _finite(value, name)
    if not 0 < number <= _MAX_SECONDS:
        raise CliError(
            "usage",
            f"{name} 必须大于 0 且不超过 {_MAX_SECONDS:g} 秒，收到 {value}。",
            EXIT_USAGE,
        )
    return number


def _timeout(value) -> float:
    """连接超时：有限正数，且不超过 3600 秒。"""
    number = _finite(value, "--timeout")
    if not 0 < number <= _MAX_SECONDS:
        raise CliError(
            "usage",
            f"--timeout 必须大于 0 且不超过 {_MAX_SECONDS:g} 秒，收到 {value}。",
            EXIT_USAGE,
        )
    return number


def _finite(value, name: str) -> float:
    """有限数字；nan/inf 与非数字一律报参数错误。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CliError("usage", f"{name} 必须是数字。", EXIT_USAGE) from exc
    if not math.isfinite(number):
        raise CliError(
            "usage", f"{name} 必须是有限数字，收到 {value}。", EXIT_USAGE
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

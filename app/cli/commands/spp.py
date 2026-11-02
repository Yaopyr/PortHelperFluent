"""``spp`` 命令组：经典蓝牙扫描、配对感知发送与系统 COM 备用通道。

- ``scan``：按 ``--duration`` 扫描经典蓝牙设备，返回 ``name``/``address``/``paired``/
  ``device_id``，可按名称/地址子串过滤、只看已配对设备；
- ``send``：``--address`` 先做一次短扫描（找到即停）解析设备，``--device-id`` 跳过扫描；
  未配对时用 ``--pin``/``--confirm`` 应答系统配对提示，然后连接、发送并按需等待回包；
- ``com``：复用 ``serial`` 的连接参数与 Task 11 的串口 transport，相当于走系统蓝牙
  COM 口的 ``serial send``，写入一次输入并按 ``--expect``/``--read-for`` 读取。

复用 :mod:`app.cli.transports.spp_io` 驱动的 ``WinRtSppBackend``，但不启动 ``SppWorker``；
``spp_io`` 的导入不触碰 ``app.io.spp_worker``、WinRT 与 PyQt6。``com`` 的串口参数
直接复用 ``serial`` 命令模块的 ``add_connection_options``/``build_config``。
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from typing import Any

from app.cli.commands import serial
from app.cli.errors import EXIT_EXPECT, EXIT_OPERATION, EXIT_USAGE, CliError
from app.cli.inputs import add_input_options, read_input
from app.cli.output import add_output_options
from app.cli.reads import make_matcher, read_until
from app.cli.transports import serial_io, spp_io
from app.core.encoding import decode_bytes
from app.services.bluetooth import validate_pin

__all__ = ["register", "run_com", "run_scan", "run_send"]

_DEFAULT_SCAN_DURATION = 8.0
_DEFAULT_TIMEOUT = spp_io.DEFAULT_TIMEOUT
_DEFAULT_READ_FOR = spp_io.DEFAULT_READ_FOR
_DEFAULT_COM_TIMEOUT = 5.0
_DEFAULT_REPEAT = 1
_DEFAULT_INTERVAL_MS = 100
_MAX_SECONDS = 3600.0

# 串口单次 ``read`` 的上界；等待窗口由 ``--expect``/``--read-for`` 约束。
READ_CHUNK = 4096


def register(subparsers) -> None:
    """注册 ``spp`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "spp",
        help="经典蓝牙：扫描设备、配对后走 RFCOMM 收发，或改用系统蓝牙 COM 口。",
        description="直接使用 WinRT SPP 后端，每条命令独立「配对→连接→操作→关闭」；"
        "可用子命令：scan、send、com。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    _register_scan(group)
    _register_send(group)
    _register_com(group)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def _register_scan(subparsers) -> None:
    parser = subparsers.add_parser(
        "scan",
        help="扫描经典蓝牙设备，返回名称、地址、配对状态与设备 ID。",
        description="扫描 --duration 秒后返回 devices 列表；--filter 按设备名称或地址"
        "做大小写不敏感的子串过滤；--paired-only 只保留已配对设备。",
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
    parser.add_argument(
        "--paired-only",
        action="store_true",
        help="只返回已在系统中配对的设备。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_scan, command="spp.scan")


def run_scan(args) -> dict[str, Any]:
    """扫描并返回 ``devices`` 列表与 ``count``。"""
    duration = _duration(getattr(args, "duration", None), "--duration")
    filter_text = getattr(args, "filter", None)
    if filter_text is not None:
        filter_text = _required_text(filter_text, "--filter")
    devices = asyncio.run(
        spp_io.scan(
            spp_io.new_backend(),
            duration,
            filter_text=filter_text,
            paired_only=bool(getattr(args, "paired_only", False)),
        )
    )
    return {
        "devices": [spp_io.device_summary(device) for device in devices],
        "count": len(devices),
    }


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def _register_send(subparsers) -> None:
    parser = subparsers.add_parser(
        "send",
        help="向经典蓝牙设备发送数据并按需等待回包。",
        description="--address 会先短扫描解析设备（找到即停），--device-id 跳过扫描；"
        "未配对时用 --pin/--confirm 应答系统提示，未提供则直接提示先配对；"
        "未命中 --expect/--expect-hex 以退出码 4 结束。",
    )
    targets = parser.add_mutually_exclusive_group(required=True)
    targets.add_argument(
        "--address", metavar="ADDR", help="设备蓝牙地址，如 AA:BB:CC:DD:EE:FF。"
    )
    targets.add_argument(
        "--device-id",
        metavar="ID",
        help="设备的系统 ID（可直接使用 spp scan 的输出），跳过扫描。",
    )
    parser.add_argument(
        "--scan-duration",
        type=float,
        default=_DEFAULT_SCAN_DURATION,
        metavar="SECONDS",
        help=f"解析 --address 的扫描时长，默认 {_DEFAULT_SCAN_DURATION:g} 秒。",
    )
    add_input_options(parser, required=True)
    parser.add_argument(
        "--pin",
        metavar="PIN",
        help="系统请求 PIN 时的应答（1–16 位数字）；已配对设备不会用到。",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="自动接受系统发送的配对确认提示。",
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
    parser.add_argument("--expect", metavar="TEXT", help="等待该文本（按 utf-8 解码）。")
    parser.add_argument("--expect-hex", metavar="HEX", help="等待该十六进制字节序列。")
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"配对/连接与等待回包的超时秒数，默认 {_DEFAULT_TIMEOUT:g}。",
    )
    parser.add_argument(
        "--read-for",
        type=float,
        default=_DEFAULT_READ_FOR,
        metavar="SECONDS",
        help=f"没有 --expect 时的固定读取窗口，默认 {_DEFAULT_READ_FOR:g} 秒；0 表示不读。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_send, command="spp.send")


def run_send(args) -> dict[str, Any]:
    """解析目标设备并发送，返回 ``sent``/``received``/``matched``/``device``。"""
    address = getattr(args, "address", None)
    device_id = getattr(args, "device_id", None)
    if address is not None:
        address = _required_text(address, "--address")
    if device_id is not None:
        device_id = _required_text(device_id, "--device-id")

    pin = getattr(args, "pin", None)
    if pin is not None:
        problem = validate_pin(pin)
        if problem:
            raise CliError("usage", problem, EXIT_USAGE)

    scan_duration = _duration(
        getattr(args, "scan_duration", _DEFAULT_SCAN_DURATION), "--scan-duration"
    )
    timeout = _timeout(getattr(args, "timeout", _DEFAULT_TIMEOUT))
    read_for = _read_for(getattr(args, "read_for", _DEFAULT_READ_FOR))
    repeat = _positive_int(getattr(args, "repeat", _DEFAULT_REPEAT), "--repeat")
    interval_ms = _non_negative_int(
        getattr(args, "interval_ms", _DEFAULT_INTERVAL_MS), "--interval-ms"
    )

    data = read_input(args)
    matcher = make_matcher(
        expect=getattr(args, "expect", None),
        expect_hex=getattr(args, "expect_hex", None),
        encoding=data.encoding,
    )

    backend = spp_io.new_backend()
    if address is not None:
        device = asyncio.run(spp_io.resolve_address(backend, address, scan_duration))
    else:
        from app.io.spp_worker import ClassicDeviceInfo

        # 设备 ID 路径不扫描，配对状态未知：交给 backend.pair() 判断，
        # 已配对设备会返回 ALREADY_PAIRED，不会弹 PIN。
        device = ClassicDeviceInfo(
            name="", address="", paired=False, device_id=device_id
        )

    return asyncio.run(
        spp_io.send(
            backend,
            device,
            data.data,
            pin=pin,
            confirm=bool(getattr(args, "confirm", False)),
            timeout=timeout,
            read_for=read_for,
            repeat=repeat,
            interval_ms=interval_ms,
            matcher=matcher,
        )
    )


# ---------------------------------------------------------------------------
# com
# ---------------------------------------------------------------------------


def _register_com(subparsers) -> None:
    parser = subparsers.add_parser(
        "com",
        help="通过系统蓝牙 COM 口发送数据并按需等待回包。",
        description="相当于使用蓝牙虚拟串口的 serial send：写入一次输入（不追加行尾、"
        "不重复），有 --expect/--expect-hex 时等待命中，否则按 --read-for 读取。",
    )
    parser.add_argument("--port", required=True, metavar="PORT", help="串口名称，如 COM9。")
    serial.add_connection_options(parser)
    add_input_options(parser, required=True)
    parser.add_argument("--expect", metavar="TEXT", help="等待该文本（按 --encoding 解码）。")
    parser.add_argument("--expect-hex", metavar="HEX", help="等待该十六进制字节序列。")
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_COM_TIMEOUT,
        metavar="SECONDS",
        help=f"等待 --expect/--expect-hex 的秒数，默认 {_DEFAULT_COM_TIMEOUT:g}。",
    )
    parser.add_argument(
        "--read-for",
        type=float,
        default=_DEFAULT_READ_FOR,
        metavar="SECONDS",
        help=f"没有 --expect 时的固定读取窗口，默认 {_DEFAULT_READ_FOR:g} 秒；0 表示不读。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_com, command="spp.com")


def run_com(args) -> dict[str, Any]:
    """写入一次输入并读取回包，返回 ``sent``/``received``/``matched``/``modem``。"""
    port = _required_text(getattr(args, "port", None), "--port")
    timeout = _timeout(getattr(args, "timeout", _DEFAULT_COM_TIMEOUT))
    read_for = _read_for(getattr(args, "read_for", _DEFAULT_READ_FOR))

    data = read_input(args)
    matcher = make_matcher(
        expect=getattr(args, "expect", None),
        expect_hex=getattr(args, "expect_hex", None),
        encoding=data.encoding,
    )

    config = replace(serial.build_config(args), port=port)

    transport = None
    total = 0
    matched = False
    outcome = None
    modem: dict[str, bool] = {}
    try:
        transport = _new_transport(config)
        _open(transport, config)
        try:
            total = int(transport.write(data.data))
            outcome = _read_response(transport, matcher, timeout, read_for)
            matched = outcome.matched
            modem = serial._normalize_modem(transport.modem_status())
        except CliError:
            raise
        except Exception as exc:
            raise CliError(
                "device_unavailable",
                f"串口 {config.port} 读写失败：{exc}",
                EXIT_OPERATION,
            ) from exc
    finally:
        if transport is not None:
            _close(transport)

    if matcher is not None and not matched:
        raise CliError(
            "expect_mismatch",
            "等待 --expect/--expect-hex 超时未命中。",
            EXIT_EXPECT,
        )

    return {
        "sent": {"bytes": total, "writes": 1},
        "received": _received_data(outcome.data, data.encoding),
        "matched": matched,
        "modem": modem,
    }


def _new_transport(config: Any):
    """构造 transport；构造失败（如驱动缺失）同样收敛成 ``device_unavailable``。"""
    try:
        return serial_io.open_transport()
    except CliError:
        raise
    except Exception as exc:
        raise CliError(
            "device_unavailable",
            f"打开串口 {config.port} 失败：{exc}",
            EXIT_OPERATION,
        ) from exc


def _open(transport: Any, config: Any) -> None:
    """打开端口；任何失败都收敛成 ``device_unavailable``。"""
    try:
        transport.open(config)
    except CliError:
        raise
    except Exception as exc:
        raise CliError(
            "device_unavailable",
            f"打开串口 {config.port} 失败：{exc}",
            EXIT_OPERATION,
        ) from exc


def _close(transport: Any) -> None:
    """关闭端口；关闭失败不覆盖已有结果或错误。"""
    try:
        transport.close()
    except Exception:
        pass


def _read_response(transport: Any, matcher, timeout: float, read_for: float):
    """给了匹配器就等待命中，否则按 ``read_for`` 做固定窗口读取。"""

    def fetch(seconds: float) -> bytes:
        return transport.read(READ_CHUNK, seconds)

    if matcher is not None:
        return read_until(fetch, timeout=timeout, match=matcher)
    return read_until(fetch, timeout=0, read_for=read_for)


def _received_data(raw: bytes, encoding: str) -> dict[str, Any]:
    """把原始回包整理成 ``hex``/``text``/``size``。"""
    return {
        "hex": raw.hex(),
        "text": decode_bytes(raw, encoding),
        "size": len(raw),
    }


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


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
    """超时秒数：有限正数，且不超过 3600 秒。"""
    number = _finite(value, "--timeout")
    if not 0 < number <= _MAX_SECONDS:
        raise CliError(
            "usage",
            f"--timeout 必须大于 0 且不超过 {_MAX_SECONDS:g} 秒，收到 {value}。",
            EXIT_USAGE,
        )
    return number


def _read_for(value) -> float:
    """固定读取窗口：有限的非负数（0 表示不读）。"""
    number = _finite(value, "--read-for")
    if number < 0:
        raise CliError(
            "usage", f"--read-for 必须是有限的非负数，收到 {value}。", EXIT_USAGE
        )
    return number


def _finite(value, name: str) -> float:
    """有限数字；nan/inf 与非数字一律报参数错误。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CliError("usage", f"{name} 必须是数字。", EXIT_USAGE) from exc
    if not math.isfinite(number):
        raise CliError("usage", f"{name} 必须是有限数字，收到 {value}。", EXIT_USAGE)
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

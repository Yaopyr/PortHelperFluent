"""``serial`` 命令组：端口枚举、单次发送与调制解调器线状态读取。

- ``list``：枚举串口，可按 device/description/hwid 的子串过滤；
- ``send``：打开串口 → 可选 DTR/RTS → 可选 BREAK → 按 ``--repeat`` 写入 →
  按 ``--expect``/``--read-for`` 读取 → 关闭；
- ``modem``：打开、读取四个线状态、立即关闭。

复用 :mod:`app.io.serial_worker` 的 ``SerialConfig``/``PyserialTransport``/``list_ports``，
但不启动 ``SerialWorker``；这些导入都在函数内部完成，模块级导入只依赖标准库与 CLI
原语，不会把 pyserial/PyQt6 带进 CLI 进程。
"""

from __future__ import annotations

import time
from typing import Any

from app.cli.errors import EXIT_EXPECT, EXIT_OPERATION, EXIT_USAGE, CliError
from app.cli.inputs import add_input_options, read_input
from app.cli.output import add_output_options
from app.cli.reads import make_matcher, read_until
from app.cli.transports import serial_io
from app.core.encoding import decode_bytes

__all__ = [
    "add_connection_options",
    "build_config",
    "register",
    "run_list",
    "run_modem",
    "run_send",
]

# 串口单次 ``read`` 的上界；等待窗口由 ``--timeout``/``--read-for`` 约束。
READ_CHUNK = 4096

_LINE_ENDINGS = {
    "none": b"",
    "cr": b"\r",
    "lf": b"\n",
    "crlf": b"\r\n",
}

_PARITY = ("none", "even", "odd", "mark", "space")
_FLOW = ("none", "rtscts", "xonxoff")
_STOP_BITS = (1, 1.5, 2)


def register(subparsers) -> None:
    """注册 ``serial`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "serial",
        help="串口：端口枚举、发送并等待回包、读取调制解调器线状态。",
        description="直接使用 pyserial transport，不启动 GUI 工作线程；"
        "可用子命令：list、send、modem。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    _register_list(group)
    _register_send(group)
    _register_modem(group)


def add_connection_options(parser) -> None:
    """添加串口连接参数（与 GUI 的三种流控一致，不支持 dsrdtr）。"""
    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
        metavar="N",
        help="波特率，默认 115200。",
    )
    parser.add_argument(
        "--data-bits",
        type=int,
        default=8,
        metavar="N",
        help="数据位，默认 8。",
    )
    parser.add_argument(
        "--stop-bits",
        type=float,
        choices=_STOP_BITS,
        default=1,
        metavar="{" + ",".join(str(item) for item in _STOP_BITS) + "}",
        help="停止位：1、1.5 或 2，默认 1。",
    )
    parser.add_argument(
        "--parity",
        choices=_PARITY,
        default="none",
        help="校验位，默认 none。",
    )
    parser.add_argument(
        "--flow",
        choices=_FLOW,
        default="none",
        help="流控：none、rtscts、xonxoff，默认 none。",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        metavar="CODEC",
        help="文本编码，默认 utf-8。",
    )


def build_config(args) -> Any:
    """把连接参数映射成 ``app.io.serial_worker.SerialConfig``。"""
    from app.io.serial_worker import SerialConfig

    return SerialConfig(
        port=str(getattr(args, "port")),
        baud=int(getattr(args, "baud", 115200)),
        bytesize=int(getattr(args, "data_bits", 8)),
        stopbits=float(getattr(args, "stop_bits", 1)),
        parity=str(getattr(args, "parity", "none")),
        flow=str(getattr(args, "flow", "none")),
        encoding=str(getattr(args, "encoding", None) or "utf-8"),
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _register_list(subparsers) -> None:
    parser = subparsers.add_parser(
        "list",
        help="枚举串口，可按设备名/描述/硬件 ID 过滤。",
        description="返回当前系统可见的串口列表，--filter 做大小写不敏感的子串过滤。",
    )
    parser.add_argument(
        "--filter",
        metavar="TEXT",
        help="按 device、description 或 hwid 的子串过滤（大小写不敏感）。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_list, command="serial.list")


def run_list(args) -> dict[str, Any]:
    """返回 ``ports`` 列表与 ``count``。"""
    needle = str(getattr(args, "filter", None) or "").strip().lower()
    ports: list[dict[str, Any]] = []
    for info in serial_io.list_ports():
        haystack = f"{info.device} {info.description} {info.hwid}".lower()
        if needle and needle not in haystack:
            continue
        ports.append(
            {
                "device": info.device,
                "description": info.description,
                "hwid": info.hwid,
                "vid": info.vid,
                "pid": info.pid,
                "serial_number": info.serial_number,
            }
        )
    return {"ports": ports, "count": len(ports)}


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def _register_send(subparsers) -> None:
    parser = subparsers.add_parser(
        "send",
        help="向串口发送数据并按需等待回包。",
        description="打开串口发送 1..N 次输入，可用 --expect/--expect-hex 等待匹配；"
        "未命中以退出码 4 结束。",
    )
    parser.add_argument("--port", required=True, metavar="PORT", help="串口名称，如 COM9。")
    add_connection_options(parser)
    add_input_options(parser, required=True)
    parser.add_argument(
        "--line-ending",
        choices=("none", "cr", "lf", "crlf"),
        default="none",
        help="追加到输入末尾的换行符，默认 none。",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="重复写入次数，默认 1。",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=0,
        metavar="N",
        help="重复写入之间的间隔毫秒数，默认 0。",
    )
    parser.add_argument("--expect", metavar="TEXT", help="等待该文本（按 --encoding 解码）。")
    parser.add_argument("--expect-hex", metavar="HEX", help="等待该十六进制字节序列。")
    parser.add_argument(
        "--timeout",
        type=float,
        default=5,
        metavar="SECONDS",
        help="等待 --expect/--expect-hex 的秒数，默认 5。",
    )
    parser.add_argument(
        "--read-for",
        type=float,
        default=1,
        metavar="SECONDS",
        help="没有 --expect 时的固定读取窗口，默认 1 秒；0 表示不读。",
    )
    parser.add_argument("--dtr", choices=("on", "off"), help="打开后设置 DTR。")
    parser.add_argument("--rts", choices=("on", "off"), help="打开后设置 RTS。")
    parser.add_argument(
        "--send-break",
        action="store_true",
        help="写入前先发送 BREAK。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_send, command="serial.send")


def run_send(args) -> dict[str, Any]:
    """发送输入并读取回包，返回 ``sent``/``received``/``matched``/``modem``。"""
    repeat, interval_ms, timeout, read_for = _validate_send_args(args)
    data = read_input(args)
    matcher = make_matcher(
        expect=getattr(args, "expect", None),
        expect_hex=getattr(args, "expect_hex", None),
        encoding=data.encoding,
    )
    payload = data.data + _LINE_ENDINGS[str(getattr(args, "line_ending", "none"))]
    config = build_config(args)

    transport = serial_io.open_transport()
    matched = False
    outcome = None
    try:
        _open(transport, config)
        try:
            _apply_lines(transport, args)
            total = _write_repeated(transport, payload, repeat, interval_ms)
            outcome = _read_response(transport, matcher, timeout, read_for)
            matched = outcome.matched
            modem = _normalize_modem(transport.modem_status())
        except CliError:
            raise
        except Exception as exc:
            raise CliError(
                "device_unavailable",
                f"串口 {config.port} 读写失败：{exc}",
                EXIT_OPERATION,
            ) from exc
    finally:
        _close(transport)

    if matcher is not None and not matched:
        raise CliError(
            "expect_mismatch",
            "等待 --expect/--expect-hex 超时未命中。",
            EXIT_EXPECT,
        )

    received = _received_data(outcome.data, data.encoding)
    return {
        "sent": {"bytes": total, "writes": repeat},
        "received": received,
        "matched": matched,
        "modem": modem,
    }


def _validate_send_args(args) -> tuple[int, int, float, float]:
    """校验 ``--repeat``/``--interval-ms``/``--timeout``/``--read-for`` 的范围。"""
    repeat = int(getattr(args, "repeat", 1))
    if repeat < 1:
        raise CliError("usage", f"--repeat 必须是正整数，收到 {repeat}。", EXIT_USAGE)

    interval_ms = int(getattr(args, "interval_ms", 0))
    if interval_ms < 0:
        raise CliError("usage", f"--interval-ms 不能为负数，收到 {interval_ms}。", EXIT_USAGE)

    timeout = float(getattr(args, "timeout", 5))
    if timeout < 0:
        raise CliError("usage", f"--timeout 不能为负数，收到 {timeout}。", EXIT_USAGE)

    read_for = float(getattr(args, "read_for", 1))
    if read_for < 0:
        raise CliError("usage", f"--read-for 不能为负数，收到 {read_for}。", EXIT_USAGE)
    return repeat, interval_ms, timeout, read_for


def _apply_lines(transport, args) -> None:
    """可选设置 DTR/RTS 并发送 BREAK。"""
    dtr = getattr(args, "dtr", None)
    if dtr is not None:
        transport.set_dtr(dtr == "on")
    rts = getattr(args, "rts", None)
    if rts is not None:
        transport.set_rts(rts == "on")
    if getattr(args, "send_break", False):
        transport.send_break()


def _write_repeated(transport, payload: bytes, repeat: int, interval_ms: int) -> int:
    """写入 ``repeat`` 次，仅在两次写入之间按 ``interval_ms`` 休眠；返回总字节数。"""
    total = 0
    for index in range(repeat):
        if index and interval_ms:
            time.sleep(interval_ms / 1000.0)
        total += int(transport.write(payload))
    return total


def _read_response(transport, matcher, timeout: float, read_for: float):
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
# modem
# ---------------------------------------------------------------------------


def _register_modem(subparsers) -> None:
    parser = subparsers.add_parser(
        "modem",
        help="读取 CTS/DSR/RING/RLSD 线状态。",
        description="打开串口后立即读取调制解调器线状态并关闭。",
    )
    parser.add_argument("--port", required=True, metavar="PORT", help="串口名称，如 COM9。")
    add_connection_options(parser)
    add_output_options(parser)
    parser.set_defaults(handler=run_modem, command="serial.modem")


def run_modem(args) -> dict[str, Any]:
    """返回规范化为 ``cts``/``dsr``/``ring``/``rlsd`` 的四条线状态。"""
    config = build_config(args)
    transport = serial_io.open_transport()
    try:
        _open(transport, config)
        try:
            status = transport.modem_status()
        except CliError:
            raise
        except Exception as exc:
            raise CliError(
                "device_unavailable",
                f"读取串口 {config.port} 调制解调器状态失败：{exc}",
                EXIT_OPERATION,
            ) from exc
    finally:
        _close(transport)
    return _normalize_modem(status)


def _normalize_modem(status) -> dict[str, bool]:
    """把 transport 的 ``cts/dsr/ri/cd`` 映射成 ``cts/dsr/ring/rlsd``。"""
    return {
        "cts": bool(status.get("cts", False)),
        "dsr": bool(status.get("dsr", False)),
        "ring": bool(status.get("ri", False)),
        "rlsd": bool(status.get("cd", False)),
    }


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


def _open(transport, config) -> None:
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


def _close(transport) -> None:
    """关闭端口；关闭失败不覆盖已有结果或错误。"""
    try:
        transport.close()
    except Exception:
        pass

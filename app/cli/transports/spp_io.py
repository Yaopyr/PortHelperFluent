"""经典蓝牙 SPP 传输的惰性桥接：设备扫描、配对感知的 RFCOMM 收发与地址解析。

模块导入只依赖标准库与 :mod:`app.services.bluetooth` 的纯函数：``app.io.spp_worker``
会连带加载 WinRT 投影与 PyQt6，因此相关导入（含 ``ClassicDeviceInfo``/``PAIR_SUCCESS``）
全部放在函数内部，CLI 进程不会因为注册了 ``spp`` 命令就付出 GUI 依赖。所有协程都由
命令层用 ``asyncio.run`` 驱动，每次 ``send`` 都是「可选配对 → 连接 → 发送 → 读取 →
关闭」，``close`` 始终在 ``finally`` 里执行。

配对提示由 CLI 直接回答：``--pin`` 回应 ``pin`` 请求，``--confirm`` 自动接受确认请求；
两者都没给且设备未配对时，``--address`` 扫描已知配对状态，直接给出「请先在系统设置中
配对」的提示；``--device-id`` 的配对状态未知，会先调 ``pair``（已配对返回
``ALREADY_PAIRED``），失败后再给出同一条提示。
运行中设备断开通过 ``set_disconnect_callback`` 置 ``asyncio.Event``，读循环立即以
``device_unavailable``/3 结束，不会一直等到 ``--timeout``。

传输层异常统一收敛成 ``device_unavailable``/3（含配对/连接超时）；调用方已是
``CliError`` 时原样透传，由 ``app.cli.main`` 输出错误信封。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.cli.errors import EXIT_EXPECT, EXIT_OPERATION, CliError
from app.core.encoding import decode_bytes
from app.services.bluetooth import explain_error, pair_status_text

__all__ = [
    "DEFAULT_READ_FOR",
    "DEFAULT_TIMEOUT",
    "device_summary",
    "new_backend",
    "resolve_address",
    "scan",
    "send",
]

# 连接/配对默认超时，与 GUI 的 ``SPP_CONNECT_TIMEOUT`` 一致。
DEFAULT_TIMEOUT = 30.0
DEFAULT_READ_FOR = 1.0

# 读循环的轮询窗口：单次等待最多越过截止时间这么多。
READ_POLL = 0.05

PEER_DISCONNECT_MESSAGE = "设备主动断开连接"
NOT_PAIRED_MESSAGE = "设备未配对：请先在系统设置中配对，或加 --pin/--confirm。"
EXPECT_MISMATCH_MESSAGE = "等待 --expect/--expect-hex 超时未命中。"


def new_backend() -> Any:
    """构造默认 WinRT SPP 后端；``app.io.spp_worker`` 只在真正用到时才导入。"""
    from app.io.spp_worker import WinRtSppBackend

    return WinRtSppBackend()


def device_summary(device: Any) -> dict[str, Any]:
    """一个经典蓝牙设备的对外摘要：``name``/``address``/``paired``/``device_id``。"""
    return {
        "name": str(getattr(device, "name", "") or ""),
        "address": str(getattr(device, "address", "") or ""),
        "paired": bool(getattr(device, "paired", False)),
        "device_id": str(getattr(device, "device_id", "") or ""),
    }


async def scan(
    backend: Any,
    duration: float,
    *,
    filter_text: str | None = None,
    paired_only: bool = False,
) -> list[Any]:
    """扫描 ``duration`` 秒，按 ``device_id`` 去重（保留首次）后过滤。

    过滤大小写不敏感：``filter_text`` 命中名称或地址的子串即保留；``paired_only``
    只保留已配对设备。按 ``device_id`` 去重不会改变第一次出现的扫描顺序。
    """
    seen: dict[str, Any] = {}

    def on_device(info: Any) -> None:
        seen.setdefault(str(getattr(info, "device_id", "") or ""), info)

    try:
        await backend.scan(duration, on_device, lambda: False)
    except CliError:
        raise
    except Exception as exc:
        raise _unavailable("scan", exc) from exc

    needle = str(filter_text or "").strip().casefold()
    devices: list[Any] = []
    for info in seen.values():
        if paired_only and not getattr(info, "paired", False):
            continue
        if needle:
            name = str(getattr(info, "name", "") or "").casefold()
            address = str(getattr(info, "address", "") or "").casefold()
            if needle not in name and needle not in address:
                continue
        devices.append(info)
    return devices


async def resolve_address(backend: Any, address: str, duration: float) -> Any:
    """短扫描解析地址对应的设备；地址精确匹配（大小写不敏感）后立即停止扫描。"""
    needle = str(address or "").strip().casefold()
    found: list[Any] = []

    def on_device(info: Any) -> None:
        if found:
            return
        if str(getattr(info, "address", "") or "").strip().casefold() == needle:
            found.append(info)

    def stop() -> bool:
        return bool(found)

    try:
        await backend.scan(duration, on_device, stop)
    except CliError:
        raise
    except Exception as exc:
        raise _unavailable("scan", exc) from exc

    if not found:
        raise CliError(
            "not_found",
            f"扫描 {duration} 秒未找到经典蓝牙设备 {address}。",
            EXIT_OPERATION,
        )
    return found[0]


async def send(
    backend: Any,
    device: Any,
    data: bytes,
    *,
    pin: str | None = None,
    confirm: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    read_for: float = DEFAULT_READ_FOR,
    repeat: int = 1,
    interval_ms: int = 100,
    matcher=None,
    emit=None,
    pair_unknown: bool = False,
) -> dict[str, Any]:
    """按需配对后连接、发送并读取回包，返回 ``sent``/``received``/``matched``/``device``。

    - 已配对设备直接 RFCOMM；未配对时先让后端 ``pair``（PIN 提示由 ``pin`` 回答，
      确认提示由 ``confirm`` 接受）。
    - ``pair_unknown`` 用于 ``--device-id`` 这类「配对状态未知」的目标：未配对且没有
      ``pin``/``confirm`` 时仍调用 ``pair``（已配对设备会返回 ``ALREADY_PAIRED``），
      失败才给出 ``--pin/--confirm`` 提示；``pair_unknown=False`` 时不会开始配对，
      直接提示先配对。
    - 发送 ``repeat`` 次，只在两次之间按 ``interval_ms`` 停顿；随后给了 ``matcher``
      就等待命中（截止 ``timeout``），否则按 ``read_for`` 做固定窗口读取（0 不读）。
    - 运行中设备断开时断开回调经 ``loop.call_soon_threadsafe`` 置事件，读循环立即抛
      ``device_unavailable``/3，消息带断开原因；``close`` 在 ``finally`` 里执行。
    ``emit`` 保留给将来的流式输出，``spp send`` 不是流式命令，不产生 NDJSON 事件。
    """
    payload = bytes(data)
    hint_only = not getattr(device, "paired", False) and not pin and not confirm
    if hint_only and not pair_unknown:
        raise CliError("device_unavailable", NOT_PAIRED_MESSAGE, EXIT_OPERATION)

    # 只有真正需要配对时才导入，模块导入阶段不触碰 WinRT/PyQt6。
    from app.io.spp_worker import PAIR_SUCCESS

    received = bytearray()
    chunks = 0
    disconnected = asyncio.Event()
    disconnect_reason = {"text": ""}
    loop = asyncio.get_running_loop()

    def on_data(chunk: bytes) -> None:
        nonlocal chunks
        raw = bytes(chunk)
        if not raw:
            return
        received.extend(raw)
        chunks += 1

    def on_disconnect(reason: str) -> None:
        disconnect_reason["text"] = str(reason or PEER_DISCONNECT_MESSAGE)
        try:
            loop.call_soon_threadsafe(disconnected.set)
        except RuntimeError:
            # 事件循环已关闭（命令已结束）：残留回调不能再置事件。
            pass

    backend.set_prompt_callback(
        lambda kind, text: pin if kind == "pin" else ("yes" if confirm else None)
    )
    backend.set_data_callback(on_data)
    backend.set_disconnect_callback(on_disconnect)

    total = 0
    writes = 0
    matched = False
    try:
        if not getattr(device, "paired", False):
            status = await _pair(backend, device, timeout)
            if status not in PAIR_SUCCESS:
                if hint_only:
                    raise CliError(
                        "device_unavailable", NOT_PAIRED_MESSAGE, EXIT_OPERATION
                    )
                raise CliError(
                    "device_unavailable", pair_status_text(status), EXIT_OPERATION
                )
        await _connect(backend, device, timeout)
        for index in range(int(repeat)):
            if index and interval_ms:
                await asyncio.sleep(interval_ms / 1000.0)
            total += int(await _send_once(backend, payload))
            writes += 1
        matched = await _read_loop(
            disconnected, disconnect_reason, matcher, timeout, read_for, received
        )
        if matcher is not None and not matched:
            raise CliError("expect_mismatch", EXPECT_MISMATCH_MESSAGE, EXIT_EXPECT)
    finally:
        try:
            backend.set_disconnect_callback(lambda _reason: None)
        except Exception:
            pass
        await _close(backend)

    return {
        "sent": {"bytes": total, "writes": writes},
        "received": _received_data(bytes(received), chunks),
        "matched": matched,
        "device": device_summary(device),
    }


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


async def _pair(backend: Any, device: Any, timeout: float) -> str:
    """跑一次配对；超时与后端异常都按 ``device_unavailable``/3 处理。"""
    try:
        return str(await asyncio.wait_for(backend.pair(device), timeout))
    except TimeoutError as exc:
        raise CliError(
            "device_unavailable",
            explain_error("pair", f"配对超时（{timeout:g} 秒）"),
            EXIT_OPERATION,
        ) from exc
    except CliError:
        raise
    except Exception as exc:
        raise _unavailable("pair", exc) from exc


async def _connect(backend: Any, device: Any, timeout: float) -> None:
    """建立 RFCOMM 连接；超时与其他蓝牙错误都按 ``device_unavailable``/3 处理。"""
    label = str(getattr(device, "address", "") or getattr(device, "device_id", "") or "")
    try:
        await asyncio.wait_for(backend.connect(device), timeout)
    except TimeoutError as exc:
        raise CliError(
            "device_unavailable",
            explain_error("connect", f"连接失败：{timeout:g} 秒内未能连上 {label}。"),
            EXIT_OPERATION,
        ) from exc
    except CliError:
        raise
    except Exception as exc:
        raise _unavailable("connect", exc) from exc


async def _send_once(backend: Any, payload: bytes) -> int:
    """发送一次；后端异常按 ``device_unavailable``/3 处理。"""
    try:
        return int(await backend.send(payload))
    except CliError:
        raise
    except Exception as exc:
        raise _unavailable("send", exc) from exc


async def _read_loop(
    disconnected: asyncio.Event,
    reason: dict[str, str],
    matcher,
    timeout: float,
    read_for: float,
    received: bytearray,
) -> bool:
    """在截止时间前轮询已收数据；断开事件优先于截止时间，立即抛出。"""
    deadline = time.monotonic() + float(timeout if matcher is not None else read_for)
    while True:
        if disconnected.is_set():
            text = reason["text"] or PEER_DISCONNECT_MESSAGE
            raise CliError(
                "device_unavailable", f"发送期间设备断开：{text}", EXIT_OPERATION
            )
        if matcher is not None and matcher(bytes(received)):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(READ_POLL, remaining))


async def _close(backend: Any) -> None:
    """关闭 RFCOMM 连接；关闭失败不覆盖已有结果或错误。"""
    try:
        await backend.close()
    except Exception:
        pass


def _received_data(raw: bytes, chunks: int) -> dict[str, Any]:
    """把原始回包整理成 ``hex``/``text``/``size``/``chunks``。"""
    return {
        "hex": raw.hex(),
        "text": decode_bytes(raw, "utf-8"),
        "size": len(raw),
        "chunks": chunks,
    }


def _unavailable(stage: str, exc: BaseException) -> CliError:
    """把传输层异常收敛成带阶段排查建议的 ``device_unavailable``/3。"""
    labels = {
        "scan": "扫描",
        "pair": "配对",
        "connect": "连接",
        "send": "发送",
    }
    label = labels.get(stage, "经典蓝牙")
    return CliError(
        "device_unavailable",
        explain_error(stage, f"{label}失败：{exc}"),
        EXIT_OPERATION,
    )

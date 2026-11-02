"""蓝牙低功耗（BLE）传输的惰性桥接：扫描、服务树、读写与通知。

模块导入只依赖标准库与 :mod:`app.services.bluetooth` 的纯函数：``app.io.ble_worker``
会连带加载 ``bleak`` 与 PyQt6，因此相关导入全部放在函数内部，CLI 进程不会因为
注册了 ``ble`` 命令就付出 GUI 依赖。所有协程都由命令层用 ``asyncio.run`` 驱动，
每条命令各自「连接 → 操作 → 断开」，断开始终在 ``finally`` 里。

传输层异常统一收敛成 ``device_unavailable``/3（含连接超时）；调用方已是 ``CliError``
时原样透传，由 ``app.cli.main`` 输出错误信封。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from app.cli.errors import EXIT_OPERATION, CliError
from app.core.encoding import decode_bytes
from app.services.bluetooth import characteristic_label, chunk_bytes, service_label

__all__ = [
    "DEFAULT_TIMEOUT",
    "device_summary",
    "new_backend",
    "notify",
    "read",
    "scan",
    "services",
    "write",
]

# 蓝牙连接默认超时，与 GUI 的 ``CONNECT_TIMEOUT`` 一致。
DEFAULT_TIMEOUT = 30.0


def new_backend() -> Any:
    """构造默认 bleak 后端；``app.io.ble_worker`` 只在真正用到时才导入。"""
    from app.io.ble_worker import BleakBackend

    return BleakBackend()


def device_summary(info: Any) -> dict[str, Any]:
    """一次广播的对外摘要：``name``/``address``/``rssi``。"""
    return {"name": info.name, "address": info.address, "rssi": info.rssi}


async def scan(
    backend: Any, duration: float, filter_text: str | None = None
) -> list[Any]:
    """扫描 ``duration`` 秒，按名称/地址子串过滤，同一地址只保留最后一次信息。

    过滤大小写不敏感；``dict`` 覆盖已有 key 不会改变插入顺序，因此去重后仍保持
    第一次出现的扫描顺序。
    """
    seen: dict[str, Any] = {}

    def on_device(info: Any) -> None:
        seen[info.address] = info

    try:
        await backend.scan(duration, on_device, lambda: False)
    except CliError:
        raise
    except Exception as exc:
        raise _unavailable("扫描", exc) from exc

    needle = (filter_text or "").strip().casefold()
    if not needle:
        return list(seen.values())
    return [
        info
        for info in seen.values()
        if needle in (info.name or "").casefold()
        or needle in (info.address or "").casefold()
    ]


async def services(
    backend: Any, address: str, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """连接后读取 GATT 服务树与 MTU，返回 ``mtu``/``services``。"""
    entries: list[Any] = []
    mtu = 0
    try:
        await _connect(backend, address, timeout)
        entries = await backend.services()
        mtu = int(backend.mtu())
    except CliError:
        raise
    except Exception as exc:
        raise _unavailable("读取服务", exc) from exc
    finally:
        await _disconnect(backend)
    return {"mtu": mtu, "services": [_service_entry(entry) for entry in entries]}


async def read(
    backend: Any, address: str, char_uuid: str, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """连接后读取一个特征值，返回 ``char``/``hex``/``text``/``size``。"""
    raw = b""
    try:
        await _connect(backend, address, timeout)
        raw = bytes(await backend.read(char_uuid))
    except CliError:
        raise
    except Exception as exc:
        raise _unavailable("读取", exc) from exc
    finally:
        await _disconnect(backend)
    return {
        "char": char_uuid,
        "hex": raw.hex(),
        "text": decode_bytes(raw, "utf-8"),
        "size": len(raw),
    }


async def write(
    backend: Any,
    address: str,
    char_uuid: str,
    data: bytes,
    *,
    response: bool = True,
    mtu_chunk: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    repeat: int = 1,
    interval_ms: int = 100,
) -> dict[str, Any]:
    """连接后写入数据，返回 ``chunks``/``bytes``/``mtu``/``response``。

    ``mtu_chunk`` 为真时按 ``chunk_bytes(data, mtu)`` 分片（单片上限 MTU-3），
    否则整块写入；``mtu`` 只在分片时读取，不分片时为 ``None``。``repeat`` 次写入
    之间按 ``interval_ms`` 停顿，``bytes`` 是 ``len(data) * repeat``。
    """
    payload = bytes(data)
    repeats = int(repeat)
    chunks = 0
    mtu: int | None = None
    try:
        await _connect(backend, address, timeout)
        if mtu_chunk:
            mtu = int(backend.mtu())
            pieces = chunk_bytes(payload, mtu)
        else:
            pieces = [payload] if payload else []
        for index in range(repeats):
            if index and interval_ms:
                await asyncio.sleep(interval_ms / 1000.0)
            for piece in pieces:
                await backend.write(char_uuid, piece, response)
                chunks += 1
    except CliError:
        raise
    except Exception as exc:
        raise _unavailable("写入", exc) from exc
    finally:
        await _disconnect(backend)
    return {
        "chunks": chunks,
        "bytes": len(payload) * repeats,
        "mtu": mtu,
        "response": bool(response),
    }


async def notify(
    backend: Any,
    address: str,
    char_uuid: str,
    *,
    duration: float,
    emit,
    timeout: float = DEFAULT_TIMEOUT,
    matcher=None,
    max_bytes: int | None = None,
    max_events: int | None = None,
) -> dict[str, Any]:
    """订阅通知并逐条 ``emit``，返回 ``duration_s``/``bytes``/``events``/``matched``。

    通知先进入 ``asyncio.Queue``，循环只在这里等待：有数据立即取走，没有数据时
    用 ``asyncio.wait_for(queue.get(), 剩余时间)`` 等到截止时间，不会忙等。结束条件
    是 ``duration`` 到点、累计 ``max_bytes``、累计 ``max_events`` 或 ``matcher`` 命中；
    ``max_bytes`` 会截断最后一条通知的数据。``finally`` 取消订阅并断开连接。
    """
    queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()

    def on_data(char: str, data: bytes) -> None:
        queue.put_nowait((str(char), bytes(data)))

    subscribed = False
    total = 0
    events = 0
    matched = False
    buffer = bytearray()
    started = time.monotonic()
    try:
        await _connect(backend, address, timeout)
        started = time.monotonic()
        deadline = started + float(duration)
        await backend.set_notify(char_uuid, True, on_data)
        subscribed = True
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                char, data = await asyncio.wait_for(queue.get(), remaining)
            except TimeoutError:
                break
            if max_bytes is not None:
                budget = max_bytes - total
                if budget <= 0:
                    break
                if len(data) > budget:
                    data = data[:budget]
            total += len(data)
            events += 1
            buffer.extend(data)
            emit(
                {
                    "event": "notification",
                    "char": char,
                    "hex": data.hex(),
                    "text": decode_bytes(data, "utf-8"),
                    "size": len(data),
                    "at": _timestamp(),
                }
            )
            if matcher is not None and matcher(bytes(buffer)):
                matched = True
                break
            if max_bytes is not None and total >= max_bytes:
                break
            if max_events is not None and events >= max_events:
                break
    except CliError:
        raise
    except Exception as exc:
        raise _unavailable("订阅", exc) from exc
    finally:
        if subscribed:
            try:
                await backend.set_notify(char_uuid, False, on_data)
            except Exception:
                pass
        await _disconnect(backend)

    summary = {
        "duration_s": round(max(time.monotonic() - started, 0.0), 3),
        "bytes": total,
        "events": events,
        "matched": matched,
    }
    emit({"event": "end", **summary, "at": _timestamp()})
    return summary


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


async def _connect(backend: Any, address: str, timeout: float) -> None:
    """连接设备；超时与其他蓝牙错误都按 ``device_unavailable``/3 处理。"""
    try:
        await asyncio.wait_for(backend.connect(address), timeout)
    except TimeoutError as exc:
        raise CliError(
            "device_unavailable",
            f"连接失败：{timeout:g} 秒内未能连上 {address}。",
            EXIT_OPERATION,
        ) from exc
    except CliError:
        raise
    except Exception as exc:
        raise _unavailable("连接", exc) from exc


async def _disconnect(backend: Any) -> None:
    """断开连接；断开失败不覆盖已有结果或错误。"""
    try:
        await backend.disconnect()
    except Exception:
        pass


def _service_entry(service: Any) -> dict[str, Any]:
    """一个服务的对外结构：UUID、中文标签与特征值列表。"""
    return {
        "uuid": str(service.uuid),
        "label": service_label(str(service.uuid)),
        "characteristics": [
            {
                "uuid": str(char.uuid),
                "label": characteristic_label(str(char.uuid)),
                "properties": list(char.properties),
                "subscribed": False,
            }
            for char in service.characteristics
        ],
    }


def _unavailable(stage: str, exc: BaseException) -> CliError:
    """把传输层异常收敛成建议文案；``CliError`` 由调用方透传。"""
    return CliError("device_unavailable", f"{stage}失败：{exc}", EXIT_OPERATION)


def _timestamp() -> str:
    """本地时区的 ISO 8601 时间戳，供流式事件的 ``at`` 字段使用。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")

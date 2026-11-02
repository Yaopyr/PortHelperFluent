"""USB HID 传输的惰性桥接：枚举、设备解析、报告构造与打开。

模块导入只依赖标准库：``app.io.hid_worker`` 会连带加载 ``hid`` 与 PyQt6，因此
所有相关导入都放在函数内部，CLI 进程不因 HID 命令缺席而多付 GUI 依赖。测试通过
monkeypatch 本模块的 :func:`new_backend` 与 :func:`_clock` 注入内存后端与假时钟。
"""

from __future__ import annotations

import base64
import time
from datetime import datetime
from typing import Any

from app.cli.errors import EXIT_OPERATION, CliError

__all__ = [
    "build_report",
    "decode_path",
    "device_summary",
    "encode_path",
    "enumerate_devices",
    "new_backend",
    "open_device",
    "resolve_device",
    "resolve_device_entry",
]

# 流式监控的轮询窗口：单次 read 最多越过 --duration 100 ms。
READ_TIMEOUT_MS = 100

# ``ambiguous_device`` 的候选摘要字段；Agent 据此改用 --index/--path-b64 消歧。
_DETAIL_FIELDS = (
    "index",
    "path_b64",
    "serial",
    "interface",
    "product",
    "vid",
    "pid",
)


def _clock() -> float:
    """可替换的单调时钟调用点；测试替换它来压缩 ``--duration``。"""
    return time.monotonic()


def _timestamp() -> str:
    """本地时区的 ISO 8601 时间戳，供流式事件的 ``at`` 字段使用。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_backend() -> Any:
    """构造默认 hidapi 后端；``hid`` 只在真正枚举时才导入。"""
    from app.io.hid_worker import HidapiBackend

    return HidapiBackend()


def build_report(data: bytes, report_id: int = 0, report_length: int = 64) -> bytes:
    """生成 Output Report（按需前置报告 ID 并填充/截断到报告长度）。"""
    from app.io.hid_worker import build_report as worker_build_report

    return worker_build_report(data, report_id, report_length)


def encode_path(path: bytes) -> str:
    """把设备路径编码成标准 base64，供 ``hid list`` 的 ``path_b64`` 使用。"""
    return base64.b64encode(bytes(path)).decode("ascii")


def decode_path(text: str) -> bytes:
    """解码标准 base64 设备路径；非法输入抛出中文 ``ValueError``。"""
    try:
        return base64.b64decode(str(text).strip(), validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"--path-b64 不是合法的 base64：{exc}") from exc


def enumerate_devices(backend: Any) -> list[Any]:
    """枚举设备；底层异常按「没有设备」处理（不把实现细节泄漏给 CLI）。"""
    try:
        return list(backend.enumerate())
    except Exception:
        return []


def device_summary(info: Any, index: int) -> dict[str, Any]:
    """一台设备的对外摘要；``index`` 是本次枚举顺序（0 基），``vid/pid`` 为 4 位大写 hex。"""
    return {
        "index": index,
        "path_b64": encode_path(info.path),
        "vid": f"{info.vendor_id:04X}",
        "pid": f"{info.product_id:04X}",
        "product": info.product,
        "manufacturer": info.manufacturer,
        "serial": info.serial,
        "usage_page": info.usage_page,
        "usage": info.usage,
        "interface": info.interface,
    }


def resolve_device(
    *,
    index: int | None = None,
    path_b64: str | None = None,
    vid: int | None = None,
    pid: int | None = None,
    serial: str | None = None,
    interface: int | None = None,
    backend: Any = None,
) -> Any:
    """按 ``--index``/``--path-b64``/``--vid+--pid`` 选出唯一设备。"""
    return resolve_device_entry(
        index=index,
        path_b64=path_b64,
        vid=vid,
        pid=pid,
        serial=serial,
        interface=interface,
        backend=backend,
    )[0]


def resolve_device_entry(
    *,
    index: int | None = None,
    path_b64: str | None = None,
    vid: int | None = None,
    pid: int | None = None,
    serial: str | None = None,
    interface: int | None = None,
    backend: Any = None,
) -> tuple[Any, int]:
    """选出唯一设备并返回 ``(设备, 本次枚举中的索引)``。

    0 个命中按 ``not_found``（退出码 3）；多个命中按 ``ambiguous_device``（退出码 3），
    ``details`` 是候选摘要列表，不会静默取第一台。``--path-b64`` 解码失败抛出
    ``ValueError``，由命令层转成 ``usage``/2。
    """
    if path_b64 is not None:
        path = decode_path(path_b64)
    backend = backend if backend is not None else new_backend()
    devices = enumerate_devices(backend)

    if index is not None:
        if index < 0 or index >= len(devices):
            raise CliError(
                "not_found",
                f"没有索引为 {index} 的 HID 设备（本次枚举共 {len(devices)} 个）。",
                EXIT_OPERATION,
            )
        return devices[index], index

    if path_b64 is not None:
        selected = [
            (position, info)
            for position, info in enumerate(devices)
            if bytes(info.path) == path
        ]
        description = f"--path-b64 {path_b64}"
    else:
        selected = [
            (position, info)
            for position, info in enumerate(devices)
            if info.vendor_id == vid and info.product_id == pid
        ]
        if serial is not None:
            selected = [item for item in selected if item[1].serial == serial]
        if interface is not None:
            selected = [item for item in selected if item[1].interface == interface]
        description = (
            f"vid/pid {vid:04X}:{pid:04X}"
            if vid is not None and pid is not None
            else "vid/pid"
        )

    if not selected:
        raise CliError(
            "not_found", f"未找到匹配的 HID 设备（{description}）。", EXIT_OPERATION
        )
    if len(selected) > 1:
        details = [
            {field: device_summary(info, position)[field] for field in _DETAIL_FIELDS}
            for position, info in selected
        ]
        raise CliError(
            "ambiguous_device",
            f"匹配到 {len(selected)} 台 HID 设备（{description}），"
            "无法确定唯一设备，请用 --index 或 --path-b64 指定。",
            EXIT_OPERATION,
            details,
        )
    return selected[0][1], selected[0][0]


def open_device(info: Any, *, backend: Any = None) -> tuple[Any, object]:
    """打开设备并返回 ``(backend, handle)``；打开失败原样上抛，由命令层映射错误。"""
    backend = backend if backend is not None else new_backend()
    handle = backend.open(bytes(info.path))
    return backend, handle

"""蓝牙调试纯函数：UUID 标签、MTU 分片、设备排序过滤与中文诊断提示。

本模块无 Qt、无蓝牙 I/O，也不读写注册表或串口；输入输出都是普通字符串与字典，
便于 BLE/SPP worker 与界面层共用，并能被单测完整覆盖。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

GATT_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"

KNOWN_SERVICES: dict[str, str] = {
    "00001800-0000-1000-8000-00805f9b34fb": "通用访问",
    "00001801-0000-1000-8000-00805f9b34fb": "通用属性",
    "0000180a-0000-1000-8000-00805f9b34fb": "设备信息",
    "0000180d-0000-1000-8000-00805f9b34fb": "心率服务",
    "0000180f-0000-1000-8000-00805f9b34fb": "电池服务",
    "00001812-0000-1000-8000-00805f9b34fb": "HID",
    "0000ffe0-0000-1000-8000-00805f9b34fb": "自定义透传",
    "0000fff0-0000-1000-8000-00805f9b34fb": "自定义透传",
}

KNOWN_CHARACTERISTICS: dict[str, str] = {
    "00002a00-0000-1000-8000-00805f9b34fb": "设备名称",
    "00002a19-0000-1000-8000-00805f9b34fb": "电量",
    "00002a29-0000-1000-8000-00805f9b34fb": "制造商",
    "0000ffe1-0000-1000-8000-00805f9b34fb": "FFE0 数据",
    "0000fff1-0000-1000-8000-00805f9b34fb": "FFF0 数据",
    "0000fff2-0000-1000-8000-00805f9b34fb": "FFF0 数据",
}

_PROPERTY_BADGES: dict[str, str] = {
    "read": "Read",
    "write": "Write",
    "write-without-response": "WriteNoRsp",
    "notify": "Notify",
    "indicate": "Indicate",
}
_BADGE_ORDER: tuple[str, ...] = ("Read", "Write", "WriteNoRsp", "Notify", "Indicate")

_MAC_PATTERN = re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}")
_PIN_PATTERN = re.compile(r"[0-9]{1,16}")

_BLUETOOTH_PORT_MARKERS: tuple[str, ...] = ("蓝牙", "bluetooth", "bthenum", "bthmodem", "rfcomm")

_ERROR_HINTS: dict[str, str] = {
    "scan": "确认设备在广播范围内，且系统蓝牙已打开",
    "connect": "确认设备未被其他程序占用，且已开机",
    "pair": "先让设备进入配对模式（指示灯快闪）",
    "write": "确认该特征值支持写入",
}

_PAIR_STATUS: dict[str, str] = {
    "paired": "配对成功",
    "unpaired": "未配对",
    "authorizing": "配对中…",
    "authorizingpasskey": "配对中，请输入配对码",
    "authorizingerror": "配对失败（系统拒绝，请重试或用系统设置配对）",
    "authenticationfailure": "配对失败（PIN 或配对码错误）",
    "authenticationtimeout": "配对失败（认证超时，请重试）",
    "authenticationrejected": "配对失败（设备拒绝配对）",
}


def expand_uuid(value: str) -> str:
    """把 16 位或 32 位短 UUID 补成小写完整 128 位形式；无法识别时原样返回。"""
    raw = value or ""
    text = raw.strip().lower()
    if len(text) == 4:
        return "0000" + text + GATT_BASE_SUFFIX
    if len(text) == 8:
        return text + GATT_BASE_SUFFIX
    if len(text) == 36:
        return text
    return raw


def short_uuid(value: str) -> str:
    """完整 128 位 UUID 压成 4 位短形式；厂商自定义 UUID 原样大写返回。"""
    full = expand_uuid(value)
    if len(full) == 36 and full.startswith("0000") and full.endswith(GATT_BASE_SUFFIX):
        return full[4:8].upper()
    return full.upper()


def service_label(uuid: str) -> str:
    """中文服务标签，形如「FFE0（自定义透传）」；未知服务标记为自定义。"""
    name = KNOWN_SERVICES.get(expand_uuid(uuid), "自定义")
    return f"{short_uuid(uuid)}（{name}）"


def characteristic_label(uuid: str) -> str:
    """中文特征值标签，形如「FFE1（FFE0 数据）」；未知特征值标记为自定义。"""
    name = KNOWN_CHARACTERISTICS.get(expand_uuid(uuid), "自定义")
    return f"{short_uuid(uuid)}（{name}）"


def properties_badges(properties: Sequence[str]) -> tuple[str, ...]:
    """把属性名映射成固定顺序的徽章文案，未知属性忽略。"""
    present = {_PROPERTY_BADGES.get(str(item).strip().lower()) for item in properties}
    return tuple(badge for badge in _BADGE_ORDER if badge in present)


def is_readable(properties: Sequence[str]) -> bool:
    """特征值是否可读。"""
    return _supports(properties, "read")


def is_writable(properties: Sequence[str]) -> bool:
    """特征值是否可写（含无响应写）。"""
    return _supports(properties, "write", "write-without-response")


def is_notifiable(properties: Sequence[str]) -> bool:
    """特征值是否可订阅（notify 或 indicate）。"""
    return _supports(properties, "notify", "indicate")


def chunk_bytes(data: bytes, mtu: int) -> list[bytes]:
    """按 MTU 切片，单片上限 mtu-3（至少 1 字节），供分包写入使用。"""
    size = max(1, int(mtu) - 3)
    return [data[index:index + size] for index in range(0, len(data), size)]


def sort_devices(devices: Sequence[dict], mode: str, favorites: Sequence[str] = ()) -> list[dict]:
    """收藏设备置顶，再按模式排序。

    "rssi" 信号强的在前且无信号垫底，"name" 未命名设备垫底，
    "recent" 保持入参顺序；未知模式按 "recent" 处理。
    """
    favorite_addresses = {address.casefold() for address in favorites}

    def rank(device: dict) -> tuple:
        pinned = 0 if str(device.get("address") or "").casefold() in favorite_addresses else 1
        if mode == "name":
            name = str(device.get("name") or "").strip()
            return (pinned, not name, name.casefold())
        if mode == "rssi":
            return (pinned, -(device.get("rssi") or -999))
        return (pinned,)

    return sorted(devices, key=rank)


def filter_devices(
    devices: Sequence[dict],
    query: str,
    only_named: bool = False,
    only_paired: bool = False,
) -> list[dict]:
    """按关键字（名称或地址）与命名/配对状态过滤，保持原顺序。"""
    needle = (query or "").strip().casefold()
    matched: list[dict] = []
    for device in devices:
        name = str(device.get("name") or "").strip()
        address = str(device.get("address") or "")
        if needle and needle not in name.casefold() and needle not in address.casefold():
            continue
        if only_named and not name:
            continue
        if only_paired and not device.get("paired"):
            continue
        matched.append(device)
    return matched


def is_bluetooth_port(device: str, description: str, hwid: str) -> bool:
    """判断串口是否为蓝牙 SPP 虚拟串口（依据端口名、描述与硬件 ID 关键字）。"""
    haystack = f"{device} {description} {hwid}".casefold()
    return any(marker in haystack for marker in _BLUETOOTH_PORT_MARKERS)


def format_rssi(rssi: int | None) -> str:
    """RSSI 展示文案，无信号时返回「未知」。"""
    return "未知" if rssi is None else f"{rssi} dBm"


def rssi_quality(rssi: int | None) -> str:
    """按 RSSI 粗略判断信号强弱。"""
    if rssi is None:
        return "未知"
    if rssi >= -60:
        return "强"
    if rssi >= -75:
        return "中"
    return "弱"


def validate_pin(text: str) -> str | None:
    """校验配对码；合法（1–16 位纯数字）返回 None，否则返回中文提示。"""
    if _PIN_PATTERN.fullmatch(text or ""):
        return None
    return "配对码需为 1–16 位数字"


def pair_status_text(status_name: str) -> str:
    """Qt 配对状态名转中文；未知状态统一按配对失败处理。"""
    key = "".join(char for char in (status_name or "").casefold() if char.isalnum())
    fallback = f"配对失败（{status_name}）" if status_name else "配对失败"
    return _PAIR_STATUS.get(key, fallback)


def explain_error(stage: str, message: str) -> str:
    """给底层错误消息补上该阶段的中文排查建议。"""
    hint = _ERROR_HINTS.get(stage, "")
    if not message:
        return hint
    return f"{message}（{hint}）" if hint else message


def address_from_device_id(device_id: str) -> str:
    """从 Windows 蓝牙设备 ID 中提取第一个 MAC 地址，找不到则原样返回。"""
    match = _MAC_PATTERN.search(device_id or "")
    return match.group(0) if match else (device_id or "")


def _supports(properties: Sequence[str], *names: str) -> bool:
    normalized = {str(item).strip().lower() for item in properties}
    return any(name in normalized for name in names)

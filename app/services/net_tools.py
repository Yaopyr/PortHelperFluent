"""网络辅助工具：系统 Ping、域名解析与本地网卡信息。

Ping 一律调用系统 ping.exe，不使用原始套接字；DNS 使用标准库 socket；
网卡信息来自 psutil。所有失败都转换成中文可读消息，不把 traceback 抛给界面。
"""

from __future__ import annotations

import ipaddress
import locale
import socket
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class PingResult:
    ok: bool
    output: str
    host: str


def ping_host(host: str, count: int = 4, timeout_s: int = 15) -> PingResult:
    """调用系统 ping 命令；返回原始输出与是否成功。"""
    target = (host or "").strip()
    if not target:
        return PingResult(False, "目标地址不能为空，请输入 IP 地址或域名。", target)
    try:
        times = max(1, int(count))
    except (TypeError, ValueError):
        times = 4
    command = ["ping", "-n", str(times), target]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=max(1, int(timeout_s)),
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        partial = _decode_output(exc.stdout) + _decode_output(exc.stderr)
        note = f"Ping 命令超过 {max(1, int(timeout_s))} 秒未完成，已终止。"
        output = (partial + "\n" + note).strip() if partial else note
        return PingResult(False, output, target)
    except OSError as exc:
        return PingResult(
            False,
            f"无法启动系统 ping 命令：{exc}。请确认系统自带 ping.exe 可用。",
            target,
        )
    output = (_decode_output(completed.stdout) + _decode_output(completed.stderr)).strip()
    if not output:
        output = f"系统 ping 没有返回任何输出（退出码 {completed.returncode}）。"
    return PingResult(completed.returncode == 0, output, target)


def resolve_host(host: str) -> list[str]:
    """解析域名或 IP，返回去重后的地址列表；失败时抛出中文 ValueError。"""
    target = (host or "").strip()
    if not target:
        raise ValueError("请输入要解析的域名或 IP 地址。")
    try:
        infos = socket.getaddrinfo(target, None)
    except socket.gaierror as exc:
        raise ValueError(
            f"无法解析“{target}”：{exc}。请检查域名拼写、网络连接或 DNS 设置。"
        ) from exc
    except OSError as exc:
        raise ValueError(
            f"解析“{target}”失败：{exc}。请检查网络连接后重试。"
        ) from exc
    addresses: list[str] = []
    for info in infos:
        if len(info) < 5 or not info[4]:
            continue
        address = str(info[4][0]).strip()
        if address and address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError(f"域名“{target}”没有解析到任何 IP 地址。请检查 DNS 设置。")
    return addresses


def adapter_info() -> list[dict]:
    """返回本机网卡信息：名称、IPv4 地址、掩码、网关（可能为空）、MAC、是否启用。"""
    try:
        import psutil

        addresses_by_name = psutil.net_if_addrs()
        stats_by_name = psutil.net_if_stats()
    except Exception as exc:
        raise RuntimeError(
            f"读取网卡信息失败：{exc}。请确认 psutil 已正确安装，且系统网络服务可用。"
        ) from exc

    link_family = getattr(psutil, "AF_LINK", None)
    gateways = _default_gateways()
    result: list[dict] = []
    for name in sorted(addresses_by_name, key=str.lower):
        ipv4 = [
            item for item in addresses_by_name[name] if item.family == socket.AF_INET
        ]
        mac = ""
        if link_family is not None:
            for item in addresses_by_name[name]:
                if item.family == link_family and item.address:
                    mac = item.address
                    break
        addresses = [item.address for item in ipv4 if item.address]
        stat = stats_by_name.get(name)
        result.append(
            {
                "name": str(name),
                "addresses": addresses,
                "netmask": ", ".join(item.netmask for item in ipv4 if item.netmask),
                "gateway": _gateway_for(name, addresses, gateways),
                "mac": mac,
                "isup": bool(stat.isup) if stat is not None else False,
            }
        )
    return result


def _decode_output(raw) -> str:
    """按系统区域编码解码 ping 输出；无法解码的字节用替代符保留。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (bytes, bytearray)):
        return str(raw)
    encoding = locale.getpreferredencoding(False) or "utf-8"
    return bytes(raw).decode(encoding, errors="replace")


# ---- Windows 默认网关（route print -4，按接口 IP 映射） -------------------

def _default_gateways() -> dict[str, str]:
    """返回 {接口 IPv4: 默认网关}；解析失败时返回空字典。"""
    if sys.platform != "win32":
        return {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["route", "print", "-4"],
            capture_output=True,
            timeout=5,
            creationflags=creationflags,
        )
        text = _decode_output(completed.stdout)
    except (OSError, subprocess.SubprocessError):
        return {}
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        destination, mask, gateway, interface = parts[:4]
        if destination == "0.0.0.0" and mask == "0.0.0.0":
            if _is_ipv4(gateway) and _is_ipv4(interface):
                result[interface] = gateway
    return result


def _gateway_for(name: str, addresses: list[str], gateways: dict[str, str]) -> str:
    """按网卡地址匹配默认路由的接口地址。"""
    matched = sorted({gateways[address] for address in addresses if address in gateways})
    return ", ".join(matched)


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
        return True
    except ValueError:
        return False

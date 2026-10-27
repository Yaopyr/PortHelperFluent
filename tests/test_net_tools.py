"""Network helper tests: DNS resolution, system ping and adapter enumeration."""

from __future__ import annotations

import socket

import pytest

from app.services.net_tools import adapter_info, ping_host, resolve_host


def test_resolve_localhost_returns_loopback_addresses() -> None:
    addresses = resolve_host("localhost")
    assert "127.0.0.1" in addresses
    assert len(addresses) == len(set(addresses))


def test_resolve_host_failure_raises_chinese_error(monkeypatch) -> None:
    def broken_getaddrinfo(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "unknown host")

    monkeypatch.setattr(socket, "getaddrinfo", broken_getaddrinfo)
    with pytest.raises(ValueError) as excinfo:
        resolve_host("no-such-host.invalid")
    assert "无法解析" in str(excinfo.value)


def test_ping_loopback_returns_raw_output() -> None:
    result = ping_host("127.0.0.1", count=1, timeout_s=10)
    assert result.host == "127.0.0.1"
    assert isinstance(result.ok, bool)
    assert result.output.strip()


def test_ping_empty_host_is_rejected() -> None:
    result = ping_host("   ")
    assert result.ok is False
    assert "目标地址" in result.output


def test_adapter_info_returns_list_with_expected_keys() -> None:
    adapters = adapter_info()
    assert isinstance(adapters, list)
    for item in adapters:
        assert set(("name", "addresses", "netmask", "gateway", "mac", "isup")).issubset(
            item
        )
        assert isinstance(item["addresses"], list)
        assert isinstance(item["isup"], bool)

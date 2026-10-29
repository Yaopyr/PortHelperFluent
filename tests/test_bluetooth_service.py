"""Bluetooth pure-function service tests: UUID labels, MTU chunking, sorting and diagnostics."""

from __future__ import annotations

from app.services.bluetooth import (
    address_from_device_id,
    chunk_bytes,
    expand_uuid,
    explain_error,
    filter_devices,
    is_bluetooth_port,
    is_notifiable,
    is_readable,
    is_writable,
    pair_status_text,
    properties_badges,
    service_label,
    short_uuid,
    sort_devices,
    validate_pin,
)


def test_expand_uuid_accepts_short_and_full_forms():
    assert expand_uuid("FFE0") == "0000ffe0-0000-1000-8000-00805f9b34fb"
    assert expand_uuid("0000FFE0-0000-1000-8000-00805F9B34FB") == "0000ffe0-0000-1000-8000-00805f9b34fb"
    assert expand_uuid("bad") == "bad"


def test_short_uuid_keeps_short_form_and_passes_through_vendor_128bit():
    assert short_uuid("0000ffe1-0000-1000-8000-00805f9b34fb") == "FFE1"
    assert short_uuid("6e400001-b5a3-f393-e0a9-e50e24dcca9e") == "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"


def test_service_label_marks_known_and_custom_services():
    assert service_label("0000ffe0-0000-1000-8000-00805f9b34fb") == "FFE0（自定义透传）"
    assert service_label("0000180f-0000-1000-8000-00805f9b34fb") == "180F（电池服务）"
    assert service_label("1234abcd-b5a3-f393-e0a9-e50e24dcca9e").endswith("（自定义）")


def test_properties_badges_maps_known_bits_in_fixed_order():
    assert properties_badges(["notify", "read", "write-without-response", "write", "indicate", "bogus"]) == (
        "Read", "Write", "WriteNoRsp", "Notify", "Indicate")


def test_capability_predicates():
    assert is_readable(["read"]) and not is_readable(["notify"])
    assert is_writable(["write-without-response"]) and not is_writable(["read"])
    assert is_notifiable(["indicate"]) and not is_notifiable(["write"])


def test_chunk_bytes_respects_mtu_minus_three():
    assert chunk_bytes(b"abcdefgh", 23) == [b"abcdefgh"[:20]]
    assert [len(c) for c in chunk_bytes(b"x" * 45, 23)] == [20, 20, 5]
    assert chunk_bytes(b"abc", 3) == [b"a", b"b", b"c"]
    assert chunk_bytes(b"", 23) == []


def test_sort_devices_puts_favorites_first_then_mode_key():
    devices = [{"address": "B", "name": "beta", "rssi": -80},
               {"address": "A", "name": "Alpha", "rssi": -40},
               {"address": "C", "name": "", "rssi": None}]
    assert [d["address"] for d in sort_devices(devices, "rssi")] == ["A", "B", "C"]
    assert [d["address"] for d in sort_devices(devices, "name")] == ["A", "B", "C"]
    assert [d["address"] for d in sort_devices(devices, "recent")] == ["B", "A", "C"]
    assert sort_devices(devices, "rssi", favorites=["B"])[0]["address"] == "B"
    assert sort_devices(devices, "bogus")[0]["address"] == "B"


def test_filter_devices_by_text_name_and_pairing_state():
    devices = [{"address": "AA:BB", "name": "HC-05", "paired": True},
               {"address": "CC:DD", "name": "", "paired": False}]
    assert [d["address"] for d in filter_devices(devices, "hc")] == ["AA:BB"]
    assert [d["address"] for d in filter_devices(devices, "aa:bb")] == ["AA:BB"]
    assert [d["address"] for d in filter_devices(devices, "", only_named=True)] == ["AA:BB"]
    assert [d["address"] for d in filter_devices(devices, "", only_paired=True)] == ["AA:BB"]
    assert len(filter_devices(devices, "")) == 2


def test_is_bluetooth_port_matches_driver_and_description():
    assert is_bluetooth_port("COM7", "蓝牙链接上的标准串行", "BTHENUM\\{00001101}")
    assert is_bluetooth_port("COM8", "Standard Serial over Bluetooth link", "")
    assert not is_bluetooth_port("COM3", "USB-SERIAL CH340", "USB VID:PID=1A86:7523")


def test_validate_pin_accepts_digits_and_rejects_bad_input():
    assert validate_pin("1234") is None
    assert validate_pin("") is not None
    assert validate_pin("abcd") is not None
    assert validate_pin("1" * 17) is not None


def test_pair_status_text_and_explain_error_are_chinese():
    assert pair_status_text("PAIRED") == "配对成功"
    assert "PIN" in pair_status_text("AUTHENTICATION_FAILURE")
    assert pair_status_text("SOMETHING_NEW").startswith("配对失败")
    assert "广播范围" in explain_error("scan", "扫描超时")
    assert explain_error("connect", "连接失败") == "连接失败（确认设备未被其他程序占用，且已开机）"


def test_address_from_device_id_extracts_mac():
    assert address_from_device_id("Bluetooth#Bluetooth00:1A:7D:DA:71:13-4c:5e:0c:11:22:33") == "00:1A:7D:DA:71:13"
    assert address_from_device_id("no-mac-here") == "no-mac-here"

"""校验引擎单元测试：公开检查值、独立参考实现、流式一致性与源码片段。"""

from __future__ import annotations

import binascii
import random

import pytest

from app.core.checksum import (
    CRC_PRESETS,
    RESULT_NAMES,
    ChecksumResult,
    CrcAccumulator,
    CrcParams,
    IntelHexAccumulator,
    ParityAccumulator,
    SumAccumulator,
    TcpIpAccumulator,
    XorAccumulator,
    compute_all,
    create_accumulators,
    format_value,
    source_text,
)

CHECK_VALUES = {
    "CRC-8/MAXIM": 0xA1,
    "CRC-8/SAE-J1850": 0x4B,
    "CRC-8/BLUETOOTH": 0x26,
    "CRC-16/MODBUS": 0x4B37,
    "CRC-16/CCITT-FALSE": 0x29B1,
    "CRC-16/XMODEM": 0x31C3,
    "CRC-16/KERMIT": 0x2189,
    "CRC-32/ISO-HDLC": 0xCBF43926,
}


def _result_map(data: bytes, invert: bool = False) -> dict[str, ChecksumResult]:
    return {result.name: result for result in compute_all(data, invert=invert)}


def _reflect(value: int, width: int) -> int:
    result = 0
    for _ in range(width):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


def _reference_crc(data: bytes, params: CrcParams) -> int:
    """按 Rocksoft 模型逐位实现的独立参考，不复用生产代码的查表法。"""
    mask = (1 << params.width) - 1
    top_bit = 1 << (params.width - 1)
    crc = params.init
    for byte in data:
        if params.refin:
            byte = _reflect(byte, 8)
        crc ^= (byte << (params.width - 8)) & mask
        for _ in range(8):
            if crc & top_bit:
                crc = ((crc << 1) ^ params.poly) & mask
            else:
                crc = (crc << 1) & mask
    if params.refout:
        crc = _reflect(crc, params.width)
    return (crc ^ params.xorout) & mask


def test_published_crc_check_values() -> None:
    results = _result_map(b"123456789")
    for name, expected in CHECK_VALUES.items():
        assert results[name].value == expected, name
        assert results[name].width == CRC_PRESETS[name].width
        assert isinstance(results[name], ChecksumResult)


@pytest.mark.parametrize("name", tuple(CRC_PRESETS))
def test_crc_matches_independent_bitwise_reference(name: str) -> None:
    params = CRC_PRESETS[name]
    samples = (
        b"",
        b"\x00",
        b"\xFF",
        b"123456789",
        bytes(range(256)),
        random.Random(0xC0DE).randbytes(1000),
    )
    for data in samples:
        accumulator = CrcAccumulator(params)
        accumulator.update(data)
        assert accumulator.result() == _reference_crc(data, params), name


def test_crc32_matches_binascii() -> None:
    for size in (0, 1, 5, 1024):
        data = random.Random(size).randbytes(size)
        accumulator = CrcAccumulator(CRC_PRESETS["CRC-32/ISO-HDLC"])
        accumulator.update(data)
        assert accumulator.result() == (binascii.crc32(data) & 0xFFFFFFFF)


@pytest.mark.parametrize(
    "params",
    [
        CrcParams(8, 0x07, 0x12, refin, refout, 0x34)
        for refin in (False, True)
        for refout in (False, True)
    ]
    + [
        CrcParams(16, 0x1021, 0x1234, refin, refout, 0x5678)
        for refin in (False, True)
        for refout in (False, True)
    ]
    + [
        CrcParams(32, 0x04C11DB7, 0x89ABCDEF, refin, refout, 0x10203040)
        for refin in (False, True)
        for refout in (False, True)
    ],
)
def test_custom_crc_init_and_reflection(params: CrcParams) -> None:
    data = random.Random(7).randbytes(333)
    accumulator = CrcAccumulator(params, "自定义 CRC")
    accumulator.update(data)
    assert accumulator.name == "自定义 CRC"
    assert accumulator.result() == _reference_crc(data, params)


def test_crc_streaming_matches_one_shot() -> None:
    data = random.Random(20261026).randbytes(64 * 1024)
    expected = {result.name: result.value for result in compute_all(data)}

    for chunk_size in (1, 7, 4096):
        accumulators = create_accumulators()
        for offset in range(0, len(data), chunk_size):
            chunk = data[offset : offset + chunk_size]
            for accumulator in accumulators:
                accumulator.update(bytearray(chunk))
        actual = {accumulator.name: accumulator.result() for accumulator in accumulators}
        assert actual == expected, f"chunk_size={chunk_size}"


def test_accumulator_result_is_repeatable_and_can_continue() -> None:
    accumulator = CrcAccumulator(CRC_PRESETS["CRC-16/MODBUS"])
    accumulator.update(b"1234")
    first = accumulator.result()
    assert accumulator.result() == first
    accumulator.update(b"56789")
    assert accumulator.result() == CHECK_VALUES["CRC-16/MODBUS"]

    accumulator.reset()
    accumulator.update(b"123456789")
    assert accumulator.result() == CHECK_VALUES["CRC-16/MODBUS"]


def test_sum_vectors_and_widths() -> None:
    results = _result_map(b"\x01\x02\x03")
    assert results["Sum-8"] == ChecksumResult("Sum-8", 6, 8)
    assert results["Sum-16"].value == 6
    assert results["Sum-32"].value == 6

    results = _result_map(b"\xFF\x02")
    assert results["Sum-8"].value == 0x01
    assert results["Sum-16"].value == 0x0101
    assert results["Sum-32"].value == 0x0101

    results = _result_map(b"\xFF" * 4)
    assert results["Sum-8"].value == 0xFC
    assert results["Sum-16"].value == 0x03FC
    assert results["Sum-32"].value == 0x03FC


def test_xor_vectors() -> None:
    assert _result_map(b"\x01\x02\x04")["XOR-8 (BCC)"].value == 0x07
    assert _result_map(b"\xFF\x0F")["XOR-8 (BCC)"].value == 0xF0
    assert _result_map(b"")["XOR-8 (BCC)"].value == 0x00
    assert XorAccumulator().width == 8


def test_parity_vectors() -> None:
    cases = (
        (b"\x00", 1, 0),
        (b"\x01", 0, 1),
        (b"\x03", 1, 0),
        (b"\x01\x01", 1, 0),
    )
    for data, odd, even in cases:
        results = _result_map(data)
        assert results["Parity-Odd"].value == odd, data
        assert results["Parity-Even"].value == even, data
        assert results["Parity-Odd"].width == 1
        assert results["Parity-Even"].width == 1
    assert ParityAccumulator("odd").name == "Parity-Odd"
    assert ParityAccumulator("even").name == "Parity-Even"


def test_intel_hex_vectors() -> None:
    assert _result_map(b"")["Intel HEX"].value == 0x00
    assert _result_map(b"\x01\x02")["Intel HEX"].value == 0xFD
    assert _result_map(b"\xFF")["Intel HEX"].value == 0x01
    # :02 0000 04 0800 F2 —— 数据字节之和为 0x0E，校验和为 0xF2。
    record = bytes.fromhex("020000040800")
    accumulator = IntelHexAccumulator()
    accumulator.update(record)
    assert accumulator.result() == 0xF2
    assert (sum(record) + accumulator.result()) & 0xFF == 0


def test_tcp_ip_vectors() -> None:
    assert _result_map(b"")["TCP/IP"].value == 0xFFFF
    assert _result_map(b"\x00\x01")["TCP/IP"].value == 0xFFFE
    assert _result_map(b"\x00\x01\x00\x02")["TCP/IP"].value == 0xFFFC
    assert _result_map(b"\x00\x01\x02")["TCP/IP"].value == 0xFDFE
    assert _result_map(b"\xFF\xFF\x00\x01")["TCP/IP"].value == 0xFFFE
    # RFC 1071 常见示例：IP 头校验和字段置零后计算得到 0xB1E6。
    header = bytes.fromhex("4500003C1C46400040060000AC100A63AC100A0C")
    accumulator = TcpIpAccumulator()
    accumulator.update(header)
    assert accumulator.result() == 0xB1E6


def test_tcp_ip_streaming_odd_boundary() -> None:
    data = bytes(range(37))
    one_shot = TcpIpAccumulator()
    one_shot.update(data)
    streamed = TcpIpAccumulator()
    for index in range(0, len(data), 3):
        streamed.update(data[index : index + 3])
    assert streamed.result() == one_shot.result()


def test_empty_input_values() -> None:
    results = _result_map(b"")
    assert results["Sum-8"].value == 0
    assert results["Sum-16"].value == 0
    assert results["Sum-32"].value == 0
    assert results["XOR-8 (BCC)"].value == 0
    assert results["Parity-Odd"].value == 1
    assert results["Parity-Even"].value == 0
    assert results["Intel HEX"].value == 0
    assert results["TCP/IP"].value == 0xFFFF
    assert results["CRC-32/ISO-HDLC"].value == 0
    assert results["CRC-16/MODBUS"].value == 0xFFFF


def test_invert_applies_per_width() -> None:
    data = b"\x01\x02\x03\x04"
    raw = _result_map(data)
    inverted = _result_map(data, invert=True)
    assert set(raw) == set(inverted)
    for name, result in raw.items():
        mask = (1 << result.width) - 1
        assert inverted[name].value == ~result.value & mask, name
        assert inverted[name].width == result.width


def test_format_value() -> None:
    assert format_value(0x0A, 8) == "0A"
    assert format_value(0x0A, 16) == "000A"
    assert format_value(0x0A, 32) == "0000000A"
    assert format_value(0x1FF, 8) == "FF"
    assert format_value(10, 8, base=2) == "00001010"
    assert format_value(10, 8, base=10) == "10"
    assert format_value(10, 8, base=8) == "012"
    assert format_value(1, 1) == "1"
    assert format_value(0, 1, base=2) == "0"
    assert format_value(0xDEADBEEF, 32) == "DEADBEEF"
    with pytest.raises(ValueError):
        format_value(0, 8, base=1)
    with pytest.raises(ValueError):
        format_value(0, 8, base=37)
    with pytest.raises(ValueError):
        format_value(0, 0)


def test_create_accumulators_names_and_order() -> None:
    accumulators = create_accumulators()
    assert tuple(accumulator.name for accumulator in accumulators) == RESULT_NAMES
    assert len(accumulators) == 8 + len(CRC_PRESETS)
    assert [result.name for result in compute_all(b"")] == list(RESULT_NAMES)


def test_source_text_available_for_every_result() -> None:
    callable_names = set(RESULT_NAMES)
    assert callable_names == {result.name for result in compute_all(b"\x00")}
    for name in RESULT_NAMES:
        snippet = source_text(name)
        assert snippet.strip(), name
        assert "def " in snippet, name
        compile(snippet, "<source>", "exec")
    with pytest.raises(KeyError):
        source_text("not-an-algorithm")


@pytest.mark.parametrize("name", RESULT_NAMES)
def test_source_snippet_executes_and_matches(name: str) -> None:
    data = b"\x00\x01\x02\xFE\xFFCopyright 2026"
    expected = _result_map(data)[name]
    namespace: dict[str, object] = {}
    exec(compile(source_text(name), "<source>", "exec"), namespace)
    functions = [
        value
        for value in namespace.values()
        if callable(value) and getattr(value, "__code__", None) is not None
    ]
    assert len(functions) == 1, name
    assert functions[0](data) == expected.value, name


def test_crc_params_validation() -> None:
    with pytest.raises(ValueError):
        CrcParams(7, 0x09, 0, False, False, 0)
    with pytest.raises(ValueError):
        CrcParams(8, 0x100, 0, False, False, 0)
    with pytest.raises(ValueError):
        CrcParams(8, 0x31, 0x100, False, False, 0)
    with pytest.raises(ValueError):
        CrcParams(8, 0x31, 0, False, False, 0x100)
    with pytest.raises(ValueError):
        CrcParams(8, True, 0, False, False, 0)
    with pytest.raises(ValueError):
        ParityAccumulator("none")
    with pytest.raises(ValueError):
        SumAccumulator(7)

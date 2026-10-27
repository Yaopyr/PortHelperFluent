"""编码转换核心函数测试：进制、补码、七段数码管与 Bin/Hex/Text。"""

from __future__ import annotations

import pytest

from app.core.encoding import (
    BASE_OUTPUT_BASES,
    SEVEN_SEG_COMMON_CATHODE,
    SEVEN_SEGMENT_BITS,
    base_outputs,
    bin_file_to_hex_text,
    format_int,
    hex_text_to_bytes,
    hex_text_to_text,
    parse_int,
    seven_segment_code,
    seven_segment_segments,
    text_to_hex_text,
)


# ---- 进制解析与格式化 -----------------------------------------------------


def test_parse_int_hex_signed_bit_pattern_is_negative() -> None:
    """0xFF 作为 8 位有符号补码就是 -1。"""
    assert parse_int("0xFF", 16, bits=8, signed=True) == -1
    assert parse_int("FF", 16, bits=8, signed=True) == -1
    assert parse_int("255", 10, bits=8, signed=True) == -1
    assert parse_int("FF", 16, bits=8, signed=False) == 255


def test_parse_int_accepts_sign_and_matching_prefixes() -> None:
    assert parse_int("-1", 10, bits=8, signed=True) == -1
    assert parse_int("+42", 10) == 42
    assert parse_int("0b1010", 2) == 10
    assert parse_int("0o17", 8) == 15
    assert parse_int("-0x10", 16, bits=32, signed=True) == -16


def test_parse_int_supports_arbitrary_bases() -> None:
    assert parse_int("z", 36) == 35
    assert parse_int("Z", 36) == 35
    assert parse_int("101", 2) == 5
    assert parse_int("777", 8) == 511
    assert parse_int("1A", 16) == 26


def test_parse_int_rejects_empty_with_chinese_message() -> None:
    for text in ("", "   ", "-", "+"):
        with pytest.raises(ValueError, match="空|缺少数字"):
            parse_int(text, 10)


def test_parse_int_rejects_unsigned_overflow() -> None:
    with pytest.raises(ValueError, match="超出") as excinfo:
        parse_int("256", 10, bits=8)
    assert "8" in str(excinfo.value)
    with pytest.raises(ValueError, match="超出"):
        parse_int("-1", 10, bits=8, signed=False)
    with pytest.raises(ValueError, match="超出"):
        parse_int("0x100", 16, bits=8)


def test_parse_int_rejects_signed_overflow() -> None:
    assert parse_int("127", 10, bits=8, signed=True) == 127
    assert parse_int("128", 10, bits=8, signed=True) == -128
    with pytest.raises(ValueError, match="超出"):
        parse_int("256", 10, bits=8, signed=True)
    with pytest.raises(ValueError, match="超出"):
        parse_int("-129", 10, bits=8, signed=True)


def test_parse_int_rejects_invalid_digits_and_base() -> None:
    with pytest.raises(ValueError, match="2 进制"):
        parse_int("102", 2)
    with pytest.raises(ValueError, match="16 进制"):
        parse_int("GG", 16)
    with pytest.raises(ValueError, match="2–36"):
        parse_int("1", 1)
    with pytest.raises(ValueError, match="2–36"):
        parse_int("1", 37)


def test_format_int_masks_and_uppercases() -> None:
    assert format_int(0x1FF, 16, bits=8) == "FF"
    assert format_int(-1, 16, bits=8) == "FF"
    assert format_int(-1, 2, bits=8) == "11111111"
    assert format_int(0xABCDEF, 16, bits=32) == "ABCDEF"
    assert format_int(35, 36) == "Z"
    assert format_int(0, 8, bits=8) == "0"


def test_format_int_signed_decimal_is_two_complement_value() -> None:
    assert format_int(0xFF, 10, bits=8, signed=True) == "-1"
    assert format_int(-1, 10, bits=8, signed=True) == "-1"
    assert format_int(0x80, 10, bits=8, signed=True) == "-128"
    assert format_int(255, 10, bits=8, signed=False) == "255"
    # 非十进制始终显示补码位模式，而不是负号。
    assert format_int(0xFF, 16, bits=8, signed=True) == "FF"
    assert format_int(0xFF, 2, bits=8, signed=True) == "11111111"


def test_base_outputs_returns_binary_octal_decimal_hex() -> None:
    outputs = base_outputs("255", 10, bits=8)

    assert tuple(outputs) == BASE_OUTPUT_BASES
    assert outputs == {2: "11111111", 8: "377", 10: "255", 16: "FF"}
    assert base_outputs("FF", 16, bits=8, signed=True)[10] == "-1"
    assert base_outputs("FF", 16, bits=8, signed=True)[16] == "FF"


def test_base_outputs_supports_64_bit_values() -> None:
    outputs = base_outputs("-1", 10, bits=64, signed=True)

    assert outputs[2] == "1" * 64
    assert outputs[16] == "FFFFFFFFFFFFFFFF"
    assert outputs[10] == "-1"


def test_base_outputs_propagates_errors() -> None:
    with pytest.raises(ValueError, match="空"):
        base_outputs("", 10)
    with pytest.raises(ValueError, match="超出"):
        base_outputs("100", 16, bits=8)


# ---- 七段数码管 -----------------------------------------------------------


def test_seven_segment_known_codes() -> None:
    assert SEVEN_SEG_COMMON_CATHODE["0"] == 0x3F
    assert seven_segment_code("0") == 0x3F
    assert seven_segment_code("9") == 0x6F
    assert seven_segment_code("A") == 0x77
    assert seven_segment_code("F") == 0x71
    assert seven_segment_code("a") == 0x77
    assert seven_segment_code(" 8 ") == 0x7F


def test_seven_segment_bit_layout() -> None:
    assert SEVEN_SEGMENT_BITS == {
        "a": 0x01,
        "b": 0x02,
        "c": 0x04,
        "d": 0x08,
        "e": 0x10,
        "f": 0x20,
        "g": 0x40,
        "dp": 0x80,
    }


def test_seven_segment_common_anode_is_inverted() -> None:
    assert seven_segment_code("0", common_cathode=False) == 0xC0
    assert seven_segment_code("F", common_cathode=False) == 0x8E
    assert seven_segment_code("0", common_cathode=True) == 0x3F


def test_seven_segment_segments_lists_letters() -> None:
    assert seven_segment_segments(0x3F) == ["a", "b", "c", "d", "e", "f"]
    assert seven_segment_segments(0x06) == ["b", "c"]
    assert seven_segment_segments(0x80) == ["dp"]
    assert seven_segment_segments(0xC0, common_cathode=False) == [
        "a",
        "b",
        "c",
        "d",
        "e",
        "f",
    ]
    assert seven_segment_segments(0xFF) == list(SEVEN_SEGMENT_BITS)


def test_seven_segment_rejects_unknown_character() -> None:
    for char in ("", "G", "10", "?"):
        with pytest.raises(ValueError, match="单个|没有七段"):
            seven_segment_code(char)


# ---- Bin/Hex/Text 文件辅助 ------------------------------------------------


def test_bin_file_to_hex_text_is_uppercase_and_spaced() -> None:
    assert bin_file_to_hex_text(b"\x00\xab\xff") == "00 AB FF"
    assert bin_file_to_hex_text(b"") == ""

    long_text = bin_file_to_hex_text(bytes(range(20)))
    lines = long_text.splitlines()
    assert len(lines) == 2
    assert lines[0] == "00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F"
    assert lines[1] == "10 11 12 13"
    assert "ab" not in long_text


def test_hex_text_and_binary_roundtrip() -> None:
    data = bytes(range(256))
    text = bin_file_to_hex_text(data)

    assert hex_text_to_bytes(text) == data
    assert hex_text_to_bytes("0x01, 02\n0A") == b"\x01\x02\x0a"
    assert bin_file_to_hex_text(hex_text_to_bytes("DE AD BE EF")) == "DE AD BE EF"


def test_hex_text_to_text_uses_selected_codec() -> None:
    assert hex_text_to_text("C4 E3", "gbk") == "你"
    text = "编码转换"
    encoded = text.encode("utf-8")
    assert hex_text_to_text(bin_file_to_hex_text(encoded), "utf-8") == text


def test_hex_text_to_text_replaces_invalid_sequences() -> None:
    assert hex_text_to_text("FF FE", "utf-8") == "\ufffd\ufffd"


def test_text_to_hex_text_matches_bin_hex() -> None:
    assert text_to_hex_text(b"Hi") == "48 69"
    assert text_to_hex_text(b"AB") == bin_file_to_hex_text(b"AB")


def test_invalid_hex_text_raises_chinese_error() -> None:
    for text in ("0x", "0x0", "GG", "0x0G"):
        with pytest.raises(ValueError, match="十六进制"):
            hex_text_to_bytes(text)

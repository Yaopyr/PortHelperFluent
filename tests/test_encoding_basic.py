import locale

import pytest

from app.core.encoding import (
    CODEC_CHOICES,
    decode_bytes,
    encode_text,
    parse_hex,
    resolve_codec,
    to_hex,
)


def test_codec_choices() -> None:
    assert CODEC_CHOICES == ("ansi", "gbk", "utf-8")


def test_resolve_codec_ansi_is_non_empty() -> None:
    codec = resolve_codec("ansi")

    assert isinstance(codec, str)
    assert codec
    assert codec == locale.getpreferredencoding(False)


def test_resolve_codec_is_case_insensitive() -> None:
    assert resolve_codec("UTF-8") == "utf-8"
    assert resolve_codec("GBK") == "gbk"


def test_resolve_codec_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="编码"):
        resolve_codec("no-such-codec")


def test_parse_hex_accepts_prefixes_and_separators() -> None:
    assert parse_hex("0x01 02,0A") == b"\x01\x02\x0a"
    assert parse_hex(r"\x01\x02") == b"\x01\x02"
    assert parse_hex("01\t02\n03;04") == b"\x01\x02\x03\x04"
    assert parse_hex("01020304") == b"\x01\x02\x03\x04"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0x01", b"\x01"),
        ("0X0A", b"\x0a"),
        (r"\x01", b"\x01"),
        (r"\X0A", b"\x0a"),
        ("0x01 02,0A", b"\x01\x02\x0a"),
        ("01\t02\n03;04", b"\x01\x02\x03\x04"),
        (r"\x01\x02", b"\x01\x02"),
        ("0x01\\x02", b"\x01\x02"),
        ("", b""),
        (" \t\r\n,; ", b""),
    ],
)
def test_parse_hex_accepts_supported_forms(text: str, expected: bytes) -> None:
    assert parse_hex(text) == expected


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("0x", "0x"),
        ("0X", "0X"),
        (r"\x", r"\x"),
        ("0x0", "0x0"),
        ("010x2", "x"),
        ("0x0G", "G"),
        ("0x0123 4", "4"),
        ("GG", "GG"),
        ("0x01Z", "Z"),
        ("0x 01", "0x"),
        ("0x0x01", "x"),
    ],
)
def test_parse_hex_rejects_malformed_forms(text: str, marker: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_hex(text)

    message = str(excinfo.value)
    assert marker in message
    assert "十六进制" in message


def test_parse_hex_allows_empty_text() -> None:
    assert parse_hex("") == b""
    assert parse_hex("   ") == b""


def test_parse_hex_rejects_odd_length() -> None:
    with pytest.raises(ValueError, match="奇数"):
        parse_hex("0x1")


def test_parse_hex_names_offending_character() -> None:
    with pytest.raises(ValueError, match="G"):
        parse_hex("0x0G")


def test_parse_hex_rejects_adjacent_0x_fragments_but_accepts_separated() -> None:
    """`0x` 片段不能直接拼接（第二个前缀的 0 会被并入数字），分隔书写才允许。"""
    with pytest.raises(ValueError):
        parse_hex("0x010x02")
    with pytest.raises(ValueError):
        parse_hex("0x01020x03")

    assert parse_hex(r"\x01\x02") == b"\x01\x02"
    assert parse_hex("0x01 0x02") == b"\x01\x02"
    assert parse_hex("0x01\\x02") == b"\x01\x02"


def test_decode_bytes_gbk() -> None:
    assert decode_bytes(b"\xc4\xe3", "gbk") == "你"


def test_decode_bytes_replaces_invalid_sequences() -> None:
    assert decode_bytes(b"\xff\xfe", "utf-8") == "\ufffd\ufffd"


def test_encode_decode_roundtrip() -> None:
    text = "串口调试"

    assert decode_bytes(encode_text(text, "gbk"), "gbk") == text
    assert decode_bytes(encode_text(text, "utf-8"), "utf-8") == text


def test_to_hex() -> None:
    assert to_hex(b"\x01\xab\xff") == "01 AB FF"
    assert to_hex(b"\x01\xab", sep="") == "01AB"
    assert to_hex(b"") == ""

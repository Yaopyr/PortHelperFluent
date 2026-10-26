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


def test_parse_hex_allows_empty_text() -> None:
    assert parse_hex("") == b""
    assert parse_hex("   ") == b""


def test_parse_hex_rejects_odd_length() -> None:
    with pytest.raises(ValueError, match="奇数"):
        parse_hex("0x1")


def test_parse_hex_names_offending_character() -> None:
    with pytest.raises(ValueError, match="G"):
        parse_hex("0x0G")


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

"""``checksum`` 命令：预设向量、取反/字节序展示、自定义 CRC、输入源、过滤与源码。

算法本身由 ``tests/test_checksum.py`` 覆盖；本文件只验证 CLI 契约（参数、``data``
字段、过滤语义与退出码），因此断言集中在「123456789」这组标准向量上。
"""

from __future__ import annotations

import io
import sys

from app.core.checksum import RESULT_NAMES

# 标准向量：b"123456789" 的 CRC 结果。
CRC32_ISO_HDLC = 0xCBF43926
CRC16_MODBUS = 0x4B37
CRC16_CCITT_FALSE = 0x29B1
CRC8_MAXIM = 0xA1


class _FakeStdin:
    """替身标准输入：只提供 ``buffer``，与 ``read_input`` 的读取方式一致。"""

    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def _names(data: dict) -> list[str]:
    return [item["name"] for item in data["results"]]


# ---------------------------------------------------------------------------
# 预设向量与结果结构
# ---------------------------------------------------------------------------


def test_known_crc_vectors(cli) -> None:
    data = cli(["checksum", "--text", "123456789", "--only", "CRC-32/ISO-HDLC"])["data"]
    assert data["results"][0]["value"] == CRC32_ISO_HDLC
    assert data["results"][0]["hex"] == "CBF43926"

    # 多个 --only 取并集；名称大小写不敏感、按子串匹配。
    data = cli(
        [
            "checksum",
            "--text",
            "123456789",
            "--only",
            "crc-16/modbus",
            "--only",
            "crc-16/ccitt-false",
            "--only",
            "crc-8/maxim",
        ]
    )["data"]
    values = {item["name"]: item["value"] for item in data["results"]}
    assert values == {
        "CRC-16/MODBUS": CRC16_MODBUS,
        "CRC-16/CCITT-FALSE": CRC16_CCITT_FALSE,
        "CRC-8/MAXIM": CRC8_MAXIM,
    }


def test_envelope_and_data_shape(cli) -> None:
    payload = cli(["checksum", "--text", "123456789", "--only", "crc-32"])
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["command"] == "checksum"

    data = payload["data"]
    assert set(data) == {"size", "encoding", "inverted", "byte_order", "results"}
    assert data["size"] == 9
    assert data["encoding"] == "utf-8"
    assert data["inverted"] is False
    assert data["byte_order"] == "big"

    item = data["results"][0]
    assert set(item) == {"name", "width", "value", "dec", "hex", "bin"}
    assert item["name"] == "CRC-32/ISO-HDLC"
    assert item["width"] == 32
    assert item["dec"] == str(CRC32_ISO_HDLC)
    assert item["bin"] == format(CRC32_ISO_HDLC, "032b")


def test_default_results_cover_every_row(cli) -> None:
    data = cli(["checksum", "--text", "123456789"])["data"]
    assert _names(data) == list(RESULT_NAMES)


# ---------------------------------------------------------------------------
# 取反与字节序展示
# ---------------------------------------------------------------------------


def test_invert_and_little_endian_display(cli) -> None:
    data = cli(
        [
            "checksum",
            "--text",
            "123456789",
            "--only",
            "CRC-16/MODBUS",
            "--invert",
            "--byte-order",
            "little",
        ]
    )["data"]
    assert data["inverted"] is True
    assert data["byte_order"] == "little"

    item = data["results"][0]
    inverted = CRC16_MODBUS ^ 0xFFFF  # 0xB4C8
    assert item["value"] == inverted
    assert item["dec"] == str(inverted)
    # 只有 hex/bin 按小端交换字节：0xB4C8 -> 0xC8B4；value/dec 仍是算术值。
    assert item["hex"] == "C8B4"
    assert item["bin"] == format(0xC8B4, "016b")


def test_big_endian_display_is_default(cli) -> None:
    data = cli(["checksum", "--text", "123456789", "--only", "CRC-16/MODBUS", "--invert"])["data"]
    item = data["results"][0]
    assert item["hex"] == "B4C8"
    assert item["value"] == 0xB4C8


def test_little_byte_order_leaves_8bit_values_alone(cli) -> None:
    data = cli(
        ["checksum", "--text", "123456789", "--only", "CRC-8/MAXIM", "--byte-order", "little"]
    )["data"]
    assert data["results"][0]["hex"] == "A1"
    assert data["byte_order"] == "little"


# ---------------------------------------------------------------------------
# 自定义 CRC
# ---------------------------------------------------------------------------


def test_custom_crc_matches_maxim(cli) -> None:
    data = cli(["checksum", "--text", "123456789", "--custom-crc", "8,0x31,0,true,true,0"])["data"]
    custom = [item for item in data["results"] if item["name"] == "Custom CRC"]
    assert len(custom) == 1
    assert custom[0]["value"] == CRC8_MAXIM
    assert custom[0]["width"] == 8
    assert custom[0]["hex"] == "A1"
    # 自定义 CRC 追加在预设之后，默认结果不受影响。
    assert data["results"][-1]["name"] == "Custom CRC"
    assert _names(data)[:-1] == list(RESULT_NAMES)


def test_custom_crc_accepts_decimal_and_boolean_digits(cli) -> None:
    # int(x, 0) 接受十进制；布尔接受 1/0，与 MODBUS 参数等价。
    data = cli(["checksum", "--text", "123456789", "--custom-crc", "16,0x8005,65535,1,1,0"])["data"]
    custom = [item for item in data["results"] if item["name"] == "Custom CRC"][0]
    assert custom["value"] == CRC16_MODBUS


def test_custom_crc_rejects_bad_arguments(cli) -> None:
    # 字段数不足、位宽不受支持、布尔非 true/false/1/0 都是参数错误（退出码 2）。
    for bad in ("8,0x31,0,true,true", "7,0x31,0,true,true,0", "8,0x31,0,maybe,true,0"):
        payload = cli(["checksum", "--text", "x", "--custom-crc", bad])
        assert payload["exit_code"] == 2
        assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# 输入源
# ---------------------------------------------------------------------------


def test_file_and_stdin_inputs(cli, tmp_path, monkeypatch) -> None:
    vector_path = tmp_path / "vector.bin"
    vector_path.write_bytes(b"123456789")
    from_file = cli(
        ["checksum", "--file", str(vector_path), "--only", "CRC-32/ISO-HDLC", "--encoding", "gbk"]
    )["data"]

    monkeypatch.setattr(sys, "stdin", _FakeStdin(b"123456789"))
    from_stdin = cli(["checksum", "--stdin", "--only", "CRC-32/ISO-HDLC"])["data"]

    for data in (from_file, from_stdin):
        assert data["size"] == 9
        assert data["results"][0]["value"] == CRC32_ISO_HDLC
    assert from_file["encoding"] == "gbk"
    assert from_stdin["encoding"] == "utf-8"


def test_encoding_controls_input_bytes(cli) -> None:
    # 同一个文本按不同编码得到不同的字节，CRC-32 因此不同。
    gbk = cli(["checksum", "--text", "中文", "--encoding", "gbk", "--only", "crc-32"])["data"]
    utf8 = cli(["checksum", "--text", "中文", "--only", "crc-32"])["data"]
    assert (gbk["size"], gbk["encoding"]) == (4, "gbk")
    assert (utf8["size"], utf8["encoding"]) == (6, "utf-8")
    assert gbk["results"][0]["value"] != utf8["results"][0]["value"]


def test_invalid_input_reuses_task1_errors(cli) -> None:
    payload = cli(["checksum", "--hex", "0x0"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "invalid_input"

    payload = cli(["checksum", "--text", "x", "--encoding", "no-such-codec"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"

    payload = cli(["checksum"])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# 过滤
# ---------------------------------------------------------------------------


def test_filters_produce_empty_results_not_error(cli) -> None:
    for filter_flag in ("--only", "--crc"):
        payload = cli(["checksum", "--text", "x", filter_flag, "no-such"])
        assert payload["ok"] is True
        assert payload["exit_code"] == 0
        assert payload["data"]["results"] == []


def test_crc_filter_keeps_only_matching_presets(cli) -> None:
    data = cli(["checksum", "--text", "123456789", "--crc", "crc-16"])["data"]
    assert _names(data) == [
        "CRC-16/MODBUS",
        "CRC-16/CCITT-FALSE",
        "CRC-16/XMODEM",
        "CRC-16/KERMIT",
    ]


# ---------------------------------------------------------------------------
# 源码示例
# ---------------------------------------------------------------------------


def test_source_text_preview(cli) -> None:
    data = cli(["checksum", "--text", "123456789", "--source", "CRC-32/ISO-HDLC"])["data"]
    assert "def crc" in data["source"]
    assert "def crc_32_iso_hdlc" in data["source"]
    # 只额外附加 source，结果列表保持默认全集。
    assert set(data) == {"size", "encoding", "inverted", "byte_order", "results", "source"}
    assert _names(data) == list(RESULT_NAMES)


def test_unknown_source_name_is_not_found(cli) -> None:
    payload = cli(["checksum", "--text", "123456789", "--source", "no-such"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"

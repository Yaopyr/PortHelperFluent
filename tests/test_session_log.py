from datetime import datetime
from pathlib import Path

from app.services.session_log import LogRecord, SessionLog


def _record(direction: str, text: str, source: str = "COM3") -> LogRecord:
    return LogRecord(
        timestamp=datetime(2026, 10, 26, 12, 34, 56, 789000),
        direction=direction,
        source=source,
        data=text.encode("utf-8"),
    )


def test_add_bytes_returns_record_and_counts() -> None:
    log = SessionLog()

    record = log.add_bytes(b"hello", "rx", "COM3")

    assert isinstance(record, LogRecord)
    assert record.direction == "rx"
    assert record.source == "COM3"
    assert log.records() == (record,)
    assert log.total_bytes() == 5
    assert log.total_bytes("rx") == 5
    assert log.total_bytes("tx") == 0


def test_max_records_drops_oldest_but_keeps_lifetime_counters() -> None:
    log = SessionLog(max_records=3)

    for index in range(5):
        log.add_bytes(str(index).encode("ascii"), "rx")

    records = log.records()
    assert [record.data for record in records] == [b"2", b"3", b"4"]
    assert log.total_bytes("rx") == 5


def test_max_bytes_drops_oldest_records() -> None:
    log = SessionLog(max_bytes=10)

    for _ in range(3):
        log.add_bytes(b"abcd", "tx")

    assert len(log.records()) == 2
    assert log.total_bytes("tx") == 12


def test_oversized_record_is_truncated_to_newest_bytes() -> None:
    log = SessionLog(max_bytes=4, max_records=10)

    log.add_bytes(b"0123456789", "rx")

    records = log.records()
    assert len(records) == 1
    assert records[0].data == b"6789"
    assert sum(len(record.data) for record in records) <= 4
    assert log.total_bytes("rx") == 10


def test_retained_bytes_stay_within_cap_after_oversized_record() -> None:
    log = SessionLog(max_bytes=6, max_records=10)

    log.add_bytes(b"abcdefghij", "rx")
    log.add_bytes(b"12", "tx")

    records = log.records()
    assert [record.data for record in records] == [b"12"]
    assert sum(len(record.data) for record in records) <= 6
    assert log.total_bytes() == 12


def test_total_bytes_without_direction_counts_only_rx_and_tx() -> None:
    log = SessionLog()
    log.add_bytes(b"abc", "rx")
    log.add_bytes(b"de", "tx")
    log.add_bytes(b"notice", "info")

    assert log.total_bytes() == 5
    assert log.total_bytes("rx") == 3
    assert log.total_bytes("tx") == 2
    assert log.total_bytes("info") == 6


def test_clear_empties_records_but_keeps_lifetime_counters() -> None:
    log = SessionLog()
    log.add_bytes(b"abc", "rx")

    log.clear()

    assert log.records() == ()
    assert log.total_bytes() == 3


def test_reset_counters_zeros_totals_without_clearing_records() -> None:
    log = SessionLog()
    log.add_bytes(b"abc", "rx")
    log.add_bytes(b"de", "tx")
    log.add_bytes(b"notice", "info")

    log.reset_counters()

    assert len(log.records()) == 3
    assert log.total_bytes() == 0
    assert log.total_bytes("rx") == 0
    assert log.total_bytes("tx") == 0
    assert log.total_bytes("info") == 0
    log.add_bytes(b"x", "rx")
    assert log.total_bytes("rx") == 1


def test_oversized_record_truncation_counts_dropped_bytes() -> None:
    log = SessionLog(max_bytes=4, max_records=10)

    log.add_bytes(b"0123456789", "rx")

    assert log.records()[0].data == b"6789"
    assert log.dropped_bytes == 6
    assert log.dropped_records == 0


def test_record_cap_drops_increment_dropped_counters() -> None:
    log = SessionLog(max_records=3)

    for index in range(5):
        log.add_bytes(str(index).encode("ascii"), "rx")

    assert [record.data for record in log.records()] == [b"2", b"3", b"4"]
    assert log.dropped_records == 2
    assert log.dropped_bytes == 2


def test_byte_cap_drops_increment_dropped_counters() -> None:
    log = SessionLog(max_bytes=10)

    for _ in range(3):
        log.add_bytes(b"abcd", "tx")

    assert len(log.records()) == 2
    assert log.dropped_records == 1
    assert log.dropped_bytes == 4


def test_clear_releases_byte_budget_and_keeps_dropped_stats() -> None:
    log = SessionLog(max_bytes=4)
    log.add_bytes(b"abcdef", "rx")
    assert log.dropped_bytes == 2

    log.clear()

    assert log.records() == ()
    assert log.total_bytes("rx") == 6
    log.add_bytes(b"wxyz", "tx")
    assert [record.data for record in log.records()] == [b"wxyz"]
    assert log.dropped_bytes == 2
    assert log.dropped_records == 0


def test_export_txt_contains_chinese_and_prefixes(tmp_path: Path) -> None:
    log = SessionLog()
    log.add(_record("rx", "你好"))
    log.add(_record("tx", "发送", source="COM4"))
    target = tmp_path / "session.txt"

    log.export_txt(target)

    text = target.read_text(encoding="utf-8")
    assert "[12:34:56.789] [RX] COM3: 你好" in text
    assert "[12:34:56.789] [TX] COM4: 发送" in text


def test_export_txt_hex_mode(tmp_path: Path) -> None:
    log = SessionLog()
    log.add(_record("rx", "A"))
    target = tmp_path / "session.txt"

    log.export_txt(target, hex_mode=True)

    assert "[RX] COM3: 41" in target.read_text(encoding="utf-8")


def test_export_txt_decodes_payload_with_codec_and_writes_utf8(tmp_path: Path) -> None:
    log = SessionLog()
    log.add(
        LogRecord(
            timestamp=datetime(2026, 10, 26, 12, 34, 56, 789000),
            direction="rx",
            source="COM3",
            data="中文".encode("gbk"),
        )
    )
    target = tmp_path / "session.txt"

    log.export_txt(target, codec="gbk")

    assert "中文" in target.read_text(encoding="utf-8")


def test_export_csv_has_bom_header_and_rows(tmp_path: Path) -> None:
    log = SessionLog()
    log.add(_record("rx", "中文"))
    target = tmp_path / "session.csv"

    log.export_csv(target)

    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert text.splitlines()[0] == "time,direction,source,length,hex,text"
    assert "中文" in text
    assert "E4B8AD" in text.upper().replace(" ", "")


def test_export_csv_hex_mode_column(tmp_path: Path) -> None:
    log = SessionLog()
    log.add(_record("tx", "A"))
    target = tmp_path / "session.csv"

    log.export_csv(target, hex_mode=True)

    text = target.read_text(encoding="utf-8-sig")
    assert "TX" in text
    assert "41" in text

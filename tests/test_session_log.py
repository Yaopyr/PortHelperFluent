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


def test_single_oversized_record_is_kept() -> None:
    log = SessionLog(max_bytes=4, max_records=10)

    log.add_bytes(b"0123456789", "rx")

    assert len(log.records()) == 1
    assert log.total_bytes("rx") == 10


def test_clear_empties_records_but_keeps_lifetime_counters() -> None:
    log = SessionLog()
    log.add_bytes(b"abc", "rx")

    log.clear()

    assert log.records() == ()
    assert log.total_bytes() == 3


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

from pathlib import Path

from app.services.exports import sanitize_name, unique_path, write_bytes_atomic, write_text_atomic


def test_sanitize_name_removes_forbidden_characters() -> None:
    assert sanitize_name('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij"


def test_sanitize_name_removes_control_characters() -> None:
    assert sanitize_name("串口\x00调试\x1f记录") == "串口调试记录"


def test_sanitize_name_keeps_chinese_and_spaces() -> None:
    assert sanitize_name("取模 输出") == "取模 输出"


def test_sanitize_name_falls_back_when_empty() -> None:
    assert sanitize_name("") == "data"
    assert sanitize_name("???:", fallback="export") == "export"


def test_sanitize_name_strips_trailing_dots_and_spaces() -> None:
    assert sanitize_name("记录. ") == "记录"


def test_unique_path_returns_original_when_free(tmp_path: Path) -> None:
    target = tmp_path / "record.txt"

    assert unique_path(target) == target


def test_unique_path_appends_counter_before_suffix(tmp_path: Path) -> None:
    target = tmp_path / "record.txt"
    target.write_text("first", encoding="utf-8")
    (tmp_path / "record_1.txt").write_text("second", encoding="utf-8")

    assert unique_path(target) == tmp_path / "record_2.txt"


def test_write_text_atomic_writes_utf8_without_tmp_file(tmp_path: Path) -> None:
    target = tmp_path / "out" / "记录.txt"

    write_text_atomic(target, "中文内容")

    assert target.read_text(encoding="utf-8") == "中文内容"
    assert list(target.parent.glob("*.tmp")) == []


def test_write_text_atomic_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")

    write_text_atomic(target, "new")

    assert target.read_text(encoding="utf-8") == "new"


def test_write_bytes_atomic(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"

    write_bytes_atomic(target, b"\x00\x01\xff")

    assert target.read_bytes() == b"\x00\x01\xff"
    assert list(tmp_path.glob("*.tmp")) == []

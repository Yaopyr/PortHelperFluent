import json
from datetime import datetime
from pathlib import Path

import pytest

from app.core.settings import DEFAULT_SETTINGS, Settings


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.json"


def test_load_without_file_gives_defaults(config_path: Path) -> None:
    settings = Settings(config_path)
    settings.load()

    assert settings.get("serial.baud") == 115200
    assert settings.get("ui.theme") == "auto"
    assert settings.get("missing.key") is None
    assert settings.get("missing.key", "fallback") == "fallback"
    assert settings.warning is None
    assert settings.as_dict() == DEFAULT_SETTINGS


def test_set_and_get(config_path: Path) -> None:
    settings = Settings(config_path)
    settings.load()

    settings.set("serial.port", "COM3")
    settings.set("ui.max_display_lines", 500)

    assert settings.get("serial.port") == "COM3"
    assert settings.get("ui.max_display_lines") == 500


def test_update(config_path: Path) -> None:
    settings = Settings(config_path)
    settings.load()

    settings.update({"serial.port": "COM9", "net.port": 9000})

    assert settings.get("serial.port") == "COM9"
    assert settings.get("net.port") == 9000
    assert settings.get("serial.baud") == 115200


def test_save_and_reload(config_path: Path) -> None:
    settings = Settings(config_path)
    settings.load()
    settings.set("serial.port", "COM7")
    settings.set("ui.auto_scroll", False)
    settings.save()

    reloaded = Settings(config_path)
    reloaded.load()

    assert reloaded.get("serial.port") == "COM7"
    assert reloaded.get("ui.auto_scroll") is False
    assert reloaded.warning is None
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["serial.port"] == "COM7"


def test_unknown_keys_in_file_are_preserved(config_path: Path) -> None:
    config_path.write_text(
        json.dumps({"serial.port": "COM4", "future.feature": {"enabled": True}}, ensure_ascii=False),
        encoding="utf-8",
    )
    settings = Settings(config_path)
    settings.load()

    settings.set("serial.baud", 9600)
    settings.save()

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["future.feature"] == {"enabled": True}
    assert raw["serial.port"] == "COM4"
    assert raw["serial.baud"] == 9600


def test_corrupt_file_is_backed_up_and_defaults_restored(config_path: Path) -> None:
    config_path.write_text("{ this is not json", encoding="utf-8")
    settings = Settings(config_path)
    settings.load()

    backups = list(config_path.parent.glob("config.bad-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{ this is not json"
    assert settings.warning is not None
    assert "备份" in settings.warning
    assert settings.get("serial.baud") == 115200


def test_non_object_json_is_treated_as_corrupt(config_path: Path) -> None:
    config_path.write_text("[1, 2, 3]", encoding="utf-8")
    settings = Settings(config_path)
    settings.load()

    assert settings.warning is not None
    assert len(list(config_path.parent.glob("config.bad-*.json"))) == 1
    assert settings.get("serial.baud") == 115200


def test_reset_restores_defaults(config_path: Path) -> None:
    settings = Settings(config_path)
    settings.load()
    settings.set("serial.port", "COM3")
    settings.set("custom.key", "value")

    settings.reset()

    assert settings.as_dict() == DEFAULT_SETTINGS
    assert settings.get("custom.key") is None


def test_save_writes_readable_utf8_json(config_path: Path) -> None:
    settings = Settings(config_path)
    settings.load()
    settings.set("serial.port", "串口一")

    settings.save()

    text = config_path.read_text(encoding="utf-8")
    assert "串口一" in text


def test_save_creates_parent_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "nested" / "dir" / "config.json"
    settings = Settings(config_path)
    settings.load()

    settings.save()

    assert config_path.is_file()
    assert not list(config_path.parent.glob("*.tmp"))


def test_bom_prefixed_config_is_loaded(config_path: Path) -> None:
    payload = json.dumps({"serial.port": "COM5"}, ensure_ascii=False).encode("utf-8")
    config_path.write_bytes(b"\xef\xbb\xbf" + payload)
    settings = Settings(config_path)

    settings.load()

    assert settings.warning is None
    assert settings.get("serial.port") == "COM5"
    assert list(config_path.parent.glob("config.bad-*.json")) == []


def test_read_oserror_keeps_defaults_without_renaming(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_text = '{"serial.port": "COM5"}'
    config_path.write_text(original_text, encoding="utf-8")
    original_read_text = Path.read_text

    def locked_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == config_path:
            raise PermissionError(13, "拒绝访问", str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked_read_text)
    settings = Settings(config_path)

    settings.load()

    assert config_path.is_file()
    assert original_read_text(config_path, encoding="utf-8") == original_text
    assert list(config_path.parent.glob("config.bad-*.json")) == []
    assert settings.warning is not None
    assert config_path.name in settings.warning
    assert "读取" in settings.warning
    assert "默认" in settings.warning
    assert settings.get("serial.port") == ""
    assert settings.get("serial.baud") == 115200


def test_invalid_utf8_is_backed_up_and_defaults_restored(config_path: Path) -> None:
    config_path.write_bytes(b'{"serial.port": "\xff\xfe"}')
    settings = Settings(config_path)

    settings.load()

    assert settings.warning is not None
    assert "备份" in settings.warning
    assert len(list(config_path.parent.glob("config.bad-*.json"))) == 1
    assert settings.get("serial.port") == ""


def test_corrupt_loads_in_same_second_create_distinct_backups(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> "FixedDatetime":
            return cls(2026, 10, 26, 13, 45, 0, 123456)

    monkeypatch.setattr("app.core.settings.datetime", FixedDatetime)
    config_path.write_text("{ first", encoding="utf-8")
    Settings(config_path).load()
    config_path.write_text("{ second", encoding="utf-8")
    Settings(config_path).load()

    backups = list(config_path.parent.glob("config.bad-*.json"))
    assert len(backups) == 2
    assert len({backup.name for backup in backups}) == 2
    assert {backup.read_text(encoding="utf-8") for backup in backups} == {
        "{ first",
        "{ second",
    }

import json
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

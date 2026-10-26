import sys
from pathlib import Path

from app.services.paths import APP_NAME, ensure_dirs, resolve_paths


def _make_exe_dir(tmp_path: Path) -> Path:
    exe_dir = tmp_path / "PortHelper"
    exe_dir.mkdir()
    return exe_dir


def test_portable_flag_redirects_data_next_to_exe(tmp_path: Path) -> None:
    exe_dir = _make_exe_dir(tmp_path)
    (exe_dir / "portable.flag").write_text("1", encoding="utf-8")

    paths = resolve_paths(exe_path=exe_dir, appdata=tmp_path / "appdata")

    assert paths.portable is True
    assert paths.exe_dir == exe_dir
    assert paths.data_dir == exe_dir / "data"
    assert paths.config_file == exe_dir / "data" / "config.json"
    assert paths.log_dir == exe_dir / "data" / "logs"


def test_non_portable_uses_injected_appdata(tmp_path: Path) -> None:
    exe_dir = _make_exe_dir(tmp_path)
    appdata = tmp_path / "roaming"

    paths = resolve_paths(exe_path=exe_dir, appdata=appdata)

    assert paths.portable is False
    assert paths.data_dir == appdata / APP_NAME
    assert paths.config_file == appdata / APP_NAME / "config.json"
    assert paths.log_dir == appdata / APP_NAME / "logs"


def test_non_portable_never_writes_next_to_exe(tmp_path: Path) -> None:
    exe_dir = _make_exe_dir(tmp_path)

    paths = resolve_paths(exe_path=exe_dir, appdata=tmp_path / "roaming")

    assert exe_dir not in paths.data_dir.parents
    assert exe_dir not in paths.log_dir.parents


def test_default_exe_dir_is_repo_root_when_not_frozen(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)

    paths = resolve_paths(appdata=Path("C:/appdata"))

    assert paths.exe_dir == Path(__file__).resolve().parents[1]


def test_default_exe_dir_follows_sys_executable_when_frozen(tmp_path: Path, monkeypatch) -> None:
    exe_dir = _make_exe_dir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "PortHelperFluent.exe"))

    paths = resolve_paths(appdata=tmp_path / "roaming")

    assert paths.exe_dir == exe_dir


def test_ensure_dirs_creates_data_and_log_dirs(tmp_path: Path) -> None:
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "roaming")

    ensure_dirs(paths)

    assert paths.data_dir.is_dir()
    assert paths.log_dir.is_dir()

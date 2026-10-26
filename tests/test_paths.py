import sys
from pathlib import Path

import pytest

from app.services.paths import APP_NAME, ensure_dirs, ensure_writable_paths, resolve_paths


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

    effective = ensure_dirs(paths)

    assert effective == paths
    assert paths.data_dir.is_dir()
    assert paths.log_dir.is_dir()
    # 可写性探针文件必须自行清理，成功路径上不允许残留。
    assert list(paths.data_dir.glob("*.probe")) == []
    assert list(paths.log_dir.glob("*.probe")) == []


def test_portable_existing_readonly_dirs_fall_back_to_appdata(tmp_path: Path, monkeypatch) -> None:
    exe_dir = _make_exe_dir(tmp_path)
    (exe_dir / "portable.flag").write_text("1", encoding="utf-8")
    (exe_dir / "data" / "logs").mkdir(parents=True)  # 目录已存在，mkdir 会静默成功
    appdata = tmp_path / "roaming"
    paths = resolve_paths(exe_path=exe_dir, appdata=appdata)
    original_open = Path.open

    def fake_open(self: Path, *args, **kwargs):
        if self.suffix == ".probe":
            raise PermissionError(13, "拒绝访问", str(self))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)

    effective, warning = ensure_writable_paths(paths, appdata=appdata)

    assert effective.portable is False
    assert effective.data_dir == appdata / APP_NAME
    assert effective.config_file == appdata / APP_NAME / "config.json"
    assert effective.log_dir == appdata / APP_NAME / "logs"
    assert effective.data_dir.is_dir()
    assert effective.log_dir.is_dir()
    assert warning
    assert "便携" in warning
    assert "AppData" in warning
    assert str(appdata / APP_NAME) in warning
    assert list(paths.data_dir.glob("*.probe")) == []
    assert list(paths.log_dir.glob("*.probe")) == []


def test_portable_dir_failure_falls_back_to_appdata(tmp_path: Path, monkeypatch) -> None:
    exe_dir = _make_exe_dir(tmp_path)
    (exe_dir / "portable.flag").write_text("1", encoding="utf-8")
    appdata = tmp_path / "roaming"
    paths = resolve_paths(exe_path=exe_dir, appdata=appdata)
    original_mkdir = Path.mkdir

    def fake_mkdir(self: Path, *args, **kwargs) -> None:
        if self == paths.data_dir:
            raise PermissionError(13, "拒绝访问", str(self))
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)

    effective, warning = ensure_writable_paths(paths, appdata=appdata)

    assert effective.portable is False
    assert effective.data_dir == appdata / APP_NAME
    assert effective.config_file == appdata / APP_NAME / "config.json"
    assert effective.log_dir == appdata / APP_NAME / "logs"
    assert effective.data_dir.is_dir()
    assert effective.log_dir.is_dir()
    assert warning
    assert "便携" in warning
    assert "AppData" in warning
    assert str(appdata / APP_NAME) in warning
    assert not paths.data_dir.exists()


def test_non_portable_dir_failure_propagates(tmp_path: Path, monkeypatch) -> None:
    appdata = tmp_path / "roaming"
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=appdata)
    original_mkdir = Path.mkdir

    def fake_mkdir(self: Path, *args, **kwargs) -> None:
        if self == paths.data_dir:
            raise PermissionError(13, "拒绝访问", str(self))
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)

    with pytest.raises(PermissionError):
        ensure_writable_paths(paths)

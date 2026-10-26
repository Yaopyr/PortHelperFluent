import os
import stat
import sys
from pathlib import Path

import pytest

from app.services.paths import APP_NAME, ensure_dirs, ensure_writable_paths, resolve_paths


def _make_exe_dir(tmp_path: Path) -> Path:
    exe_dir = tmp_path / "PortHelper"
    exe_dir.mkdir()
    return exe_dir


def _make_read_only_file(path: Path, content: str) -> None:
    """创建带内容的只读文件，并返回后由调用方在 finally 里恢复可写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, stat.S_IREAD)


def _restore_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


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

    result = ensure_dirs(paths)

    assert result is None
    assert paths.data_dir.is_dir()
    assert paths.log_dir.is_dir()


def test_ensure_dirs_raises_and_does_not_fall_back(tmp_path: Path, monkeypatch) -> None:
    appdata = tmp_path / "roaming"
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=appdata)
    original_mkdir = Path.mkdir

    def fake_mkdir(self: Path, *args, **kwargs) -> None:
        if self == paths.data_dir:
            raise PermissionError(13, "拒绝访问", str(self))
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)

    with pytest.raises(PermissionError):
        ensure_dirs(paths)

    # ensure_dirs 只创建或抛错，回退逻辑属于 ensure_writable_paths。
    assert not (appdata / APP_NAME).exists()


def test_ensure_writable_paths_creates_dirs_and_cleans_probes(tmp_path: Path) -> None:
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "roaming")

    effective, warning = ensure_writable_paths(paths)

    assert effective == paths
    assert warning is None
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
        # 只有便携目录只读；AppData 回退目录必须能被真实探测（写入探针）。
        if self.suffix == ".probe" and paths.data_dir in self.parents:
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


def test_appdata_fallback_is_probed_for_writability(tmp_path: Path, monkeypatch) -> None:
    """AppData 目录能创建但实际只读时，必须探测出来并抛出中文错误，而不是返回坏路径。"""
    exe_dir = _make_exe_dir(tmp_path)
    (exe_dir / "portable.flag").write_text("1", encoding="utf-8")
    appdata = tmp_path / "roaming"
    paths = resolve_paths(exe_path=exe_dir, appdata=appdata)
    original_mkdir = Path.mkdir
    original_open = Path.open

    def fake_mkdir(self: Path, *args, **kwargs) -> None:
        if self == paths.data_dir:
            raise PermissionError(13, "拒绝访问", str(self))
        original_mkdir(self, *args, **kwargs)

    def fake_open(self: Path, *args, **kwargs):
        if self.suffix == ".probe" and appdata in self.parents:
            raise PermissionError(13, "拒绝访问", str(self))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    monkeypatch.setattr(Path, "open", fake_open)

    with pytest.raises(OSError) as info:
        ensure_writable_paths(paths, appdata=appdata)

    message = str(info.value)
    assert "不可写" in message
    assert str(appdata / APP_NAME) in message
    assert list((appdata / APP_NAME / "logs").glob("*.probe")) == []


def test_portable_and_appdata_both_unwritable_raises_chinese_error(
    tmp_path: Path, monkeypatch
) -> None:
    exe_dir = _make_exe_dir(tmp_path)
    (exe_dir / "portable.flag").write_text("1", encoding="utf-8")
    appdata = tmp_path / "roaming"
    paths = resolve_paths(exe_path=exe_dir, appdata=appdata)
    original_mkdir = Path.mkdir

    def fake_mkdir(self: Path, *args, **kwargs) -> None:
        if self in (paths.data_dir, appdata / APP_NAME):
            raise PermissionError(13, "拒绝访问", str(self))
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)

    with pytest.raises(OSError) as info:
        ensure_writable_paths(paths, appdata=appdata)

    message = str(info.value)
    assert "不可写" in message
    assert str(paths.data_dir) in message
    assert str(appdata / APP_NAME) in message
    assert isinstance(info.value.__cause__, PermissionError)


def _portable_layout(tmp_path: Path):
    exe_dir = _make_exe_dir(tmp_path)
    (exe_dir / "portable.flag").write_text("1", encoding="utf-8")
    appdata = tmp_path / "roaming"
    return resolve_paths(exe_path=exe_dir, appdata=appdata), appdata


@pytest.mark.parametrize(
    ("target_name", "content", "label"),
    [
        ("app.log", "旧日志内容\n", "日志文件"),
        ("config.json", '{"ui.theme": "dark"}\n', "配置文件"),
    ],
)
def test_portable_read_only_existing_file_falls_back_to_appdata(
    tmp_path: Path, target_name: str, content: str, label: str
) -> None:
    """便携目录可写但其中的 app.log/config.json 只读时，也必须回退到 AppData。"""
    paths, appdata = _portable_layout(tmp_path)
    assert paths.portable is True
    target = (paths.log_dir / "app.log") if target_name == "app.log" else paths.config_file
    _make_read_only_file(target, content)

    try:
        effective, warning = ensure_writable_paths(paths, appdata=appdata)
    finally:
        _restore_writable(target)

    assert effective.portable is False
    assert effective.data_dir == appdata / APP_NAME
    assert effective.log_dir == appdata / APP_NAME / "logs"
    assert warning
    assert "便携" in warning
    assert "AppData" in warning
    assert str(target) in warning
    assert label in warning
    # 探针只打开并关闭，绝不改写被探测的文件。
    assert target.read_text(encoding="utf-8") == content
    assert list(paths.data_dir.rglob("*.probe")) == []


@pytest.mark.parametrize(
    ("target_name", "content", "label"),
    [
        ("app.log", "旧日志内容\n", "日志文件"),
        ("config.json", '{"ui.theme": "dark"}\n', "配置文件"),
    ],
)
def test_appdata_read_only_existing_file_raises_chinese_error(
    tmp_path: Path, target_name: str, content: str, label: str
) -> None:
    """非便携模式下已有文件只读时，必须抛出清晰的中文错误，而不是返回坏路径。"""
    appdata = tmp_path / "roaming"
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=appdata)
    target = (paths.log_dir / "app.log") if target_name == "app.log" else paths.config_file
    _make_read_only_file(target, content)

    try:
        with pytest.raises(OSError) as info:
            ensure_writable_paths(paths)
    finally:
        _restore_writable(target)

    message = str(info.value)
    assert label in message
    assert str(target) in message
    assert "只读" in message
    assert isinstance(info.value.__cause__, PermissionError)
    assert target.read_text(encoding="utf-8") == content
    assert list(paths.data_dir.rglob("*.probe")) == []


def test_portable_read_only_file_and_unwritable_appdata_raises_chinese_error(
    tmp_path: Path, monkeypatch
) -> None:
    """便携目录里的文件只读、AppData 也不可写时，回退失败必须给出中文错误。"""
    paths, appdata = _portable_layout(tmp_path)
    target = paths.log_dir / "app.log"
    _make_read_only_file(target, "旧日志内容\n")
    original_open = Path.open

    def fake_open(self: Path, *args, **kwargs):
        if self.suffix == ".probe" and appdata in self.parents:
            raise PermissionError(13, "拒绝访问", str(self))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)

    try:
        with pytest.raises(OSError) as info:
            ensure_writable_paths(paths, appdata=appdata)
    finally:
        _restore_writable(target)

    message = str(info.value)
    assert "不可写" in message
    assert str(appdata / APP_NAME) in message
    assert target.read_text(encoding="utf-8") == "旧日志内容\n"

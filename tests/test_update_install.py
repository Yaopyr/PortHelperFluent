"""自替换安装器测试：只生成脚本并注入假 popen，不真正结束进程或替换 exe。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import APP_VERSION
from app.services import updater_install
from app.services.updater_install import (
    BACKUP_PREFIX,
    INSTALL_SCRIPT_NAME,
    UPDATE_LOG_NAME,
    can_replace,
    install_update,
    write_install_script,
)
from app.services.updates import UpdateError


def test_install_script_is_ascii_and_contains_required_steps(tmp_path: Path) -> None:
    new_exe = tmp_path / "updates" / "PortHelperFluent-1.0.1.exe"
    target = tmp_path / "app" / "PortHelperFluent.exe"
    work_dir = tmp_path / "updates"
    backup = work_dir / f"{BACKUP_PREFIX}{APP_VERSION}.exe"

    script = write_install_script(new_exe, target, work_dir, pid=4321)

    assert script == work_dir / INSTALL_SCRIPT_NAME
    assert script.exists()
    text = script.read_text(encoding="utf-8-sig")
    # 脚本正文不得含中文：PowerShell 5.1 读取非 ASCII 字符时容易乱码。
    assert text.isascii()
    assert "Wait-Process -Id 4321 -Timeout 60" in text
    # 优先用 .NET 的原子替换，绝不在目标目录产生 .new 之类的旁路文件
    assert f"[System.IO.File]::Replace('{new_exe}', '{target}', [NullString]::Value)" in text
    assert f"Copy-Item -LiteralPath '{new_exe}' -Destination '{target}' -Force" in text
    assert ".new" not in text
    assert "Move-Item" not in text
    assert f"Start-Process -FilePath '{target}'" in text
    # 失败分支：按 AppData 里的备份还原目标并重启旧版
    assert f"$backupExe = '{backup}'" in text
    assert "Test-Path -LiteralPath $backupExe" in text
    assert f"Copy-Item -LiteralPath $backupExe -Destination '{target}' -Force" in text
    assert "Restored the backup exe." in text
    assert str(work_dir / UPDATE_LOG_NAME) in text
    # 顺序必须是：等待旧进程 → 原子替换 → 重启
    positions = [
        text.index("Wait-Process -Id"),
        text.index("[System.IO.File]::Replace("),
        text.index("Start-Process -FilePath"),
    ]
    assert positions == sorted(positions)


def test_install_script_escapes_single_quotes(tmp_path: Path) -> None:
    new_exe = tmp_path / "it's" / "new.exe"
    target = tmp_path / "it's" / "PortHelperFluent.exe"
    work_dir = tmp_path / "updates"

    text = write_install_script(new_exe, target, work_dir, pid=7).read_text(
        encoding="utf-8-sig"
    )

    assert "it''s" in text
    assert text.count("''") >= 2


def test_install_update_backs_up_and_launches_script(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    target = tmp_path / "app" / "PortHelperFluent.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-exe")
    work_dir = tmp_path / "updates"
    work_dir.mkdir()
    new_exe = work_dir / "PortHelperFluent-1.0.1.exe"
    new_exe.write_bytes(b"new-exe")
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return object()

    script = install_update(new_exe, target, work_dir, pid=1234, popen=fake_popen)

    backup = work_dir / f"{BACKUP_PREFIX}{APP_VERSION}.exe"
    assert backup.read_bytes() == b"old-exe"
    assert script == work_dir / INSTALL_SCRIPT_NAME
    assert script.exists()
    command, kwargs = calls[0]
    assert command[0] == "powershell"
    assert "-NoProfile" in command
    assert "-ExecutionPolicy" in command
    assert str(script) in command
    assert kwargs["close_fds"] is True
    assert kwargs["creationflags"] & 0x00000008  # DETACHED_PROCESS
    # 备份、脚本与日志都只写在 work_dir，exe 目录不新增任何文件。
    assert [path.name for path in target.parent.iterdir()] == ["PortHelperFluent.exe"]


def test_install_update_requires_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os, "name", "posix")

    with pytest.raises(UpdateError) as excinfo:
        install_update(tmp_path / "new.exe", tmp_path / "old.exe", tmp_path / "updates")

    assert str(excinfo.value) == "自动安装仅支持 Windows。"
    assert not (tmp_path / "updates").exists()


def test_install_update_rotates_oversized_update_log(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    target = tmp_path / "app" / "PortHelperFluent.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-exe")
    work_dir = tmp_path / "updates"
    work_dir.mkdir()
    new_exe = work_dir / "PortHelperFluent-1.0.1.exe"
    new_exe.write_bytes(b"new-exe")
    log_path = work_dir / UPDATE_LOG_NAME
    log_path.write_bytes(b"x" * (updater_install.UPDATE_LOG_MAX_BYTES + 1))

    install_update(new_exe, target, work_dir, pid=1, popen=lambda command, **kwargs: object())

    assert not log_path.exists()
    assert (work_dir / f"{UPDATE_LOG_NAME}.old").exists()


def test_install_update_keeps_small_update_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    target = tmp_path / "app" / "PortHelperFluent.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-exe")
    work_dir = tmp_path / "updates"
    work_dir.mkdir()
    new_exe = work_dir / "PortHelperFluent-1.0.1.exe"
    new_exe.write_bytes(b"new-exe")
    log_path = work_dir / UPDATE_LOG_NAME
    log_path.write_text("旧的安装记录\n", encoding="utf-8")

    install_update(new_exe, target, work_dir, pid=1, popen=lambda command, **kwargs: object())

    assert log_path.exists()
    assert not (work_dir / f"{UPDATE_LOG_NAME}.old").exists()


def test_can_replace_returns_true_for_writable_directory(tmp_path: Path) -> None:
    target = tmp_path / "PortHelperFluent.exe"
    target.write_bytes(b"exe")

    assert can_replace(target) is True
    # 可写目标直接以 r+b 打开验证，不在目录里留下任何探针文件。
    assert list(tmp_path.glob(".porthelper-write-probe-*")) == []


def test_can_replace_returns_true_when_target_missing(tmp_path: Path) -> None:
    assert can_replace(tmp_path / "PortHelperFluent.exe") is True
    assert list(tmp_path.glob(".porthelper-write-probe-*")) == []


def test_can_replace_falls_back_to_directory_when_exe_is_running(
    tmp_path: Path, monkeypatch
) -> None:
    """运行中的 exe 无法获得写句柄；此时退回目录探针，不能因此判定不可替换。"""
    target = tmp_path / "PortHelperFluent.exe"
    target.write_bytes(b"exe")
    original_open = Path.open

    def fake_open(self, mode="r", *args, **kwargs):
        if mode == "r+b" and self == target:
            raise PermissionError(13, "sharing violation", str(self))
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)

    assert can_replace(target) is True
    # 回退路径会写探针，但必须立即清理
    assert list(tmp_path.glob(".porthelper-write-probe-*")) == []


def test_can_replace_returns_false_when_probe_fails(tmp_path: Path, monkeypatch) -> None:
    def broken_probe(directory):
        raise PermissionError(13, "denied", str(directory))

    monkeypatch.setattr(updater_install, "probe_write", broken_probe)

    assert can_replace(tmp_path / "PortHelperFluent.exe") is False

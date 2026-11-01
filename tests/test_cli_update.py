"""``update`` 命令组：检查、下载与安装的 CLI 契约。

网络、下载与 PowerShell 启动全部被替换成假实现：本文件永不发起真实请求，也永不
真正替换 exe。``check``/``download`` 的版本解析、资产选择与哈希校验已由
``tests/test_updates.py`` 覆盖，这里只验证 CLI 的字段形状、``--yes`` 保护、
源码模式拒绝与退出码。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import APP_VERSION
import app.cli.commands.update as update
from app.services.updates import ReleaseAsset, ReleaseInfo, UpdateError

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CHECK_KEYS = {
    "current",
    "latest",
    "newer",
    "title",
    "notes",
    "page_url",
    "published_at",
    "asset",
}

EXE_URL = (
    "https://github.com/Yaopyr/PortHelperFluent/releases/download/v9.9.9/PortHelperFluent.exe"
)
CHECKSUM_URL = (
    "https://github.com/Yaopyr/PortHelperFluent/releases/download/v9.9.9/SHA256.txt"
)


def _release(version: str = "9.9.9", *, with_exe: bool = True) -> ReleaseInfo:
    """构造一个版本 ``9.9.9`` 的 ReleaseInfo；``with_exe`` 为假时不带 exe 资产。"""
    exe = (
        ReleaseAsset(name="PortHelperFluent.exe", url=EXE_URL, size=2048, digest="")
        if with_exe
        else None
    )
    checksum = ReleaseAsset(name="SHA256.txt", url=CHECKSUM_URL, size=65, digest="")
    return ReleaseInfo(
        version=version,
        tag=f"v{version}",
        title=f"PortHelperFluent v{version}",
        notes="修复若干问题",
        page_url="https://github.com/Yaopyr/PortHelperFluent/releases/tag/v9.9.9",
        published_at="2026-10-31T12:00:00Z",
        exe=exe,
        checksum=checksum,
    )


def _freeze(monkeypatch) -> None:
    """把当前进程伪装成冻结后的 exe：``update install`` 只在冻结模式允许替换。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)


def _fake_download(monkeypatch, work_dirs: list[Path] | None = None) -> None:
    """把 ``download_release`` 换成“写一个假 exe 并返回路径”的假实现。"""

    def fake(release: ReleaseInfo, work_dir: Path) -> Path:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        if work_dirs is not None:
            work_dirs.append(work_dir)
        target = work_dir / f"PortHelperFluent-{release.version}.exe"
        target.write_bytes(b"fake-exe")
        return target

    monkeypatch.setattr(update, "download_release", fake)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_reports_newer(cli, monkeypatch) -> None:
    monkeypatch.setattr(update, "fetch_latest_release", lambda: _release())

    payload = cli(["update", "check"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "update.check"

    data = payload["data"]
    assert set(data) == CHECK_KEYS
    assert data["current"] == APP_VERSION
    assert data["latest"] == "9.9.9"
    assert data["newer"] is True
    assert data["title"] == "PortHelperFluent v9.9.9"
    assert data["notes"] == "修复若干问题"
    assert data["page_url"].endswith("/v9.9.9")
    assert data["published_at"] == "2026-10-31T12:00:00Z"
    assert data["asset"] == {"name": "PortHelperFluent.exe", "size": 2048, "digest": ""}


def test_check_reports_not_newer_and_missing_asset(cli, monkeypatch) -> None:
    monkeypatch.setattr(update, "fetch_latest_release", lambda: _release("1.0.0", with_exe=False))

    payload = cli(["update", "check"])
    assert payload["exit_code"] == 0
    data = payload["data"]
    assert data["newer"] is False
    assert data["asset"] is None


def test_check_maps_update_error(cli, monkeypatch) -> None:
    def fail() -> ReleaseInfo:
        raise UpdateError("仓库还没有发布任何 Release。")

    monkeypatch.setattr(update, "fetch_latest_release", fail)

    payload = cli(["update", "check"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "update_failed"
    assert "Release" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def test_download_returns_path_and_sha(cli, monkeypatch, tmp_path) -> None:
    work_dirs: list[Path] = []
    monkeypatch.setattr(update, "fetch_latest_release", lambda: _release())
    _fake_download(monkeypatch, work_dirs)

    payload = cli(["update", "download"])
    assert payload["exit_code"] == 0
    assert payload["command"] == "update.download"

    data = payload["data"]
    assert set(data) == {"path", "size", "sha256"}
    assert data["path"].endswith("PortHelperFluent-9.9.9.exe")
    assert data["size"] == len(b"fake-exe")
    assert data["sha256"] == hashlib.sha256(b"fake-exe").hexdigest()
    # 下载目录必须是 paths 的 updates 目录，而不是任意临时位置。
    assert len(work_dirs) == 1 and work_dirs[0].name == "updates"
    assert Path(data["path"]).parent == work_dirs[0]


def test_download_maps_update_error(cli, monkeypatch) -> None:
    monkeypatch.setattr(update, "fetch_latest_release", lambda: _release())

    def fail(release: ReleaseInfo, work_dir: Path) -> Path:
        raise UpdateError("下载文件校验失败（SHA-256 不匹配），已删除。")

    monkeypatch.setattr(update, "download_release", fail)

    payload = cli(["update", "download"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "update_failed"


# ---------------------------------------------------------------------------
# install：参数门槛
# ---------------------------------------------------------------------------


def test_install_requires_yes_and_exactly_one_source(cli, tmp_path) -> None:
    exe = tmp_path / "PortHelperFluent-9.9.9.exe"
    exe.write_bytes(b"fake-exe")
    digest = hashlib.sha256(b"fake-exe").hexdigest()

    for argv in (
        ["update", "install"],
        ["update", "install", "--latest"],
        ["update", "install", "--latest", "--file", str(exe), "--sha256", digest],
        ["update", "install", "--yes", "--file", str(exe)],  # --file 必须配 --sha256
    ):
        payload = cli(argv)
        assert payload["exit_code"] == 2, argv
        assert payload["error"]["code"] == "usage", argv


def test_install_requires_sha256_with_file(cli, tmp_path) -> None:
    exe = tmp_path / "PortHelperFluent-9.9.9.exe"
    exe.write_bytes(b"fake-exe")

    payload = cli(["update", "install", "--yes", "--file", str(exe)])
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "usage"


# ---------------------------------------------------------------------------
# install：源码模式与文件校验
# ---------------------------------------------------------------------------


def test_install_rejects_source_mode(cli, monkeypatch, tmp_path) -> None:
    called: list[object] = []
    monkeypatch.setattr(update, "install_update", lambda *a, **k: called.append(a))
    monkeypatch.setattr(update, "fetch_latest_release", lambda: pytest.fail("--file 不应联网"))

    exe = tmp_path / "PortHelperFluent-9.9.9.exe"
    exe.write_bytes(b"fake-exe")
    digest = hashlib.sha256(b"fake-exe").hexdigest()

    payload = cli(["update", "install", "--yes", "--file", str(exe), "--sha256", digest])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "unsupported"
    assert "源码运行只下载，不替换 exe" in payload["error"]["message"]
    assert called == []


def test_install_file_must_exist(cli, monkeypatch, tmp_path) -> None:
    _freeze(monkeypatch)

    missing = tmp_path / "missing.exe"
    payload = cli(["update", "install", "--yes", "--file", str(missing), "--sha256", "0" * 64])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "not_found"


def test_install_file_sha_mismatch(cli, monkeypatch, tmp_path) -> None:
    _freeze(monkeypatch)
    calls: list[object] = []
    monkeypatch.setattr(update, "install_update", lambda *a, **k: calls.append(a))

    exe = tmp_path / "PortHelperFluent-9.9.9.exe"
    exe.write_bytes(b"fake-exe")

    payload = cli(["update", "install", "--yes", "--file", str(exe), "--sha256", "1" * 64])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "update_failed"
    assert calls == []


# ---------------------------------------------------------------------------
# install：成功路径（不真的启动 PowerShell）
# ---------------------------------------------------------------------------


def test_install_file_runs_install_update(cli, monkeypatch, tmp_path) -> None:
    _freeze(monkeypatch)
    monkeypatch.setattr(update, "fetch_latest_release", lambda: pytest.fail("--file 不应联网"))

    exe = tmp_path / "PortHelperFluent-9.9.9.exe"
    exe.write_bytes(b"fake-exe")
    digest = hashlib.sha256(b"fake-exe").hexdigest()

    calls: list[tuple[Path, Path, Path, int | None]] = []

    def fake_install(new_exe, target, work_dir, *, pid=None) -> Path:
        calls.append((Path(new_exe), Path(target), Path(work_dir), pid))
        return Path(work_dir) / "apply-update.ps1"

    monkeypatch.setattr(update, "install_update", fake_install)

    payload = cli(["update", "install", "--yes", "--file", str(exe), "--sha256", digest])
    assert payload["exit_code"] == 0
    assert payload["command"] == "update.install"
    assert payload["data"] == {"script": str(calls[0][2] / "apply-update.ps1")}

    assert len(calls) == 1
    new_exe, target, work_dir, pid = calls[0]
    assert new_exe == exe
    assert target == Path(sys.executable)
    assert work_dir.name == "updates" and work_dir.is_dir()
    assert pid == os.getpid()


def test_install_latest_downloads_then_installs(cli, monkeypatch) -> None:
    _freeze(monkeypatch)
    work_dirs: list[Path] = []
    downloaded: list[Path] = []
    monkeypatch.setattr(update, "fetch_latest_release", lambda: _release())
    _fake_download(monkeypatch, work_dirs)

    def fake_install(new_exe, target, work_dir, *, pid=None) -> Path:
        downloaded.append(Path(new_exe))
        return Path(work_dir) / "apply-update.ps1"

    monkeypatch.setattr(update, "install_update", fake_install)

    payload = cli(["update", "install", "--yes", "--latest"])
    assert payload["exit_code"] == 0
    assert payload["data"] == {"script": str(work_dirs[0] / "apply-update.ps1")}
    # --latest 先下载到 updates 目录，再把同一个文件交给替换流程。
    assert downloaded == [work_dirs[0] / "PortHelperFluent-9.9.9.exe"]


def test_install_latest_maps_update_error(cli, monkeypatch) -> None:
    _freeze(monkeypatch)

    def fail() -> ReleaseInfo:
        raise UpdateError("无法连接 GitHub，请检查网络或代理设置。")

    monkeypatch.setattr(update, "fetch_latest_release", fail)

    payload = cli(["update", "install", "--yes", "--latest"])
    assert payload["exit_code"] == 3
    assert payload["error"]["code"] == "update_failed"


# ---------------------------------------------------------------------------
# 命令组与导入开销
# ---------------------------------------------------------------------------


def test_missing_arguments_are_usage_errors(cli) -> None:
    for argv in (["update"], ["update", "no-such-action"]):
        payload = cli(argv)
        assert payload["exit_code"] == 2, argv
        assert payload["error"]["code"] == "usage", argv


def test_output_options_are_accepted(cli, monkeypatch) -> None:
    monkeypatch.setattr(update, "fetch_latest_release", lambda: _release())

    text = cli(["update", "check", "--format", "text"])
    assert text["exit_code"] == 0
    assert "ok: true" in text["raw"] and "update.check" in text["raw"]

    pretty = cli(["update", "check", "--pretty"])
    assert pretty["ok"] is True and pretty["command"] == "update.check"


def test_update_module_import_stays_light() -> None:
    """更新命令只依赖标准库与无头服务模块：导入时不能连带加载 PyQt6。"""
    code = (
        "import sys\n"
        "import app.cli.commands.update\n"
        "print(sorted(name for name in ('PyQt6', 'qfluentwidgets') if name in sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"

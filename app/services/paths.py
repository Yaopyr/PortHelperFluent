"""应用数据、配置与日志路径的解析。

非便携模式下所有可写数据都放在 `%APPDATA%\\PortHelperFluent`，绝不写入程序
所在目录（通常是 `Program Files`）；只有显式放置 `portable.flag` 时才启用便携模式。
`ensure_dirs` 只负责“创建目录或抛错”；便携目录创建或写入失败时的 AppData 回退
集中在 `ensure_writable_paths`，并由它返回中文提示。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "PortHelperFluent"
PORTABLE_FLAG = "portable.flag"


@dataclass(frozen=True)
class AppPaths:
    exe_dir: Path
    data_dir: Path
    config_file: Path
    log_dir: Path
    portable: bool


def _default_exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # paths.py 位于 <repo>/app/services/，向上三级即仓库根目录。
    return Path(__file__).resolve().parents[2]


def _default_appdata() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata)
    return Path.home() / "AppData" / "Roaming"


def _appdata_paths(exe_dir: Path, appdata: Path | None) -> AppPaths:
    base_dir = Path(appdata) if appdata is not None else _default_appdata()
    data_dir = base_dir / APP_NAME
    return AppPaths(
        exe_dir=exe_dir,
        data_dir=data_dir,
        config_file=data_dir / "config.json",
        log_dir=data_dir / "logs",
        portable=False,
    )


def resolve_paths(exe_path: Path | None = None, appdata: Path | None = None) -> AppPaths:
    """解析应用路径。`exe_path` 与 `appdata` 仅供测试注入。"""
    exe_dir = Path(exe_path) if exe_path is not None else _default_exe_dir()
    if (exe_dir / PORTABLE_FLAG).exists():
        data_dir = exe_dir / "data"
        return AppPaths(
            exe_dir=exe_dir,
            data_dir=data_dir,
            config_file=data_dir / "config.json",
            log_dir=data_dir / "logs",
            portable=True,
        )
    return _appdata_paths(exe_dir, appdata)


def ensure_dirs(paths: AppPaths) -> None:
    """创建数据目录与日志目录；失败时原样抛出 `OSError`，不做任何回退。"""
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)


def ensure_writable_paths(
    paths: AppPaths, appdata: Path | None = None
) -> tuple[AppPaths, str | None]:
    """创建并验证数据目录与日志目录，便携模式不可写时回退到 AppData。

    返回实际生效的 `AppPaths` 与中文回退提示（未回退时为 None）。
    回退目录同样要创建并实际写一次探针文件；便携目录与 AppData 都不可写时抛出
    中文 `OSError`，绝不返回一个稍后才失败的路径。
    非便携模式下 AppData 本身就不可写，原始异常原样抛出，不做无意义的二次尝试。
    """
    try:
        ensure_dirs(paths)
        _probe_write(paths.data_dir)
        _probe_write(paths.log_dir)
    except OSError as exc:
        if not paths.portable:
            raise
        fallback = _appdata_paths(paths.exe_dir, appdata)
        try:
            ensure_dirs(fallback)
            _probe_write(fallback.data_dir)
            _probe_write(fallback.log_dir)
        except OSError as fallback_exc:
            raise OSError(
                "便携模式数据目录与 AppData 回退目录都不可写："
                f"{paths.data_dir}（{exc}）；{fallback.data_dir}（{fallback_exc}）。"
                "请检查目录权限，或改用有写入权限的账户运行。"
            ) from fallback_exc
        warning = (
            f"便携模式数据目录不可写（{exc}），已回退到 AppData：{fallback.data_dir}。"
            "配置文件与会话日志将保存在该目录。"
        )
        return fallback, warning
    return paths, None


def _probe_write(directory: Path) -> None:
    """在目录内写入一个带 PID 的临时探针文件并删除，确认目录真实可写。

    仅靠 `mkdir(exist_ok=True)` 无法发现“目录已存在但只读”的情况，因此必须实际
    写一次文件。无论成功与否都在 `finally` 中清理，不留下残留文件。
    """
    probe = directory / f".porthelper-write-probe-{os.getpid()}.probe"
    try:
        with probe.open("wb") as handle:
            handle.write(b"0")
    finally:
        probe.unlink(missing_ok=True)

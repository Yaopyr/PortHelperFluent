"""应用数据、配置与日志路径的解析。

非便携模式下所有可写数据都放在 `%APPDATA%\\PortHelperFluent`，绝不写入程序
所在目录（通常是 `Program Files`）；只有显式放置 `portable.flag` 时才启用便携模式。
便携目录创建失败时回退到 AppData，并通过 `ensure_writable_paths` 返回中文提示。
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


def ensure_writable_paths(
    paths: AppPaths, appdata: Path | None = None
) -> tuple[AppPaths, str | None]:
    """创建数据目录与日志目录，便携模式失败时回退到 AppData。

    返回实际生效的 `AppPaths` 与中文回退提示（未回退时为 None）。
    非便携模式下 AppData 本身就不可写，异常会原样抛出，不做无意义的二次尝试。
    """
    try:
        _mkdirs(paths)
    except OSError as exc:
        if not paths.portable:
            raise
        fallback = _appdata_paths(paths.exe_dir, appdata)
        _mkdirs(fallback)  # AppData 也不可写时让异常向上抛出，由启动失败流程处理
        warning = (
            f"便携模式数据目录不可写（{exc}），已回退到 AppData：{fallback.data_dir}。"
            "配置文件与会话日志将保存在该目录。"
        )
        return fallback, warning
    return paths, None


def ensure_dirs(paths: AppPaths, appdata: Path | None = None) -> AppPaths:
    """兼容入口：确保目录存在并返回实际生效的路径；需要回退提示时请用 `ensure_writable_paths`。"""
    effective, _warning = ensure_writable_paths(paths, appdata=appdata)
    return effective


def _mkdirs(paths: AppPaths) -> None:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)

"""应用数据、配置与日志路径的解析。

非便携模式下所有可写数据都放在 `%APPDATA%\\PortHelperFluent`，绝不写入程序
所在目录（通常是 `Program Files`）；只有显式放置 `portable.flag` 时才启用便携模式。
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


def resolve_paths(exe_path: Path | None = None, appdata: Path | None = None) -> AppPaths:
    """解析应用路径。`exe_path` 与 `appdata` 仅供测试注入。"""
    exe_dir = Path(exe_path) if exe_path is not None else _default_exe_dir()
    portable = (exe_dir / PORTABLE_FLAG).exists()
    if portable:
        data_dir = exe_dir / "data"
    else:
        base_dir = Path(appdata) if appdata is not None else _default_appdata()
        data_dir = base_dir / APP_NAME
    return AppPaths(
        exe_dir=exe_dir,
        data_dir=data_dir,
        config_file=data_dir / "config.json",
        log_dir=data_dir / "logs",
        portable=portable,
    )


def ensure_dirs(paths: AppPaths) -> None:
    """创建数据目录与日志目录，已存在时不做任何事。"""
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)

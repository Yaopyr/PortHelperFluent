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

from app.core.logging_setup import LOG_FILE_NAME

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
    """创建并验证数据目录、日志目录以及已有配置文件/日志文件，便携模式不可写时回退。

    返回实际生效的 `AppPaths` 与中文回退提示（未回退时为 None）。
    除了目录探针，还会打开已有的 `logs/app.log` 与 `config.json` 验证文件级写入
    权限——目录可写但文件只读/被占用同样会让日志与配置在稍后失败，必须提前发现。
    便携模式遇到任何写失败都回退 AppData；AppData 模式下的文件级失败抛出中文
    `OSError`，绝不返回一个稍后才失败的路径。非便携模式的目录创建失败保持原样
    抛出，不做无意义的二次尝试。
    """
    try:
        ensure_dirs(paths)
        probe_write(paths.data_dir)
        probe_write(paths.log_dir)
        _probe_existing_files(paths)
    except OSError as exc:
        if not paths.portable:
            raise
        fallback = _appdata_paths(paths.exe_dir, appdata)
        try:
            ensure_dirs(fallback)
            probe_write(fallback.data_dir)
            probe_write(fallback.log_dir)
            _probe_existing_files(fallback)
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


def probe_write(directory: Path) -> None:
    """在目录内写入一个带 PID 的临时探针文件并删除，确认目录真实可写。

    仅靠 `mkdir(exist_ok=True)` 无法发现“目录已存在但只读”的情况，因此必须实际
    写一次文件。无论成功与否都在 `finally` 中清理，不留下残留文件。该探针也供
    `main.run_selftest` 复用，避免自检另写一份容易泄漏临时文件的实现。
    """
    probe = directory / f".porthelper-write-probe-{os.getpid()}.probe"
    try:
        with probe.open("wb") as handle:
            handle.write(b"0")
    finally:
        probe.unlink(missing_ok=True)


def _probe_existing_files(paths: AppPaths) -> None:
    """打开已有 `logs/app.log` 与 `config.json` 验证文件级写权限，不写入任何内容。

    只读或被占用的文件在 Windows 上会让 `open(..., "ab")` / `open(..., "r+b")`
    抛 `PermissionError`，正好暴露“目录可写但文件不可写”的情况。文件不存在时跳过
    （目录探针已经证明可以新建），避免留下空文件。失败时抛出带中文说明的
    `OSError`，由调用方决定回退还是上抛。
    """
    for target, mode, label in (
        (paths.log_dir / LOG_FILE_NAME, "ab", "日志文件"),
        (paths.config_file, "r+b", "配置文件"),
    ):
        if not target.exists():
            continue
        try:
            with target.open(mode):
                # 只打开并立即关闭：追加/更新模式不会截断或改写已有内容。
                pass
        except OSError as exc:
            raise PermissionError(
                13,
                f"{label}不可写：{target}。"
                "请取消文件的只读属性、关闭占用它的程序，"
                "或把数据目录改到有写入权限的位置。",
                str(target),
            ) from exc

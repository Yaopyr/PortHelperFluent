"""``update`` 命令组：检查更新、下载校验、替换并重启 exe。

三个子命令复用 :mod:`app.services.updates` 与 :mod:`app.services.updater_install`，
本模块只负责参数门槛、错误码映射与 JSON 字段：

- ``check``：只读，返回当前/最新版本与是否更新，附 Release 标题、说明与资产信息；
- ``download``：把 exe 下载到 ``data_dir/updates`` 并返回路径、大小与 SHA-256；
- ``install``：破坏性操作，必须 ``--yes``；``--file`` 不联网、只校验已下载的文件，
  ``--latest`` 先检查并下载；源码运行（无 ``sys.frozen``）拒绝替换，只提示下载。

测试与 Agent 调用都不应真的启动 PowerShell：替换脚本由 ``install_update`` 生成，
本模块只把脚本路径原样返回。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from app import APP_VERSION
from app.cli.errors import EXIT_OPERATION, EXIT_USAGE, CliError
from app.cli.output import add_output_options
from app.services.paths import AppPaths, ensure_writable_paths, resolve_paths
from app.services.updater_install import install_update
from app.services.updates import (
    ReleaseInfo,
    UpdateError,
    download_release,
    fetch_latest_release,
    is_newer,
    sha256_file,
    updates_dir,
    verify_sha256,
)

__all__ = ["register", "run_check", "run_download", "run_install"]


def register(subparsers) -> None:
    """注册 ``update`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "update",
        help="检查、下载并安装 GitHub Releases 上的新版本。",
        description="在线更新；可用子命令：check、download、install。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    _register_check(group)
    _register_download(group)
    _register_install(group)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _register_check(subparsers) -> None:
    parser = subparsers.add_parser(
        "check",
        help="查看最新 Release 以及是否有新版本。",
        description="访问 GitHub 最新 Release，返回当前版本、最新版本、是否更新与发布信息。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_check, command="update.check")


def run_check(args) -> dict[str, Any]:
    """返回 ``current/latest/newer`` 与 Release 展示信息；``asset`` 无 exe 时为 null。"""
    release = _fetch_release()
    asset = None
    if release.exe is not None:
        asset = {
            "name": release.exe.name,
            "size": release.exe.size,
            "digest": release.exe.digest,
        }
    return {
        "current": APP_VERSION,
        "latest": release.version,
        "newer": is_newer(release.version, APP_VERSION),
        "title": release.title,
        "notes": release.notes,
        "page_url": release.page_url,
        "published_at": release.published_at,
        "asset": asset,
    }


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def _register_download(subparsers) -> None:
    parser = subparsers.add_parser(
        "download",
        help="下载最新 exe 到数据目录并校验 SHA-256。",
        description="下载最新 Release 的 exe 到 data_dir/updates，返回路径、大小与 SHA-256；"
        "不替换任何文件。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_download, command="update.download")


def run_download(args) -> dict[str, Any]:
    """下载并校验后返回 ``path/size/sha256``；失败统一按 ``update_failed``（3）。"""
    paths = _writable_paths()
    work_dir = _updates_dir(paths)
    release = _fetch_release()
    try:
        target = Path(download_release(release, work_dir))
        size = target.stat().st_size
        digest = sha256_file(target)
    except UpdateError as exc:
        raise CliError("update_failed", str(exc), EXIT_OPERATION) from exc
    except OSError as exc:
        raise CliError(
            "update_failed",
            f"读取下载的更新文件失败：{exc}。请检查数据目录权限。",
            EXIT_OPERATION,
        ) from exc
    return {"path": str(target), "size": size, "sha256": digest}


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def _register_install(subparsers) -> None:
    parser = subparsers.add_parser(
        "install",
        help="用新版本替换当前 exe 并重启（破坏性，必须 --yes）。",
        description="替换当前 exe 并重启 GUI；必须显式加 --yes，且只在冻结后的 exe 中可用。",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认替换当前 exe：命令返回后由 PowerShell 脚本替换并重启。",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--latest",
        action="store_true",
        help="先检查并下载最新版本，再安装。",
    )
    source.add_argument(
        "--file",
        metavar="PATH",
        help="已下载的 exe 路径；必须配合 --sha256，不联网。",
    )
    parser.add_argument(
        "--sha256",
        metavar="HEX",
        help="--file 的期望 SHA-256（取自 update download 的 data.sha256）。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_install, command="update.install")


def run_install(args) -> dict[str, Any]:
    """校验参数与文件后生成替换脚本，返回 ``script``。

    ``--yes`` 与来源二选一是参数门槛；源码运行只下载不替换，因此先于文件读取
    按 ``unsupported`` 拒绝，避免下载或校验做了也白做。
    """
    if not getattr(args, "yes", False):
        raise CliError(
            "usage",
            "update install 会替换当前 exe 并重启，请显式加 --yes 确认。",
            EXIT_USAGE,
        )
    use_latest = bool(getattr(args, "latest", False))
    file_arg = getattr(args, "file", None)
    sha256_arg = str(getattr(args, "sha256", None) or "").strip()
    if not use_latest and file_arg is None:
        raise CliError(
            "usage",
            "update install 需要 --latest 或 --file PATH --sha256 HEX。",
            EXIT_USAGE,
        )
    if file_arg is not None and not sha256_arg:
        raise CliError(
            "usage",
            "--file 必须同时提供 --sha256（update download 会在 data.sha256 返回该值）。",
            EXIT_USAGE,
        )
    if not getattr(sys, "frozen", False):
        raise CliError("unsupported", "源码运行只下载，不替换 exe", EXIT_OPERATION)

    paths = _writable_paths()
    work_dir = _updates_dir(paths)
    if use_latest:
        new_exe = _download_latest(work_dir)
    else:
        new_exe = _verified_file(Path(str(file_arg)), sha256_arg)
    try:
        script = install_update(new_exe, Path(sys.executable), work_dir, pid=os.getpid())
    except UpdateError as exc:
        raise CliError("update_failed", str(exc), EXIT_OPERATION) from exc
    return {"script": str(script)}


def _download_latest(work_dir: Path) -> Path:
    """``--latest``：检查并下载最新 exe，返回本地路径。"""
    release = _fetch_release()
    try:
        return Path(download_release(release, work_dir))
    except UpdateError as exc:
        raise CliError("update_failed", str(exc), EXIT_OPERATION) from exc


def _verified_file(path: Path, expected: str) -> Path:
    """``--file``：文件必须存在且 SHA-256 匹配，否则不交给替换流程。"""
    if not path.is_file():
        raise CliError("not_found", f"更新文件不存在：{path}。", EXIT_OPERATION)
    if not verify_sha256(path, expected):
        raise CliError(
            "update_failed",
            f"更新文件校验失败：{path} 与 --sha256 不匹配，已取消安装。",
            EXIT_OPERATION,
        )
    return path


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------


def _fetch_release() -> ReleaseInfo:
    """读取最新 Release；``UpdateError`` 统一转 ``update_failed``（3）。"""
    try:
        return fetch_latest_release()
    except UpdateError as exc:
        raise CliError("update_failed", str(exc), EXIT_OPERATION) from exc


def _writable_paths() -> AppPaths:
    """解析并确认数据目录可写（便携模式不可写时自动回退 AppData）。"""
    paths = resolve_paths()
    try:
        paths, _ = ensure_writable_paths(paths)
    except OSError as exc:
        raise CliError(
            "permission_denied",
            f"数据目录不可写：{paths.data_dir}（{exc}）。请检查目录权限。",
            EXIT_OPERATION,
        ) from exc
    return paths


def _updates_dir(paths: AppPaths) -> Path:
    """``data_dir/updates``；创建失败按 ``permission_denied``（3）上报。"""
    try:
        return updates_dir(paths)
    except OSError as exc:
        raise CliError(
            "permission_denied",
            f"创建更新目录失败：{paths.data_dir}（{exc}）。请检查目录权限。",
            EXIT_OPERATION,
        ) from exc

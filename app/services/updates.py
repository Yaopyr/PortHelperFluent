"""GitHub Releases 在线更新：版本解析、资产选择、下载与 SHA-256 校验。

只用标准库 `urllib.request`，不引入第三方 HTTP 客户端；所有网络/格式错误都收敛为
中文 `UpdateError`，界面可以直接展示 `str(exc)`。下载安全策略：仅 https、主机必须
在 `ALLOWED_HOSTS`（或其子域）内、重定向后的最终地址再校验一次、先写 `.part` 再
`os.replace`，取消或异常都会清理临时文件。本模块不依赖 Qt，测试可直接注入假 opener。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app import APP_VERSION
from app.core.logging_setup import get_logger

REPO_SLUG = "Yaopyr/PortHelperFluent"
RELEASE_API_URL = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
RELEASE_PAGE_URL = f"https://github.com/{REPO_SLUG}/releases/latest"
EXE_ASSET_NAME = "PortHelperFluent.exe"
CHECKSUM_ASSET_NAME = "SHA256.txt"
REQUEST_TIMEOUT = 10.0
CHUNK_SIZE = 64 * 1024
ALLOWED_HOSTS = (
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
)
USER_AGENT = f"PortHelperFluent/{APP_VERSION}"
GITHUB_ACCEPT = "application/vnd.github+json"
UPDATES_DIR_NAME = "updates"
PRUNE_MAX_AGE_SECONDS = 24 * 3600
# 与 updater_install.INSTALL_SCRIPT_NAME / UPDATE_LOG_NAME 一致；不反向导入可避免循环依赖。
_PRUNE_PATTERNS = ("PortHelperFluent-*.exe", "*.part", "apply-update.ps1", "update.log.old")
_VERSION_CORE = re.compile(r"[-+\s]")
_NUMERIC_PREFIX = re.compile(r"^\d+")
_SHA256_PATTERN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_DIGEST_PREFIX = "sha256:"

ProgressCallback = Callable[[int, int], None]
CancelCallback = Callable[[], bool]


class UpdateError(Exception):
    """更新流程中的可展示错误；`str(exc)` 即为中文提示。"""


@dataclass(frozen=True)
class ReleaseAsset:
    """Release 中的一个资产文件。"""

    name: str
    url: str
    size: int
    digest: str


@dataclass(frozen=True)
class ReleaseInfo:
    """一个 Release 的展示信息与已选好的资产。"""

    version: str
    tag: str
    title: str
    notes: str
    page_url: str
    published_at: str
    exe: ReleaseAsset | None
    checksum: ReleaseAsset | None


def parse_version(text: str) -> tuple[int, ...]:
    """把 `v1.0.1`、`1.0.1-beta` 之类的版本串解析成数字元组。

    去掉前导 `v`，丢弃 `-`/`+`/空白之后的后缀，逐段取数字前缀；任一段没有数字
    就停止。空串与乱码返回空元组，由调用方自行决定回退。
    """
    raw = (text or "").strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:].strip()
    core = _VERSION_CORE.split(raw, maxsplit=1)[0]
    parts: list[int] = []
    for segment in core.split("."):
        match = _NUMERIC_PREFIX.match(segment)
        if match is None:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def _normalized(parts: tuple[int, ...]) -> tuple[int, ...]:
    """去掉尾部 0，使 `1.0` 与 `1.0.0` 视为同一版本。"""
    end = len(parts)
    while end and parts[end - 1] == 0:
        end -= 1
    return parts[:end]


def is_newer(remote: str, current: str = APP_VERSION) -> bool:
    """远端版本是否比当前版本新；任一版本解析失败时返回 False。"""
    remote_parts = parse_version(remote)
    current_parts = parse_version(current)
    if not remote_parts or not current_parts:
        return False
    return _normalized(remote_parts) > _normalized(current_parts)


def select_assets(
    assets: list[dict],
) -> tuple[ReleaseAsset | None, ReleaseAsset | None]:
    """从 API 的 assets 列表选出 exe 与校验文件。

    exe 先精确匹配 `EXE_ASSET_NAME`（忽略大小写），否则退回第一个 `.exe`；
    校验文件匹配 `SHA256.txt`。`digest` 原样保留（可能为空）。
    """
    exe: ReleaseAsset | None = None
    fallback_exe: ReleaseAsset | None = None
    checksum: ReleaseAsset | None = None
    for item in assets or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        asset = ReleaseAsset(
            name=name,
            url=str(item.get("browser_download_url") or ""),
            size=_as_int(item.get("size")),
            digest=str(item.get("digest") or ""),
        )
        lowered = name.lower()
        if lowered == EXE_ASSET_NAME.lower():
            if exe is None:
                exe = asset
        elif lowered == CHECKSUM_ASSET_NAME.lower():
            if checksum is None:
                checksum = asset
        elif lowered.endswith(".exe") and fallback_exe is None:
            fallback_exe = asset
    return (exe or fallback_exe, checksum)


def fetch_latest_release(
    opener: Callable[..., object] | None = None,
    *,
    timeout: float = REQUEST_TIMEOUT,
) -> ReleaseInfo:
    """读取 GitHub 最新的正式 Release；任何失败都抛中文 `UpdateError`。"""
    request = urllib.request.Request(
        RELEASE_API_URL,
        headers={"User-Agent": USER_AGENT, "Accept": GITHUB_ACCEPT},
    )
    response = _open_url(
        request,
        opener,
        action="访问 GitHub",
        timeout=timeout,
        missing_message="仓库还没有发布任何 Release。",
    )
    try:
        payload = response.read()  # type: ignore[attr-defined]
    except Exception as exc:
        raise UpdateError(f"读取 GitHub 返回的数据失败：{exc}。") from exc
    finally:
        _close_response(response)
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, AttributeError) as exc:
        raise UpdateError("GitHub 返回的数据无法解析，请稍后重试。") from exc
    if not isinstance(data, dict):
        raise UpdateError("GitHub 返回的数据格式不正确。")
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        raise UpdateError("GitHub 返回的数据格式不正确（缺少 tag_name）。")
    exe, checksum = select_assets(data.get("assets") or [])
    parsed = parse_version(tag)
    version = ".".join(str(part) for part in parsed) if parsed else tag.lstrip("vV")
    return ReleaseInfo(
        version=version,
        tag=tag,
        title=str(data.get("name") or tag),
        notes=str(data.get("body") or ""),
        page_url=str(data.get("html_url") or RELEASE_PAGE_URL),
        published_at=str(data.get("published_at") or ""),
        exe=exe,
        checksum=checksum,
    )


def download_asset(
    url: str,
    dest: Path,
    *,
    opener: Callable[..., object] | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
    allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS,
) -> Path:
    """把 `url` 下载到 `dest`；先写 `.part`，成功后原子替换。

    仅接受 https 且主机在白名单内（重定向后的最终地址再校验一次）。`cancel`
    返回真时抛 `UpdateError("下载已取消。")`；任何异常都会删除 `.part`。
    `progress(received, total)` 每读完一块调用一次，`total` 未知时为 0。
    """
    dest = Path(dest)
    _check_url(url, allowed_hosts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    try:
        response = _open_url(
            request, opener, action="下载更新", timeout=REQUEST_TIMEOUT
        )
        try:
            final_url = str(getattr(response, "geturl", lambda: url)())
            # 重定向可能把请求带去任意主机，最终地址必须重新校验。
            _check_url(final_url or url, allowed_hosts)
            total = _content_length(response)
            received = 0
            with part.open("wb") as handle:
                while True:
                    if cancel is not None and cancel():
                        raise UpdateError("下载已取消。")
                    chunk = response.read(CHUNK_SIZE)  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    if progress is not None:
                        progress(received, total)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            _close_response(response)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return dest


def parse_checksum(text: str) -> str | None:
    """从 `SHA256.txt` 之类的文本中取出第一段 64 位十六进制哈希（小写）。"""
    match = _SHA256_PATTERN.search(text or "")
    return match.group().lower() if match is not None else None


def sha256_file(path: Path) -> str:
    """分块计算文件 SHA-256，返回小写十六进制字符串。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> bool:
    """校验文件哈希；期望值不是 64 位十六进制时直接返回 False。"""
    target = (expected or "").strip().lower()
    if target.startswith(_DIGEST_PREFIX):
        target = target[len(_DIGEST_PREFIX) :].strip()
    if not re.fullmatch(r"[0-9a-f]{64}", target):
        return False
    try:
        return sha256_file(path) == target
    except OSError:
        return False


def updates_dir(paths) -> Path:
    """更新文件目录：`data_dir/updates`（非便携即 `%APPDATA%\\PortHelperFluent`）。"""
    directory = Path(paths.data_dir) / UPDATES_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def prune_updates(
    work_dir: Path, *, max_age_seconds: int = PRUNE_MAX_AGE_SECONDS
) -> None:
    """删除更新目录中过期的下载包、残留 `.part`、安装脚本与旧日志。

    备份文件 `backup-PortHelperFluent-*.exe` 是回滚依据，永不删除。
    """
    work_dir = Path(work_dir)
    if not work_dir.is_dir():
        return
    cutoff = time.time() - max(max_age_seconds, 0)
    for pattern in _PRUNE_PATTERNS:
        for path in work_dir.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                get_logger().warning("清理旧的更新文件失败：%s", path, exc_info=True)


def download_release(
    release: ReleaseInfo,
    work_dir: Path,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelCallback | None = None,
    downloader: Callable[..., Path] = download_asset,
) -> Path:
    """下载 Release 的 exe 并校验 SHA-256，返回本地路径。

    目标文件名固定为 `PortHelperFluent-{version}.exe`（不采信远端文件名）。
    校验值优先取 API 的 `assets[].digest`，否则下载 `SHA256.txt` 解析；两者都
    没有则拒绝安装。哈希不符时删除下载文件。缺少 exe 资产同样直接拒绝。
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    asset = release.exe
    if asset is None or not asset.url:
        raise UpdateError("发布中找不到可下载的 exe 文件，已取消更新。")
    if _asset_digest(asset) is None and (release.checksum is None or not release.checksum.url):
        # 没有校验依据时连下载都不做，避免留下无法验证的文件。
        raise UpdateError("发布缺少 SHA-256 校验信息，已取消更新。")
    target = work_dir / f"PortHelperFluent-{release.version}.exe"
    try:
        downloader(asset.url, target, progress=progress, cancel=cancel)
    except UpdateError:
        raise
    except OSError as exc:
        raise UpdateError(f"保存更新文件失败：{exc}。") from exc
    expected = _asset_digest(asset)
    if expected is None and release.checksum is not None and release.checksum.url:
        expected = _checksum_from_asset(
            release.checksum, work_dir, downloader, progress, cancel
        )
    if expected is None:
        target.unlink(missing_ok=True)
        raise UpdateError("发布缺少 SHA-256 校验信息，已取消更新。")
    if not verify_sha256(target, expected):
        target.unlink(missing_ok=True)
        raise UpdateError("下载文件校验失败（SHA-256 不匹配），已删除。")
    return target


# ---- 内部辅助 ---------------------------------------------------------------


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """主机必须与白名单完全一致，或是白名单主机的子域（如 `*.githubusercontent.com`）。"""
    lowered = host.strip().strip(".").lower()
    if not lowered:
        return False
    return any(
        lowered == allowed or lowered.endswith(f".{allowed}") for allowed in allowed_hosts
    )


def _check_url(url: str, allowed_hosts: tuple[str, ...]) -> None:
    """校验下载地址：仅 https，且主机在白名单内。"""
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
    except ValueError as exc:
        raise UpdateError("更新下载地址无法解析，已取消更新。") from exc
    if parsed.scheme.lower() != "https":
        raise UpdateError("更新下载地址必须使用 https。")
    if not _host_allowed(parsed.hostname or "", allowed_hosts):
        raise UpdateError(
            f"更新下载地址不在允许的主机列表中：{parsed.hostname or url}。"
        )


def _open_url(
    request,
    opener,
    *,
    action: str,
    timeout: float,
    missing_message: str | None = None,
):
    """打开 URL，把 urllib 的异常翻译成中文 `UpdateError`。"""
    open_url = opener if opener is not None else urllib.request.urlopen
    try:
        return open_url(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and missing_message is not None:
            raise UpdateError(missing_message) from exc
        if exc.code in (403, 429):
            raise UpdateError(
                f"GitHub 接口访问受限（HTTP {exc.code}），可能请求过于频繁，请稍后再试。"
            ) from exc
        raise UpdateError(f"{action}失败（HTTP {exc.code}），请稍后重试。") from exc
    except urllib.error.URLError as exc:
        raise UpdateError("无法连接 GitHub，请检查网络或代理设置。") from exc
    except OSError as exc:
        raise UpdateError(f"{action}失败：{exc}。请检查网络或代理设置。") from exc


def _close_response(response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _content_length(response) -> int:
    """读取响应头里的总长度；缺失或非法时返回 0（表示未知）。"""
    headers = getattr(response, "headers", None)
    if headers is None:
        return 0
    try:
        value = headers.get("Content-Length")
    except Exception:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _asset_digest(asset: ReleaseAsset) -> str | None:
    """从资产的 `digest` 字段取出 SHA-256；没有或格式非法时返回 None。"""
    raw = (asset.digest or "").strip()
    if raw.lower().startswith(_DIGEST_PREFIX):
        raw = raw[len(_DIGEST_PREFIX) :].strip()
    return raw.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", raw) else None


def _checksum_from_asset(
    asset: ReleaseAsset,
    work_dir: Path,
    downloader: Callable[..., Path],
    progress: ProgressCallback | None,
    cancel: CancelCallback | None,
) -> str | None:
    """下载校验文件并解析哈希；用完即删，不在更新目录留下残留。"""
    checksum_path = work_dir / CHECKSUM_ASSET_NAME
    try:
        downloader(asset.url, checksum_path, progress=progress, cancel=cancel)
        text = checksum_path.read_text(encoding="utf-8", errors="replace")
        return parse_checksum(text)
    except OSError as exc:
        raise UpdateError(f"下载校验文件失败：{exc}。") from exc
    finally:
        checksum_path.unlink(missing_ok=True)

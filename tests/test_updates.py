"""更新服务与后台线程测试：全部注入假 opener / 假下载器，绝不产生真实网络请求。

`fetch_latest_release` / `download_asset` 只通过 `opener` 参数替换网络层，
worker 则直接调用 `run()`（不启动事件循环），因此测试无需网络也不需要等待。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from app.io.update_worker import (
    UNEXPECTED_ERROR_MESSAGE,
    UpdateCheckWorker,
    UpdateDownloadWorker,
)
from app.services.paths import resolve_paths
from app.services.updates import (
    CHECKSUM_ASSET_NAME,
    EXE_ASSET_NAME,
    RELEASE_API_URL,
    REQUEST_TIMEOUT,
    USER_AGENT,
    ReleaseAsset,
    ReleaseInfo,
    UpdateError,
    download_asset,
    download_release,
    fetch_latest_release,
    is_newer,
    parse_checksum,
    parse_version,
    prune_updates,
    select_assets,
    sha256_file,
    updates_dir,
    verify_sha256,
)
from app.ui.qt_utils import process_events_until

EXE_URL = "https://github.com/Yaopyr/PortHelperFluent/releases/download/v1.0.1/PortHelperFluent.exe"
CHECKSUM_URL = "https://github.com/Yaopyr/PortHelperFluent/releases/download/v1.0.1/SHA256.txt"


class FakeResponse:
    """假的 urlopen 返回值：按块吐数据、记录是否关闭，并模拟最终 URL。"""

    def __init__(
        self,
        payload: bytes = b"",
        *,
        chunks: list[bytes] | None = None,
        url: str = "",
        total: int | None = None,
    ) -> None:
        self._chunks = list(chunks) if chunks is not None else [payload]
        self._url = url
        self.closed = False
        if total is None:
            total = sum(len(chunk) for chunk in self._chunks)
        self.headers = {"Content-Length": str(total)} if total else {}

    def read(self, size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    """记录请求与超时参数；可返回预设响应或抛出预设异常。"""

    def __init__(self, response: FakeResponse | None = None, *, error: Exception | None = None):
        self.requests = []
        self.timeouts = []
        self._response = response
        self._error = error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self._error is not None:
            raise self._error
        return self._response


class FakeDownloader:
    """按 URL 返回预设内容；签名与 `download_asset` 的调用方式一致。"""

    def __init__(self, contents: dict[str, bytes]) -> None:
        self.contents = contents
        self.calls: list[str] = []
        self.progress_values: list[object] = []

    def __call__(self, url, dest, *, progress=None, cancel=None):
        self.calls.append(url)
        self.progress_values.append(progress)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.contents[url])
        return dest


def _payload(**overrides) -> bytes:
    data = {
        "tag_name": "v1.0.1",
        "name": "PortHelper Fluent 1.0.1",
        "body": "修复若干问题",
        "html_url": "https://github.com/Yaopyr/PortHelperFluent/releases/tag/v1.0.1",
        "published_at": "2026-10-28T10:00:00Z",
        "assets": [
            {
                "name": EXE_ASSET_NAME,
                "browser_download_url": EXE_URL,
                "size": 1024,
                "digest": "sha256:" + "a" * 64,
            },
            {
                "name": CHECKSUM_ASSET_NAME,
                "browser_download_url": CHECKSUM_URL,
                "size": 88,
                "digest": "",
            },
        ],
    }
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


def _release(**overrides) -> ReleaseInfo:
    fields = {
        "version": "1.0.1",
        "tag": "v1.0.1",
        "title": "PortHelper Fluent 1.0.1",
        "notes": "修复若干问题",
        "page_url": "https://github.com/Yaopyr/PortHelperFluent/releases/tag/v1.0.1",
        "published_at": "2026-10-28T10:00:00Z",
        "exe": ReleaseAsset(EXE_ASSET_NAME, EXE_URL, 9, ""),
        "checksum": None,
    }
    fields.update(overrides)
    return ReleaseInfo(**fields)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(RELEASE_API_URL, code, "error", {}, None)


# ---- 版本解析 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v1.0.1", (1, 0, 1)),
        ("1.0", (1, 0)),
        ("1.0.1-beta", (1, 0, 1)),
        ("", ()),
        ("版本未知", ()),
    ],
)
def test_parse_version(text: str, expected: tuple[int, ...]) -> None:
    assert parse_version(text) == expected


def test_is_newer_compares_numeric_parts() -> None:
    assert is_newer("1.0.1", "1.0.0") is True
    assert is_newer("v1.0.10", "1.0.9") is True
    assert is_newer("1.0.0", "1.0.0") is False
    assert is_newer("1.0", "1.0.0") is False
    assert is_newer("0.9.9", "1.0.0") is False
    assert is_newer("not-a-version", "1.0.0") is False


# ---- 资产选择 ---------------------------------------------------------------


def test_select_assets_prefers_exact_names() -> None:
    assets = [
        {"name": "other.exe", "browser_download_url": "u1", "size": 1, "digest": ""},
        {"name": EXE_ASSET_NAME, "browser_download_url": EXE_URL, "size": 9, "digest": "sha256:abc"},
        {"name": CHECKSUM_ASSET_NAME, "browser_download_url": CHECKSUM_URL, "size": 3, "digest": ""},
    ]

    exe, checksum = select_assets(assets)

    assert exe is not None and exe.name == EXE_ASSET_NAME and exe.url == EXE_URL
    # digest 原样保留，不在选择阶段做校验
    assert exe.digest == "sha256:abc"
    assert exe.size == 9
    assert checksum is not None and checksum.name == CHECKSUM_ASSET_NAME


def test_select_assets_ignores_name_case() -> None:
    assets = [
        {"name": "porthelperfluent.EXE", "browser_download_url": "u1", "size": 1},
        {"name": "sha256.TXT", "browser_download_url": "u2", "size": 2},
    ]

    exe, checksum = select_assets(assets)

    assert exe is not None and exe.name == "porthelperfluent.EXE"
    assert checksum is not None and checksum.name == "sha256.TXT"


def test_select_assets_falls_back_to_first_exe() -> None:
    assets = [
        {"name": "PortHelperFluent-1.0.1.exe", "browser_download_url": "u1", "size": 1},
        {"name": "PortHelperFluent-1.0.2.exe", "browser_download_url": "u2", "size": 2},
    ]

    exe, checksum = select_assets(assets)

    assert exe is not None and exe.url == "u1"
    assert checksum is None


def test_select_assets_returns_none_without_exe() -> None:
    exe, checksum = select_assets([{"name": "notes.txt", "browser_download_url": "u"}])

    assert exe is None
    assert checksum is None


# ---- 拉取 Release -----------------------------------------------------------


def test_fetch_latest_release_parses_payload() -> None:
    opener = FakeOpener(FakeResponse(_payload(), url=RELEASE_API_URL))

    release = fetch_latest_release(opener)

    request = opener.requests[0]
    headers = {key.lower(): value for key, value in request.headers.items()}
    assert request.full_url == RELEASE_API_URL
    assert headers["user-agent"] == USER_AGENT
    assert headers["accept"] == "application/vnd.github+json"
    assert opener.timeouts == [REQUEST_TIMEOUT]
    assert release.version == "1.0.1"
    assert release.tag == "v1.0.1"
    assert release.title == "PortHelper Fluent 1.0.1"
    assert release.notes == "修复若干问题"
    assert release.published_at == "2026-10-28T10:00:00Z"
    assert release.exe is not None and release.exe.name == EXE_ASSET_NAME
    assert release.checksum is not None
    assert release.page_url.endswith("/releases/tag/v1.0.1")


def test_fetch_latest_release_reports_missing_release() -> None:
    opener = FakeOpener(error=_http_error(404))

    with pytest.raises(UpdateError) as excinfo:
        fetch_latest_release(opener)

    assert str(excinfo.value) == "仓库还没有发布任何 Release。"


def test_fetch_latest_release_reports_rate_limit() -> None:
    opener = FakeOpener(error=_http_error(403))

    with pytest.raises(UpdateError) as excinfo:
        fetch_latest_release(opener)

    assert "HTTP 403" in str(excinfo.value)
    assert "频繁" in str(excinfo.value)


def test_fetch_latest_release_reports_network_error() -> None:
    opener = FakeOpener(error=urllib.error.URLError("no route"))

    with pytest.raises(UpdateError) as excinfo:
        fetch_latest_release(opener)

    assert "无法连接 GitHub" in str(excinfo.value)


def test_fetch_latest_release_rejects_invalid_json() -> None:
    opener = FakeOpener(FakeResponse(b"{not json}"))

    with pytest.raises(UpdateError) as excinfo:
        fetch_latest_release(opener)

    assert "无法解析" in str(excinfo.value)


def test_fetch_latest_release_rejects_missing_tag() -> None:
    opener = FakeOpener(FakeResponse(_payload(tag_name="")))

    with pytest.raises(UpdateError) as excinfo:
        fetch_latest_release(opener)

    assert "tag_name" in str(excinfo.value)


# ---- 下载 -------------------------------------------------------------------


def test_download_asset_writes_chunks_and_reports_progress(tmp_path: Path) -> None:
    dest = tmp_path / "updates" / "PortHelperFluent-1.0.1.exe"
    response = FakeResponse(chunks=[b"abc", b"de"], url=EXE_URL, total=5)
    progress: list[tuple[int, int]] = []

    download_asset(EXE_URL, dest, opener=FakeOpener(response), progress=lambda r, t: progress.append((r, t)))

    assert dest.read_bytes() == b"abcde"
    assert not dest.with_name(dest.name + ".part").exists()
    assert progress == [(3, 5), (5, 5)]
    assert response.closed is True


def test_download_asset_cancel_removes_partial_file(tmp_path: Path) -> None:
    dest = tmp_path / "PortHelperFluent-1.0.1.exe"
    progress: list[tuple[int, int]] = []
    calls = {"count": 0}

    def cancel() -> bool:
        calls["count"] += 1
        return calls["count"] > 1

    with pytest.raises(UpdateError) as excinfo:
        download_asset(
            EXE_URL,
            dest,
            opener=FakeOpener(FakeResponse(chunks=[b"partial"])),
            progress=lambda r, t: progress.append((r, t)),
            cancel=cancel,
        )

    assert "已取消" in str(excinfo.value)
    assert progress == [(7, 7)]
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/Yaopyr/PortHelperFluent/a.exe",
        "https://example.com/PortHelperFluent.exe",
        "https://evilgithub.com/PortHelperFluent.exe",
    ],
)
def test_download_asset_rejects_untrusted_url(tmp_path: Path, url: str) -> None:
    opener = FakeOpener(FakeResponse(b"x"))

    with pytest.raises(UpdateError):
        download_asset(url, tmp_path / "app.exe", opener=opener)

    assert opener.requests == []
    assert not (tmp_path / "app.exe").exists()


def test_download_asset_rechecks_redirected_host(tmp_path: Path) -> None:
    response = FakeResponse(chunks=[b"payload"], url="https://example.com/PortHelperFluent.exe")

    with pytest.raises(UpdateError) as excinfo:
        download_asset(EXE_URL, tmp_path / "app.exe", opener=FakeOpener(response))

    assert "主机" in str(excinfo.value)
    assert not (tmp_path / "app.exe").exists()


def test_download_asset_reports_network_error_while_reading(tmp_path: Path) -> None:
    class BrokenResponse(FakeResponse):
        def read(self, size: int = -1) -> bytes:
            raise OSError("connection reset by peer")

    dest = tmp_path / "app.exe"

    with pytest.raises(UpdateError) as excinfo:
        download_asset(EXE_URL, dest, opener=FakeOpener(BrokenResponse(url=EXE_URL)))

    message = str(excinfo.value)
    assert message.startswith("下载更新失败")
    assert "保存更新文件失败" not in message
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


# ---- 下载并校验 Release ------------------------------------------------------


def test_download_release_verifies_api_digest(tmp_path: Path) -> None:
    payload = b"exe-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    exe = ReleaseAsset(EXE_ASSET_NAME, EXE_URL, len(payload), f"sha256:{digest}")
    downloader = FakeDownloader({EXE_URL: payload})

    path = download_release(_release(exe=exe), tmp_path, downloader=downloader)

    assert path == tmp_path / "PortHelperFluent-1.0.1.exe"
    assert path.read_bytes() == payload
    assert downloader.calls == [EXE_URL]


def test_download_release_falls_back_to_checksum_asset(tmp_path: Path) -> None:
    payload = b"exe-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    exe = ReleaseAsset(EXE_ASSET_NAME, EXE_URL, len(payload), "")
    checksum = ReleaseAsset(CHECKSUM_ASSET_NAME, CHECKSUM_URL, 80, "")
    downloader = FakeDownloader(
        {EXE_URL: payload, CHECKSUM_URL: f"{digest}  PortHelperFluent.exe\n".encode()}
    )
    progress_values: list[tuple[int, int]] = []

    def progress(received: int, total: int) -> None:
        progress_values.append((received, total))

    path = download_release(
        _release(exe=exe, checksum=checksum), tmp_path, progress=progress, downloader=downloader
    )

    assert path.exists()
    assert downloader.calls == [EXE_URL, CHECKSUM_URL]
    # 校验文件不推送主下载进度，避免界面进度条回退
    assert downloader.progress_values == [progress, None]
    # 校验文件用完即删，不在更新目录留下残留
    assert not (tmp_path / CHECKSUM_ASSET_NAME).exists()


def test_download_release_rejects_unsafe_remote_version(tmp_path: Path) -> None:
    payload = b"exe"
    digest = hashlib.sha256(payload).hexdigest()
    exe = ReleaseAsset(EXE_ASSET_NAME, EXE_URL, len(payload), f"sha256:{digest}")
    downloader = FakeDownloader({EXE_URL: payload})

    target = download_release(
        _release(version="x/../../evil", exe=exe), tmp_path, downloader=downloader
    )

    # 远端 tag 不可信：净化后仍然只能写在 work_dir 内
    assert target.parent == tmp_path
    assert "/" not in target.name and "\\" not in target.name
    assert ".." not in target.name
    assert target.name.startswith("PortHelperFluent-")


def test_download_release_removes_exe_when_checksum_stage_fails(
    tmp_path: Path,
) -> None:
    payload = b"exe-bytes"
    exe = ReleaseAsset(EXE_ASSET_NAME, EXE_URL, len(payload), "")
    checksum = ReleaseAsset(CHECKSUM_ASSET_NAME, CHECKSUM_URL, 10, "")

    class FailingChecksum(FakeDownloader):
        def __call__(self, url, dest, *, progress=None, cancel=None):
            if url == CHECKSUM_URL:
                raise UpdateError("下载更新失败：网络已断开，请检查网络后重试。")
            return super().__call__(url, dest, progress=progress, cancel=cancel)

    downloader = FailingChecksum({EXE_URL: payload})

    with pytest.raises(UpdateError) as excinfo:
        download_release(_release(exe=exe, checksum=checksum), tmp_path, downloader=downloader)

    assert "网络" in str(excinfo.value)
    # 取校验值失败时也必须删除已下载的 exe，不能留下未验证的文件
    assert list(tmp_path.glob("*.exe")) == []


def test_download_release_requires_checksum_info(tmp_path: Path) -> None:
    downloader = FakeDownloader({})

    with pytest.raises(UpdateError) as excinfo:
        download_release(_release(), tmp_path, downloader=downloader)

    assert str(excinfo.value) == "发布缺少 SHA-256 校验信息，已取消更新。"
    assert downloader.calls == []


def test_download_release_deletes_file_on_digest_mismatch(tmp_path: Path) -> None:
    exe = ReleaseAsset(EXE_ASSET_NAME, EXE_URL, 3, "sha256:" + "0" * 64)
    downloader = FakeDownloader({EXE_URL: b"abc"})

    with pytest.raises(UpdateError) as excinfo:
        download_release(_release(exe=exe), tmp_path, downloader=downloader)

    assert "校验失败" in str(excinfo.value)
    assert list(tmp_path.glob("*.exe")) == []


def test_download_release_rejects_release_without_exe(tmp_path: Path) -> None:
    with pytest.raises(UpdateError) as excinfo:
        download_release(_release(exe=None), tmp_path)

    assert "exe" in str(excinfo.value)


# ---- 校验、目录与清理 --------------------------------------------------------


def test_parse_checksum_reads_first_hash() -> None:
    digest = "a" * 64

    assert parse_checksum(f"{digest}  PortHelperFluent.exe\n") == digest
    assert parse_checksum("SHA256=" + "B" * 64) == "b" * 64
    assert parse_checksum("没有哈希") is None
    assert parse_checksum("") is None


def test_sha256_file_and_verify(tmp_path: Path) -> None:
    target = tmp_path / "a.bin"
    target.write_bytes(b"data")
    digest = hashlib.sha256(b"data").hexdigest()

    assert sha256_file(target) == digest
    assert verify_sha256(target, digest) is True
    assert verify_sha256(target, digest.upper()) is True
    assert verify_sha256(target, "sha256:" + digest) is True
    assert verify_sha256(target, "sha256:") is False
    assert verify_sha256(target, "0" * 64) is False
    assert verify_sha256(tmp_path / "missing.bin", digest) is False


def test_updates_dir_is_created_under_data_dir(tmp_path: Path) -> None:
    paths = resolve_paths(tmp_path / "exe", tmp_path / "appdata")

    directory = updates_dir(paths)

    assert directory == paths.data_dir / "updates"
    assert directory.is_dir()


def test_prune_updates_removes_stale_files_only(tmp_path: Path) -> None:
    old_exe = tmp_path / "PortHelperFluent-0.9.0.exe"
    old_exe.write_bytes(b"old")
    part = tmp_path / "PortHelperFluent-1.0.1.exe.part"
    part.write_bytes(b"part")
    script = tmp_path / "apply-update.ps1"
    script.write_text("x", encoding="ascii")
    old_log = tmp_path / "update.log.old"
    old_log.write_text("x", encoding="ascii")
    backup = tmp_path / f"backup-PortHelperFluent-1.0.0.exe"
    backup.write_bytes(b"backup")
    fresh = tmp_path / "PortHelperFluent-1.0.1.exe"
    fresh.write_bytes(b"fresh")
    stale = time.time() - 48 * 3600
    for path in (old_exe, part, script, old_log, backup):
        os.utime(path, (stale, stale))

    prune_updates(tmp_path)

    assert not old_exe.exists()
    assert not part.exists()
    assert not script.exists()
    assert not old_log.exists()
    assert fresh.exists()
    # 备份是回滚依据，永不清理
    assert backup.exists()


# ---- 后台任务 ---------------------------------------------------------------


def test_check_worker_emits_checked_and_finished(qapp) -> None:
    release = _release()
    worker = UpdateCheckWorker(fetcher=lambda: release)
    checked: list[object] = []
    failed: list[str] = []
    finished: list[bool] = []
    worker.checked.connect(checked.append)
    worker.failed.connect(failed.append)
    worker.finished.connect(lambda: finished.append(True))

    worker.start()

    assert worker.wait(3000) is True
    assert worker.isRunning() is False
    # 跨线程信号通过事件队列投递，处理一次事件循环后应收到 checked/finished。
    assert process_events_until(lambda: bool(checked) and bool(finished), timeout_ms=1000)
    assert checked == [release]
    assert failed == []
    assert finished == [True]


def test_check_worker_runs_on_daemon_thread(qapp) -> None:
    gate = threading.Event()

    def fetcher():
        gate.wait(5)
        return _release()

    worker = UpdateCheckWorker(fetcher=fetcher)
    worker.start()

    threads = [item for item in threading.enumerate() if item.name == "update-check"]
    assert [item.daemon for item in threads] == [True]

    gate.set()
    assert worker.wait(3000) is True
    assert worker.isRunning() is False


def test_check_worker_start_is_idempotent_while_running(qapp) -> None:
    gate = threading.Event()

    def fetcher():
        gate.wait(5)
        return _release()

    worker = UpdateCheckWorker(fetcher=fetcher)
    worker.start()
    worker.start()
    worker.start()

    names = [item.name for item in threading.enumerate() if item.name == "update-check"]
    assert names == ["update-check"]

    gate.set()
    assert worker.wait(3000) is True


def test_check_worker_reports_update_error(qapp) -> None:
    def fetcher():
        raise UpdateError("仓库还没有发布任何 Release。")

    worker = UpdateCheckWorker(fetcher=fetcher)
    failed: list[str] = []
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == ["仓库还没有发布任何 Release。"]


def test_check_worker_reports_unexpected_error(qapp) -> None:
    def fetcher():
        raise RuntimeError("boom")

    worker = UpdateCheckWorker(fetcher=fetcher)
    failed: list[str] = []
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == [UNEXPECTED_ERROR_MESSAGE]


def test_download_worker_emits_progress_and_path(qapp, tmp_path: Path) -> None:
    release = _release()
    target = tmp_path / "PortHelperFluent-1.0.1.exe"
    seen_work_dirs: list[Path] = []

    def runner(release_arg, work_dir, *, progress=None, cancel=None):
        assert release_arg is release
        seen_work_dirs.append(work_dir)
        progress(5, 10)
        progress(10, 10)
        return target

    worker = UpdateDownloadWorker(release, tmp_path, runner=runner)
    progress: list[tuple[int, int]] = []
    downloaded: list[object] = []
    failed: list[str] = []
    finished: list[bool] = []
    worker.progress.connect(lambda received, total: progress.append((received, total)))
    worker.downloaded.connect(downloaded.append)
    worker.failed.connect(failed.append)
    worker.finished.connect(lambda: finished.append(True))

    worker.run()

    assert seen_work_dirs == [tmp_path]
    assert progress == [(5, 10), (10, 10)]
    assert downloaded == [target]
    assert failed == []
    # run() 被直接调用时不依赖线程状态，也不应发 finished（由外壳负责）。
    assert finished == []


def test_download_worker_reports_update_error(qapp, tmp_path: Path) -> None:
    def runner(release, work_dir, *, progress=None, cancel=None):
        raise UpdateError("下载已取消。")

    worker = UpdateDownloadWorker(_release(), tmp_path, runner=runner)
    failed: list[str] = []
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == ["下载已取消。"]


def test_download_worker_maps_interruption_to_cancel(qapp, tmp_path: Path) -> None:
    started = threading.Event()
    observed: list[bool] = []

    def runner(release, work_dir, *, progress=None, cancel=None):
        started.set()
        # 真调用传入的 cancel()：requestInterruption() 置 threading.Event 后应变为 True。
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if cancel():
                observed.append(True)
                raise UpdateError("下载已取消。")
            time.sleep(0.01)
        observed.append(False)
        return work_dir / "PortHelperFluent-1.0.1.exe"

    worker = UpdateDownloadWorker(_release(), tmp_path, runner=runner)
    failed: list[str] = []
    finished: list[bool] = []
    worker.failed.connect(failed.append)
    worker.finished.connect(lambda: finished.append(True))

    worker.start()
    try:
        assert started.wait(2)
        worker.requestInterruption()
        assert worker.wait(3000) is True
        assert worker.isRunning() is False
    finally:
        # 例外路径也不留下仍在跑的线程。
        worker.wait(3000)

    assert observed == [True]
    # 跨线程的信号通过事件队列投递，等待期间处理一次事件循环。
    assert process_events_until(lambda: bool(failed) and bool(finished), timeout_ms=1000)
    assert failed == ["下载已取消。"]
    assert finished == [True]

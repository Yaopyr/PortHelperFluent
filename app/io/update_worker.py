"""在线更新的后台线程：检查最新 Release 与下载安装包，都不阻塞界面。

worker 自身不发起任何网络请求，检查/下载分别由注入的 `fetcher`、`runner` 完成，
因此单元测试可以直接调用 `run()` 并注入假实现，不需要事件循环也不联网。
`UpdateError` 原样转成中文 `failed` 文本；其它异常只写日志并给出统一提示。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.logging_setup import get_logger
from app.services.updates import (
    ReleaseInfo,
    UpdateError,
    download_release,
    fetch_latest_release,
)

UNEXPECTED_ERROR_MESSAGE = "更新时发生未预期错误，详情见日志。"


class UpdateCheckWorker(QThread):
    """在后台检查最新 Release；构造时不联网。"""

    checked = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        parent=None,
        *,
        fetcher: Callable[..., ReleaseInfo] = fetch_latest_release,
    ) -> None:
        super().__init__(parent)
        self._fetcher = fetcher

    def run(self) -> None:
        try:
            release = self._fetcher()
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception:
            get_logger().exception("检查更新时发生未预期错误")
            self.failed.emit(UNEXPECTED_ERROR_MESSAGE)
        else:
            self.checked.emit(release)


class UpdateDownloadWorker(QThread):
    """在后台下载并校验更新包；`requestInterruption()` 即取消下载。"""

    progress = pyqtSignal(int, int)
    downloaded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        release: ReleaseInfo,
        work_dir: Path,
        parent=None,
        *,
        runner: Callable[..., Path] = download_release,
    ) -> None:
        super().__init__(parent)
        self._release = release
        self._work_dir = Path(work_dir)
        self._runner = runner

    def run(self) -> None:
        try:
            path = self._runner(
                self._release,
                self._work_dir,
                progress=self._emit_progress,
                cancel=self.isInterruptionRequested,
            )
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception:
            get_logger().exception("下载更新时发生未预期错误")
            self.failed.emit(UNEXPECTED_ERROR_MESSAGE)
        else:
            self.downloaded.emit(path)

    def _emit_progress(self, received: int, total: int) -> None:
        self.progress.emit(int(received), int(total))

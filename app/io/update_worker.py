"""在线更新的后台任务：检查最新 Release 与下载安装包，都不阻塞界面。

实现方式：用 daemon `threading.Thread` 承载 `run()`，结果通过 QObject 的 pyqtSignal
投递回调用方（PyQt 按接收方线程自动排队）。不用 QThread 的原因：`fetcher` 里是阻塞的
urllib 读取，`requestInterruption()` 无法打断它；`wait()` 超时后若窗口销毁仍在运行的
线程对象，Qt 会报 `QThread: Destroyed while thread is still running` 并以 0xC0000409
中止进程。daemon 线程不阻塞解释器退出，`wait(ms)` 只是一次有限等待，调用方可以安全地
丢弃引用（线程运行期间通过 bound method 持有 worker 自身的 Python 引用）。

worker 自身不发起任何网络请求，检查/下载分别由注入的 `fetcher`、`runner` 完成，
因此单元测试可以直接调用 `run()` 并注入假实现，不需要事件循环也不联网。
`UpdateError` 原样转成中文 `failed` 文本；其它异常只写日志并给出统一提示。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal

from app.core.logging_setup import get_logger
from app.services.updates import (
    ReleaseInfo,
    UpdateError,
    download_release,
    fetch_latest_release,
)

UNEXPECTED_ERROR_MESSAGE = "更新时发生未预期错误，详情见日志。"
DEFAULT_JOIN_TIMEOUT_MS = 2000
CHECK_THREAD_NAME = "update-check"
DOWNLOAD_THREAD_NAME = "update-download"


class _UpdateWorkerBase(QObject):
    """更新任务的公共外壳：daemon 线程 + 信号 + 中断/等待接口。

    `start()` 重复调用不会产生第二条线程；`wait(ms)` 只等待有限时间并返回线程是否
    已经退出，超时不抛异常，调用方可立即丢弃引用（daemon 线程不会阻止进程退出）。
    """

    finished = pyqtSignal()

    def __init__(self, parent=None, *, thread_name: str) -> None:
        super().__init__(parent)
        self._thread_name = thread_name
        self._thread: threading.Thread | None = None
        self._interrupted = threading.Event()

    # ---- 生命周期 -----------------------------------------------------

    def start(self) -> None:
        if self.isRunning():
            return
        thread = threading.Thread(
            target=self._run_guarded, name=self._thread_name, daemon=True
        )
        self._thread = thread
        thread.start()

    def _run_guarded(self) -> None:
        """在线程内执行 `run()`，无论结果如何都发一次 `finished`。"""
        try:
            self.run()
        except Exception:
            # 子类 run() 已自行处理异常；这里只兜住真正漏网的实现错误。
            get_logger().exception("更新任务执行失败")
        finally:
            try:
                self.finished.emit()
            except RuntimeError:
                # 调用方已丢弃引用且对象已销毁：没有接收者，不发信号即可。
                pass

    def run(self) -> None:
        raise NotImplementedError

    def requestInterruption(self) -> None:
        self._interrupted.set()

    def isInterruptionRequested(self) -> bool:
        return self._interrupted.is_set()

    def wait(self, ms: int = DEFAULT_JOIN_TIMEOUT_MS) -> bool:
        """最多等待 `ms` 毫秒，返回线程是否已退出；超时返回 False。"""
        thread = self._thread
        if thread is None:
            return True
        try:
            thread.join(max(0, int(ms)) / 1000.0)
        except RuntimeError:
            # 不能在当前线程里 join 自己：视为未等待成功。
            return False
        return not thread.is_alive()

    def isRunning(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())


class UpdateCheckWorker(_UpdateWorkerBase):
    """在后台检查最新 Release；构造时不联网，重复 `start()` 不会多起线程。"""

    checked = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        parent=None,
        *,
        fetcher: Callable[..., ReleaseInfo] = fetch_latest_release,
    ) -> None:
        super().__init__(parent, thread_name=CHECK_THREAD_NAME)
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


class UpdateDownloadWorker(_UpdateWorkerBase):
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
        super().__init__(parent, thread_name=DOWNLOAD_THREAD_NAME)
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

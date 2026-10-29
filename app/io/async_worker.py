"""daemon 线程 + 私有 asyncio loop 的后台任务外壳，供蓝牙 BLE/SPP worker 复用。

与 `update_worker` 的阻塞式线程不同，这里要在后台跑协程（bleak / WinRT 的异步 API），
所以每条 worker 线程自带一个 asyncio loop：界面线程只用线程安全的
`loop.call_soon_threadsafe` 投递任务，结果一律通过 Qt 信号回传，绝不阻塞界面。

线程是 daemon，解释器退出时不会卡住。`shutdown()` 先取消在跑的任务、再停 loop、
有限等待线程退出；重复调用直接返回，`finished` 只发一次。对外方法都不抛异常：
loop 未启动、已停或已关停时 `submit()` 返回 False，由子类决定提示文案。

子类只需要声明自己的业务信号，不要重复声明 `error`/`finished`；执行具体操作时把
`self._stage` 设成当前阶段（如 "connect"），并覆盖 `_describe_error()` 把异常转成
带排查建议的中文文案。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Coroutine

from PyQt6.QtCore import QObject, pyqtSignal

from app.core.logging_setup import get_logger

THREAD_NAME = "bluetooth-async"
LOOP_START_TIMEOUT = 2.0
DEFAULT_SHUTDOWN_TIMEOUT = 3.0


class AsyncLoopWorker(QObject):
    """一条 daemon 线程 + 一个私有 asyncio loop 的异步任务外壳。"""

    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = False
        self._lock = threading.Lock()
        self._stage = ""

    # ---- 生命周期 -----------------------------------------------------

    def start(self) -> None:
        """起 daemon 线程与 asyncio loop；已在运行或已 `shutdown()` 时什么都不做。"""
        with self._lock:
            if self._closed:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            thread = threading.Thread(target=self._run_loop, name=THREAD_NAME, daemon=True)
            self._thread = thread
            thread.start()
        # 等 loop 真正建好再返回，调用方紧接着 `submit()` 才不会拿到 False。
        self._ready.wait(LOOP_START_TIMEOUT)

    def thread_alive(self) -> bool:
        """后台线程是否还在跑（`shutdown()` 之后应为 False）。"""
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def submit(self, factory: Callable[[], Coroutine[Any, Any, None]]) -> bool:
        """把 `factory()` 产出的协程投递到后台 loop；loop 不可用时返回 False。"""
        with self._lock:
            loop = self._loop
            if self._closed or loop is None:
                return False
        try:
            # loop 可能刚建好还没进 run_forever：call_soon_threadsafe 会先排队，
            # 等 loop 跑起来立刻执行，不必再额外判 is_running()。
            loop.call_soon_threadsafe(self._schedule, loop, factory)
        except RuntimeError:
            # loop 在关停过程中：按不可用处理，不向调用方抛异常。
            return False
        return True

    def shutdown(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """取消任务、停 loop、有限等待线程退出；幂等，`finished` 只发一次。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._cancel_tasks, loop)
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))
        self._ready.clear()
        self.finished.emit()

    def _run_loop(self) -> None:
        """线程主体：建 loop、跑事件循环，退出时清理残留任务再关 loop。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            if self._closed:
                # shutdown() 抢在 loop 建好之前调用：不必再跑，直接收尾。
                loop.close()
                self._ready.set()
                return
            self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            self._drain(loop)

    def _drain(self, loop: asyncio.AbstractEventLoop) -> None:
        """停 loop 后取消剩余任务并跑完它们各自的清理，避免 pending task 警告。"""
        try:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            get_logger().exception("关闭蓝牙后台线程时清理任务失败")
        finally:
            with self._lock:
                if self._loop is loop:
                    self._loop = None
            loop.close()

    def _cancel_tasks(self, loop: asyncio.AbstractEventLoop) -> None:
        """在 loop 线程里取消所有未完成任务（由 `shutdown()` 投递）。"""
        for task in asyncio.all_tasks(loop):
            if not task.done():
                task.cancel()

    # ---- 任务执行与错误文案 -------------------------------------------

    def _schedule(
        self, loop: asyncio.AbstractEventLoop, factory: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        """在 loop 线程里建任务；连 `factory()` 自身抛异常都收敛成 `error` 信号。"""
        try:
            coroutine = factory()
        except Exception as exc:
            self.error.emit(self._describe_error(exc))
            return
        task = loop.create_task(self._guard(coroutine))
        # 任务在首次调度前就被取消时 `_guard` 体根本不会执行，这里兜底关闭协程，
        # 避免 asyncio 报 "coroutine was never awaited"；已结束的协程 close() 是空操作。
        task.add_done_callback(lambda _task: coroutine.close())

    async def _guard(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """执行任务：异常转成 `error` 文案，任务被取消时静默退出。"""
        try:
            await coroutine
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            get_logger().debug("蓝牙后台任务异常：%s", exc)
            self.error.emit(self._describe_error(exc))

    def _describe_error(self, exc: BaseException) -> str:
        """把异常转成给界面的文案；子类可覆盖并按 `self._stage` 补排查建议。"""
        text = str(exc).strip()
        return text or exc.__class__.__name__

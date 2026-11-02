"""模拟上下文：真实主窗口 + 虚拟设备 + 模态框策略 + 证据记录。"""

from __future__ import annotations

import gc
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QCoreApplication, QEvent, QTimer
from PyQt6.QtWidgets import QApplication

import main
from app.core.settings import Settings
from app.services.paths import AppPaths, resolve_paths
from app.services.updates import ReleaseAsset, ReleaseInfo, UpdateError
from app.ui.qt_utils import process_events_until
from qfluentwidgets import qconfig

from .assets import find_font
from .injection import install_injections
from .modal import ModalPolicy
from ..devices import VirtualDeviceHub


class SimulationContext:
    def __init__(
        self,
        qapp: QApplication,
        tmp_path: Path,
        monkeypatch,
        scenario_id: str,
        report_dir: Path,
    ) -> None:
        self.qapp = qapp
        self.tmp_path = Path(tmp_path)
        self.monkeypatch = monkeypatch
        self.scenario_id = scenario_id
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_mode = "failure"
        self.checks: list[dict] = []
        self.screenshots: list[str] = []
        self.artifacts: list[str] = []
        self.hub = VirtualDeviceHub()
        self.modal = ModalPolicy()
        self.modal.dialog_screenshot = self._grab_dialog
        self.font_path = find_font()
        self.install_calls: list[tuple] = []
        self.can_replace_flag = True
        self.frozen = False
        self.fake_exe = self.tmp_path / "PortHelperFluent.exe"
        self.fake_exe.write_bytes(b"MZ-simulation")
        self.update_error: str | None = None
        self.download_error: str | None = None
        self.fetch_calls = 0
        self.release = ReleaseInfo(
            version="9.9.9",
            tag="v9.9.9",
            title="PortHelper Fluent 9.9.9（模拟）",
            notes="模拟更新说明",
            page_url="https://github.com/Yaopyr/PortHelperFluent/releases/latest",
            published_at="2026-10-30T00:00:00Z",
            exe=ReleaseAsset(
                name="PortHelperFluent.exe",
                url="https://github.com/Yaopyr/PortHelperFluent/releases/download/v9.9.9/exe",
                size=5 * 1024 * 1024,
                digest="",
            ),
            checksum=None,
        )

        self.paths: AppPaths = resolve_paths(
            exe_path=self.tmp_path / "exe", appdata=self.tmp_path / "appdata"
        )
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths.log_dir.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(self.paths.config_file)
        self.settings.load()

        self.modal.install(monkeypatch)
        install_injections(monkeypatch, self)

        self._orig_theme = qconfig.themeMode.value
        self._orig_color = qconfig.themeColor.value
        self.window = main.create_main_window(self.settings, self.paths)
        self.window.show()
        self.qapp.processEvents()
        self._closed = False
        self._safety = QTimer()
        self._safety.setInterval(500)
        self._safety.timeout.connect(self._modal_safety)
        self._safety.start()
        self._modal_since: float | None = None

    # ---- 页面与交互 ---------------------------------------------------

    def page(self, route: str):
        return self.window.pages[route]

    def switch(self, route: str):
        page = self.page(route)
        self.window.switchTo(page)
        process_events_until(lambda: page.isVisible(), timeout_ms=1500)
        self.qapp.processEvents()
        return page

    def wait(self, predicate: Callable[[], bool], timeout_ms: int = 3000) -> bool:
        return process_events_until(predicate, timeout_ms=timeout_ms)

    def wait_text(self, widget, text: str, timeout_ms: int = 3000) -> bool:
        return self.wait(lambda: text in widget.toPlainText(), timeout_ms=timeout_ms)

    def log_text(self, page, attribute: str = "log_pane") -> str:
        pane = getattr(page, attribute)
        flush = getattr(pane, "_flush_pending", None)
        if callable(flush):
            flush()
        return pane.log_view.toPlainText()

    # ---- 断言与证据 ---------------------------------------------------

    def check(self, name: str, actual: Any, expected: Any) -> None:
        ok = actual == expected
        self.checks.append(
            {"name": name, "ok": ok, "actual": _short(actual), "expected": _short(expected)}
        )
        if not ok:
            raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")

    def check_true(self, name: str, condition: bool, detail: str = "") -> None:
        self.checks.append(
            {"name": name, "ok": bool(condition), "actual": _short(detail), "expected": True}
        )
        if not condition:
            raise AssertionError(f"{name}: {detail or '条件不成立'}")

    def check_in(self, name: str, needle: str, haystack: str) -> None:
        ok = needle in (haystack or "")
        self.checks.append(
            {"name": name, "ok": ok, "actual": _short(haystack), "expected": f"包含 {needle!r}"}
        )
        if not ok:
            raise AssertionError(f"{name}: 未找到 {needle!r}，实际为 {haystack!r}")

    def screenshot(self, tag: str) -> str | None:
        path = self._save_grab(self.window, tag)
        if path is not None:
            self.screenshots.append(path)
        return path

    def artifact(self, path: Path, label: str = "") -> str | None:
        source = Path(path)
        if not source.exists():
            return None
        target_dir = self.report_dir / "artifacts" / self.scenario_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (label or source.name)
        if source.is_file():
            shutil.copy2(source, target)
        else:
            target = source
        relative = str(target.relative_to(self.report_dir)) if target.is_relative_to(self.report_dir) else str(target)
        self.artifacts.append(relative)
        return relative

    def snapshot(self, name: str = "devices.json") -> str | None:
        target = self.report_dir / "artifacts" / self.scenario_id / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.hub.snapshot(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        relative = str(target.relative_to(self.report_dir))
        self.artifacts.append(relative)
        return relative

    def capture_failure(self, traceback_text: str) -> None:
        try:
            if self._screenshots_enabled():
                self.screenshot("failure")
            self.snapshot("devices-failure.json")
            settings_path = self.report_dir / "artifacts" / self.scenario_id / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(self.settings.as_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.artifacts.append(str(settings_path.relative_to(self.report_dir)))
        except Exception:
            pass
        self.checks.append(
            {"name": "失败 traceback", "ok": False, "actual": traceback_text[-4000:], "expected": ""}
        )

    def _save_grab(self, widget, tag: str) -> str | None:
        target = self.report_dir / "screenshots" / f"{self.scenario_id}-{tag}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        pixmap = widget.grab()
        if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
            from PyQt6.QtGui import QPixmap

            fallback = QPixmap(widget.size())
            widget.render(fallback)
            pixmap = fallback
        if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
            return None
        if not pixmap.save(str(target), "PNG"):
            return None
        return str(target.relative_to(self.report_dir))

    def _grab_dialog(self, dialog, tag: str) -> None:
        try:
            path = self._save_grab(dialog, tag)
            if path:
                self.screenshots.append(path)
        except Exception:
            pass

    def _screenshots_enabled(self) -> bool:
        return self.screenshot_mode != "none"

    def _modal_safety(self) -> None:
        active = QApplication.activeModalWidget()
        if active is None:
            self._modal_since = None
            return
        now = time.monotonic()
        if self._modal_since is None:
            self._modal_since = now
            return
        if now - self._modal_since > 3.0:
            self.modal.events.append(("safety_close", active.windowTitle()))
            active.close()
            self._modal_since = None

    # ---- 更新与安装桩 -------------------------------------------------

    def fetch_release(self):
        self.fetch_calls += 1
        if self.update_error:
            raise UpdateError(self.update_error)
        return self.release

    def download_release(self, release, work_dir, progress=None, cancel=None):
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / "PortHelperFluent-sim.exe"
        if self.download_error:
            raise UpdateError(self.download_error)
        payload = b"MZ simulated update payload"
        target.write_bytes(payload)
        if progress is not None:
            progress(0, len(payload))
            progress(len(payload), len(payload))
        return target

    def record_install(self, downloaded, target, work_dir):
        self.install_calls.append((Path(downloaded), Path(target), Path(work_dir)))
        return Path(work_dir) / "apply-update.ps1"

    def can_replace(self, target) -> bool:
        return bool(self.can_replace_flag)

    # ---- 线程与关闭 ---------------------------------------------------

    def thread_states(self) -> dict[str, bool]:
        states: dict[str, bool] = {}
        serial_page = self.window.pages.get("serial")
        worker = getattr(serial_page, "_worker", None)
        if worker is not None and hasattr(worker, "isRunning"):
            states["serial"] = bool(worker.isRunning())
        hid_page = self.window.pages.get("hid")
        worker = getattr(hid_page, "_worker", None)
        if worker is not None and hasattr(worker, "isRunning"):
            states["hid"] = bool(worker.isRunning())
        bt_page = self.window.pages.get("bluetooth")
        for name in ("_ble_worker", "_spp_worker"):
            worker = getattr(bt_page, name, None)
            if worker is not None and hasattr(worker, "thread_alive"):
                states[name] = bool(worker.thread_alive())
        settings_page = self.window.pages.get("settings")
        for name in ("_check_worker", "_download_worker"):
            worker = getattr(settings_page, name, None)
            if worker is not None and hasattr(worker, "isRunning"):
                states[name] = bool(worker.isRunning())
        worker = getattr(self.window, "_startup_update_worker", None)
        if worker is not None and hasattr(worker, "isRunning"):
            states["startup_update"] = bool(worker.isRunning())
        return states

    def wait_threads(self, timeout_ms: int = 6000) -> bool:
        return self.wait(lambda: not any(self.thread_states().values()), timeout_ms=timeout_ms)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._safety.stop()
        except Exception:
            pass
        try:
            self.settings.set("ui.confirm_exit", False)
            self.settings.save()
        except Exception:
            pass
        try:
            self.window.close()
            self.window.stop_workers()
        except Exception:
            pass
        self.qapp.processEvents()
        self.wait_threads(6000)
        states = {name: alive for name, alive in self.thread_states().items() if alive}
        if states:
            self.check_true("关闭后无后台线程存活", False, str(states))
        from PyQt6.QtCore import QThreadPool

        QThreadPool.globalInstance().waitForDone(3000)
        for widget in QApplication.topLevelWidgets():
            if widget is self.window:
                continue
            try:
                widget.close()
                # 有父窗口的对话框随父窗口销毁；这里只删除真正无父的顶层窗口，
                # 避免页面仍持有包装对象时 deleteLater 造成后续访问崩溃。
                if widget.parent() is None:
                    widget.deleteLater()
            except Exception:
                pass
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qapp.processEvents()
        gc.collect()
        try:
            from qfluentwidgets import setTheme, setThemeColor

            setTheme(self._orig_theme)
            setThemeColor(self._orig_color)
        except Exception:
            pass
        gc.collect()


def _short(value: Any, limit: int = 300) -> Any:
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).hex()
    else:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"

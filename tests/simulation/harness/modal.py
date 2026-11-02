"""统一接管模态框与系统桌面调用，避免离屏测试阻塞或触碰真实桌面。"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QFileDialog,
    QInputDialog,
    QMenu,
    QMessageBox,
)

from app.ui.dialogs.multi_send_dialog import MultiSendDialog
from app.ui.dialogs.session_dialog import SessionDialog


class ModalPolicy:
    def __init__(self) -> None:
        self.open_files: deque[str] = deque()
        self.save_files: deque[str] = deque()
        self.directories: deque[str] = deque()
        self.questions: deque[Any] = deque()
        self.texts: deque[tuple[str, bool]] = deque()
        self.colors: deque[QColor] = deque()
        self.menu_items: deque[str] = deque()
        self.urls: list[str] = []
        self.events: list[tuple[str, Any]] = []
        self.multi_send_handler: Callable[[Any], int] | None = None
        self.session_handler: Callable[[Any], int] | None = None
        self.dialog_screenshot: Callable[[Any, str], None] | None = None
        self._safety_timer = None

    # ---- 安装 ---------------------------------------------------------

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(self._open_file))
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(self._save_file))
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", staticmethod(self._existing_directory)
        )
        monkeypatch.setattr(QMessageBox, "question", staticmethod(self._question))
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(self._get_text))
        monkeypatch.setattr(QColorDialog, "getColor", staticmethod(self._get_color))
        monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(self._open_url))
        monkeypatch.setattr(
            QMenu, "exec", lambda menu, *args, **kwargs: self._menu_exec(menu, *args, **kwargs)
        )
        monkeypatch.setattr(MultiSendDialog, "exec", lambda dialog: self._multi_exec(dialog))
        monkeypatch.setattr(SessionDialog, "exec", lambda dialog: self._session_exec(dialog))

    # ---- 静态入口 -----------------------------------------------------

    def _open_file(self, *args, **kwargs):
        path = self.open_files.popleft() if self.open_files else ""
        self.events.append(("open_file", path))
        return (path, "所有文件 (*.*)")

    def _save_file(self, *args, **kwargs):
        path = self.save_files.popleft() if self.save_files else ""
        self.events.append(("save_file", path))
        return (path, "所有文件 (*.*)")

    def _existing_directory(self, *args, **kwargs):
        path = self.directories.popleft() if self.directories else ""
        self.events.append(("directory", path))
        return path

    def _question(self, *args, **kwargs):
        answer = (
            self.questions.popleft()
            if self.questions
            else QMessageBox.StandardButton.No
        )
        self.events.append(("question", answer))
        return answer

    def _get_text(self, *args, **kwargs):
        value = self.texts.popleft() if self.texts else ("", False)
        self.events.append(("text", value))
        return value

    def _get_color(self, *args, **kwargs):
        value = self.colors.popleft() if self.colors else QColor()
        self.events.append(("color", value))
        return value

    def _open_url(self, url) -> bool:
        text = url.toString() if isinstance(url, QUrl) else str(url)
        self.urls.append(text)
        self.events.append(("url", text))
        return True

    def _menu_exec(self, menu: QMenu, *args, **kwargs):
        wanted = self.menu_items.popleft() if self.menu_items else ""
        self.events.append(("menu", wanted))
        for action in menu.actions():
            if action.text() == wanted:
                return action
        return None

    # ---- 复杂对话框 ---------------------------------------------------

    def _multi_exec(self, dialog) -> int:
        self.events.append(("multi_send", dialog))
        if self.multi_send_handler is not None:
            return int(self.multi_send_handler(dialog) or 0)
        return self._show_and_close(dialog, "multi_send")

    def _session_exec(self, dialog) -> int:
        self.events.append(("session", dialog))
        if self.session_handler is not None:
            return int(self.session_handler(dialog) or 0)
        return self._show_and_close(dialog, "session")

    def _show_and_close(self, dialog, tag: str) -> int:
        dialog.show()
        QApplication.processEvents()
        if self.dialog_screenshot is not None:
            self.dialog_screenshot(dialog, tag)
        dialog.close()
        QApplication.processEvents()
        return 0

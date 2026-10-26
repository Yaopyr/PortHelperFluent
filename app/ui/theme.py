"""主题与 Mica：跟随系统/浅色/深色、强调色，以及 Win11 云母效果降级。"""

from __future__ import annotations

import sys

from PyQt6.QtGui import QColor
from qfluentwidgets import Theme, setTheme, setThemeColor

from app.core.logging_setup import get_logger

DEFAULT_ACCENT = "#0A84FF"
_WINDOWS_11_BUILD = 22000

# Qt 的主题枚举与配置里的字符串一一对应；未知值按“跟随系统”处理。
_THEME_MAP = {
    "auto": Theme.AUTO,
    "light": Theme.LIGHT,
    "dark": Theme.DARK,
}


def apply_theme(settings) -> None:
    """应用设置中的主题与强调色；值非法时退回跟随系统与默认强调色。"""
    name = str(settings.get("ui.theme", "auto")).strip().lower()
    theme = _THEME_MAP.get(name, Theme.AUTO)
    setTheme(theme)

    accent = str(settings.get("ui.accent", DEFAULT_ACCENT)).strip() or DEFAULT_ACCENT
    color = QColor(accent)
    if not color.isValid():
        get_logger().warning("强调色无效（%s），改用默认值 %s", accent, DEFAULT_ACCENT)
        color = QColor(DEFAULT_ACCENT)
    setThemeColor(color)


def enable_mica(window) -> bool:
    """在 Windows 11 上启用云母效果；不可用或失败时返回 False，绝不阻塞启动。"""
    build = _windows_build()
    if build is None or build < _WINDOWS_11_BUILD:
        return False
    try:
        window.setMicaEffectEnabled(True)
    except Exception:
        get_logger().exception("启用 Mica 效果失败，继续使用 Fluent 背景")
        return False
    return True


def _windows_build() -> int | None:
    """返回当前 Windows 构建号；非 Windows 或读取失败时返回 None。"""
    if not sys.platform.startswith("win"):
        return None
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return None

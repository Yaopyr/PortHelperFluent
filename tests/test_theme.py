"""主题应用测试：设置映射正确，并在测试结束后恢复全局 qconfig。"""

from __future__ import annotations

from pathlib import Path

from qfluentwidgets import Theme, qconfig, setTheme, setThemeColor

from app.core.settings import Settings
from app.ui.theme import DEFAULT_ACCENT, apply_theme


def test_apply_theme_maps_settings_and_restores_qconfig(qapp, tmp_path: Path) -> None:
    original_mode = qconfig.themeMode.value
    original_color = qconfig.themeColor.value
    settings = Settings(tmp_path / "config.json")
    settings.load()
    try:
        settings.set("ui.theme", "dark")
        settings.set("ui.accent", "#112233")
        apply_theme(settings)
        assert qconfig.themeMode.value == Theme.DARK
        assert qconfig.theme == Theme.DARK
        assert qconfig.themeColor.value.name().upper() == "#112233"

        settings.set("ui.theme", "light")
        settings.set("ui.accent", "#445566")
        apply_theme(settings)
        assert qconfig.themeMode.value == Theme.LIGHT
        assert qconfig.theme == Theme.LIGHT
        assert qconfig.themeColor.value.name().upper() == "#445566"

        settings.set("ui.theme", "auto")
        settings.set("ui.accent", "not-a-color")
        apply_theme(settings)
        # AUTO 会解析成实际明暗主题，但主题模式本身必须是 AUTO。
        assert qconfig.themeMode.value == Theme.AUTO
        assert qconfig.theme in (Theme.LIGHT, Theme.DARK)
        assert qconfig.themeColor.value.name().upper() == DEFAULT_ACCENT.upper()
    finally:
        setTheme(original_mode)
        setThemeColor(original_color)

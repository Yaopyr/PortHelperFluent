"""设置与关于页离屏测试：主题/强调色、行为偏好、目录入口与重置。"""

from __future__ import annotations

import gc
import json
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QMessageBox
from qfluentwidgets import Theme, qconfig

from app import APP_VERSION
from app.core.settings import DEFAULT_SETTINGS, Settings
from app.services.paths import AppPaths, resolve_paths
from app.ui.pages import settings_page as settings_page_module
from app.ui.pages.settings_page import SettingsPage


@pytest.fixture
def page(qapp, tmp_path: Path):
    paths: AppPaths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    settings = Settings(paths.config_file)
    settings.load()
    original_mode = qconfig.themeMode.value
    original_color = qconfig.themeColor.value
    widget = SettingsPage(settings, paths)
    widget.show()
    try:
        yield widget, settings, paths
    finally:
        widget.close()
        # 主题恢复会遍历 qfluentwidgets 的弱引用样式表；先回收已关闭的控件，
        # 避免 GC 在遍历期间删除条目（qfluentwidgets 的 WeakKeyDictionary 限制）。
        gc.collect()
        from qfluentwidgets import setTheme, setThemeColor

        setTheme(original_mode)
        setThemeColor(original_color)


def _saved(paths: AppPaths) -> dict:
    return json.loads(paths.config_file.read_text(encoding="utf-8"))


def test_page_metadata_and_controls(page) -> None:
    widget, _settings, paths = page

    assert widget.objectName() == "settings"
    assert widget.config_path_label.text() == str(paths.config_file)
    assert widget.log_path_label.text() == str(paths.log_dir)
    assert widget.config_path_label.isReadOnly() is True
    assert widget.log_path_label.isReadOnly() is True
    assert widget.theme_combo.count() == 3
    assert [widget.theme_combo.itemData(index) for index in range(3)] == [
        "auto",
        "light",
        "dark",
    ]
    assert widget.encoding_combo.count() == 3
    assert widget.max_lines_spin.minimum() == 1000
    assert widget.max_lines_spin.maximum() == 100000
    assert widget.log_font_spin.minimum() == 9
    assert widget.log_font_spin.maximum() == 24
    assert widget.accent_label.text() == DEFAULT_SETTINGS["ui.accent"]
    assert widget.auto_scroll_switch.isChecked() is True
    assert widget.confirm_exit_switch.isChecked() is True


def test_theme_switch_applies_and_persists(page) -> None:
    widget, settings, paths = page

    index = widget.theme_combo.findData("dark")
    widget.theme_combo.setCurrentIndex(index)

    assert settings.get("ui.theme") == "dark"
    assert qconfig.theme == Theme.DARK
    assert _saved(paths)["ui.theme"] == "dark"

    widget.theme_combo.setCurrentIndex(widget.theme_combo.findData("light"))
    assert qconfig.theme == Theme.LIGHT
    assert _saved(paths)["ui.theme"] == "light"


def test_accent_applies_and_persists(page) -> None:
    widget, settings, paths = page

    assert widget.apply_accent("#ff5a36") is True

    assert settings.get("ui.accent") == "#FF5A36"
    assert widget.accent_label.text() == "#FF5A36"
    assert qconfig.themeColor.value.name().upper() == "#FF5A36"
    assert _saved(paths)["ui.accent"] == "#FF5A36"

    assert widget.apply_accent("not-a-color") is False
    assert settings.get("ui.accent") == "#FF5A36"


def test_behaviour_controls_persist_and_round_trip(page) -> None:
    widget, settings, paths = page

    widget.encoding_combo.setCurrentIndex(widget.encoding_combo.findData("gbk"))
    widget.max_lines_spin.setValue(5000)
    widget.auto_scroll_switch.setChecked(False)
    widget.confirm_exit_switch.setChecked(False)
    widget.log_font_spin.setValue(12)

    assert settings.get("ui.default_encoding") == "gbk"
    assert settings.get("ui.max_display_lines") == 5000
    assert settings.get("ui.auto_scroll") is False
    assert settings.get("ui.confirm_exit") is False
    assert settings.get("ui.log_font_size") == 12

    reloaded = Settings(paths.config_file)
    reloaded.load()
    assert reloaded.get("ui.default_encoding") == "gbk"
    assert reloaded.get("ui.max_display_lines") == 5000
    assert reloaded.get("ui.auto_scroll") is False
    assert reloaded.get("ui.confirm_exit") is False
    assert reloaded.get("ui.log_font_size") == 12

    second = SettingsPage(reloaded, paths)
    try:
        assert second.encoding_combo.currentData() == "gbk"
        assert second.max_lines_spin.value() == 5000
        assert second.auto_scroll_switch.isChecked() is False
        assert second.confirm_exit_switch.isChecked() is False
        assert second.log_font_spin.value() == 12
    finally:
        second.close()


def test_invalid_numbers_are_clamped(page) -> None:
    widget, settings, _paths = page
    settings.set("ui.max_display_lines", 10)
    settings.set("ui.log_font_size", 100)

    widget.refresh_fields()

    assert widget.max_lines_spin.value() == 1000
    assert widget.log_font_spin.value() == 24
    # 刷新字段不应把收敛后的值写回配置。
    assert settings.get("ui.max_display_lines") == 10
    assert settings.get("ui.log_font_size") == 100


def test_reset_config_restores_defaults(page, monkeypatch) -> None:
    widget, settings, paths = page
    settings.set("ui.theme", "dark")
    settings.set("ui.max_display_lines", 4321)
    settings.set("ui.auto_scroll", False)
    settings.save()
    widget.refresh_fields()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    widget.reset_config_button.click()

    assert settings.as_dict() == DEFAULT_SETTINGS
    assert _saved(paths) == DEFAULT_SETTINGS
    assert widget.max_lines_spin.value() == DEFAULT_SETTINGS["ui.max_display_lines"]
    assert widget.auto_scroll_switch.isChecked() is True
    assert widget.theme_combo.currentData() == "auto"
    assert qconfig.themeMode.value == Theme.AUTO


def test_reset_config_can_be_cancelled(page, monkeypatch) -> None:
    widget, settings, _paths = page
    settings.set("ui.theme", "dark")
    widget.refresh_fields()
    questioned: list[bool] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: questioned.append(True)
        or QMessageBox.StandardButton.No,
    )

    widget.reset_config()

    assert questioned == [True]
    assert settings.get("ui.theme") == "dark"
    assert widget.theme_combo.currentData() == "dark"


def test_open_folder_buttons_call_desktop_services(page, monkeypatch) -> None:
    widget, _settings, paths = page
    opened: list[object] = []

    class FakeDesktopServices:
        @staticmethod
        def openUrl(url):
            opened.append(url)
            return True

    monkeypatch.setattr(settings_page_module, "QDesktopServices", FakeDesktopServices)

    widget.open_config_button.click()
    widget.open_log_button.click()

    assert len(opened) == 2
    assert Path(opened[0].toLocalFile()) == paths.config_file.parent
    assert Path(opened[1].toLocalFile()) == paths.log_dir


def test_export_config_writes_json(page, monkeypatch, tmp_path: Path) -> None:
    widget, settings, _paths = page
    target = tmp_path / "exported" / "config-backup"
    monkeypatch.setattr(
        settings_page_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(target), "JSON 文件 (*.json)")),
    )

    widget.export_config_button.click()

    exported = target.with_suffix(".json")
    assert exported.is_file()
    assert json.loads(exported.read_text(encoding="utf-8")) == settings.as_dict()


def test_about_card_lists_unsupported_items_without_urls(page) -> None:
    widget, _settings, _paths = page

    about = "\n".join(
        label.text()
        for label in (
            widget.version_label,
            widget.statement_label,
            widget.unsupported_label,
            widget.dependencies_label,
            widget.evidence_label,
        )
    )

    assert APP_VERSION in about
    assert "PortHelper V1.8.8" in about
    assert "温子祺" in about
    assert "独立 Python 复刻" in about
    for item in ("蓝牙", "内核串口监视", "CH372", "在线更新", "Keil 配色替换"):
        assert item in about
    assert "REA" in about
    assert "GPL-3.0" in about
    lowered = about.lower()
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "www." not in lowered
    assert "自动更新" in about


def test_settings_page_without_settings_or_paths_does_not_crash(qapp) -> None:
    widget = SettingsPage(None, None)
    try:
        assert widget.objectName() == "settings"
        assert widget.theme_combo.currentData() == "auto"
        assert widget.open_config_button.isEnabled() is False
        assert widget.has_active_work() is False
        widget.stop_workers()
    finally:
        widget.close()

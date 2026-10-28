"""设置与关于页离屏测试：主题/强调色、行为偏好、目录入口与重置。"""

from __future__ import annotations

import gc
import json
import time
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QMessageBox
from qfluentwidgets import Theme, qconfig

from app import APP_VERSION
from app.core.settings import DEFAULT_SETTINGS, Settings
from app.services.paths import AppPaths, resolve_paths
from app.services.updates import RELEASE_PAGE_URL, ReleaseAsset, ReleaseInfo
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
    for item in ("蓝牙", "内核串口监视", "CH372", "Keil 配色替换"):
        assert item in about
    assert "REA" in about
    assert "GPL-3.0" in about
    lowered = about.lower()
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "www." not in lowered
    # 在线更新已实现，不再出现在“不支持的功能”里。
    assert "在线更新" not in about
    assert "不提供自动更新" not in about


def test_update_card_controls_and_auto_check_persist(page) -> None:
    widget, settings, paths = page

    assert widget.current_version_label.text() == APP_VERSION
    assert widget.check_update_button.isEnabled() is True
    assert widget.update_status_label.text() == settings_page_module.UPDATE_IDLE_TEXT
    assert widget.update_notes_edit.isHidden() is True
    assert widget.update_notes_edit.isReadOnly() is True
    assert widget.update_progress.isHidden() is True
    assert widget.install_update_button.isHidden() is True
    assert widget.auto_check_switch.isChecked() is True

    widget.auto_check_switch.setChecked(False)

    assert settings.get("ui.check_updates_on_start") is False
    assert _saved(paths)["ui.check_updates_on_start"] is False

    widget.refresh_fields()

    assert widget.auto_check_switch.isChecked() is False


def test_update_check_shows_newer_release_and_hides_when_current(page) -> None:
    widget, _settings, _paths = page
    notes = "更新说明" * (settings_page_module.UPDATE_NOTES_MAX_CHARS // 2)
    release = ReleaseInfo(
        version="99.0.0",
        tag="v99.0.0",
        title="PortHelper Fluent 99.0.0",
        notes=notes,
        page_url=RELEASE_PAGE_URL,
        published_at="2026-10-28T08:00:00Z",
        exe=ReleaseAsset(
            name="PortHelperFluent.exe",
            url="https://github.com/Yaopyr/PortHelperFluent/releases/download/v99.0.0/exe",
            size=12 * 1024 * 1024,
            digest="",
        ),
        checksum=None,
    )

    widget._on_update_checked(release)

    status = widget.update_status_label.text()
    assert "99.0.0" in status
    assert "12.0 MB" in status
    assert "2026-10-28" in status
    assert widget.update_notes_edit.isHidden() is False
    assert widget.update_notes_edit.toPlainText() == notes[: settings_page_module.UPDATE_NOTES_MAX_CHARS]
    assert widget.install_update_button.isHidden() is False
    assert widget.install_update_button.isEnabled() is True
    assert widget._release is release

    widget._on_update_checked(
        ReleaseInfo(
            version=APP_VERSION,
            tag=f"v{APP_VERSION}",
            title="当前版本",
            notes="",
            page_url=RELEASE_PAGE_URL,
            published_at="2026-10-28T08:00:00Z",
            exe=None,
            checksum=None,
        )
    )

    assert widget.update_status_label.text() == f"已是最新版本（{APP_VERSION}）。"
    assert widget.update_notes_edit.isHidden() is True
    assert widget.install_update_button.isHidden() is True
    assert widget._release is None


def test_update_check_failure_reports_message(page) -> None:
    widget, _settings, _paths = page
    message = "无法连接 GitHub，请检查网络或代理设置。"
    widget._on_update_checked(
        ReleaseInfo(
            version="99.0.0",
            tag="v99.0.0",
            title="",
            notes="旧结果",
            page_url=RELEASE_PAGE_URL,
            published_at="",
            exe=None,
            checksum=None,
        )
    )

    widget._on_update_failed(message)

    assert widget.update_status_label.text() == f"无法检查更新：{message}"
    # 失败后不保留上一次的更新说明与安装按钮。
    assert widget._release is None
    assert widget.update_notes_edit.isHidden() is True
    assert widget.update_notes_edit.toPlainText() == ""
    assert widget.install_update_button.isHidden() is True

    # 启动静默检查失败时只改状态文字，不弹提示。
    widget._on_update_failed("限流", manual=False)

    assert widget.update_status_label.text() == "无法检查更新：限流"


def test_install_click_ignored_while_downloading(page, monkeypatch) -> None:
    widget, _settings, _paths = page
    widget._on_update_checked(
        ReleaseInfo(
            version="99.0.0",
            tag="v99.0.0",
            title="",
            notes="",
            page_url=RELEASE_PAGE_URL,
            published_at="",
            exe=ReleaseAsset(
                name="PortHelperFluent.exe",
                url="https://github.com/Yaopyr/PortHelperFluent/releases/download/exe",
                size=1024,
                digest="",
            ),
            checksum=None,
        )
    )

    class RunningDownloadWorker:
        def isRunning(self) -> bool:
            return True

    widget._download_worker = RunningDownloadWorker()
    created: list[bool] = []
    monkeypatch.setattr(
        settings_page_module,
        "UpdateDownloadWorker",
        lambda *args, **kwargs: created.append(True),
    )

    widget._on_install_clicked()

    assert created == []
    assert widget.update_status_label.text().startswith("发现新版本 99.0.0")


def test_stop_workers_does_not_wait_forever(page) -> None:
    widget, _settings, _paths = page
    calls: list[str] = []

    class SlowWorker:
        def requestInterruption(self) -> None:
            calls.append("interrupt")

        def wait(self, ms) -> bool:
            calls.append(f"wait:{ms}")
            return False

    widget._check_worker = SlowWorker()
    widget._download_worker = SlowWorker()

    started = time.monotonic()
    widget.stop_workers()
    elapsed = time.monotonic() - started

    # wait 超时也只等一次，不重试、不阻塞关闭。
    assert calls == ["interrupt", "wait:2000", "interrupt", "wait:2000"]
    assert elapsed < 1
    assert widget._check_worker is None
    assert widget._download_worker is None


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

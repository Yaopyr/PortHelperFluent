"""设置与在线更新场景（更新全部走注入桩，不联网、不执行 PowerShell）。"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from PyQt6.QtWidgets import QMessageBox
from qfluentwidgets import Theme, qconfig

from app import APP_VERSION
from app.core.settings import DEFAULT_SETTINGS
from app.services.updates import updates_dir
from app.ui.pages import settings_page as settings_page_module


def set_01_theme_and_accent(sim) -> None:
    page = sim.switch("settings")
    page.theme_combo.setCurrentIndex(page.theme_combo.findData("dark"))
    sim.check("深色主题生效", qconfig.theme, Theme.DARK)
    sim.check("主题持久化", sim.settings.get("ui.theme"), "dark")
    page.theme_combo.setCurrentIndex(page.theme_combo.findData("light"))
    sim.check("浅色主题生效", qconfig.theme, Theme.LIGHT)
    sim.check_true("强调色应用", page.apply_accent("#ff5a36") is True, "强调色被拒绝")
    sim.check("强调色持久化", sim.settings.get("ui.accent"), "#FF5A36")
    sim.check("强调色标签", page.accent_label.text(), "#FF5A36")
    sim.check_true("非法强调色拒绝", page.apply_accent("not-a-color") is False, "非法颜色被接受")
    sim.screenshot("settings-theme")


def set_02_behaviour_and_clamp(sim) -> None:
    page = sim.switch("settings")
    page.encoding_combo.setCurrentIndex(page.encoding_combo.findData("gbk"))
    page.max_lines_spin.setValue(5000)
    page.auto_scroll_switch.setChecked(False)
    page.confirm_exit_switch.setChecked(False)
    page.log_font_spin.setValue(12)
    sim.check("默认编码持久化", sim.settings.get("ui.default_encoding"), "gbk")
    sim.check("显示行数持久化", sim.settings.get("ui.max_display_lines"), 5000)
    sim.check("自动滚动持久化", sim.settings.get("ui.auto_scroll"), False)
    sim.check("退出确认持久化", sim.settings.get("ui.confirm_exit"), False)
    sim.check("日志字号持久化", sim.settings.get("ui.log_font_size"), 12)

    sim.settings.set("ui.max_display_lines", 10)
    sim.settings.set("ui.log_font_size", 100)
    page.refresh_fields()
    sim.check("显示行数 clamp", page.max_lines_spin.value(), 1000)
    sim.check("日志字号 clamp", page.log_font_spin.value(), 24)
    sim.check("clamp 不回写配置", sim.settings.get("ui.max_display_lines"), 10)
    sim.screenshot("settings-behaviour")


def set_03_config_export_reset_and_folders(sim) -> None:
    page = sim.switch("settings")
    page.confirm_exit_switch.setChecked(True)
    target = sim.tmp_path / "exported" / "config-backup.json"
    sim.modal.save_files.append(str(target))
    page.export_config_button.click()
    sim.check_true("配置导出文件存在", target.is_file(), str(target))
    if target.is_file():
        sim.check("配置导出内容", json.loads(target.read_text(encoding="utf-8")), sim.settings.as_dict())
        sim.artifact(target, "config-export.json")

    page.theme_combo.setCurrentIndex(page.theme_combo.findData("dark"))
    sim.modal.questions.append(QMessageBox.StandardButton.No)
    page.reset_config_button.click()
    sim.check("取消重置不改主题", sim.settings.get("ui.theme"), "dark")
    sim.modal.questions.append(QMessageBox.StandardButton.Yes)
    page.reset_config_button.click()
    sim.check("重置恢复默认", sim.settings.as_dict(), DEFAULT_SETTINGS)
    sim.check("重置后主题", page.theme_combo.currentData(), "auto")

    page.open_config_button.click()
    page.open_log_button.click()
    urls = [url.replace("\\", "/") for url in sim.modal.urls]
    sim.check_true(
        "打开配置目录",
        any(str(sim.paths.config_file.parent).replace("\\", "/") in url for url in urls),
        str(sim.modal.urls),
    )
    sim.check_true(
        "打开日志目录",
        any(str(sim.paths.log_dir).replace("\\", "/") in url for url in urls),
        str(sim.modal.urls),
    )
    sim.screenshot("settings-config")


def _check_newer(sim, page) -> None:
    sim.release = replace(
        sim.release,
        version="9.9.9",
        tag="v9.9.9",
        exe=replace(sim.release.exe) if sim.release.exe is not None else None,
    )
    sim.update_error = None
    page.check_update_button.click()
    sim.check_true(
        "检查到新版本",
        sim.wait(lambda: "9.9.9" in page.update_status_label.text(), 5000),
        page.update_status_label.text(),
    )
    sim.check_true("安装按钮可见", page.install_update_button.isVisible(), "安装按钮不可见")


def set_04_update_check(sim) -> None:
    page = sim.switch("settings")
    _check_newer(sim, page)
    sim.check_in("更新说明可见", "模拟更新说明", page.update_notes_edit.toPlainText())

    page.auto_check_switch.setChecked(False)
    sim.check("自动检查持久化", sim.settings.get("ui.check_updates_on_start"), False)
    page.auto_check_switch.setChecked(True)

    sim.release = replace(sim.release, version=APP_VERSION, tag=f"v{APP_VERSION}", exe=None)
    page.check_update_button.click()
    sim.check_true(
        "已是最新版本",
        sim.wait(lambda: "已是最新版本" in page.update_status_label.text(), 5000),
        page.update_status_label.text(),
    )
    sim.check_true("最新版本隐藏安装按钮", page.install_update_button.isHidden(), "安装按钮仍可见")

    sim.update_error = "模拟网络错误"
    page.check_update_button.click()
    sim.check_true(
        "更新检查失败",
        sim.wait(lambda: "无法检查更新" in page.update_status_label.text(), 5000),
        page.update_status_label.text(),
    )
    sim.update_error = None

    page.auto_check_switch.setChecked(True)
    counts_before = len(sim.install_calls)
    fetches_before = sim.fetch_calls
    sim.window.schedule_update_check()
    sim.check_true(
        "启动静默检查完成",
        sim.wait(lambda: sim.fetch_calls > fetches_before, 5000),
        f"fetch_calls={sim.fetch_calls}",
    )
    sim.check("启动检查不触发安装", len(sim.install_calls), counts_before)
    sim.screenshot("settings-update-check")


def set_05_download_success_and_failure(sim) -> None:
    page = sim.switch("settings")
    _check_newer(sim, page)
    sim.download_error = None
    page.install_update_button.click()
    sim.check_true(
        "更新下载完成",
        sim.wait(lambda: "更新包已下载" in page.update_status_label.text(), 8000),
        page.update_status_label.text(),
    )
    directory = updates_dir(sim.paths)
    downloaded = list(directory.glob("PortHelperFluent-sim.exe"))
    sim.check_true("更新包文件存在", bool(downloaded), str(list(directory.iterdir())))
    if downloaded:
        sim.artifact(downloaded[0], "update-download.exe")
    sim.check_true(
        "打开更新目录",
        any(str(directory).replace("\\", "/") in url.replace("\\", "/") for url in sim.modal.urls),
        str(sim.modal.urls),
    )

    sim.download_error = "模拟下载失败"
    page.install_update_button.click()
    sim.check_true(
        "更新下载失败",
        sim.wait(lambda: "下载更新失败" in page.update_status_label.text(), 8000),
        page.update_status_label.text(),
    )
    sim.download_error = None
    sim.screenshot("settings-download")


def set_06_install_paths(sim) -> None:
    page = sim.switch("settings")
    _check_newer(sim, page)
    sim.monkeypatch.setattr(
        settings_page_module,
        "sys",
        SimpleNamespace(frozen=True, executable=str(sim.fake_exe)),
    )
    sim.can_replace_flag = False
    page.install_update_button.click()
    sim.wait(lambda: False, 300)
    sim.check("不可替换不调用安装", sim.install_calls, [])
    sim.check_true("不可替换不启动下载", page.update_progress.isHidden(), "下载进度被显示")
    sim.check_true("不可替换后按钮仍可用", page.install_update_button.isEnabled(), "安装按钮被禁用")

    sim.can_replace_flag = True
    sim.modal.questions.append(QMessageBox.StandardButton.No)
    page.install_update_button.click()
    sim.check_true(
        "安装确认 No 后保留窗口",
        sim.wait(lambda: "稍后可重新安装" in page.update_status_label.text(), 8000),
        page.update_status_label.text(),
    )
    sim.check("No 不调用安装", sim.install_calls, [])
    sim.check_true("No 后窗口仍打开", sim.window.isVisible(), "窗口被关闭")

    sim.modal.questions.append(QMessageBox.StandardButton.Yes)
    page.install_update_button.click()
    sim.check_true(
        "安装 Yes 调用替换桩",
        sim.wait(lambda: bool(sim.install_calls), 8000),
        page.update_status_label.text(),
    )
    sim.check_true("安装后窗口关闭", sim.wait(lambda: not sim.window.isVisible(), 5000), "窗口未关闭")

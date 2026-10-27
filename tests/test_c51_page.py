"""C51 向导页离屏测试：参数联动、预览、导出与配置持久化。"""

from __future__ import annotations

import json

import pytest
from PyQt6.QtWidgets import QApplication

from app.core.settings import Settings
from app.services.paths import resolve_paths
from app.ui.pages import c51_page as c51_page_module
from app.ui.pages.c51_page import C51Highlighter, C51Page

_CREATED_WIDGETS: list = []


@pytest.fixture(autouse=True)
def _dispose_widgets(qapp):
    yield
    from PyQt6.QtCore import QCoreApplication, QEvent

    while _CREATED_WIDGETS:
        widget = _CREATED_WIDGETS.pop()
        try:
            widget.close()
            widget.deleteLater()
        except RuntimeError:
            pass
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def _make_page(tmp_path, monkeypatch):
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    settings = Settings(paths.config_file)
    settings.load()
    errors: list[tuple] = []
    successes: list[tuple] = []
    monkeypatch.setattr(c51_page_module, "show_error", lambda *args: errors.append(args))
    monkeypatch.setattr(
        c51_page_module, "show_success", lambda *args: successes.append(args)
    )
    page = C51Page(settings, paths)
    _CREATED_WIDGETS.append(page)
    page.show()
    return page, settings, paths, errors, successes


def _configure_50ms(page) -> None:
    page.crystal_spin.setValue(12.0)
    page.timer_mode_combos[0].setCurrentIndex(1)
    page.timer_time_spins[0].setValue(50_000)
    page.timer_run_checks[0].setChecked(True)
    page.generate()


# ---- 页面基础 -----------------------------------------------------------


def test_page_metadata_and_lifecycle(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    assert page.objectName() == "c51"
    assert page.title_label.text() == "C51 向导"
    assert "8051" in page.subtitle_label.text()
    assert page.has_active_work() is False
    assert page.stop_workers() is None
    assert isinstance(page.highlighter, C51Highlighter)
    assert QApplication.activeModalWidget() is None


def test_generate_timer_registers_in_preview(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, _successes = _make_page(tmp_path, monkeypatch)
    _configure_50ms(page)
    text = page.code_preview.toPlainText()
    assert "TH0 = 0x3C" in text
    assert "TL0 = 0xB0" in text
    assert "TMOD = 0x" in text
    assert "0x3C" in page.timer_labels[0].text()
    assert "行 C 代码" in page.status_label.text()
    assert errors == []
    assert QApplication.activeModalWidget() is None


def test_preset_and_double_speed_switch(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.preset_buttons[0].click()
    assert abs(page.crystal_spin.value() - 11.0592) < 1e-6
    assert abs(page.current_config().crystal_mhz - 11.0592) < 1e-6

    page.crystal_spin.setValue(12.0)
    page.double_speed_switch.setChecked(True)
    page.timer_time_spins[0].setValue(1000)
    page.timer_run_checks[0].setChecked(True)
    page.generate()
    text = page.code_preview.toPlainText()
    assert page.current_config().double_speed is True
    assert "TH0 = 0xF8" in text and "TL0 = 0x30" in text


# ---- 导出 ---------------------------------------------------------------


def test_export_c_and_keil_project(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, successes = _make_page(tmp_path, monkeypatch)
    _configure_50ms(page)
    page.set_output_dir(tmp_path / "out")

    written = page.export_c()
    assert written == tmp_path / "out" / "Main.c"
    assert written.is_file()
    assert "TH0 = 0x3C" in written.read_text(encoding="utf-8")

    keil_paths = page.export_keil(output_dir=tmp_path / "keil")
    assert [path.name for path in keil_paths] == [
        "Main.c",
        "M51Project.Uv2",
        "M51Project.Opt",
        "STARTUP.A51",
    ]
    assert all(path.is_file() for path in keil_paths)
    assert keil_paths[0].read_text(encoding="utf-8") == page.code_preview.toPlainText()
    uv2 = keil_paths[1].read_text(encoding="ascii")
    assert "Main.c" in uv2 and "STARTUP.A51" in uv2
    startup = keil_paths[3].read_text(encoding="ascii")
    assert "self-written" in startup.lower()
    assert errors == []
    assert QApplication.activeModalWidget() is None


def test_export_without_directory_reports_inline_error(
    qapp, tmp_path, monkeypatch
) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.output_dir_edit.setText("")
    with pytest.raises(ValueError, match="输出目录"):
        page.export_keil()
    assert page.error_label.isVisible() is True

    blocker = tmp_path / "blocker.txt"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(OSError, match="Keil 工程"):
        page.export_keil(output_dir=blocker / "sub")
    assert page.error_label.isVisible() is True
    assert QApplication.activeModalWidget() is None


# ---- 警告与联动 ---------------------------------------------------------


def test_overflow_warning_panel(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.crystal_spin.setValue(12.0)
    page.timer_time_spins[0].setValue(100_000)
    page.timer_run_checks[0].setChecked(True)
    page.generate()
    assert "超过" in page.warning_view.toPlainText()
    assert page.timer_warning_labels[0].isVisible() is True
    assert "超过" in page.timer_warning_labels[0].text()
    assert "0x00" in page.timer_labels[0].text()


def test_port_checkbox_updates_hex_and_code(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.port_checks["P1"][0].setChecked(True)
    page.port_checks["P1"][2].setChecked(True)
    assert page.port_hex_labels["P1"].text() == "0x05"
    assert page.port_value("P1") == 5
    assert page.current_config().ports["P1"] == 5
    assert "P1 = 0x05" in page.code_preview.toPlainText()

    page.set_port_value("P2", 0xA5)
    assert page.port_hex_labels["P2"].text() == "0xA5"
    assert page.current_config().ports["P2"] == 0xA5
    page.port_checks["P1"][0].setChecked(False)
    page.port_checks["P1"][2].setChecked(False)
    assert page.port_hex_labels["P1"].text() == "0x00"


def test_serial_baud_actual_label_and_warning(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.crystal_spin.setValue(11.0592)
    page.serial_mode_combo.setCurrentIndex(1)
    page.baud_spin.setValue(9600)
    page.smod_check.setChecked(False)
    page.generate()
    label = page.baud_label.text()
    assert "9600" in label and "0.00" in label
    assert "TH1=0xFD" in label

    page.baud_spin.setValue(115200)
    page.smod_check.setChecked(True)
    page.generate()
    assert "115200" in page.baud_label.text()
    assert "误差" in page.warning_view.toPlainText()


def test_interrupts_generate_isrs(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.interrupt_checks["et0"].setChecked(True)
    page.interrupt_checks["es"].setChecked(True)
    page.priority_combos["et0"].setCurrentIndex(1)
    page.generate()
    text = page.code_preview.toPlainText()
    assert "void timer0_isr(void) interrupt 1 using 1" in text
    assert "void uart_isr(void) interrupt 4" in text
    assert "void ext0_isr" not in text
    assert page.current_config().interrupts.high_priority == {"et0"}

    page.interrupt_checks["ex0"].setChecked(True)
    page.ext0_trigger_combo.setCurrentIndex(1)
    page.generate()
    assert "void ext0_isr(void) interrupt 0" in page.code_preview.toPlainText()
    assert page.current_config().interrupts.ext0_edge is True


def test_direct_reload_updates_timer_label(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.timer_reload_checks[0].setChecked(True)
    assert page.timer_reload_spins[0].isEnabled() is True
    assert page.timer_time_spins[0].isEnabled() is False
    page.timer_reload_spins[0].setValue(0xFE)
    page.timer_run_checks[0].setChecked(True)
    page.generate()
    text = page.code_preview.toPlainText()
    assert "TH0 = 0x00" in text and "TL0 = 0xFE" in text
    assert page.current_config().timers[0].reload == 0xFE


# ---- 配置持久化 ---------------------------------------------------------


def test_settings_round_trip(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, paths, _errors, _successes = _make_page(tmp_path, monkeypatch)
    page.crystal_spin.setValue(24.0)
    page.double_speed_switch.setChecked(True)
    page.set_port_value("P2", 0xA5)
    page.timer_mode_combos[0].setCurrentIndex(2)  # 方式 2
    page.timer_time_spins[0].setValue(250)
    page.timer_run_checks[0].setChecked(True)
    page.timer_reload_checks[1].setChecked(True)
    page.timer_reload_spins[1].setValue(0xFE)
    page.serial_mode_combo.setCurrentIndex(3)
    page.baud_spin.setValue(4800)
    page.smod_check.setChecked(True)
    page.generator_combo.setCurrentIndex(1)  # T2
    page.interrupt_checks["et0"].setChecked(True)
    page.interrupt_checks["es"].setChecked(True)
    page.priority_combos["et0"].setCurrentIndex(1)
    page.priority_combos["es"].setCurrentIndex(1)
    page.interrupt_checks["ex0"].setChecked(True)
    page.ext0_trigger_combo.setCurrentIndex(1)
    page.power_combo.setCurrentIndex(1)  # 空闲
    page.delay_spin.setValue(500)
    page.set_output_dir(tmp_path / "exports")
    page.save_settings()

    raw = json.loads(paths.config_file.read_text(encoding="utf-8"))
    assert raw["c51.crystal_mhz"] == 24.0
    assert raw["c51.double_speed"] is True
    assert raw["c51.ports"]["P2"] == 0xA5
    assert raw["c51.serial"]["baud"] == 4800
    assert raw["c51.serial"]["generator"] == "t2"
    assert sorted(raw["c51.interrupts"]["high_priority"]) == ["es", "et0"]
    assert isinstance(raw["c51.interrupts"]["high_priority"], list)

    reloaded = Settings(paths.config_file)
    reloaded.load()
    second = C51Page(reloaded, paths)
    _CREATED_WIDGETS.append(second)
    assert second.crystal_spin.value() == 24.0
    assert second.double_speed_switch.isChecked() is True
    assert second.port_value("P2") == 0xA5
    assert second.port_hex_labels["P2"].text() == "0xA5"
    assert second.timer_mode_combos[0].currentData() == 2
    assert second.timer_time_spins[0].value() == 250
    assert second.timer_run_checks[0].isChecked() is True
    assert second.timer_reload_checks[1].isChecked() is True
    assert second.timer_reload_spins[1].value() == 0xFE
    assert second.serial_mode_combo.currentData() == 3
    assert second.baud_spin.value() == 4800
    assert second.smod_check.isChecked() is True
    assert second.generator_combo.currentData() == "t2"
    assert second.interrupt_checks["et0"].isChecked() is True
    assert second.interrupt_checks["es"].isChecked() is True
    assert second.priority_combos["et0"].currentData() == "high"
    assert second.priority_combos["es"].currentData() == "high"
    assert second.interrupt_checks["ex0"].isChecked() is True
    assert second.ext0_trigger_combo.currentData() == "edge"
    assert second.power_combo.currentData() == "idle"
    assert second.delay_spin.value() == 500
    assert second.output_dir() == tmp_path / "exports"
    assert second.current_config() == page.current_config()
    assert QApplication.activeModalWidget() is None


# ---- 交互与生命周期 -----------------------------------------------------


def test_copy_code_and_buttons(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, successes = _make_page(tmp_path, monkeypatch)
    _configure_50ms(page)
    text = page.copy_code()
    assert text == page.code_preview.toPlainText()
    assert QApplication.clipboard().text() == text
    page.copy_button.click()
    assert successes
    assert errors == []
    assert QApplication.activeModalWidget() is None


def test_ctrl_s_exports_when_directory_is_set(qapp, tmp_path, monkeypatch) -> None:
    page, _settings, _paths, errors, successes = _make_page(tmp_path, monkeypatch)
    _configure_50ms(page)
    page.set_output_dir(tmp_path / "shortcut")
    page._on_save_shortcut()
    assert (tmp_path / "shortcut" / "Main.c").is_file()
    assert successes
    assert errors == []
    assert QApplication.activeModalWidget() is None

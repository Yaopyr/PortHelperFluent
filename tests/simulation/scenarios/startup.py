"""启动、退出与配置场景。"""

from __future__ import annotations

import json
import os
import site
import subprocess
import sys
from pathlib import Path

from app import APP_VERSION

EXPECTED_ROUTES = (
    "serial",
    "bluetooth",
    "net-client",
    "net-server",
    "hid",
    "checksum",
    "convert",
    "image",
    "dotmatrix",
    "c51",
    "settings",
)


def sys_01_main_window(sim) -> None:
    window = sim.window
    sim.check("页面数量", len(window.pages), len(EXPECTED_ROUTES))
    sim.check("页面路由与顺序", tuple(window.pages.keys()), EXPECTED_ROUTES)
    sim.check("默认页是串口", window.stackedWidget.currentWidget(), window.pages["serial"])
    sim.check_in("标题包含版本", APP_VERSION, window.windowTitle())
    sim.check_true("smoke_test 通过", bool(window.smoke_test()), "窗口页面未完整装配")
    for route in EXPECTED_ROUTES:
        page = sim.switch(route)
        sim.check_true(f"{route} 页面可见", page.isVisible(), route)
        sim.screenshot(route)
    sim.check("堆栈页面数", window.stackedWidget.count(), len(EXPECTED_ROUTES))


def sys_02_exit_confirmation(sim) -> None:
    from PyQt6.QtWidgets import QMessageBox

    window = sim.window
    serial = sim.switch("serial")
    serial.port_combo.setCurrentText("COM7")
    serial.open_button.click()
    sim.check_true("串口已打开", serial.has_active_work(), "打开虚拟串口失败")
    sim.settings.set("ui.confirm_exit", True)
    sim.settings.save()

    sim.modal.questions.append(QMessageBox.StandardButton.No)
    window.close()
    sim.qapp.processEvents()
    sim.check_true("选择 No 时窗口保持打开", window.isVisible(), "窗口被错误关闭")
    sim.check_true("选择 No 时串口仍打开", serial.has_active_work(), "后台任务被错误停止")

    sim.modal.questions.append(QMessageBox.StandardButton.Yes)
    window.close()
    sim.qapp.processEvents()
    sim.check_true("选择 Yes 后窗口关闭", not window.isVisible(), "窗口未关闭")
    sim.check_true("退出后串口线程停止", sim.wait_threads(5000), str(sim.thread_states()))


def _run_main(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )


def sys_03_selftest(sim) -> None:
    report = sim.tmp_path / "selftest.json"
    env = os.environ.copy()
    env["PYTHONUSERBASE"] = site.getuserbase()
    env["APPDATA"] = str(sim.tmp_path / "subprocess-appdata")
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    (sim.tmp_path / "subprocess-appdata").mkdir(parents=True, exist_ok=True)
    completed = _run_main(["--selftest", "--report", str(report)], env, Path.cwd())
    sim.check("selftest 退出码", completed.returncode, 0)
    sim.check_true("selftest 报告存在", report.is_file(), completed.stderr)
    if report.is_file():
        payload = json.loads(report.read_text(encoding="utf-8"))
        sim.check_true("selftest ok", bool(payload.get("ok")), str(payload)[:1000])
        sim.artifact(report, "selftest.json")


def sys_04_smoke_test(sim) -> None:
    env = os.environ.copy()
    env["PYTHONUSERBASE"] = site.getuserbase()
    env["APPDATA"] = str(sim.tmp_path / "smoke-appdata")
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PORTHELPER_NO_DIALOGS"] = "1"
    env["PORTHELPER_NO_UPDATE_CHECK"] = "1"
    (sim.tmp_path / "smoke-appdata").mkdir(parents=True, exist_ok=True)
    completed = _run_main(["--smoke-test"], env, Path.cwd())
    sim.check("smoke 退出码", completed.returncode, 0)
    log_path = sim.tmp_path / "smoke-appdata" / "PortHelperFluent" / "logs" / "app.log"
    sim.check_true("smoke 日志存在", log_path.is_file(), str(log_path))
    if log_path.is_file():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        sim.check_in("smoke 完成日志", "启动冒烟测试完成", log_text)
        sim.artifact(log_path, "smoke-app.log")


def sys_05_corrupt_config(sim) -> None:
    from app.core.settings import Settings

    config = sim.tmp_path / "corrupt" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{ not json", encoding="utf-8")
    settings = Settings(config)
    settings.load()
    sim.check_true("损坏配置产生 warning", bool(settings.warning), "未报告配置损坏")
    backups = list(config.parent.glob("config.bad-*.json"))
    sim.check_true("损坏配置已备份", bool(backups), str(list(config.parent.iterdir())))
    sim.check("损坏配置回退默认值", settings.get("ui.theme"), "auto")
    sim.artifact(config, "config-corrupt.json")

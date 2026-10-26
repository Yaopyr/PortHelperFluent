import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _selftest_env() -> dict[str, str]:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def test_selftest_cli_writes_ok_report(tmp_path: Path) -> None:
    report_path = tmp_path / "reports" / "selftest.json"

    result = subprocess.run(
        [sys.executable, "main.py", "--selftest", "--report", str(report_path)],
        cwd=REPO_ROOT,
        env=_selftest_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["errors"] == []
    assert payload["python"] == sys.version.split()[0]
    assert payload["qt"]
    assert payload["qfluentwidgets"] == version("PyQt6-Fluent-Widgets")
    assert payload["version"] == "1.0.0"
    assert set(payload) == {"ok", "errors", "python", "qt", "qfluentwidgets", "version"}


def test_selftest_cli_creates_missing_report_directory(tmp_path: Path) -> None:
    report_path = tmp_path / "a" / "b" / "selftest.json"

    result = subprocess.run(
        [sys.executable, "main.py", "--selftest", "--report", str(report_path)],
        cwd=REPO_ROOT,
        env=_selftest_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert report_path.is_file()


def test_selftest_reports_import_failure(tmp_path: Path, monkeypatch) -> None:
    import main

    monkeypatch.setattr(main, "REQUIRED_MODULES", ("no_such_module_for_selftest",))
    report_path = tmp_path / "report.json"

    exit_code = main.run_selftest(report_path)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["ok"] is False
    assert any("no_such_module_for_selftest" in error for error in payload["errors"])


def test_binding_check_accepts_current_install() -> None:
    from main import check_fluent_binding

    check_fluent_binding()


def test_placeholder_window_is_a_fluent_window(qapp) -> None:
    from PyQt6.QtWidgets import QWidget
    from qfluentwidgets import FluentWindow

    from main import build_placeholder_window

    window = build_placeholder_window()

    assert isinstance(window, FluentWindow)
    assert isinstance(window, QWidget)
    assert "PortHelper" in window.windowTitle()
    window.close()

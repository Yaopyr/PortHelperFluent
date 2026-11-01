import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyqt6")
sys.dont_write_bytecode = True

import pytest


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path, monkeypatch):
    """所有测试的 %APPDATA% 都指向本次 tmp_path，绝不污染真实用户目录。

    需要验证“默认路径”的测试可以继续调用 `resolve_paths()`：它读到的是这个临时
    目录；需要真实目录的测试请显式注入路径。单个测试还可以再 monkeypatch 覆盖。
    """
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    return appdata


@pytest.fixture(autouse=True)
def reset_qt_message_handler():
    """测试可能安装全局 Qt 消息处理器；结束后必须卸载，避免污染后续测试。"""
    yield
    from app.ui.qt_utils import uninstall_qt_message_handler

    uninstall_qt_message_handler()


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def cli(capsys):
    """调用 CLI 入口并返回信封 dict（附 ``exit_code`` 键）。

    供所有 CLI 命令测试使用：``cli(["checksum", "--text", "123456789"])["data"]``。
    非 JSON 输出放在 ``raw`` 键下；流式命令的多行 NDJSON 取最后一行的信封。
    """

    def _run(argv):
        # 惰性导入：本 fixture 被任何 CLI 测试使用时才加载 CLI 入口。
        from app.cli.main import run

        code = run(list(argv))
        out = capsys.readouterr().out
        text = out.strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                lines = [line for line in text.splitlines() if line.strip()]
                payload = json.loads(lines[-1]) if lines else {}
                payload["raw"] = out
            payload["exit_code"] = code
            return payload
        return {"raw": out, "exit_code": code}

    return _run

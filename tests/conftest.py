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


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app

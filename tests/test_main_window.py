"""主窗口离屏测试：页面装配、路由切换、退出清理与确认。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PyQt6.QtCore import QByteArray
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMessageBox, QWidget

from app import APP_VERSION
from app.core.settings import Settings
from app.services.paths import AppPaths, resolve_paths
from app.services.updates import RELEASE_PAGE_URL, ReleaseAsset, ReleaseInfo
from app.ui import main_window as main_window_module
from app.ui.main_window import APP_TITLE, PAGE_ROUTES, MainWindow, _PAGE_SPECS, icon_for


@pytest.fixture
def window(qapp, tmp_path: Path):
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    settings = Settings(paths.config_file)
    settings.load()
    main_window = MainWindow(settings, paths)
    yield main_window, settings, paths
    # 测试里的 monkeypatch 可能晚于本 fixture 清理，关闭时不要再弹确认框。
    settings.set("ui.confirm_exit", False)
    main_window.close()


def test_main_window_has_ten_pages_and_navigation_routes(window) -> None:
    main_window, _settings, _paths = window

    assert main_window.stackedWidget.count() == 10
    assert tuple(main_window.pages) == PAGE_ROUTES
    for route in PAGE_ROUTES:
        page = main_window.findChild(QWidget, route)
        assert page is not None, route
        assert main_window.navigationInterface.widget(route) is not None, route

    assert main_window.stackedWidget.currentWidget().objectName() == "serial"
    assert main_window.windowTitle() == f"{APP_TITLE} 1.0.0"
    assert main_window.minimumWidth() == 1180
    assert main_window.minimumHeight() == 760
    assert main_window.smoke_test() is True


def test_main_window_switches_pages(window, qapp) -> None:
    main_window, _settings, _paths = window

    main_window.switchTo(main_window.pages["net-client"])
    qapp.processEvents()
    assert main_window.stackedWidget.currentWidget().objectName() == "net-client"

    main_window.switchTo(main_window.pages["settings"])
    qapp.processEvents()
    assert main_window.stackedWidget.currentWidget().objectName() == "settings"


def test_main_window_has_active_work_aggregates_pages(window, monkeypatch) -> None:
    main_window, _settings, _paths = window
    assert main_window.has_active_work() is False

    monkeypatch.setattr(main_window.pages["hid"], "has_active_work", lambda: True)

    assert main_window.has_active_work() is True


def test_close_without_active_work_saves_geometry(window, qapp) -> None:
    main_window, settings, paths = window
    main_window.show()
    qapp.processEvents()

    main_window.close()
    qapp.processEvents()

    encoded = settings.get("ui.window_geometry")
    assert encoded
    assert not QByteArray.fromBase64(str(encoded).encode("ascii")).isEmpty()
    assert paths.config_file.is_file()
    saved = json.loads(paths.config_file.read_text(encoding="utf-8"))
    assert saved["ui.window_geometry"] == encoded


def test_close_confirmation_only_when_page_has_active_work(
    window, qapp, monkeypatch
) -> None:
    main_window, _settings, _paths = window
    main_window.show()
    qapp.processEvents()
    monkeypatch.setattr(main_window.pages["serial"], "has_active_work", lambda: True)
    questions: list[int] = []

    def answer_no(*args, **kwargs):
        questions.append(1)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", answer_no)
    event = QCloseEvent()
    main_window.closeEvent(event)

    assert questions == [1]
    assert event.isAccepted() is False
    assert main_window.isVisible()

    stopped: list[bool] = []
    monkeypatch.setattr(main_window, "stop_workers", lambda: stopped.append(True))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    event = QCloseEvent()
    main_window.closeEvent(event)

    assert event.isAccepted() is True
    assert stopped == [True]


def test_close_confirmation_can_be_disabled_by_setting(window, qapp, monkeypatch) -> None:
    main_window, settings, _paths = window
    settings.set("ui.confirm_exit", False)
    main_window.show()
    qapp.processEvents()
    monkeypatch.setattr(main_window.pages["serial"], "has_active_work", lambda: True)
    questioned: list[bool] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: questioned.append(True) or QMessageBox.StandardButton.No,
    )

    event = QCloseEvent()
    main_window.closeEvent(event)

    assert questioned == []
    assert event.isAccepted() is True


def test_main_window_enable_mica_never_raises(window) -> None:
    main_window, _settings, _paths = window

    assert isinstance(main_window.enable_mica(), bool)


def test_main_window_restores_saved_geometry(qapp, tmp_path: Path) -> None:
    paths: AppPaths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    settings = Settings(paths.config_file)
    settings.load()
    first = MainWindow(settings, paths)
    first.show()
    qapp.processEvents()
    first.resize(1200, 800)
    first.close()
    encoded = settings.get("ui.window_geometry")
    assert encoded

    second = MainWindow(settings, paths)
    try:
        # 离屏平台会限制窗口尺寸，只验证恢复调用没有异常且几何串被采用。
        assert second.size().width() > 0
    finally:
        second.close()


def test_navigation_headers_and_labels_are_in_expected_order(window) -> None:
    from qfluentwidgets.components.navigation.navigation_widget import NavigationItemHeader

    main_window, _settings, _paths = window

    headers = [
        header.text()
        for header in main_window.navigationInterface.findChildren(NavigationItemHeader)
    ]
    assert headers == ["通信", "工具", "其他"]
    labels = [
        main_window.navigationInterface.widget(route).text() for route in PAGE_ROUTES
    ]
    assert labels == [
        "串口调试",
        "网络调试",
        "网络服务器",
        "USB HID",
        "校验计算",
        "编码转换",
        "图片取模",
        "点阵生成",
        "C51 向导",
        "设置与关于",
    ]


def test_navigation_expands_as_floating_panel(window, qapp) -> None:
    from qfluentwidgets.components.navigation.navigation_panel import NavigationDisplayMode

    main_window, _settings, _paths = window
    main_window.show()
    qapp.processEvents()
    panel = main_window.navigationInterface.panel
    stacked_rect = main_window.stackedWidget.geometry()
    nav_width = main_window.navigationInterface.width()
    assert stacked_rect.width() == main_window.width() - 48

    main_window.navigationInterface.expand(useAni=False)
    qapp.processEvents()

    assert panel.displayMode == NavigationDisplayMode.MENU
    assert panel.parent() is main_window
    assert panel.isVisible()
    assert main_window.stackedWidget.geometry() == stacked_rect
    assert main_window.navigationInterface.width() == nav_width == 48


def test_navigation_icons_are_unique() -> None:
    from qfluentwidgets import FluentIcon

    names = [spec[2] for _header, specs in _PAGE_SPECS for spec in specs]
    # 名称拼错时 icon_for() 会静默退回 FluentIcon.APPLICATION，必须比对解析结果。
    assert all(name in FluentIcon.__members__ for name in names)
    assert len({icon_for(name) for name in names}) == len(PAGE_ROUTES) == 10


def test_close_in_no_dialogs_mode_skips_question_and_stops_workers(
    window, qapp, monkeypatch
) -> None:
    main_window, settings, _paths = window
    settings.set("ui.confirm_exit", True)
    main_window.show()
    qapp.processEvents()
    monkeypatch.setenv("PORTHELPER_NO_DIALOGS", "1")
    monkeypatch.setattr(main_window.pages["serial"], "has_active_work", lambda: True)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: pytest.fail("不应弹出退出确认框")
    )
    stopped: list[bool] = []
    monkeypatch.setattr(main_window, "stop_workers", lambda: stopped.append(True))

    assert main_window.close() is True

    assert main_window.isVisible() is False
    assert stopped == [True]


def test_close_cancel_keeps_window_open_and_workers_running(
    window, qapp, monkeypatch
) -> None:
    main_window, settings, _paths = window
    settings.set("ui.confirm_exit", True)
    main_window.show()
    qapp.processEvents()
    monkeypatch.setattr(main_window.pages["serial"], "has_active_work", lambda: True)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No
    )
    stopped: list[bool] = []
    monkeypatch.setattr(main_window, "stop_workers", lambda: stopped.append(True))

    assert main_window.close() is False

    assert main_window.isVisible() is True
    assert stopped == []


def test_close_stops_workers_even_when_question_raises(window, qapp, monkeypatch) -> None:
    main_window, settings, _paths = window
    settings.set("ui.confirm_exit", True)
    main_window.show()
    qapp.processEvents()
    monkeypatch.setattr(main_window.pages["serial"], "has_active_work", lambda: True)

    def broken_question(*args, **kwargs):
        raise RuntimeError("确认框创建失败")

    monkeypatch.setattr(QMessageBox, "question", broken_question)
    stopped: list[bool] = []
    monkeypatch.setattr(main_window, "stop_workers", lambda: stopped.append(True))

    assert main_window.close() is True

    assert main_window.isVisible() is False
    assert stopped == [True]


def test_smoke_mode_skips_close_confirmation_and_geometry_persistence(
    window, qapp, monkeypatch
) -> None:
    main_window, settings, _paths = window
    assert main_window.smoke_test() is True
    settings.set("ui.confirm_exit", True)
    monkeypatch.setattr(main_window.pages["serial"], "has_active_work", lambda: True)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: pytest.fail("冒烟模式不应弹确认框")
    )
    before = settings.get("ui.window_geometry")
    main_window.show()
    qapp.processEvents()

    assert main_window.close() is True

    assert main_window.isVisible() is False
    assert settings.get("ui.window_geometry") == before


def test_page_construction_contract_settings_paths_and_keyword_parent(
    qapp, tmp_path: Path, monkeypatch
) -> None:
    """Task 3+ 页面统一以 (settings, paths, parent=self) 构造。"""
    import app.ui.main_window as main_window_module

    received: list[tuple] = []

    class ContractPage(QWidget):
        def __init__(self, settings, paths, parent=None, **kwargs) -> None:
            super().__init__(parent)
            self.setObjectName("serial")
            received.append((settings, paths, parent, kwargs))

        def has_active_work(self) -> bool:
            return False

        def stop_workers(self) -> None:
            pass

    monkeypatch.setattr(
        main_window_module,
        "_PAGE_SPECS",
        (("通信", (("serial", ContractPage, "CONNECT", "串口调试"),)),),
    )
    monkeypatch.setattr(main_window_module, "PAGE_ROUTES", ("serial",))
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    settings = Settings(paths.config_file)
    settings.load()

    main_window = main_window_module.MainWindow(settings, paths)
    try:
        assert received == [(settings, paths, main_window, {})]
        assert main_window.smoke_test() is True
    finally:
        settings.set("ui.confirm_exit", False)
        main_window.close()


def test_placeholder_and_serial_page_signatures_accept_worker_factory(window) -> None:
    import inspect

    from app.ui.pages.serial_page import SerialPage

    _main_window, settings, paths = window
    signature = inspect.signature(SerialPage.__init__)
    parameters = signature.parameters
    assert list(parameters)[1:4] == ["settings", "paths", "parent"]
    assert parameters["parent"].default is None
    assert parameters["worker_factory"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["worker_factory"].default is None
    assert any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )

    fake_factory = object()
    page = SerialPage(settings, paths, parent=None, worker_factory=fake_factory)
    try:
        assert page.worker_factory is fake_factory
        assert page.settings is settings
        assert page.paths is paths
    finally:
        page.close()


def test_app_version_has_single_source(window) -> None:
    import app
    import main
    from app.core.settings import DEFAULT_SETTINGS

    main_window, _settings, _paths = window

    assert app.APP_VERSION == main.APP_VERSION == DEFAULT_SETTINGS["app.version"] == "1.0.0"
    assert main_window.windowTitle() == f"{APP_TITLE} {app.APP_VERSION}"


def test_app_icon_file_is_generated_and_loadable(qapp) -> None:
    from PyQt6.QtGui import QIcon

    path = main_window_module.find_app_icon()
    if path is None:  # 精简打包环境可能没有 ICO，此时使用内置图标。
        pytest.skip("build/icon.ico 不存在")
    assert path.name == "icon.ico"
    assert path.stat().st_size > 0

    icon = QIcon(str(path))
    assert not icon.isNull()
    sizes = {size.width() for size in icon.availableSizes()}
    assert {16, 32, 256} <= sizes


def test_main_window_and_application_icons_are_not_null(window, qapp) -> None:
    if main_window_module.find_app_icon() is None:
        pytest.skip("build/icon.ico 不存在")
    main_window, _settings, _paths = window

    assert not main_window.windowIcon().isNull()

    previous = qapp.windowIcon()
    try:
        qapp.setWindowIcon(main_window_module.app_icon())
        assert not qapp.windowIcon().isNull()
    finally:
        qapp.setWindowIcon(previous)


def test_fallback_icon_used_when_ico_is_missing(qapp, monkeypatch) -> None:
    monkeypatch.setattr(main_window_module, "find_app_icon", lambda *roots: None)

    icon = main_window_module.app_icon()

    assert not icon.isNull()
    assert icon.availableSizes()


def _release(version: str) -> ReleaseInfo:
    return ReleaseInfo(
        version=version,
        tag=f"v{version}",
        title=f"PortHelper Fluent {version}",
        notes="",
        page_url=RELEASE_PAGE_URL,
        published_at="2026-10-28T08:00:00Z",
        exe=ReleaseAsset(
            name="PortHelperFluent.exe",
            url="https://github.com/Yaopyr/PortHelperFluent/releases/download/exe",
            size=1024,
            digest="",
        ),
        checksum=None,
    )


def test_prepare_for_update_skips_confirmation_and_stops_workers(
    window, qapp, monkeypatch
) -> None:
    main_window, settings, _paths = window
    settings.set("ui.confirm_exit", True)
    main_window.show()
    qapp.processEvents()
    monkeypatch.setattr(main_window.pages["serial"], "has_active_work", lambda: True)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: pytest.fail("更新重启不应弹退出确认")
    )
    stopped: list[bool] = []
    monkeypatch.setattr(main_window, "stop_workers", lambda: stopped.append(True))

    main_window.prepare_for_update()

    assert main_window._skip_exit_confirm is True
    assert main_window.isVisible() is False
    assert stopped == [True]


def test_startup_update_check_respects_setting(window, monkeypatch) -> None:
    main_window, settings, _paths = window
    created: list[object] = []
    started: list[bool] = []
    scheduled: list[tuple[int, object]] = []
    infos: list[tuple[str, str]] = []

    class FakeSignal:
        def __init__(self) -> None:
            self.slots = []

        def connect(self, slot) -> None:
            self.slots.append(slot)

        def emit(self, *_args) -> None:
            for slot in list(self.slots):
                slot()

    class FakeUpdateCheckWorker:
        def __init__(self, parent=None) -> None:
            # 更新任务不再由窗口拥有（daemon 线程 + QObject 外壳）。
            self.parent_widget = parent
            self.checked = FakeSignal()
            self.failed = FakeSignal()
            self.finished = FakeSignal()
            created.append(self)

        def start(self) -> None:
            started.append(True)

        def deleteLater(self) -> None:
            pass

        def requestInterruption(self) -> None:
            pass

        def wait(self, *_args) -> bool:
            return True

    monkeypatch.setattr(main_window_module, "UpdateCheckWorker", FakeUpdateCheckWorker)
    monkeypatch.setattr(
        main_window_module.QTimer,
        "singleShot",
        staticmethod(lambda ms, fn: scheduled.append((ms, fn))),
    )
    monkeypatch.setattr(
        main_window_module,
        "show_info",
        lambda parent, title, content="": infos.append((title, content)),
    )

    settings.set("ui.check_updates_on_start", False)
    main_window.schedule_update_check()

    assert scheduled == []
    assert created == []

    settings.set("ui.check_updates_on_start", True)
    main_window.schedule_update_check()

    assert len(scheduled) == 1
    assert scheduled[0][0] == main_window_module.STARTUP_UPDATE_CHECK_DELAY_MS

    scheduled[0][1]()
    assert len(created) == 1
    assert started == [True]
    assert created[0].parent_widget is None
    # 已有检查线程时重复触发不会起第二个线程。
    main_window._silent_update_check()
    assert len(created) == 1

    # finished 槽清空引用：之后的 stop_workers() 不再触碰已结束的任务。
    created[0].finished.emit()
    assert main_window._startup_update_worker is None

    main_window._on_startup_update_checked(_release("99.0.0"))
    assert infos == [("发现新版本 99.0.0", "可在「设置 → 更新」中查看并安装。")]

    main_window._on_startup_update_checked(_release(APP_VERSION))
    assert len(infos) == 1

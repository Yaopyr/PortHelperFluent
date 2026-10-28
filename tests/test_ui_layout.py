"""页面布局/滚动原语的离屏测试：滚动条风格、最小尺寸与内容边距。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QWidget,
)
from qfluentwidgets import SingleDirectionScrollArea, SmoothScrollArea, SmoothScrollBar

from app.core.settings import Settings
from app.services.paths import resolve_paths
from app.ui.main_window import MainWindow
from app.ui.widgets.page_layout import (
    PAGE_MARGINS,
    PAGE_MIN_SIZE,
    PAGE_SPACING,
    PANEL_MIN_WIDTH,
    apply_page_minimum,
    page_content,
    scroll_area,
    scroll_container,
)


@pytest.fixture
def window(qapp, tmp_path: Path):
    """装配真实主窗口，用于逐页检查滚动容器与最小尺寸。"""
    paths = resolve_paths(exe_path=tmp_path / "exe", appdata=tmp_path / "appdata")
    settings = Settings(paths.config_file)
    settings.load()
    main_window = MainWindow(settings, paths)
    yield main_window
    # 清理时关掉退出确认，避免弹窗阻塞。
    settings.set("ui.confirm_exit", False)
    main_window.close()


def test_scroll_area_is_single_direction_scroll_area(qapp) -> None:
    area = scroll_area()
    assert isinstance(area, SingleDirectionScrollArea)
    assert area.widgetResizable()
    assert area.frameShape() == QFrame.Shape.NoFrame
    area.deleteLater()


def test_scroll_area_bar_policies(qapp, monkeypatch) -> None:
    """横向恒隐藏；纵向“按需出现”。

    `SingleDirectionScrollArea` 把两个原生滚动条策略都强制设为 AlwaysOff，真实意图
    只能通过 `SmoothScrollBar.setForceHidden` 表达。这里记录该方法的调用参数：
    没有调用（或调用了却没传对参数）都会失败，不依赖任何私有属性或可见性时序。
    """
    recorded: list[tuple[SmoothScrollBar, bool]] = []
    original = SmoothScrollBar.setForceHidden

    def _record(self, hidden: bool) -> None:
        recorded.append((self, hidden))
        original(self, hidden)

    monkeypatch.setattr(SmoothScrollBar, "setForceHidden", _record)

    area = scroll_area()

    assert [hidden for bar, hidden in recorded if bar is area.vScrollBar] == [False]
    assert [hidden for bar, hidden in recorded if bar is area.hScrollBar] == [True]
    assert area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    area.deleteLater()


def test_scroll_area_transparent_stylesheet(qapp) -> None:
    qss = scroll_area().styleSheet()
    assert "background: transparent" in qss
    assert "border: none" in qss


def test_scroll_area_min_width(qapp) -> None:
    area = scroll_area(min_width=360)
    assert area.minimumWidth() == 360
    area.deleteLater()


def test_scroll_container_wraps_content(qapp) -> None:
    host = QWidget()
    content = QWidget()
    area = scroll_container(host, content, min_width=360)
    assert isinstance(area, SingleDirectionScrollArea)
    assert area.widget() is content
    assert area.minimumWidth() == 360
    assert area.parent() is host
    host.deleteLater()


def test_page_content_margins_and_spacing(qapp) -> None:
    parent = QWidget()
    content, layout = page_content(parent)
    margins = layout.contentsMargins()
    assert (
        margins.left(),
        margins.top(),
        margins.right(),
        margins.bottom(),
    ) == PAGE_MARGINS
    assert layout.spacing() == PAGE_SPACING
    assert content.layout() is layout
    assert content.parent() is parent
    parent.deleteLater()


def test_apply_page_minimum(qapp) -> None:
    page = QWidget()
    apply_page_minimum(page)
    assert page.minimumSize() == PAGE_MIN_SIZE
    page.deleteLater()


def test_all_pages_use_shared_minimum_size(window) -> None:
    for route, page in window.pages.items():
        assert page.minimumSize() == PAGE_MIN_SIZE, route


def test_all_page_scroll_areas_are_fluent(window) -> None:
    for route, page in window.pages.items():
        areas = page.findChildren(QScrollArea)
        assert areas, route
        for area in areas:
            assert isinstance(
                area, (SingleDirectionScrollArea, SmoothScrollArea)
            ), f"{route}: {type(area).__name__}"


def test_side_panels_use_shared_min_width(window) -> None:
    for route in ("serial", "net-client", "net-server", "hid"):
        splitter = window.pages[route].findChildren(QSplitter)[0]
        first = splitter.widget(0)
        assert first is not None, route
        assert first.minimumWidth() == PANEL_MIN_WIDTH, route

    c51_areas = [
        area
        for area in window.pages["c51"].findChildren(SingleDirectionScrollArea)
        if area.minimumWidth() == PANEL_MIN_WIDTH
    ]
    assert c51_areas


def test_dotmatrix_preview_scroll_is_fluent(window) -> None:
    preview = window.pages["dotmatrix"].preview_scroll
    assert isinstance(preview, SmoothScrollArea)
    assert preview.widgetResizable() is False


_SCROLL_INTERNALS = {
    "SmoothScrollBar",
    "ScrollBarGroove",
    "ArrowButton",
    "ScrollBarHandle",
    "QScrollBar",
    "EditLayer",
    "QLineEdit",
    "InfoBar",
}
_TEXT_WIDGET_TYPES = (QLabel, QCheckBox, QPushButton)


def _is_layout_internal(widget: QWidget) -> bool:
    """滚动条内部件、输入框内部层与 InfoBar 浮层不参与布局审查。"""
    return type(widget).__name__ in _SCROLL_INTERNALS or widget.objectName().startswith("qt_")


def _visible_children(parent: QWidget) -> list[QWidget]:
    return [
        child
        for child in parent.findChildren(
            QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
        )
        if child.isVisible() and not _is_layout_internal(child)
    ]


def test_minimum_window_has_no_clipping_or_overlap(window, qapp) -> None:
    """主窗口压到最小尺寸时，逐页检查文字控件裁切、同级重叠与滚动内容越界。"""
    window.resize(1180, 760)
    window.show()
    qapp.processEvents()
    problems: list[str] = []

    for route, page in window.pages.items():
        window.switchTo(page)
        qapp.processEvents()

        for parent in [page, *page.findChildren(QWidget)]:
            if _is_layout_internal(parent):
                continue
            children = _visible_children(parent)
            for child in children:
                if not isinstance(child, _TEXT_WIDGET_TYPES):
                    continue
                # 纯图标 CheckBox 固定宽 26 是刻意紧凑布局，不算裁切。
                if isinstance(child, QCheckBox) and not child.text().strip():
                    continue
                need = child.minimumSizeHint().width()
                if child.width() < need - 2:
                    problems.append(
                        f"{route}: 裁切 {type(child).__name__}"
                        f"('{child.text()[:18]}') 宽 {child.width()} < {need}"
                    )
            for index, first in enumerate(children):
                for second in children[index + 1 :]:
                    overlap = first.geometry().intersected(second.geometry())
                    if overlap.width() > 4 and overlap.height() > 4:
                        problems.append(
                            f"{route}: 重叠 {type(first).__name__} ∩ "
                            f"{type(second).__name__} = "
                            f"{overlap.width()}x{overlap.height()}"
                        )

        for area in page.findChildren(SingleDirectionScrollArea):
            content = area.widget()
            if content is None:
                continue
            need = content.minimumSizeHint().width()
            viewport = area.viewport().width()
            if need > viewport + 2:
                problems.append(
                    f"{route}: 滚动内容最小宽 {need} > 视口 {viewport}"
                )

    window.hide()
    assert not problems, "\n".join(problems)

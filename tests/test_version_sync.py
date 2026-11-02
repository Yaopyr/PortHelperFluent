"""版本号与文档基线的同步断言。

版本号只有一个源头（``app.APP_VERSION``）；``build/version_info.txt`` 是手工维护的
独立副本，必须与它一致，否则资源管理器里显示的版本会和窗口标题、自检报告对不上。
"""

from __future__ import annotations

from pathlib import Path

from app import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_version_sources_are_in_sync() -> None:
    assert APP_VERSION == "1.2.0"
    text = (PROJECT_ROOT / "build" / "version_info.txt").read_text(encoding="utf-8")
    assert "(1, 2, 0, 0)" in text
    assert "FileVersion', '1.2.0'" in text
    assert "ProductVersion', '1.2.0'" in text


def test_building_doc_matches_current_version() -> None:
    """发布示例、标签与自检 JSON 都改到当前版本，避免照文档发布出旧标签。"""
    text = (PROJECT_ROOT / "docs" / "BUILDING.md").read_text(encoding="utf-8")
    assert "1.1.2" not in text
    assert APP_VERSION in text


def test_changelog_has_current_release_section() -> None:
    text = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{APP_VERSION}] - 2026-11-01" in text

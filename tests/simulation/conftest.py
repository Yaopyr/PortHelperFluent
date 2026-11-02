"""仿真套件的 fixture、收集校验与报告钩子。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.simulation.harness.context import SimulationContext
from tests.simulation.manifest import AUTO_FEATURES, BY_ID, validate_manifest
from tests.simulation.report import Recorder


def pytest_addoption(parser) -> None:
    group = parser.getgroup("simulation")
    group.addoption("--sim-report-dir", action="store", default=None, help="仿真报告输出目录")
    group.addoption(
        "--sim-screenshots",
        action="store",
        default="failure",
        choices=("always", "failure", "none"),
        help="截图策略",
    )
    group.addoption("--sim-strict", action="store_true", help="严格校验功能映射")
    group.addoption("--sim-baseline", action="store", default=None, help="现有回归基线 JSON")


def pytest_configure(config) -> None:
    validate_manifest()
    config.addinivalue_line("markers", "simulation: 端到端软件模拟验证场景")
    config.addinivalue_line("markers", "scenario: 场景对应的功能 ID")
    report_dir = config.getoption("--sim-report-dir")
    if not report_dir:
        report_dir = (
            Path("validation-reports")
            / f"sim-{datetime.now():%Y%m%d-%H%M%S}"
        )
    recorder = Recorder(Path(report_dir), config.getoption("--sim-screenshots"))
    config._sim_recorder = recorder
    config._sim_unmapped = []
    config._sim_duplicates = []
    baseline_path = config.getoption("--sim-baseline")
    config._sim_baseline = {}
    if baseline_path and Path(baseline_path).is_file():
        try:
            config._sim_baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
        except Exception:
            config._sim_baseline = {}


def pytest_collection_modifyitems(config, items) -> None:
    seen: dict[str, str] = {}
    for item in items:
        callspec = getattr(item, "callspec", None)
        scenario_id = callspec.params.get("scenario_id") if callspec is not None else None
        if scenario_id is None:
            if "test_selfcheck" not in item.nodeid:
                config._sim_unmapped.append(item.nodeid)
            continue
        if scenario_id not in BY_ID or BY_ID[scenario_id].kind != "auto":
            config._sim_unmapped.append(f"{item.nodeid} -> {scenario_id}")
            continue
        if scenario_id in seen:
            config._sim_duplicates.append(f"{scenario_id}: {seen[scenario_id]} / {item.nodeid}")
        seen[scenario_id] = item.nodeid
    config._sim_seen = seen


@pytest.fixture
def sim(qapp, tmp_path, monkeypatch, request):
    callspec = getattr(request.node, "callspec", None)
    scenario_id = callspec.params.get("scenario_id") if callspec is not None else "HARNESS"
    recorder: Recorder = request.config._sim_recorder
    if scenario_id in BY_ID and BY_ID[scenario_id].kind == "auto":
        recorder.begin(scenario_id, request.node.nodeid)
    context = SimulationContext(
        qapp,
        tmp_path,
        monkeypatch,
        scenario_id,
        recorder.report_dir,
    )
    context.screenshot_mode = recorder.screenshots_mode
    request.node._sim_context = context
    request.node._sim_scenario_id = scenario_id
    request.node._sim_recorder = recorder
    try:
        yield context
    finally:
        try:
            context.close()
        finally:
            if scenario_id in recorder.records:
                record = recorder.records[scenario_id]
                record.checks = list(context.checks)
                record.screenshots = list(context.screenshots)
                record.artifacts = list(context.artifacts)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "setup":
        item._sim_setup_report = report
    elif report.when == "call":
        item._sim_call_report = report
        if report.failed:
            context = getattr(item, "_sim_context", None)
            if context is not None:
                try:
                    context.capture_failure(str(report.longrepr))
                except Exception:
                    pass
    elif report.when == "teardown":
        item._sim_teardown_report = report
        _finalize_item(item)


def _finalize_item(item) -> None:
    scenario_id = getattr(item, "_sim_scenario_id", None)
    recorder: Recorder | None = getattr(item, "_sim_recorder", None)
    if recorder is None or scenario_id not in recorder.records:
        return
    setup_report = getattr(item, "_sim_setup_report", None)
    call_report = getattr(item, "_sim_call_report", None)
    teardown_report = getattr(item, "_sim_teardown_report", None)
    if setup_report is not None and setup_report.failed:
        recorder.finish(scenario_id, "FAIL", str(setup_report.longrepr))
        return
    if call_report is not None and call_report.skipped:
        reason = str(getattr(call_report, "longrepr", ""))
        recorder.finish(scenario_id, "SKIP", reason)
        return
    if call_report is not None and call_report.failed:
        recorder.finish(scenario_id, "FAIL", str(call_report.longrepr))
        return
    if teardown_report is not None and teardown_report.failed:
        recorder.finish(scenario_id, "FAIL", str(teardown_report.longrepr))
        return
    record = recorder.records[scenario_id]
    if not record.checks:
        recorder.finish(scenario_id, "FAIL", "场景没有任何断言证据，不能判定 PASS")
        return
    recorder.finish(scenario_id, "PASS", "")


def pytest_sessionfinish(session, exitstatus) -> None:
    config = session.config
    recorder: Recorder = getattr(config, "_sim_recorder", None)
    if recorder is None:
        return
    try:
        markdown, json_path = recorder.write_reports(getattr(config, "_sim_baseline", {}))
        summary = recorder.summary()
        strict = bool(config.getoption("--sim-strict"))
        violations = []
        missing = summary["not_implemented"]
        if missing:
            violations.append(f"未映射自动化功能：{', '.join(missing)}")
        unmapped = getattr(config, "_sim_unmapped", [])
        if unmapped:
            violations.append(f"未映射测试：{unmapped}")
        duplicates = getattr(config, "_sim_duplicates", [])
        if duplicates:
            violations.append(f"重复场景：{duplicates}")
        if strict and violations:
            session.exitstatus = 1
        writer = config.pluginmanager.getplugin("terminalreporter")
        if writer is not None:
            writer.write_line("")
            writer.write_line(f"仿真报告：{markdown}")
            writer.write_line(
                "自动化：{PASS} PASS / {FAIL} FAIL / {SKIP} SKIP / {MISSING} 未映射；"
                "人工：{manual} 项".format(
                    PASS=summary["auto_counts"].get("PASS", 0),
                    FAIL=summary["auto_counts"].get("FAIL", 0),
                    SKIP=summary["auto_counts"].get("SKIP", 0),
                    MISSING=summary["auto_counts"].get("MISSING", 0),
                    manual=summary["manual_total"],
                )
            )
            for violation in violations:
                writer.write_line(f"严格模式问题：{violation}")
    except Exception as exc:
        session.exitstatus = 1
        writer = config.pluginmanager.getplugin("terminalreporter")
        if writer is not None:
            writer.write_line(f"生成仿真报告失败：{exc}")

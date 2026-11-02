"""场景结果模型与中文 Markdown/JSON 报告渲染。"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .manifest import AUTO_FEATURES, BY_ID, FEATURES, MANUAL_FEATURES, NA_FEATURES


@dataclass
class ScenarioRecord:
    feature_id: str
    status: str = "FAIL"
    duration_ms: int = 0
    checks: list[dict] = field(default_factory=list)
    error: str = ""
    screenshots: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    notes: str = ""
    started_at: str = ""
    nodeid: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class Recorder:
    def __init__(self, report_dir: Path, screenshots_mode: str = "failure") -> None:
        self.report_dir = Path(report_dir)
        self.screenshots_mode = screenshots_mode
        self.records: dict[str, ScenarioRecord] = {}
        self.start_clock: dict[str, float] = {}
        self.results_path = self.report_dir / "results.jsonl"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.results_path.write_text("", encoding="utf-8")

    def begin(self, feature_id: str, nodeid: str = "") -> ScenarioRecord:
        record = ScenarioRecord(feature_id=feature_id, nodeid=nodeid)
        record.started_at = datetime.now().isoformat(timespec="seconds")
        self.records[feature_id] = record
        import time

        self.start_clock[feature_id] = time.monotonic()
        return record

    def finish(self, feature_id: str, status: str, error: str = "") -> ScenarioRecord:
        import time

        record = self.records.setdefault(feature_id, ScenarioRecord(feature_id=feature_id))
        record.status = status
        record.error = error
        started = self.start_clock.get(feature_id)
        if started is not None:
            record.duration_ms = int((time.monotonic() - started) * 1000)
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        with self.results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")
            handle.flush()
        return record

    def summary(self) -> dict:
        auto_status = {feature.id: "MISSING" for feature in AUTO_FEATURES}
        for feature_id, record in self.records.items():
            if feature_id in auto_status:
                auto_status[feature_id] = record.status
        counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "MISSING": 0}
        for status in auto_status.values():
            counts[status] = counts.get(status, 0) + 1
        return {
            "auto_total": len(AUTO_FEATURES),
            "auto_status": auto_status,
            "auto_counts": counts,
            "manual_total": len(MANUAL_FEATURES),
            "na_total": len(NA_FEATURES),
            "not_implemented": sorted(
                feature_id for feature_id, status in auto_status.items() if status == "MISSING"
            ),
        }

    def write_reports(self, baseline: dict | None = None) -> tuple[Path, Path]:
        summary = self.summary()
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "features": [asdict(feature) for feature in FEATURES],
            "manual": [asdict(feature) for feature in MANUAL_FEATURES],
            "na": [asdict(feature) for feature in NA_FEATURES],
            "results": {key: record.as_dict() for key, record in self.records.items()},
            "summary": summary,
            "baseline": baseline or {},
        }
        json_path = self.report_dir / "report.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_path = self.report_dir / "report.md"
        markdown_path.write_text(
            render_markdown(self, summary, baseline or {}), encoding="utf-8"
        )
        return markdown_path, json_path


def _status_for(recorder: Recorder, feature_id: str) -> str:
    record = recorder.records.get(feature_id)
    return record.status if record is not None else "MISSING"


def _evidence(record: ScenarioRecord | None) -> str:
    if record is None:
        return "—"
    parts = []
    if record.checks:
        parts.append(f"{len(record.checks)} 项断言")
    if record.screenshots:
        parts.append(f"{len(record.screenshots)} 截图")
    if record.artifacts:
        parts.append(f"{len(record.artifacts)} 证据文件")
    return "、".join(parts) or "无"


def render_markdown(recorder: Recorder, summary: dict, baseline: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []
    lines.append("# PortHelper Fluent 软件模拟验证报告")
    lines.append("")
    lines.append(f"- 生成时间：{now}")
    lines.append(f"- Python：{sys.version.split()[0]}")
    lines.append(f"- 平台：{platform.platform()}")
    if baseline:
        lines.append(
            f"- 现有回归基线：{baseline.get('passed', '?')} passed"
            f"（{baseline.get('duration_s', '?')} s，退出码 {baseline.get('returncode', '?')}）"
        )
    lines.append("")
    counts = summary["auto_counts"]
    lines.append("## 结论摘要")
    lines.append("")
    lines.append(
        f"自动化场景 **{summary['auto_total']}** 项："
        f"PASS {counts.get('PASS', 0)} / FAIL {counts.get('FAIL', 0)} / "
        f"SKIP {counts.get('SKIP', 0)} / 未映射 {counts.get('MISSING', 0)}；"
        f"人工验收 **{summary['manual_total']}** 项（待执行）；"
        f"文档明确不支持 **{summary['na_total']}** 项。"
    )
    lines.append("")
    if counts.get("FAIL", 0) or counts.get("MISSING", 0):
        lines.append("> **结论：存在失败或未映射功能，不能判定全部功能正常。**")
    else:
        lines.append(
            "> **结论：所有可在源码层自动验证的功能均通过；"
            "真实硬件、Keil、更新替换、音频听感与打包 exe 需按人工清单验收。**"
        )
    lines.append("")
    lines.append("## 功能矩阵")
    lines.append("")
    lines.append("| 功能 ID | 页面 | 功能 | 状态 | 证据 | 补充现有测试 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for feature in FEATURES:
        record = recorder.records.get(feature.id)
        status = _status_for(recorder, feature.id)
        existing = "、".join(feature.existing_tests) if feature.existing_tests else "—"
        lines.append(
            f"| {feature.id} | {feature.page} | {feature.title} | **{status}** | "
            f"{_evidence(record)} | {existing} |"
        )
    lines.append("")
    failures = [record for record in recorder.records.values() if record.status == "FAIL"]
    if failures:
        lines.append("## 失败详情")
        lines.append("")
        for record in failures:
            lines.append(f"### {record.feature_id} — {BY_ID.get(record.feature_id, record.feature_id)}")
            lines.append("")
            lines.append("```text")
            lines.append(record.error or "（无 traceback）")
            lines.append("```")
            if record.screenshots:
                lines.append("")
                lines.append("截图：" + "、".join(record.screenshots))
            if record.artifacts:
                lines.append("")
                lines.append("证据：" + "、".join(record.artifacts))
            lines.append("")
    skipped = [record for record in recorder.records.values() if record.status == "SKIP"]
    if skipped:
        lines.append("## 环境跳过")
        lines.append("")
        for record in skipped:
            lines.append(f"- {record.feature_id}：{record.error or '未说明原因'}")
        lines.append("")
    lines.append("## 人工验收清单")
    lines.append("")
    lines.append("| 功能 ID | 项目 | 说明 | 状态 |")
    lines.append("| --- | --- | --- | --- |")
    for feature in MANUAL_FEATURES:
        lines.append(f"| {feature.id} | {feature.title} | {feature.note} | **MANUAL（待执行）** |")
    lines.append("")
    lines.append("## 文档明确不支持")
    lines.append("")
    for feature in NA_FEATURES:
        lines.append(f"- {feature.id}：{feature.title}（N/A）")
    lines.append("")
    lines.append("## 验证边界")
    lines.append("")
    lines.append(
        "- 自动化场景在离屏 Qt 中运行真实 MainWindow、真实页面、真实 SerialWorker/HidWorker/"
        "BleWorker/SppWorker 与真实信号槽。"
    )
    lines.append(
        "- 串口/HID/BLE/SPP 由有状态虚拟设备替代，验证的是应用逻辑、状态机、错误路径与导出；"
        "不验证驱动、芯片兼容性与真实时序。"
    )
    lines.append(
        "- TCP/UDP 使用 127.0.0.1 回环；Ping/DNS/网卡/更新检查/下载/安装均为注入桩；"
        "未执行 PowerShell 自我替换。"
    )
    lines.append(
        "- 真实硬件、Keil 打开工程、真实更新替换、声音听感、打包 exe 在无 Python 环境运行，"
        "必须按人工清单验收。"
    )
    lines.append("")
    lines.append("## 证据索引")
    lines.append("")
    lines.append(f"- 机器可读结果：`{recorder.report_dir / 'report.json'}`")
    lines.append(f"- 逐场景增量结果：`{recorder.report_dir / 'results.jsonl'}`")
    lines.append(f"- 截图目录：`{recorder.report_dir / 'screenshots'}`")
    lines.append(f"- 证据目录：`{recorder.report_dir / 'artifacts'}`")
    lines.append("")
    return "\n".join(lines)

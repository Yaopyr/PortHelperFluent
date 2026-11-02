"""运行软件模拟验证套件并生成报告。

用法（PowerShell，仓库根目录）：
    python tools/run_simulation.py --strict --screenshots always --with-baseline
    python tools/run_simulation.py --only SER
"""

from __future__ import annotations

import argparse
import json
import os
import re
import site
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_ROOT = REPO_ROOT / "validation-reports"


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PortHelper Fluent 软件模拟验证")
    parser.add_argument("--only", action="append", default=[], help="只运行匹配的功能前缀，可重复")
    parser.add_argument("--strict", action="store_true", help="严格校验功能映射")
    parser.add_argument(
        "--screenshots",
        choices=("always", "failure", "none"),
        default="failure",
        help="截图策略",
    )
    parser.add_argument("--report-dir", default=None, help="指定报告目录")
    parser.add_argument("--with-baseline", action="store_true", help="先运行现有回归测试并写入报告")
    parser.add_argument("--timeout", type=int, default=900, help="整次仿真超时（秒）")
    return parser.parse_args(argv)


def _env(report_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    appdata = report_dir / "tmp_appdata"
    appdata.mkdir(parents=True, exist_ok=True)
    # APPDATA 同时决定 Python 用户级 site-packages；本机依赖装在用户目录，
    # 必须显式保留 PYTHONUSERBASE，否则子进程会缺 pygments 等测试依赖。
    env["PYTHONUSERBASE"] = site.getuserbase()
    env["APPDATA"] = str(appdata)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTHONUTF8", "1")
    return env


def _baseline(report_dir: Path, env: dict[str, str]) -> dict:
    # 不要额外加 -q：pytest.ini 已有 -q，重复会让 pytest 省略 “N passed” 汇总行。
    command = [sys.executable, "-m", "pytest"]
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    duration = time.monotonic() - started
    output = (completed.stdout or "") + (completed.stderr or "")
    (report_dir / "baseline.log").write_text(output, encoding="utf-8", errors="replace")
    match = re.search(r"(\d+) passed", output)
    duration_match = re.search(r"in ([\d.]+)s", output)
    result = {
        "command": "python -m pytest -q",
        "returncode": completed.returncode,
        "passed": int(match.group(1)) if match else None,
        "duration_s": float(duration_match.group(1)) if duration_match else round(duration, 1),
    }
    (report_dir / "baseline.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    args = _parse_args(argv)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_dir = (
        Path(args.report_dir).resolve()
        if args.report_dir
        else (REPORTS_ROOT / f"sim-{stamp}").resolve()
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    env = _env(report_dir)

    baseline = {}
    if args.with_baseline:
        print(f"运行现有回归基线…（{REPO_ROOT}）")
        baseline = _baseline(report_dir, env)
        print(
            f"基线：{baseline.get('passed')} passed / "
            f"exit {baseline.get('returncode')} / {baseline.get('duration_s')} s"
        )
        if baseline.get("returncode") != 0:
            print("现有回归基线未通过，停止仿真。")
            return 1

    command = [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        "pytest-sim.ini",
        "tests/simulation",
        "--sim-report-dir",
        str(report_dir),
        "--sim-screenshots",
        args.screenshots,
    ]
    if args.strict:
        command.append("--sim-strict")
    if baseline:
        command.extend(["--sim-baseline", str(report_dir / "baseline.json")])
    if args.only:
        expression = " or ".join(args.only)
        command.extend(["-k", expression])

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, args.timeout),
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        output = str(exc.stdout or "") + "\n仿真超时"
        returncode = 1
    elapsed = time.monotonic() - started
    (report_dir / "pytest.log").write_text(output, encoding="utf-8", errors="replace")
    print(output.rstrip())
    print(f"\n仿真耗时：{elapsed:.1f} s")

    markdown = report_dir / "report.md"
    json_path = report_dir / "report.json"
    if not markdown.is_file() or not json_path.is_file():
        print(f"报告缺失：{markdown} / {json_path}")
        return 1
    (REPORTS_ROOT / "latest.txt").write_text(str(report_dir), encoding="utf-8")
    print(f"报告：{markdown}")
    print(f"JSON：{json_path}")
    return 0 if returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

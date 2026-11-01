"""``system`` 命令组：``version`` 与 ``selftest``。

``selftest`` 是「CLI 不写 app.log」的唯一例外：自检本身要验证 ``app.log`` 可写，
必须初始化日志；这是自检的职责，不是普通命令的副作用。报告 ``ok=false`` 时映射
为退出码 3，而旧的 ``--selftest`` 入口仍保持原有 0/1。
"""

from __future__ import annotations

import io
import json
import platform
import sys
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any, Callable, Iterator

from app import APP_VERSION
from app.cli.errors import EXIT_OPERATION, CliError
from app.cli.output import add_output_options
from app.services.exports import write_text_atomic

__all__ = ["register", "run_selftest", "run_version"]

SelftestReport = Callable[[], dict[str, Any]]


def register(subparsers, selftest_report: SelftestReport | None = None) -> None:
    """注册 ``version`` 与 ``selftest``；``selftest_report`` 由根 ``main()`` 注入。"""
    version_parser = subparsers.add_parser(
        "version",
        help="输出版本、Python、是否冻结与可执行文件路径。",
        description="输出版本信息（version/python/frozen/executable）。",
    )
    add_output_options(version_parser)
    version_parser.set_defaults(handler=run_version, command="version")

    selftest_parser = subparsers.add_parser(
        "selftest",
        help="执行启动自检并把同一份报告输出到 stdout。",
        description="执行启动自检；给 --report 时额外写出 JSON 报告文件。",
    )
    add_output_options(selftest_parser)
    selftest_parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="自检报告 JSON 的额外输出路径（目录不存在时自动创建）。",
    )
    # 收集器作为解析器默认值传入：冻结后入口是 ``__main__``，命令不能再 ``import main``。
    selftest_parser.set_defaults(
        handler=run_selftest,
        command="selftest",
        selftest_report=selftest_report,
    )


def run_version(args) -> dict[str, Any]:
    """返回版本信息；``executable`` 在源码运行时是 python，冻结后是 exe。"""
    return {
        "version": APP_VERSION,
        "python": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
    }


def run_selftest(args) -> dict[str, Any]:
    """执行自检并返回报告；``ok=false`` 抛 ``selftest_failed``（退出码 3）。"""
    collector = getattr(args, "selftest_report", None) or _collect_selftest_report
    with _stdout_to_stderr():
        report = collector()
    report_path = getattr(args, "report", None)
    if report_path is not None:
        _write_report(Path(report_path), report)
    if not report.get("ok"):
        raise CliError(
            "selftest_failed",
            "启动自检未通过，详见 details 中的报告。",
            EXIT_OPERATION,
            details=report,
        )
    return report


@contextmanager
def _stdout_to_stderr() -> Iterator[None]:
    """自检会导入 qfluentwidgets 等第三方库，它们可能向 stdout 打印提示。

    stdout 必须是机器可读的 JSON，因此把自检期间的打印改道 stderr（stderr 不可用
    时丢弃），避免第三方提示行混进信封。
    """
    target = sys.stderr if sys.stderr is not None else io.StringIO()
    with redirect_stdout(target):
        yield


def _collect_selftest_report() -> dict[str, Any]:
    """未注入收集器时的兜底（源码运行或测试直接调用 ``run``）。"""
    from main import collect_selftest_report

    return collect_selftest_report()


def _write_report(report_path: Path, report: dict[str, Any]) -> None:
    try:
        write_text_atomic(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    except Exception as exc:
        raise CliError(
            "permission_denied",
            f"写入自检报告失败：{exc}",
            EXIT_OPERATION,
        ) from exc

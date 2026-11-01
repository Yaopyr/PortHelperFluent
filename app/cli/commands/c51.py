"""``c51`` 命令组：8051 寄存器计算、C 代码生成与 Keil 工程导出。

- ``show-config``：回显默认配置（``C51Config().to_dict()``），``--out`` 写 JSON；
- ``calc``：返回 ``config``/``registers``/``timers``（0/1/2 的重载与计数）/``baud``/``warnings``；
- ``code``：``generate_c`` 的文本，``--out`` 原子写出；
- ``export``：``generate_keil_project`` 写出 Main.c、Uv2、Opt 与自写 STARTUP.A51。

配置来源互斥且可省略：``--config FILE``（``-`` 表示从标准输入读 JSON）或
``--config-json STRING``，都不给时用 :class:`C51Config` 默认值。寄存器位域、定时器
重载与波特率误差全部来自 :mod:`app.core.c51`（与 GUI 同一套实现），本模块只做参数
映射、JSON 解析与结果组装。

核心模块是纯标准库实现，因此模块级导入不会拉起 PyQt6/Pillow/numpy。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, NoReturn

from app.cli.errors import EXIT_OPERATION, CliError
from app.cli.output import add_output_options
from app.core.c51 import (
    C51Config,
    baud_reload,
    build_registers,
    build_warnings,
    generate_c,
    generate_keil_project,
    timer_reload,
)
from app.services.exports import write_text_atomic

__all__ = ["register", "run_calc", "run_code", "run_export", "run_show_config"]

# 定时器编号固定为 0/1/2；``timer_reload`` 只接受这三个值。
TIMER_INDEXES = (0, 1, 2)


def register(subparsers) -> None:
    """注册 ``c51`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "c51",
        help="C51 向导：配置回显、定时器/波特率计算、C 代码与 Keil 工程导出。",
        description="8051 定时器、串口与寄存器计算及代码生成；"
        "可用子命令：show-config、calc、code、export。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    _register_show_config(group)
    _register_calc(group)
    _register_code(group)
    _register_export(group)


# ---------------------------------------------------------------------------
# show-config
# ---------------------------------------------------------------------------


def _register_show_config(subparsers) -> None:
    parser = subparsers.add_parser(
        "show-config",
        help="回显默认 C51 配置；--out 写出 JSON。",
        description="输出 C51Config 默认值的 JSON，可直接作为 --config 的模板。",
    )
    _add_out_arg(parser, "把默认配置写到该 JSON 文件（先写临时文件再替换）。")
    add_output_options(parser)
    parser.set_defaults(handler=run_show_config, command="c51.show-config")


def run_show_config(args) -> dict[str, Any]:
    """输出默认配置字典；``--out`` 时把同一份 JSON 原子写出。"""
    data = C51Config().to_dict()
    out_path = getattr(args, "out", None)
    if out_path is not None:
        _write_text(out_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", "配置 JSON")
    return data


# ---------------------------------------------------------------------------
# calc
# ---------------------------------------------------------------------------


def _register_calc(subparsers) -> None:
    parser = subparsers.add_parser(
        "calc",
        help="计算寄存器、定时器重载、波特率与警告。",
        description="按配置计算 TMOD/TCON/SCON/IE 等寄存器、定时器 0/1/2 的 TH/TL "
        "与计数、串口重载值及冲突警告。",
    )
    _add_config_options(parser)
    add_output_options(parser)
    parser.set_defaults(handler=run_calc, command="c51.calc")


def run_calc(args) -> dict[str, Any]:
    """返回 ``config``/``registers``/``timers``/``baud``/``warnings``。"""
    config = _load_config(args)
    timers = []
    for index in TIMER_INDEXES:
        result = timer_reload(config, index)
        timers.append(
            {
                "th": result.th,
                "tl": result.tl,
                "counts": result.counts,
                "warnings": list(result.warnings),
            }
        )
    return {
        "config": config.to_dict(),
        "registers": build_registers(config),
        "timers": timers,
        "baud": baud_reload(config),
        "warnings": build_warnings(config),
    }


# ---------------------------------------------------------------------------
# code
# ---------------------------------------------------------------------------


def _register_code(subparsers) -> None:
    parser = subparsers.add_parser(
        "code",
        help="生成 8051 初始化 C 代码；--out 写出文件。",
        description="输出带中文注释的初始化 C 代码预览；--out 时同时写文件。",
    )
    _add_config_options(parser)
    _add_out_arg(parser, "把生成的 C 代码写到该文件（先写临时文件再替换）。")
    add_output_options(parser)
    parser.set_defaults(handler=run_code, command="c51.code")


def run_code(args) -> dict[str, Any]:
    """返回 ``text``；给了 ``--out`` 时把同一段文本原子写出。"""
    config = _load_config(args)
    text = generate_c(config)
    out_path = getattr(args, "out", None)
    if out_path is not None:
        _write_text(out_path, text, "C 代码")
    return {"text": text}


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def _register_export(subparsers) -> None:
    parser = subparsers.add_parser(
        "export",
        help="导出 Keil 工程（Main.c、Uv2、Opt、STARTUP.A51）。",
        description="按配置生成最小 Keil uVision2 工程；四个文件都是本工具自写模板。",
    )
    _add_config_options(parser)
    parser.add_argument(
        "--out-dir",
        required=True,
        metavar="DIR",
        help="工程输出目录，不存在时自动创建。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_export, command="c51.export")


def run_export(args) -> dict[str, Any]:
    """写出 Keil 工程文件，返回 ``files``。"""
    config = _load_config(args)
    out_dir = Path(getattr(args, "out_dir"))
    try:
        files = generate_keil_project(config, out_dir)
    except OSError as exc:
        raise CliError(
            "permission_denied",
            f"写出 Keil 工程失败：{out_dir}（{exc}）。"
            "请检查输出目录是否存在、是否为目录以及是否有写入权限。",
            EXIT_OPERATION,
        ) from exc
    return {"files": [str(path) for path in files]}


# ---------------------------------------------------------------------------
# 参数与配置读取
# ---------------------------------------------------------------------------


def _add_config_options(parser) -> None:
    """添加互斥的配置来源：文件（``-`` 为 stdin）或 JSON 字符串，可都不给。"""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--config",
        metavar="FILE|-",
        help="配置文件路径（内容为 C51Config.to_dict() 的 JSON）；用 - 从标准输入读取。",
    )
    group.add_argument(
        "--config-json",
        metavar="JSON",
        help="直接传入配置 JSON 字符串，例如 '{\"crystal_mhz\": 12.0}'。",
    )


def _add_out_arg(parser, help_text: str) -> None:
    parser.add_argument("--out", metavar="PATH", help=help_text)


def _load_config(args) -> C51Config:
    """按 ``--config``/``--config-json`` 读配置；都没给时返回默认 :class:`C51Config`。"""
    path_value = getattr(args, "config", None)
    raw_json = getattr(args, "config_json", None)
    if raw_json is not None:
        return _config_from_json(raw_json, "配置 JSON")
    if path_value is None:
        return C51Config()
    path = str(path_value)
    if path == "-":
        return _config_from_json(_read_stdin(), "标准输入中的配置 JSON")
    return _config_from_json(_read_config_file(path), f"配置文件 {path}")


def _read_config_file(path: str) -> str:
    """读配置文件；文件不存在按 ``not_found``，其余读取失败按输入非法。"""
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CliError(
            "not_found",
            f"找不到配置文件：{path}。请确认路径是否正确，或改用 --config-json。",
            EXIT_OPERATION,
        ) from exc
    except OSError as exc:
        raise CliError(
            "invalid_input", f"读取配置文件 {path} 失败：{exc}", EXIT_OPERATION
        ) from exc


def _read_stdin() -> str:
    """``--config -``：把标准输入的整段文本当配置 JSON。"""
    stream = sys.stdin
    if stream is None:
        raise CliError(
            "invalid_input",
            "无法从标准输入读取配置：标准输入不可用。请改用 --config 或 --config-json。",
            EXIT_OPERATION,
        )
    try:
        return stream.read()
    except OSError as exc:
        raise CliError(
            "invalid_input", f"读取标准输入失败：{exc}", EXIT_OPERATION
        ) from exc


def _config_from_json(text: str, label: str) -> C51Config:
    """JSON 文本 → :class:`C51Config`；语法错误、非有限数值或不是对象都按输入非法（3）。"""
    try:
        data = json.loads(text, parse_constant=_reject_constant)
        _ensure_finite(data)
    except ValueError as exc:
        raise CliError(
            "invalid_input", f"{label} 无效：{exc}", EXIT_OPERATION
        ) from exc
    if not isinstance(data, dict):
        raise CliError(
            "invalid_input",
            f"{label}必须是 JSON 对象（键值对），收到 {type(data).__name__}。",
            EXIT_OPERATION,
        )
    try:
        config = C51Config.from_dict(data)
    except (OverflowError, ValueError) as exc:
        raise CliError(
            "invalid_input", f"{label}中的数值超出范围：{exc}", EXIT_OPERATION
        ) from exc
    _ensure_config_finite(config, label)
    return config


def _reject_constant(token: str) -> NoReturn:
    """拒绝 ``NaN``/``Infinity``/``-Infinity``：Python 的 ``json`` 默认接受它们。

    这类常量一旦写进信封，stdout 就不再是标准 JSON。
    """
    raise ValueError(f"出现不支持的数值常量 {token}（配置只接受有限数值）")


def _ensure_finite(value: Any) -> None:
    """递归拒绝非有限浮点数：覆盖 ``1e999`` 这类溢出成 inf 的标准数字字面量。"""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"出现非有限数值 {value}（配置只接受有限数值）")
    if isinstance(value, dict):
        for item in value.values():
            _ensure_finite(item)
    elif isinstance(value, list):
        for item in value:
            _ensure_finite(item)


def _ensure_config_finite(config: C51Config, label: str) -> None:
    """对构造后的有效配置再查一遍：``from_dict`` 会把字符串 ``"NaN"`` 变成 NaN。

    钳位比较对 NaN 恒为假，NaN 因此原样留在 ``crystal_mhz`` 并写进信封；
    原始 JSON 树里它是字符串，所以必须在 ``to_dict()`` 上复查。
    """
    try:
        _ensure_finite(config.to_dict())
    except ValueError as exc:
        raise CliError("invalid_input", f"{label} 无效：{exc}", EXIT_OPERATION) from exc


def _write_text(path_value: str, text: str, label: str) -> None:
    """原子写出文本；失败按 ``permission_denied`` 点名目标与原因。"""
    target = Path(str(path_value))
    try:
        write_text_atomic(target, text)
    except OSError as exc:
        raise CliError(
            "permission_denied",
            f"写出{label}失败：{target}（{exc}）。请检查父目录是否存在以及是否有写入权限。",
            EXIT_OPERATION,
        ) from exc

"""``config`` 命令组：路径与配置回显、单键读写、导出与重置。

- ``show``：返回隔离后的路径、便携标记、加载警告与生效配置（``settings.as_dict()``）；
- ``get``：读取单个键；``DEFAULT_SETTINGS`` 之外且配置文件里也没有的键按 ``not_found``；
- ``set``：只允许 ``DEFAULT_SETTINGS`` 中的键，值先按 JSON 解析（失败按字符串），
  再按默认值的类型校验，最后走 ``Settings.save()`` 的原子写入；
- ``export``：把生效配置用 :func:`write_text_atomic` 写成本地 JSON；
- ``reset``：破坏性操作，必须 ``--yes``，否则按用法错误（2）拒绝。

配置加载与保存全部复用 :mod:`app.core.settings` 与 :mod:`app.services.paths`，
本模块只做参数映射、类型校验与结果组装；除 ``reset``/``set`` 外不修改用户状态。
模块级导入不拉起 PyQt6/Pillow/numpy，也不初始化文件日志。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.cli.errors import EXIT_OPERATION, EXIT_USAGE, CliError
from app.cli.output import add_output_options
from app.core.settings import DEFAULT_SETTINGS, Settings
from app.services.exports import write_text_atomic
from app.services.paths import AppPaths, ensure_writable_paths, resolve_paths

__all__ = [
    "register",
    "run_export",
    "run_get",
    "run_reset",
    "run_set",
    "run_show",
]


def register(subparsers) -> None:
    """注册 ``config`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "config",
        help="查看、读取、修改、导出或重置应用配置。",
        description="应用配置（config.json）的查看与维护；"
        "可用子命令：show、get、set、export、reset。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    _register_show(group)
    _register_get(group)
    _register_set(group)
    _register_export(group)
    _register_reset(group)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _register_show(subparsers) -> None:
    parser = subparsers.add_parser(
        "show",
        help="输出路径、加载警告与生效配置。",
        description="输出隔离后的数据/日志/配置路径、便携标记、加载警告与生效配置。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_show, command="config.show")


def run_show(args) -> dict[str, Any]:
    """返回路径、便携标记、加载警告与生效配置（见 ``data`` 的七个键）。"""
    paths, settings, fallback_warning = _load_settings()
    return {
        "exe_dir": str(paths.exe_dir),
        "data_dir": str(paths.data_dir),
        "log_dir": str(paths.log_dir),
        "config_path": str(paths.config_file),
        "portable": paths.portable,
        "warning": _join_warnings(settings.warning, fallback_warning),
        "settings": settings.as_dict(),
    }


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def _register_get(subparsers) -> None:
    parser = subparsers.add_parser(
        "get",
        help="读取单个配置项的值。",
        description="读取单个配置项的值；未知键按 not_found（3）返回。",
    )
    parser.add_argument("--key", required=True, metavar="KEY", help="配置键，例如 ui.theme。")
    add_output_options(parser)
    parser.set_defaults(handler=run_get, command="config.get")


def run_get(args) -> dict[str, Any]:
    """返回 ``key``/``value``；键既不在默认值也不在配置文件里时按 ``not_found``。"""
    key = args.key
    _, settings, _ = _load_settings()
    if key not in settings.as_dict():
        raise CliError(
            "not_found",
            f"未找到配置项：{key}。请用 config show 查看生效配置。",
            EXIT_OPERATION,
        )
    return {"key": key, "value": settings.get(key)}


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


def _register_set(subparsers) -> None:
    parser = subparsers.add_parser(
        "set",
        help="修改单个配置项（只允许内置键）。",
        description="修改单个配置项并原子保存；值先按 JSON 解析，失败时按字符串处理，"
        "再按默认值的类型校验。",
    )
    parser.add_argument("--key", required=True, metavar="KEY", help="配置键，例如 ui.theme。")
    parser.add_argument(
        "--value",
        required=True,
        metavar="VALUE",
        help="新值；先按 JSON 解析（如 true、20000、null），解析失败时按字符串。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_set, command="config.set")


def run_set(args) -> dict[str, Any]:
    """校验并保存单个键，返回 ``key``/``value``（保存的是解析后的值）。"""
    key = args.key
    value = _validate_value(key, args.value)
    paths, settings, _ = _load_settings()
    settings.set(key, value)
    _save_settings(settings, paths.config_file, "保存配置")
    return {"key": key, "value": value}


def _validate_value(key: str, raw: str) -> Any:
    """把命令行文本解析成配置值并按默认值类型校验。

    只允许 ``DEFAULT_SETTINGS`` 中的键（未知键按 ``not_found``）；值先 ``json.loads``，
    语法错误时整段按字符串；类型与默认值不匹配时按 ``invalid_input``。
    """
    if key not in DEFAULT_SETTINGS:
        raise CliError(
            "not_found",
            f"未知配置项：{key}。config set 只允许写入内置键，请用 config show 查看。",
            EXIT_OPERATION,
        )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    default = DEFAULT_SETTINGS[key]
    if not _matches_default_type(value, default):
        raise CliError(
            "invalid_input",
            f"配置项 {key} 的值类型不匹配：期望 {_type_label(default)}，"
            f"收到 {_type_label(value)}（{raw!r}）。",
            EXIT_OPERATION,
        )
    return value


def _matches_default_type(value: Any, default: Any) -> bool:
    """按默认值的类型校验；``bool`` 是 ``int`` 的子类，需要单独判断。"""
    if default is None:
        return value is None or isinstance(value, str)
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return False


def _type_label(value: Any) -> str:
    """给错误信息用的类型名；JSON 能表达的类型用 JSON 习惯叫法。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def _register_export(subparsers) -> None:
    parser = subparsers.add_parser(
        "export",
        help="把生效配置导出为 JSON 文件。",
        description="把生效配置写成 JSON（先写临时文件再替换）。",
    )
    parser.add_argument("--out", required=True, metavar="PATH", help="导出的 JSON 文件路径。")
    add_output_options(parser)
    parser.set_defaults(handler=run_export, command="config.export")


def run_export(args) -> dict[str, Any]:
    """原子写出配置 JSON，返回 ``path``。"""
    _, settings, _ = _load_settings()
    target = Path(str(args.out))
    text = json.dumps(settings.as_dict(), ensure_ascii=False, indent=2)
    try:
        write_text_atomic(target, text)
    except OSError as exc:
        raise CliError(
            "permission_denied",
            f"导出配置失败：{target}（{exc}）。请检查父目录是否存在以及是否有写入权限。",
            EXIT_OPERATION,
        ) from exc
    return {"path": str(target)}


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def _register_reset(subparsers) -> None:
    parser = subparsers.add_parser(
        "reset",
        help="恢复默认配置（破坏性，必须 --yes）。",
        description="清空用户配置并恢复默认值；必须显式加 --yes，否则按用法错误拒绝。",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认重置：会覆盖当前 config.json 中的全部设置。",
    )
    add_output_options(parser)
    parser.set_defaults(handler=run_reset, command="config.reset")


def run_reset(args) -> dict[str, Any]:
    """缺 ``--yes`` 按用法错误（2）拒绝；否则恢复默认并保存，返回 ``reset``。"""
    if not getattr(args, "yes", False):
        raise CliError(
            "usage",
            "config reset 会覆盖全部用户配置，请显式加 --yes 确认。",
            EXIT_USAGE,
        )
    paths, settings, _ = _load_settings()
    settings.reset()
    _save_settings(settings, paths.config_file, "重置配置")
    return {"reset": True}


# ---------------------------------------------------------------------------
# 加载与保存
# ---------------------------------------------------------------------------


def _load_settings() -> tuple[AppPaths, Settings, str | None]:
    """解析路径、确保可写并加载配置；返回 ``(paths, settings, 回退提示)``。

    与 GUI 启动一致：便携模式不可写时回退 AppData，回退提示随 ``show`` 一起输出。
    配置目录不可写的 ``OSError`` 收敛成 ``permission_denied``（3）。
    """
    paths = resolve_paths()
    try:
        paths, fallback_warning = ensure_writable_paths(paths)
    except OSError as exc:
        raise CliError(
            "permission_denied",
            f"配置目录不可写：{paths.data_dir}（{exc}）。请检查目录权限。",
            EXIT_OPERATION,
        ) from exc
    settings = Settings(paths.config_file)
    settings.load()
    return paths, settings, fallback_warning


def _save_settings(settings: Settings, path: Path, action: str) -> None:
    """保存配置；失败时点名目标与原因，按 ``permission_denied``（3）上报。"""
    try:
        settings.save()
    except OSError as exc:
        raise CliError(
            "permission_denied",
            f"{action}失败：{path}（{exc}）。请检查配置目录与文件是否有写入权限。",
            EXIT_OPERATION,
        ) from exc


def _join_warnings(*warnings: str | None) -> str | None:
    """把配置损坏提示与便携回退提示合并成一条；都为空时为 None。"""
    present = [warning for warning in warnings if warning]
    return "；".join(present) if present else None

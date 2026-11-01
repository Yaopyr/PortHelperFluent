"""``convert`` 命令组：数值、文本与媒体转换。

本组只建解析器并委派给同级模块注册叶子命令：``numeric`` 提供
``base``/``binhex``/``sevenseg``/``palette``，媒体类子命令由 ``media`` 另行加入。
"""

from __future__ import annotations

from app.cli.commands.convert import numeric

__all__ = ["register"]


def register(subparsers) -> None:
    """注册 ``convert`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "convert",
        help="进制、Bin/Hex/文本、七段数码管与调色板换算。",
        description="数值与文本转换工具；可用子命令：base、binhex、sevenseg、palette。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    numeric.register(group)

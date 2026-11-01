"""``convert`` 命令组：数值、文本与媒体转换。

本组只建解析器并委派给同级模块注册叶子命令：``numeric`` 提供
``base``/``binhex``/``sevenseg``/``palette``，``media`` 提供 ``gif``/``audio``/``qr``。
"""

from __future__ import annotations

from app.cli.commands.convert import media, numeric

__all__ = ["register"]


def register(subparsers) -> None:
    """注册 ``convert`` 命令组（不带处理器的中间层解析器）。"""
    parser = subparsers.add_parser(
        "convert",
        help="进制、Bin/Hex/文本、七段数码管、调色板、GIF、音频与二维码。",
        description="数值、文本与媒体转换工具；可用子命令：base、binhex、sevenseg、"
        "palette、gif、audio、qr。",
    )
    group = parser.add_subparsers(title="子命令", metavar="<action>")
    numeric.register(group)
    media.register(group)

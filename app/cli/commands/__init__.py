"""CLI 命令组：每个模块按 ``register(subparsers)`` 注册自己的命令。

本包顶层不导入具体命令模块，由 ``app.cli.main.build_parser`` 只注册已实现的组；
这样某个组尚未实现时，其余命令仍然可用。
"""

"""面向 AI Agent 的命令行层：不依赖 PyQt6，可被单文件 exe 的 CLI 模式直接调用。

模块划分（早期任务只建立基础契约，命令模块由后续任务逐步加入）：

- ``errors``：稳定的错误码、退出码与 :class:`CliError`；
- ``output``：JSON 信封、NDJSON 事件与 UTF-8 标准流；
- ``inputs``：``--text/--hex/--file/--stdin`` 互斥输入；
- ``reads``：有界的等待读取与流式选项。

本包顶层不导入任何子模块与 Qt，避免离线命令启动时产生额外开销。
"""

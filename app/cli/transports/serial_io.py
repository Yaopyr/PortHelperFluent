"""串口 transport 的惰性桥接。

模块导入只依赖标准库：``app.io.serial_worker`` 会连带加载 pyserial 与 PyQt6，
因此所有 ``from app.io.serial_worker import ...`` 都放在函数内部。CLI 命令只调用本
模块的 :func:`list_ports` 与 :func:`open_transport`，测试可以 monkeypatch 这两个
模块级函数替换成内存 transport。
"""

from __future__ import annotations

from typing import Any

__all__ = ["list_ports", "open_transport"]


def list_ports() -> list:  # 元素类型是惰性导入的 app.io.serial_worker.PortInfo
    """枚举当前串口，返回 ``PortInfo`` 列表（未打开任何设备）。"""
    from app.io.serial_worker import list_ports as worker_list_ports

    return worker_list_ports()


def open_transport() -> Any:
    """构造 pyserial transport；真正打开端口由调用方执行 ``open(config)``。"""
    from app.io.serial_worker import PyserialTransport

    return PyserialTransport()

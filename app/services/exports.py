"""导出文件相关的通用工具：文件名清洗、重名去重与原子写入。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_FORBIDDEN_CHARS = frozenset('<>:"/\\|?*')


def sanitize_name(name: str, fallback: str = "data") -> str:
    """移除 Windows 文件名非法字符与控制字符，保留中文；结果为空时返回 fallback。"""
    cleaned = "".join(
        char
        for char in name
        if char not in _FORBIDDEN_CHARS and ord(char) >= 32 and ord(char) != 127
    )
    # Windows 不允许文件名以空格或点结尾。
    cleaned = cleaned.strip(" .")
    return cleaned or fallback


def unique_path(path: Path) -> Path:
    """若路径已存在，则在扩展名前追加 `_1`、`_2` 直到不冲突。"""
    path = Path(path)
    if not path.exists():
        return path
    index = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def write_text_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    """先写临时文件再替换，避免导出中断留下半截文件。"""
    write_bytes_atomic(path, text.encode(encoding))


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

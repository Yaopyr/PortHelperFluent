"""导出文件相关的通用工具：文件名清洗、重名去重与原子写入。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_FORBIDDEN_CHARS = frozenset('<>:"/\\|?*')
_MAX_NAME_LENGTH = 120
_FALLBACK_NAME = "data"
# Windows 设备名（CON、NUL、COM1-LPT9 等）即使带扩展名也不能用作文件名。
_RESERVED_NAMES = frozenset(
    ("CON", "PRN", "AUX", "NUL")
    + tuple(f"COM{index}" for index in range(1, 10))
    + tuple(f"LPT{index}" for index in range(1, 10))
)


def sanitize_name(name: str, fallback: str = _FALLBACK_NAME) -> str:
    """移除 Windows 文件名非法字符与控制字符，保留中文。

    fallback 同样会经过清洗与保留名校验（不会把 `../x` 原样返回）；名字与
    fallback 清洗后都为空时返回终极默认名 `data`。结果最多 120 个字符，且
    不会以空格或点结尾；设备名会加 `_` 前缀（如 `CON` -> `_CON`）。
    """
    cleaned = _sanitize_component(name) or _sanitize_component(fallback)
    return cleaned or _FALLBACK_NAME


def _sanitize_component(text: str) -> str:
    """清洗单个候选文件名；清洗后为空时返回空字符串，由调用方决定回退。"""
    cleaned = "".join(
        char
        for char in text
        if char not in _FORBIDDEN_CHARS and ord(char) >= 32 and ord(char) != 127
    )
    # Windows 不允许文件名以空格或点结尾。
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return ""
    if cleaned.split(".", 1)[0].rstrip(" .").upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    # 截断后可能重新露出空格或点（例如第 120 个字符正好是空格），必须再清理。
    return cleaned[:_MAX_NAME_LENGTH].rstrip(" .")


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

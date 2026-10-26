"""应用配置：默认值合并、损坏文件备份与原子保存。"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS: dict[str, Any] = {
    "app.version": "1.0.0",
    "ui.theme": "auto",
    "ui.accent": "#0A84FF",
    "ui.window_geometry": "",
    "ui.max_display_lines": 20000,
    "ui.auto_scroll": True,
    "ui.confirm_exit": True,
    "serial.port": "",
    "serial.baud": 115200,
    "serial.data_bits": 8,
    "serial.stop_bits": 1,
    "serial.parity": "none",
    "serial.flow": "none",
    "serial.encoding": "utf-8",
    "serial.hex_send": False,
    "serial.hex_display": False,
    "serial.timestamp": True,
    "net.host": "127.0.0.1",
    "net.port": 8080,
    "net.udp_port": 8080,
    "net.encoding": "utf-8",
    "server.port": 8080,
    "server.max_clients": 16,
    "hid.report_id": 0,
    "hid.report_length": 64,
    "checksum.encoding": "utf-8",
    "paths.last_export": "",
}


class Settings:
    """基于 JSON 文件的配置容器；未知键在读写时原样保留。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._data: dict[str, Any] = copy.deepcopy(DEFAULT_SETTINGS)
        self._warning: str | None = None

    @property
    def warning(self) -> str | None:
        """上一次 load 遇到问题时的中文提示，正常时为 None。"""
        return self._warning

    def load(self) -> None:
        self._data = copy.deepcopy(DEFAULT_SETTINGS)
        self._warning = None
        if not self._path.is_file():
            return
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._recover_from_bad_file(exc)
            return
        if not isinstance(loaded, dict):
            self._recover_from_bad_file(ValueError("配置内容不是 JSON 对象"))
            return
        self._data.update(loaded)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self._data, ensure_ascii=False, indent=2)
        fd, temp_name = tempfile.mkstemp(prefix=f"{self._path.name}.", suffix=".tmp", dir=self._path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, values: dict[str, Any]) -> None:
        self._data.update(values)

    def reset(self) -> None:
        self._data = copy.deepcopy(DEFAULT_SETTINGS)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def _recover_from_bad_file(self, error: Exception) -> None:
        backup = self._path.with_name(f"config.bad-{datetime.now():%Y%m%d-%H%M%S}.json")
        try:
            self._path.replace(backup)
            backup_note = f"已备份为 {backup.name}"
        except OSError:
            backup_note = "备份失败，请检查文件权限"
        self._warning = f"配置文件损坏（{error}），{backup_note}，已恢复默认设置。"

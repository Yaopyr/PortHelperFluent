"""应用配置：默认值合并、损坏文件备份与原子保存。"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from app import APP_VERSION

DEFAULT_SETTINGS: dict[str, Any] = {
    "app.version": APP_VERSION,
    "ui.theme": "auto",
    "ui.accent": "#0A84FF",
    "ui.window_geometry": "",
    "ui.max_display_lines": 20000,
    "ui.auto_scroll": True,
    # 启动静默检查 GitHub Releases 的开关；关闭后启动时不再发起任何更新请求。
    "ui.check_updates_on_start": True,
    "ui.confirm_exit": True,
    # 设置页的“默认编码/日志字体大小”在下次打开相关页面时生效。
    "ui.default_encoding": "utf-8",
    "ui.log_font_size": 10,
    "serial.port": "",
    "serial.baud": 115200,
    "serial.data_bits": 8,
    "serial.stop_bits": 1,
    "serial.parity": "none",
    "serial.flow": "none",
    "serial.encoding": None,
    "serial.hex_send": False,
    "serial.hex_display": False,
    "serial.timestamp": True,
    "net.host": "127.0.0.1",
    "net.port": 8080,
    "net.udp_port": 8080,
    "net.encoding": None,
    "server.port": 8080,
    "server.max_clients": 16,
    "hid.report_id": 0,
    "hid.report_length": 64,
    "checksum.encoding": None,
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
            # utf-8-sig 兼容带 BOM 的合法配置，避免把记事本等工具写出的 BOM 当损坏。
            text = self._path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            self._recover_from_bad_file(exc)
            return
        except OSError as exc:
            # 读不了（只读/被占用/权限不足）不等于内容损坏：保留默认值且绝不移动文件，
            # 否则用户修复权限后配置已经不见了。
            self._warning = (
                f"无法读取配置文件 {self._path}（{exc}），已使用默认设置；"
                "原文件未作改动，请检查文件权限或是否被其他程序占用。"
            )
            return
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
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
        # 保存成功后配置文件已经有效，上一次 load 的损坏/只读警告不再成立。
        self._warning = None

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, values: dict[str, Any]) -> None:
        self._data.update(values)

    def reset(self) -> None:
        self._data = copy.deepcopy(DEFAULT_SETTINGS)
        self._warning = None

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def _recover_from_bad_file(self, error: Exception) -> None:
        backup: Path | None = None
        try:
            backup = self._unique_backup_path()
            self._path.replace(backup)
        except OSError:
            if backup is not None:
                try:
                    backup.unlink()
                except OSError:
                    pass
            backup_note = "备份失败，请检查文件权限"
        else:
            backup_note = f"已备份为 {backup.name}"
        self._warning = f"配置文件损坏（{error}），{backup_note}，已恢复默认设置。"

    def _unique_backup_path(self) -> Path:
        """生成同一秒内也不会重名的备份路径，并以独占方式占位。

        文件名带微秒与 PID；极端情况下（同一微秒、同一进程）出现重名时再追加
        `-1`、`-2` 后缀。`O_EXCL` 保证占位是原子的，不会覆盖已有备份。
        """
        timestamp = datetime.now()
        stamp = f"{timestamp:%Y%m%d-%H%M%S}-{timestamp.microsecond:06d}-pid{os.getpid()}"
        for attempt in range(1000):
            suffix = f"-{attempt}" if attempt else ""
            candidate = self._path.with_name(f"config.bad-{stamp}{suffix}.json")
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            os.close(fd)
            return candidate
        raise OSError(f"无法在 {self._path.parent} 中创建唯一的配置备份文件名")


def resolve_page_encoding(settings: Settings, page_key: str) -> str:
    """页面自己的编码优先；未设置时使用设置页的默认编码。"""
    value = settings.get(page_key)
    if value:
        return str(value)
    fallback = settings.get("ui.default_encoding") or "utf-8"
    return str(fallback)

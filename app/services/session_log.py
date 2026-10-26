"""会话记录：保存收发原始字节、统计收发量并导出 TXT/CSV。"""

from __future__ import annotations

import csv
import io
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.encoding import decode_bytes, to_hex
from app.services.exports import write_text_atomic


@dataclass(frozen=True)
class LogRecord:
    timestamp: datetime
    direction: str  # "rx"、"tx" 或 "info"
    source: str
    data: bytes


class SessionLog:
    """内存会话记录。

    记录数或字节数超过上限时丢弃最旧记录，但累计收发字节数保留生命周期总量，
    因此界面上的收发计数不会因丢帧或清空记录而回退。
    """

    def __init__(self, max_bytes: int = 8 * 1024 * 1024, max_records: int = 100_000) -> None:
        if max_bytes <= 0 or max_records <= 0:
            raise ValueError("会话记录上限必须大于 0")
        self._max_bytes = max_bytes
        self._max_records = max_records
        self._records: deque[LogRecord] = deque()
        self._kept_bytes = 0
        self._lifetime_bytes: dict[str, int] = {"rx": 0, "tx": 0, "info": 0}

    def add(self, record: LogRecord) -> None:
        self._records.append(record)
        self._kept_bytes += len(record.data)
        self._lifetime_bytes[record.direction] = (
            self._lifetime_bytes.get(record.direction, 0) + len(record.data)
        )
        # 至少保留最新一条，避免单条超长记录把日志清空。
        while len(self._records) > 1 and (
            len(self._records) > self._max_records or self._kept_bytes > self._max_bytes
        ):
            dropped = self._records.popleft()
            self._kept_bytes -= len(dropped.data)

    def add_bytes(self, data: bytes, direction: str, source: str = "") -> LogRecord:
        record = LogRecord(datetime.now(), direction, source, bytes(data))
        self.add(record)
        return record

    def clear(self) -> None:
        self._records.clear()
        self._kept_bytes = 0

    def records(self) -> tuple[LogRecord, ...]:
        return tuple(self._records)

    def total_bytes(self, direction: str | None = None) -> int:
        if direction is None:
            return sum(self._lifetime_bytes.values())
        return self._lifetime_bytes.get(direction, 0)

    def export_txt(self, path: Path, codec: str = "utf-8", hex_mode: bool = False) -> None:
        """导出文本；文件固定 UTF-8，codec 只用于把原始字节解码成文本列。"""
        lines = []
        for record in self._records:
            payload = to_hex(record.data) if hex_mode else decode_bytes(record.data, codec)
            prefix = f"{record.source}: " if record.source else ""
            lines.append(
                f"[{_format_time(record.timestamp)}] [{record.direction.upper()}] {prefix}{payload}"
            )
        write_text_atomic(Path(path), "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def export_csv(self, path: Path, codec: str = "utf-8", hex_mode: bool = False) -> None:
        """导出 CSV；文件使用带 BOM 的 UTF-8，hex_mode 为 True 时 text 列留空。"""
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(["time", "direction", "source", "length", "hex", "text"])
        for record in self._records:
            writer.writerow(
                [
                    _format_datetime(record.timestamp),
                    record.direction.upper(),
                    record.source,
                    len(record.data),
                    to_hex(record.data),
                    "" if hex_mode else decode_bytes(record.data, codec),
                ]
            )
        write_text_atomic(Path(path), buffer.getvalue(), encoding="utf-8-sig")


def _format_time(timestamp: datetime) -> str:
    return f"{timestamp:%H:%M:%S}.{timestamp.microsecond // 1000:03d}"


def _format_datetime(timestamp: datetime) -> str:
    return f"{timestamp:%Y-%m-%d %H:%M:%S}.{timestamp.microsecond // 1000:03d}"

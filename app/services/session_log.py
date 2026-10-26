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

DATA_DIRECTIONS = ("rx", "tx")


@dataclass(frozen=True)
class LogRecord:
    timestamp: datetime
    direction: str  # "rx"、"tx" 或 "info"
    source: str
    data: bytes


class SessionLog:
    """内存会话记录，仅供 GUI 主线程使用（非线程安全）。

    工作线程先通过信号把数据投递到 GUI 线程，再调用本类；不要在串口/网络线程
    直接读写。记录数或保留字节数超过上限时先截断单条超长记录（只保留最新字节），
    再丢弃最旧记录；累计收发字节数保留生命周期总量，因此界面上的收发计数不会因
    丢帧、截断或清空记录而回退。截断/丢弃造成的损失可通过 `dropped_bytes` 与
    `dropped_records` 查看，`reset_counters()` 只清零生命周期字节计数。
    """

    def __init__(self, max_bytes: int = 8 * 1024 * 1024, max_records: int = 100_000) -> None:
        if max_bytes <= 0 or max_records <= 0:
            raise ValueError("会话记录上限必须大于 0")
        self._max_bytes = max_bytes
        self._max_records = max_records
        self._records: deque[LogRecord] = deque()
        self._kept_bytes = 0
        self._lifetime_bytes: dict[str, int] = {"rx": 0, "tx": 0, "info": 0}
        self._dropped_bytes = 0
        self._dropped_records = 0

    @property
    def dropped_bytes(self) -> int:
        """因单条截断或旧记录被丢弃而未保留的字节总数（生命周期累计）。"""
        return self._dropped_bytes

    @property
    def dropped_records(self) -> int:
        """因记录数或字节上限被丢弃的完整记录条数（单条截断不计入）。"""
        return self._dropped_records

    def add(self, record: LogRecord) -> None:
        full_size = len(record.data)
        self._lifetime_bytes[record.direction] = (
            self._lifetime_bytes.get(record.direction, 0) + full_size
        )
        if full_size > self._max_bytes:
            # 单条记录超过字节上限时保留最新 max_bytes 字节，确保保留量不超限。
            record = LogRecord(
                timestamp=record.timestamp,
                direction=record.direction,
                source=record.source,
                data=bytes(record.data[-self._max_bytes :]),
            )
            self._dropped_bytes += full_size - self._max_bytes
        self._records.append(record)
        self._kept_bytes += len(record.data)
        while self._records and (
            len(self._records) > self._max_records or self._kept_bytes > self._max_bytes
        ):
            dropped = self._records.popleft()
            self._kept_bytes -= len(dropped.data)
            self._dropped_records += 1
            self._dropped_bytes += len(dropped.data)

    def add_bytes(self, data: bytes, direction: str, source: str = "") -> LogRecord:
        """追加一条记录；返回值是调用方创建的完整记录（存储副本可能因字节上限被截断）。"""
        record = LogRecord(datetime.now(), direction, source, bytes(data))
        self.add(record)
        return record

    def clear(self) -> None:
        """清空已保留的记录与占用字节；生命周期计数与丢弃统计保持不变。"""
        self._records.clear()
        self._kept_bytes = 0

    def reset_counters(self) -> None:
        """清零生命周期 RX/TX/info 字节计数，但保留记录内容与丢弃统计。"""
        self._lifetime_bytes = {"rx": 0, "tx": 0, "info": 0}

    def records(self) -> tuple[LogRecord, ...]:
        return tuple(self._records)

    def total_bytes(self, direction: str | None = None) -> int:
        """累计字节数；direction 为 None 时只统计 RX+TX，info 不计入界面收发计数。"""
        if direction is None:
            return sum(self._lifetime_bytes.get(item, 0) for item in DATA_DIRECTIONS)
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

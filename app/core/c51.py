"""C51 向导核心：8051 定时器/波特率计算、寄存器位域与 C/Keil 工程生成。

纯 Python 实现（不导入 Qt），供页面与单元测试直接调用。Keil 工程文件均为
本模块自行编写的最小模板，不复制任何 Keil 原始文件。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PORT_NAMES = ("P0", "P1", "P2", "P3")
TIMER_COUNT = 3
POWER_MODES = ("normal", "idle", "powerdown")
SERIAL_GENERATORS = ("t1", "t2")
HIGH_PRIORITY_KEYS = ("ex0", "et0", "ex1", "et1", "es", "et2")
MIN_CRYSTAL_MHZ = 0.1
MAX_CRYSTAL_MHZ = 100.0
BAUD_WARNING_PERCENT = 5.0
MAX_TIMER_US = 10_000_000

_MODE_LABELS = {
    0: "方式 0（13 位）",
    1: "方式 1（16 位）",
    2: "方式 2（8 位自动重装）",
    3: "方式 3（双 8 位）",
}
# 中断号与中文名称按向量顺序排列，用于 ISR 生成。
_INTERRUPT_TABLE = (
    ("ex0", "外部中断 0", 0, "ext0_isr"),
    ("et0", "定时器 0", 1, "timer0_isr"),
    ("ex1", "外部中断 1", 2, "ext1_isr"),
    ("et1", "定时器 1", 3, "timer1_isr"),
    ("es", "串口", 4, "uart_isr"),
    ("et2", "定时器 2", 5, "timer2_isr"),
)
_PRIORITY_ALIASES = {
    "ex0": "ex0", "int0": "ex0", "px0": "ex0",
    "et0": "et0", "timer0": "et0", "t0": "et0", "pt0": "et0",
    "ex1": "ex1", "int1": "ex1", "px1": "ex1",
    "et1": "et1", "timer1": "et1", "t1": "et1", "pt1": "et1",
    "es": "es", "serial": "es", "uart": "es", "ps": "es",
    "et2": "et2", "timer2": "et2", "t2": "et2", "pt2": "et2",
}
# 8051 中断向量地址；STARTUP.A51 为未启用的向量生成 RETI 兜底。
_VECTOR_ADDRESSES = (0x03, 0x0B, 0x13, 0x1B, 0x23, 0x2B)


@dataclass
class TimerConfig:
    mode: int = 1
    enabled: bool = False
    gate: bool = False
    time_us: int = 1000
    reload: int | None = None


@dataclass
class SerialConfig:
    mode: int = 1
    baud: int = 9600
    ren: bool = True
    sm2: bool = False
    tb8: bool = False
    rb8: bool = False
    smod: bool = False
    generator: str = "t1"


@dataclass
class InterruptConfig:
    ea: bool = True
    ex0: bool = False
    et0: bool = False
    ex1: bool = False
    et1: bool = False
    es: bool = False
    et2: bool = False
    high_priority: set[str] = field(default_factory=set)
    ext0_edge: bool = False
    ext1_edge: bool = False


@dataclass
class C51Config:
    crystal_mhz: float = 12.0
    double_speed: bool = False
    ports: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in PORT_NAMES}
    )
    timers: list[TimerConfig] = field(
        default_factory=lambda: [TimerConfig() for _ in range(TIMER_COUNT)]
    )
    serial: SerialConfig = field(default_factory=SerialConfig)
    interrupts: InterruptConfig = field(default_factory=InterruptConfig)
    power_mode: str = "normal"
    delay_us: int = 0

    def to_dict(self) -> dict[str, Any]:
        """返回可直接写入 JSON 的配置字典（集合转换为排序列表）。"""
        serial = _serial_config(self)
        ports = getattr(self, "ports", None)
        ports = ports if isinstance(ports, dict) else {}
        return {
            "crystal_mhz": _as_float(getattr(self, "crystal_mhz", 12.0), 12.0),
            "double_speed": _as_bool(getattr(self, "double_speed", False), False),
            "ports": {
                name: _clamp_int(ports.get(name, 0), 0, 0xFF, 0)
                for name in PORT_NAMES
            },
            "timers": [
                _timer_to_dict(_timer_config(self, index))
                for index in range(TIMER_COUNT)
            ],
            "serial": {
                "mode": _effective_serial_mode(self),
                "baud": _clamp_int(serial.baud, 1, 4_000_000, 9600),
                "ren": bool(serial.ren),
                "sm2": bool(serial.sm2),
                "tb8": bool(serial.tb8),
                "rb8": bool(serial.rb8),
                "smod": bool(serial.smod),
                "generator": _serial_generator(self),
            },
            "interrupts": {
                key: bool(getattr(self.interrupts, key, False))
                for key in ("ea", "ex0", "et0", "ex1", "et1", "es", "et2")
            }
            | {
                "high_priority": sorted(_high_priority(self)),
                "ext0_edge": bool(getattr(self.interrupts, "ext0_edge", False)),
                "ext1_edge": bool(getattr(self.interrupts, "ext1_edge", False)),
            },
            "power_mode": self.power_mode if self.power_mode in POWER_MODES else "normal",
            "delay_us": _clamp_int(self.delay_us, 0, MAX_TIMER_US, 0),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "C51Config":
        """从 JSON 字典恢复配置；缺失或非法字段回退到安全默认值。"""
        data = data if isinstance(data, dict) else {}
        raw_timers = data.get("timers")
        raw_timers = raw_timers if isinstance(raw_timers, list) else []
        timers = [
            _timer_from_dict(
                raw_timers[index]
                if index < len(raw_timers) and isinstance(raw_timers[index], dict)
                else {},
                index,
            )
            for index in range(TIMER_COUNT)
        ]
        raw_ports = data.get("ports")
        raw_ports = raw_ports if isinstance(raw_ports, dict) else {}
        ports = {
            name: _clamp_int(raw_ports.get(name, 0), 0, 0xFF, 0)
            for name in PORT_NAMES
        }
        raw_serial = data.get("serial")
        raw_serial = raw_serial if isinstance(raw_serial, dict) else {}
        serial_mode = _clamp_int(raw_serial.get("mode", 1), 0, 3, 1)
        generator = str(raw_serial.get("generator", "t1")).strip().lower()
        serial = SerialConfig(
            mode=serial_mode,
            baud=_clamp_int(raw_serial.get("baud", 9600), 1, 4_000_000, 9600),
            ren=_as_bool(raw_serial.get("ren", True), True),
            sm2=_as_bool(raw_serial.get("sm2", False), False),
            tb8=_as_bool(raw_serial.get("tb8", False), False),
            rb8=_as_bool(raw_serial.get("rb8", False), False),
            smod=_as_bool(raw_serial.get("smod", False), False),
            generator=generator if generator in SERIAL_GENERATORS else "t1",
        )
        raw_interrupts = data.get("interrupts")
        raw_interrupts = raw_interrupts if isinstance(raw_interrupts, dict) else {}
        raw_priority = raw_interrupts.get("high_priority")
        if isinstance(raw_priority, (list, tuple, set, frozenset)):
            priority = {
                _PRIORITY_ALIASES.get(str(item).strip().lower(), "")
                for item in raw_priority
            } & set(HIGH_PRIORITY_KEYS)
        else:
            priority = set()
        interrupts = InterruptConfig(
            ea=_as_bool(raw_interrupts.get("ea", True), True),
            ex0=_as_bool(raw_interrupts.get("ex0", False), False),
            et0=_as_bool(raw_interrupts.get("et0", False), False),
            ex1=_as_bool(raw_interrupts.get("ex1", False), False),
            et1=_as_bool(raw_interrupts.get("et1", False), False),
            es=_as_bool(raw_interrupts.get("es", False), False),
            et2=_as_bool(raw_interrupts.get("et2", False), False),
            high_priority=priority,
            ext0_edge=_as_bool(raw_interrupts.get("ext0_edge", False), False),
            ext1_edge=_as_bool(raw_interrupts.get("ext1_edge", False), False),
        )
        power_mode = str(data.get("power_mode", "normal")).strip().lower()
        return cls(
            crystal_mhz=_clamp_float(
                data.get("crystal_mhz", 12.0),
                MIN_CRYSTAL_MHZ,
                MAX_CRYSTAL_MHZ,
                12.0,
            ),
            double_speed=_as_bool(data.get("double_speed", False), False),
            ports=ports,
            timers=timers,
            serial=serial,
            interrupts=interrupts,
            power_mode=power_mode if power_mode in POWER_MODES else "normal",
            delay_us=_clamp_int(data.get("delay_us", 0), 0, MAX_TIMER_US, 0),
        )


@dataclass(frozen=True)
class TimerResult:
    th: int
    tl: int
    counts: int
    warnings: list[str] = field(default_factory=list)


# ---- 定时器 ------------------------------------------------------------


def timer_reload(cfg: C51Config, index: int) -> TimerResult:
    """计算指定定时器的重载值与 TH/TL；越界只警告并按下限/上限处理。"""
    if index not in (0, 1, 2):
        raise ValueError(f"定时器编号必须是 0/1/2，收到 {index!r}")
    timer = _timer_config(cfg, index)
    raw_mode = _as_int(timer.mode, 1)
    warnings: list[str] = []
    if index == 2:
        # 定时器 2 固定为 16 位自动重装，不参与方式设置。
        mode = 1
    else:
        mode = raw_mode if raw_mode in (0, 1, 2, 3) else 1
        if raw_mode not in (0, 1, 2, 3):
            warnings.append(f"方式 {raw_mode} 无效，已按方式 1（16 位）计算。")
        if index == 1 and mode == 3:
            warnings.append("方式 3 仅适用于定时器 0，定时器 1 已按方式 1（16 位）计算。")
            mode = 1
    max_counts = _timer_max_counts(index, mode)
    machine = _machine_cycles_per_second(cfg)
    counts = 0
    if machine <= 0:
        warnings.append("晶振频率必须大于 0，无法计算定时器重载值。")
        reload = max_counts
    elif timer.reload is not None:
        raw_reload = _as_int(timer.reload, 0)
        limit = max_counts - 1
        if raw_reload < 0 or raw_reload > limit:
            warnings.append(
                f"重载值 {raw_reload} 超出方式 {mode} 的范围 0–0x{limit:02X}"
                f"（{limit}），已按边界值计算。"
            )
            raw_reload = min(max(raw_reload, 0), limit)
        reload = raw_reload
        counts = max_counts - reload
    else:
        time_us = max(0, _as_int(timer.time_us, 0))
        counts = int(round(time_us * 1e-6 * machine))
        if counts > max_counts:
            max_us = max_counts / machine * 1e6
            warnings.append(
                f"定时时间 {time_us} us 超过方式 {mode} 的最大定时 {max_us:.1f} us，"
                "已按最大定时计算；请降低定时时间或在中断中累加计数。"
            )
            counts = max_counts
        reload = max_counts - counts
    if index == 2:
        th, tl = (reload >> 8) & 0xFF, reload & 0xFF
    elif mode == 0:
        th, tl = (reload >> 5) & 0xFF, reload & 0x1F
    elif mode == 2:
        th = tl = reload & 0xFF
    elif mode == 3:
        th = tl = reload & 0xFF
    else:
        th, tl = (reload >> 8) & 0xFF, reload & 0xFF
    if mode == 3 and index == 0:
        warnings.append(
            "定时器 0 方式 3 会拆分为两个 8 位定时器：TL0 使用 TR0，"
            "TH0 借用 TR1，定时器 1 不能独立启停。"
        )
    return TimerResult(th=th, tl=tl, counts=counts, warnings=warnings)


# ---- 串口 / 波特率 -----------------------------------------------------


def baud_reload(cfg: C51Config) -> dict[str, int | float | list[str]]:
    """计算串口波特率与实际误差；返回 TH/TL 或 RCAP2 重载值。"""
    serial = _serial_config(cfg)
    mode = _effective_serial_mode(cfg)
    warnings: list[str] = []
    raw_mode = _as_int(serial.mode, 1)
    if raw_mode not in (0, 1, 2, 3):
        warnings.append(f"串口方式 {raw_mode} 无效，已按方式 1 计算。")
    baud = _as_int(serial.baud, 9600)
    if baud <= 0:
        warnings.append("波特率必须大于 0，已按 9600 bps 计算。")
        baud = 9600
    fosc = _crystal_hz(cfg)
    reload = th = tl = rcap2h = rcap2l = 0
    actual = 0.0
    if fosc <= 0:
        warnings.append("晶振频率必须大于 0，无法计算波特率。")
    elif mode == 0:
        actual = fosc / 12.0
    elif mode == 2:
        actual = (2.0 if serial.smod else 1.0) * fosc / 64.0
    else:
        generator = _serial_generator(cfg)
        if generator == "t2":
            raw = 65536 - int(round(fosc / (32.0 * baud)))
            if raw < 0 or raw > 0xFFFF:
                warnings.append(
                    f"定时器 2 重载值 {raw} 超出 16 位范围 0–65535；"
                    "请降低波特率或提高晶振频率。"
                )
                raw = min(max(raw, 0), 0xFFFF)
            reload = raw
            th, tl = (raw >> 8) & 0xFF, raw & 0xFF
            rcap2h, rcap2l = th, tl
            actual = fosc / (32.0 * (65536 - reload))
        else:
            smod_factor = 2 if serial.smod else 1
            raw = 256 - int(round(fosc / (12.0 * 32.0 * baud) * smod_factor))
            if raw < 0 or raw > 0xFF:
                warnings.append(
                    f"定时器 1 重载值 {raw} 超出 8 位范围 0–255；"
                    "请降低波特率、提高晶振频率或设置 SMOD=1。"
                )
                raw = min(max(raw, 0), 0xFF)
            reload = raw
            th = tl = raw
            actual = smod_factor * fosc / (12.0 * 32.0 * (256 - reload))
    error = abs(actual - baud) / baud * 100.0 if baud and actual > 0 else 0.0
    if actual > 0 and error > BAUD_WARNING_PERCENT:
        fixed = "（该方式波特率由硬件固定）" if mode in (0, 2) else ""
        warnings.append(
            f"波特率误差 {error:.2f}% 超过 {BAUD_WARNING_PERCENT:.1f}%{fixed}："
            f"目标 {baud} bps，实际约 {actual:.1f} bps；请调整晶振频率或波特率。"
        )
    return {
        "mode": mode,
        "baud": int(round(actual)),
        "baud_exact": round(actual, 3),
        "requested": baud,
        "error_percent": round(error, 2),
        "reload": reload,
        "th": th,
        "tl": tl,
        "rcap2h": rcap2h,
        "rcap2l": rcap2l,
        "warnings": warnings,
    }


# ---- 寄存器 ------------------------------------------------------------


def build_registers(cfg: C51Config) -> dict[str, int]:
    """按配置生成需要写入的寄存器值（含端口与定时器重载寄存器）。"""
    serial = _serial_config(cfg)
    intr = _interrupts(cfg)
    timer0, timer1 = _timer_config(cfg, 0), _timer_config(cfg, 1)
    mode0 = _effective_mode(cfg, 0)
    tr0 = bool(timer0.enabled)
    tr1 = bool(
        timer1.enabled
        or _serial_uses_t1(cfg)
        or (timer0.enabled and mode0 == 3)
    )
    registers: dict[str, int] = {
        "TMOD": _tmod_value(cfg),
        "TCON": (
            (0x40 if tr1 else 0)
            | (0x10 if tr0 else 0)
            | (0x04 if intr.ext1_edge else 0)
            | (0x01 if intr.ext0_edge else 0)
        ),
        "SCON": _scon_value(cfg),
        "PCON": _pcon_value(cfg),
        "IE": _ie_value(cfg),
        "IP": _ip_value(cfg),
    }
    for name in PORT_NAMES:
        registers[name] = _port_value(cfg, name)
    if _timer_active(cfg, 0):
        result = timer_reload(cfg, 0)
        registers["TH0"], registers["TL0"] = result.th, result.tl
    if _serial_uses_t1(cfg):
        info = baud_reload(cfg)
        registers["TH1"] = int(info["th"]) & 0xFF
        registers["TL1"] = int(info["tl"]) & 0xFF
    elif _timer_active(cfg, 1):
        result = timer_reload(cfg, 1)
        registers["TH1"], registers["TL1"] = result.th, result.tl
    if _serial_uses_t2(cfg):
        info = baud_reload(cfg)
        high, low = int(info["rcap2h"]), int(info["rcap2l"])
        registers.update(
            {"TH2": high, "TL2": low, "RCAP2H": high, "RCAP2L": low, "T2CON": 0x34}
        )
    elif _timer2_active(cfg):
        result = timer_reload(cfg, 2)
        registers.update(
            {
                "TH2": result.th,
                "TL2": result.tl,
                "RCAP2H": result.th,
                "RCAP2L": result.tl,
                "T2CON": 0x04,
            }
        )
    return registers


def build_warnings(cfg: C51Config) -> list[str]:
    """汇总配置冲突与越界提示，供页面警告面板显示。"""
    warnings: list[str] = []
    crystal = _as_float(getattr(cfg, "crystal_mhz", 0.0), 0.0)
    if crystal <= 0:
        warnings.append("晶振频率必须大于 0。")
    elif not MIN_CRYSTAL_MHZ <= crystal <= MAX_CRYSTAL_MHZ:
        warnings.append(
            f"晶振频率 {crystal:.4f} MHz 超出常用范围 "
            f"{MIN_CRYSTAL_MHZ:.1f}–{MAX_CRYSTAL_MHZ:.0f} MHz，请确认硬件。"
        )
    for index in range(TIMER_COUNT):
        if index == 1 and _serial_uses_t1(cfg) and _effective_mode(cfg, 1) == 2:
            continue
        if index == 2 and _serial_uses_t2(cfg):
            continue
        for message in timer_reload(cfg, index).warnings:
            warnings.append(f"定时器 {index}：{message}")
    info = baud_reload(cfg)
    mode = int(info["mode"])
    for message in info["warnings"]:
        warnings.append(f"串口：{message}")
    serial = _serial_config(cfg)
    if serial.sm2 and mode in (0, 1):
        warnings.append("串口：SM2 只在方式 2/3 的多机通信中生效，方式 0/1 下设置无效。")
    if (serial.tb8 or serial.rb8) and mode in (0, 1):
        warnings.append("串口：TB8/RB8 只在方式 2/3 中使用，当前方式下不会生效。")
    if mode in (1, 3):
        generator = _serial_generator(cfg)
        if generator == "t1":
            raw_mode = _as_int(_timer_config(cfg, 1).mode, 1)
            if raw_mode != 2:
                warnings.append(
                    "串口：使用定时器 1 作为波特率发生器时将按方式 2"
                    f"（8 位自动重装）生成；当前选择的是方式 {raw_mode}，该选择不会生效。"
                )
            if _timer_config(cfg, 1).gate:
                warnings.append(
                    "串口：定时器 1 的 GATE 位对波特率发生器无效，生成代码会忽略 GATE。"
                )
            if _timer_config(cfg, 1).enabled:
                warnings.append(
                    "定时器 1 既被勾选为定时器运行，又用于串口波特率发生器；"
                    "生成代码将优先使用波特率重载值，TH1/TL1 不再按定时时间计算。"
                )
        else:
            if _timer_config(cfg, 2).enabled:
                warnings.append(
                    "定时器 2 既被勾选为定时器运行，又用于串口波特率发生器；"
                    "生成代码将优先使用波特率重载值。"
                )
            warnings.append(
                "串口：使用定时器 2 作为波特率发生器需要 8052 内核（reg52.h），"
                "生成代码会同时置位 RCLK/TCLK 与 TR2。"
            )
    if _effective_mode(cfg, 0) == 3 and mode in (1, 3) and _serial_generator(cfg) == "t1":
        warnings.append(
            "定时器 0 的方式 3 会占用 TR1，与串口使用 T1 作为波特率发生器冲突；"
            "请改用定时器 2 或调整定时器 0 的方式。"
        )
    if _interrupts(cfg).et2 and not _timer2_active(cfg):
        warnings.append("已使能定时器 2 中断（ET2），但定时器 2 未运行，中断不会触发。")
    if _interrupts(cfg).et1 and _serial_uses_t1(cfg):
        warnings.append(
            "串口使用定时器 1 作为波特率发生器时不应使能 ET1，"
            "否则每次溢出都会进入中断并影响波特率。"
        )
    power_mode = str(getattr(cfg, "power_mode", "normal"))
    if power_mode not in POWER_MODES:
        warnings.append(f"未知功耗模式 {power_mode!r}，已按正常模式生成。")
    elif power_mode == "powerdown":
        warnings.append("掉电模式会停止晶振，定时器、串口与中断计数都会停止工作。")
    return warnings


# ---- C 代码 ------------------------------------------------------------


def generate_c(cfg: C51Config) -> str:
    """生成带中文注释的 8051 初始化 C 代码。"""
    registers = build_registers(cfg)
    lines = ["/*"]
    lines.append(" * 8051 初始化程序（由 PortHelper Fluent C51 向导生成）")
    lines.append(" * 本文件为独立生成的示例代码，不包含任何 Keil 原始模板内容。")
    lines.append(" *")
    for text in _summary_lines(cfg):
        lines.append(f" * {text}")
    lines.append(" */")
    lines.append(
        f"#include <{'reg52.h' if _uses_timer2(cfg) else 'reg51.h'}>"
    )
    lines.append("")
    isr_lines = _isr_lines(cfg)
    if isr_lines:
        lines.extend(isr_lines)
        lines.append("")
    delay_lines = _delay_lines(cfg)
    if delay_lines:
        lines.extend(delay_lines)
        lines.append("")
    lines.extend(_main_lines(cfg, registers))
    return "\n".join(lines).rstrip() + "\n"


def generate_keil_project(cfg: C51Config, output_dir: Path) -> list[Path]:
    """写出 Main.c、M51Project.Uv2、M51Project.Opt 与自写 STARTUP.A51。"""
    directory = Path(output_dir)
    files = (
        (directory / "Main.c", generate_c(cfg), "utf-8"),
        (directory / "M51Project.Uv2", _keil_uv2(cfg), "ascii"),
        (directory / "M51Project.Opt", _keil_opt(cfg), "ascii"),
        (directory / "STARTUP.A51", _startup_a51(cfg), "ascii"),
    )
    written: list[Path] = []
    for path, text, encoding in files:
        _write_text(path, text, encoding)
        written.append(path)
    return written


# ---- 内部：配置读取 ----------------------------------------------------


def _timer_config(cfg: C51Config, index: int) -> TimerConfig:
    timers = getattr(cfg, "timers", None)
    if isinstance(timers, (list, tuple)) and 0 <= index < len(timers):
        timer = timers[index]
        if isinstance(timer, TimerConfig):
            return timer
    return TimerConfig()


def _serial_config(cfg: C51Config) -> SerialConfig:
    serial = getattr(cfg, "serial", None)
    return serial if isinstance(serial, SerialConfig) else SerialConfig()


def _interrupts(cfg: C51Config) -> InterruptConfig:
    interrupts = getattr(cfg, "interrupts", None)
    return interrupts if isinstance(interrupts, InterruptConfig) else InterruptConfig()


def _high_priority(cfg: C51Config) -> set[str]:
    raw = getattr(_interrupts(cfg), "high_priority", set()) or set()
    if not isinstance(raw, (set, frozenset, list, tuple)):
        return set()
    normalized = {
        _PRIORITY_ALIASES.get(str(item).strip().lower(), "") for item in raw
    }
    return normalized & set(HIGH_PRIORITY_KEYS)


def _effective_mode(cfg: C51Config, index: int) -> int:
    if index == 2:
        return 1
    mode = _as_int(_timer_config(cfg, index).mode, 1)
    if mode not in (0, 1, 2, 3):
        mode = 1
    # T1 作为串口波特率发生器时必须工作在方式 2（8 位自动重装），
    # 否则 16 位方式不会自动重载，实测波特率会漂移。
    if index == 1 and _serial_uses_t1(cfg):
        return 2
    if index == 1 and mode == 3:
        return 1
    return mode


def _effective_serial_mode(cfg: C51Config) -> int:
    mode = _as_int(_serial_config(cfg).mode, 1)
    return mode if mode in (0, 1, 2, 3) else 1


def _serial_generator(cfg: C51Config) -> str:
    generator = str(_serial_config(cfg).generator or "t1").strip().lower()
    return generator if generator in SERIAL_GENERATORS else "t1"


def _serial_uses_t1(cfg: C51Config) -> bool:
    return _effective_serial_mode(cfg) in (1, 3) and _serial_generator(cfg) == "t1"


def _serial_uses_t2(cfg: C51Config) -> bool:
    return _effective_serial_mode(cfg) in (1, 3) and _serial_generator(cfg) == "t2"


def _timer_active(cfg: C51Config, index: int) -> bool:
    if index == 1 and _serial_uses_t1(cfg):
        return True
    if index == 2 and _serial_uses_t2(cfg):
        return True
    return bool(_timer_config(cfg, index).enabled)


def _timer2_active(cfg: C51Config) -> bool:
    return _timer_active(cfg, 2)


def _uses_timer2(cfg: C51Config) -> bool:
    return _timer2_active(cfg) or _interrupts(cfg).et2


def _crystal_hz(cfg: C51Config) -> float:
    return max(0.0, _as_float(getattr(cfg, "crystal_mhz", 0.0), 0.0)) * 1e6


def _machine_cycles_per_second(cfg: C51Config) -> float:
    fosc = _crystal_hz(cfg)
    divisor = 6.0 if bool(getattr(cfg, "double_speed", False)) else 12.0
    return fosc / divisor if fosc > 0 else 0.0


def _machine_cycle_us(cfg: C51Config) -> float:
    cycles = _machine_cycles_per_second(cfg)
    return 1e6 / cycles if cycles > 0 else 0.0


def _timer_max_counts(index: int, mode: int) -> int:
    if index == 2:
        return 65536
    return {0: 8192, 1: 65536, 2: 256, 3: 256}.get(mode, 65536)


# ---- 内部：寄存器位域 --------------------------------------------------


def _tmod_value(cfg: C51Config) -> int:
    timer0, timer1 = _timer_config(cfg, 0), _timer_config(cfg, 1)
    value = 0
    if timer1.gate and not _serial_uses_t1(cfg):
        value |= 0x80
    value |= (_effective_mode(cfg, 1) & 0x03) << 4
    if timer0.gate:
        value |= 0x08
    value |= _effective_mode(cfg, 0) & 0x03
    return value & 0xFF


def _scon_value(cfg: C51Config) -> int:
    serial = _serial_config(cfg)
    mode = _effective_serial_mode(cfg)
    sm0 = 1 if mode in (2, 3) else 0
    sm1 = 1 if mode in (1, 3) else 0
    return (
        (sm0 << 7)
        | (sm1 << 6)
        | (0x20 if serial.sm2 else 0)
        | (0x10 if serial.ren else 0)
        | (0x08 if serial.tb8 else 0)
        | (0x04 if serial.rb8 else 0)
    )


def _pcon_value(cfg: C51Config) -> int:
    serial = _serial_config(cfg)
    power_mode = str(getattr(cfg, "power_mode", "normal"))
    return (
        (0x80 if serial.smod else 0)
        | (0x01 if power_mode == "idle" else 0)
        | (0x02 if power_mode == "powerdown" else 0)
    )


def _ie_value(cfg: C51Config) -> int:
    intr = _interrupts(cfg)
    return (
        (0x80 if intr.ea else 0)
        | (0x20 if intr.et2 else 0)
        | (0x10 if intr.es else 0)
        | (0x08 if intr.et1 else 0)
        | (0x04 if intr.ex1 else 0)
        | (0x02 if intr.et0 else 0)
        | (0x01 if intr.ex0 else 0)
    )


def _ip_value(cfg: C51Config) -> int:
    priority = _high_priority(cfg)
    return (
        (0x20 if "et2" in priority else 0)
        | (0x10 if "es" in priority else 0)
        | (0x08 if "et1" in priority else 0)
        | (0x04 if "ex1" in priority else 0)
        | (0x02 if "et0" in priority else 0)
        | (0x01 if "ex0" in priority else 0)
    )


def _port_value(cfg: C51Config, name: str) -> int:
    ports = getattr(cfg, "ports", None)
    if not isinstance(ports, dict):
        return 0
    return _clamp_int(ports.get(name, 0), 0, 0xFF, 0)


# ---- 内部：文本生成 ----------------------------------------------------


def _summary_lines(cfg: C51Config) -> list[str]:
    crystal = _as_float(getattr(cfg, "crystal_mhz", 0.0), 0.0)
    cycle_us = _machine_cycle_us(cfg)
    style = "6T（双倍速）" if bool(getattr(cfg, "double_speed", False)) else "12T"
    lines = [
        f"晶振：{crystal:.4f} MHz，{style} 模式，机器周期约 {cycle_us:.4f} us",
        "端口初值：" + "，".join(
            f"{name}=0x{_port_value(cfg, name):02X}" for name in PORT_NAMES
        ),
    ]
    for index in range(TIMER_COUNT):
        if index == 1 and _serial_uses_t1(cfg):
            info = baud_reload(cfg)
            lines.append(
                f"定时器 1：方式 2（串口波特率发生器），TH1=0x{int(info['th']):02X}，"
                f"实际 {int(info['baud'])} bps"
            )
            continue
        if index == 2 and _serial_uses_t2(cfg):
            info = baud_reload(cfg)
            lines.append(
                f"定时器 2：16 位自动重装（串口波特率发生器），"
                f"RCAP2=0x{int(info['rcap2h']):02X}{int(info['rcap2l']):02X}，"
                f"实际 {int(info['baud'])} bps"
            )
            continue
        timer = _timer_config(cfg, index)
        if not timer.enabled:
            lines.append(f"定时器 {index}：未启用")
            continue
        result = timer_reload(cfg, index)
        duration = result.counts * cycle_us
        time_text = f"{duration / 1000.0:.3f} ms" if duration >= 1000 else f"{duration:.1f} us"
        mode_text = "16 位自动重装" if index == 2 else f"方式 {_effective_mode(cfg, index)}"
        lines.append(
            f"定时器 {index}：{mode_text}，{result.counts} 个机器周期，"
            f"约 {time_text}，TH{index}=0x{result.th:02X}，TL{index}=0x{result.tl:02X}"
        )
    info = baud_reload(cfg)
    lines.append(
        f"串口：方式 {int(info['mode'])}，目标 {int(info['requested'])} bps，"
        f"实际约 {int(info['baud'])} bps（误差 {float(info['error_percent']):.2f}%）"
    )
    names = _enabled_interrupt_names(cfg)
    lines.append("中断：" + ("、".join(names) if names else "未使能"))
    power_labels = {"normal": "正常", "idle": "空闲", "powerdown": "掉电"}
    power = str(getattr(cfg, "power_mode", "normal"))
    delay = max(0, _as_int(getattr(cfg, "delay_us", 0), 0))
    lines.append(f"功耗模式：{power_labels.get(power, '正常')}；软件延时：{delay} us")
    return lines


def _enabled_interrupt_names(cfg: C51Config) -> list[str]:
    intr = _interrupts(cfg)
    return [label for key, label, _vector, _name in _INTERRUPT_TABLE if getattr(intr, key)]


def _main_lines(cfg: C51Config, registers: dict[str, int]) -> list[str]:
    lines = ["void main(void)", "{"]
    lines.append("    /* ---- 端口初始化 ---- */")
    for name in PORT_NAMES:
        value = registers[name]
        lines.append(_reg_line(name, value, f"{_bits(value)}，P{name[1]}.0–P{name[1]}.7 输出初值"))
    lines.append("")
    lines.append("    /* ---- 定时器 / 串口控制寄存器 ---- */")
    lines.append(_reg_line("TMOD", registers["TMOD"], _describe_tmod(cfg)))
    lines.append(
        _reg_line(
            "TCON",
            registers["TCON"] & ~0x50,
            _describe_tcon(cfg) + "；TR0/TR1 在初始化末尾启动",
        )
    )
    if "T2CON" in registers:
        lines.append(
            _reg_line(
                "T2CON",
                registers["T2CON"] & ~0x04,
                _describe_t2con(cfg) + "；TR2 在初始化末尾启动",
            )
        )
    lines.append(_reg_line("SCON", registers["SCON"], _describe_scon(cfg)))
    lines.append(_reg_line("PCON", registers["PCON"], _describe_pcon(cfg)))
    lines.append(_reg_line("IE", registers["IE"], _describe_ie(cfg)))
    lines.append(_reg_line("IP", registers["IP"], _describe_ip(cfg)))
    reload_names = (
        "TH0", "TL0", "TH1", "TL1", "TH2", "TL2", "RCAP2H", "RCAP2L"
    )
    if any(name in registers for name in reload_names):
        lines.append("")
        lines.append("    /* ---- 定时器重载值 ---- */")
        for name in reload_names:
            if name in registers:
                lines.append(
                    _reg_line(name, registers[name], _reload_comment(name, cfg))
                )
    lines.append("")
    lines.append("    /* ---- 启动定时器 ---- */")
    started = False
    if registers["TCON"] & 0x10:
        lines.append(_statement("TR0 = 1;", "启动定时器 0（方式 3 时仅启动 TL0）"))
        started = True
    if registers["TCON"] & 0x40:
        lines.append(_statement("TR1 = 1;", "启动定时器 1 / TH0（方式 3）"))
        started = True
    if "T2CON" in registers and registers["T2CON"] & 0x04:
        lines.append(_statement("TR2 = 1;", "启动定时器 2"))
        started = True
    if not started:
        lines.append("    /* 没有启用任何定时器 */")
    delay = max(0, _as_int(getattr(cfg, "delay_us", 0), 0))
    if delay > 0:
        lines.append("")
        lines.append(f"    /* 软件延时 {delay} us（按当前晶振估算，请用示波器校准） */")
        lines.append(_statement(f"delay_us({delay});", "粗略阻塞延时"))
    power = str(getattr(cfg, "power_mode", "normal"))
    if power == "idle":
        lines.append("")
        lines.append("    /* 进入空闲模式：CPU 停止，定时器/串口继续运行，任一中断唤醒 */")
        lines.append(_statement("PCON |= 0x01;", "空闲模式"))
    elif power == "powerdown":
        lines.append("")
        lines.append("    /* 进入掉电模式：晶振停止，外部中断或复位唤醒 */")
        lines.append(_statement("PCON |= 0x02;", "掉电模式"))
    lines.append("")
    lines.append("    /* 中断驱动的主循环：CPU 在此等待中断 */")
    lines.append("    while (1);")
    lines.append("}")
    return lines


def _isr_lines(cfg: C51Config) -> list[str]:
    intr = _interrupts(cfg)
    lines: list[str] = []
    for key, label, vector, name in _INTERRUPT_TABLE:
        if not getattr(intr, key):
            continue
        body: list[str] = []
        if key in ("ex0", "ex1"):
            edge = intr.ext0_edge if key == "ex0" else intr.ext1_edge
            title = f"{label}：{'下降沿' if edge else '低电平'}触发"
            body.append(f"    /* TODO: 在此添加{label}的处理代码 */")
        elif key == "et0":
            title, body = _timer_title_body(cfg, 0)
        elif key == "et1":
            title, body = _timer_title_body(cfg, 1)
        elif key == "et2":
            title, body = _timer_title_body(cfg, 2)
        else:
            title = "串口中断：RI/TI 共用中断 4"
            body = [
                "    if (RI)",
                "    {",
                "        RI = 0;",
                "        /* TODO: 读取 SBUF 处理接收数据 */",
                "    }",
                "    if (TI)",
                "    {",
                "        TI = 0;",
                "        /* TODO: 发送完成处理 */",
                "    }",
            ]
        lines.extend(
            _isr_block(name, vector, title, key in _high_priority(cfg), body)
        )
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _timer_title_body(cfg: C51Config, index: int) -> tuple[str, list[str]]:
    if index == 2:
        if _serial_uses_t2(cfg):
            return (
                "定时器 2：串口波特率发生器，16 位自动重装",
                [
                    "    TF2 = 0;",
                    "    /* 定时器 2 由 RCAP2 自动重装；本中断通常无需重载 */",
                    "    /* TODO: 在此添加定时器 2 的处理代码 */",
                ],
            )
        return (
            "定时器 2：16 位自动重装",
            [
                "    TF2 = 0;",
                "    /* 自动重装值来自 RCAP2H/RCAP2L，无需在此重写 TH2/TL2 */",
                "    /* TODO: 在此添加定时器 2 的处理代码 */",
            ],
        )
    if index == 1 and _serial_uses_t1(cfg):
        return (
            "定时器 1：串口波特率发生器（方式 2 自动重装），"
            "进入中断会影响波特率，建议关闭 ET1",
            ["    /* TODO: 定时器 1 已用于波特率发生器，通常不应使能本中断 */"],
        )
    mode = _effective_mode(cfg, index)
    result = timer_reload(cfg, index)
    title = f"定时器 {index}：方式 {mode}，{result.counts} 个机器周期"
    if mode in (0, 1):
        body = [
            f"    TH{index} = {_hex(result.th)};",
            f"    TL{index} = {_hex(result.tl)};",
            "    /* 方式 0/1 需要软件重装，以上两行不能省略 */",
        ]
    elif mode == 2:
        body = ["    /* 方式 2 由硬件自动重装，无需重写 TH/TL */"]
    else:
        body = ["    /* 方式 3：TH0 借用定时器 1 的控制位，注意与串口冲突 */"]
    body.append(f"    /* TODO: 在此添加定时器 {index} 的处理代码 */")
    return title, body


def _isr_block(
    name: str,
    vector: int,
    title: str,
    high_priority: bool,
    body: list[str],
) -> list[str]:
    # 高优先级中断使用寄存器组 1，避免与主循环争用工作寄存器。
    signature = f"void {name}(void) interrupt {vector}"
    if high_priority:
        signature += " using 1"
    return [f"/* {title}{'，高优先级' if high_priority else ''} */", signature, "{", *body, "}"]


def _delay_lines(cfg: C51Config) -> list[str]:
    delay = max(0, _as_int(getattr(cfg, "delay_us", 0), 0))
    if delay <= 0:
        return []
    cycle_us = _machine_cycle_us(cfg)
    loops = max(1, int(round(cycle_us))) if cycle_us > 0 else 1
    return [
        "/* 粗略软件延时：按当前晶振估算，编译器优化后可能偏短，请用示波器校准 */",
        "static void delay_us(unsigned long us)",
        "{",
        "    unsigned int i;",
        "",
        "    while (us--)",
        "    {",
        f"        for (i = 0; i < {loops}; i++)",
        "        {",
        "            ;",
        "        }",
        "    }",
        "}",
    ]


def _describe_tmod(cfg: C51Config) -> str:
    timer0, timer1 = _timer_config(cfg, 0), _timer_config(cfg, 1)
    parts: list[str] = []
    if timer1.gate and not _serial_uses_t1(cfg):
        parts.append("T1 GATE=1")
    parts.append(f"T1 {_MODE_LABELS[_effective_mode(cfg, 1)]}")
    if timer0.gate:
        parts.append("T0 GATE=1")
    parts.append(f"T0 {_MODE_LABELS[_effective_mode(cfg, 0)]}")
    return "，".join(parts)


def _describe_tcon(cfg: C51Config) -> str:
    intr = _interrupts(cfg)
    return (
        f"IT0={'1（INT0 下降沿）' if intr.ext0_edge else '0（INT0 低电平）'}，"
        f"IT1={'1（INT1 下降沿）' if intr.ext1_edge else '0（INT1 低电平）'}"
    )


def _describe_t2con(cfg: C51Config) -> str:
    if _serial_uses_t2(cfg):
        return "RCLK=1，TCLK=1（波特率由定时器 2 产生），16 位自动重装"
    return "定时器 2 16 位自动重装，C/T2=0"


def _describe_scon(cfg: C51Config) -> str:
    serial = _serial_config(cfg)
    return (
        f"串口方式 {_effective_serial_mode(cfg)}，"
        f"REN={int(serial.ren)}，SM2={int(serial.sm2)}，"
        f"TB8={int(serial.tb8)}，RB8={int(serial.rb8)}"
    )


def _describe_pcon(cfg: C51Config) -> str:
    serial = _serial_config(cfg)
    power = str(getattr(cfg, "power_mode", "normal"))
    labels = {"normal": "正常", "idle": "空闲", "powerdown": "掉电"}
    return f"SMOD={int(serial.smod)}，{labels.get(power, '正常')}功耗"


def _describe_ie(cfg: C51Config) -> str:
    intr = _interrupts(cfg)
    names = []
    if intr.ea:
        names.append("EA=1")
    for key, label, _vector, _name in _INTERRUPT_TABLE:
        if getattr(intr, key):
            names.append(f"{label} 使能")
    return "，".join(names) if names else "中断全部关闭"


def _describe_ip(cfg: C51Config) -> str:
    priority = _high_priority(cfg)
    if not priority:
        return "全部低优先级"
    labels = {key: label for key, label, _vector, _name in _INTERRUPT_TABLE}
    return "高优先级：" + "、".join(labels[key] for key in HIGH_PRIORITY_KEYS if key in priority)


def _reload_comment(name: str, cfg: C51Config) -> str:
    if name.startswith("RCAP2"):
        return "定时器 2 自动重装值（高/低 8 位）"
    index = int(name[-1])
    if name.startswith("TH") or name.startswith("TL"):
        high = name.startswith("TH")
        if index == 1 and _serial_uses_t1(cfg):
            return "串口 T1 波特率重载值" + ("（高 8 位，与 TL1 相同）" if high else "（低 8 位）")
        if index == 2 and _serial_uses_t2(cfg):
            return "定时器 2 波特率重载值" + ("（高 8 位）" if high else "（低 8 位）")
        return f"定时器 {index} 重载值" + ("（高 8 位）" if high else "（低 8 位）")
    return "寄存器值"


def _reg_line(name: str, value: int, comment: str) -> str:
    return f"    {name} = {_hex(value)};".ljust(32) + f"/* {comment} */"


def _statement(text: str, comment: str) -> str:
    return f"    {text}".ljust(36) + f"/* {comment} */"


def _hex(value: int) -> str:
    return f"0x{int(value) & 0xFF:02X}"


def _bits(value: int) -> str:
    bits = f"{int(value) & 0xFF:08b}"
    return f"{bits[:4]} {bits[4:]}"


# ---- 内部：Keil 工程 ---------------------------------------------------


def _keil_uv2(cfg: C51Config) -> str:
    crystal = _as_float(getattr(cfg, "crystal_mhz", 12.0), 12.0)
    return "\n".join(
        [
            "; uVision2 Project File",
            "; Generated by PortHelper Fluent C51 Wizard (self-written template).",
            "; No Keil original file is copied. Marker: PORTHELPER-SELF-WRITTEN-UV2",
            "",
            "[UV2]",
            'ORGANIZATION="PortHelper Fluent"',
            'VERSION="2.00"',
            "DEVICE=AT89C52",
            "CPUTYPE=8051",
            "TARGETNAME=M51Project",
            "",
            "[TOOLS]",
            r"TOOL0=.\C51\BIN\C51.EXE",
            r"TOOL1=.\C51\BIN\A51.EXE",
            r"TOOL2=.\C51\BIN\BL51.EXE",
            r"TOOL3=.\C51\BIN\OH51.EXE",
            r"TOOL4=.\C51\BIN\LIB51.EXE",
            "",
            "[GROUPS]",
            "GroupNumber=1",
            "Group0=Source",
            "Group0Count=2",
            "Group0File0=Main.c",
            "Group0File1=STARTUP.A51",
            "",
            "[FILES]",
            "FileNumber=2",
            "File0=Main.c",
            "File1=STARTUP.A51",
            "",
            "[OPTIONS]",
            "TargetName=M51Project",
            f"Crystal={crystal:.6f}",
            "CreateHexFile=1",
        ]
    ) + "\n"


def _keil_opt(cfg: C51Config) -> str:
    crystal = _as_float(getattr(cfg, "crystal_mhz", 12.0), 12.0)
    return "\n".join(
        [
            "; uVision2 Options File",
            "; Generated by PortHelper Fluent C51 Wizard (self-written, no Keil files copied).",
            "; Marker: PORTHELPER-SELF-WRITTEN-OPT",
            "",
            "[Target]",
            "TargetName=M51Project",
            "Device=AT89C52",
            f"Crystal={crystal:.6f}",
            "",
            "[Output]",
            "CreateHexFile=1",
            "DebugInformation=1",
            "",
            "[Compiler]",
            "Optimization=8",
        ]
    ) + "\n"


def _startup_a51(cfg: C51Config) -> str:
    intr = _interrupts(cfg)
    enabled = {
        key: getattr(intr, key)
        for key in ("ex0", "et0", "ex1", "et1", "es", "et2")
    }
    labels = {
        "ex0": "INT0",
        "et0": "Timer0",
        "ex1": "INT1",
        "et1": "Timer1",
        "es": "Serial",
        "et2": "Timer2",
    }
    lines = [
        "; STARTUP.A51 - minimal startup generated by PortHelper Fluent C51 Wizard",
        "; This file is self-written and is NOT a copy of the Keil STARTUP.A51 template.",
        "; Marker: PORTHELPER-SELF-WRITTEN-STARTUP",
        "",
        "NAME ?C_STARTUP",
        "",
        "?C_STARTUP SEGMENT CODE",
        "        RSEG ?C_STARTUP",
        "        EXTRN CODE (?C_START)",
        "",
        "        CSEG AT 0",
        "        LJMP STARTUP1",
        "",
        "; RETI stubs for unused interrupt vectors; enabled vectors are provided by C ISRs.",
    ]
    for key, address in zip(
        ("ex0", "et0", "ex1", "et1", "es", "et2"), _VECTOR_ADDRESSES
    ):
        if enabled[key]:
            lines.append(f"; 0x{address:02X}  {labels[key]}: C interrupt function")
        else:
            lines.extend(
                [
                    f"        CSEG AT 0x{address:02X}",
                    "        RETI",
                    f"; {labels[key]}",
                ]
            )
    lines.extend(
        [
            "",
            "; Startup code begins at 0x30 (ORG 0x30), after the vector table.",
            "        CSEG AT 0x30",
            "STARTUP1:",
            "        MOV SP, #0x60",
            "; Add IDATA clear or bit-area init here if your application needs it.",
            "        LJMP ?C_START",
            "",
            "        END",
        ]
    )
    return "\n".join(lines) + "\n"


# ---- 内部：文件与类型工具 ----------------------------------------------


def _write_text(path: Path, text: str, encoding: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _timer_to_dict(timer: TimerConfig) -> dict[str, Any]:
    return {
        "mode": _as_int(timer.mode, 1),
        "enabled": bool(timer.enabled),
        "gate": bool(timer.gate),
        "time_us": max(0, _as_int(timer.time_us, 0)),
        "reload": None if timer.reload is None else _as_int(timer.reload, 0),
    }


def _timer_from_dict(data: dict[str, Any], index: int) -> TimerConfig:
    mode = _clamp_int(data.get("mode", 1), 0, 3, 1)
    if index == 1 and mode == 3:
        mode = 2
    if index == 2:
        mode = 1
    raw_reload = data.get("reload")
    reload = None if raw_reload is None else _clamp_int(raw_reload, 0, 0xFFFF, 0)
    return TimerConfig(
        mode=mode,
        enabled=_as_bool(data.get("enabled", False), False),
        gate=_as_bool(data.get("gate", False), False),
        time_us=_clamp_int(data.get("time_us", 1000), 0, MAX_TIMER_US, 1000),
        reload=reload,
    )


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return default


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    number = _as_int(value, default)
    return min(max(number, low), high)


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    number = _as_float(value, default)
    if number < low:
        return low
    if number > high:
        return high
    return number

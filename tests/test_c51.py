"""C51 核心测试：定时器/波特率计算、寄存器位域与 C/Keil 导出。"""

from __future__ import annotations

import json

import pytest

from app.core.c51 import (
    C51Config,
    InterruptConfig,
    SerialConfig,
    TimerConfig,
    baud_reload,
    build_registers,
    build_warnings,
    generate_c,
    generate_keil_project,
    timer_reload,
)


def _cfg_with_timer(index: int, **values) -> C51Config:
    cfg = C51Config()
    cfg.timers[index] = TimerConfig(**values)
    return cfg


# ---- 定时器 ------------------------------------------------------------


def test_timer_mode1_50ms_at_12mhz() -> None:
    cfg = _cfg_with_timer(0, mode=1, enabled=True, time_us=50_000)
    result = timer_reload(cfg, 0)
    assert result.counts == 50_000
    assert (result.th, result.tl) == (0x3C, 0xB0)
    assert result.warnings == []


def test_timer_mode2_250us_at_12mhz() -> None:
    cfg = _cfg_with_timer(0, mode=2, enabled=True, time_us=250)
    result = timer_reload(cfg, 0)
    assert result.counts == 250
    assert result.th == result.tl == 0x06
    assert result.warnings == []


def test_timer_mode0_uses_13bit_split() -> None:
    cfg = _cfg_with_timer(0, mode=0, time_us=1000)
    result = timer_reload(cfg, 0)
    assert result.counts == 1000
    reload = (8192 - 1000) & 0x1FFF
    assert result.th == (reload >> 5) & 0xFF
    assert result.tl == reload & 0x1F
    assert (result.th << 5 | result.tl) == reload


def test_double_speed_halves_machine_cycle() -> None:
    normal = timer_reload(_cfg_with_timer(0, mode=1, time_us=1000), 0)
    fast_cfg = _cfg_with_timer(0, mode=1, time_us=1000)
    fast_cfg.double_speed = True
    fast = timer_reload(fast_cfg, 0)
    assert fast.counts == normal.counts * 2
    assert (fast.th, fast.tl) == (0xF8, 0x30)


def test_timer_overflow_is_clamped_with_warning() -> None:
    cfg = _cfg_with_timer(0, mode=1, time_us=100_000)
    result = timer_reload(cfg, 0)
    assert result.counts == 65536
    assert (result.th, result.tl) == (0x00, 0x00)
    assert any("超过" in message for message in result.warnings)

    mode2 = timer_reload(_cfg_with_timer(0, mode=2, time_us=1000), 0)
    assert mode2.counts == 256
    assert (mode2.th, mode2.tl) == (0x00, 0x00)
    assert mode2.warnings


def test_direct_reload_overrides_time() -> None:
    cfg = _cfg_with_timer(0, mode=2, time_us=50_000, reload=0xFE)
    result = timer_reload(cfg, 0)
    assert result.counts == 2
    assert result.th == result.tl == 0xFE
    assert result.warnings == []

    out_of_range = timer_reload(_cfg_with_timer(0, mode=2, reload=300), 0)
    assert out_of_range.th == out_of_range.tl == 0xFF
    assert any("超出" in message for message in out_of_range.warnings)


def test_timer1_mode3_falls_back_to_16bit() -> None:
    cfg = _cfg_with_timer(1, mode=3, time_us=1000)
    result = timer_reload(cfg, 1)
    assert result.counts == 1000
    assert (result.th, result.tl) == (0xFC, 0x18)
    assert any("方式 3" in message for message in result.warnings)


def test_timer2_is_always_16bit_autoreload() -> None:
    cfg = _cfg_with_timer(2, mode=2, time_us=250)
    result = timer_reload(cfg, 2)
    assert result.counts == 250
    assert (result.th, result.tl) == (0xFF, 0x06)
    assert result.warnings == []


# ---- 串口 / 波特率 -----------------------------------------------------


def test_baud_t1_9600_at_11_0592_mhz() -> None:
    cfg = C51Config(crystal_mhz=11.0592)
    cfg.serial = SerialConfig(mode=1, baud=9600, smod=False, generator="t1")
    info = baud_reload(cfg)
    assert info["reload"] == 253
    assert (info["th"], info["tl"]) == (0xFD, 0xFD)
    assert info["baud"] == 9600
    assert info["error_percent"] == 0.0
    assert info["warnings"] == []


def test_baud_smod_doubles_t1_baud() -> None:
    cfg = C51Config(crystal_mhz=11.0592)
    cfg.serial = SerialConfig(mode=1, baud=9600, smod=True, generator="t1")
    info = baud_reload(cfg)
    assert info["reload"] == 250
    assert info["baud"] == 9600
    assert info["warnings"] == []


def test_baud_115200_at_11_0592_reports_error() -> None:
    cfg = C51Config(crystal_mhz=11.0592)
    cfg.serial = SerialConfig(mode=1, baud=115200, smod=True, generator="t1")
    info = baud_reload(cfg)
    assert info["reload"] == 0xFF
    assert info["error_percent"] > 5.0
    assert any("115200" in message for message in info["warnings"])
    assert any("误差" in message for message in info["warnings"])


def test_baud_timer2_generator() -> None:
    cfg = C51Config(crystal_mhz=11.0592)
    cfg.serial = SerialConfig(mode=1, baud=9600, generator="t2")
    info = baud_reload(cfg)
    assert info["reload"] == 65500
    assert (info["rcap2h"], info["rcap2l"]) == (0xFF, 0xDC)
    assert info["baud"] == 9600
    assert info["warnings"] == []


def test_baud_mode0_and_mode2_are_fixed() -> None:
    cfg = C51Config(crystal_mhz=11.0592)
    cfg.serial = SerialConfig(mode=0, baud=921_600)
    mode0 = baud_reload(cfg)
    assert mode0["baud"] == 921_600
    assert mode0["reload"] == 0
    assert mode0["warnings"] == []

    cfg = C51Config(crystal_mhz=12.0)
    cfg.serial = SerialConfig(mode=2, baud=187_500, smod=False)
    mode2 = baud_reload(cfg)
    assert mode2["baud"] == 187_500
    assert mode2["warnings"] == []

    cfg.serial.smod = True
    doubled = baud_reload(cfg)
    assert doubled["baud"] == 375_000


# ---- 寄存器 ------------------------------------------------------------


def test_registers_for_known_config() -> None:
    cfg = C51Config(crystal_mhz=12.0)
    cfg.ports = {"P0": 0x55, "P1": 0x0F, "P2": 0xAA, "P3": 0x01}
    cfg.timers = [
        TimerConfig(mode=1, enabled=True, time_us=50_000),
        TimerConfig(mode=2, enabled=True),
        TimerConfig(),
    ]
    cfg.serial = SerialConfig(mode=1, baud=9600, smod=False, generator="t1")
    cfg.interrupts = InterruptConfig(
        ea=True, ex0=True, et0=True, es=True, high_priority={"et0"}, ext0_edge=True
    )
    registers = build_registers(cfg)
    assert registers["TMOD"] == 0x21
    assert registers["TCON"] == 0x51
    assert registers["SCON"] == 0x50
    assert registers["PCON"] == 0x00
    assert registers["IE"] == 0x93
    assert registers["IP"] == 0x02
    assert (registers["TH0"], registers["TL0"]) == (0x3C, 0xB0)
    assert (registers["TH1"], registers["TL1"]) == (0xFD, 0xFD)
    assert registers["P0"] == 0x55
    assert registers["P1"] == 0x0F
    assert registers["P2"] == 0xAA
    assert registers["P3"] == 0x01
    assert "T2CON" not in registers


def test_registers_for_timer2_and_rcap2() -> None:
    cfg = _cfg_with_timer(2, enabled=True, time_us=1000)
    registers = build_registers(cfg)
    assert registers["T2CON"] == 0x04
    assert (registers["TH2"], registers["TL2"]) == (0xFC, 0x18)
    assert (registers["RCAP2H"], registers["RCAP2L"]) == (0xFC, 0x18)

    cfg = C51Config(crystal_mhz=11.0592)
    cfg.serial = SerialConfig(mode=1, baud=9600, generator="t2")
    registers = build_registers(cfg)
    assert registers["T2CON"] == 0x34
    assert (registers["RCAP2H"], registers["RCAP2L"]) == (0xFF, 0xDC)
    assert (registers["TH2"], registers["TL2"]) == (0xFF, 0xDC)


# ---- 警告 --------------------------------------------------------------


def test_warnings_cover_conflicts_and_suggestions() -> None:
    cfg = C51Config()
    cfg.serial = SerialConfig(mode=1, baud=9600, sm2=True)
    text = "\n".join(build_warnings(cfg))
    assert "SM2" in text
    assert "方式 2" in text  # T1 作为发生器应工作在方式 2

    cfg = C51Config()
    cfg.serial = SerialConfig(mode=1, baud=9600, generator="t1")
    cfg.timers[1] = TimerConfig(mode=2, enabled=True)
    text = "\n".join(build_warnings(cfg))
    assert "波特率发生器" in text

    cfg = C51Config(power_mode="powerdown")
    cfg.interrupts.et2 = True
    cfg.crystal_mhz = 250.0
    text = "\n".join(build_warnings(cfg))
    assert "掉电" in text
    assert "ET2" in text
    assert "范围" in text

    cfg = C51Config()
    cfg.serial = SerialConfig(mode=1, baud=9600, generator="t1")
    cfg.timers[1] = TimerConfig(mode=2, enabled=True)
    cfg.interrupts.et1 = True
    text = "\n".join(build_warnings(cfg))
    assert "ET1" in text

    cfg = C51Config()
    cfg.serial = SerialConfig(mode=1, baud=9600, generator="t1")
    cfg.timers[1] = TimerConfig(mode=3)
    text = "\n".join(build_warnings(cfg))
    assert "方式 3" in text


def test_warnings_mode3_timer0_and_serial_conflict() -> None:
    cfg = C51Config()
    cfg.timers[0] = TimerConfig(mode=3, enabled=True, time_us=100)
    cfg.serial = SerialConfig(mode=1, baud=9600, generator="t1")
    cfg.timers[1] = TimerConfig(mode=2)
    text = "\n".join(build_warnings(cfg))
    assert "方式 3" in text
    assert "TR1" in text


# ---- C 代码 ------------------------------------------------------------


def test_generate_c_contains_registers_isr_and_main() -> None:
    cfg = C51Config()
    cfg.timers[0] = TimerConfig(mode=1, enabled=True, time_us=50_000)
    cfg.ports["P0"] = 0x55
    cfg.interrupts = InterruptConfig(et0=True, es=True)
    cfg.delay_us = 100
    text = generate_c(cfg)
    assert "#include <reg51.h>" in text
    assert "TMOD = 0x" in text
    assert "TH0 = 0x3C" in text and "TL0 = 0xB0" in text
    assert "P0 = 0x55;" in text
    assert "void main(void)" in text
    assert "while (1);" in text
    assert "void timer0_isr(void) interrupt 1" in text
    assert "void uart_isr(void) interrupt 4" in text
    assert "void ext0_isr" not in text
    assert "static void delay_us" in text and "delay_us(100);" in text
    assert "定时器 0" in text


def test_generate_c_selects_reg52_for_timer2() -> None:
    cfg = C51Config()
    assert "#include <reg51.h>" in generate_c(cfg)

    cfg.timers[2] = TimerConfig(enabled=True, time_us=1000)
    assert "#include <reg52.h>" in generate_c(cfg)

    cfg = C51Config(crystal_mhz=11.0592)
    cfg.serial = SerialConfig(mode=1, baud=9600, generator="t2")
    text = generate_c(cfg)
    assert "#include <reg52.h>" in text
    assert "T2CON = 0x30;" in text
    assert "RCAP2H = 0xFF;" in text and "RCAP2L = 0xDC;" in text
    assert "TR2 = 1;" in text


def test_generate_c_high_priority_uses_register_bank() -> None:
    cfg = C51Config()
    cfg.interrupts = InterruptConfig(et0=True, high_priority={"et0"})
    assert "interrupt 1 using 1" in generate_c(cfg)

    cfg.interrupts.high_priority = set()
    assert "using 1" not in generate_c(cfg)


def test_generate_c_power_modes() -> None:
    idle = C51Config(power_mode="idle")
    assert "PCON |= 0x01;" in generate_c(idle)
    powerdown = C51Config(power_mode="powerdown")
    assert "PCON |= 0x02;" in generate_c(powerdown)
    normal = generate_c(C51Config())
    assert "PCON |= 0x01;" not in normal and "PCON |= 0x02;" not in normal


# ---- Keil 工程 ---------------------------------------------------------


def test_generate_keil_project_creates_four_files(tmp_path) -> None:
    cfg = C51Config()
    cfg.timers[0] = TimerConfig(mode=1, enabled=True, time_us=50_000)
    output = tmp_path / "nested" / "keil"
    paths = generate_keil_project(cfg, output)
    assert [path.name for path in paths] == [
        "Main.c",
        "M51Project.Uv2",
        "M51Project.Opt",
        "STARTUP.A51",
    ]
    assert all(path.is_file() for path in paths)
    assert paths[0].read_text(encoding="utf-8") == generate_c(cfg)
    uv2 = paths[1].read_text(encoding="ascii")
    assert "Main.c" in uv2 and "STARTUP.A51" in uv2
    assert "PORTHELPER-SELF-WRITTEN-UV2" in uv2
    opt = paths[2].read_text(encoding="ascii")
    assert "Crystal=12.000000" in opt
    startup = paths[3].read_text(encoding="ascii")
    assert "PORTHELPER-SELF-WRITTEN-STARTUP" in startup
    assert "self-written" in startup.lower()
    assert "NOT a copy of the Keil" in startup
    assert "CSEG AT 0" in startup
    assert "LJMP STARTUP1" in startup
    assert "STARTUP1:" in startup
    assert "RETI" in startup
    assert startup.rstrip().endswith("END")


def test_startup_skips_vectors_used_by_c_isrs(tmp_path) -> None:
    cfg = C51Config()
    cfg.interrupts = InterruptConfig(et0=True, es=True)
    paths = generate_keil_project(cfg, tmp_path / "keil")
    startup = paths[3].read_text(encoding="ascii")
    assert "0x0B  Timer0: C interrupt function" in startup
    assert "0x23  Serial: C interrupt function" in startup
    assert "CSEG AT 0x03" in startup  # INT0 未使用，仍有 RETI 兜底
    assert "CSEG AT 0x0B" not in startup


# ---- 配置字典 ----------------------------------------------------------


def test_config_dict_round_trip_is_json_safe() -> None:
    cfg = C51Config(crystal_mhz=24.0, double_speed=True, power_mode="idle", delay_us=500)
    cfg.ports = {"P0": 0x55, "P1": 0x0F, "P2": 0xAA, "P3": 0xFF}
    cfg.timers = [
        TimerConfig(mode=2, enabled=True, gate=True, time_us=250, reload=None),
        TimerConfig(mode=2, enabled=False, time_us=1000, reload=0xFE),
        TimerConfig(mode=1, enabled=True, time_us=1000),
    ]
    cfg.serial = SerialConfig(
        mode=3, baud=4800, ren=False, sm2=True, tb8=True, rb8=True, smod=True,
        generator="t2",
    )
    cfg.interrupts = InterruptConfig(
        ea=True, et0=True, es=True, high_priority={"et0", "es"},
        ext0_edge=True, ext1_edge=False,
    )
    raw = cfg.to_dict()
    json.dumps(raw)  # 集合必须已经转成列表，否则这里会抛 TypeError
    assert raw["interrupts"]["high_priority"] == ["es", "et0"]
    restored = C51Config.from_dict(raw)
    assert restored == cfg


def test_config_from_bad_dict_uses_safe_defaults() -> None:
    restored = C51Config.from_dict(
        {
            "crystal_mhz": "bad",
            "ports": [],
            "timers": "bad",
            "serial": {"mode": 9, "baud": -1, "generator": "x"},
            "interrupts": {"high_priority": ["serial", "BOGUS"]},
            "power_mode": "bad",
            "delay_us": -5,
        }
    )
    assert restored.crystal_mhz == 12.0
    assert all(value == 0 for value in restored.ports.values())
    assert len(restored.timers) == 3
    assert 0 <= restored.serial.mode <= 3
    assert restored.serial.generator == "t1"
    assert restored.interrupts.high_priority == {"es"}
    assert restored.power_mode == "normal"
    assert restored.delay_us == 0


def test_timer_reload_rejects_unknown_index() -> None:
    with pytest.raises(ValueError):
        timer_reload(C51Config(), 3)

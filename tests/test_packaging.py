"""打包约定的静态断言：控制台子系统、单 exe 与 CLI 验证。

本文件只读取 ``build/app.spec`` 与 ``build/build.ps1`` 的文本，不调用 PyInstaller、
也不运行构建脚本；真正的构建与 exe 验证由流水线（以及 Task 20 的构建）负责。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = PROJECT_ROOT / "build" / "app.spec"
BUILD_SCRIPT_PATH = PROJECT_ROOT / "build" / "build.ps1"

_EXE_NAME = re.compile(r"[A-Za-z0-9_-]+\.exe")


def _read(path: Path) -> str:
    """按 UTF-8 读取文件；显式编码避免 Windows 默认代码页影响断言。"""
    return path.read_text(encoding="utf-8")


def test_spec_uses_console_with_hide_console() -> None:
    """控制台子系统 + hide-early：双击时隐藏窗口，shell 调用时保留输出与退出码。"""
    text = _read(SPEC_PATH)
    assert "console=True" in text
    assert 'hide_console="hide-early"' in text
    assert "console=False" not in text


def test_spec_builds_a_single_exe() -> None:
    """spec 只允许一个 EXE 输出，且不含 COLLECT（COLLECT 会产出目录式分发）。"""
    text = _read(SPEC_PATH)
    assert text.count("EXE(") == 1
    assert "COLLECT(" not in text
    assert text.count('name="PortHelperFluent"') == 1


def test_build_script_invokes_cli_directly_and_keeps_exit_code() -> None:
    """CLI 验证必须用调用运算符直连 exe（Start-Process 拿不到 stdout）。"""
    text = _read(BUILD_SCRIPT_PATH)
    assert "function Invoke-CliJson" in text
    assert "& $Exe @arguments" in text
    assert "$LASTEXITCODE" in text
    assert "ConvertFrom-Json" in text


def test_build_script_verifies_cli() -> None:
    """三项 CLI 冒烟：version、checksum 与 convert base 的固定期望值。"""
    text = _read(BUILD_SCRIPT_PATH)
    assert "-Command 'version'" in text
    assert "checksum --text 123456789" in text
    assert "convert base --value 255 --from-base 10 --bits 8" in text
    assert "$CliVersionText -ne '1.2.0'" in text
    assert "$CliChecksumResults.Count -lt 1" in text
    assert "$CliChecksumText -ne 'CBF43926'" in text
    assert "$CliBaseText -ne 'FF'" in text
    # 结果写进 build.log 的 Packaged verification 段，未运行时为 'not run'。
    assert "cli version:         $CliVersion" in text
    assert "cli checksum:        $CliChecksum" in text
    assert "cli convert base:    $CliBase" in text
    for variable in ("$CliVersion = 'not run'", "$CliChecksum = 'not run'", "$CliBase = 'not run'"):
        assert variable in text


def test_build_script_checks_single_exe_in_dist() -> None:
    """dist 下必须恰好一个 exe；该项在构建完成后、验证前执行。"""
    text = _read(BUILD_SCRIPT_PATH)
    assert "Checking that dist holds exactly one exe" in text
    assert "Get-ChildItem -LiteralPath $DistDir -Filter '*.exe' -File" in text
    assert "$DistExeFiles.Count -ne 1" in text


def test_packaging_targets_only_one_executable() -> None:
    """spec 与构建脚本里出现的 exe 文件名只能是 PortHelperFluent.exe。"""
    for path in (SPEC_PATH, BUILD_SCRIPT_PATH):
        names = set(_EXE_NAME.findall(_read(path)))
        assert names <= {"PortHelperFluent.exe"}, f"{path.name} references {sorted(names)}"


def test_build_script_keeps_existing_verification() -> None:
    """--selftest / --smoke-test 与哈希、归档检查保持原样。"""
    text = _read(BUILD_SCRIPT_PATH)
    assert "'--selftest'" in text
    assert "'--smoke-test'" in text
    assert "Get-FileHash -Algorithm SHA256" in text
    assert "archive_viewer" in text
    assert "temp-copy selftest:  $TempSelftest" in text
    assert "temp-copy smoke:     $SmokeResult" in text

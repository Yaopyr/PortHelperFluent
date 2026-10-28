"""Windows 自替换安装：生成 PowerShell 脚本，退出后替换 exe 并重启。

应用自身无法覆盖正在运行的 exe，因此先写一个脚本，再用 `DETACHED_PROCESS` 启动
独立进程执行：等待旧进程退出 → 覆盖目标 exe → 写 `update.log` → 重启新版本。
脚本正文只用 ASCII 英文（PowerShell 5.1 在无 BOM 时会按 ANSI 解码），并以带 BOM
的 UTF-8 落盘，保证路径里出现中文用户名时也不会乱码；路径一律单引号包裹并把
`'` 双写成 `''`。备份、脚本与日志都写在 `work_dir`（即 `data_dir/updates`），
绝不在 exe 所在目录新增文件。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from app import APP_VERSION
from app.core.logging_setup import get_logger
from app.services.paths import probe_write
from app.services.updates import UpdateError

INSTALL_SCRIPT_NAME = "apply-update.ps1"
UPDATE_LOG_NAME = "update.log"
BACKUP_PREFIX = "backup-PortHelperFluent-"

# subprocess 的这两个常量只在 Windows 上定义，非 Windows 解释器用数值兜底。
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_POWERSHELL_ARGS = (
    "powershell",
    "-NoProfile",
    "-WindowStyle",
    "Hidden",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
)

# 脚本正文：@NEW_EXE@ / @TARGET_EXE@ / @LOG_PATH@ / @TARGET_PID@ 由 write_install_script 替换。
# 注意 PowerShell 的 $PID 是只读自动变量，等待用的进程号必须换个名字。
_SCRIPT_TEMPLATE = """# PortHelper Fluent update script. Generated automatically, ASCII-only.
$ErrorActionPreference = 'Continue'
$newExe = '@NEW_EXE@'
$targetExe = '@TARGET_EXE@'
$logPath = '@LOG_PATH@'

function Write-UpdateLog([string]$message) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $logPath -Value ($stamp + ' ' + $message) -ErrorAction SilentlyContinue
}

Write-UpdateLog ('Update started for process @TARGET_PID@. New: ' + $newExe + ' Target: ' + $targetExe)
try {
    try {
        Wait-Process -Id @TARGET_PID@ -Timeout 60 -ErrorAction Stop
    }
    catch {
        Write-UpdateLog ('Wait-Process skipped: ' + $_.Exception.Message)
    }
    Start-Sleep -Milliseconds 900
    Copy-Item -LiteralPath '@NEW_EXE@' -Destination '@TARGET_EXE@' -Force -ErrorAction Stop
    Write-UpdateLog 'Copy-Item succeeded.'
    Start-Process -FilePath '@TARGET_EXE@'
    Write-UpdateLog 'Restart requested.'
    Remove-Item -LiteralPath $newExe -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
catch {
    Write-UpdateLog ('Update failed: ' + $_.Exception.Message)
    try {
        Start-Process -FilePath '@TARGET_EXE@'
        Write-UpdateLog 'Restarted the previous version.'
    }
    catch {
        Write-UpdateLog ('Restart after failure also failed: ' + $_.Exception.Message)
    }
}
"""


def can_replace(target: Path) -> bool:
    """目标 exe 所在目录是否可写；不可写时更新只能提示用户手动替换。"""
    try:
        probe_write(Path(target).parent)
    except OSError:
        return False
    return True


def write_install_script(
    new_exe: Path, target: Path, work_dir: Path, *, pid: int
) -> Path:
    """生成纯英文的 PowerShell 脚本，返回脚本路径。"""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    script_path = work_dir / INSTALL_SCRIPT_NAME
    content = (
        _SCRIPT_TEMPLATE.replace("@NEW_EXE@", _ps_escape(new_exe))
        .replace("@TARGET_EXE@", _ps_escape(target))
        .replace("@LOG_PATH@", _ps_escape(work_dir / UPDATE_LOG_NAME))
        .replace("@TARGET_PID@", str(int(pid)))
    )
    # 带 BOM 的 UTF-8：PowerShell 5.1 在无 BOM 时按 ANSI 解码，非 ASCII 路径会乱码。
    script_path.write_text(content, encoding="utf-8-sig", newline="\r\n")
    return script_path


def install_update(
    new_exe: Path,
    target: Path,
    work_dir: Path,
    *,
    pid: int | None = None,
    popen: Callable[..., object] = subprocess.Popen,
) -> Path:
    """备份旧 exe、写脚本并后台启动它；返回脚本路径。

    只支持 Windows（其他平台抛 `UpdateError`）。`popen` 可注入以便测试；备份
    失败只记日志，不阻断替换。
    """
    if os.name != "nt":
        raise UpdateError("自动安装仅支持 Windows。")
    new_exe = Path(new_exe)
    target = Path(target)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    backup = work_dir / f"{BACKUP_PREFIX}{APP_VERSION}.exe"
    try:
        shutil.copy2(target, backup)
    except OSError:
        get_logger().warning("备份旧版本 exe 失败：%s", backup, exc_info=True)
    script_path = write_install_script(
        new_exe, target, work_dir, pid=pid if pid is not None else os.getpid()
    )
    command = [*_POWERSHELL_ARGS, str(script_path)]
    try:
        popen(
            command,
            creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as exc:
        raise UpdateError(
            f"启动更新脚本失败：{exc}。请手动运行 {script_path} 完成替换。"
        ) from exc
    return script_path


def _ps_escape(path: Path) -> str:
    """转义 PowerShell 单引号字符串的内容部分；路径内的 `'` 双写成 `''`。"""
    return str(path).replace("'", "''")

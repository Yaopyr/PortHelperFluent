"""Windows 自替换安装：生成 PowerShell 脚本，退出后替换 exe 并重启。

应用自身无法覆盖正在运行的 exe，因此先写一个脚本，再用 `DETACHED_PROCESS` 启动
独立进程执行：等待旧进程退出 → 用 `[System.IO.File]::Replace` 原子替换目标 → 写
`update.log` → 重启新版本；`File.Replace` 要求源与目标同卷，跨卷等失败时退回
`Copy-Item -Force`（同样不在目标目录产生旁路文件）。替换失败时用备份还原目标并
重启旧版。

onefile 打包的程序有 bootloader 父进程 + Python 子进程，`os.getpid()` 只是子进程
的 pid：子进程先退出时父进程仍映射着 exe（还要清理 `_MEI`），此时替换必然失败。
因此脚本在快速路径 `Wait-Process -Id` 之后，还会按**进程名**等待所有同名进程退出，
再以「可独占打开目标 exe」轮询确认解锁（两者各最多 60 s）才开始替换。

脚本正文只用 ASCII 英文（PowerShell 5.1 在无 BOM 时会按 ANSI 解码），并以带 BOM
的 UTF-8 落盘，保证路径里出现中文用户名时也不会乱码；路径一律单引号包裹并把
`'` 双写成 `''`。备份、脚本与日志都写在 `work_dir`（即 `data_dir/updates`），
exe 所在目录（例如桌面）不会留下文件（写权限探针创建后立即删除）：替换直接落到
目标 exe 上。
"""

from __future__ import annotations

import os
import shutil
import stat
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
UPDATE_LOG_MAX_BYTES = 1024 * 1024

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

# 脚本正文：@NEW_EXE@ / @TARGET_EXE@ / @PROCESS_NAME@ / @BACKUP_EXE@ / @LOG_PATH@ /
# @TARGET_PID@ 由 write_install_script 替换。注意两点：
# 1) PowerShell 的 $PID 是只读自动变量，等待用的进程号直接内联，不定义同名变量；
# 2) File.Replace 的第三个参数必须传 [NullString]::Value——PowerShell 会把 $null
#    转成空字符串传给 .NET string 参数，File.Replace 会因此抛 "The path is empty"。
_SCRIPT_TEMPLATE = """# PortHelper Fluent update script. Generated automatically, ASCII-only.
$ErrorActionPreference = 'Continue'
$newExe = '@NEW_EXE@'
$targetExe = '@TARGET_EXE@'
$processName = '@PROCESS_NAME@'
$backupExe = '@BACKUP_EXE@'
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
    # Onefile builds run a bootloader parent plus a Python child: the reported pid may
    # exit first while the parent still maps the exe. Wait for every process with the
    # target name, then poll until the exe can be opened exclusively.
    try {
        Get-Process -Name $processName -ErrorAction Stop | Wait-Process -Timeout 60 -ErrorAction SilentlyContinue
        Write-UpdateLog ('Waited for running processes named ' + $processName)
    }
    catch {
        # No process with that name is running.
        Write-UpdateLog ('No running process named ' + $processName)
    }
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        try {
            $stream = [System.IO.File]::Open('@TARGET_EXE@', 'Open', 'ReadWrite', 'None')
            $stream.Close()
            Write-UpdateLog 'Target exe is no longer locked.'
            break
        }
        catch {
            Start-Sleep -Milliseconds 300
        }
    }
    Start-Sleep -Milliseconds 400
    try {
        # Third argument is the backup path: PowerShell converts $null to an empty
        # string for .NET string parameters, so [NullString]::Value is required.
        [System.IO.File]::Replace('@NEW_EXE@', '@TARGET_EXE@', [NullString]::Value)
        $replaced = $true
        Write-UpdateLog 'File.Replace replaced the target exe.'
    }
    catch {
        $replaced = $false
        Write-UpdateLog ('File.Replace failed, falling back to Copy-Item: ' + $_.Exception.Message)
    }
    if (-not $replaced) {
        Copy-Item -LiteralPath '@NEW_EXE@' -Destination '@TARGET_EXE@' -Force -ErrorAction Stop
        Remove-Item -LiteralPath '@NEW_EXE@' -Force -ErrorAction SilentlyContinue
    }
    Start-Process -FilePath '@TARGET_EXE@'
    Write-UpdateLog 'Restart requested.'
    Remove-Item -LiteralPath $newExe -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
catch {
    Write-UpdateLog ('Update failed: ' + $_.Exception.Message)
    if (Test-Path -LiteralPath $backupExe) {
        try {
            Copy-Item -LiteralPath $backupExe -Destination '@TARGET_EXE@' -Force -ErrorAction Stop
            Write-UpdateLog 'Restored the backup exe.'
        }
        catch {
            Write-UpdateLog ('Restore from backup failed: ' + $_.Exception.Message)
        }
    }
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
    """目标 exe 是否可以就地替换。

    目标已存在时先看只读属性，再尝试以 `r+b` 打开它（只打开、不写任何字节），
    这样既验证写权限，也不会在 exe 目录留下探针文件。正在运行的 exe 会被系统
    以共享冲突拒绝写句柄——这属于预期状态（替换由退出后的脚本完成），此时退回
    目录探针判断；目标不存在（首次安装或路径异常）同样用目录探针。
    """
    target = Path(target)
    try:
        if target.exists():
            if not target.stat().st_mode & stat.S_IWRITE:
                return False
            try:
                with target.open("r+b"):
                    pass
            except PermissionError:
                # 运行中的映像文件无法获得写句柄，能否替换取决于目标目录是否可写。
                probe_write(target.parent)
            return True
        probe_write(target.parent)
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
        .replace("@PROCESS_NAME@", _ps_escape_text(target.stem))
        .replace("@BACKUP_EXE@", _ps_escape(_backup_path(work_dir)))
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
    失败只记日志，不阻断替换（脚本失败时会再尝试用备份回滚）。
    """
    if os.name != "nt":
        raise UpdateError("自动安装仅支持 Windows。")
    new_exe = Path(new_exe)
    target = Path(target)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    _rotate_update_log(work_dir)
    try:
        shutil.copy2(target, _backup_path(work_dir))
    except OSError:
        get_logger().warning(
            "备份旧版本 exe 失败：%s", _backup_path(work_dir), exc_info=True
        )
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


def _backup_path(work_dir: Path) -> Path:
    """旧版本备份文件路径（脚本回滚与手工恢复都用它）。"""
    return Path(work_dir) / f"{BACKUP_PREFIX}{APP_VERSION}.exe"


def _rotate_update_log(work_dir: Path) -> None:
    """启动脚本前把超过 1 MB 的 update.log 轮转为 update.log.old。

    `prune_updates` 会清理过期的 `update.log.old`，因此日志不会无限增长也不会堆积。
    轮转失败只记日志，不阻断更新。
    """
    log_path = Path(work_dir) / UPDATE_LOG_NAME
    try:
        if log_path.is_file() and log_path.stat().st_size > UPDATE_LOG_MAX_BYTES:
            os.replace(log_path, log_path.with_name(f"{UPDATE_LOG_NAME}.old"))
    except OSError:
        get_logger().warning("轮转更新日志失败：%s", log_path, exc_info=True)


def _ps_escape(path: Path) -> str:
    """转义路径中的 PowerShell 单引号内容；路径内的 `'` 双写成 `''`。"""
    return _ps_escape_text(str(path))


def _ps_escape_text(text: str) -> str:
    """转义任意文本里的 PowerShell 单引号内容（用于进程名等非路径值）。"""
    return str(text).replace("'", "''")

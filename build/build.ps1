# PortHelper Fluent - one-file packaging and delivery verification.
#
# Usage (from any directory):
#   powershell -ExecutionPolicy Bypass -File build/build.ps1
#
# The script runs the full test suite, regenerates the icon, builds the single
# file exe, writes SHA256.txt/build.log and verifies the exe from a temporary
# directory with an isolated APPDATA. Any failure stops the build.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RepoRoot

$DistDir = Join-Path $RepoRoot 'dist'
$WorkDir = Join-Path $RepoRoot 'build_work'
$SpecPath = Join-Path $RepoRoot 'build\app.spec'
$IconPath = Join-Path $RepoRoot 'build\icon.ico'
$VersionPath = Join-Path $RepoRoot 'build\version_info.txt'
$ExePath = Join-Path $DistDir 'PortHelperFluent.exe'
$ShaPath = Join-Path $DistDir 'SHA256.txt'
$BuildLog = Join-Path $DistDir 'build.log'

$Python = (Get-Command python -ErrorAction Stop).Source
$StartedAt = Get-Date

function Invoke-NativeLogged {
    param(
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    # Native tools write progress to stderr; with $ErrorActionPreference=Stop
    # PowerShell would treat those lines as terminating errors.  Redirect all
    # streams to the log while temporarily relaxing the preference, then keep
    # the real process exit code.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Action *> $LogPath
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    return $code
}

function Invoke-PackagedExe {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $proc = Start-Process -FilePath $Exe -ArgumentList $Arguments -PassThru
    if (-not $proc.WaitForExit(180000)) {
        try { & taskkill /PID $proc.Id /T /F | Out-Null } catch { }
        throw "$Label timed out after 180 seconds"
    }
    if ($proc.ExitCode -ne 0) {
        throw "$Label failed with exit code $($proc.ExitCode)"
    }
}

function Assert-SelftestReport {
    param(
        [Parameter(Mandatory = $true)][string]$ReportPath,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $ReportPath)) {
        throw "$Label did not write a report to $ReportPath"
    }
    $payload = Get-Content -Raw -LiteralPath $ReportPath | ConvertFrom-Json
    if ($payload.ok -ne $true) {
        $details = ($payload.errors -join '; ')
        throw "$Label reported ok=false: $details"
    }
    foreach ($moduleName in @('PyQt6', 'qfluentwidgets', 'serial', 'psutil', 'PIL', 'numpy', 'hid', 'qrcode')) {
        if ($payload.resources.modules.$moduleName -ne $true) {
            throw "$Label reports module not available: $moduleName"
        }
    }
    # A literal dotted key would be parsed as a nested property path; reading it
    # through the loop variable keeps it a single property name.
    foreach ($moduleName in @('bleak', 'winrt.windows.devices.bluetooth.rfcomm', 'winrt.windows.networking.sockets')) {
        if ($payload.resources.modules.$moduleName -ne $true) {
            throw "$Label reports module not available: $moduleName"
        }
    }
    if ($payload.resources.qfluentwidgets -ne 'ok') {
        throw "$Label reports qfluentwidgets resources unavailable"
    }
    if ([string]::IsNullOrWhiteSpace([string]$payload.resources.icon)) {
        throw "$Label reports no bundled application icon"
    }
    return $payload
}

Write-Host '==> Cleaning dist and build_work'
foreach ($dir in @($DistDir, $WorkDir)) {
    if (Test-Path -LiteralPath $dir) {
        Remove-Item -LiteralPath $dir -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path $DistDir, $WorkDir | Out-Null

$PytestLog = Join-Path $WorkDir 'pytest.log'
$Env:QT_QPA_PLATFORM = 'offscreen'
$Env:PYTHONDONTWRITEBYTECODE = '1'

Write-Host '==> Running full test suite'
$TestExit = Invoke-NativeLogged -LogPath $PytestLog -Action { & $Python -m pytest }
Get-Content -LiteralPath $PytestLog | Select-Object -Last 25 | ForEach-Object { Write-Host $_ }
if ($TestExit -ne 0) {
    throw "pytest failed with exit code $TestExit (see $PytestLog)"
}
$PytestText = Get-Content -Raw -LiteralPath $PytestLog
$TestMatch = [regex]::Match($PytestText, '(\d+) passed')
if (-not $TestMatch.Success) {
    throw "Could not find the pytest summary in $PytestLog"
}
$TestCount = [int]$TestMatch.Groups[1].Value
$TestSummary = $TestMatch.Value
$TestDuration = [regex]::Match($PytestText, 'in ([0-9.]+s)')
if ($TestDuration.Success) { $TestSummary = "$TestSummary in $($TestDuration.Groups[1].Value)" }

Write-Host '==> Regenerating application icon'
$IconLog = Join-Path $WorkDir 'make_icon.log'
$IconExit = Invoke-NativeLogged -LogPath $IconLog -Action { & $Python build\make_icon.py }
Get-Content -LiteralPath $IconLog | Select-Object -Last 3 | ForEach-Object { Write-Host $_ }
if ($IconExit -ne 0) { throw "make_icon.py failed with exit code $IconExit" }

Write-Host '==> Building single-file exe'
$PyInstallerLog = Join-Path $WorkDir 'pyinstaller.log'
$BuildExit = Invoke-NativeLogged -LogPath $PyInstallerLog -Action {
    & $Python -m PyInstaller --noconfirm --clean --distpath $DistDir --workpath $WorkDir $SpecPath
}
if ($BuildExit -ne 0) {
    Get-Content -LiteralPath $PyInstallerLog | Select-Object -Last 60 | ForEach-Object { Write-Host $_ }
    throw "PyInstaller failed with exit code $BuildExit (see $PyInstallerLog)"
}
if (-not (Test-Path -LiteralPath $ExePath)) {
    throw "PyInstaller did not produce $ExePath"
}

Write-Host '==> Hashing artifact'
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ExePath).Hash.ToLowerInvariant()
$SizeBytes = (Get-Item -LiteralPath $ExePath).Length
$SizeMb = [math]::Round($SizeBytes / 1MB, 2)
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($ShaPath, "$Hash  PortHelperFluent.exe`r`n", $Utf8NoBom)

Write-Host '==> Checking archive for excluded content'
$ArchiveList = Join-Path $WorkDir 'archive-list.txt'
$ArchiveExit = Invoke-NativeLogged -LogPath $ArchiveList -Action {
    & $Python -m PyInstaller.utils.cliutils.archive_viewer -l -r -b $ExePath
}
$ArchiveText = if (Test-Path -LiteralPath $ArchiveList) { Get-Content -Raw -LiteralPath $ArchiveList } else { '' }
$ArchiveCheck = 'archive_viewer unavailable; inspect build_work/app/warn-app.txt and build/app.spec'
if ($ArchiveExit -eq 0 -and -not [string]::IsNullOrEmpty($ArchiveText)) {
    $ForbiddenPatterns = @(
        '(?i)(^|[/\\.\s])(analysis|docs|tests|\.git)([/\\]|\.|$)',
        '(?i)PyQt5',
        '(?i)WebEngine'
    )
    $Found = @()
    foreach ($pattern in $ForbiddenPatterns) {
        if ($ArchiveText -match $pattern) { $Found += $pattern }
    }
    if ($Found.Count -gt 0) {
        throw "Archive contains excluded entries matching: $($Found -join ', ') (see $ArchiveList)"
    }
    $ArchiveCheck = 'ok: no analysis/, docs/, tests/, .git/, PyQt5 or QtWebEngine entries'
}
Write-Host "    $ArchiveCheck"

Write-Host '==> Verifying packaged exe from an isolated temporary directory'
$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("porthelper-verify-" + [Guid]::NewGuid().ToString('N'))
$IsolatedAppData = Join-Path $TempRoot 'appdata'
$DeliveryDir = Join-Path $TempRoot 'delivery'
New-Item -ItemType Directory -Force -Path $IsolatedAppData, $DeliveryDir | Out-Null
Copy-Item -LiteralPath $ExePath -Destination $DeliveryDir
$DeliveryExe = Join-Path $DeliveryDir 'PortHelperFluent.exe'

$OldAppData = $env:APPDATA
$OldPythonUserBase = $env:PYTHONUSERBASE
$OldNoDialogs = $env:PORTHELPER_NO_DIALOGS
$UserBase = (& $Python -c "import site; print(site.getuserbase())").Trim()

$DistSelftest = 'not run'
$DistSmoke = 'not run'
$TempSelftest = 'not run'
$SmokeResult = 'not run'
try {
    $Env:APPDATA = $IsolatedAppData
    if (-not [string]::IsNullOrWhiteSpace($UserBase)) { $Env:PYTHONUSERBASE = $UserBase }
    $Env:PORTHELPER_NO_DIALOGS = '1'

    # The packaged app must use the real Windows platform plugin; pytest above
    # was offscreen, but the exe verification must not inherit that setting.
    Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue

    $DistReport = Join-Path $TempRoot 'selftest-dist.json'
    Invoke-PackagedExe -Exe $ExePath -Arguments @('--selftest', '--report', ('"{0}"' -f $DistReport)) -Label 'dist selftest'
    $DistPayload = Assert-SelftestReport -ReportPath $DistReport -Label 'dist selftest'
    $DistSelftest = "exit 0, ok=true, icon=$($DistPayload.resources.icon)"

    Invoke-PackagedExe -Exe $ExePath -Arguments @('--smoke-test') -Label 'dist smoke test'
    $DistSmoke = 'exit 0'

    $TempReport = Join-Path $TempRoot 'selftest.json'
    Invoke-PackagedExe -Exe $DeliveryExe -Arguments @('--selftest', '--report', ('"{0}"' -f $TempReport)) -Label 'temp-copy selftest'
    $TempPayload = Assert-SelftestReport -ReportPath $TempReport -Label 'temp-copy selftest'
    $TempSelftest = "exit 0, ok=true, icon=$($TempPayload.resources.icon)"

    Invoke-PackagedExe -Exe $DeliveryExe -Arguments @('--smoke-test') -Label 'temp-copy smoke test'
    $SmokeResult = 'exit 0'
}
finally {
    $env:APPDATA = $OldAppData
    if ($null -eq $OldPythonUserBase) {
        Remove-Item Env:PYTHONUSERBASE -ErrorAction SilentlyContinue
    } else {
        $Env:PYTHONUSERBASE = $OldPythonUserBase
    }
    if ($null -eq $OldNoDialogs) {
        Remove-Item Env:PORTHELPER_NO_DIALOGS -ErrorAction SilentlyContinue
    } else {
        $Env:PORTHELPER_NO_DIALOGS = $OldNoDialogs
    }
    Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '==> Writing build.log'
$VersionsJson = & $Python -c "import json,platform,sys,importlib.metadata as m; print(json.dumps({'python':sys.version.split()[0],'platform':platform.platform(),'PyQt6':m.version('PyQt6'),'PyQt6-Fluent-Widgets':m.version('PyQt6-Fluent-Widgets'),'pyserial':m.version('pyserial'),'psutil':m.version('psutil'),'Pillow':m.version('Pillow'),'numpy':m.version('numpy'),'hidapi':m.version('hidapi'),'qrcode':m.version('qrcode'),'pytest':m.version('pytest'),'PyInstaller':m.version('pyinstaller')}))"
$Versions = $VersionsJson | ConvertFrom-Json
$Head = (& git rev-parse HEAD).Trim()
$Branch = (& git branch --show-current).Trim()
$Elapsed = [math]::Round(((Get-Date) - $StartedAt).TotalSeconds, 1)
$WarnFile = Get-ChildItem -Path $WorkDir -Recurse -Filter 'warn-*.txt' -ErrorAction SilentlyContinue | Select-Object -First 1
$WarnPath = if ($null -ne $WarnFile) { $WarnFile.FullName } else { '(not found)' }

$LogLines = @(
    'PortHelper Fluent build log',
    '============================',
    "Timestamp:      $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')",
    "Repository:     $RepoRoot",
    "Branch:         $Branch",
    "Commit:         $Head",
    "Build duration: $Elapsed s",
    '',
    'Versions',
    '--------',
    "Python:              $($Versions.python)",
    "Platform:            $($Versions.platform)",
    "PyQt6:               $($Versions.'PyQt6')",
    "PyQt6-Fluent-Widgets: $($Versions.'PyQt6-Fluent-Widgets')",
    "pyserial:            $($Versions.pyserial)",
    "psutil:              $($Versions.psutil)",
    "Pillow:              $($Versions.Pillow)",
    "numpy:               $($Versions.numpy)",
    "hidapi:              $($Versions.hidapi)",
    "qrcode:              $($Versions.qrcode)",
    "pytest:              $($Versions.pytest)",
    "PyInstaller:         $($Versions.PyInstaller)",
    '',
    'Tests',
    '-----',
    "Command: python -m pytest (QT_QPA_PLATFORM=offscreen, PYTHONDONTWRITEBYTECODE=1)",
    "Result:  $TestSummary",
    "Count:   $TestCount passed",
    '',
    'Artifact',
    '--------',
    "Path:    $ExePath",
    ("Size:    {0} bytes ({1} MB)" -f $SizeBytes, $SizeMb),
    "SHA-256: $Hash",
    "SHA256.txt: $ShaPath",
    '',
    'Packaged verification (isolated APPDATA, PORTHELPER_NO_DIALOGS=1)',
    '---------------------------------------------------------------',
    "dist selftest:       $DistSelftest",
    "dist smoke:          $DistSmoke",
    "temp-copy selftest:  $TempSelftest",
    "temp-copy smoke:     $SmokeResult",
    "Archive check:       $ArchiveCheck",
    '',
    'Warnings:',
    "  PyInstaller warnings: $WarnPath",
    "  Archive listing:      $ArchiveList"
)
[System.IO.File]::WriteAllLines($BuildLog, [string[]]$LogLines, $Utf8NoBom)

Write-Host ''
Write-Host 'Build and verification succeeded.'
Write-Host ("EXE:     {0}" -f $ExePath)
Write-Host ("Size:    {0} bytes ({1} MB)" -f $SizeBytes, $SizeMb)
Write-Host ("SHA-256: {0}" -f $Hash)
Write-Host ("Tests:   {0}" -f $TestSummary)

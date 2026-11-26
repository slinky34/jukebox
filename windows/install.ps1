# Installs Jukebox so it starts with Windows and restarts if it stops.
# Run it with install-windows.bat in the jukebox folder (it asks for admin rights).
# Safe to run again: it updates the existing install and restarts Jukebox.
param([string]$User)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$TaskName = 'Jukebox'

function Done($code) {
    Write-Host ''
    Read-Host 'Press Enter to close'
    exit $code
}
function Fail($msg) {
    Write-Host ''
    Write-Host "Problem: $msg" -ForegroundColor Red
    Done 1
}

# ---- admin rights ------------------------------------------------------------
$me = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($me)).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    # Remember who asked, so Jukebox runs as you and not as the admin account.
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-User', "`"$($me.Name)`"")
    exit
}
if (-not $User) { $User = $me.Name }

Write-Host 'Installing Jukebox' -ForegroundColor Cyan
Write-Host "  Folder:  $Root"
Write-Host "  Runs as: $User"
Write-Host ''

# ---- config ------------------------------------------------------------------
$config = Join-Path $Root 'config.toml'
if (-not (Test-Path $config) -and (Test-Path "$config.txt")) {
    # Notepad saved it as config.toml.txt (Windows hides the .txt).
    Rename-Item "$config.txt" 'config.toml'
    Write-Host 'Renamed config.toml.txt to config.toml.'
}
if (-not (Test-Path $config)) {
    Fail 'config.toml is missing. Copy config.example.toml to config.toml and put your Plex token in it.'
}
$configText = Get-Content $config -Raw
if ($configText -notmatch '(?m)^\s*plex_token\s*=\s*"[^"]+"') {
    Fail 'plex_token in config.toml is empty. See "Finding your Plex token" in README.md.'
}
$port = 8765
if ($configText -match '(?m)^\s*port\s*=\s*(\d+)') { $port = [int]$Matches[1] }

# ---- Python and packages -----------------------------------------------------
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host 'Setting up Python packages (about a minute)...'
    $base = $null
    foreach ($cand in @(@('py', '-3'), @('python'))) {
        try {
            $exe = $cand[0]; $rest = @($cand | Select-Object -Skip 1)
            # No quotes inside the code: Windows PowerShell 5.1 strips them from native-command arguments.
            $v = & $exe @rest -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>$null
            if ($LASTEXITCODE -eq 0 -and "$v".Trim() -match '^\d+$' -and [int]"$v".Trim() -ge 309) { $base = $cand; break }
        } catch { }
    }
    if (-not $base) {
        Fail 'Python 3.9 or newer was not found. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this again.'
    }
    $exe = $base[0]; $rest = @($base | Select-Object -Skip 1)
    & $exe @rest -m venv (Join-Path $Root '.venv')
    if ($LASTEXITCODE -ne 0) { Fail 'Could not create the Python environment.' }
    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet -r (Join-Path $Root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Fail 'Installing Python packages failed (see above).' }
}

# ---- check Plex --------------------------------------------------------------
Write-Host 'Checking the connection to Plex...'
$env:PYTHONUTF8 = '1'
Push-Location $Root
& $venvPy run.py --check
$ok = $LASTEXITCODE -eq 0
Pop-Location
if (-not $ok) { Fail 'Could not connect to Plex with the settings in config.toml (see the message above).' }

# ---- scheduled task ----------------------------------------------------------
Write-Host ''
Write-Host 'Setting Jukebox to start with Windows...'
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
# Stop any other copy still running (e.g. an old start.bat window). Two copies at once
# fight over everyone's playlists.
$old = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique)
foreach ($procId in $old) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
    if (-not $p) { continue }
    if ($p.Name -notlike 'python*') {
        Fail "Port $port is used by another program ($($p.Name)). Change port in config.toml and run this again."
    }
    Write-Host "Stopping an old copy of Jukebox (process $procId)..."
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    # A venv's python.exe is a small launcher that starts the real Python; stop it too.
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($p.ParentProcessId)" -ErrorAction SilentlyContinue
    if ($parent -and $parent.Name -like 'python*') {
        Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
if ($old.Count) { Start-Sleep -Seconds 2 }
$logFile = Join-Path $Root 'jukebox.log'
$action = New-ScheduledTaskAction -Execute $venvPy `
    -Argument "run.py --log-file `"$logFile`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
# S4U: runs as you whether or not you're signed in, without storing your password.
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Jukebox: shared Plex playlists' -Force | Out-Null

# ---- firewall ----------------------------------------------------------------
Get-NetFirewallRule -DisplayName 'Jukebox' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'Jukebox' -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPort $port -Profile Private, Domain | Out-Null

# ---- start and confirm -------------------------------------------------------
Start-ScheduledTask -TaskName $TaskName
Write-Host 'Starting...'
$up = $false
for ($i = 0; $i -lt 20 -and -not $up; $i++) {
    Start-Sleep -Seconds 1
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$port/api/config" | Out-Null
        $up = $true
    } catch { }
}
if (-not $up) { Fail "Jukebox didn't start. Look at $logFile for the reason." }

$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -ExpandProperty IPAddress
Write-Host ''
Write-Host 'Jukebox is running and will start automatically with Windows.' -ForegroundColor Green
Write-Host 'Open it from any device on your network:'
foreach ($ip in $ips) { Write-Host "  http://${ip}:$port" }
Write-Host ''
Write-Host "Log file: $logFile"
$profiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object NetworkCategory -eq 'Public'
if ($profiles) {
    Write-Host ''
    Write-Host 'Note: this PC sees your network as "Public", so other devices may be blocked.' -ForegroundColor Yellow
    Write-Host 'Fix: Settings > Network & internet > your connection > set Network profile to Private.'
}
Done 0

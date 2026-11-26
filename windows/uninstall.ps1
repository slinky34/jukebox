# Stops Jukebox and removes its startup task and firewall rule.
# Leaves the folder alone, including data\jukebox.db (your shared playlists).
# The playlists already copied into everyone's Plex stay there; delete them in Plex if you want.

$ErrorActionPreference = 'Stop'
$me = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($me)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    exit
}

if (Get-ScheduledTask -TaskName 'Jukebox' -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName 'Jukebox' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'Jukebox' -Confirm:$false
    Write-Host 'Removed the startup task.'
} else {
    Write-Host 'No startup task found.'
}
Get-NetFirewallRule -DisplayName 'Jukebox' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host 'Removed the firewall rule.'
Write-Host ''
Read-Host 'Done. Press Enter to close'

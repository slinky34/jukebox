@echo off
rem Stops Jukebox and removes it from Windows startup. Your playlists (data\jukebox.db) are kept.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\uninstall.ps1"

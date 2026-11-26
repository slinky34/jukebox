@echo off
rem Sets Jukebox to start with Windows. Double-click it; Windows will ask for admin rights.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install.ps1"

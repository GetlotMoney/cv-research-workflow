@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start-workflow.ps1"
if errorlevel 1 pause

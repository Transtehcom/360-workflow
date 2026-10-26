@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Запуск приложения через PowerShell; окно остаётся открытым при ошибке.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
pause

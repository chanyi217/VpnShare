@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title 还原系统代理

echo ================================
echo   还原 Windows 系统代理设置
echo ================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-VpnShare.ps1" -Restore

echo.
pause

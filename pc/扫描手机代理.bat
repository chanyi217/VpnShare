@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title 扫描手机代理

echo ================================
echo   扫描局域网内的代理服务
echo   （只探测，不修改系统设置）
echo ================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-VpnShare.ps1" -ScanOnly

echo.
pause

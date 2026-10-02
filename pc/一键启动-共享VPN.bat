@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

:: ============================================================
::  VPN Share - 一键启动（双击这个文件）
::  自动请求管理员权限 -> 探测手机代理 -> 设置系统代理
::  关闭这个窗口 = 自动还原
:: ============================================================

set "PS1=%~dp0Start-VpnShare.ps1"

:: 检查脚本是否存在
if not exist "%PS1%" (
    echo [FAIL] 找不到 Start-VpnShare.ps1，请确认两个文件在同一目录。
    pause
    exit /b 1
)

:: 以管理员身份重新启动自身（改代理需要）
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在请求管理员权限...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" >nul 2>&1
    if %errorlevel% neq 0 (
        echo.
        echo [警告] 你拒绝了管理员授权。
        echo        仍可继续，但部分系统级应用可能无法读到代理设置。
        echo.
        pause
    ) else (
        exit /b 0
    )
)

title VPN Share - 手机代理共享给电脑
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
    echo 代理共享已结束，系统代理已还原。
) else (
    echo 脚本退出码: %RC%
)
echo.
pause
endlocal

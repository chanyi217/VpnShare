@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title VpnShare - USB 直连模式

:: ================================================================
::  通过 USB 数据线，把手机代理映射到电脑本地端口
::  适用场景：手机和电脑不在同一网段（比如手机连了别的 WiFi）
::            或者热点不支持设备互访
::  原理：adb forward 把手机的 8080/1080 端口转发到电脑的 18080/11080
:: ================================================================

set "ADB=%LOCALAPPDATA%\..\Android\Sdk\platform-tools\adb.exe"
if not exist "%ADB%" set "ADB=C:\Users\%USERNAME%\Android\Sdk\platform-tools\adb.exe"
if not exist "%ADB%" (
    echo [FAIL] 找不到 adb.exe
    echo        请确认 Android SDK 已安装，或修改本脚本里的 ADB 路径
    pause & exit /b 1
)

set "LOCAL_HTTP=18080"
set "LOCAL_SOCKS=11080"

echo ================================================
echo   VpnShare - USB 直连模式
echo ================================================
echo.

:: 1. 检查设备
echo [ .. ] 检查 USB 设备 ...
%ADB% start-server >nul 2>&1
for /f "tokens=1,2" %%a in ('%ADB% devices ^| findstr /r "device$"') do set "DEV=%%a"
if "%DEV%"=="" (
    echo [FAIL] 没有检测到 USB 设备
    echo        1^) 手机打开「开发者选项」-「USB 调试」
    echo        2^) 插上数据线，手机上点「允许 USB 调试」
    pause & exit /b 1
)
echo [ OK ] 设备: %DEV%

:: 2. 检查手机端代理是否在运行
echo [ .. ] 检查手机代理端口 ...
%ADB% shell "netstat -tln 2>/dev/null | grep -q ':8080'" 2>nul
if errorlevel 1 (
    echo [ !! ] 手机的 8080 端口没有在监听
    echo        请先在手机上打开 VpnShare，点「启动共享」
    pause & exit /b 1
)
echo [ OK ] 手机代理正在运行

:: 3. 建立端口映射
echo [ .. ] 建立端口映射 ...
%ADB% forward --remove-all >nul 2>&1
%ADB% forward tcp:%LOCAL_HTTP% tcp:8080
%ADB% forward tcp:%LOCAL_SOCKS% tcp:1080
echo [ OK ] 映射已建立

:: 4. 验证
echo [ .. ] 验证代理链路 ...
curl -s --noproxy "*" -x http://127.0.0.1:%LOCAL_HTTP% -o nul ^
     -w "HTTP 代理 -> 状态 %%{http_code}\n" --max-time 10 http://www.baidu.com
if errorlevel 1 (
    echo [ !! ] 验证失败，但映射已建立，可继续尝试
) else (
    echo [ OK ] 代理链路正常
)

echo.
echo ------------------------------------------------
echo  电脑本地代理地址（USB 直连）
echo    HTTP   : 127.0.0.1:%LOCAL_HTTP%
echo    SOCKS5 : 127.0.0.1:%LOCAL_SOCKS%
echo ------------------------------------------------
echo.
echo  接下来可以：
echo    1^) 用 一键启动-共享VPN.bat 自动设置系统代理
echo       但要先用扫描找到 127.0.0.1:%LOCAL_HTTP%
echo    2^) 或者手动在 Windows 代理设置里填 127.0.0.1:%LOCAL_HTTP%
echo.
echo  按任意键断开 USB 映射并退出 ...
pause >nul

echo.
echo [ .. ] 清理端口映射 ...
%ADB% forward --remove-all >nul 2>&1
echo [ OK ] 已断开
echo.
echo 注意：系统代理若已设置，请运行 还原代理.bat 还原。
timeout /t 3 >nul

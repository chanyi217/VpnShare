@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title VpnShare - USB 安装到手机

:: ================================================================
::  通过 USB 数据线把 APK 安装到手机
::  前提：手机已开启「开发者选项 - USB 调试」，并已授权本电脑
:: ================================================================

set "ADB=%USERPROFILE%\Android\Sdk\platform-tools\adb.exe"
if not exist "%ADB%" set "ADB=C:\Android\Sdk\platform-tools\adb.exe"

if not exist "%ADB%" (
    echo [FAIL] 找不到 adb.exe
    echo        请修改本脚本里的 ADB 路径为你实际的 Android SDK 位置
    pause & exit /b 1
)

set "APK=%~dp0..\apk\VpnShare-v1.0.apk"
if not exist "%APK%" set "APK=%~dp0VpnShare-v1.0.apk"
if not exist "%APK%" (
    echo [FAIL] 找不到 APK 文件
    echo        预期位置: ..\apk\VpnShare-v1.0.apk
    pause & exit /b 1
)

echo ================================================
echo   VpnShare - USB 安装
echo ================================================
echo.

echo [ .. ] 启动 adb 服务 ...
"%ADB%" start-server >nul 2>&1

echo [ .. ] 检查 USB 设备 ...
"%ADB%" devices
echo.

:: 判断是否有已授权设备
for /f "skip=1 tokens=1,2" %%a in ('"%ADB%" devices') do (
    if "%%b"=="device" set "DEV=%%a"
    if "%%b"=="unauthorized" set "UNAUTH=1"
)

if defined UNAUTH (
    echo [ !! ] 设备未授权
    echo        请看手机屏幕，点击「允许 USB 调试」弹窗
    pause & exit /b 1
)

if not defined DEV (
    echo [FAIL] 没有检测到已授权的设备
    echo        1^) 手机 - 设置 - 关于手机 - 连点 MIUI 版本 7 次，开启开发者模式
    echo        2^) 设置 - 更多设置 - 开发者选项 - 打开「USB 调试」
    echo        3^) 插上数据线，手机上点「允许 USB 调试」
    pause & exit /b 1
)

echo [ OK ] 设备: %DEV%
echo.
echo [ .. ] 正在安装 %APK% ...
"%ADB%" install -r "%APK%"

if errorlevel 1 (
    echo.
    echo [FAIL] 安装失败
    echo        若提示签名冲突，先卸载旧版本：
    echo          "%ADB%" uninstall com.vpnshare.app
    pause & exit /b 1
)

echo.
echo [ OK ] 安装成功
echo.

echo [ .. ] 启动 VpnShare ...
"%ADB%" shell am start -n com.vpnshare.app/.MainActivity >nul 2>&1
echo [ OK ] 已启动

echo.
echo ================================================
echo  下一步：
echo    在手机上点「启动共享」，然后：
echo      - 电脑和手机在同一 WiFi/热点 → 双击 一键启动-共享VPN.bat
echo      - 不在同一网段 → 双击 USB直连模式.bat
echo ================================================
echo.
pause

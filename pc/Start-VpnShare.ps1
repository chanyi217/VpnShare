<#
.SYNOPSIS
    VPN Share - 一键把手机上的代理/VPN 共享给电脑使用（Windows 端）

.DESCRIPTION
    原理：手机上的 Every Proxy 之类软件在手机本地（或 0.0.0.0）起了
    HTTP 代理端口。手机开着 VPN 时，手机自己是 VPN 的出口，
    它所起的 HTTP 代理服务进程发起的 outbound 连接天然走 VPN 隧道。
    电脑只要把系统代理指向「手机IP:端口」，流量就变成
        电脑 -> 手机HTTP代理 -> 手机VPN隧道 -> 目标网站
    于是电脑也"蹭"上了手机的 VPN。

    本脚本干三件事：
      1. 自动探测同网段内开放的代理端口（或使用你指定的 IP:端口）
      2. 设置 Windows 系统代理（WinINET 注册表，覆盖 Edge/Chrome/系统级应用）
      3. 退出时（或异常/断电后重跑 restore）自动还原

.EXAMPLE
    # 自动发现并启用
    .\Start-VpnShare.ps1

    # 手动指定（推荐，最快）
    .\Start-VpnShare.ps1 -ProxyHost 192.168.43.1 -ProxyPort 8080

    # 只探测不修改，看看手机代理开没开
    .\Start-VpnShare.ps1 -ScanOnly

    # 还原系统代理
    .\Start-VpnShare.ps1 -Restore
#>

[CmdletBinding()]
param(
    [string]   $ProxyHost        = "",
    [int]      $ProxyPort        = 0,
    [int[]]    $PortsToScan      = @(8080, 18080, 8888, 1080, 11080, 8118, 3128, 10809, 7890, 9999),
    [switch]   $ScanOnly,
    [switch]   $Restore,
    [switch]   $NoPac,
    [switch]   $KeepOnExit,
    [int]      $ScanTimeoutMs    = 220
)

$ErrorActionPreference = 'Stop'
$script:RegPath   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$script:BackupKey = 'HKCU:\Software\VpnShare'
$script:BackupVal = 'ProxyBackup'

# ---------- 输出小工具 ----------
function Say  ($m) { Write-Host $m }
function Ok   ($m) { Write-Host "[ OK ] $m"   -ForegroundColor Green }
function Info ($m) { Write-Host "[ .. ] $m"   -ForegroundColor Cyan }
function Warn ($m) { Write-Host "[ !! ] $m"   -ForegroundColor Yellow }
function Bad  ($m) { Write-Host "[FAIL] $m"   -ForegroundColor Red }

# ---------- 管理员检查（改注册表 HKCU 不需要，但刷新 WinINET 需要） ----------
function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ---------- 让 WinINET 立即重读代理设置 ----------
function Invoke-ProxyRefresh {
    if (-not ("WinInetRefresh" -as [type])) {
        Add-Type -Namespace WinInetRefresh -Name Api -MemberDefinition @'
[DllImport("wininet.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern bool InternetSetOption(IntPtr hInternet, int dwOption, IntPtr lpBuffer, int dwBufferLength);
'@
    }
    # 39 = INTERNET_OPTION_SETTINGS_CHANGED, 37 = INTERNET_OPTION_REFRESH
    [void][WinInetRefresh.Api]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0)
    [void][WinInetRefresh.Api]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0)
}

# ---------- 读取本机 IPv4 和网段 ----------
function Get-LocalNetworks {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
        ForEach-Object {
            $ip = $_.IPAddress
            $prefix = $_.PrefixLength
            $bytes = [System.Net.IPAddress]::Parse($ip).GetAddressBytes()
            [pscustomobject]@{
                IP     = $ip
                Prefix = $prefix
                Bytes  = $bytes
                Subnet = (($bytes[0..2] -join '.') + '.x')
            }
        }
}

# ---------- 端口探测 ----------
function Test-ProxyPort {
    param([string]$IP, [int]$Port, [int]$TimeoutMs = 220)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($IP, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch { return $false } finally { $client.Close() }
}

function Get-CandidateIPs {
    $ips = New-Object System.Collections.Generic.List[string]

    # 0) USB 直连模式：adb forward 映射到本机回环地址，优先扫这个
    $ips.Add('127.0.0.1')

    # 1) 本机自己的各网段 .1 / 常见网关（手机开热点时，手机通常是 .1 或网关）
    $nets = @(Get-LocalNetworks)
    foreach ($n in $nets) {
        $ips.Add(($n.Bytes[0..2] -join '.') + '.1')
        try {
            $gw = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
                   Sort-Object RouteMetric | Select-Object -First 1).NextHop
            if ($gw -and $gw -notlike '0.*') { $ips.Add($gw) }
        } catch {}
    }

    # 2) 本网段活跃主机（ARP 表，最快）
    try {
        Get-NetNeighbor -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.State -in @('Reachable','Stale','Permanent') -and $_.IPAddress -notlike '224.*' } |
            ForEach-Object { $ips.Add($_.IPAddress) }
    } catch {}

    $ips | Where-Object { $_ -and $_ -notlike '255.*' } | Select-Object -Unique
}

function Find-Proxy {
    param([int[]]$Ports, [int]$TimeoutMs)
    $candidates = @(Get-CandidateIPs)
    Info "扫描 $(($candidates).Count) 个候选地址 x $($Ports.Count) 个端口 ..."
    foreach ($ip in $candidates) {
        foreach ($p in $Ports) {
            if (Test-ProxyPort -IP $ip -Port $p -TimeoutMs $TimeoutMs) {
                return [pscustomobject]@{ Host = $ip; Port = $p }
            }
        }
    }
    return $null
}

# ---------- 系统代理设置 / 还原 ----------
function Get-CurrentProxy {
    $p = Get-ItemProperty -Path $script:RegPath -ErrorAction SilentlyContinue
    [pscustomobject]@{
        Enable   = $p.ProxyEnable
        Server   = $p.ProxyServer
        Pac      = $p.AutoConfigURL
        Override = $p.ProxyOverride
    }
}

function Backup-Proxy {
    if (Test-Path "$script:BackupKey\$script:BackupVal") {
        Warn "已存在代理备份，跳过覆盖（如需重建请先 -Restore）"
        return
    }
    $cur = Get-CurrentProxy
    New-Item -Path $script:BackupKey -Force | Out-Null
    $json = $cur | ConvertTo-Json -Compress
    Set-ItemProperty -Path $script:BackupKey -Name $script:BackupVal -Value $json -Type String
    Ok "已备份原始代理设置"
}

function Set-SystemProxy {
    param([string]$Host_, [int]$Port, [switch]$SkipPac)
    Backup-Proxy

    $bypass = '<local>;localhost;127.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.2*;172.30.*;172.31.*;192.168.*'
    Set-ItemProperty -Path $script:RegPath -Name 'ProxyEnable'   -Value 1 -Type DWord
    Set-ItemProperty -Path $script:RegPath -Name 'ProxyServer'   -Value "$Host_`:$Port" -Type String
    Set-ItemProperty -Path $script:RegPath -Name 'ProxyOverride' -Value $bypass -Type String
    if ($SkipPac) {
        Remove-ItemProperty -Path $script:RegPath -Name 'AutoConfigURL' -ErrorAction SilentlyContinue
    } else {
        Set-ItemProperty -Path $script:RegPath -Name 'AutoConfigURL' -Value "http://$Host_`:$Port/proxy.pac" -Type String
    }
    Invoke-ProxyRefresh
}

function Restore-SystemProxy {
    if (-not (Test-Path "$script:BackupKey\$script:BackupVal")) {
        # 没备份，就清掉代理，恢复直连
        Warn "无备份记录，执行清空代理（恢复直连）"
        Set-ItemProperty -Path $script:RegPath -Name 'ProxyEnable' -Value 0 -Type DWord
        Remove-ItemProperty -Path $script:RegPath -Name 'ProxyServer'   -ErrorAction SilentlyContinue
        Remove-ItemProperty -Path $script:RegPath -Name 'AutoConfigURL' -ErrorAction SilentlyContinue
        Invoke-ProxyRefresh
        Ok "已恢复为直连"
        return
    }
    $json = Get-ItemProperty -Path $script:BackupKey -Name $script:BackupVal
    $b = $json.$script:BackupVal | ConvertFrom-Json
    Set-ItemProperty -Path $script:RegPath -Name 'ProxyEnable' -Value ([int]$b.Enable) -Type DWord
    foreach ($pair in @(@('ProxyServer',$b.Server), @('AutoConfigURL',$b.Pac), @('ProxyOverride',$b.Override))) {
        if ($pair[1]) { Set-ItemProperty -Path $script:RegPath -Name $pair[0] -Value $pair[1] -Type String }
        else { Remove-ItemProperty -Path $script:RegPath -Name $pair[0] -ErrorAction SilentlyContinue }
    }
    Invoke-ProxyRefresh
    Remove-Item -Path $script:BackupKey -Recurse -Force -ErrorAction SilentlyContinue
    Ok "已还原原始代理设置"
}

# ---------- 连通性验证 ----------
function Test-ThroughProxy {
    param([string]$Host_, [int]$Port)
    try {
        $r = Invoke-WebRequest -Uri 'http://www.gstatic.com/generate_204' `
             -Proxy "http://$Host_`:$Port" -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop
        return $true
    } catch {
        # 很多网络下 gstatic 不通属正常，退一步用百度判断"代理链路是否活着"
        try {
            $r = Invoke-WebRequest -Uri 'http://www.baidu.com' `
                 -Proxy "http://$Host_`:$Port" -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop
            return $true
        } catch { return $false }
    }
}

# =====================================================================
#  主流程
# =====================================================================
Say ""
Say "================= VPN Share (Windows) =================" -ForegroundColor Magenta
Say ""

if ($Restore) {
    Restore-SystemProxy
    exit 0
}

# --- 1. 定位代理 ---
$target = $null
if ($ProxyHost -and $ProxyPort -gt 0) {
    Info "使用手动指定的代理：$ProxyHost`:$ProxyPort"
    if (-not (Test-ProxyPort -IP $ProxyHost -Port $ProxyPort -TimeoutMs 800)) {
        Bad "无法连接 $ProxyHost`:$ProxyPort —— 请确认手机上代理已启动、且在同一热点下"
        exit 1
    }
    $target = [pscustomobject]@{ Host = $ProxyHost; Port = $ProxyPort }
    Ok "代理可达"
} else {
    Info "未指定代理，开始自动发现 ..."
    $target = Find-Proxy -Ports $PortsToScan -TimeoutMs $ScanTimeoutMs
    if (-not $target) {
        Bad "没有发现可用的代理端口"
        Say ""
        Say "请检查：" -ForegroundColor Yellow
        Say "  1) 手机上代理软件已启动 HTTP Proxy"
        Say "  2) 手机和电脑连的是同一个热点/WiFi"
        Say "  3) 手机代理监听在 0.0.0.0（允许其他设备接入），不是仅 127.0.0.1"
        Say "  4) 手机系统设置里代理软件的『允许局域网设备连接』已打开"
        Say ""
        Say "也可以用 -ProxyHost 手机IP -ProxyPort 端口 手动指定"
        exit 1
    }
    Ok "发现代理：$($target.Host):$($target.Port)"
}

if ($ScanOnly) {
    Say ""
    Ok "仅探测模式，未修改任何系统设置"
    exit 0
}

# --- 2. 验证隧道 ---
Info "验证代理链路 ..."
if (Test-ThroughProxy -Host_ $target.Host -Port $target.Port) {
    Ok "代理链路正常"
} else {
    Warn "代理端口通，但测试请求未成功（可能是 VPN 尚未连接，或目标站点被拦）"
    Warn "仍然继续设置系统代理"
}

# --- 3. 应用到系统 ---
Info "正在设置 Windows 系统代理 ..."
Set-SystemProxy -Host_ $target.Host -Port $target.Port -SkipPac:$NoPac
Ok "系统代理已指向 $($target.Host):$($target.Port)"

Say ""
Say "-----------------------------------------------" -ForegroundColor DarkGray
Say " 代理地址 : http://$($target.Host):$($target.Port)"
if (-not $NoPac) { Say " PAC 脚本 : http://$($target.Host):$($target.Port)/proxy.pac" }
Say " 状态     : 已启用（浏览器/系统级应用立即生效）"
Say "-----------------------------------------------" -ForegroundColor DarkGray
Say ""
Say "提示：Firefox / 部分软件不读系统代理，需单独在软件内填写上面地址。" -ForegroundColor DarkGray
Say ""

# --- 4. 常驻，Ctrl+C 或关窗自动还原 ---
if ($KeepOnExit) {
    Info "-KeepOnExit 已指定，脚本退出不还原代理"
    exit 0
}

Say "正在保持代理生效。按 Ctrl+C 或关闭窗口即自动还原。" -ForegroundColor Yellow
Say ""
try {
    while ($true) {
        Start-Sleep -Seconds 5
    }
} finally {
    Say ""
    Info "正在还原系统代理 ..."
    Restore-SystemProxy
}

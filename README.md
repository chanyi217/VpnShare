# VpnShare — 把手机的代理 / VPN 共享给电脑

手机开了代理或 VPN，让同一 WiFi / 热点下的电脑也能用。

一套两端程序：**手机端 Android App** 开代理服务，**电脑端 Windows 程序**改系统代理，
中间不用任何第三方服务器。

```
电脑浏览器 ──► 电脑本机中转(127.0.0.1:8888) ──► 手机代理(App) ──► 手机网络出口
                                                                    └─ 走 VPN 隧道（若已开）
```

## 下载即用（不看源码也可以）

| 文件 | 说明 | 大小 |
|---|---|---|
| [`apk/VpnShare-v1.2.apk`](apk/) | 手机端 App（Android 7.0+ / API 24+） | 4.8 MB |
| [`pc-gui/release/VpnShare.exe`](pc-gui/release/) | 电脑端 Windows 程序，单文件免安装 | 12 MB |

**三步跑起来：**

1. 手机装 APK，打开 → 点「**启动共享**」
2. 手机按需开 VPN（不开也能用，只是只能访问国内站点）
3. 电脑双击 `VpnShare.exe` → 点「**一键启动共享**」

完整图文步骤见 [使用手册](docs/使用手册.md)。

> ⚠️ **手机端必须用 v1.2 及以上**。v1.1 之前的版本在 fake-IP 模式的 VPN 下
> 会全部连接失败（表现为浏览器 `ERR_CONNECTION_CLOSED`），v1.1 已修复，
> v1.2 增加了流量图。详情见 [原理说明](docs/原理说明.md)。

## v1.2 / v3.4：两端实时流量折线图

```
实时流量                        ↓ 1.85 MB/s   ↑ 128 KB/s
  2M ┤        ╭─╮
     │   ╭────╯ ╰──╮      ╭╮
  1M ┤ ╭─╯         ╰──────╯╰─╮
     │╱                     ╰──╮
   0 └──────────────────────────╰──────────
      绿色 = 下载（带面积填充）   蓝色 = 上传
```

两端都是 **1 秒采样 / 60 秒滑动窗口**，Y 轴量程自适应（峰值向上取整到 1/2/5×10ⁿ）。

口径说明：手机端统计代理转发的全部字节（HTTP + SOCKS5），
电脑端统计经本机中转到浏览器的字节，正常使用下两者接近但不会完全相同。

## 为什么能做到：两个关键点

### 1. 代理进程无需特殊处理就能被 VPN 接管

手机上的 VPN（`VpnService`）是 TUN 模式、网络层全局接管。
代理 App 用**普通的 `java.net.Socket`** 出站，流量天然落进隧道 ——
不需要绑定 VPN 接口，不需要 root。

这一点是通过阅读 [everyProxyCore](https://github.com/lahuman/everyProxyCore)
的源码确认的：它的 `SocketConnector` 就是裸 Socket。

### 2. ★ 开了 VPN 反而全断？那是 fake-IP 在作祟

这是本项目排查中最反直觉的一个坑，也是 v1.1 的核心修复。

**现象**：手机本机上网完全正常，电脑经代理后**所有**网页 `ERR_CONNECTION_CLOSED`。

**根因**：VPN 工作在 **fake-IP 模式** —— 它劫持 DNS，把域名一律解析成
`198.18.x.x`（RFC 保留段，公网不存在）。这个假地址**只在 VPN 内核内有意义**，
内核靠映射表把它反查回域名。任何**外部进程**（包括代理 App）用系统解析器
拿到假地址后直接 connect，必然失败。

**判据**（一条命令定位）：

```bash
adb shell curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://1.1.1.1/        # → 301 ✅
adb shell curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://www.baidu.com/  # → 000 ❌
```

**真实 IP 全通、域名全断** → 就是 fake-IP，与路由 / bypass 无关。

**修复**：53 端口的 DNS 也被内核拦截了（实测经代理向 `223.5.5.5:53` 查询
照样返回 `198.18.x.x`），所以走 **443 端口的 DoH** 拿真实 IP，再 connect：

```kotlin
// 旧（错）：InetSocketAddress(host, port) 内部 getByName → 拿到 fake-IP
// 新（对）：
val realIp = DohResolver.resolve(host)      // 443/TLS DoH
socket.connect(InetSocketAddress(realIp, port), timeout)
```

实测：`baidu → 110.242.70.57`、`google → 142.251.155.119`，端到端 7/7 通过。

## 电脑端为什么不直接指向手机

Windows 系统代理是「全有或全无」。直接指向手机时，手机没开 VPN，
`google.com` 这类目标会**挂住**（TCP 连不通一直等到超时），浏览器只能干等 ——
表现就是「网页一直转圈，跟断网一样」。

所以 v3.0 起加了一层**本机中转**：系统代理指向 `127.0.0.1:8888`，
由中转去连手机，给上游设 **3 秒超时**，连不上立刻回 502。

```
[旧] 系统代理直指手机 → 浏览器干等 8s+ 才报错
[新] 系统代理指本机中转 → 3s 内快速失败，国内站点不受影响
```

## 目录结构

```
vpn-share/
├── README.md                        <- 本文件（项目主页）
├── LICENSE                          <- MIT
├── THIRD-PARTY-NOTICES.md           <- 所有依赖与参考来源（含许可证）
│
├── apk/VpnShare-v1.2.apk            <- 【产物】手机端 APK
│
├── android-app/                     <- 【源码】Android 工程（Kotlin）
│   ├── app/src/main/java/com/vpnshare/app/
│   │   ├── MainActivity.kt              UI + 1 秒刷新循环
│   │   ├── ProxyForegroundService.kt    前台服务保活
│   │   ├── view/SparklineView.kt        ★ 流量折线图（纯 Canvas 自绘）
│   │   └── proxy/
│   │       ├── HttpProxyServer.kt       HTTP 代理 + PAC 分发
│   │       ├── SocksProxyServer.kt      SOCKS5 代理
│   │       ├── DohResolver.kt           ★ DoH 解析（绕过 fake-IP）
│   │       ├── SocketConnector.kt       出站连接（用真实 IP）
│   │       ├── TrafficMonitor.kt        ★ 速率采样（1Hz / 60 点）
│   │       ├── TcpRelay.kt              双向数据搬运
│   │       ├── ProxyRuntime.kt          统一启停
│   │       └── ProxyTypes.kt            基础类型
│   └── keystore.properties.example      签名配置模板
│
├── pc-gui/                          <- 【源码】Windows GUI（Python + Tkinter）
│   ├── app/
│   │   ├── app.py                       GUI 主程序（v3.4）
│   │   ├── proxy_core.py                代理探测 / 系统代理读写 / 手机诊断
│   │   ├── local_relay.py               本机中转（快速失败 + 流量统计）
│   │   └── traffic_chart.py         ★ 流量折线图（Canvas 手绘）
│   ├── build.py                         PyInstaller 打包脚本
│   └── release/VpnShare.exe             【产物】Windows exe
│
├── pc/                              <- 早期 PowerShell / bat 脚本（保留）
├── android/vpnshare.sh              <- 早期 Termux 脚本方案（保留）
└── docs/
    ├── 使用手册.md                      详细操作步骤
    ├── 原理说明.md                      原理、踩坑记录与排查方法
    └── GITHUB发布步骤.md               推到 GitHub 的操作流程（含登录点）
```

## 自己构建

### 手机端 APK

```bash
cd android-app
# 需要：JDK 17+、Android SDK（compileSdk 34）
./gradlew assembleRelease      # 或 gradle --no-daemon assembleRelease
```

产物在 `app/build/outputs/apk/release/`。

**签名**：仓库不含 keystore。没有签名配置时会自动退回 debug 签名，**照样能构建**。
要用自己的签名，复制模板并填写：

```bash
cp keystore.properties.example keystore.properties   # 填入你的路径与口令
```

### 电脑端 exe

```bash
cd pc-gui
python -m venv .venv
.venv/Scripts/pip install pyinstaller pillow     # pillow 仅用于生成图标
.venv/Scripts/python.exe build.py
```

产物在 `release/VpnShare.exe`。

> **必须用带 Tkinter 的 CPython 3.x**。某些 Python 发行版（如部分 3.13 托管版）
> 未编入 tkinter，打包 GUI 会失败。

## 依赖

一句话：**手机端无第三方运行库，电脑端也无第三方运行库**。

- 手机端只用 Android 标准库（Socket / SSL / org.json / Canvas），图表是 Canvas 手绘
- 电脑端只用 Python 标准库 + 自带 Tkinter，图表是 Canvas 手绘
- 图表刻意没用 MPAndroidChart / matplotlib —— 前者会让 APK 涨数百 KB，后者让 exe 涨约 30 MB

构建期用到的第三方库（PyInstaller、Material Components、AndroidX 等），
以及调研阶段参考过的开源项目，全部列在
[**THIRD-PARTY-NOTICES.md**](THIRD-PARTY-NOTICES.md) 中。

## 常见问题

**Q：手机没开 VPN 能用吗？**
能。此时就是一个普通代理，国内站点正常访问；国外站点 3 秒内快速报错（不会卡死）。

**Q：开了 VPN 后电脑全站 ERR_CONNECTION_CLOSED？**
先确认手机端是 **v1.2**（旧版有 fake-IP bug）。然后用上面那条 `curl` 判据确认是否 fake-IP。

**Q：电脑端提示"链路不通"？**
程序会**拒绝**写系统代理（避免把浏览器搞废），并在日志里给出具体原因。
常见：手机代理没启动、手机省电策略冻结了后台、手机上多个 App 抢同一个端口。

**Q：Firefox 能用吗？**
Firefox 不读系统代理，需单独在它的设置里填 `http://手机IP:8080`。

## 许可证

[MIT](LICENSE)。第三方依赖均为宽松许可证，详见
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。

本项目仅用于合法的局域网网络共享。请遵守你所在地区的网络相关法律法规。

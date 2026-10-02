# 第三方依赖与参考来源

本项目在实现过程中使用了以下开源项目与外部服务。
分三类说明：**构建期依赖**（打进产物）、**运行期依赖**（运行必需）、**参考来源**（调研时读过，未直接复用代码）。

---

## 一、手机端（Android / Kotlin）

### 构建期依赖（会打进 APK）

| 项目 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| [Android Gradle Plugin](https://developer.android.com/build) | 8.5.2 | Apache-2.0（Google） | Android 构建插件 |
| [Kotlin](https://github.com/JetBrains/kotlin) | 1.9.24 | Apache-2.0 | 开发语言 |
| [Gradle](https://github.com/gradle/gradle) | 8.14.3 | Apache-2.0 | 构建工具 |
| [AndroidX Core KTX](https://github.com/androidx/androidx) | 1.13.1 | Apache-2.0 | `ContextCompat`、`ContentValues` 等基础扩展 |
| [AndroidX AppCompat](https://github.com/androidx/androidx) | 1.7.0 | Apache-2.0 | `AppCompatActivity`、ViewBinding 宿主 |
| [Material Components for Android](https://github.com/material-components/material-components-android) | 1.12.0 | Apache-2.0 | 卡片、Switch、主题配色 |
| [AndroidX ConstraintLayout](https://github.com/androidx/androidx) | 2.1.4 | Apache-2.0 | 布局容器（部分界面用到） |
| [AndroidX Lifecycle Runtime KTX](https://github.com/androidx/androidx) | 2.8.4 | Apache-2.0 | 生命周期感知 |
| [Kotlinx Coroutines Android](https://github.com/Kotlin/kotlinx.coroutines) | 1.8.1 | Apache-2.0 | 协程支持 |

**JDK**：Eclipse Temurin 21（`compileOptions` 设为 Java 17 兼容）

### 运行期依赖

**无第三方运行库。** 代理核心（HTTP / SOCKS5 / PAC / TCP 中转 / DoH）全部基于
Java / Android 标准库手写：

- `java.net.Socket` / `ServerSocket` —— 代理监听与出站连接
- `javax.net.ssl.SSLContext` / `SSLSocketFactory` —— DoH 的 443/TLS 查询
- `org.json`（Android 平台内置）—— 解析 DoH 返回的 JSON
- `java.util.concurrent.atomic.AtomicLong` —— 流量字节计数
- `android.graphics.Canvas` / `Path` / `LinearGradient` —— 实时流量折线图自绘

> 折线图**没有**使用 MPAndroidChart 等图表库，是纯 Canvas 手绘，
> 目的是避免 APK 体积增加数百 KB（实测整包仅 4.8 MB）。

### 外部服务（非代码依赖）

| 服务 | 用途 | 备注 |
|---|---|---|
| [阿里公共 DoH](https://alidns.com) `dns.alidns.com`（223.5.5.5 / 223.6.6.6） | 主 DoH 解析（443 端口） | 用于绕过 VPN 的 fake-IP 劫持 |
| [DNSPod 公共 DoH](https://www.dnspod.cn) `doh.pub`（119.29.29.29） | 备用 DoH 解析 | 同上 |

> **为什么需要 DoH**：见 `docs/原理说明.md` 的 fake-IP 章节。
> 若不想使用这两个公共 DoH，可在 `DohResolver.kt` 的 `SERVERS` 列表中替换为你自己的。

---

## 二、电脑端（Windows / Python）

### 运行期依赖

**零第三方依赖。** 仅使用 Python 标准库与自带的 Tkinter：

| 模块 | 用途 |
|---|---|
| `tkinter` / `tkinter.ttk` | 图形界面（Python 自带） |
| `socket` / `select` | 本机中转的 TCP 转发与双向泵 |
| `threading` | 每连接一线程 |
| `winreg` | 读写 Windows 系统代理注册表 |
| `ctypes` | 调 `InternetSetOptionW` 通知 WinINET 刷新 |
| `ipaddress` / `re` / `json` / `subprocess` / `shutil` | IP 校验、文本解析、adb 调用等 |

> 这是有意为之：不依赖 requests / psutil 等库，
> 打包后的 exe 才可能做到单文件、免安装、12 MB。

### 构建期依赖（不进产物，仅在本机打包 exe 时需要）

| 项目 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | 6.22.3 | GPL-2.0 + 例外条款 | 把 Python 程序打包成单文件 exe |
| [Pillow](https://github.com/python-pillow/Pillow) | 12.3.0 | HPND | 仅用于**生成应用图标**（`icon.ico`），产物不含它 |
| [pywin32-ctypes](https://github.com/mhammond/pywin32-ctypes) | 0.2.3 | PSF-2.0 | PyInstaller 的 Windows 辅助依赖 |

> **关于 PyInstaller 的 GPL**：PyInstaller 自带
> [例外条款](https://github.com/pyinstaller/pyinstaller/blob/develop/LICENSE)，
> 明确允许用它打包**任意许可证**的程序（含闭源商业软件），
> 不会传染到你自己的代码。本项目的 MIT 许可不受影响。

**Python 版本要求**：3.12（打包 GUI **必须**用带 Tkinter 的 CPython；
托管的 Python 3.13 未编入 tkinter，无法打包 GUI）

### 外部工具（可选）

| 工具 | 用途 | 是否必需 |
|---|---|---|
| `adb`（Android SDK Platform Tools） | 自动检测手机 App 存活、后台权限、VPN 状态 | **可选**；没有它程序照常工作，只是少几项自动诊断 |

---

## 三、调研阶段参考过的开源项目（未复用代码）

本项目是**独立实现**，未 fork 或复制下列项目的代码。
它们的作用是在需求调研阶段帮我确认「手机共享 VPN」这条路径可行，
以及验证代理服务的基本写法：

| 项目 | 语言 | 许可证 | 参考了什么 |
|---|---|---|---|
| [lahuman/everyProxyCore](https://github.com/lahuman/everyProxyCore) | Kotlin | — | **最关键**：验证了 Every Proxy 的 `SocketConnector` 就是裸 `java.net.Socket`，证明「代理进程无需特殊处理即可被 VPN 接管」 |
| [hect0x7/android-proxy-server](https://github.com/hect0x7/android-proxy-server) | Kotlin | Apache-2.0 | 代理服务在 Android 上的工程结构 |
| [heiher/socks5](https://github.com/heiher/socks5) | Java | MIT | SOCKS5 握手与 CONNECT 的字段格式 |
| [h4ckm310n/S5W2C](https://github.com/h4ckm310n/S5W2C) | Kotlin | — | SOCKS5 转 HTTP 的实现思路 |
| [code3-dev/GNet](https://github.com/code3-dev/GNet) | Kotlin | GPL-3.0 | 参考了 Android 网络库的组织方式（方向相反，它是客户端） |
| [1dao/xproxy-android](https://github.com/1dao/xproxy-android) | C | — | 原生层代理实现的对照 |

---

## 四、协议与文档参考

实现 SOCKS5 / HTTP 代理时遵循的公开标准：

- **RFC 1928** — SOCKS Protocol Version 5
- **RFC 1929** — Username/Password Authentication for SOCKS V5
- **RFC 7230 / 7231** — HTTP/1.1 Message Syntax and Routing（代理请求格式、CONNECT 方法）
- **RFC 8484** — DNS Queries over HTTPS (DoH)
- **RFC 5737** — `198.18.0.0/15` 等保留地址段（fake-IP 用到的地址空间）

---

## 五、许可证汇总

本项目主体采用 **MIT**（见 `LICENSE`）。

所有第三方依赖均为宽松许可证（Apache-2.0 / MIT / HPND / PSF-2.0），
不含 AGPL 等具有网络传染性的条款，可安全用于闭源二次开发。

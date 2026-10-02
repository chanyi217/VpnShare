# VpnShare — 手机端 APK

把手机上的代理/VPN 一键共享给同网络的电脑使用。免 root。

## 直接安装

APK 已编译好，位置：

```
vpn-share/android-app/app/build/outputs/apk/release/app-release.apk
```

把这个文件传到手机，点击安装即可（需要允许「安装未知来源应用」）。

- 包名：`com.vpnshare.app`
- 最低系统：Android 7.0（API 24）
- 目标系统：Android 14（API 34）
- 已用自签名证书签名，可直接安装

## 使用步骤

1. **手机先连 VPN**（全局模式）
2. 打开 VpnShare，点「启动共享」
3. 界面会显示三个地址，例如：

```
HTTP 代理 : 192.168.43.1:8080
SOCKS5    : 192.168.43.1:1080
PAC 脚本  : http://192.168.43.1:8080/proxy.pac
```

4. **电脑与手机连同一个 WiFi/热点**
5. 电脑上把系统代理设成上面的地址：
   - 最简单：双击 `pc/一键启动-共享VPN.bat`（会自动找到手机并设置）
   - 手动：Windows 设置 → 网络和 Internet → 代理 → 手动设置代理

## 界面功能

| 功能 | 说明 |
|---|---|
| 服务状态 | 运行中 / 已停止 |
| 本机局域网 IP | 自动识别，点击「刷新」重新检测 |
| 三个地址 | 点击任意一行即可复制 |
| 复制 PAC 脚本内容 | 把 PAC 的 JS 内容复制出来，可手动存成 .pac 文件 |
| HTTP 端口 | 默认 8080，可改 |
| 启用 SOCKS5 | 默认开，端口 1080 |
| 启用 PAC 服务 | 默认开 |
| 访问认证 | 填用户名密码则开启 Basic 认证 |

## 自己改代码后重新编译

环境要求：JDK 17+、Android SDK（platform 34 + build-tools 34.0.0）。

```bash
cd android-app

# 1. 配置 SDK 路径（改成你自己的）
echo 'sdk.dir=C\:\\Users\\你的用户名\\Android\\Sdk' > local.properties

# 2. 编译
./gradlew assembleRelease

# 产物在 app/build/outputs/apk/release/app-release.apk
```

### 签名

仓库**不含** keystore 与口令（已被 `.gitignore` 排除，否则等于把私钥公开）。

默认行为：**没有 keystore 时自动退回 debug 签名**，clone 下来直接 `assembleRelease` 也能出包，
只是装到手机上会被视为 debug 包——自用完全够。

要出正式签名包，在本地放好 keystore 后：

```bash
cp keystore.properties.example keystore.properties   # 再填入你自己的口令
```

或者用环境变量 `VPN_SHARE_KS_PWD`，连文件都不用落盘。
`keystore.properties` 已在 `.gitignore` 中，不会被提交。

## 代码结构

```
app/src/main/java/com/vpnshare/app/
├── MainActivity.kt                  主界面
├── ProxyForegroundService.kt        前台服务（保证后台存活 + 通知栏状态）
├── util/NetUtils.kt                 获取局域网 IP
└── proxy/
    ├── ProxyTypes.kt                基础类型、认证、统计
    ├── SocketConnector.kt           出站连接（关键：就是普通 Socket）
    ├── TcpRelay.kt                  双向数据搬运
    ├── HttpProxyServer.kt           HTTP/HTTPS 代理（含 CONNECT 隧道）
    ├── SocksProxyServer.kt          SOCKS5 代理（RFC 1928/1929）
    ├── PacFile.kt                   PAC 脚本生成
    └── ProxyRuntime.kt              统一启停管理
```

## 为什么"共享 VPN"能生效

核心就在 `SocketConnector.kt`：

```kotlin
fun connect(host: String, port: Int): Socket {
    val socket = Socket()
    socket.tcpNoDelay = true
    socket.connect(InetSocketAddress(host, port), connectTimeoutMs)
    return socket
}
```

**没有任何 VPN 相关代码。** 手机 VPN 工作在系统网络层（TUN 模式），
本进程发起的普通连接会被自动接管。代理只是提供了"一个对外的入口"，
流量出了手机就进隧道了。Every Proxy 的原理完全一样。

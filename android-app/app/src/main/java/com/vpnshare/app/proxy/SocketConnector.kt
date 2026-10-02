package com.vpnshare.app.proxy

import java.net.InetSocketAddress
import java.net.Socket

/**
 * 出站连接器。
 *
 * ============================ ★ 这里是核心修复点 ============================
 *
 * 【旧实现的问题】
 * ```kotlin
 * socket.connect(InetSocketAddress(host, port), connectTimeoutMs)
 * ```
 * `InetSocketAddress(host, port)` 内部会调 `InetAddress.getByName(host)`，
 * 走 libc `getaddrinfo` → 被手机的 fake-IP VPN 劫持 → 返回 `198.18.x.x`
 * → 拿这个**假地址**去 connect → VPN 内核认不出 → 连接被重置、0 字节。
 *
 * 【新实现】
 * 先经 **DoH(443端口)** 拿到**真实 IP**，再用真实 IP 连接：
 * ```kotlin
 * val realIp = DohResolver.resolve(host)   // 绕过 53 端口劫持
 * socket.connect(InetSocketAddress(realIp, port), connectTimeoutMs)
 * ```
 * 实测：真实 IP 可以正常走 VPN 隧道；fake-IP 不行。
 *
 * 【回退策略】
 * 如果 DoH 也失败（极端网络环境），退回系统解析。
 * 虽然在 fake-IP VPN 下会失败，但在**没有 VPN 的环境**下是正常的，
 * 而且能让"手机没开 VPN 时共享国内网络"这个主场景继续工作。
 *
 * 注意：这里就是最普通的 java.net.Socket —— 没有 bind 特定网卡、
 * 没有 setNetwork()。之所以共享 VPN 能生效，是因为手机 VPN 工作在
 * 系统网络层（TUN），本进程发起的普通连接会自动被 VPN 接管。
 */
class DirectSocketConnector(private val connectTimeoutMs: Int = 15_000) {

    /** 统计：DoH 解析成功/回退次数，便于诊断 */
    @Volatile var dohHits = 0
        private set
    @Volatile var fallbackCount = 0
        private set

    /** 可选日志回调 */
    var onLog: ((String) -> Unit)? = null

    fun connect(host: String, port: Int): Socket {
        val targetIp = resolveTarget(host)

        val socket = Socket()
        socket.tcpNoDelay = true
        socket.soTimeout = 0
        socket.connect(InetSocketAddress(targetIp, port), connectTimeoutMs)
        return socket
    }

    /**
     * 决定用哪个 IP 去连：
     *   1) 本来是 IP 字面量 → 直接用
     *   2) DoH 能解析出真实 IP → 用真实的（★ 关键路径）
     *   3) DoH 失败 → 回退系统解析（在无 VPN 环境可用）
     */
    private fun resolveTarget(host: String): String {
        if (DohResolver.isIpLiteral(host)) return host

        val real = try {
            DohResolver.resolve(host, timeoutMs = 5000)
        } catch (_: Exception) {
            null
        }
        if (real != null) {
            dohHits++
            onLog?.invoke("DoH 解析 $host → $real")
            return real
        }

        // ---- 回退：系统解析 ----
        fallbackCount++
        val sys = try {
            java.net.InetAddress.getByName(host).hostAddress
        } catch (_: Exception) {
            host
        }
        if (sys != null && sys.startsWith("198.18.")) {
            // 明确识别出 fake-IP：说明 VPN 在跑但 DoH 挂了，这个连接注定失败
            onLog?.invoke("⚠️ $host 被解析为 fake-IP($sys)，DoH 未能取得真实地址")
        } else {
            onLog?.invoke("DoH 未命中，回退系统解析 $host → ${sys ?: host}")
        }
        return sys ?: host
    }
}

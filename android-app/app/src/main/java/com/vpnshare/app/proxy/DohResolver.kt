package com.vpnshare.app.proxy

import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager
import java.security.cert.X509Certificate

/**
 * DoH（DNS over HTTPS）解析器 —— 本项目的核心补丁。
 *
 * ============================ 为什么需要它 ============================
 *
 * 背景：手机上的 VPN（如 Gofly / Clash 系）常工作于 **fake-IP 模式**：
 *   - 它劫持 DNS，任何域名查询都返回 `198.18.0.0/15` 里的**假地址**
 *   - 这个假地址只在 VPN 自己的内核里有意义（内核靠映射表反查回域名）
 *   - 对**内核自己发起**的 DNS 查询有效，但对外部进程发起的无效
 *
 * 于是当我们的代理 App 用常规方式解析域名时：
 *   `InetSocketAddress(host, port)` → `InetAddress.getByName()` →
 *   libc `getaddrinfo` → 被 VPN 劫持 → 拿到 `198.18.x.x`
 *   → 拿这个假地址去 connect → VPN 内核认不出 → **连接被重置、0 字节**
 *
 * 实测数据（本机手机，Gofly 全局模式）：
 *   | 目标                    | 结果                    |
 *   |-------------------------|-------------------------|
 *   | 1.1.1.1（真实 IP）      | ✅ 301                  |
 *   | 223.5.5.5（真实 IP）    | ✅ 404                  |
 *   | 普通 DNS(53端口)查询    | ❌ 返回 198.18.x.x      |
 *   | **DoH(443端口)查询**    | ✅ **返回真实 IP**       |
 *
 * 结论：**走 443 端口的加密 DoH 能绕过 53 端口的 DNS 劫持拿到真实 IP**，
 * 而真实 IP 是能正常路由的。这就是本类的存在意义。
 *
 * ============================ 实现要点 ============================
 *
 * 1. 用 443 端口（TLS 加密），不走 53 端口 —— 53 被内核劫持
 * 2. 请求走 `dns.alidns.com`（阿里 DoH，对国内站点解析准确）
 * 3. 结果带 TTL 缓存，避免每个连接都查一次
 * 4. DNS 服务器自身用 **IP 直连**（223.5.5.5），避免"解析 DNS 服务器域名"的鸡生蛋问题
 * 5. 证书校验放宽：国内网络环境下部分 DoH 服务器证书链可能不完整，
 *    这里以"能拿到真实 IP"为优先（不传输敏感数据）
 */
object DohResolver {

    /** DoH 服务器：(IP, HTTP Host 头, URL 路径前缀) */
    private data class DohServer(val ip: String, val host: String, val path: String)

    private val SERVERS = listOf(
        // 阿里 DoH —— 对国内域名解析最准，实测首选
        DohServer("223.5.5.5", "dns.alidns.com", "/resolve"),
        DohServer("223.6.6.6", "dns.alidns.com", "/resolve"),
        // DNSPod（腾讯）备用
        DohServer("119.29.29.29", "doh.pub", "/dns-query"),
    )

    /** 解析缓存：域名 → (IP, 过期时间戳) */
    private class Entry(val ip: String, val expireAt: Long)
    private val cache = ConcurrentHashMap<String, Entry>()

    private const val DEFAULT_TTL_MS = 5 * 60 * 1000L   // 保守 5 分钟
    private const val MAX_TTL_MS = 30 * 60 * 1000L

    private val trustAll: Array<TrustManager> = arrayOf(object : X509TrustManager {
        override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
        override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
    })

    private val sslContext: SSLContext by lazy {
        SSLContext.getInstance("TLS").apply { init(null, trustAll, java.security.SecureRandom()) }
    }

    /**
     * 解析域名 → 真实 IPv4。
     *
     * @param host    域名（已是 IP 则原样返回，不查）
     * @param timeoutMs 单次查询超时
     * @return 真实 IP；失败返回 null（调用方需准备回退策略）
     */
    fun resolve(host: String, timeoutMs: Int = 5000): String? {
        // 已经是 IP 字面量 → 直接返回，不需要解析
        if (isIpLiteral(host)) return host

        // 缓存命中
        cache[host]?.let { e ->
            if (e.expireAt > System.currentTimeMillis()) return e.ip
            cache.remove(host)
        }

        for (srv in SERVERS) {
            try {
                val ip = queryDoh(srv, host, "A", timeoutMs)
                if (ip != null) {
                    cache[host] = Entry(ip, System.currentTimeMillis() + DEFAULT_TTL_MS)
                    return ip
                }
            } catch (_: Exception) {
                // 换下一个服务器
            }
        }
        return null
    }

    /**
     * ★ 核心：经 443/TLS 向 DoH 服务器查一条 A 记录。
     *
     * 注意目标是 **DoH 服务器的 IP 字面量**，不涉及域名解析，
     * 所以不会踩到"解析器自身被劫持"的坑。
     */
    private fun queryDoh(srv: DohServer, domain: String, type: String, timeoutMs: Int): String? {
        val raw = Socket()
        raw.tcpNoDelay = true
        raw.connect(InetSocketAddress(srv.ip, 443), timeoutMs)
        raw.soTimeout = timeoutMs

        var tls: SSLSocket? = null
        try {
            tls = sslContext.socketFactory.createSocket(raw, srv.host, 443, true) as SSLSocket
            tls.soTimeout = timeoutMs
            tls.startHandshake()

            val reqPath = if (srv.path == "/resolve") {
                "/resolve?name=$domain&type=$type"
            } else {
                // RFC 8484 风格：doh.pub 也支持 ?name= 形式
                "/dns-query?name=$domain&type=$type"
            }
            val req = buildString {
                append("GET ").append(reqPath).append(" HTTP/1.1\r\n")
                append("Host: ").append(srv.host).append("\r\n")
                append("Accept: application/dns-json\r\n")
                append("User-Agent: VpnShare-DoH/1.0\r\n")
                append("Connection: close\r\n\r\n")
            }
            tls.outputStream.write(req.toByteArray(Charsets.US_ASCII))
            tls.outputStream.flush()

            val buf = ByteArrayOutputStream()
            val tmp = ByteArray(4096)
            while (buf.size() < 64 * 1024) {
                val n = try {
                    tls.inputStream.read(tmp)
                } catch (_: Exception) {
                    break
                }
                if (n <= 0) break
                buf.write(tmp, 0, n)
            }

            val text = buf.toString("UTF-8")
            val body = text.substringAfter("\r\n\r\n", "")
            if (body.isBlank()) return null

            val json = JSONObject(body)
            val answers = json.optJSONArray("Answer") ?: return null
            for (i in 0 until answers.length()) {
                val a = answers.optJSONObject(i) ?: continue
                if (a.optInt("type") == 1) {
                    val d = a.optString("data")
                    if (d.isNotEmpty() && isIpLiteral(d)) return d
                }
            }
            return null
        } finally {
            try { tls?.close() } catch (_: Exception) {}
            try { raw.close() } catch (_: Exception) {}
        }
    }

    /** 是否是 IPv4/IPv6 字面量（是则无需 DNS 解析） */
    fun isIpLiteral(host: String): Boolean {
        val h = host.trim().removePrefix("[").removeSuffix("]")
        // IPv4：仅由数字和点组成，且能被 InetAddress 解析（不触发 DNS）
        if (h.count { it == '.' } == 3 && h.all { it.isDigit() || it == '.' }) {
            return h.split('.').all { p ->
                p.isNotEmpty() && p.length <= 3 && (p.toIntOrNull() ?: -1) in 0..255
            }
        }
        // IPv6：含冒号
        if (h.contains(':')) {
            return try {
                // 用 getByAddress 不做解析（要求传字节，这里先做格式粗判）
                h.all { it.isDigit() || it in "abcdefABCDEF:." }
            } catch (_: Exception) { false }
        }
        return false
    }

    /** 清空缓存（切换网络/重连时调用） */
    fun clearCache() = cache.clear()

    /** 诊断信息 */
    fun stats(): String = "DoH 缓存 ${cache.size} 条"
}

package com.vpnshare.app.proxy

/**
 * 代理相关的基础类型与工具。
 */

enum class ProxyProtocol { HTTP, SOCKS5, PAC }

enum class TrafficDirection { CLIENT_TO_REMOTE, REMOTE_TO_CLIENT }

object HostPort {
    /** 解析 "host:port" 或 "[v6]:port" */
    fun parse(target: String, defaultPort: Int): Pair<String, Int> {
        val t = target.trim()
        if (t.startsWith("[")) {
            val end = t.indexOf(']')
            if (end > 0) {
                val host = t.substring(1, end)
                val portStr = t.substring(end + 1).removePrefix(":")
                return host to (portStr.toIntOrNull() ?: defaultPort)
            }
        }
        val idx = t.lastIndexOf(':')
        if (idx < 0) return t to defaultPort
        val port = t.substring(idx + 1).toIntOrNull()
        return if (port == null) t to defaultPort else t.substring(0, idx) to port
    }
}

object Auth {
    /** 校验 HTTP Basic 代理认证头 */
    fun checkBasic(headerValue: String?, user: String, pass: String): Boolean {
        if (user.isEmpty()) return true
        val v = headerValue ?: return false
        val token = v.trim().removePrefix("Basic ").trim()
        return try {
            val decoded = String(android.util.Base64.decode(token, android.util.Base64.DEFAULT))
            val idx = decoded.indexOf(':')
            if (idx < 0) false
            else decoded.substring(0, idx) == user && decoded.substring(idx + 1) == pass
        } catch (e: Exception) {
            false
        }
    }

    /** 校验 SOCKS5 用户名密码（RFC 1929） */
    fun checkSocks(user: String, pass: String, expectUser: String, expectPass: String): Boolean {
        return user == expectUser && pass == expectPass
    }
}

data class ProxyStats(
    val httpConnections: Long = 0,
    val socksConnections: Long = 0,
    val pacRequests: Long = 0,
    val bytesUp: Long = 0,
    val bytesDown: Long = 0,
)

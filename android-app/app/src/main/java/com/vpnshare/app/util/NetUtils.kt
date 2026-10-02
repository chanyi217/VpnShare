package com.vpnshare.app.util

import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * 获取本机局域网 IP。
 * 优先 wlan0，其次其它非回环 IPv4 接口。
 */
object NetUtils {

    fun getLanIp(): String {
        val candidates = mutableListOf<Pair<String, String>>()  // ifaceName to ip

        try {
            val ifaces = NetworkInterface.getNetworkInterfaces() ?: return ""
            for (nif in ifaces) {
                if (!nif.isUp || nif.isLoopback) continue
                for (addr in nif.inetAddresses) {
                    if (addr is Inet4Address && !addr.isLoopbackAddress) {
                        val ip = addr.hostAddress ?: continue
                        if (ip.startsWith("127.")) continue
                        candidates.add(nif.name to ip)
                    }
                }
            }
        } catch (_: Exception) {
            return ""
        }

        if (candidates.isEmpty()) return ""

        // 优先级：wlan > ap > eth > 其它
        val priority = listOf("wlan", "ap", "swlan", "eth", "rmnet")
        for (p in priority) {
            candidates.firstOrNull { it.first.startsWith(p) }?.let { return it.second }
        }
        return candidates.first().second
    }

    /** 粗略判断是否可能是热点网段（手机做热点时一般是 x.x.x.1 或 .43.1） */
    fun describe(ip: String): String = when {
        ip.isEmpty() -> "未获取到"
        ip.startsWith("192.168.43.") -> "$ip（疑似热点）"
        ip.startsWith("172.20.10.") -> "$ip（疑似热点）"
        else -> ip
    }
}

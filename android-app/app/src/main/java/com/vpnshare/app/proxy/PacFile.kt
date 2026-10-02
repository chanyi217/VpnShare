package com.vpnshare.app.proxy

/**
 * PAC（Proxy Auto-Config）脚本生成器。
 *
 * PAC 就是一个含 FindProxyForURL(url, host) 的 JS 文件，
 * 浏览器每次请求前调用它，返回值决定走哪个代理。
 *
 * 返回串格式：用 ";" 分隔的候选链，按顺序尝试。
 *   SOCKS5 ip:port  走 SOCKS5
 *   PROXY  ip:port  走 HTTP
 *   DIRECT          直连
 */
object PacFileGenerator {

    data class Config(
        val host: String,
        val httpPort: Int,
        val socksPort: Int,
        val httpEnabled: Boolean,
        val socksEnabled: Boolean,
        /** 这些域名/网段直连，不走代理 */
        val directPatterns: List<String> = emptyList(),
    )

    fun generate(config: Config): String {
        require(config.httpEnabled || config.socksEnabled) {
            "PAC 需要至少启用 HTTP 或 SOCKS5 之一"
        }
        require(config.host.isValidHost()) { "PAC host 含非法字符" }

        val host = config.host.toPacHost()

        // 代理链
        val chain = buildList {
            if (config.socksEnabled) add("SOCKS5 $host:${config.socksPort}")
            if (config.httpEnabled) add("PROXY $host:${config.httpPort}")
            add("DIRECT")
        }.joinToString("; ")

        // 直连规则（可选）
        val directBlock = if (config.directPatterns.isEmpty()) {
            ""
        } else {
            val conditions = config.directPatterns.joinToString(" ||\n            ") { p ->
                "shExpMatch(host, \"$p\")"
            }
            """
            if ($conditions) {
                return "DIRECT";
            }
            """.trimIndent()
        }

        return """
            // 由 VpnShare 自动生成
            // 把手机上的代理共享给局域网设备（电脑等）
            function FindProxyForURL(url, host) {
            $directBlock
                return "$chain";
            }
        """.trimIndent()
    }

    /** 直接访问本机/内网地址时不该绕代理 */
    val DEFAULT_DIRECT = listOf(
        "localhost",
        "127.*",
        "10.*",
        "172.16.*", "172.17.*", "172.18.*", "172.19.*",
        "172.2?.*", "172.30.*", "172.31.*",
        "192.168.*",
        "*.local",
    )

    private fun String.isValidHost(): Boolean =
        none { it == '"' || it == '\\' || it.isISOControl() || it == '\n' || it == '\r' }

    /** IPv6 需要方括号 */
    private fun String.toPacHost(): String =
        if (contains(":") && !(startsWith("[") && endsWith("]"))) "[$this]" else this
}

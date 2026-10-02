package com.vpnshare.app.proxy

import android.util.Log

/**
 * 代理运行时：统一启停 HTTP / SOCKS5 / PAC，并持有统计。
 */
class ProxyRuntime {

    companion object {
        private const val TAG = "ProxyRuntime"
        @Volatile private var instance: ProxyRuntime? = null

        fun get(): ProxyRuntime =
            instance ?: synchronized(this) {
                instance ?: ProxyRuntime().also { instance = it }
            }
    }

    data class Config(
        val httpEnabled: Boolean,
        val httpPort: Int,
        val socksEnabled: Boolean,
        val socksPort: Int,
        val lanIp: String,
        val user: String,
        val pass: String,
        val pacEnabled: Boolean,
    )

    private var http: HttpProxyServer? = null
    private var socks: SocksProxyServer? = null

    /**
     * ★ v1.1：连接器挂上日志回调，便于观察 DoH 解析是否命中。
     * DoH 是本版的核心修复 —— 旧版直接用系统解析器，
     * 在 fake-IP 模式的 VPN 下会拿到 198.18.x.x 假地址导致全部连接失败。
     */
    private val connector = DirectSocketConnector().apply {
        onLog = { msg -> log(msg) }
    }

    @Volatile var isRunning = false
        private set

    @Volatile var currentConfig: Config? = null
        private set

    private val logs = ArrayDeque<String>()

    fun snapshotLogs(): List<String> = synchronized(logs) { logs.toList() }

    private fun log(msg: String) {
        Log.i(TAG, msg)
        synchronized(logs) {
            logs.addLast(msg)
            while (logs.size > 200) logs.removeFirst()
        }
    }

    fun start(cfg: Config) {
        if (isRunning) stop()

        // 每次启动清空 DoH 缓存：网络环境/VPN 状态可能已变，
        // 旧的解析结果（尤其是 fake-IP 环境的）不应复用。
        DohResolver.clearCache()

        val auth = if (cfg.user.isNotEmpty()) cfg.user to cfg.pass else null

        val pacProvider: (() -> String)? = if (cfg.pacEnabled) {
            {
                PacFileGenerator.generate(
                    PacFileGenerator.Config(
                        host = cfg.lanIp,
                        httpPort = cfg.httpPort,
                        socksPort = cfg.socksPort,
                        httpEnabled = cfg.httpEnabled,
                        socksEnabled = cfg.socksEnabled,
                        directPatterns = PacFileGenerator.DEFAULT_DIRECT,
                    )
                )
            }
        } else null

        try {
            val h = HttpProxyServer(
                bindHost = "0.0.0.0",
                port = cfg.httpPort,
                connector = connector,
                pacProvider = pacProvider,
                auth = auth,
                onLog = { log(it) },
            )
            h.start()
            http = h

            if (cfg.socksEnabled) {
                val s = SocksProxyServer(
                    bindHost = "0.0.0.0",
                    port = cfg.socksPort,
                    connector = connector,
                    auth = auth,
                    onLog = { log(it) },
                )
                s.start()
                socks = s
            }

            currentConfig = cfg
            isRunning = true
            // ★ v1.2：启动流量采集。必须在 isRunning=true 之后调用，
            // 因为 reset() 会读 stats() 取当前累计值作为基线。
            TrafficMonitor.reset()
            log("代理已启动：HTTP=${if (cfg.httpEnabled) cfg.httpPort else "关"} " +
                "SOCKS5=${if (cfg.socksEnabled) cfg.socksPort else "关"}")
        } catch (e: Exception) {
            log("启动失败：${e.message}")
            stop()
            throw e
        }
    }

    fun stop() {
        try { http?.close() } catch (_: Exception) {}
        try { socks?.close() } catch (_: Exception) {}
        http = null
        socks = null
        isRunning = false
        // 停止采集但保留历史曲线，让用户还能看到停止前的流量形态
        TrafficMonitor.stop()
        log("代理已停止")
    }

    fun stats(): ProxyStats {
        val h = http?.stats() ?: ProxyStats()
        val s = socks?.stats() ?: ProxyStats()
        return ProxyStats(
            httpConnections = h.httpConnections + s.socksConnections,
            socksConnections = s.socksConnections,
            pacRequests = h.pacRequests,
            bytesUp = h.bytesUp + s.bytesUp,
            bytesDown = h.bytesDown + s.bytesDown,
        )
    }

    /** PAC 脚本预览（给 UI 展示用） */
    fun previewPac(): String {
        val cfg = currentConfig ?: return ""
        return PacFileGenerator.generate(
            PacFileGenerator.Config(
                host = cfg.lanIp,
                httpPort = cfg.httpPort,
                socksPort = cfg.socksPort,
                httpEnabled = cfg.httpEnabled,
                socksEnabled = cfg.socksEnabled,
                directPatterns = PacFileGenerator.DEFAULT_DIRECT,
            )
        )
    }

    fun clearLogs() = synchronized(logs) { logs.clear() }
}

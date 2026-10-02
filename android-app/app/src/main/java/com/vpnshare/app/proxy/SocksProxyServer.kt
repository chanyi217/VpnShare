package com.vpnshare.app.proxy

import android.util.Log
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

/**
 * SOCKS5 代理服务器（RFC 1928 + RFC 1929 用户名密码认证）。
 *
 * 握手流程：
 *   1. 客户端 -> 0x05 NMETHODS METHODS...
 *   2. 服务端 -> 0x05 METHOD            (0x00 无需认证 / 0x02 用户名密码 / 0xFF 拒绝)
 *   3. [若 0x02] 客户端 -> 0x01 ULEN UNAME PLEN PASSWD
 *               服务端 -> 0x01 STATUS
 *   4. 客户端 -> 0x05 CMD RSV ATYP DST.ADDR DST.PORT
 *   5. 服务端 -> 0x05 REP RSV ATYP BND.ADDR BND.PORT
 *   6. 双向转发
 */
class SocksProxyServer(
    private val bindHost: String,
    private val port: Int,
    private val connector: DirectSocketConnector,
    private val auth: Pair<String, String>?,
    private val onLog: (String) -> Unit,
) : AutoCloseable {

    companion object {
        private const val TAG = "SocksProxyServer"
        private const val VER = 0x05
        private const val METHOD_NO_AUTH = 0x00
        private const val METHOD_USERPASS = 0x02
        private const val METHOD_NONE = 0xFF
        private const val CMD_CONNECT = 0x01
        private const val ATYP_IPV4 = 0x01
        private const val ATYP_DOMAIN = 0x03
        private const val ATYP_IPV6 = 0x04
        private const val REP_SUCCESS = 0x00
        private const val REP_HOST_UNREACH = 0x04
        private const val REP_CMD_UNSUPPORTED = 0x07
    }

    private val running = AtomicBoolean(false)
    private lateinit var serverSocket: ServerSocket
    private val connCount = AtomicLong(0)
    private val bytesUp = AtomicLong(0)
    private val bytesDown = AtomicLong(0)

    val boundPort: Int get() = if (::serverSocket.isInitialized) serverSocket.localPort else port
    fun stats() = ProxyStats(
        socksConnections = connCount.get(),
        bytesUp = bytesUp.get(),
        bytesDown = bytesDown.get(),
    )

    fun start() {
        check(running.compareAndSet(false, true)) { "SOCKS5 代理已启动" }
        serverSocket = ServerSocket().apply {
            reuseAddress = true
            bind(InetSocketAddress(bindHost, port))
        }
        onLog("SOCKS5 代理监听 $bindHost:${boundPort}")
        thread(name = "socks-accept", isDaemon = true) { acceptLoop() }
    }

    private fun acceptLoop() {
        while (running.get()) {
            try {
                val client = serverSocket.accept()
                client.tcpNoDelay = true
                thread(name = "socks-client", isDaemon = true) { handleSafely(client) }
            } catch (e: Exception) {
                if (running.get()) Log.w(TAG, "accept 异常", e)
            }
        }
    }

    private fun handleSafely(client: Socket) {
        try {
            handle(client)
        } catch (e: Exception) {
            Log.w(TAG, "客户端处理异常", e)
        } finally {
            closeQuietly(client)
        }
    }

    private fun handle(client: Socket) {
        client.soTimeout = 20_000
        val input = client.getInputStream()
        val output = client.getOutputStream()

        // --- 1. 版本与认证方式协商 ---
        val head = ByteArray(2)
        if (readFully(input, head) < 2) return
        if ((head[0].toInt() and 0xFF) != VER) return
        val nMethods = head[1].toInt() and 0xFF
        val methods = ByteArray(nMethods)
        if (readFully(input, methods) < nMethods) return

        val wantUserPass = auth != null
        val chosen = if (wantUserPass) {
            if (methods.contains(METHOD_USERPASS.toByte())) METHOD_USERPASS else METHOD_NONE
        } else {
            if (methods.contains(METHOD_NO_AUTH.toByte())) METHOD_NO_AUTH else METHOD_NONE
        }

        // --- 2. 回复选定的认证方式 ---
        output.write(byteArrayOf(VER.toByte(), chosen.toByte()))
        output.flush()
        if (chosen == METHOD_NONE) {
            onLog("SOCKS5 认证方式不匹配 <- ${client.inetAddress.hostAddress}")
            return
        }

        // --- 3. 用户名密码认证 ---
        if (chosen == METHOD_USERPASS) {
            val ver = ByteArray(1)
            if (readFully(input, ver) < 1) return
            if ((ver[0].toInt() and 0xFF) != 0x01) {
                output.write(byteArrayOf(0x01, 0x01)); output.flush(); return
            }
            val uLenB = ByteArray(1); if (readFully(input, uLenB) < 1) return
            val uLen = uLenB[0].toInt() and 0xFF
            val uBuf = ByteArray(uLen); if (readFully(input, uBuf) < uLen) return
            val pLenB = ByteArray(1); if (readFully(input, pLenB) < 1) return
            val pLen = pLenB[0].toInt() and 0xFF
            val pBuf = ByteArray(pLen); if (readFully(input, pBuf) < pLen) return

            val user = String(uBuf, Charsets.UTF_8)
            val pass = String(pBuf, Charsets.UTF_8)
            val ok = auth != null && Auth.checkSocks(user, pass, auth.first, auth.second)

            output.write(byteArrayOf(0x01, if (ok) 0x00 else 0x01))
            output.flush()
            if (!ok) {
                onLog("SOCKS5 认证失败 <- ${client.inetAddress.hostAddress}")
                return
            }
        }

        // --- 4. 读取请求 ---
        val req = ByteArray(4)
        if (readFully(input, req) < 4) return
        val cmd = req[1].toInt() and 0xFF
        val atyp = req[3].toInt() and 0xFF

        val host: String = when (atyp) {
            ATYP_IPV4 -> {
                val b = ByteArray(4); if (readFully(input, b) < 4) return
                b.joinToString(".") { (it.toInt() and 0xFF).toString() }
            }
            ATYP_DOMAIN -> {
                val lenB = ByteArray(1); if (readFully(input, lenB) < 1) return
                val len = lenB[0].toInt() and 0xFF
                val b = ByteArray(len); if (readFully(input, b) < len) return
                String(b, Charsets.UTF_8)
            }
            ATYP_IPV6 -> {
                val b = ByteArray(16); if (readFully(input, b) < 16) return
                java.net.InetAddress.getByAddress(b).hostAddress ?: return
            }
            else -> return
        }
        val portB = ByteArray(2); if (readFully(input, portB) < 2) return
        val targetPort = ((portB[0].toInt() and 0xFF) shl 8) or (portB[1].toInt() and 0xFF)

        if (cmd != CMD_CONNECT) {
            replyError(output, REP_CMD_UNSUPPORTED)
            onLog("SOCKS5 不支持的命令 $cmd")
            return
        }

        // --- 5. 建立出站连接 ---
        val remote = try {
            connector.connect(host, targetPort)
        } catch (e: Exception) {
            replyError(output, REP_HOST_UNREACH)
            onLog("SOCKS5 连接失败 $host:$targetPort : ${e.message}")
            return
        }

        remote.use { r ->
            // 成功响应，BND.ADDR 用 0.0.0.0
            output.write(byteArrayOf(
                VER.toByte(), REP_SUCCESS.toByte(), 0x00.toByte(),
                ATYP_IPV4.toByte(), 0, 0, 0, 0, 0, 0
            ))
            output.flush()

            connCount.incrementAndGet()
            client.soTimeout = 0
            onLog("SOCKS5 $host:$targetPort <- ${client.inetAddress.hostAddress}")

            TcpRelay.relay(ProxyProtocol.SOCKS5, client, r) { dir, n ->
                if (dir == TrafficDirection.CLIENT_TO_REMOTE) bytesUp.addAndGet(n)
                else bytesDown.addAndGet(n)
            }
        }
    }

    private fun replyError(output: java.io.OutputStream, code: Int) {
        try {
            output.write(byteArrayOf(
                VER.toByte(), code.toByte(), 0x00.toByte(),
                ATYP_IPV4.toByte(), 0, 0, 0, 0, 0, 0
            ))
            output.flush()
        } catch (_: Exception) {}
    }

    /** 尽量读满 buf */
    private fun readFully(input: java.io.InputStream, buf: ByteArray): Int {
        var off = 0
        while (off < buf.size) {
            val n = input.read(buf, off, buf.size - off)
            if (n == -1) break
            off += n
        }
        return off
    }

    private fun closeQuietly(socket: Socket) {
        try { socket.close() } catch (_: Exception) {}
    }

    @Synchronized
    override fun close() {
        if (running.compareAndSet(true, false)) {
            try { serverSocket.close() } catch (_: Exception) {}
            onLog("SOCKS5 代理已停止")
        }
    }
}

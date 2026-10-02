package com.vpnshare.app.proxy

import android.util.Log
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

/**
 * HTTP/HTTPS 代理服务器。
 *
 * 支持：
 *   - CONNECT 方法（HTTPS 隧道，443 等）
 *   - 普通 HTTP 请求转发（GET/POST ...）
 *   - 可选 Basic 代理认证
 *   - 内置 PAC 分发（GET /proxy.pac）
 *
 * 监听 0.0.0.0，局域网内其他设备可连接。
 */
class HttpProxyServer(
    private val bindHost: String,
    private val port: Int,
    private val connector: DirectSocketConnector,
    private val pacProvider: (() -> String)?,
    private val auth: Pair<String, String>?,   // user to pass，null 表示不鉴权
    private val onLog: (String) -> Unit,
) : AutoCloseable {

    companion object {
        private const val TAG = "HttpProxyServer"
        private const val MAX_HEADER = 64 * 1024
        private const val HEADER_READ_TIMEOUT_MS = 20_000
    }

    private val running = AtomicBoolean(false)
    private lateinit var serverSocket: ServerSocket

    private val bytesUp = AtomicLong(0)
    private val bytesDown = AtomicLong(0)
    private val connCount = AtomicLong(0)
    private val pacHits = AtomicLong(0)

    val boundPort: Int get() = if (::serverSocket.isInitialized) serverSocket.localPort else port
    fun stats() = ProxyStats(
        httpConnections = connCount.get(),
        pacRequests = pacHits.get(),
        bytesUp = bytesUp.get(),
        bytesDown = bytesDown.get(),
    )

    fun start() {
        check(running.compareAndSet(false, true)) { "HTTP 代理已启动" }
        serverSocket = ServerSocket().apply {
            reuseAddress = true
            bind(InetSocketAddress(bindHost, port))
        }
        onLog("HTTP 代理监听 $bindHost:${boundPort}")
        thread(name = "http-accept", isDaemon = true) { acceptLoop() }
    }

    private fun acceptLoop() {
        while (running.get()) {
            try {
                val client = serverSocket.accept()
                client.tcpNoDelay = true
                thread(name = "http-client", isDaemon = true) { handleSafely(client) }
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
            closeQuietly(client)
        }
    }

    private fun handle(client: Socket) {
        client.use { cs ->
            cs.soTimeout = HEADER_READ_TIMEOUT_MS
            val input = java.io.BufferedInputStream(cs.getInputStream(), 8192)
            val headerBytes = readHeader(input) ?: return
            if (headerBytes.isEmpty()) return

            val header = String(headerBytes, Charsets.ISO_8859_1)
            val lines = header.split("\r\n").filter { it.isNotEmpty() }
            val requestLine = lines.firstOrNull() ?: return

            val parts = requestLine.split(" ", limit = 3)
            if (parts.size < 2) {
                respond(cs, "HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                return
            }
            val method = parts[0]
            val target = parts[1]

            // ---- PAC 分发 ----
            if (method.equals("GET", true) && target.substringBefore("?").trimEnd('/').endsWith("proxy.pac")) {
                val script = pacProvider?.invoke()
                if (script != null) {
                    pacHits.incrementAndGet()
                    val body = script.toByteArray(Charsets.UTF_8)
                    val head = "HTTP/1.1 200 OK\r\n" +
                        "Content-Type: application/x-ns-proxy-autoconfig\r\n" +
                        "Cache-Control: no-store\r\n" +
                        "Connection: close\r\n" +
                        "Content-Length: ${body.size}\r\n\r\n"
                    cs.getOutputStream().apply {
                        write(head.toByteArray(Charsets.ISO_8859_1)); write(body); flush()
                    }
                    onLog("PAC 请求 <- ${cs.inetAddress.hostAddress}")
                    return
                }
            }

            // ---- 认证 ----
            val headers = parseHeaders(lines.drop(1))
            if (auth != null) {
                if (!Auth.checkBasic(headers["proxy-authorization"], auth.first, auth.second)) {
                    respond(
                        cs,
                        "HTTP/1.1 407 Proxy Authentication Required\r\n" +
                            "Proxy-Authenticate: Basic realm=\"VpnShare\"\r\n" +
                            "Connection: close\r\n\r\n"
                    )
                    onLog("认证失败 <- ${cs.inetAddress.hostAddress}")
                    return
                }
            }

            connCount.incrementAndGet()

            if (method.equals("CONNECT", true)) {
                handleConnect(cs, target)
            } else {
                handleForward(cs, input, method, target, lines, headers)
            }
        }
    }

    // ---------------- CONNECT 隧道 ----------------
    private fun handleConnect(client: Socket, target: String) {
        val (host, port) = HostPort.parse(target, 443)
        val remote = try {
            connector.connect(host, port)
        } catch (e: Exception) {
            respond(client, "HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            onLog("CONNECT 失败 $target : ${e.message}")
            return
        }
        remote.use { r ->
            client.soTimeout = 0
            client.getOutputStream().apply {
                write("HTTP/1.1 200 Connection Established\r\n\r\n".toByteArray(Charsets.ISO_8859_1))
                flush()
            }
            onLog("CONNECT $target <- ${client.inetAddress.hostAddress}")
            TcpRelay.relay(ProxyProtocol.HTTP, client, r) { dir, n ->
                if (dir == TrafficDirection.CLIENT_TO_REMOTE) bytesUp.addAndGet(n)
                else bytesDown.addAndGet(n)
            }
        }
    }

    // ---------------- 普通 HTTP 转发 ----------------
    private fun handleForward(
        client: Socket,
        input: java.io.BufferedInputStream,
        method: String,
        rawTarget: String,
        lines: List<String>,
        headers: Map<String, String>,
    ) {
        // 目标可能是完整 URL（http://host/path），也可能只有路径（配合 Host 头）
        val (host, port, path) = parseForwardTarget(rawTarget, lines)
        if (host == null) {
            respond(client, "HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            return
        }

        val remote = try {
            connector.connect(host, port)
        } catch (e: Exception) {
            respond(client, "HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            onLog("转发失败 $host:$port : ${e.message}")
            return
        }

        remote.use { r ->
            client.soTimeout = 0
            val version = lines[0].split(" ").getOrElse(2) { "HTTP/1.1" }

            // 重写请求行：absolute-form -> origin-form
            val rewritten = StringBuilder()
            rewritten.append("$method $path $version\r\n")
            for (line in lines.drop(1)) {
                val name = line.substringBefore(':').trim().lowercase()
                // 过滤 hop-by-hop 头，它们只对当前这一跳有意义
                if (name == "proxy-authorization" ||
                    name == "proxy-connection" ||
                    name == "connection") continue
                rewritten.append(line).append("\r\n")
            }
            rewritten.append("\r\n")

            val remoteOut = r.getOutputStream()
            remoteOut.write(rewritten.toString().toByteArray(Charsets.ISO_8859_1))
            remoteOut.flush()

            // ---- 转发请求 body ----
            // 支持 Content-Length 与 chunked 两种。
            // 注意：input 是 BufferedInputStream，之前多读的字节还在缓冲里，不会丢。
            val hasBody = headers.containsKey("content-length") ||
                (headers["transfer-encoding"]?.contains("chunked", true) == true)

            if (hasBody) {
                val contentLength = headers["content-length"]?.trim()?.toLongOrNull()
                if (contentLength != null && contentLength > 0) {
                    var remaining = contentLength
                    val buf = ByteArray(16 * 1024)
                    while (remaining > 0) {
                        val want = minOf(remaining, buf.size.toLong()).toInt()
                        val n = input.read(buf, 0, want)
                        if (n == -1) break
                        remoteOut.write(buf, 0, n)
                        remaining -= n
                        bytesUp.addAndGet(n.toLong())
                    }
                    remoteOut.flush()
                } else if (headers["transfer-encoding"]?.contains("chunked", true) == true) {
                    // chunked：按块解析转发
                    forwardChunkedBody(input, remoteOut)
                }
            }

            onLog("$method $host:$port$path <- ${client.inetAddress.hostAddress}")

            // ---- 转发响应（双向里只留 远端->客户端 方向需要我们自己读） ----
            TcpRelay.relay(ProxyProtocol.HTTP, client, r) { dir, n ->
                if (dir == TrafficDirection.CLIENT_TO_REMOTE) bytesUp.addAndGet(n)
                else bytesDown.addAndGet(n)
            }
        }
    }

    /** 逐块转发 chunked 编码的 body，读到终止块为止 */
    private fun forwardChunkedBody(
        input: java.io.BufferedInputStream,
        out: java.io.OutputStream,
    ) {
        try {
            while (true) {
                // 读 size 行
                val sizeLine = readLine(input) ?: return
                out.write((sizeLine + "\r\n").toByteArray(Charsets.ISO_8859_1))
                val size = sizeLine.trim().substringBefore(';').toIntOrNull(16) ?: return
                if (size == 0) {
                    // 读 trailer 直到空行
                    while (true) {
                        val t = readLine(input) ?: break
                        out.write((t + "\r\n").toByteArray(Charsets.ISO_8859_1))
                        if (t.isEmpty()) break
                    }
                    out.flush()
                    return
                }
                val buf = ByteArray(size)
                var off = 0
                while (off < size) {
                    val n = input.read(buf, off, size - off)
                    if (n == -1) return
                    off += n
                }
                out.write(buf)
                val crlf = ByteArray(2)
                if (input.read(crlf, 0, 2) < 2) return
                out.write(crlf)
                bytesUp.addAndGet((size + 2).toLong())
                out.flush()
            }
        } catch (_: Exception) {
        }
    }

    private fun readLine(input: java.io.InputStream): String? {
        val sb = StringBuilder()
        while (true) {
            val b = input.read()
            if (b == -1) return if (sb.isEmpty()) null else sb.toString()
            if (b == '\n'.code) return sb.toString().trimEnd('\r')
            sb.append(b.toChar())
        }
    }

    private data class ForwardTarget(val host: String?, val port: Int, val path: String)

    private fun parseForwardTarget(raw: String, lines: List<String>): ForwardTarget {
        if (raw.startsWith("http://", true) || raw.startsWith("https://", true)) {
            val noScheme = raw.substringAfter("://")
            val hostPort = noScheme.substringBefore('/')
            val path = noScheme.substringAfter('/', "/").let { "/$it" }
            val isHttps = raw.startsWith("https://", true)
            val (h, p) = HostPort.parse(hostPort, if (isHttps) 443 else 80)
            return ForwardTarget(h, p, path)
        }
        // 相对路径形式，靠 Host 头
        val hostHeader = lines.drop(1)
            .firstOrNull { it.substringBefore(':').trim().equals("Host", true) }
            ?.substringAfter(':')?.trim()
            ?: return ForwardTarget(null, 80, raw)
        val (h, p) = HostPort.parse(hostHeader, 80)
        return ForwardTarget(h, p, raw)
    }

    // ---------------- 工具 ----------------
    /** 读到 \r\n\r\n 为止，只返回请求头（不含 body） */
    private fun readHeader(input: InputStream): ByteArray? {
        val out = java.io.ByteArrayOutputStream(4096)
        var state = 0
        while (out.size() < MAX_HEADER) {
            val b = input.read()
            if (b == -1) return if (out.size() == 0) null else out.toByteArray()
            out.write(b)
            state = when {
                state == 0 && b == '\r'.code -> 1
                state == 1 && b == '\n'.code -> 2
                state == 2 && b == '\r'.code -> 3
                state == 3 && b == '\n'.code -> return out.toByteArray()
                else -> if (b == '\r'.code) 1 else 0
            }
        }
        return out.toByteArray()
    }

    private fun parseHeaders(lines: List<String>): Map<String, String> {
        val map = HashMap<String, String>()
        for (line in lines) {
            val idx = line.indexOf(':')
            if (idx > 0) {
                map[line.substring(0, idx).trim().lowercase()] = line.substring(idx + 1).trim()
            }
        }
        return map
    }

    private fun respond(client: Socket, text: String) {
        try {
            client.getOutputStream().apply {
                write(text.toByteArray(Charsets.ISO_8859_1)); flush()
            }
        } catch (_: Exception) {}
    }

    private fun closeQuietly(socket: Socket) {
        try { socket.close() } catch (_: Exception) {}
    }

    @Synchronized
    override fun close() {
        if (running.compareAndSet(true, false)) {
            try { serverSocket.close() } catch (_: Exception) {}
            onLog("HTTP 代理已停止")
        }
    }
}

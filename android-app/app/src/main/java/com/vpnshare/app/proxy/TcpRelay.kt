package com.vpnshare.app.proxy

import java.io.InputStream
import java.io.OutputStream
import java.net.Socket
import java.util.concurrent.CountDownLatch
import kotlin.concurrent.thread

/**
 * 双向数据搬运。
 *
 * 关键点：这里对 client 和 remote 两个 socket 做对拷，不关心数据内容。
 * remote socket 是通过 SocketConnector 建立的普通出站连接——
 * 在手机上，这类连接的流量会被系统 VPN（TUN 模式）自动接管，
 * 因此代理出去的流量天然走 VPN 隧道。这正是"共享 VPN"的原理。
 */
object TcpRelay {

    private const val BUFFER_SIZE = 32 * 1024

    fun relay(
        protocol: ProxyProtocol,
        client: Socket,
        remote: Socket,
        onBytes: ((TrafficDirection, Long) -> Unit)? = null,
    ) {
        val done = CountDownLatch(2)

        pump(protocol, TrafficDirection.CLIENT_TO_REMOTE,
            client.getInputStream(), remote.getOutputStream(), done, onBytes)

        pump(protocol, TrafficDirection.REMOTE_TO_CLIENT,
            remote.getInputStream(), client.getOutputStream(), done, onBytes)

        // 等两个方向都结束，连接才算彻底关闭
        done.await()
    }

    private fun pump(
        protocol: ProxyProtocol,
        direction: TrafficDirection,
        input: InputStream,
        output: OutputStream,
        done: CountDownLatch,
        onBytes: ((TrafficDirection, Long) -> Unit)?,
    ) {
        thread(name = "relay-$protocol-$direction") {
            try {
                val buffer = ByteArray(BUFFER_SIZE)
                while (true) {
                    val read = input.read(buffer)
                    if (read == -1) break
                    output.write(buffer, 0, read)
                    output.flush()
                    onBytes?.invoke(direction, read.toLong())
                }
            } catch (_: Exception) {
                // 对端关闭或超时，正常终止
            } finally {
                closeQuietly(output)
                done.countDown()
            }
        }
    }

    private fun closeQuietly(out: OutputStream) {
        try { out.close() } catch (_: Exception) {}
    }
}

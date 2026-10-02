package com.vpnshare.app.proxy

import java.util.concurrent.CopyOnWriteArrayList

/**
 * 流量速率采样器（v1.2 新增）
 * ===========================
 *
 * 职责：把 ProxyRuntime 里的**累计字节数**转成**每秒速率**，并保留一段历史，
 * 供 UI 画实时折线图。
 *
 * 为什么需要单独一层
 * ------------------
 * HTTP/SOCKS 服务器里已经用 AtomicLong 累计了 bytesUp / bytesDown，
 * 但那是**累计值**（只增不减）。折线图要的是"这一刻每秒多少字节"，
 * 所以必须有采样：每隔固定间隔读一次累计值，与上次相减、除以时间差。
 *
 * 采样间隔取 1 秒：
 *  - 太短（100ms）→ 曲线抖动剧烈，且手机端耗电、UI 刷新压力大
 *  - 太长（5s）   → 图表反应迟钝，看不出瞬时变化
 *  - 1 秒是流量曲线图的常规选择
 *
 * 历史窗口保留 [HISTORY] 个点（即 1 分钟的滑动窗口）。
 * 用 CopyOnWriteArrayList 是因为：采样线程写、UI 线程读，
 * 而写入频率只有 1Hz —— 正是 COW 最合适的场景（读多写极少）。
 */
object TrafficMonitor {

    /** 历史窗口长度：60 个采样点 = 60 秒 */
    const val HISTORY = 60

    /** 采样间隔（毫秒） */
    const val INTERVAL_MS = 1000L

    /** 一个采样点：每秒的上行/下行字节数 */
    data class Sample(val up: Double, val down: Double)

    private val history = CopyOnWriteArrayList<Sample>()

    /** 上一次采样的累计值快照 + 时间戳 */
    private var lastUp = 0L
    private var lastDown = 0L
    private var lastAt = 0L

    /** 本轮会话的峰值速率（字节/秒），画图时用来定 Y 轴量程 */
    @Volatile var peakUp = 0.0
        private set

    @Volatile var peakDown = 0.0
        private set

    /** 是否处于采集中（代理运行中） */
    @Volatile var active = false
        private set

    /** 供 UI 读取的历史快照（不可变副本，避免遍历时的并发问题） */
    fun snapshot(): List<Sample> = history.toList()

    /** 当前瞬时速率（最近一个采样点），UI 显示数字用 */
    fun current(): Sample = history.lastOrNull() ?: Sample(0.0, 0.0)

    /**
     * 开始新一轮采集。
     *
     * 会清空历史与峰值 —— 因为上一轮（上次启动代理）的流量曲线
     * 和这一轮没有可比性，混在一起看会误导。
     *
     * 注意要重置 lastUp/lastDown 为**当前累计值**，
     * 否则第一秒会把"历史累计总量"当成"1 秒内产生的量"，画出一个虚假的巨峰。
     */
    fun reset() {
        val st = ProxyRuntime.get().stats()
        lastUp = st.bytesUp
        lastDown = st.bytesDown
        lastAt = System.currentTimeMillis()
        history.clear()
        peakUp = 0.0
        peakDown = 0.0
        active = true
    }

    /** 停止采集（保留历史，让用户还能看到停止前的曲线） */
    fun stop() {
        active = false
    }

    /**
     * 采一次。由 UI 层按 INTERVAL_MS 定时驱动，
     * 而不是自己起线程 —— UI 里本来就有刷新循环，
     * 复用它可以少一个线程，也避免 App 退到后台后仍在耗电采样。
     */
    fun tick() {
        if (!active) return

        val st = ProxyRuntime.get().stats()
        val now = System.currentTimeMillis()
        val dtMs = now - lastAt
        if (dtMs <= 0) return

        // 增量可能为负（理论上不会，除非统计被重置），兜底为 0
        val dUp = (st.bytesUp - lastUp).coerceAtLeast(0L)
        val dDown = (st.bytesDown - lastDown).coerceAtLeast(0L)

        val sec = dtMs / 1000.0
        val upRate = dUp / sec
        val downRate = dDown / sec

        lastUp = st.bytesUp
        lastDown = st.bytesDown
        lastAt = now

        history.add(Sample(upRate, downRate))
        while (history.size > HISTORY) history.removeAt(0)

        if (upRate > peakUp) peakUp = upRate
        if (downRate > peakDown) peakDown = downRate
    }

    /** 人类可读的速率文本，如 "1.2 MB/s" */
    fun fmtRate(bytesPerSec: Double): String {
        val b = bytesPerSec
        return when {
            b < 1024 -> String.format("%.0f B/s", b)
            b < 1024 * 1024 -> String.format("%.1f KB/s", b / 1024)
            b < 1024.0 * 1024 * 1024 -> String.format("%.2f MB/s", b / 1024 / 1024)
            else -> String.format("%.2f GB/s", b / 1024 / 1024 / 1024)
        }
    }
}

package com.vpnshare.app.view

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Shader
import android.util.AttributeSet
import android.view.View
import com.vpnshare.app.proxy.TrafficMonitor

/**
 * 实时流量折线图（v1.2 新增）
 * ===========================
 *
 * 纯 Canvas 自绘，**不依赖任何第三方图表库**。理由：
 *  - 手机端只需要"两条随时间变化的曲线"，引 MPAndroidChart 会让 APK 多几百 KB
 *  - 自绘完全可控（配色、留白、空状态都能贴合 App 现有视觉）
 *
 * 视觉设计
 * --------
 *  - 下载线：绿色，曲线下方带渐变填充（面积图效果，量感更直观）
 *  - 上传线：蓝色，只画线不加填充（避免两条填充互相遮挡）
 *  - 背景：横向网格线 + Y 轴刻度（自动选"好看"的量级）
 *  - 左上角：当前速率数值（↑/↓ 分别标注）
 *
 * Y 轴量程策略
 * ------------
 * 取窗口内峰值向上取整到"好看"的量级（100KB / 500KB / 1MB / 5MB ...）。
 * 关键细节：**量程下限设为 64KB/s**，否则没有流量时（全 0）
 * 会得到一个 0 高度的刻度，Y 轴标签全是 "0B"，看起来像坏了。
 * 缓慢回落（见 yMax 的衰减逻辑）避免曲线因为一次尖峰被长期压扁。
 */
class SparklineView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0,
) : View(context, attrs, defStyleAttr) {

    private val density = resources.displayMetrics.density

    // ---------------- 画笔 ----------------

    private val gridPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#E8EBEF")
        strokeWidth = dp(1f)
        style = Paint.Style.STROKE
    }

    private val axisTextPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#9AA1AB")
        textSize = dp(9f)
    }

    private val legendPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = dp(10f)
        isFakeBoldText = true
    }

    private val downLinePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#0AA869")   // 绿色 = 下载
        strokeWidth = dp(2f)
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }

    private val upLinePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#2B6CD4")   // 蓝色 = 上传
        strokeWidth = dp(2f)
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }

    /** 下载曲线下方的渐变填充 */
    private var downFillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val emptyTextPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#B8BEC6")
        textSize = dp(11f)
        textAlign = Paint.Align.CENTER
    }

    // ---------------- 内部状态 ----------------

    private var samples: List<TrafficMonitor.Sample> = emptyList()

    /** 当前 Y 轴上限（字节/秒）。用平滑回落，避免尖峰后曲线被长期压扁 */
    private var yMax = MIN_Y_MAX

    companion object {
        /** Y 轴下限：64 KB/s。没流量时也保证刻度可读 */
        private const val MIN_Y_MAX = 64.0 * 1024
        /** 平滑回落系数：新峰值低于当前量程太多时，按这个比例缩 */
        private const val DECAY = 0.85
        /** 触发回落的阈值：峰值 < 当前量程的 1/3 才开始缩 */
        private const val DECAY_TRIGGER = 3.0
    }

    /**
     * 由 MainActivity 定时投喂数据（UI 线程调用）。
     *
     * @param list 历史采样点（最新在末尾）
     */
    fun setData(list: List<TrafficMonitor.Sample>) {
        samples = list
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)

        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0 || h <= 0) return

        // 内边距：左侧留给 Y 轴刻度，其余留白
        val padLeft = dp(40f)
        val padRight = dp(6f)
        val padTop = dp(22f)      // 留给顶部图例
        val padBottom = dp(6f)

        val chartW = w - padLeft - padRight
        val chartH = h - padTop - padBottom
        if (chartW <= 0 || chartH <= 0) return

        drawLegend(canvas, padLeft, padTop)

        if (samples.size < 2) {
            drawEmpty(canvas, padLeft + chartW / 2f, padTop + chartH / 2f)
            updateYMax(null)
            return
        }

        updateYMax(samples)

        // ---- 网格 + Y 轴刻度（4 等分）----
        val gridCount = 4
        for (i in 0..gridCount) {
            val y = padTop + chartH * i / gridCount
            canvas.drawLine(padLeft, y, padLeft + chartW, y, gridPaint)

            // 刻度值：顶部最大，底部 0
            val value = yMax * (gridCount - i) / gridCount
            val label = fmtAxis(value)
            val textW = axisTextPaint.measureText(label)
            canvas.drawText(label, padLeft - dp(6f) - textW, y + dp(3.5f), axisTextPaint)
        }

        // ---- 曲线 ----
        // X 轴固定铺满 60 个点宽（而不是只铺 samples.size），
        // 这样窗口未满时曲线从左边逐渐长出，视觉上更符合"实时流入"的直觉。
        val total = TrafficMonitor.HISTORY
        val stepX = chartW / (total - 1).toFloat()

        // 数据对齐到右端（最新的在右侧）
        val offset = total - samples.size

        val upPath = Path()
        val downPath = Path()
        val downFill = Path()

        samples.forEachIndexed { i, s ->
            val x = padLeft + (offset + i) * stepX
            val yUp = valueToY(s.up, padTop, chartH)
            val yDown = valueToY(s.down, padTop, chartH)

            if (i == 0) {
                upPath.moveTo(x, yUp)
                downPath.moveTo(x, yDown)
                downFill.moveTo(x, padTop + chartH)
                downFill.lineTo(x, yDown)
            } else {
                upPath.lineTo(x, yUp)
                downPath.lineTo(x, yDown)
                downFill.lineTo(x, yDown)
            }
        }

        val lastX = padLeft + (offset + samples.size - 1) * stepX
        downFill.lineTo(lastX, padTop + chartH)
        downFill.close()

        // 下载面积填充（渐变：绿色 → 透明）
        downFillPaint.shader = LinearGradient(
            0f, padTop, 0f, padTop + chartH,
            Color.parseColor("#330AA869"), Color.parseColor("#000AA869"),
            Shader.TileMode.CLAMP,
        )
        canvas.drawPath(downFill, downFillPaint)

        canvas.drawPath(downPath, downLinePaint)
        canvas.drawPath(upPath, upLinePaint)
    }

    /** 顶部图例：当前速率 + 峰值 */
    private fun drawLegend(canvas: Canvas, padLeft: Float, padTop: Float) {
        val cur = samples.lastOrNull() ?: TrafficMonitor.Sample(0.0, 0.0)
        val y = padTop - dp(8f)

        var x = padLeft
        // 下载
        legendPaint.color = Color.parseColor("#0AA869")
        canvas.drawText("↓ ${TrafficMonitor.fmtRate(cur.down)}", x, y, legendPaint)
        x += legendPaint.measureText("↓ ${TrafficMonitor.fmtRate(cur.down)}") + dp(12f)

        // 上传
        legendPaint.color = Color.parseColor("#2B6CD4")
        canvas.drawText("↑ ${TrafficMonitor.fmtRate(cur.up)}", x, y, legendPaint)
        x += legendPaint.measureText("↑ ${TrafficMonitor.fmtRate(cur.up)}") + dp(14f)

        // 峰值（灰色小字）
        legendPaint.color = Color.parseColor("#9AA1AB")
        legendPaint.isFakeBoldText = false
        val peak = "峰值 ↓${TrafficMonitor.fmtRate(TrafficMonitor.peakDown)}" +
                " ↑${TrafficMonitor.fmtRate(TrafficMonitor.peakUp)}"
        legendPaint.textSize = dp(9f)
        canvas.drawText(peak, x, y - dp(0.5f), legendPaint)
    }

    private fun drawEmpty(canvas: Canvas, cx: Float, cy: Float) {
        val msg = if (TrafficMonitor.active) "等待流量 ..." else "启动共享后显示实时流量"
        canvas.drawText(msg, cx, cy, emptyTextPaint)
    }

    /**
     * 更新 Y 轴量程。
     *
     * 规则：
     *  - 出现更高峰值 → 立刻抬升（保证曲线不出界）
     *  - 峰值持续远低于当前量程（< 1/3）→ 缓慢回落，避免曲线被压成一条直线
     */
    private fun updateYMax(list: List<TrafficMonitor.Sample>?) {
        val peak = list?.maxOfOrNull { maxOf(it.up, it.down) } ?: 0.0
        val nice = niceCeil(peak)
        yMax = when {
            nice > yMax -> nice                       // 抬升：立即
            nice < yMax / DECAY_TRIGGER ->            // 回落：平滑过渡
                (yMax * DECAY).coerceAtLeast(nice).coerceAtLeast(MIN_Y_MAX)
            else -> yMax
        }
    }

    /** 把任意值向上取整到"好看"的量级（1/2/5 × 10^n） */
    private fun niceCeil(v: Double): Double {
        if (v <= MIN_Y_MAX) return MIN_Y_MAX
        var unit = 1.0
        while (unit * 10 < v) unit *= 10
        val n = v / unit
        val m = when {
            n <= 1 -> 1.0
            n <= 2 -> 2.0
            n <= 5 -> 5.0
            else -> 10.0
        }
        return (m * unit).coerceAtLeast(MIN_Y_MAX)
    }

    /** Y 轴刻度文本：用更短的单位（K / M），避免标签过长挤占绘图区 */
    private fun fmtAxis(b: Double): String = when {
        b <= 0 -> "0"
        b < 1024 -> "${b.toInt()}B"
        b < 1024 * 1024 -> "${(b / 1024).toInt()}K"
        else -> String.format("%.0fM", b / 1024 / 1024)
    }

    private fun valueToY(v: Double, padTop: Float, chartH: Float): Float {
        val ratio = (v / yMax).coerceIn(0.0, 1.0)
        return padTop + chartH * (1f - ratio).toFloat()
    }

    private fun dp(v: Float): Float = v * density
}

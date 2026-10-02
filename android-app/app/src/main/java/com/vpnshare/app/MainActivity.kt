package com.vpnshare.app

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.vpnshare.app.databinding.ActivityMainBinding
import com.vpnshare.app.proxy.ProxyRuntime
import com.vpnshare.app.proxy.TrafficMonitor
import com.vpnshare.app.util.NetUtils

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val runtime get() = ProxyRuntime.get()

    /** 流量图表刷新循环的句柄，用于 onPause 时取消 */
    private var trafficTick: Runnable? = null

    private val notifPermLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        askNotificationPermission()
        refreshUi()
        bindActions()
    }

    override fun onResume() {
        super.onResume()
        refreshUi()
        startTrafficLoop()
    }

    override fun onPause() {
        super.onPause()
        // 界面不可见时停掉刷新：代理服务在前台 Service 里照常跑，
        // 但没必要为了一个看不见的图表持续唤醒 UI 线程。
        stopTrafficLoop()
    }

    /**
     * 流量图表刷新循环（每秒一次）。
     *
     * 用 `postDelayed` 自驱动而不是 `Timer`/协程：
     * 它天然绑定在 UI 线程 + View 生命周期上，
     * Activity 销毁后 post 的 Runnable 会随 View 一起被丢弃，不用额外取消逻辑。
     */
    private fun startTrafficLoop() {
        stopTrafficLoop()
        val tick = object : Runnable {
            override fun run() {
                TrafficMonitor.tick()
                binding.sparkline.setData(TrafficMonitor.snapshot())
                binding.tvStats.text = statsText()
                binding.root.postDelayed(this, TrafficMonitor.INTERVAL_MS)
            }
        }
        trafficTick = tick
        binding.root.postDelayed(tick, TrafficMonitor.INTERVAL_MS)
    }

    private fun stopTrafficLoop() {
        trafficTick?.let { binding.root.removeCallbacks(it) }
        trafficTick = null
    }

    private fun askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                notifPermLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
    }

    private fun bindActions() {
        binding.btnToggle.setOnClickListener {
            if (runtime.isRunning) stopProxy() else startProxy()
        }

        binding.tvHttpValue.setOnClickListener { copy(binding.tvHttpValue.text.toString()) }
        binding.tvSocksValue.setOnClickListener { copy(binding.tvSocksValue.text.toString()) }
        binding.tvPacValue.setOnClickListener { copy(binding.tvPacValue.text.toString()) }

        binding.btnCopyPac.setOnClickListener {
            val pac = runtime.previewPac()
            if (pac.isBlank()) {
                Toast.makeText(this, "请先启动代理", Toast.LENGTH_SHORT).show()
            } else {
                copy(pac)
                Toast.makeText(this, "PAC 脚本已复制", Toast.LENGTH_SHORT).show()
            }
        }

        binding.btnRefreshIp.setOnClickListener {
            refreshUi()
            Toast.makeText(this, "已刷新", Toast.LENGTH_SHORT).show()
        }
    }

    private fun startProxy() {
        val ip = NetUtils.getLanIp()
        if (ip.isEmpty()) {
            Toast.makeText(this, "未获取到局域网 IP，请先连接 WiFi/热点", Toast.LENGTH_LONG).show()
            return
        }

        val cfg = ProxyRuntime.Config(
            httpEnabled = true,
            httpPort = binding.etHttpPort.text.toString().toIntOrNull() ?: 8080,
            socksEnabled = binding.swSocks.isChecked,
            socksPort = binding.etSocksPort.text.toString().toIntOrNull() ?: 1080,
            lanIp = ip,
            user = binding.etUser.text.toString().trim(),
            pass = binding.etPass.text.toString(),
            pacEnabled = binding.swPac.isChecked,
        )

        try {
            startForegroundService(ProxyForegroundService.startIntent(this, cfg))
            binding.root.postDelayed({
                refreshUi()
                // 启动后立刻开一轮采集，并让图表从空状态切到"等待流量"
                startTrafficLoop()
            }, 600)
        } catch (e: Exception) {
            Toast.makeText(this, "启动失败：${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun stopProxy() {
        startService(ProxyForegroundService.stopIntent(this))
        binding.root.postDelayed({ refreshUi() }, 400)
    }

    private fun refreshUi() {
        val ip = NetUtils.getLanIp()
        val cfg = runtime.currentConfig
        val running = runtime.isRunning

        binding.tvStatus.text = if (running) "运行中" else "已停止"
        binding.tvStatus.setTextColor(
            ContextCompat.getColor(this, if (running) R.color.ok_green else R.color.muted)
        )
        binding.btnToggle.text = if (running) "停止共享" else "启动共享"
        binding.tvLanIp.text = NetUtils.describe(ip)

        val httpPort = cfg?.httpPort ?: (binding.etHttpPort.text.toString().toIntOrNull() ?: 8080)
        val socksPort = cfg?.socksPort ?: (binding.etSocksPort.text.toString().toIntOrNull() ?: 1080)

        binding.tvHttpValue.text = if (ip.isEmpty()) "-" else "$ip:$httpPort"
        binding.tvSocksValue.text = if (ip.isEmpty()) "-" else "$ip:$socksPort"
        binding.tvPacValue.text = if (ip.isEmpty()) "-" else "http://$ip:$httpPort/proxy.pac"

        binding.etHttpPort.isEnabled = !running
        binding.etSocksPort.isEnabled = !running
        binding.swSocks.isEnabled = !running
        binding.swPac.isEnabled = !running
        binding.etUser.isEnabled = !running
        binding.etPass.isEnabled = !running

        binding.tvStats.text = statsText()
        binding.sparkline.setData(TrafficMonitor.snapshot())
    }

    /** 统计文本：累计连接数 / PAC 请求 / 累计流量 */
    private fun statsText(): String {
        val st = runtime.stats()
        return "连接 ${st.httpConnections}    PAC ${st.pacRequests}    " +
            "累计 ↑${fmt(st.bytesUp)} ↓${fmt(st.bytesDown)}"
    }

    private fun fmt(b: Long): String = when {
        b < 1024 -> "${b}B"
        b < 1024 * 1024 -> String.format("%.1fK", b / 1024.0)
        b < 1024L * 1024 * 1024 -> String.format("%.1fM", b / 1024.0 / 1024)
        else -> String.format("%.2fG", b / 1024.0 / 1024 / 1024)
    }

    private fun copy(text: String) {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("vpnshare", text))
        Toast.makeText(this, "已复制：$text", Toast.LENGTH_SHORT).show()
    }
}

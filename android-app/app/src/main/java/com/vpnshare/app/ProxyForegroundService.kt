package com.vpnshare.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.vpnshare.app.proxy.ProxyRuntime

/**
 * 前台服务：保证代理在后台存活，并在通知栏常驻状态。
 */
class ProxyForegroundService : Service() {

    companion object {
        const val ACTION_START = "com.vpnshare.app.START"
        const val ACTION_STOP = "com.vpnshare.app.STOP"
        private const val CHANNEL_ID = "vpnshare_proxy"
        private const val NOTIF_ID = 1001

        const val EXTRA_HTTP = "http_enabled"
        const val EXTRA_HTTP_PORT = "http_port"
        const val EXTRA_SOCKS = "socks_enabled"
        const val EXTRA_SOCKS_PORT = "socks_port"
        const val EXTRA_USER = "user"
        const val EXTRA_PASS = "pass"
        const val EXTRA_PAC = "pac_enabled"

        fun startIntent(ctx: Context, cfg: ProxyRuntime.Config): Intent {
            return Intent(ctx, ProxyForegroundService::class.java).apply {
                action = ACTION_START
                putExtra(EXTRA_HTTP, cfg.httpEnabled)
                putExtra(EXTRA_HTTP_PORT, cfg.httpPort)
                putExtra(EXTRA_SOCKS, cfg.socksEnabled)
                putExtra(EXTRA_SOCKS_PORT, cfg.socksPort)
                putExtra(EXTRA_USER, cfg.user)
                putExtra(EXTRA_PASS, cfg.pass)
                putExtra(EXTRA_PAC, cfg.pacEnabled)
            }
        }

        fun stopIntent(ctx: Context): Intent =
            Intent(ctx, ProxyForegroundService::class.java).apply { action = ACTION_STOP }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                ProxyRuntime.get().stop()
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            else -> {
                val cfg = ProxyRuntime.Config(
                    httpEnabled = intent?.getBooleanExtra(EXTRA_HTTP, true) ?: true,
                    httpPort = intent?.getIntExtra(EXTRA_HTTP_PORT, 8080) ?: 8080,
                    socksEnabled = intent?.getBooleanExtra(EXTRA_SOCKS, true) ?: true,
                    socksPort = intent?.getIntExtra(EXTRA_SOCKS_PORT, 1080) ?: 1080,
                    lanIp = com.vpnshare.app.util.NetUtils.getLanIp(),
                    user = intent?.getStringExtra(EXTRA_USER) ?: "",
                    pass = intent?.getStringExtra(EXTRA_PASS) ?: "",
                    pacEnabled = intent?.getBooleanExtra(EXTRA_PAC, true) ?: true,
                )
                try {
                    ProxyRuntime.get().start(cfg)
                    startForegroundCompat(cfg)
                } catch (e: Exception) {
                    stopSelf()
                }
            }
        }
        return START_STICKY
    }

    private fun startForegroundCompat(cfg: ProxyRuntime.Config) {
        createChannel()

        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val text = buildString {
            append("HTTP ${cfg.lanIp}:${cfg.httpPort}")
            if (cfg.socksEnabled) append("  |  SOCKS5 :${cfg.socksPort}")
        }

        val notification: Notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("VpnShare 代理运行中")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setOngoing(true)
            .setContentIntent(open)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIF_ID, notification,
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIF_ID, notification)
        }
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val mgr = getSystemService(NotificationManager::class.java)
            if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
                val ch = NotificationChannel(
                    CHANNEL_ID, "代理服务", NotificationManager.IMPORTANCE_LOW
                ).apply { description = "VpnShare 代理运行状态" }
                mgr.createNotificationChannel(ch)
            }
        }
    }

    override fun onDestroy() {
        ProxyRuntime.get().stop()
        super.onDestroy()
    }
}

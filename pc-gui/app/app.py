# -*- coding: utf-8 -*-
"""
VPN Share - 图形界面（Tkinter）

单窗口，三态：
  ● 未启用（灰）—— 系统代理是直连
  ● 已启用（绿）—— 系统代理已指向手机，正在共享
  ● 工作中（黄）—— 正在探测 / 设置

关窗即自动还原（可关闭该行为）。
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import sys
import os
import traceback

# frozen(onefile) 时 __file__ 指向 _MEIxxxx 临时目录，往里插 sys.path 没意义，
# 还可能干扰 PyInstaller 的模块查找。仅在源码运行时插入。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import proxy_core as core
from local_relay import LocalRelay
from traffic_chart import TrafficChart, fmt_rate


def _log_dir() -> str:
    """日志目录：优先 %LOCALAPPDATA%\\VpnShare（一定可写），
    失败则退回 exe / 脚本所在目录。"""
    cand = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "VpnShare"
    )
    try:
        os.makedirs(cand, exist_ok=True)
        return cand
    except Exception:
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))


LOG_DIR = _log_dir()


def _boot_log(msg: str):
    """启动期打点，用于排查 --windowed 下静默退出。

    写入 %LOCALAPPDATA%\\VpnShare\\boot.log（不受 exe 所在目录权限影响）。
    """
    try:
        with open(os.path.join(LOG_DIR, "boot.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass

APP_TITLE = "VPN Share — 手机代理共享给电脑"
APP_VER = "v3.4"

# 配色
C_BG = "#f5f6f8"
C_CARD = "#ffffff"
C_TEXT = "#1f2329"
C_SUB = "#8a9099"
C_GREEN = "#0aa869"
C_GRAY = "#b8bec6"
C_YELLOW = "#f0a020"
C_RED = "#d9534f"
C_BLUE = "#2b6cd4"
C_BORDER = "#e3e6ea"


class VpnShareApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_TITLE}  {APP_VER}")
        self.root.geometry("620x700")
        self.root.minsize(560, 620)
        self.root.configure(bg=C_BG)

        self.state = "idle"          # idle | busy | active
        self.target: core.ProxyTarget | None = None
        self.proxy_target: core.ProxyTarget | None = None
        self.relay: LocalRelay | None = None      # 本机中转（快速失败）
        self.cancel = threading.Event()
        self.msgq: queue.Queue[tuple[str, str]] = queue.Queue()
        self.restore_on_exit = tk.BooleanVar(value=True)

        # 流量采样状态：上一次的累计字节与时间戳
        self._tr_last_up = 0
        self._tr_last_down = 0
        self._tr_last_at = 0.0
        self._tr_job = None

        self._build_ui()
        _boot_log("_build_ui 完成")
        self._pump()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 启动时如果发现已有备份且代理正指向某处，认为可能是上次没还原干净
        self._detect_initial_state()
        _boot_log("_detect_initial_state 完成")

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # ===== 顶部状态卡 =====
        top = tk.Frame(self.root, bg=C_CARD, highlightbackground=C_BORDER,
                       highlightthickness=1)
        top.pack(fill="x", padx=14, pady=(14, 8))

        head = tk.Frame(top, bg=C_CARD)
        head.pack(fill="x", padx=16, pady=(14, 4))

        self.lamp = tk.Canvas(head, width=16, height=16, bg=C_CARD,
                              highlightthickness=0)
        self.lamp.pack(side="left", pady=2)
        self.lamp_id = self.lamp.create_oval(2, 2, 14, 14, fill=C_GRAY, outline="")

        self.lbl_state = tk.Label(head, text="未启用", font=("Microsoft YaHei UI", 13, "bold"),
                                  bg=C_CARD, fg=C_TEXT)
        self.lbl_state.pack(side="left", padx=(9, 0))

        self.lbl_target = tk.Label(top, text="系统代理：直连（未使用代理）",
                                   font=("Microsoft YaHei UI", 9), bg=C_CARD, fg=C_SUB)
        self.lbl_target.pack(anchor="w", padx=16, pady=(0, 14))

        # ===== 主按钮 =====
        btnbar = tk.Frame(self.root, bg=C_BG)
        btnbar.pack(fill="x", padx=14, pady=(0, 8))

        self.btn_main = tk.Button(
            btnbar, text="一键启动共享", font=("Microsoft YaHei UI", 12, "bold"),
            bg=C_BLUE, fg="white", activebackground="#1f57ad", activeforeground="white",
            relief="flat", cursor="hand2", height=2, command=self.on_main,
        )
        self.btn_main.pack(fill="x")

        sub = tk.Frame(self.root, bg=C_BG)
        sub.pack(fill="x", padx=14, pady=(0, 8))

        self.btn_scan = self._flat_btn(sub, "仅探测", self.on_scan)
        self.btn_scan.pack(side="left")
        self.btn_restore = self._flat_btn(sub, "还原系统代理", self.on_restore)
        self.btn_restore.pack(side="left", padx=(6, 0))
        self.btn_copy = self._flat_btn(sub, "复制代理地址", self.on_copy)
        self.btn_copy.pack(side="left", padx=(6, 0))

        # 修复按钮单独一行，橙色醒目 —— 上不了网时一眼能找到
        fixbar = tk.Frame(self.root, bg=C_BG)
        fixbar.pack(fill="x", padx=14, pady=(0, 8))
        self.btn_repair = tk.Button(
            fixbar, text="上网异常？点这里修复（强制恢复直连）",
            font=("Microsoft YaHei UI", 9), command=self.on_repair,
            bg="#fff4e5", fg="#b26a00", activebackground="#ffe9cc",
            relief="flat", cursor="hand2", padx=12, pady=6, bd=0,
        )
        self.btn_repair.pack(fill="x")

        # ===== 手动指定 =====
        manual = tk.Frame(self.root, bg=C_CARD, highlightbackground=C_BORDER,
                          highlightthickness=1)
        manual.pack(fill="x", padx=14, pady=(0, 8))
        inner = tk.Frame(manual, bg=C_CARD)
        inner.pack(fill="x", padx=16, pady=10)

        tk.Label(inner, text="手动指定：", font=("Microsoft YaHei UI", 9),
                 bg=C_CARD, fg=C_TEXT).pack(side="left")

        self.var_host = tk.StringVar()
        self.var_port = tk.StringVar()
        e1 = tk.Entry(inner, textvariable=self.var_host, width=16,
                      font=("Consolas", 10), relief="solid", bd=1)
        e1.pack(side="left", padx=(2, 0))
        tk.Label(inner, text=":", bg=C_CARD, fg=C_TEXT).pack(side="left")
        e2 = tk.Entry(inner, textvariable=self.var_port, width=7,
                      font=("Consolas", 10), relief="solid", bd=1)
        e2.pack(side="left")
        tk.Label(inner, text="（如 192.168.43.1 : 8080）", font=("Microsoft YaHei UI", 8),
                 bg=C_CARD, fg=C_SUB).pack(side="left", padx=(8, 0))

        # ===== 实时流量图 =====
        chartcard = tk.Frame(self.root, bg=C_CARD, highlightbackground=C_BORDER,
                             highlightthickness=1)
        chartcard.pack(fill="x", padx=14, pady=(0, 8))

        charthead = tk.Frame(chartcard, bg=C_CARD)
        charthead.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(charthead, text="实时流量", font=("Microsoft YaHei UI", 9, "bold"),
                 bg=C_CARD, fg=C_SUB).pack(side="left")

        self.lbl_traffic = tk.Label(charthead, text="未启动", font=("Consolas", 9),
                                    bg=C_CARD, fg=C_SUB)
        self.lbl_traffic.pack(side="right")

        self.chart = TrafficChart(chartcard, width=580, height=140)
        self.chart.pack(fill="x", padx=10, pady=(4, 10))

        # ===== 日志 =====
        logcard = tk.Frame(self.root, bg=C_CARD, highlightbackground=C_BORDER,
                           highlightthickness=1)
        logcard.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        tk.Label(logcard, text="日志", font=("Microsoft YaHei UI", 9, "bold"),
                 bg=C_CARD, fg=C_SUB).pack(anchor="w", padx=16, pady=(10, 2))

        self.txt = tk.Text(logcard, height=7, font=("Consolas", 9),
                           bg="#fbfbfc", fg=C_TEXT, relief="flat",
                           wrap="word", padx=10, pady=6, state="disabled")
        self.txt.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        # ===== 底部 =====
        bottom = tk.Frame(self.root, bg=C_BG)
        bottom.pack(fill="x", padx=14, pady=(0, 12))

        tk.Checkbutton(
            bottom, text="关闭窗口时自动还原系统代理",
            variable=self.restore_on_exit, font=("Microsoft YaHei UI", 9),
            bg=C_BG, fg=C_TEXT, activebackground=C_BG, selectcolor=C_CARD,
            highlightthickness=0, bd=0,
        ).pack(side="left")

        self.lbl_admin = tk.Label(bottom, text="", font=("Microsoft YaHei UI", 8),
                                  bg=C_BG, fg=C_SUB)
        self.lbl_admin.pack(side="right")

        if core.is_admin():
            self.lbl_admin.config(text="管理员", fg=C_GREEN)
        else:
            # 普通权限下改 HKCU 代理是够的，所以不强制提权；
            # 提供按钮给确实需要提权的场景（如企业策略锁了代理）。
            self.lbl_admin.config(text="普通权限", fg=C_SUB)
            tk.Button(
                bottom, text="以管理员重启", font=("Microsoft YaHei UI", 8),
                bg=C_BG, fg=C_BLUE, activebackground=C_BG, relief="flat",
                cursor="hand2", bd=0, command=self.on_elevate,
            ).pack(side="right", padx=(0, 8))

    def _flat_btn(self, parent, text, cmd) -> tk.Button:
        return tk.Button(
            parent, text=text, font=("Microsoft YaHei UI", 9), command=cmd,
            bg="#eef0f3", fg=C_TEXT, activebackground="#e2e6ea",
            relief="flat", cursor="hand2", padx=12, pady=6, bd=0,
        )

    # ------------------------------------------------------------------
    # 状态与日志
    # ------------------------------------------------------------------
    def _set_state(self, state: str, text: str, color: str, target_text: str = ""):
        self.state = state
        self.lamp.itemconfig(self.lamp_id, fill=color)
        self.lbl_state.config(text=text, fg=color)
        if target_text:
            self.lbl_target.config(text=target_text)

        if state == "busy":
            self.btn_main.config(text="处理中 ...", state="disabled", bg=C_GRAY)
        elif state == "active":
            self.btn_main.config(text="停止共享（还原代理）", state="normal", bg=C_RED)
        else:
            self.btn_main.config(text="一键启动共享", state="normal", bg=C_BLUE)

    def log(self, msg: str):
        self.msgq.put(("log", msg))

    def _pump(self):
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == "log":
                    self.txt.config(state="normal")
                    self.txt.insert("end", payload + "\n")
                    self.txt.see("end")
                    self.txt.config(state="disabled")
                elif kind == "state":
                    self._set_state(*payload)
                elif kind == "msgbox":
                    messagebox.showinfo(APP_TITLE, payload)
                elif kind == "alert":
                    messagebox.showwarning(
                        APP_TITLE,
                        "代理链路不可用，已阻止修改系统代理。\n"
                        "（浏览器仍可正常上网）\n\n" + payload,
                    )
        except queue.Empty:
            pass
        except Exception as e:
            _boot_log(f"_pump 异常: {e!r}\n{traceback.format_exc()}")
        finally:
            try:
                self._pump_id = self.root.after(120, self._pump)
            except Exception as e:
                _boot_log(f"after() 调度失败: {e!r}")

    # ------------------------------------------------------------------
    # 实时流量采样
    # ------------------------------------------------------------------
    def _start_traffic(self):
        """开始流量采样循环（启动共享成功后调用）。"""
        self._stop_traffic()
        if self.relay is None:
            self.lbl_traffic.config(text="未使用本机中转", fg=C_SUB)
            return
        # 基线取当前累计值，避免把"本轮之前的总量"当成 1 秒增量
        self._tr_last_up, self._tr_last_down = self.relay.traffic()
        self._tr_last_at = time.time()
        self.chart.reset("等待流量 ...")
        self.lbl_traffic.config(text="采样中", fg=C_GREEN)
        self._tick_traffic()

    def _stop_traffic(self):
        if self._tr_job is not None:
            try:
                self.root.after_cancel(self._tr_job)
            except Exception:
                pass
            self._tr_job = None

    def _tick_traffic(self):
        """每 1 秒采一次：读累计字节 → 与上次相减 → 除以时间 → 推入图表。"""
        try:
            if self.relay is None or not self.relay.running:
                self.lbl_traffic.config(text="未启动", fg=C_SUB)
                self._tr_job = None
                return

            up, down = self.relay.traffic()
            now = time.time()
            dt = now - self._tr_last_at
            if dt <= 0:
                dt = 1.0

            d_up = max(0, up - self._tr_last_up)
            d_down = max(0, down - self._tr_last_down)
            self._tr_last_up, self._tr_last_down = up, down
            self._tr_last_at = now

            up_rate = d_up / dt
            down_rate = d_down / dt
            self.chart.push(up_rate, down_rate)
            self.lbl_traffic.config(
                text=f"↓ {fmt_rate(down_rate)}   ↑ {fmt_rate(up_rate)}",
                fg=C_TEXT,
            )
        except Exception as e:
            _boot_log(f"_tick_traffic 异常: {e!r}")
        finally:
            if self.relay is not None and self.relay.running:
                self._tr_job = self.root.after(1000, self._tick_traffic)
            else:
                self._tr_job = None

    def _detect_initial_state(self):
        diag = core.diagnose_proxy_state()

        # 优先检查异常残留：代理残留会让浏览器所有网页都打不开
        if not diag["healthy"]:
            self._set_state(
                "idle", "检测到代理残留", C_YELLOW,
                "系统代理配置不干净，可能上不了网"
            )
            self.log("=" * 52)
            self.log("[警告] 检测到系统代理配置存在残留：")
            if diag["stale_pac"]:
                self.log(f"  连接配置里还留着 PAC 地址：{diag['conn_pac']}")
                self.log("  这会导致浏览器反复尝试拉取一个连不上的配置，")
                self.log("  表现为「所有网页都打不开」。")
            if diag["stale_proxy"]:
                self.log(f"  连接配置里还留着代理地址：{diag['conn_proxy']}")
            self.log("")
            self.log("  点下面的橙色按钮「上网异常？点这里修复」即可解决。")
            self.log("=" * 52)
            return

        snap = core.get_current_proxy()
        if core.has_backup() and snap.enable:
            # 有备份且代理开着：说明上次可能异常退出，仍处于共享态
            self._set_state(
                "active", "共享中（上次未还原）", C_GREEN,
                f"系统代理：{snap.server or '?'}"
            )
            self.log("检测到系统代理处于启用状态，且存在备份记录。")
            self.log("如需恢复，请点「还原系统代理」。")
        else:
            self._set_state("idle", "未启用", C_GRAY,
                            "系统代理：直连（未使用代理）")

    # ------------------------------------------------------------------
    # 业务动作
    # ------------------------------------------------------------------
    def on_main(self):
        if self.state == "active":
            self.on_restore()
        else:
            self._start()

    def _start(self):
        host = self.var_host.get().strip()
        port_s = self.var_port.get().strip()

        manual = None
        if host and port_s:
            try:
                manual = core.ProxyTarget(host, int(port_s))
            except ValueError:
                messagebox.showwarning(APP_TITLE, "端口必须是数字")
                return
        elif host or port_s:
            messagebox.showwarning(APP_TITLE, "IP 和端口要一起填")
            return

        self.cancel.clear()
        self._set_state("busy", "正在探测 ...", C_YELLOW)
        threading.Thread(target=self._worker_start, args=(manual,), daemon=True).start()

    def _stop_relay(self):
        """停掉本机中转（幂等，可在任何状态调用）。"""
        # 先停采样：否则采样回调可能在 relay 被置空后仍尝试读它
        self._stop_traffic()
        if self.relay is not None:
            try:
                self.relay.stop()
            except Exception:
                pass
            self.relay = None

    def _worker_start(self, manual):
        try:
            if manual:
                self.log(f"使用手动指定：{manual}")
                if not core.test_port(manual.host, manual.port, 1.0):
                    self.log(f"[失败] 连不上 {manual} —— 手机代理没开，或不在同一热点")
                    self.msgq.put(("state", ("idle", "未启用", C_GRAY, "")))
                    return
                target = manual
            else:
                self.log("开始自动发现手机代理 ...")
                target = core.find_proxy(log=self.log, cancel=self.cancel)
                if self.cancel.is_set():
                    self.log("已取消")
                    return
                if target is None:
                    self.log("")
                    self.log("[失败] 没有发现可用的代理端口。请检查：")
                    self.log("  1) 手机上代理软件已启动 HTTP Proxy")
                    self.log("  2) 手机和电脑连的是同一个热点 / WiFi")
                    self.log("  3) 手机代理监听在 0.0.0.0（允许其他设备接入）")
                    self.log("  4) 也可在上方手动填 手机IP : 端口")
                    self.msgq.put(("state", ("idle", "未启用", C_GRAY, "")))
                    return

            self.log(f"发现代理：{target}")

            # ★ 关键防呆：先预检，链路不通就绝不写系统代理
            self.log("正在预检代理链路（TCP / 协议 / 出网）...")
            pf = core.preflight_proxy(target)
            if not pf.ok:
                self.log("")
                self.log(f"[已拦截] 代理链路不可用（卡在：{pf.stage}）")
                for line in pf.reason.split("\n"):
                    self.log("  " + line)

                # 如果是"能连但不讲协议"，顺手查一下手机端端口占用情况
                if pf.stage == "protocol":
                    try:
                        owners = core.phone_port_owners()
                    except Exception:
                        owners = []
                    if owners:
                        self.log("")
                        self.log("[手机端诊断] 检测到这些敏感端口正在被监听：")
                        for o in owners:
                            pkg = o.get("package") or "（无法确定归属，需 root）"
                            self.log(f"    {o['port']:>6}  ←  {pkg}")
                        self.log("  " + core.phone_diagnose_hint().replace("\n", "\n  "))

                self.log("")
                self.log("已保持系统代理不变 —— 也就是说浏览器仍可正常上网。")
                self.log("请按上面提示在手机端处理好后，再点一次「一键启动共享」。")
                self.msgq.put(("state", ("idle", "未启用（链路不通）", C_RED, "")))
                self.msgq.put(("alert", pf.reason))
                return

            self.log("[OK] 代理链路正常")

            # 按探测到的协议设置（SOCKS5 必须写成 socks=host:port）
            if pf.scheme:
                target.scheme = pf.scheme
            if target.is_socks:
                self.log("检测到该端口为 SOCKS5 代理，将按 socks= 形式写入系统代理")
            else:
                self.log("检测到该端口为 HTTP 代理")

            # ★ 手机端"后台运行"前置检查（用户明确要求的一整串校验）
            # 三步走：① 进程还活着吗 ② 省电/自启动权限够吗 ③ 别被系统冻结
            # 注意：查不到 ≠ 没问题，hint 会明确说明"没查成"。
            try:
                running = core.phone_app_running()
                self.log(core.phone_app_status_hint(running))

                perms = core.phone_permissions()
                hint = core.phone_permission_hint(perms)
                if hint:
                    self.log(hint)
                self.log("")
            except Exception as e:
                # 不静默吞掉：这类失败本身就是要告诉用户的信息
                self.log(f"[提示] 后台权限自动检查未完成（{type(e).__name__}），"
                         "请手动确认手机端省电策略为「无限制」。")
                self.log("")

            # ★ 关键检查：手机端代理链路是否真的能转发数据
            # 代理端口能连 ≠ 能转发。实测遇到过"握手成功、CONNECT 成功，
            # 但发请求收到 0 字节、连接被立刻关闭"的坏状态 ——
            # 这时设了系统代理，浏览器打开**任何**网页都是 ERR_CONNECTION_CLOSED。
            # 所以这种状态必须**直接拒绝启用**，而不是硬设了让用户全站报错。
            self.log("正在验证手机端代理能否真正转发数据 ...")
            vpn_state, vpn_ok, vpn_msg = core.vpn_effective(target, timeout_s=6.0)

            if vpn_state == "broken":
                self.log("")
                # ★ v3.2：先取手机 VPN 配置，判断是不是「全局 VPN + 局域网 bypass」死结
                vinfo = {}
                try:
                    vinfo = core.phone_vpn_info()
                except Exception as e:
                    self.log(f"[提示] 读取手机 VPN 配置失败（{type(e).__name__}），仅给出通用建议。")

                if vinfo.get("has_vpn") and vinfo.get("bypass_lan"):
                    self.log("[已拦截] 手机 VPN 开着，但「局域网网段被排除在隧道外」——")
                    self.log("         电脑的请求进了死胡同，启用代理只会全站报错。")
                    self.log("         注意：这不是程序坏了，你的手机本机上网是正常的。")
                else:
                    self.log("[已拦截] 手机端代理连国内站点都转发不了，启用只会全站报错")
                for line in vpn_msg.split("\n"):
                    self.log("  " + line)
                self.log("")
                self.log("已保持系统代理不变 —— 浏览器仍可正常上网。")
                self.log("请在手机上处理好后，再点一次「一键启动共享」。")
                self.msgq.put(("state", ("idle", "未启用（手机端异常）", C_RED, "")))
                self.msgq.put(("alert", vpn_msg))
                return

            if vpn_ok:
                self.log("[OK] " + vpn_msg)
            else:
                # no_vpn：国内通、国外不通 —— 这是正常状态，继续启用
                self.log("[提示] " + vpn_msg.split("\n")[0])
                self.log("")
                self.log("  这不影响使用：国内网站照常快速打开，")
                self.log("  需要翻墙的站点会在几秒内快速报错（不会卡死）。")

            # ★★ 关键改造（v3.0）：系统代理指向「本机中转」，而不是直接指向手机
            #
            # 为什么：Windows 系统代理是全有或全无。直接指向手机时，手机没连 VPN，
            # Google / YouTube 这类目标会**挂住**（TCP 连不通，一直等到超时），
            # 浏览器只能干等 —— 表现就是「网页一直转圈，跟断网一样」。
            #
            # 本机中转让它变成「快速失败」：3 秒连不上就立刻回错，
            # 浏览器马上知道打不开，国内站点该多快还是多快。
            self.log("")
            self.log("正在启动本机中转（让网页不再卡死）...")
            self._stop_relay()
            relay = LocalRelay(
                up_host=target.host, up_port=target.port, up_scheme=target.scheme,
                listen_host="127.0.0.1", listen_port=core.RELAY_PORT,
                connect_timeout=core.RELAY_TIMEOUT,
                log=self.log,
            )
            try:
                relay.start()
            except OSError as e:
                self.log(f"[失败] 本机中转启动不了（端口 {core.RELAY_PORT} 被占？）：{e}")
                self.log("仍会直接指向手机代理（但可能遇到网页卡住的情况）。")
                relay = None
            self.relay = relay

            # 代理指向本机中转；中转失败则退回直连手机
            if relay is not None:
                proxy_target = core.ProxyTarget("127.0.0.1", core.RELAY_PORT, "http")
                mode_desc = f"本机中转 127.0.0.1:{core.RELAY_PORT} → 手机 {target.host}:{target.port}"
            else:
                proxy_target = target
                mode_desc = f"直连手机 {target.host}:{target.port}"

            self.log("正在设置 Windows 系统代理...")
            core.apply_proxy(proxy_target, log=self.log)
            self.target = target
            self.proxy_target = proxy_target

            self.log("")
            self.log(f"代理地址 : {proxy_target.http_url}")
            self.log(f"注册表值 : ProxyServer = {proxy_target.proxy_server}")
            self.log(f"链路     : {mode_desc}")
            self.log("模式     : 手动代理（不依赖 PAC），超时快速失败 "
                     f"{core.RELAY_TIMEOUT:.0f}s")
            self.log(f"VPN 状态 : {'已生效' if vpn_ok else '未生效（只能访问国内站点）'}")
            self.log("状态     : 已启用（浏览器 / 系统级应用立即生效）")
            self.log("")
            if relay is not None:
                self.log("提示：手机没连 VPN 时，Google 等站点会 3 秒内快速报错，")
                self.log("      而不是一直转圈。手机连上 VPN 后这些站点就能打开。")
            self.log("提示：Firefox 等不读系统代理的软件，需单独填上面的地址。")

            self.msgq.put(("state", (
                "active", "共享中", C_GREEN,
                f"系统代理：127.0.0.1:{core.RELAY_PORT}"
                f" → 手机 {target.host}:{target.port}"
                f"（{'SOCKS5' if target.is_socks else 'HTTP'}）"
            )))

            # ★ v3.4：中转已在跑，开始采样并画实时流量曲线
            self._start_traffic()
        except Exception as e:
            self.log(f"[异常] {e}")
            self.msgq.put(("state", ("idle", "未启用", C_GRAY, "")))

    def on_scan(self):
        if self.state == "busy":
            return
        self.cancel.clear()
        self._set_state("busy", "正在探测 ...", C_YELLOW)
        self.log("—— 仅探测模式，不会修改系统代理 ——")

        def work():
            try:
                t = core.find_proxy(log=self.log, cancel=self.cancel)
                if t:
                    self.log(f"[OK] 发现代理：{t}")
                    pf = core.preflight_proxy(t)
                    if pf.ok:
                        self.log("[OK] 链路预检全部通过，可以点「一键启动共享」")
                    else:
                        self.log(f"[注意] 链路不可用（卡在：{pf.stage}）")
                        for line in pf.reason.split("\n"):
                            self.log("  " + line)
                else:
                    self.log("[失败] 未发现可用代理")
            finally:
                self.msgq.put(("state", ("idle", "未启用", C_GRAY, "")))

        threading.Thread(target=work, daemon=True).start()

    def on_repair(self):
        """强制恢复直连：清掉所有代理残留。

        用于"用了之后所有网页都打不开"的场景（代理/PAC 残留导致浏览器卡死）。
        """
        if self.state == "busy":
            return
        if not messagebox.askyesno(
            APP_TITLE,
            "这会把系统代理设置全部清空、恢复为直连，\n"
            "并清除本程序的备份记录。\n\n"
            "如果你正在用其他代理软件（Clash / v2ray 等），"
            "它们的设置也会被一起清掉。\n\n确定继续？",
        ):
            return

        self._set_state("busy", "正在修复 ...", C_YELLOW)

        def work():
            try:
                self._stop_relay()
                core.repair_direct(log=self.log)
                self.log("")
                self.log("提示：浏览器如果还打不开网页，请完全退出后重开。")
            except Exception as e:
                self.log(f"[异常] {e}")
            finally:
                self.target = None
                self.msgq.put(("state", (
                    "idle", "未启用", C_GRAY, "系统代理：直连（未使用代理）"
                )))

        threading.Thread(target=work, daemon=True).start()

    def on_restore(self):
        if self.state == "busy":
            return
        self._set_state("busy", "正在还原 ...", C_YELLOW)

        def work():
            try:
                self._stop_relay()
                core.restore_proxy(log=self.log)
            finally:
                self.target = None
                self.msgq.put(("state", (
                    "idle", "未启用", C_GRAY, "系统代理：直连（未使用代理）"
                )))

        threading.Thread(target=work, daemon=True).start()

    def on_copy(self):
        snap = core.get_current_proxy()
        if self.target:
            text = self.target.http_url
        elif snap.server:
            text = f"http://{snap.server}"
        else:
            text = ""
        if not text:
            messagebox.showinfo(APP_TITLE, "当前没有可复制的代理地址")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log(f"已复制到剪贴板：{text}")

    # ------------------------------------------------------------------
    def on_elevate(self):
        """以管理员身份重启自身"""
        if core.is_admin():
            messagebox.showinfo(APP_TITLE, "当前已经是管理员权限")
            return
        self.log("正在请求管理员权限 ...")
        if core.relaunch_as_admin():
            self.log("已发起提权，本窗口即将关闭。")
            if self.restore_on_exit.get():
                try:
                    diag = core.diagnose_proxy_state()
                    if core.has_backup() or diag["reg_proxy_enable"] or not diag["healthy"]:
                        core.restore_proxy(log=lambda m: None)
                except Exception as e:
                    _boot_log(f"提权前清理失败: {e!r}")
            self.root.after(400, self.root.destroy)
        else:
            self.log("[失败] 提权被拒绝或失败")
            messagebox.showwarning(APP_TITLE, "提权失败或被取消")

    def _on_close(self):
        """关窗时清理代理。

        注意：这里不能只看 has_backup()。
        如果程序在"设置完代理但备份被清掉"之类的中间状态下退出，
        只看备份就会漏掉残留，导致浏览器后续上不了网。
        所以改成：只要系统代理不干净，就还原。
        """
        # 先停本机中转：它监听 127.0.0.1，不停掉会留下占用的端口
        self._stop_relay()
        if self.restore_on_exit.get():
            try:
                need = core.has_backup()
                if not need:
                    diag = core.diagnose_proxy_state()
                    # 注册表开了代理，或连接配置里有残留，都要清
                    need = bool(diag["reg_proxy_enable"]) or not diag["healthy"]
                if need:
                    core.restore_proxy(log=lambda m: None)
            except Exception as e:
                _boot_log(f"关窗清理失败: {e!r}")
        self.root.destroy()


def main():
    _boot_log("main() 进入")

    # 捕获 Tk 回调里的所有未处理异常，落盘（--windowed 下 stderr 是黑洞）
    def _tk_excepthook(exc_type, exc_val, exc_tb):
        _boot_log("Tk 回调未捕获异常:\n" + "".join(
            traceback.format_exception(exc_type, exc_val, exc_tb)))

    root = tk.Tk()
    root.report_callback_exception = _tk_excepthook
    _boot_log("Tk() 创建成功")

    VpnShareApp(root)
    _boot_log("VpnShareApp 构建完成，进入 mainloop")
    root.mainloop()
    _boot_log("mainloop 退出（窗口关闭或异常终止）")


if __name__ == "__main__":
    # 打包成 --windowed exe 后 stderr 会被丢弃，一旦启动期抛异常就是"双击没反应"。
    # 这里把异常落到可信目录，并弹窗提示，避免用户一头雾水。

    def _crash_log(exc: BaseException):
        try:
            with open(os.path.join(LOG_DIR, "error.log"), "a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"{exc}\n\n详细信息已写入：\n{os.path.join(LOG_DIR, 'error.log')}",
                "VPN Share 启动失败", 0x10,
            )
        except Exception:
            pass

    try:
        main()
    except BaseException as e:  # noqa
        _crash_log(e)
        raise

# -*- coding: utf-8 -*-
"""
实时流量折线图（Tkinter Canvas 手绘）
=====================================

为什么不用 matplotlib
--------------------
matplotlib 会让 PyInstaller 打包体积增加 ~30MB，且嵌入 Tkinter 需要
FigureCanvasTkAgg 桥接。而这里只需要"两条随时间滚动的曲线"，
用 Canvas 手绘不到 200 行，零依赖、零体积代价、渲染还更快。

设计
----
- 下载线：绿色，曲线下带渐变填充（面积图，量感更直接）
- 上传线：蓝色，只画线
- 网格 + Y 轴刻度，量程自动取"好看"的量级
- 左上角图例显示当前速率与峰值

历史窗口 60 个点（配合 1 秒采样 = 1 分钟）。
"""

from __future__ import annotations

import tkinter as tk

# 与 app.py 的配色保持一致
C_BG = "#ffffff"
C_GRID = "#e8ebef"
C_AXIS_TEXT = "#9aa1ab"
C_DOWN = "#0aa869"      # 绿 = 下载
C_UP = "#2b6cd4"        # 蓝 = 上传

HISTORY = 60
MIN_Y_MAX = 64.0 * 1024        # Y 轴下限 64KB/s，保证空数据时刻度可读


def fmt_rate(bps: float) -> str:
    """把字节/秒转成人类可读文本。"""
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    if bps < 1024.0 * 1024 * 1024:
        return f"{bps / 1024 / 1024:.2f} MB/s"
    return f"{bps / 1024 / 1024 / 1024:.2f} GB/s"


def _fmt_axis(b: float) -> str:
    if b <= 0:
        return "0"
    if b < 1024:
        return f"{int(b)}B"
    if b < 1024 * 1024:
        return f"{int(b / 1024)}K"
    return f"{b / 1024 / 1024:.0f}M"


def _nice_ceil(v: float) -> float:
    """把值向上取整到 1/2/5 × 10^n 这种"好看"的量级。"""
    if v <= MIN_Y_MAX:
        return MIN_Y_MAX
    unit = 1.0
    while unit * 10 < v:
        unit *= 10
    n = v / unit
    if n <= 1:
        m = 1.0
    elif n <= 2:
        m = 2.0
    elif n <= 5:
        m = 5.0
    else:
        m = 10.0
    return max(m * unit, MIN_Y_MAX)


class TrafficChart(tk.Canvas):
    """实时流量折线图控件。

    用法：
        chart = TrafficChart(parent)
        chart.pack(...)
        chart.push(up_rate, down_rate)     # 每次采样调一次
    """

    def __init__(self, master, width: int = 560, height: int = 150, **kw):
        super().__init__(master, width=width, height=height, bg=C_BG,
                         highlightthickness=0, **kw)
        self._up: list[float] = []
        self._down: list[float] = []
        self._y_max = MIN_Y_MAX
        self._peak_up = 0.0
        self._peak_down = 0.0
        self._empty_text = "启动共享后显示实时流量"

    # ------------------------------------------------------------------ 数据

    def reset(self, empty_text: str = "启动共享后显示实时流量") -> None:
        """清空曲线（新一轮共享开始时调用）。"""
        self._up.clear()
        self._down.clear()
        self._y_max = MIN_Y_MAX
        self._peak_up = 0.0
        self._peak_down = 0.0
        self._empty_text = empty_text
        self.redraw()

    def push(self, up_rate: float, down_rate: float) -> None:
        """追加一个采样点并重绘。"""
        self._up.append(max(0.0, up_rate))
        self._down.append(max(0.0, down_rate))
        while len(self._up) > HISTORY:
            self._up.pop(0)
            self._down.pop(0)

        self._peak_up = max(self._peak_up, up_rate)
        self._peak_down = max(self._peak_down, down_rate)

        self._update_y_max()
        self.redraw()

    def current(self) -> tuple[float, float]:
        """返回最近一次采样 (up, down)。"""
        if not self._up:
            return 0.0, 0.0
        return self._up[-1], self._down[-1]

    def _update_y_max(self) -> None:
        peak = max(max(self._up, default=0.0), max(self._down, default=0.0))
        nice = _nice_ceil(peak)
        if nice > self._y_max:
            self._y_max = nice                     # 抬升：立即
        elif nice < self._y_max / 3.0:
            # 回落：平滑过渡，避免一次尖峰让曲线长期被压扁
            self._y_max = max(self._y_max * 0.85, nice, MIN_Y_MAX)

    # ------------------------------------------------------------------ 绘制

    def redraw(self) -> None:
        self.delete("all")
        w = int(self["width"])
        h = int(self["height"])
        if w <= 4 or h <= 4:
            return

        pad_left = 46
        pad_right = 8
        pad_top = 24
        pad_bottom = 8
        cw = w - pad_left - pad_right
        ch = h - pad_top - pad_bottom
        if cw <= 0 or ch <= 0:
            return

        self._draw_legend(pad_left, pad_top)

        if len(self._up) < 2:
            self.create_text(pad_left + cw / 2, pad_top + ch / 2,
                             text=self._empty_text, fill="#b8bec6",
                             font=("Microsoft YaHei UI", 9))
            return

        # ---- 网格与 Y 刻度 ----
        grid_n = 4
        for i in range(grid_n + 1):
            y = pad_top + ch * i / grid_n
            self.create_line(pad_left, y, pad_left + cw, y, fill=C_GRID)
            val = self._y_max * (grid_n - i) / grid_n
            self.create_text(pad_left - 6, y, text=_fmt_axis(val),
                             anchor="e", fill=C_AXIS_TEXT,
                             font=("Consolas", 8))

        # ---- 坐标换算 ----
        # X 轴固定按 HISTORY 个点铺满，这样窗口未满时曲线从左侧生长
        step = cw / (HISTORY - 1)
        offset = HISTORY - len(self._up)

        def px(i: int) -> float:
            return pad_left + (offset + i) * step

        def py(v: float) -> float:
            ratio = min(max(v / self._y_max, 0.0), 1.0)
            return pad_top + ch * (1.0 - ratio)

        # ---- 下载面积填充（用多条竖线模拟渐变，Tkinter Canvas 无原生渐变）----
        self._fill_area(px, py, self._down, pad_top + ch, C_DOWN, step)

        # ---- 两条曲线 ----
        self._draw_line(px, py, self._down, C_DOWN)
        self._draw_line(px, py, self._up, C_UP)

    def _fill_area(self, px, py, series, base_y, color, step) -> None:
        """面积填充：逐列画竖线，按高度调透明度（近似渐变）。"""
        n = len(series)
        line_w = max(1, int(step) + 1)
        for i in range(n - 1):
            x0 = px(i)
            x1 = px(i + 1)
            y0 = py(series[i])
            y1 = py(series[i + 1])
            # 相邻点连线在该列上的平均高度，画一根竖线
            ym = (y0 + y1) / 2
            if ym >= base_y:
                continue
            # 由下至上分段，靠近顶部的段颜色更"重"
            seg = 6
            for k in range(seg):
                sy0 = ym + (base_y - ym) * k / seg
                sy1 = ym + (base_y - ym) * (k + 1) / seg
                t = 1.0 - k / seg          # 越靠上越接近曲线
                shade = self._blend("#ffffff", color, 0.06 + 0.12 * t)
                self.create_line(x0, sy0, x1, sy1, fill=shade, width=line_w)

    def _draw_line(self, px, py, series, color) -> None:
        n = len(series)
        if n < 2:
            return
        pts = []
        for i in range(n):
            pts.extend([px(i), py(series[i])])
        if len(pts) >= 4:
            self.create_line(*pts, fill=color, width=2, smooth=True,
                             capstyle="round", joinstyle="round")

    def _draw_legend(self, pad_left: int, pad_top: int) -> None:
        up = self._up[-1] if self._up else 0.0
        down = self._down[-1] if self._down else 0.0
        y = pad_top - 10

        x = pad_left
        t1 = self.create_text(x, y, text=f"↓ {fmt_rate(down)}", anchor="w",
                              fill=C_DOWN, font=("Microsoft YaHei UI", 9, "bold"))
        x = self.bbox(t1)[2] + 12
        t2 = self.create_text(x, y, text=f"↑ {fmt_rate(up)}", anchor="w",
                              fill=C_UP, font=("Microsoft YaHei UI", 9, "bold"))
        x = self.bbox(t2)[2] + 14

        peak = f"峰值 ↓{fmt_rate(self._peak_down)}  ↑{fmt_rate(self._peak_up)}"
        self.create_text(x, y, text=peak, anchor="w",
                         fill=C_AXIS_TEXT, font=("Microsoft YaHei UI", 8))

    @staticmethod
    def _blend(c1: str, c2: str, t: float) -> str:
        """在两个 #rrggbb 之间按 t 线性插值。"""
        t = min(max(t, 0.0), 1.0)
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        m = [int(a[j] + (b[j] - a[j]) * t) for j in range(3)]
        return f"#{m[0]:02x}{m[1]:02x}{m[2]:02x}"

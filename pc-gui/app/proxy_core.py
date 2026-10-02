# -*- coding: utf-8 -*-
"""
VPN Share - 核心逻辑层（Windows）

职责：
  1. 探测局域网内的手机代理（HTTP 代理端口）
  2. 读写 WinINET 系统代理注册表
  3. 备份 / 设置 / 还原，并刷新 WinINET 让设置立即生效

原理见 README：手机开 VPN 后，手机上的 HTTP 代理进程 outbound 天然走 VPN 隧道，
电脑把系统代理指向「手机IP:端口」即可蹭上手机的 VPN。
"""

from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from typing import Callable, Iterable, Optional

import winreg  # pywin32 不必须，标准库 winreg 就够

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
# WinINET 真正生效的连接配置在下面这个子键的二进制值里。
# 只改 REG_PATH 下的 ProxyEnable/ProxyServer 是不够的：
# 浏览器实际读的是这里的 DefaultConnectionSettings，里面固化了
# flags（直连/手动代理/PAC/自动探测）+ 代理地址 + 绕过列表 + PAC 地址。
# 如果这里没清干净，还原后浏览器仍会去拉一个连不上的 PAC，导致全部网页打不开。
CONNS_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings\Connections"
CONN_VALUE = "DefaultConnectionSettings"
CONN_LEGACY = "SavedLegacySettings"

BACKUP_PATH = r"Software\VpnShare"
BACKUP_VALUE = "ProxyBackup"
BACKUP_CONN_VALUE = "ConnBackup"

# 常见代理端口（按命中概率排序）
# 常见代理端口（按命中概率排序）
# 1080 放最前：Every Proxy / 各类 VPN 的 SOCKS5 几乎都用它。
# 8080 放最后：它经常是 PAC/Web 端口，能连上但不响应代理协议，
#             一旦被自动发现优先命中，就会卡在预检上（实测踩过）。
DEFAULT_PORTS = [1080, 18080, 8888, 10809, 7890, 8118, 3128, 9999, 11080, 8080]

# 默认绕过列表：内网 / 本地不走代理
# ---- 本机中转层（local_relay）参数 ----
# 系统代理指向它，由它去连手机代理。核心价值是「快速失败」：
# 手机没连 VPN 时 Google 之类的目标会挂住，直连手机会让浏览器干等；
# 经本机中转后 3 秒内就报错返回，网页不再卡死。
RELAY_PORT = 8888
RELAY_TIMEOUT = 3.0

DEFAULT_BYPASS = (
    "<local>;localhost;127.*;"
    "10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.2*;172.30.*;172.31.*;192.168.*"
)

# 用于判断"代理链路是否活着"的探测目标
PROBE_URLS = [
    # 优先用轻量 204；国内网络下 gstatic 常不通，所以备选百度
    ("http://www.gstatic.com/generate_204", 204),
    ("http://www.baidu.com", None),
]

# 用于判断"VPN 是否真的生效"的探测目标 —— 这些站点在国内直连是打不开的。
# 如果代理通了但这几个打不开，说明手机端 VPN 没连上（或没走全局），
# 用户会感觉"浏览器跟断网一样"（页面里嵌的 Google 资源一直转圈）。
#
# ★ 格式 (host, port, path)：必须带 path，因为判据要"真的发一次请求
#   并收到数据"—— 只看 CONNECT 应答会被"答应但不转发"的坏代理骗过。
# generate_204 是刻意选的：正常返回 204 且几乎无 body，开销最小。
VPN_PROBE_HOSTS = [
    ("www.google.com", 443),
    ("www.youtube.com", 443),
]
VPN_PROBE_TARGETS = [
    ("www.gstatic.com", 443, "/generate_204"),
    ("www.google.com", 443, "/generate_204"),
]


# ----------------------------------------------------------------------------

# WinINET 刷新用的 option 常量
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------

@dataclass
class ProxyTarget:
    host: str
    port: int
    # 代理协议：'http' 或 'socks'。Windows 的 ProxyServer 字段支持
    # `socks=host:port` 写法；若不写前缀，Windows 一律按 HTTP 代理处理，
    # 对 SOCKS5 端口就会失败。
    scheme: str = "http"

    @property
    def is_socks(self) -> bool:
        """★ 必须同时接受 'socks' 和 'socks5'。

        坑：外部调用方（含 app.py 和自检脚本）常写 "socks5"，
        而这里原本只认 "socks" → is_socks 返回 False →
        于是用 HTTP 协议去跟 SOCKS5 端口对话，全部失败。
        实测踩过：SOCKS5 端口被误判为 broken，而同一链路手工测是通的。
        """
        return self.scheme in ("socks", "socks5", "socks4")

    @property
    def proxy_server(self) -> str:
        """写进注册表 ProxyServer 的值（带协议前缀）。"""
        if self.is_socks:
            return f"socks={self.host}:{self.port}"
        return f"{self.host}:{self.port}"

    @property
    def http_url(self) -> str:
        if self.is_socks:
            return f"socks5://{self.host}:{self.port}"
        return f"http://{self.host}:{self.port}"

    @property
    def pac_url(self) -> str:
        """按「代理端口也提供 PAC」的假设拼出的地址。

        ⚠️ 仅供展示/推测，**不要**直接拿去设 AutoConfigURL。
        实测 Every Proxy 的代理端口（1080）不提供 /proxy.pac，
        把浏览器指到这里会永久挂起 → 所有网页打不开。
        要用 PAC 必须走单独的 HTTP 服务地址（见 pac_url_alive 校验）。
        """
        return f"http://{self.host}:{self.port}/proxy.pac"

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class ProxySnapshot:
    """系统代理的原始状态，用于还原"""
    enable: Optional[int] = None
    server: Optional[str] = None
    pac: Optional[str] = None
    override: Optional[str] = None


# ----------------------------------------------------------------------------
# 注册表读写
# ----------------------------------------------------------------------------

def _read_reg_value(root, path: str, name: str):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, name)
            return val
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _write_reg_value(root, path: str, name: str, value, vtype) -> None:
    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, vtype, value)


def _delete_reg_value(root, path: str, name: str) -> None:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def get_current_proxy() -> ProxySnapshot:
    """读取当前 WinINET 代理设置"""
    r = winreg.HKEY_CURRENT_USER
    return ProxySnapshot(
        enable=_read_reg_value(r, REG_PATH, "ProxyEnable"),
        server=_read_reg_value(r, REG_PATH, "ProxyServer"),
        pac=_read_reg_value(r, REG_PATH, "AutoConfigURL"),
        override=_read_reg_value(r, REG_PATH, "ProxyOverride"),
    )


def refresh_wininet() -> None:
    """通知 WinINET 立即重读代理设置（否则浏览器要等一会才生效）"""
    try:
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# WinINET 连接配置（Connections\DefaultConnectionSettings 二进制 blob）
#
# 结构（小端）：
#   [0:4]   版本号（当前为 70）
#   [4:8]   计数器，每次修改 +1
#   [8]     flags 位：
#             0x01 = 直连
#             0x02 = 使用手动代理
#             0x04 = 使用自动配置脚本(PAC)
#             0x08 = 自动探测设置
#             0x10 = 是否使用该连接（通常置位）
#   [9:12]  保留
#   之后依次是 4 个「长度(4字节小端=字节数) + 字符串」槽位：
#             0=代理服务器 / 1=绕过列表 / 2=PAC 地址 / 3=自动配置(常空)
#
#   ★ 关键：字符串是 **ANSI/UTF-8 单字节**编码，不是 UTF-16LE！
#     实测 37 字节 = len("http://192.168.123.123:8080/proxy.pac")
#     Windows 会重写这个 blob，重写后尾部可能补 28 个零字节凑到 93 字节。
# ----------------------------------------------------------------------------

CONN_FLAG_DIRECT = 0x01
CONN_FLAG_PROXY = 0x02
CONN_FLAG_PAC = 0x04
CONN_FLAG_AUTODETECT = 0x08


def _conn_read_sz(blob: bytes, pos: int) -> tuple[str, int]:
    """从 blob 的 pos 处读一个「长度 + 字符串」。

    实测（Windows 10/11 规范化后的 blob）：长度字段是**字节数**，
    字符串是 **ANSI/UTF-8 单字节编码**，不是 UTF-16LE。
    用 utf-16-le 解会得到 "瑨灴⼺..." 这类乱码。
    """
    if pos + 4 > len(blob):
        return "", pos
    n = int.from_bytes(blob[pos:pos + 4], "little")
    pos += 4
    if n <= 0 or pos + n > len(blob):
        return "", pos
    raw = blob[pos:pos + n]
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc), pos + n
        except Exception:
            continue
    return raw.decode("latin-1", errors="replace"), pos + n


def read_conn_settings() -> Optional[dict]:
    """读取 WinINET 连接配置。读不到返回 None。

    blob 布局（36/93 字节版本均适用，小端）：
      [0:4]   版本号（新版为 70 / 0x46）
      [4:8]   修改计数器
      [8]     flags 位
      [9:12]  保留
      [12:]   依次 4 个「长度(4字节) + 字符串」槽位：
              0=代理服务器  1=绕过列表  2=PAC 地址  3=自动配置
    """
    raw = _read_reg_value(winreg.HKEY_CURRENT_USER, CONNS_PATH, CONN_VALUE)
    if not isinstance(raw, bytes) or len(raw) < 12:
        return None
    flags = raw[8]
    pos = 12
    slots = []
    for _ in range(4):
        s, pos = _conn_read_sz(raw, pos)
        slots.append(s)
    # 兼容只有 3 个槽位的旧 blob
    proxy, bypass, pac = slots[0], slots[1], slots[2]
    return {
        "version": int.from_bytes(raw[0:4], "little"),
        "counter": int.from_bytes(raw[4:8], "little"),
        "flags": flags,
        "proxy": proxy,
        "bypass": bypass,
        "pac": pac,
    }


def _conn_build(proxy: str, bypass: str, pac: str) -> bytes:
    """按当前配置构造一个 DefaultConnectionSettings blob。

    flags 根据三个字段是否为空自动推导：
      有 pac    -> 置 PAC 位（并保持直连位，让 PAC 拉不到时能回退直连）
      有 proxy  -> 置手动代理位
      都为空    -> 只留直连位
    """
    cur = read_conn_settings()
    version = cur["version"] if cur else 70
    counter = (cur["counter"] if cur else 0) + 1

    flags = CONN_FLAG_DIRECT
    if pac:
        flags |= CONN_FLAG_PAC
    if proxy:
        flags |= CONN_FLAG_PROXY

    def _sz(s: str) -> bytes:
        # 与 Windows 一致：长度 = 字节数，字符串用 UTF-8/ANSI 单字节编码
        b = (s or "").encode("utf-8")
        return len(b).to_bytes(4, "little") + b

    blob = version.to_bytes(4, "little")
    blob += counter.to_bytes(4, "little")
    blob += bytes([flags, 0x00, 0x00, 0x00])
    blob += _sz(proxy)
    blob += _sz(bypass)
    blob += _sz(pac)
    blob += _sz("")          # 第 4 槽：自动配置，保持空
    blob += b"\x00" * 28     # 尾部补零，对齐 Windows 的 93 字节长度
    return blob


def write_conn_settings(proxy: str, bypass: str, pac: str) -> None:
    """把连接配置写进 WinINET 真正读取的二进制值（两个值都要写）。"""
    blob = _conn_build(proxy, bypass, pac)
    for name in (CONN_VALUE, CONN_LEGACY):
        try:
            _write_reg_value(winreg.HKEY_CURRENT_USER, CONNS_PATH, name, blob, winreg.REG_BINARY)
        except Exception:
            pass


def reset_conn_to_direct() -> None:
    """把连接配置强制重置为纯直连（清空代理/PAC）。"""
    write_conn_settings("", "", "")


def diagnose_proxy_state() -> dict:
    """检查当前代理状态是否"干净"，用于判断是否残留了导致断网的配置。

    返回 dict，含：
      reg_proxy_enable / reg_proxy_server / reg_pac : 注册表表面值
      conn_flags / conn_proxy / conn_pac            : 连接配置实际值
      stale_pac / stale_proxy                       : 是否残留了代理配置
      healthy                                       : 是否处于干净状态
    """
    r = winreg.HKEY_CURRENT_USER
    reg_enable = _read_reg_value(r, REG_PATH, "ProxyEnable") or 0
    reg_server = _read_reg_value(r, REG_PATH, "ProxyServer") or ""
    reg_pac = _read_reg_value(r, REG_PATH, "AutoConfigURL") or ""

    conn = read_conn_settings() or {}
    conn_proxy = conn.get("proxy", "") or ""
    conn_pac = conn.get("pac", "") or ""
    conn_flags = conn.get("flags", 0)

    # 残留判定：代理开关是关的，但连接配置里还留着代理/PAC —— 这会让浏览器卡死
    stale_pac = bool(conn_pac) and not reg_pac
    stale_proxy = bool(conn_proxy) and not reg_server

    return {
        "reg_proxy_enable": reg_enable,
        "reg_proxy_server": reg_server,
        "reg_pac": reg_pac,
        "conn_flags": conn_flags,
        "conn_proxy": conn_proxy,
        "conn_pac": conn_pac,
        "stale_pac": stale_pac,
        "stale_proxy": stale_proxy,
        "healthy": not (stale_pac or stale_proxy),
    }


def repair_direct(log: Callable[[str], None] = print) -> None:
    """一键修复：把所有代理配置清成直连，消除残留。

    用于"用了之后所有网页都打不开"的场景。
    """
    r = winreg.HKEY_CURRENT_USER
    log("正在清理注册表代理设置 ...")
    _write_reg_value(r, REG_PATH, "ProxyEnable", 0, winreg.REG_DWORD)
    for name in ("ProxyServer", "AutoConfigURL", "ProxyOverride"):
        _delete_reg_value(r, REG_PATH, name)

    log("正在重置 WinINET 连接配置 ...")
    reset_conn_to_direct()

    log("正在通知系统刷新 ...")
    refresh_wininet()

    # 清掉可能残留的备份，避免后续误还原到坏状态
    try:
        winreg.DeleteKey(r, BACKUP_PATH)
    except OSError:
        pass

    log("已全部恢复直连，浏览器重开即可正常上网")


def has_backup() -> bool:
    return _read_reg_value(winreg.HKEY_CURRENT_USER, BACKUP_PATH, BACKUP_VALUE) is not None


def backup_current(log: Callable[[str], None] = print) -> bool:
    """备份当前代理设置（含 WinINET 连接配置）。已有备份则不覆盖。"""
    if has_backup():
        log("已存在代理备份，跳过（还原后会清除，可再次备份）")
        return False
    snap = get_current_proxy()
    data = json.dumps(asdict(snap), ensure_ascii=False)
    r = winreg.HKEY_CURRENT_USER
    _write_reg_value(r, BACKUP_PATH, BACKUP_VALUE, data, winreg.REG_SZ)

    # 连接配置的原始 blob 也存一份（完整字节，还原时原样写回）
    try:
        raw_blob = _read_reg_value(r, CONNS_PATH, CONN_VALUE)
        if isinstance(raw_blob, bytes):
            _write_reg_value(r, BACKUP_PATH, BACKUP_CONN_VALUE, raw_blob, winreg.REG_BINARY)
    except Exception:
        pass

    log("已备份原始代理设置")
    return True


def apply_proxy(target: ProxyTarget, use_pac: bool = False,
                pac_url: str = "",
                log: Callable[[str], None] = print) -> None:
    """把系统代理指向 target，并刷新 WinINET。

    同时更新两处，缺一不可：
      1. Internet Settings 下的 ProxyEnable / ProxyServer / AutoConfigURL（界面显示用）
      2. Connections\\DefaultConnectionSettings 二进制值（WinINET 真正读的）

    ★ 默认 use_pac=False：只用手动代理。

    为什么默认不用 PAC：手机上的代理端口（如 Every Proxy 的 1080）是**纯代理
    监听端口**，它只会响应 CONNECT/GET 这类代理请求，**不会**用 HTTP 提供
    /proxy.pac 文件。若把 AutoConfigURL 指到 http://手机IP:1080/proxy.pac，
    浏览器会去拉一个永远不响应的"文件"，表现为**所有网页都打不开/搜索无反应**。

    只有在用户明确给了独立的 PAC 服务地址（真正能返回 PAC 的 HTTP 服务）时，
    才通过 pac_url 传入并设 use_pac=True。
    """
    backup_current(log)
    r = winreg.HKEY_CURRENT_USER
    server = target.proxy_server   # SOCKS5 时是 socks=host:port

    _write_reg_value(r, REG_PATH, "ProxyEnable", 1, winreg.REG_DWORD)
    _write_reg_value(r, REG_PATH, "ProxyServer", server, winreg.REG_SZ)
    _write_reg_value(r, REG_PATH, "ProxyOverride", DEFAULT_BYPASS, winreg.REG_SZ)

    pac = (pac_url or "").strip() if use_pac else ""
    if pac:
        _write_reg_value(r, REG_PATH, "AutoConfigURL", pac, winreg.REG_SZ)
        log(f"PAC 已启用：{pac}")
    else:
        _delete_reg_value(r, REG_PATH, "AutoConfigURL")

    # 关键：同步连接配置二进制值（pac 必须与上面一致，否则残留会导致断网）
    write_conn_settings(
        proxy=server,
        bypass=DEFAULT_BYPASS,
        pac=pac,
    )

    refresh_wininet()


def restore_proxy(log: Callable[[str], None] = print) -> None:
    """还原系统代理：有备份就还原，没备份就清空为直连。

    注意：必须同时还原 Connections 的二进制配置，
    否则浏览器会继续尝试连之前设置的代理/PAC，表现为"所有网页都打不开"。
    """
    r = winreg.HKEY_CURRENT_USER
    raw = _read_reg_value(r, BACKUP_PATH, BACKUP_VALUE)

    if raw is None:
        log("无备份记录，清空代理（恢复直连）")
        _write_reg_value(r, REG_PATH, "ProxyEnable", 0, winreg.REG_DWORD)
        _delete_reg_value(r, REG_PATH, "ProxyServer")
        _delete_reg_value(r, REG_PATH, "AutoConfigURL")
        _delete_reg_value(r, REG_PATH, "ProxyOverride")
        # 清空连接配置里的代理与 PAC，避免残留导致浏览器卡死
        reset_conn_to_direct()
        refresh_wininet()
        log("已恢复为直连")
        return

    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    snap = ProxySnapshot(
        enable=data.get("enable"),
        server=data.get("server"),
        pac=data.get("pac"),
        override=data.get("override"),
    )

    # ProxyEnable 是 DWORD，缺省按 0
    try:
        enable_int = int(snap.enable) if snap.enable is not None else 0
    except (TypeError, ValueError):
        enable_int = 0
    _write_reg_value(r, REG_PATH, "ProxyEnable", enable_int, winreg.REG_DWORD)

    for name, val in (("ProxyServer", snap.server),
                      ("AutoConfigURL", snap.pac),
                      ("ProxyOverride", snap.override)):
        if val:
            _write_reg_value(r, REG_PATH, name, val, winreg.REG_SZ)
        else:
            _delete_reg_value(r, REG_PATH, name)

    # 关键：还原 Connections 二进制配置
    raw_blob = _read_reg_value(r, BACKUP_PATH, BACKUP_CONN_VALUE)
    if isinstance(raw_blob, bytes) and len(raw_blob) >= 12:
        # 有原始 blob：原样写回（最忠实）
        for name in (CONN_VALUE, CONN_LEGACY):
            try:
                _write_reg_value(r, CONNS_PATH, name, raw_blob, winreg.REG_BINARY)
            except Exception:
                pass
    else:
        # 没存到 blob：按备份里的字段重建
        write_conn_settings(
            proxy=snap.server or "",
            bypass=snap.override or "",
            pac=snap.pac or "",
        )

    refresh_wininet()
    # 还原后清除备份，允许下次重新备份
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, BACKUP_PATH)
    except OSError:
        pass
    log("已还原原始代理设置")


# ----------------------------------------------------------------------------
# 网络探测
# ----------------------------------------------------------------------------

# adb 路径缓存：一次解析，全程复用
_ADB_PATH: Optional[str] = None
_ADB_RESOLVED = False


def find_adb() -> Optional[str]:
    """定位 adb 可执行文件。

    优先顺序：
      1. PATH 里的 adb（shutil.which）
      2. 环境变量 ADB
      3. Android SDK 常见安装位置（用户机实测在 C:\\Users\\<u>\\Android\\Sdk）

    找不到时返回 None。注意：**不要**因为 adb 不在 PATH 就直接放弃 ——
    大量 Windows 机器装了 Android Studio 但没把 platform-tools 加进 PATH。
    """
    global _ADB_PATH, _ADB_RESOLVED
    if _ADB_RESOLVED:
        return _ADB_PATH
    _ADB_RESOLVED = True

    # 1) PATH
    p = shutil.which("adb")
    if p:
        _ADB_PATH = p
        return p

    # 2) 环境变量
    env_adb = os.environ.get("ADB")
    if env_adb and os.path.isfile(env_adb):
        _ADB_PATH = env_adb
        return env_adb

    # 3) 常见安装位置
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Android", "Sdk", "platform-tools", "adb.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk",
                     "platform-tools", "adb.exe"),
        os.path.join(os.environ.get("ANDROID_HOME", ""), "platform-tools", "adb.exe"),
        os.path.join(os.environ.get("ANDROID_SDK_ROOT", ""), "platform-tools", "adb.exe"),
        r"C:\platform-tools\adb.exe",
        r"C:\Android\platform-tools\adb.exe",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            _ADB_PATH = c
            return c

    _ADB_PATH = None
    return None


def _run_cmd(args: list[str], timeout: float = 15.0) -> str:
    """跑外部命令并稳健解码。

    Windows 上 arp / ipconfig 等输出是 GBK(936) 而非 UTF-8，
    用 text=True 会抛 UnicodeDecodeError 并丢掉整个结果。
    这里统一拿 bytes 再按 GBK -> UTF-8 顺序尝试。

    如果 args[0] 是 "adb"，会自动替换成解析出来的 adb 绝对路径，
    这样即使用户没把 platform-tools 加进 PATH 也能用。
    """
    if args and args[0] == "adb":
        real = find_adb()
        if real:
            args = [real] + list(args[1:])
    r = subprocess.run(
        args, capture_output=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    raw = r.stdout or b""
    for enc in ("gbk", "utf-8", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _is_usable_ip(ip: str) -> bool:
    """过滤掉不该扫描的地址"""
    if not ip or ip.count(".") != 3:
        return False
    if ip.startswith(("127.", "224.", "239.", "255.", "0.")):
        return False
    # 169.254.* 是 APIPA（网卡没拿到 DHCP），扫它没意义
    if ip.startswith("169.254."):
        return False
    # 广播地址（末位 .255，或网段广播）：直接排掉
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_multicast or addr.is_unspecified:
            return False
    except ValueError:
        return False
    if ip.endswith(".255"):
        return False
    return True


def test_port(ip: str, port: int, timeout_s: float = 0.25) -> bool:
    """TCP 连通性探测（非阻塞式短超时）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def local_networks() -> list[tuple[str, int]]:
    """枚举本机 IPv4 网卡 (ip, prefixlen)"""
    out: list[tuple[str, int]] = []
    try:
        # 直接用 PowerShell 拿更稳（Get-NetIPAddress），失败则退到 socket
        ps = (
            "Get-NetIPAddress -AddressFamily IPv4 | "
            "Where-Object { $_.IPAddress -notlike '127.*' } | "
            "ForEach-Object { \"$($_.IPAddress)/$($_.PrefixLength)\" }"
        )
        text = _run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
        for line in text.splitlines():
            line = line.strip()
            if "/" in line:
                ip, _, plen = line.partition("/")
                ip = ip.strip()
                try:
                    plen_i = int(plen)
                except ValueError:
                    continue
                # 跳过 169.254.*（APIPA，网卡没拿到 IP）
                if not _is_usable_ip(ip):
                    continue
                out.append((ip, plen_i))
    except Exception:
        pass

    if not out:
        # 兜底：用 UDP connect 试探本机出口 IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            out.append((s.getsockname()[0], 24))
            s.close()
        except Exception:
            pass

    # 去重
    seen = set()
    uniq = []
    for ip, plen in out:
        if ip not in seen:
            seen.add(ip)
            uniq.append((ip, plen))
    return uniq


def candidate_ips() -> list[str]:
    """生成待扫描的候选 IP 列表（有序，越靠前越可能命中）"""
    ips: list[str] = []

    # 0) USB 直连模式：adb forward 映射到本机回环，优先
    ips.append("127.0.0.1")

    nets = local_networks()
    for ip, plen in nets:
        if not _is_usable_ip(ip):
            continue
        try:
            net = ipaddress.ip_network(f"{ip}/{plen}", strict=False)
            # 该网段的 .1（手机开热点时通常自己就是 .1）
            first = next(net.hosts(), None)
            if first and _is_usable_ip(str(first)):
                ips.append(str(first))
        except Exception:
            pass

    # 默认网关
    try:
        ps = (
            "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
            "Sort-Object RouteMetric | Select-Object -First 1).NextHop"
        )
        gw = _run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]).strip()
        if _is_usable_ip(gw):
            ips.append(gw)
    except Exception:
        pass

    # ARP 邻居表（同网段活跃主机，命中率最高的一批）
    try:
        text = _run_cmd(["arp", "-a"], timeout=10.0)
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                cand = parts[0]
                if _is_usable_ip(cand):
                    ips.append(cand)
    except Exception:
        pass

    # 去重保序
    seen = set()
    result = []
    for ip in ips:
        if ip and ip not in seen:
            seen.add(ip)
            result.append(ip)
    return result


def find_proxy(ports: Iterable[int] = DEFAULT_PORTS,
               timeout_s: float = 0.25,
               log: Callable[[str], None] = print,
               cancel: Optional[threading.Event] = None,
               verify: bool = True) -> Optional[ProxyTarget]:
    """在候选 IP × 端口 上扫，返回能**真正讲代理协议**的那个。

    只看 TCP 通是不够的：PAC / Web 端口也能连上，但不响应代理协议，
    选错就会卡在预检。所以这里默认做一次协议识别，优先返回可用的。

    策略：
      - 先收集所有 TCP 可连的候选
      - 逐个做协议识别（HTTP / SOCKS5），命中即返回，并带上协议
      - 若全部都不讲代理协议，回退返回第一个可连的（让预检去报清楚的错）
    """
    ports = list(ports)
    ips = candidate_ips()
    log(f"扫描 {len(ips)} 个候选地址 × {len(ports)} 个端口 ...")

    reachable: list[ProxyTarget] = []
    for ip in ips:
        if cancel is not None and cancel.is_set():
            return None
        for p in ports:
            if cancel is not None and cancel.is_set():
                return None
            if test_port(ip, p, timeout_s):
                reachable.append(ProxyTarget(ip, p))

    if not reachable:
        return None

    if not verify:
        return reachable[0]

    log(f"发现 {len(reachable)} 个可连端口，正在识别代理协议 ...")
    for t in reachable:
        if cancel is not None and cancel.is_set():
            return None
        ok_h, _ = _probe_http_proxy(t.host, t.port, timeout_s=1.5)
        if ok_h:
            t.scheme = "http"
            log(f"  {t.host}:{t.port} -> HTTP 代理")
            return t
        ok_s, _ = _probe_socks5(t.host, t.port, timeout_s=1.5)
        if ok_s:
            t.scheme = "socks"
            log(f"  {t.host}:{t.port} -> SOCKS5 代理")
            return t
        log(f"  {t.host}:{t.port} -> 可连但不响应代理协议，跳过")

    log("  未识别到代理协议，回退到第一个可连端口（预检会给出具体原因）")
    return reachable[0]


def probe_through_proxy(target: ProxyTarget, timeout_s: float = 8.0) -> bool:
    """通过代理发一个 HTTP 请求，验证链路是否真的通。

    会先用 HTTP 代理方式试，再用手写 SOCKS5 方式试 —— 因为手机端代理
    可能只开了 SOCKS5，或者 HTTP 端口没开。任一种能通即算成功。
    """
    import urllib.request

    # 方式一：HTTP 代理
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({
            "http": target.http_url,
            "https": target.http_url,
        })
    )
    for url, expect_code in PROBE_URLS:
        try:
            resp = opener.open(url, timeout=timeout_s)
            code = resp.getcode()
            if expect_code is None or code == expect_code:
                return True
        except Exception:
            continue

    # 方式二：手写 SOCKS5（不依赖第三方库）
    for url, expect_code in PROBE_URLS:
        try:
            host, _, path = url.split("/", 2)[2].partition("/")
            body = _socks5_http_get(target.host, target.port, host,
                                    "/" + path, timeout_s)
            if body:
                if expect_code is None:
                    return True
                if f" {expect_code} ".encode() in body[:64]:
                    return True
        except Exception:
            continue
    return False


def _socks5_http_get(proxy_host: str, proxy_port: int,
                     dst_host: str, dst_path: str,
                     timeout_s: float) -> bytes:
    """经 SOCKS5 代理发一个 HTTP GET，返回响应头（失败返回 b""）。

    手写实现，避免引入 PySocks 依赖（打包体积 + 兼容性）。
    """
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout_s)
    s.settimeout(timeout_s)
    try:
        # 握手：只要 NOAUTH
        s.sendall(b"\x05\x01\x00")
        if _recv_exact(s, 2, timeout_s) != b"\x05\x00":
            return b""
        # 请求：域名方式连接 80 端口
        hb = dst_host.encode("idna") if not _is_ip(dst_host) else None
        if hb is None:
            parts = [int(x) for x in dst_host.split(".")]
            req = bytes([0x05, 0x01, 0x00, 0x01]) + bytes(parts)
        else:
            req = bytes([0x05, 0x01, 0x00, 0x03, len(hb)]) + hb
        req += (80).to_bytes(2, "big")
        s.sendall(req)
        rep = _recv_exact(s, 4, timeout_s)
        if len(rep) < 4 or rep[1] != 0x00:
            return b""
        # ★ 吃掉 BND.ADDR / BND.PORT —— 必须读满，否则残留字节污染响应
        atyp = rep[3]
        if atyp == 0x01:
            _recv_exact(s, 4 + 2, timeout_s)
        elif atyp == 0x03:
            n = _recv_exact(s, 1, timeout_s)
            if n:
                _recv_exact(s, n[0] + 2, timeout_s)
        elif atyp == 0x04:
            _recv_exact(s, 16 + 2, timeout_s)
        # 发真正的 HTTP 请求
        s.sendall(
            f"GET {dst_path} HTTP/1.1\r\nHost: {dst_host}\r\n"
            f"Connection: close\r\nUser-Agent: VpnShare/2.1\r\n\r\n".encode()
        )
        return s.recv(256)
    finally:
        try:
            s.close()
        except Exception:
            pass


def _is_ip(host: str) -> bool:
    try:
        socket.inet_aton(host)
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------------
# 代理链路预检
# ----------------------------------------------------------------------------

class PreflightResult:
    """预检结果：stage 表示卡在哪一步，reason 是给人看的说明。"""

    def __init__(self, ok: bool, stage: str, reason: str, scheme: str = ""):
        self.ok = ok
        self.stage = stage
        self.reason = reason
        # 探测出来的可用协议（'http' / 'socks'），ok=True 时有效
        self.scheme = scheme

    def __repr__(self) -> str:
        return f"PreflightResult(ok={self.ok}, stage={self.stage!r}, reason={self.reason!r})"


def _probe_http_proxy(host: str, port: int, timeout_s: float) -> tuple[bool, str]:
    """按 HTTP 代理协议探一下。返回 (是否成功, 说明)。

    ★ 必须校验响应内容是不是**代理应答**，不能只看"有回应"。

    踩过的坑：手机端那个"VPN 热点"App 开了自己的 Web 管理接口，
    收到 HTTP 请求会返回 404 / 412。如果只看"有回应就放行"，
    会把这种 Web 服务误判成代理，然后写进系统代理 → 浏览器全废。

    真实 HTTP 代理的应答应满足下面之一：
      - `200`：成功取到目标页面（说明它真的能代理）
      - `407`：要求代理鉴权（也是真代理）
      - `301/302`：目标站点跳转（也算代理通了）
    明确排除：`404` / `412` / `400` 等 —— 那是 Web 服务器，不是代理。
    """
    try:
        s = socket.create_connection((host, port), timeout=timeout_s)
        s.settimeout(timeout_s)
        s.sendall(
            b"GET http://www.baidu.com/ HTTP/1.1\r\n"
            b"Host: www.baidu.com\r\n"
            b"Proxy-Connection: close\r\n"
            b"User-Agent: VpnShare-Preflight/2.0\r\n\r\n"
        )
        data = s.recv(64)
        s.close()
        if not data:
            return False, "no-data"

        first = data.split(b"\r\n")[0].decode("latin-1", errors="replace")
        # 形如 "HTTP/1.1 404 Not Found"
        parts = first.split(" ")
        code = parts[1] if len(parts) >= 2 else ""
        if code in ("200", "301", "302", "307", "407"):
            return True, first
        return False, f"非代理应答({code or first[:40]})"
    except socket.timeout:
        return False, "timeout"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _probe_socks5(host: str, port: int, timeout_s: float) -> tuple[bool, str]:
    """按 SOCKS5 协议握手。返回 (是否成功, 说明)。

    手机端代理常常同时开 HTTP 和 SOCKS5 两个端口。如果端口是 SOCKS5，
    我们发 HTTP 明文是**收不到回应**的（它在等 SOCKS5 的版本字节），
    单发 HTTP 会误判成"端口死了"。所以必须两种协议都试。

    ★ 这里做**两段式**校验，避免把一个恰好回了 0x05 的其他服务误判成代理：
      1. 方法协商：应回 `05 xx`，且 xx 是 0x00(免认证) 或 0x02(用户名密码)
      2. 真实连接请求：对 baidu:80 发起 CONNECT，应回 `05 00`（成功）
         或 `05 02/03/04/05`（明确的拒绝码，也证明它读懂并回应了 SOCKS5）
    """
    try:
        s = socket.create_connection((host, port), timeout=timeout_s)
        s.settimeout(timeout_s)
        s.sendall(b"\x05\x01\x00")          # VER=5, NMETHODS=1, NOAUTH
        r = s.recv(2)
        if len(r) != 2 or r[0] != 0x05:
            s.close()
            return False, "bad-reply"
        if r[1] not in (0x00, 0x02):
            s.close()
            return False, f"方法协商失败(0x{r[1]:02x})"

        # 第二段：真的发一个 CONNECT 请求（域名方式连 baidu:80）
        host_b = b"www.baidu.com"
        req = bytes([0x05, 0x01, 0x00, 0x03, len(host_b)]) + host_b + (80).to_bytes(2, "big")
        s.sendall(req)
        rep = s.recv(4)
        s.close()
        if len(rep) < 2 or rep[0] != 0x05:
            return False, "connect-no-reply"
        # rep[1] == 0x00 表示连接成功；其他是标准 SOCKS5 拒绝码，
        # 同样说明对方是真 SOCKS5 服务（只是当前目标连不上）
        if rep[1] == 0x00:
            return True, "SOCKS5 (CONNECT ok)"
        return True, f"SOCKS5 (reply=0x{rep[1]:02x})"
    except socket.timeout:
        return False, "timeout"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def preflight_proxy(target: ProxyTarget, timeout_s: float = 5.0) -> PreflightResult:
    """在改动系统代理**之前**，逐级验证链路。

    这是关键防呆：很多"点了启动之后浏览器就废了"的场景，
    根因是端口虽然能连上，但代理协议根本不响应。此时如果直接写系统代理，
    浏览器就会把每个请求都发给一个死端口 —— 表现就是所有网页/搜索全部卡死。

    检查顺序：
      1. TCP 是否连得上
      2. HTTP 代理 / SOCKS5 **两种协议都试**，任一种有正常回应即通过
      3. 通过代理能否真的取到外部网页
    """
    # 1) TCP
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect((target.host, target.port))
    except Exception as e:
        return PreflightResult(
            False, "tcp",
            f"连不上 {target.host}:{target.port}（{type(e).__name__}）。\n"
            f"请检查：手机和电脑是否在同一热点、手机端代理已开启、"
            f"并允许局域网访问（监听 0.0.0.0 而不是 127.0.0.1）。",
        )
    finally:
        try:
            s.close()
        except Exception:
            pass

    # 2) 两种协议都试
    http_ok, http_msg = _probe_http_proxy(target.host, target.port, timeout_s)
    socks_ok, socks_msg = _probe_socks5(target.host, target.port, timeout_s)

    if http_ok:
        return PreflightResult(True, "ok", f"HTTP 代理有响应：{http_msg}", scheme="http")
    if socks_ok:
        return PreflightResult(True, "ok", f"SOCKS5 代理有响应：{socks_msg}", scheme="socks")

    # 两种都不通，给出准确说明（不要再提"端口被占用"，那是误导）
    if http_msg == "timeout" and socks_msg == "timeout":
        return PreflightResult(
            False, "protocol",
            f"连上了 {target.host}:{target.port}，但 HTTP 和 SOCKS5 两种探测\n"
            f"等了 {timeout_s:.0f} 秒都没有任何回应。\n"
            f"\n"
            f"这表示「端口能建立连接，但不响应代理协议」。按经验，最可能的原因是：\n"
            f"  ★ 手机上有多个 VPN / 代理类 App 在抢同一个端口。\n"
            f"    先启动的那个占住了端口，但它提供的不是标准代理服务，\n"
            f"    所以电脑连得上却用不了。\n"
            f"    典型组合：Every Proxy + 某个自带代理监听的「VPN 热点」类 App。\n"
            f"\n"
            f"其他可能：\n"
            f"  - 端口填错了（例如填成了 PAC / Web 端口）\n"
            f"  - 代理服务开着但还没就绪（VPN 刚连上需等几秒）\n"
            f"  - 代理进程被系统省电策略冻结\n"
            f"\n"
            f"建议：①只留一个代理 App 在跑，关掉其余 VPN/热点类 App；\n"
            f"      ②在保留的那个 App 里确认代理开关已打开、端口号是多少；\n"
            f"      ③回电脑端按它的真实端口重试。\n"
            f"\n"
            f"（此时没有修改你的系统代理，浏览器仍可正常上网）",
        )

    return PreflightResult(
        False, "protocol",
        f"{target.host}:{target.port} 有连接但不响应代理协议。\n"
        f"HTTP 探测：{http_msg}\n"
        f"SOCKS5 探测：{socks_msg}\n"
        f"请回手机端核对代理端口号是否填对。",
    )

    # 3) 真实出网
    if probe_through_proxy(target, timeout_s=max(timeout_s, 8.0)):
        return PreflightResult(True, "ok", "链路正常，代理可正常出网")

    return PreflightResult(
        False, "egress",
        f"代理端口有响应，但通过它取不到外网页面。\n"
        f"可能原因：手机 VPN 没连上、或 VPN 不是全局模式（分流没覆盖代理进程）。\n"
        f"请确认手机上 VPN 已连接，且 Every Proxy 的出站走 VPN。",
    )


def pac_url_alive(pac_url: str, timeout_s: float = 3.0) -> bool:
    """检查 PAC 地址是否真的能返回内容（返回 200 且体积 > 0）。

    用于拦住"把 PAC 指到代理端口"这类错误配置。
    """
    import urllib.request

    if not pac_url:
        return False
    try:
        req = urllib.request.Request(pac_url, headers={"User-Agent": "VpnShare/2.0"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        resp = opener.open(req, timeout=timeout_s)
        body = resp.read(4096)
        return resp.getcode() == 200 and len(body) > 0
    except Exception:
        return False


def phone_port_owners() -> list[dict]:
    """通过 adb 查手机端哪些端口在监听，用于诊断"端口被别的 App 抢占"。

    典型场景：手机装了多个 VPN/代理类 App（如 Every Proxy + 某个"VPN 热点"
    全家桶），它们都想用 1080 / 8080，先起的那个占住，后起的报"端口被占用"；
    而电脑端自动发现又会扫到这个"能连但不讲代理协议"的端口，一路卡死。

    返回 [{'port': 1080, 'uid': '10123', 'package': 'com.xxx' or ''}]。
    包名可能为空 —— Android 普通 shell **拿不到**端口→进程映射
    （ss -p / netstat -p 都需要 root），此时 package 留空而不是瞎猜。
    """
    if not find_adb():
        return []

    owners: list[dict] = []

    # 1) 拿监听端口。
    #    优先读 /proc/net/tcp —— 它有 uid 列，可以精确归属到 App，
    #    且不受 "Cannot open netlink socket: Permission denied" 影响。
    #    第 1 列 local_address 形如 0100007F:0438（小端 IP : 大端端口）
    port_uid: dict[int, int] = {}
    for proto in ("tcp", "tcp6"):
        raw = _run_cmd(["adb", "shell", "cat", f"/proc/net/{proto}"], timeout=10.0)
        for i, line in enumerate(raw.splitlines()):
            if i == 0 or ":" not in line:
                continue
            parts = line.split()
            if len(parts) < 8:
                continue
            # parts[1]=local_address  parts[3]=st(0A=LISTEN)  parts[7]=uid
            if parts[3].upper() != "0A":
                continue
            try:
                port = int(parts[1].rsplit(":", 1)[1], 16)
                uid = int(parts[7])
            except (ValueError, IndexError):
                continue
            port_uid.setdefault(port, uid)

    # 兜底：用 ss 只拿端口号（无 uid）
    if not port_uid:
        out = _run_cmd(["adb", "shell", "ss", "-tln"], timeout=10.0)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0].upper() != "LISTEN":
                continue
            addr = parts[3]
            try:
                port_uid[int(addr.rsplit(":", 1)[1])] = -1
            except (ValueError, IndexError):
                continue

    if not port_uid:
        return []

    # 2) uid -> 包名
    uid_map: dict[int, str] = {}
    pm = _run_cmd(["adb", "shell", "pm", "list", "packages", "-U"], timeout=15.0)
    for line in pm.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        pkg = ""
        uid = -1
        for tok in line[len("package:"):].split():
            if tok.startswith("uid:"):
                try:
                    uid = int(tok[4:])
                except ValueError:
                    pass
            elif not pkg:
                pkg = tok
        if uid >= 0 and pkg:
            uid_map[uid] = pkg

    # 3) 只报"敏感端口"（代理/VPN 重灾区）
    sensitive = {1080, 8080, 8888, 8118, 10809, 7890, 3128}
    for p in sorted(set(port_uid) & sensitive):
        uid = port_uid.get(p, -1)
        pkg = uid_map.get(uid, "") if uid >= 0 else ""
        owners.append({
            "port": p,
            "uid": str(uid) if uid >= 0 else "",
            "package": pkg,
        })

    return owners


def phone_diagnose_hint() -> str:
    """给出针对手机端的排查建议（不依赖 adb 也能用）。"""
    return (
        "手机端常见冲突：装了多个代理/VPN 类 App，它们抢同一个端口。\n"
        "  - 1080 / 8080 是重灾区（几乎每个 VPN App 都想用）\n"
        "  - 先启动的占住端口，后启动的会提示「端口被占用」\n"
        "  - 电脑端扫到这个端口能连上，但它不响应代理协议 → 浏览器卡死\n"
        "建议：只保留一个代理 App 在运行，把其余 VPN/热点类 App 关掉再试。"
    )


def phone_vpn_info() -> dict:
    """读取手机端 VPN 的真实配置（通过 adb dumpsys）。

    ★ v3.2 新增：这是定位「手机能上网、电脑经代理全断」的关键。

    返回 dict：
      {
        "has_vpn": bool,          # 手机是否处于 VPN 连接态
        "pkg": str,               # VPN App 包名（如 com.ambrose.overwall）
        "session": str,           # sessionId（如 Gofly）
        "iface": str,             # 隧道接口（tun0）
        "bypass_lan": bool,       # ★ 局域网段是否被 VPN 绕过（致命项）
        "bypass_list": str,       # 完整 bypass 列表
        "global": bool,           # 是否全局模式（路由几乎覆盖全部 IPv4）
        "raw": str,               # 原始关键行，便于排查
      }
    失败时返回 {"has_vpn": False, "error": "..."} 。
    """
    adb = find_adb()
    out: dict = {"has_vpn": False}
    if not adb:
        out["error"] = "未找到 adb，无法读取手机 VPN 配置"
        return out
    try:
        raw = _run_cmd([adb, "shell", "dumpsys", "connectivity"], timeout=25)
    except Exception as e:
        out["error"] = f"dumpsys 失败：{e}"
        return out
    if not raw:
        out["error"] = "dumpsys 无输出"
        return out

    # ---- 是否处于 VPN 连接态 ----
    m = re.search(r"NetworkAgentInfo\{network\{\d+\}\s+handle\{\d+\}\s+ni\{VPN ([A-Z_]+)", raw)
    if not m:
        # 没找到 VPN 网络条目
        out["has_vpn"] = False
        return out
    out["has_vpn"] = True
    out["state"] = m.group(1)  # CONNECTED / DISCONNECTED 等

    # ---- VPN 包名（extra: VPN:com.xxx.yyy）----
    m = re.search(r"VPN CONNECTED extra: VPN:([\w.]+)", raw)
    if m:
        out["pkg"] = m.group(1)

    # ---- VpnTransportInfo：sessionId / OwnerUid ----
    m = re.search(r"VpnTransportInfo\{type=(\d+),\s*sessionId=([\w.-]+)[^}]*?\}", raw)
    if m:
        out["session"] = m.group(2)
    m = re.search(r"VpnTransportInfo\{[^}]*\}.*?OwnerUid:\s*(\d+)", raw)
    if m:
        out["owner_uid"] = m.group(1)

    # ---- 隧道接口名 ----
    m = re.search(r"InterfaceName:\s*(tun\d+)", raw)
    if m:
        out["iface"] = m.group(1)

    # ---- HttpProxy 配置（仅作展示，不是故障判据）----
    m = re.search(r"HttpProxy:\s*\[([^\]]+)\]\s*(\d+)\s*xl=([^}]*)\}", raw)
    if m:
        out["http_proxy"] = f"{m.group(1)}:{m.group(2)}"
        out["bypass_list"] = m.group(3)
        # ⚠️ 注意：xl= 列表**不是**「局域网流量不走隧道」的判据。
        # 实测教训：曾据此误判为"局域网 bypass 死结"，完全错误。
        # 该列表只影响 VPN 自身 HTTP 代理的绕过规则，不影响 tun 路由。
        # 真正的原因是 fake-IP（见 phone_fakeip_hint）。

    # ---- 路由覆盖范围：判断是否全局模式 ----
    routes = re.findall(r"(\d+\.\d+\.\d+\.\d+/\d+)\s*->\s*[\d.]+\s+(?:tun\d+|0\.0\.0\.0)", raw)
    if routes:
        # 全局模式会挂大量 /7 /6 /5 这类大段路由
        big = [r for r in routes if r.endswith(("/4", "/5", "/6", "/7", "/8"))]
        out["global"] = len(big) >= 10
        out["route_count"] = len(routes)

    # ---- ★ 核心判据：DNS 是否被 fake-IP 劫持 ----
    # fake-IP 模式（Clash 系常见）会把域名解析成 198.18.0.0/15 的保留地址。
    # 这个地址只在 VPN 内核内有意义，外部进程（包括我们的代理 App）
    # 拿它去 connect 必然失败 → 0 字节。
    out["fakeip"] = phone_fakeip_active(adb)

    return out


def phone_fakeip_active(adb: str) -> bool:
    """探测手机 DNS 是否处于 fake-IP 状态。

    ★ 这是「手机能上网、电脑经代理全断」的真正判据。

    做法：在手机上解析一个必定存在的域名，看返回的是不是
    198.18.0.0/15（RFC 保留段，公网不存在）。

    实测证据：Gofly 全局模式下 `ping www.baidu.com` 返回 198.18.2.25。
    """
    try:
        out = _run_cmd([adb, "shell", "ping", "-c", "1", "-W", "2", "www.baidu.com"],
                       timeout=10)
    except Exception:
        return False
    if not out:
        return False
    m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", out)
    if not m:
        return False
    ip = m.group(1)
    try:
        first, second = int(ip.split(".")[0]), int(ip.split(".")[1])
    except Exception:
        return False
    # 198.18.0.0/15 → 198.18.x.x 或 198.19.x.x
    return first == 198 and second in (18, 19)


def phone_vpn_diagnosis(info: dict, target: ProxyTarget) -> str:
    """把 phone_vpn_info() 的结果翻译成人话 + 可操作建议。

    ★ v3.3：改为围绕 **fake-IP** 这个真正的根因来诊断。
       （v3.2 曾误诊为「局域网 bypass 死结」，那个结论是错的，已废弃。）
    """
    if not info or not info.get("has_vpn"):
        return ""

    pkg = info.get("pkg", "未知")
    sess = info.get("session", "")
    iface = info.get("iface", "tun0")

    lines = [f"手机 VPN 现状：{pkg}"
             + (f"（{sess}）" if sess else "")
             + f"，隧道接口 {iface}，"
             + ("全局模式" if info.get("global") else "分流模式")]

    if info.get("fakeip"):
        lines.append("")
        lines.append("★ 找到症结：手机 DNS 处于 **fake-IP 模式**。")
        lines.append("  表现：域名被解析成 198.18.x.x（RFC 保留段，公网不存在）")
        lines.append("  这会造成一个死结，请注意分辨：")
        lines.append("   · 手机本机访问域名 → 内核知道映射关系 → 正常")
        lines.append("   · 其他 App/设备拿到域名 → 自己解析 → 得到 198.18.x.x")
        lines.append("     → 内核的映射表里没有这条 → 不知怎么路由 → 0 字节")
        lines.append("     → 浏览器 ERR_CONNECTION_CLOSED")
        lines.append("")
        lines.append("  **这不是网络问题，是 DNS 层的 fake-IP 机制。**")
        lines.append("  判据：真实 IP（如 1.1.1.1）能通，域名不通。")
        lines.append("")
        lines.append("  处理（按推荐度）：")
        lines.append("   1) **升级手机端 App 到 v1.1+** —— 新版走 443 端口 DoH")
        lines.append("      拿真实 IP，可绕过 53 端口的 DNS 劫持（已验证有效）；")
        lines.append("   2) 或在 VPN App 里把 DNS 模式从 fake-ip 改成 redir-host")
        lines.append("      （若该 App 提供此开关）；")
        lines.append("   3) 或改用「热点共享」类 VPN（支持 tether 且不用 fake-ip）。")
    else:
        lines.append("  DNS 未处于 fake-IP 状态（域名能解析出真实 IP）。")
        lines.append("  若仍不通，可能是隧道本身没接通，或 VPN 处于「仅本机」模式。")

    return "\n".join(lines)


def vpn_effective(target: ProxyTarget,
                  timeout_s: float = 8.0) -> tuple[str, bool, str]:
    """检查手机端代理链路与 VPN 状态。

    返回 (state, ok, 说明)。state ∈ {"ok", "no_vpn", "broken"}：
      - "ok"     ：国外可访问，VPN 生效
      - "no_vpn" ：国内可访问、国外不可 —— VPN 没开（正常状态，国内能用）
      - "broken" ：**国内也不通** —— 代理/VPN 坏了，设了代理也只会全站报错

    ★ "broken" 必须被调用方当成**禁止启用**的信号：因为这种状态下
    浏览器打开任何网页都是 ERR_CONNECTION_CLOSED。
    """
    if not target:
        return "broken", False, "无代理目标"

    # ---- 国外探测：必须真的拿到数据 ----
    reachable = []
    blocked = []
    for host, port, path in VPN_PROBE_TARGETS:
        got = _http_get_via_proxy(target, host, port, path, timeout_s)
        (reachable if got else blocked).append(host)

    if reachable:
        return "ok", True, f"VPN 已生效（{', '.join(reachable)} 可访问）"

    # ---- 国外不通时，再试国内站点，区分 no_vpn 和 broken ----
    domestic_ok, domestic_msg = _probe_domestic_via_proxy(target, timeout_s)

    if domestic_ok:
        return "no_vpn", False, (
            f"经代理无法访问 {', '.join(blocked)} —— 判断手机端 VPN **没有真正生效**。\n"
            f"  表现：国内站点能开，但页面里嵌的 Google 资源一直转圈，\n"
            f"        看起来就像「整个网络断了」或「搜不了东西」。\n"
            f"  原因：代理只是在转发现有网络，并没有走 VPN 隧道。\n"
            f"  处理：如果想访问国外站点，回手机把 VPN 连上\n"
            f"        （并确认是全局/规则模式把代理进程也覆盖）。\n"
            f"        只访问国内的话，现在这样就能正常用。"
        )

    # ---- 国内国外都不通 → 先查手机 VPN 配置，给出精准诊断 ----
    vinfo = phone_vpn_info()
    diag = phone_vpn_diagnosis(vinfo, target)

    if vinfo.get("has_vpn") and vinfo.get("bypass_lan"):
        # 命中「全局 VPN + 局域网 bypass」这个已知死结，给出针对性说明
        return "broken", False, (
            f"⚠️ 经代理连**国内站点也访问不了**（{domestic_msg}）。\n"
            f"   但请注意：**你的手机本机其实是可以正常上网的**。\n"
            f"   这两个现象不矛盾，原因见下：\n"
            f"\n{diag}"
        )

    return "broken", False, (
        f"⚠️ 经代理连**国内站点也访问不了**（{domestic_msg}）——\n"
        f"   手机端代理/VPN 处于**异常状态**：能建立连接，但不转发数据。\n"
        f"   典型原因：VPN 的隧道设备建起来了、路由规则也挂上了，\n"
        f"             但隧道本身没真正接通，走隧道的流量全进了黑洞；\n"
        f"             或者代理 App 自身卡死（握手正常但不转发）。\n"
        f"   浏览器表现：ERR_CONNECTION_CLOSED（连接建立后立刻被关闭）。\n"
        f"   处理：回手机上把 VPN **断开再重连**，或把代理 App **关掉重开**；\n"
        f"        先在手机浏览器里确认能上网，再回电脑点「一键启动共享」。"
        + (f"\n\n{diag}" if diag else "")
    )


def _http_get_via_proxy(target: ProxyTarget, host: str, port: int,
                        path: str, timeout_s: float) -> bool:
    """经代理发一次真实 GET，收到非空响应才返回 True。

    ★ 这是"隧道是否真的在工作"的唯一可靠判据 ——
    CONNECT 成功只代表代理"答应"了，不代表它真的转发。
    """
    import ssl
    try:
        if target.is_socks:
            if port == 443:
                raw = _socks5_connect_raw(target.host, target.port,
                                          host, port, timeout_s)
                try:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    s = ctx.wrap_socket(raw, server_hostname=host)
                    s.settimeout(timeout_s)
                    s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                              f"Connection: close\r\n\r\n".encode())
                    return bool(s.recv(64))
                finally:
                    try:
                        raw.close()
                    except Exception:
                        pass
            body = _socks5_http_get(target.host, target.port, host, path, timeout_s)
            return bool(body)

        # HTTP 代理
        s = socket.create_connection((target.host, target.port), timeout=timeout_s)
        s.settimeout(timeout_s)
        if port == 443:
            s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\n"
                      f"Host: {host}:{port}\r\n\r\n".encode())
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 8192:
                c = s.recv(1024)
                if not c:
                    break
                buf += c
            if b" 200 " not in buf:
                s.close()
                return False
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                tls = ctx.wrap_socket(s, server_hostname=host)
                tls.settimeout(timeout_s)
                tls.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                            f"Connection: close\r\n\r\n".encode())
                got = bool(tls.recv(64))
                tls.close()
                return got
            except Exception:
                try:
                    s.close()
                except Exception:
                    pass
                return False
        s.sendall(f"GET http://{host}{path} HTTP/1.1\r\nHost: {host}\r\n"
                  f"Connection: close\r\n\r\n".encode())
        data = s.recv(256)
        s.close()
        return bool(data)
    except Exception:
        return False


def _recv_exact(s: socket.socket, n: int, timeout: float) -> bytes:
    """★ 精确读满 n 字节。

    坑：`sock.recv(n)` 在 TCP 下**不保证**返回 n 字节，
    可能少读。少读的字节会污染后续读取，造成"CONNECT 成功却收不到数据"
    的假象 —— 实测踩过，导致 SOCKS5 被误判成 broken。
    """
    s.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _socks5_connect_raw(proxy_host: str, proxy_port: int, dst_host: str,
                        dst_port: int, timeout: float):
    """SOCKS5 CONNECT，返回**原始 socket**（供上面包 TLS 用）。"""
    s = socket.create_connection((proxy_host, proxy_port), timeout)
    s.settimeout(timeout)
    s.sendall(b"\x05\x01\x00")
    if _recv_exact(s, 2, timeout) != b"\x05\x00":
        s.close()
        raise OSError("SOCKS5 协商失败")
    hb = dst_host.encode("idna") if dst_host.isascii() else dst_host.encode()
    s.sendall(bytes([5, 1, 0, 3, len(hb)]) + hb + dst_port.to_bytes(2, "big"))

    head = _recv_exact(s, 4, timeout)
    if len(head) < 4 or head[0] != 0x05 or head[1] != 0x00:
        s.close()
        raise OSError("SOCKS5 CONNECT 被拒")

    # ★ 必须把 BND.ADDR + BND.PORT 全部读掉，否则残留字节会污染后续数据
    atyp = head[3]
    if atyp == 1:
        _recv_exact(s, 4 + 2, timeout)
    elif atyp == 4:
        _recv_exact(s, 16 + 2, timeout)
    elif atyp == 3:
        n = _recv_exact(s, 1, timeout)
        if n:
            _recv_exact(s, n[0] + 2, timeout)
    return s


def _probe_domestic_via_proxy(target: ProxyTarget,
                              timeout_s: float = 6.0) -> tuple[bool, str]:
    """经代理请求一个国内站点，判断"国内是否也不通"。

    这是 v3.1 新增的判据：把「VPN 没开」（国内通）和
    「VPN 开着但隧道坏的」（国内也不通）区分开。
    返回 (是否通, 简述)。
    """
    try:
        if target.is_socks:
            body = _socks5_http_get(target.host, target.port,
                                    "www.baidu.com", "/", timeout_s)
            return (bool(body), "baidu 正常" if body else "baidu 无响应（连接被关闭）")
        s = socket.create_connection((target.host, target.port), timeout=timeout_s)
        s.settimeout(timeout_s)
        s.sendall(b"GET http://www.baidu.com/ HTTP/1.1\r\n"
                  b"Host: www.baidu.com\r\n"
                  b"User-Agent: Mozilla/5.0\r\n"
                  b"Connection: close\r\n\r\n")
        data = s.recv(256)
        s.close()
        if not data:
            return False, "baidu 无响应（连接被关闭）"
        first = data.split(b"\r\n")[0]
        return (b" 200 " in first or b" 301 " in first or b" 302 " in first,
                first.decode("latin-1", "replace")[:60])
    except socket.timeout:
        return False, "baidu 超时"
    except Exception as e:
        return False, f"baidu {type(e).__name__}: {e}"


def phone_permissions() -> list[dict]:
    """检查手机上代理 App 的后台运行相关权限（防"切后台就失效"）。

    手机 App 一旦被系统冻结（省电策略/后台限制），代理会静默失效 ——
    电脑端表现为端口还在但连不通，用户会以为"程序坏了"。

    检查项：
      - 是否在电池优化白名单（dumpsys deviceidle whitelist）
      - 待机桶等级（am get-standby-bucket，5=Active 最好，<=40 容易被限制）

    返回 [{'package','whitelisted','bucket','level'}]，无 adb 时返回 []。
    调用方应结合 phone_permissions_checked() 判断是"查了没问题"还是
    "根本没查成" —— 后者不能当成绿灯。
    """
    import re

    if not find_adb():
        return []

    pkgs = ["com.gorillasoftware.everyproxy", "com.vpnshare.app"]
    result: list[dict] = []

    # 白名单。输出格式是逗号分隔的三列：
    #   user,com.vpnshare.app,10653
    #   system-excidle,com.android.vending,10108
    # 包名在**第二列**（不要取最后一列，那是 uid）。
    whitelist_raw = _run_cmd(["adb", "shell", "dumpsys", "deviceidle", "whitelist"],
                             timeout=10.0)
    whitelisted = set()
    for line in whitelist_raw.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 2 and parts[1]:
            whitelisted.add(parts[1])

    bucket_name = {
        5: "Active", 10: "Working Set", 20: "Frequent",
        30: "Rare", 40: "Restricted", 45: "Never", 50: "Exempted",
    }

    for pkg in pkgs:
        entry = {
            "package": pkg,
            "whitelisted": pkg in whitelisted,
            "bucket": None,
            "level": "",
        }
        try:
            out = _run_cmd(["adb", "shell", "am", "get-standby-bucket", pkg],
                           timeout=8.0).strip()
            m = re.search(r"(\d+)", out)
            if m:
                b = int(m.group(1))
                entry["bucket"] = b
                entry["level"] = bucket_name.get(b, str(b))
        except Exception:
            pass
        result.append(entry)

    return result


def phone_permission_hint(perms: list[dict]) -> str:
    """把权限检查结果转成给用户的建议。

    三种情况要区分清楚（这是"静默漏检"的防线）：
      - 没拿到任何数据 → 明确告诉用户"没查成"，并给出自查步骤
      - 查到问题       → 列出具体项 + 处理办法
      - 查到且正常     → 明确报平安
    """
    if not perms:
        # 查不到 ≠ 没问题。必须显式说明，否则用户以为一切正常。
        if not find_adb():
            return ("  后台权限检查：**没能执行**（未找到 adb）。\n"
                    "    这不代表权限没问题，只是无法自动确认。\n"
                    "    请手动确认：手机「设置 → 应用 → Every Proxy / VPN共享\n"
                    "    → 省电策略」设为「无限制」，并允许「自启动」「后台运行」。")
        return ("  后台权限检查：**没能执行**（adb 没连上手机）。\n"
                "    这不代表权限没问题。请确认 USB 连接 + 已授权 USB 调试，\n"
                "    或手动检查「设置 → 应用 → 省电策略 → 无限制」。")

    lines = []
    bad = []
    for p in perms:
        pkg = p["package"]
        flags = []
        if not p["whitelisted"]:
            flags.append("不在电池优化白名单")
            bad.append(pkg)
        b = p.get("bucket")
        if b is not None and b >= 40:
            flags.append(f"待机桶={p.get('level')}（容易被冻结）")
            bad.append(pkg)
        if flags:
            lines.append(f"  {pkg}: " + "、".join(flags))
    if not lines:
        detail = "、".join(
            f"{p['package'].split('.')[-1]}({p.get('level') or '?'})"
            for p in perms
        )
        return f"  后台权限检查：全部正常（{detail}）。"
    head = "  后台权限检查发现问题（切后台后代理可能静默失效）：\n"
    tail = ("\n  处理：手机「设置 → 应用 → 找到该 App → 省电策略」设为「无限制」，\n"
            "        并允许「自启动」和「后台运行」。")
    return head + "\n".join(lines) + tail


def phone_app_running(pkg: str = "") -> Optional[bool]:
    """检查手机上代理 App 的进程是否还活着。

    用户担心"App 贴到后台就不工作了"。这个函数直接给答案：
      True  = 进程存在（pidof 有输出）
      False = 进程不存在（被系统杀了 / 没启动）
      None  = 查不了（adb 不可用或设备未连接）

    返回 None 时调用方不要当作"正常"，要明确告知用户"无法确认"。
    """
    if not find_adb():
        return None

    targets = [pkg] if pkg else ["com.vpnshare.app", "com.gorillasoftware.everyproxy"]
    for p in targets:
        try:
            out = _run_cmd(["adb", "shell", "pidof", p], timeout=6.0).strip()
        except Exception:
            return None
        if out and out.split()[0].isdigit():
            return True
    # 所有目标包都没进程
    return False


def phone_app_status_hint(pkg_running: Optional[bool]) -> str:
    """把进程检查结果转成给用户的说明。"""
    if pkg_running is None:
        return ("  手机 App 运行状态：**无法确认**（adb 未连接）。\n"
                "    建议手动看一眼手机，确认代理 App 还在前台/通知栏里活着。")
    if pkg_running:
        return "  手机 App 运行状态：正常（进程存活，未被系统杀掉）。"
    return ("  手机 App 运行状态：**未运行**！代理 App 的进程已经不在了。\n"
            "    → 去手机把 App 重新打开并点开代理开关，再回来重试。\n"
            "    → 若反复被系统杀掉，请在「设置 → 应用 → 省电策略」设为「无限制」，\n"
            "       并允许「自启动」「后台运行」。")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """以管理员身份重启自身（UAC 提权）。成功发起提权返回 True。"""
    import sys
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            params = ""
        else:
            exe = sys.executable
            params = " ".join(f'"{a}"' for a in sys.argv)

        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, None, 1
        )
        # ShellExecuteW 返回值 > 32 表示成功
        return int(rc) > 32
    except Exception:
        return False

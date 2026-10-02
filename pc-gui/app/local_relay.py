# -*- coding: utf-8 -*-
"""
本机代理中转层（Local Relay）
============================

为什么需要它
------------
Windows 的系统代理是「全有或全无」：一旦设成 `手机IP:端口`，浏览器会把
**所有**请求丢给手机。手机没连 VPN 时，Google / YouTube 这类目标会**挂住**
（不是快速拒绝，而是 TCP 连不通一直等到超时），浏览器只能干等 —— 表现就是
「网页一直转圈，跟断网一样」。

解决办法：在电脑本机跑一个小小的代理，让浏览器连**本机**
（快、稳、可控），由它去和手机代理打交道：

    浏览器 ──► 127.0.0.1:8888 ──► 手机代理(手机IP:端口) ──► 手机网络出口
                    │
                    └─ 在这里做「快速失败」：
                       连不上/超时 → 立刻断开，不让浏览器干等

这样即便手机没 VPN、国外站点打不开，浏览器也会**快速失败**而不是卡死，
国内站点该多快还是多快。

设计要点
--------
- 只监听 127.0.0.1，不对局域网暴露（安全）
- 支持 HTTP 明文转发 + HTTPS 的 CONNECT 隧道
- 上游（手机代理）支持 HTTP 和 SOCKS5 两种协议
- 连接超时可配，默认 3 秒 —— 这是「不卡死」的核心
- 多线程处理并发，每连接一线程（足够家用场景）
"""

from __future__ import annotations

import select
import socket
import threading
import time
from typing import Callable


# ---------------------------------------------------------------------------
# SOCKS5 客户端（连上游用，不依赖第三方库）
# ---------------------------------------------------------------------------

def _socks5_connect(proxy_host: str, proxy_port: int, dst_host: str,
                    dst_port: int, timeout: float) -> socket.socket:
    """通过 SOCKS5 代理建立到 dst 的连接，返回已连通的 socket。"""
    s = socket.create_connection((proxy_host, proxy_port), timeout)
    s.settimeout(timeout)

    # 1) 方法协商：只报「无认证」
    s.sendall(b"\x05\x01\x00")
    resp = s.recv(2)
    if len(resp) != 2 or resp[0] != 0x05:
        s.close()
        raise OSError("SOCKS5 方法协商失败")
    if resp[1] == 0xFF:
        s.close()
        raise OSError("SOCKS5 代理不接受无认证")

    # 2) CONNECT 请求
    if _is_ip_literal(dst_host):
        import ipaddress
        ip = ipaddress.ip_address(dst_host)
        if ip.version == 4:
            addr = b"\x01" + ip.packed
        else:
            addr = b"\x04" + ip.packed
    else:
        h = dst_host.encode("idna") if dst_host.isascii() else dst_host.encode("utf-8")
        addr = b"\x03" + bytes([len(h)]) + h
    s.sendall(b"\x05\x01\x00" + addr + dst_port.to_bytes(2, "big"))

    # 3) 读应答（首 4 字节后再按 ATYP 读掉地址）
    rep = s.recv(4)
    if len(rep) < 2 or rep[0] != 0x05:
        s.close()
        raise OSError("SOCKS5 CONNECT 无应答")
    if rep[1] != 0x00:
        s.close()
        raise OSError(f"SOCKS5 CONNECT 被拒 (0x{rep[1]:02x})")
    atyp = rep[3] if len(rep) >= 4 else 0x01
    if atyp == 0x01:
        s.recv(4 + 2)
    elif atyp == 0x04:
        s.recv(16 + 2)
    elif atyp == 0x03:
        n = s.recv(1)
        if n:
            s.recv(n[0] + 2)
    return s


def _is_ip_literal(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _connect_upstream(upstream_scheme: str, up_host: str, up_port: int,
                      dst_host: str, dst_port: int,
                      timeout: float) -> socket.socket:
    """按上游协议连到目标（HTTP 代理用 CONNECT，SOCKS5 走原生）。"""
    # ★ 同时接受 'socks' 和 'socks5'：外部调用方两种写法都有，
    #   只认 'socks' 会让 SOCKS5 上游被当成 HTTP 代理 → 全部失败。
    if upstream_scheme in ("socks", "socks5", "socks4"):
        return _socks5_connect(up_host, up_port, dst_host, dst_port, timeout)

    # HTTP 代理：用 CONNECT 建立隧道（HTTPS 和裸 TCP 都靠它）
    s = socket.create_connection((up_host, up_port), timeout)
    s.settimeout(timeout)
    req = (f"CONNECT {dst_host}:{dst_port} HTTP/1.1\r\n"
           f"Host: {dst_host}:{dst_port}\r\n"
           f"Proxy-Connection: Keep-Alive\r\n\r\n")
    s.sendall(req.encode("latin-1"))
    # 读应答头（直到空行）
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 8192:
        chunk = s.recv(1024)
        if not chunk:
            break
        buf += chunk
    first = buf.split(b"\r\n", 1)[0]
    parts = first.split(b" ", 2)
    code = parts[1] if len(parts) >= 2 else b""
    if code not in (b"200", b"201"):
        s.close()
        raise OSError(f"上游 HTTP 代理拒绝 CONNECT ({code.decode('latin-1', 'replace')})")
    return s


# ---------------------------------------------------------------------------
# 本地中转服务
# ---------------------------------------------------------------------------

class LocalRelay:
    """把浏览器请求转发给手机代理的本机中转服务。"""

    def __init__(self, up_host: str, up_port: int, up_scheme: str = "http",
                 listen_host: str = "127.0.0.1", listen_port: int = 8888,
                 connect_timeout: float = 3.0,
                 log: Callable[[str], None] = print):
        self.up_host = up_host
        self.up_port = up_port
        self.up_scheme = up_scheme
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.connect_timeout = connect_timeout
        self.log = log

        self._srv: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False

        # 简单统计，便于在界面上显示
        self.stats = {"ok": 0, "fail": 0, "active": 0}

        # ★ v3.4：双向流量累计（字节）。用 Lock 保护，
        # 因为 _pump 在多个连接线程里并发累加，而 GUI 线程会读它算速率。
        self._byte_lock = threading.Lock()
        self._bytes_up = 0        # 上行：浏览器 → 手机（请求方向）
        self._bytes_down = 0      # 下行：手机 → 浏览器（响应方向）

    # ---------------------------------------------------------------- 流量统计

    def _add_bytes(self, up: int, down: int) -> None:
        if up:
            with self._byte_lock:
                self._bytes_up += up
        if down:
            with self._byte_lock:
                self._bytes_down += down

    def traffic(self) -> tuple[int, int]:
        """返回 (上行累计, 下行累计)，单位字节。线程安全。"""
        with self._byte_lock:
            return self._bytes_up, self._bytes_down

    def reset_traffic(self) -> None:
        with self._byte_lock:
            self._bytes_up = 0
            self._bytes_down = 0

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # ★ 注意：这里**不能**用 SO_REUSEADDR。
        # 在 Windows 上它允许多个 socket 绑定同一 (addr, port)，于是"端口被占"
        # 时 bind 不会报错，中转其实没在工作，而系统代理已被指向这个死端口。
        # 实测验证过这个坑，所以保持默认（不设 REUSEADDR），让冲突直接暴露。
        srv.bind((self.listen_host, self.listen_port))
        srv.listen(128)
        srv.settimeout(0.5)

        # ★ 二次自检：确认真的在监听（防上面那种"静默绑定成功但没生效"）
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.8)
        try:
            probe.connect((self.listen_host, self.listen_port))
        except OSError as e:
            srv.close()
            raise OSError(
                f"中转端口 {self.listen_host}:{self.listen_port} 无法监听"
                f"（可能被其他程序占用）：{e}"
            )
        finally:
            probe.close()

        self._srv = srv
        self.running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.log(f"[中转] 已启动：{self.listen_host}:{self.listen_port} "
                 f"→ {self.up_scheme}://{self.up_host}:{self.up_port}")

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        if self._srv:
            try:
                self._srv.close()
            except Exception:
                pass
            self._srv = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.log("[中转] 已停止")

    @property
    def local_url(self) -> str:
        return f"http://{self.listen_host}:{self.listen_port}"

    # ---------------------------------------------------------------- 主循环

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr),
                             daemon=True).start()

    def _handle(self, conn: socket.socket, addr) -> None:
        self.stats["active"] += 1
        try:
            conn.settimeout(self.connect_timeout)
            # 读请求首行
            head = self._read_head(conn)
            if not head:
                return
            first_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = first_line.split()
            if len(parts) < 3:
                return
            method, target, _ver = parts[0], parts[1], parts[2]

            if method.upper() == "CONNECT":
                self._do_connect(conn, target)
            else:
                self._do_forward(conn, head, method, target)
        except Exception as e:
            self.stats["fail"] += 1
            self._fail(conn, 502, f"{type(e).__name__}: {e}")
        finally:
            self.stats["active"] -= 1
            try:
                conn.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- 请求处理

    @staticmethod
    def _read_head(conn: socket.socket, limit: int = 65536) -> bytes:
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < limit:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            # CONNECT 只要首行就够，不必等完整头
            if buf.startswith(b"CONNECT ") and b"\r\n" in buf:
                break
        return buf

    def _do_connect(self, conn: socket.socket, target: str) -> None:
        """处理 HTTPS 的 CONNECT 隧道。"""
        if ":" not in target:
            self._fail(conn, 400, "CONNECT 目标缺少端口")
            return
        host, _, port_s = target.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            self._fail(conn, 400, "CONNECT 端口非法")
            return

        t0 = time.time()
        try:
            up = _connect_upstream(self.up_scheme, self.up_host, self.up_port,
                                   host, port, self.connect_timeout)
        except Exception as e:
            self.stats["fail"] += 1
            # ★ 快速失败：立刻回 502，浏览器马上得到「连不上」而不是干等
            self.log(f"[中转] ✗ {host}:{port} 连不上（{time.time()-t0:.1f}s）"
                     f" — {type(e).__name__}: {e}")
            self._fail(conn, 502, f"无法通过手机代理连到 {host}:{port}")
            return

        self.stats["ok"] += 1
        try:
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except Exception:
            up.close()
            return
        # 双向泵数据
        self._pump(conn, up)

    def _do_forward(self, conn: socket.socket, head: bytes,
                    method: str, url: str) -> None:
        """处理普通 HTTP 请求（明文）。"""
        # 解析出 host:port
        host, port, path = self._parse_url(url, head)
        if not host:
            self._fail(conn, 400, "无法解析请求目标")
            return

        t0 = time.time()
        try:
            up = _connect_upstream(self.up_scheme, self.up_host, self.up_port,
                                   host, port, self.connect_timeout)
        except Exception as e:
            self.stats["fail"] += 1
            self.log(f"[中转] ✗ {method} {host}:{port} 连不上"
                     f"（{time.time()-t0:.1f}s） — {type(e).__name__}: {e}")
            self._fail(conn, 502, f"无法通过手机代理连到 {host}:{port}")
            return

        self.stats["ok"] += 1
        # 把请求行改成 origin-form，去掉绝对 URL 和 Proxy-Connection
        out = self._rewrite_request(head, host, port, path)
        try:
            up.sendall(out)
            self._pump(conn, up)
        except Exception:
            pass
        finally:
            try:
                up.close()
            except Exception:
                pass

    @staticmethod
    def _parse_url(url: str, head: bytes) -> tuple[str, int, str]:
        """从请求行 / Host 头解析出 host、port、path。"""
        if url.lower().startswith("http://"):
            rest = url[7:]
            hostport, _, path = rest.partition("/")
            path = "/" + path if path else "/"
        else:
            hostport = ""
            path = url
            # 从 Host 头拿
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"host:"):
                    hostport = line[5:].strip().decode("latin-1")
                    break
        if not hostport:
            return "", 0, path
        if ":" in hostport:
            h, _, p = hostport.rpartition(":")
            try:
                return h, int(p), path
            except ValueError:
                pass
        return hostport, 80, path

    @staticmethod
    def _rewrite_request(head: bytes, host: str, port: int, path: str) -> bytes:
        """把代理格式的请求改写成源服务器格式。"""
        lines = head.split(b"\r\n")
        if lines:
            parts = lines[0].split(b" ")
            if len(parts) >= 3:
                lines[0] = b" ".join([parts[0], path.encode("latin-1"), parts[2]])
        out = []
        for ln in lines:
            low = ln.lower()
            if low.startswith(b"proxy-connection:"):
                continue
            out.append(ln)
        return b"\r\n".join(out)

    # ---------------------------------------------------------------- 数据泵

    def _pump(self, a: socket.socket, b: socket.socket) -> None:
        """在 a、b 之间双向转发，任一方向关闭即结束。

        改成实例方法（原为 staticmethod）是为了能累加流量统计 —— v3.4 新增。
        """
        socks = [a, b]
        try:
            a.setblocking(False)
            b.setblocking(False)
        except Exception:
            pass
        idle = 0.0
        try:
            while True:
                r, _, x = select.select(socks, [], socks, 1.0)
                if x:
                    break
                if not r:
                    idle += 1.0
                    if idle > 120:      # 两分钟没动静就收工
                        break
                    continue
                idle = 0.0
                done = False
                for src in r:
                    dst = b if src is a else a
                    try:
                        data = src.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except Exception:
                        done = True
                        break
                    if not data:
                        # 半关闭：把 FIN 透过去，让另一端正常收尾
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except Exception:
                            pass
                        done = True
                        break
                    try:
                        dst.sendall(data)
                    except Exception:
                        done = True
                        break
                    # ★ v3.4：src is a（浏览器侧）读到的算上行，否则算下行
                    if src is a:
                        self._add_bytes(len(data), 0)
                    else:
                        self._add_bytes(0, len(data))
                if done:
                    break
        finally:
            for s in (a, b):
                try:
                    s.setblocking(True)
                except Exception:
                    pass

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _fail(conn: socket.socket, code: int, msg: str) -> None:
        reason = {400: "Bad Request", 502: "Bad Gateway",
                  504: "Gateway Timeout"}.get(code, "Error")
        body = (f"<html><body><h3>VpnShare 本机中转</h3>"
                f"<p>{code} {reason}</p><p>{msg}</p>"
                f"<p>这是<b>快速失败</b>提示：手机代理连不上该站点，"
                f"没有让你干等。国内网站不受影响。</p></body></html>")
        raw = (f"HTTP/1.1 {code} {reason}\r\n"
               f"Content-Type: text/html; charset=utf-8\r\n"
               f"Content-Length: {len(body.encode('utf-8'))}\r\n"
               f"Connection: close\r\n\r\n").encode("latin-1") + body.encode("utf-8")
        try:
            conn.sendall(raw)
        except Exception:
            pass

#!/data/data/com.termux/files/usr/bin/bash
# =====================================================================
#  VPN Share for Android (Termux) - 一键启动脚本
#  把手机本地的 HTTP/SOCKS5 代理 + PAC 服务器开起来，
#  让同热点下的电脑可以直接使用（配合手机上已开的 VPN）。
#
#  免 root、免编译。运行前请先安装 Termux 并执行：
#      pkg update -y && pkg install -y python
#
#  用法：
#      bash vpnshare.sh              # 启动
#      bash vpnshare.sh stop         # 停止
#      bash vpnshare.sh status       # 查看状态
# =====================================================================

set -u

# ---------------- 配置区 ----------------
HTTP_PORT="${HTTP_PORT:-8080}"     # HTTP 代理端口
SOCKS_PORT="${SOCKS_PORT:-1080}"   # SOCKS5 代理端口（不启用可忽略）
PAC_PORT="${PAC_PORT:-8080}"       # PAC 服务端口（跟 HTTP 复用同一端口，走 /proxy.pac 路径）
ENABLE_SOCKS="${ENABLE_SOCKS:-1}"
PROXY_USER="${PROXY_USER:-}"       # 留空 = 不鉴权（局域网内使用）
PROXY_PASS="${PROXY_PASS:-}"
# ----------------------------------------

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SELF_DIR/.vpnshare.pid"
LOG_FILE="$SELF_DIR/vpnshare.log"
PY_BIN="$(command -v python3 || command -v python || true)"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'
YELLOW='\033[1;33m'; GRAY='\033[0;90m'; NC='\033[0m'

say()  { printf "%b\n" "$1"; }
ok()   { printf "${GREEN}[ OK ]${NC} %s\n" "$1"; }
info() { printf "${CYAN}[ .. ]${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}[ !! ]${NC} %s\n" "$1"; }
bad()  { printf "${RED}[FAIL]${NC} %s\n" "$1"; }

# ---------------- 获取本机局域网 IP ----------------
get_lan_ip() {
    local ip=""
    # Termux 推荐方式
    if command -v ifconfig >/dev/null 2>&1; then
        ip=$(ifconfig 2>/dev/null | awk '/wlan0|ap0|swlan0/{f=1} f&&/inet /{print $2; exit}')
    fi
    if [ -z "$ip" ]; then
        ip=$(ip -4 addr show 2>/dev/null | awk '/wlan0|ap0|swlan0/{f=1} f&&/inet /{gsub(/\/.*/,"",$2); print $2; exit}')
    fi
    # 兜底：任何非 127 的 IPv4
    if [ -z "$ip" ]; then
        ip=$(ifconfig 2>/dev/null | awk '/inet /{gsub(/addr:/,"",$2); if($2!~/^127\./) {print $2; exit}}')
    fi
    if [ -z "$ip" ]; then
        ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
    fi
    echo "$ip"
}

# ---------------- 停止 ----------------
do_stop() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            sleep 1
            kill -9 "$pid" 2>/dev/null
            ok "已停止代理服务 (PID $pid)"
        else
            warn "PID 文件存在但进程已不在，清理掉"
        fi
        rm -f "$PID_FILE"
    else
        warn "没有正在运行的代理服务"
    fi
}

# ---------------- 状态 ----------------
do_status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
        local ip
        ip=$(get_lan_ip)
        ok "代理服务运行中 (PID $(cat "$PID_FILE"))"
        say "  HTTP 代理 : http://$ip:$HTTP_PORT"
        say "  PAC 脚本  : http://$ip:$PAC_PORT/proxy.pac"
        [ "$ENABLE_SOCKS" = "1" ] && say "  SOCKS5    : $ip:$SOCKS_PORT"
    else
        warn "代理服务未运行"
    fi
}

case "${1:-start}" in
    stop)   do_stop; exit 0 ;;
    status) do_status; exit 0 ;;
    restart) do_stop; sleep 1 ;;
esac

# ---------------- Python 依赖检查 ----------------
if [ -z "$PY_BIN" ]; then
    bad "未找到 Python，请先执行： pkg install -y python"
    exit 1
fi
say ""
say "============= VPN Share (Android/Termux) ============="
say ""
info "Python: $PY_BIN ($($PY_BIN --version 2>&1))"

# ---------------- 写代理服务程序 ----------------
PROXY_PY="$SELF_DIR/proxyserver.py"

cat > "$PROXY_PY" <<'PYEOF'
#!/usr/bin/env python3
"""
一个极简的 HTTP/HTTPS 代理 + SOCKS5 代理 + PAC 服务器。
监听 0.0.0.0，允许局域网内其他设备（电脑）通过它上网。

当手机同时开着 VPN 时，本进程发起的 outbound 连接会走手机的
VPN 隧道（前提：系统 VPN 是全局模式），于是电脑也共享了该 VPN。
"""
import os, sys, socket, threading, select, base64, struct, time

HTTP_PORT   = int(os.environ.get("HTTP_PORT", "8080"))
SOCKS_PORT  = int(os.environ.get("SOCKS_PORT", "1080"))
ENABLE_SOCKS= os.environ.get("ENABLE_SOCKS", "1") == "1"
PROXY_USER  = os.environ.get("PROXY_USER", "")
PROXY_PASS  = os.environ.get("PROXY_PASS", "")
LAN_IP      = os.environ.get("LAN_IP", "127.0.0.1")

BUF = 65536
socket.setdefaulttimeout(None)

def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

# ---------- PAC 文件 ----------
def pac_script():
    chain = []
    if ENABLE_SOCKS:
        chain.append(f"SOCKS5 {LAN_IP}:{SOCKS_PORT}")
    chain.append(f"PROXY {LAN_IP}:{HTTP_PORT}")
    chain.append("DIRECT")
    joined = "; ".join(chain)
    return (
        "function FindProxyForURL(url, host) {\n"
        f'    return "{joined}";\n'
        "}\n"
    )

# ---------- 工具 ----------
def pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a], [], [], 60)
            if not r:
                continue
            data = a.recv(BUF)
            if not data:
                break
            b.sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try: s.shutdown(socket.SHUT_RDWR)
            except Exception: pass

def connect_target(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.connect((host, port))
    return s

def check_auth(header):
    if not PROXY_USER:
        return True
    for line in header.split("\r\n"):
        if line.lower().startswith("proxy-authorization:"):
            token = line.split(" ", 2)[-1].strip()
            try:
                raw = base64.b64decode(token).decode()
                u, p = raw.split(":", 1)
                return u == PROXY_USER and p == PROXY_PASS
            except Exception:
                return False
    return False

# ---------- HTTP 代理 ----------
def handle_http(client, addr):
    try:
        client.settimeout(30)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(BUF)
            if not chunk:
                return
            data += chunk
            if len(data) > 65536:
                return

        header, _, rest = data.partition(b"\r\n\r\n")
        text = header.decode("latin-1", "replace")
        lines = text.split("\r\n")
        parts = lines[0].split(" ", 2)
        if len(parts) < 2:
            return
        method, target = parts[0], parts[1]

        # PAC 请求
        if method.upper() == "GET" and target.split("?")[0].rstrip("/").endswith("proxy.pac"):
            body = pac_script().encode()
            resp = (b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/x-ns-proxy-autoconfig\r\n"
                    b"Cache-Control: no-store\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
            client.sendall(resp + body)
            return

        if not check_auth(text):
            client.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                           b'Proxy-Authenticate: Basic realm="VpnShare"\r\n'
                           b"Connection: close\r\n\r\n")
            return

        if method.upper() == "CONNECT":
            host, _, port = target.partition(":")
            port = int(port or 443)
            try:
                remote = connect_target(host, port)
            except Exception as e:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                log("CONNECT fail", target, e)
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            log(f"CONNECT {target} <- {addr[0]}")
            client.settimeout(None)
            t = threading.Thread(target=pipe, args=(client, remote), daemon=True)
            t.start()
            pipe(remote, client)
        else:
            # 普通 HTTP：target 是完整 URL
            from urllib.parse import urlparse
            u = urlparse(target)
            host = u.hostname
            port = u.port or 80
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            try:
                remote = connect_target(host, port)
            except Exception as e:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            new_lines = [f"{method} {path} HTTP/1.1"] + lines[1:]
            out = ("\r\n".join(new_lines) + "\r\n\r\n").encode("latin-1")
            remote.sendall(out + rest)
            log(f"GET {host}{path} <- {addr[0]}")
            client.settimeout(None)
            t = threading.Thread(target=pipe, args=(client, remote), daemon=True)
            t.start()
            pipe(remote, client)
    except Exception as e:
        log("HTTP err", e)
    finally:
        try: client.close()
        except Exception: pass

# ---------- SOCKS5 代理 ----------
def handle_socks(client, addr):
    try:
        client.settimeout(30)
        hdr = client.recv(2)
        if len(hdr) < 2 or hdr[0] != 5:
            return
        nmethods = hdr[1]
        methods = client.recv(nmethods) if nmethods else b""
        if PROXY_USER:
            if 2 not in methods:
                client.sendall(b"\x05\xff")
                return
            client.sendall(b"\x05\x02")
            # 用户名密码认证
            v = client.recv(1)
            ulen = client.recv(1)[0]
            uname = client.recv(ulen).decode()
            plen = client.recv(1)[0]
            passwd = client.recv(plen).decode()
            if uname != PROXY_USER or passwd != PROXY_PASS:
                client.sendall(b"\x01\x01")
                return
            client.sendall(b"\x01\x00")
        else:
            client.sendall(b"\x05\x00")

        req = client.recv(4)
        if len(req) < 4:
            return
        _, cmd, _, atyp = req
        if atyp == 1:
            host = socket.inet_ntoa(client.recv(4))
        elif atyp == 3:
            ln = client.recv(1)[0]
            host = client.recv(ln).decode()
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, client.recv(16))
        else:
            return
        port = struct.unpack(">H", client.recv(2))[0]

        if cmd != 1:  # 只支持 CONNECT
            client.sendall(b"\x05\x07\x00\x01" + b"\x00"*6)
            return
        try:
            remote = connect_target(host, port)
        except Exception as e:
            client.sendall(b"\x05\x05\x00\x01" + b"\x00"*6)
            log("SOCKS fail", host, port, e)
            return
        client.sendall(b"\x05\x00\x00\x01" + b"\x00"*6)
        log(f"SOCKS5 {host}:{port} <- {addr[0]}")
        client.settimeout(None)
        t = threading.Thread(target=pipe, args=(client, remote), daemon=True)
        t.start()
        pipe(remote, client)
    except Exception as e:
        log("SOCKS err", e)
    finally:
        try: client.close()
        except Exception: pass

# ---------- 监听 ----------
def serve(port, handler, name):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(128)
    log(f"{name} listening on 0.0.0.0:{port}")
    while True:
        try:
            c, a = srv.accept()
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=handler, args=(c, a), daemon=True).start()
        except Exception as e:
            log(f"{name} accept err", e)

def main():
    log("=" * 46)
    log("VPN Share proxy starting")
    log(f"  LAN IP      : {LAN_IP}")
    log(f"  HTTP proxy  : {HTTP_PORT}")
    log(f"  PAC         : http://{LAN_IP}:{HTTP_PORT}/proxy.pac")
    if ENABLE_SOCKS:
        log(f"  SOCKS5      : {SOCKS_PORT}")
    log(f"  Auth        : {'yes' if PROXY_USER else 'no'}")
    log("=" * 46)
    if ENABLE_SOCKS:
        threading.Thread(target=serve, args=(SOCKS_PORT, handle_socks, "SOCKS5"), daemon=True).start()
    serve(HTTP_PORT, handle_http, "HTTP")

if __name__ == "__main__":
    main()
PYEOF

# ---------------- 启动 ----------------
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
    warn "代理服务已在运行 (PID $(cat "$PID_FILE"))，先执行 stop"
    exit 1
fi

LAN_IP="$(get_lan_ip)"
if [ -z "$LAN_IP" ]; then
    bad "无法获取局域网 IP，请确认已连接 WiFi/热点"
    exit 1
fi
info "本机局域网 IP: $LAN_IP"

export HTTP_PORT SOCKS_PORT PAC_PORT ENABLE_SOCKS PROXY_USER PROXY_PASS LAN_IP

nohup "$PY_BIN" "$PROXY_PY" >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

sleep 1.5
if kill -0 "$NEW_PID" 2>/dev/null; then
    ok "代理服务已启动 (PID $NEW_PID)"
    say ""
    say "-----------------------------------------------"
    say "  HTTP 代理 : http://$LAN_IP:$HTTP_PORT"
    say "  SOCKS5    : $LAN_IP:$SOCKS_PORT"
    say "  PAC 脚本  : http://$LAN_IP:$HTTP_PORT/proxy.pac"
    say "-----------------------------------------------"
    say ""
    say "电脑端请填写：  地址 $LAN_IP   端口 $HTTP_PORT"
    say "或使用 PAC：    http://$LAN_IP:$HTTP_PORT/proxy.pac"
    say ""
    warn "注意：手机需已连接 VPN，且 VPN 为全局模式，电脑才共享得到。"
    say ""
    info "停止： bash $0 stop"
    info "日志： $LOG_FILE"
else
    bad "启动失败，日志如下："
    tail -20 "$LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi

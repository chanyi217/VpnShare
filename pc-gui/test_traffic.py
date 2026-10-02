# -*- coding: utf-8 -*-
"""验证 LocalRelay 的双向流量统计（v3.4）

思路：起一个**真正的假 HTTP 代理**当上游 —— 它支持 CONNECT，
收到 CONNECT 后回 200，然后在同一连接上双向转发数据。
中继连上它之后，我们再从客户端灌入已知大小的数据，
最后检查 relay.traffic() 统计的字节数是否吻合。

注意：第一次写这个测试时我把假上游写成了普通 HTTP 服务器，
结果它把 CONNECT 当普通请求处理，导致测出来的数据不可信。
现在按真实代理语义重写。
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

from local_relay import LocalRelay  # noqa: E402
from traffic_chart import TrafficChart, fmt_rate, _nice_ceil, MIN_Y_MAX  # noqa: E402

CLIENT_UP = 8 * 1024          # 客户端发给上游的字节数
SERVER_DOWN = 300 * 1024      # 上游回给客户端的字节数


def fake_http_proxy(port_holder, ready_evt, stop_evt):
    """一个只支持 CONNECT 的极简 HTTP 代理。

    收到 CONNECT 后回 200，然后：
      - 把客户端发来的数据全部丢弃（只计数）
      - 主动推送 SERVER_DOWN 字节给客户端
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port_holder.append(srv.getsockname()[1])
    srv.settimeout(0.5)
    ready_evt.set()

    while not stop_evt.is_set():
        try:
            c, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_handle_proxy_conn, args=(c,), daemon=True).start()
    srv.close()


def _handle_proxy_conn(c: socket.socket):
    try:
        c.settimeout(3.0)
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = c.recv(4096)
            if not d:
                return
            buf += d
        if not buf.startswith(b"CONNECT"):
            c.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

        # 起一个线程把客户端发来的数据读掉（丢弃）
        def drain():
            try:
                c.settimeout(5.0)
                while True:
                    d = c.recv(65536)
                    if not d:
                        break
            except Exception:
                pass

        threading.Thread(target=drain, daemon=True).start()

        # 主动推送下行数据
        payload = b"B" * 8192
        sent = 0
        while sent < SERVER_DOWN:
            n = min(len(payload), SERVER_DOWN - sent)
            c.sendall(payload[:n])
            sent += n
        time.sleep(0.5)      # 留出时间让中继把数据搬完并记账
    except Exception:
        pass
    finally:
        try:
            c.close()
        except Exception:
            pass


def main():
    holder, ready, stop = [], threading.Event(), threading.Event()
    threading.Thread(target=fake_http_proxy, args=(holder, ready, stop), daemon=True).start()
    ready.wait(3)
    up_port = holder[0]
    print(f"[1] 假 HTTP 代理已起在 127.0.0.1:{up_port}")

    relay = LocalRelay(
        up_host="127.0.0.1", up_port=up_port, up_scheme="http",
        listen_host="127.0.0.1", listen_port=18910,
        connect_timeout=5.0, log=lambda m: None,
    )
    relay.start()
    print(f"[2] 中转已启动 127.0.0.1:18910 → 127.0.0.1:{up_port}")

    up0, down0 = relay.traffic()
    assert (up0, down0) == (0, 0), f"基线应为 0，实际 {(up0, down0)}"
    print(f"[3] 基线字节：up={up0} down={down0}")

    # 客户端发起 CONNECT 隧道，然后灌入 CLIENT_UP 字节
    c = socket.create_connection(("127.0.0.1", 18910), 5)
    c.settimeout(8)
    c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n"
              b"Host: example.com:443\r\n\r\n")

    # 等 200 Established
    head = b""
    while b"\r\n\r\n" not in head:
        d = c.recv(1024)
        if not d:
            break
        head += d
    print(f"[4] 隧道应答：{head.split(chr(13).encode())[0].decode()}")

    # 灌上行数据
    c.sendall(b"U" * CLIENT_UP)
    print(f"[5] 客户端已发送上行 {CLIENT_UP} 字节")

    # 收下行数据
    got = 0
    while got < SERVER_DOWN:
        try:
            d = c.recv(65536)
        except socket.timeout:
            break
        if not d:
            break
        got += len(d)
    print(f"[6] 客户端收到下行 {got} 字节")
    c.close()

    time.sleep(0.6)      # 等统计落账

    up1, down1 = relay.traffic()
    d_up, d_down = up1 - up0, down1 - down0
    print(f"[7] 统计增量：up={d_up} down={d_down}")

    print()
    ok = True
    # 上行：中继读到的应当 >= 我们发的（可能含 CONNECT 隧道后的少量额外字节）
    if d_up >= CLIENT_UP:
        print(f"[OK] 上行统计正确（{d_up} >= {CLIENT_UP}）")
    else:
        print(f"[FAIL] 上行统计 {d_up} < 期望 {CLIENT_UP}")
        ok = False

    if d_down >= SERVER_DOWN:
        print(f"[OK] 下行统计正确（{d_down} >= {SERVER_DOWN}）")
    else:
        print(f"[FAIL] 下行统计 {d_down} < 期望 {SERVER_DOWN}")
        ok = False

    # 速率换算
    print(f"[8] 300KB/s 显示为：{fmt_rate(300 * 1024)}")
    print(f"    1.5MB/s 显示为：{fmt_rate(1.5 * 1024 * 1024)}")

    # 量程
    assert _nice_ceil(0) == MIN_Y_MAX
    assert _nice_ceil(300 * 1024) == 500000, _nice_ceil(300 * 1024)
    assert _nice_ceil(1.2 * 1024 * 1024) == 2000000
    assert _nice_ceil(3 * 1024 * 1024) == 5000000
    print("[9] Y 轴量程取整正确（0→64K, 300K→500K, 1.2M→2M, 3M→5M）")

    stop.set()
    relay.stop()

    print()
    print("结果:", "全部通过 ✅" if ok else "有失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

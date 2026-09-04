#!/usr/bin/env python3
"""
FreePower 静态文件服务器

同时监听 TCP 端口和 Unix domain socket，兼容 fnOS 统一网关：
- TCP 端口：供 checkport 健康检查与直连访问
- Unix socket：供 fnOS 网关（nginx）通过 gatewaySocket 代理流量

关键适配：
1. 修复 Unix socket 下 client_address 为空字符串导致 IndexError 的问题
2. 处理 fnOS 网关转发的 gatewayPrefix 路径前缀（如 /app/FreePower）
"""
import functools
import os
import socketserver
import sys
import threading
import http.server


class GatewayHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """处理 fnOS 网关转发的 HTTP 请求。"""

    gateway_prefix = "/app/FreePower"

    def address_string(self):
        """修复 Unix socket 下 client_address 为空字符串导致的 IndexError。

        Unix socket 连接的 client_address 是空字符串 ''，而默认实现
        self.client_address[0] 取首字符会抛 IndexError，导致 send_error
        等日志记录流程崩溃，网关收不到有效响应从而返回 502。
        """
        try:
            if isinstance(self.client_address, str):
                return "unix"
            return self.client_address[0] or "unix"
        except (IndexError, TypeError):
            return "unix"

    def translate_path(self, path):
        """去掉网关前缀后再映射到文件系统路径。

        网关会把 /app/FreePower 前缀的路径转发给应用，需要去掉前缀
        才能正确映射到静态文件目录，否则会查找 ui/app/FreePower 导致 404。
        """
        prefix = self.gateway_prefix
        if path.startswith(prefix):
            path = path[len(prefix):]
            if not path.startswith('/'):
                path = '/' + path
        return super().translate_path(path)


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        socketserver.UnixStreamServer.server_bind(self)
        # 允许 nginx 等其他用户访问 socket
        os.chmod(self.server_address, 0o666)


def main():
    if len(sys.argv) < 4:
        print("Usage: serve.py <directory> <tcp_port> <socket_path> [gateway_prefix]", file=sys.stderr)
        sys.exit(1)

    directory = sys.argv[1]
    tcp_port = int(sys.argv[2])
    socket_path = sys.argv[3]
    gateway_prefix = sys.argv[4] if len(sys.argv) > 4 else "/app/FreePower"

    # 设置网关前缀
    GatewayHTTPRequestHandler.gateway_prefix = gateway_prefix

    handler = functools.partial(
        GatewayHTTPRequestHandler,
        directory=directory,
    )

    # 清理旧 socket 文件
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    # TCP 服务器（健康检查 + 直连）
    tcp_server = http.server.ThreadingHTTPServer(("0.0.0.0", tcp_port), handler)
    tcp_thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    tcp_thread.start()
    print(f"[serve] TCP listening on 0.0.0.0:{tcp_port}", flush=True)
    print(f"[serve] gateway_prefix={gateway_prefix}", flush=True)

    # Unix socket 服务器（fnOS 网关代理）
    unix_server = ThreadingUnixHTTPServer(socket_path, handler)
    unix_thread = threading.Thread(target=unix_server.serve_forever, daemon=True)
    unix_thread.start()
    print(f"[serve] Unix socket listening on {socket_path}", flush=True)

    try:
        while True:
            pass
    except KeyboardInterrupt:
        pass
    finally:
        tcp_server.shutdown()
        unix_server.shutdown()
        if os.path.exists(socket_path):
            os.unlink(socket_path)


if __name__ == "__main__":
    main()

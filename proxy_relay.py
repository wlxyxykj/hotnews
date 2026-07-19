"""
国内代理中继 —— 在国内电脑上运行，用 cloudflared tunnel 穿透后给 Render 使用

使用方法：
  1. 在国内电脑上运行: python proxy_relay.py --port 8899
  2. 用 cloudflared 穿透: cloudflared tunnel --url http://localhost:8899
  3. 在 Render 环境变量中设置:
     HTTP_PROXY=https://xxx.trycloudflare.com
     (xxx 是 cloudflared 输出的临时域名)

也可以不用 cloudflared，直接端口映射/内网穿透到有公网 IP 的机器上。
"""

import http.server
import urllib.request
import argparse
import socket

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """简单的 HTTP 正向代理"""

    def do_GET(self):
        self._proxy()

    def do_HEAD(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def _proxy(self):
        try:
            url = self.path
            body = None
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", 0))
                if length > 0 and length < 10 * 1024 * 1024:  # 最多 10MB
                    body = self.rfile.read(length)

            req = urllib.request.Request(
                url,
                data=body,
                headers={k: v for k, v in self.headers.items()
                        if k.lower() not in ("host", "proxy-connection")},
                method=self.command,
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())

        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
        except urllib.error.URLError as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"代理错误: {e.reason}".encode())
        except socket.timeout:
            self.send_response(504)
            self.end_headers()
            self.wfile.write(b"proxy timeout")
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, format, *args):
        # 简化日志
        print(f"[{self.command}] {args[0]}")

def main():
    parser = argparse.ArgumentParser(description="国内代理中继")
    parser.add_argument("--port", type=int, default=8899, help="代理端口 (默认 8899)")
    parser.add_argument("--host", default="0.0.0.0", help="绑定地址 (默认 0.0.0.0)")
    args = parser.parse_args()

    server = http.server.HTTPServer((args.host, args.port), ProxyHandler)
    print(f"代理中继已启动: http://{args.host}:{args.port}")
    print("接下来用 cloudflared 穿透:")
    print(f"  cloudflared tunnel --url http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n代理已停止")
        server.shutdown()

if __name__ == "__main__":
    main()

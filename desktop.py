"""
热点聚合 - 桌面应用启动器
================================
双击运行：后台启动 Flask 服务，弹出原生窗口显示热点聚合页面。
关闭窗口即退出，无需命令行，无需浏览器。

实现：
  - 后台线程用 werkzeug 的 make_server 跑 app:app（静默，不污染控制台）
  - 主线程开 pywebview 窗口（EdgeChromium 渲染，体验接近原生）
  - 窗口关闭 → 停止 server → 进程退出
"""

import os
import sys
import threading
import time

# 兼容 PyInstaller onefile 打包：sys._MEIPASS 是临时解压目录
if getattr(sys, "frozen", False):
    # 打包后的 exe 运行：资源在 _MEIPASS，切换工作目录让 Flask 能找到 templates/
    _BASE_DIR = sys._MEIPASS
    os.chdir(_BASE_DIR)
    if _BASE_DIR not in sys.path:
        sys.path.insert(0, _BASE_DIR)
else:
    # 开发模式：脚本所在目录
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    if _BASE_DIR not in sys.path:
        sys.path.insert(0, _BASE_DIR)

PORT = int(os.environ.get("PORT", "5000"))
HOST = "127.0.0.1"


def start_flask_server():
    """在后台线程启动 Flask 服务（静默，抑制 werkzeug 访问日志）"""
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    from app import app  # 导入 Flask app
    # 打包后显式指定 templates 目录，避免 __file__ 路径错位
    app.template_folder = os.path.join(_BASE_DIR, "templates")
    app.static_folder = os.path.join(_BASE_DIR, "static") if os.path.isdir(os.path.join(_BASE_DIR, "static")) else app.static_folder

    from werkzeug.serving import make_server
    server = make_server(HOST, PORT, app, threaded=True)
    # 把 server 挂到全局，供关闭窗口时 shutdown
    global _http_server
    _http_server = server
    server.serve_forever()


_http_server = None


def wait_for_server(timeout=20):
    """等待 Flask 起来（轮询 /api/ping），最多 timeout 秒"""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/ping", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    # 1. 启动 Flask 后台线程
    flask_thread = threading.Thread(target=start_flask_server, daemon=True)
    flask_thread.start()

    # 2. 等服务就绪（窗口不能在服务起来前打开，否则白屏）
    if not wait_for_server():
        # 服务没起来，回退用浏览器打开（兜底）
        import webbrowser
        webbrowser.open(f"http://{HOST}:{PORT}/")
        # 保持进程不退出，让 Flask 持续服务
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    # 3. 打开 pywebview 窗口
    try:
        import webview
        webview.create_window(
            title="热点聚合 · 实时榜单",
            url=f"http://{HOST}:{PORT}/",
            width=1280,
            height=860,
            min_size=(900, 600),
            text_select=True,
        )
        webview.start()
    except ImportError:
        # 没装 pywebview，回退浏览器
        import webbrowser
        webbrowser.open(f"http://{HOST}:{PORT}/")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass

    # 4. 窗口关闭后，停止 Flask 并退出
    if _http_server:
        _http_server.shutdown()


if __name__ == "__main__":
    main()

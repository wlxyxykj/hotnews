"""
热点聚合工具 v2.2 - Flask 后端
策略：能抓的全力抓，抓不到的诚实标记「不可用」，不伪造数据
"""

import os, time, threading, traceback, hashlib, json, sqlite3, random
from datetime import datetime, timezone, timedelta
import concurrent.futures

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

# 北京时间时区
BEIJING_TZ = timezone(timedelta(hours=8))

try:
    import jwt as pyjwt
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False

app = Flask(__name__)
CORS(app)

SECRET_KEY = os.environ.get("SECRET_KEY", "hotnews-secret-2024-xK9mP")
DB_PATH    = os.environ.get("DB_PATH", "hotnews.db")
CACHE_TTL  = int(os.environ.get("CACHE_TTL", "180"))
STALE_TTL  = int(os.environ.get("STALE_TTL", "21600"))  # 抓取失败时，旧成功数据最多保留 6 小时兜底

# ─── 数据库 ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        platform TEXT,
        saved_at TEXT DEFAULT (datetime('now')),
        UNIQUE(user_id, url)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        platform TEXT,
        viewed_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.close()
init_db()

# ─── 缓存 ─────────────────────────────────────────────
_cache: dict = {}
_stale_ok: dict = {}   # 各平台最近一次成功数据（跨 CACHE_TTL 保留，供失败兜底）
_lock = threading.Lock()

def get_cache(key):
    with _lock:
        item = _cache.get(key)
        if item and (time.time() - item["ts"] < CACHE_TTL):
            return item["data"]
    return None

def set_cache(key, data):
    with _lock:
        _cache[key] = {"ts": time.time(), "data": data}
        if data.get("status") == "success" and data.get("items"):
            _stale_ok[key] = {"ts": time.time(), "data": data}

def _stale_fallback(key):
    """失败兜底：返回 6h 内的上一次成功数据（明确标注延迟），无则 None"""
    prev = _stale_ok.get(key)
    if prev and time.time() - prev["ts"] < STALE_TTL:
        delayed = dict(prev["data"])
        delayed["is_realtime"] = False
        delayed["update_note"] = f"上次成功 {prev['data'].get('fetched_at','')} · 数据延迟"
        return delayed
    return None

# ─── 工具函数 ──────────────────────────────────────────
def now_str():
    """返回北京时间 HH:MM 格式"""
    return datetime.now(BEIJING_TZ).strftime("%H:%M")

def now_full():
    """返回完整北京时间字符串"""
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")

# ─── HTTP 会话 + 重试机制 ──────────────────────────────────
# 使用连接池复用 TCP 连接，减少握手开销，降低被识别为爬虫的概率
_session = None
_session_lock = threading.Lock()

def _get_session():
    """获取全局 requests.Session（带连接池 + 自动重试 + 代理支持）"""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                _session = requests.Session()
                # 代理：通过 SCRAPE_PROXY 环境变量配置（避免影响 pip install）
                # 例如：SCRAPE_PROXY=http://127.0.0.1:7890
                proxy_url = os.environ.get("SCRAPE_PROXY", "")
                if proxy_url:
                    _session.proxies = {"http": proxy_url, "https": proxy_url}
                # 连接池：最多 20 个连接
                adapter = HTTPAdapter(
                    pool_connections=20,
                    pool_maxsize=20,
                    max_retries=Retry(
                        total=3,
                        backoff_factor=0.5,
                        status_forcelist=[429, 500, 502, 503, 504],
                        allowed_methods=["GET", "HEAD"],
                    ),
                )
                _session.mount("https://", adapter)
                _session.mount("http://", adapter)
    return _session

# User-Agent 池，模拟真实浏览器
_USER_AGENTS = [
    # Chrome 124 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 124 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Edge 124 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Safari 17 iPhone
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    # Chrome Android
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
]

def _random_ua():
    return random.choice(_USER_AGENTS)

def _looks_decoded(data: bytes) -> bool:
    """粗判响应体是否已是明文（已解压）。br 压缩流首字节通常不在可打印 ASCII 范围。"""
    if not data:
        return True
    first = data[:16]
    # 明文 HTML/XML/JSON 通常以 < ? { [ 或空白开头
    return any(first.startswith(p) for p in (b"<", b"<?", b"<!", b"{", b"[", b"<!DOC", b"\xef\xbb\xbf")) \
        or first.lstrip()[:1] in (b"<", b"{", b"[")

def http_get(url, timeout=15, referer=None, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", parse_json=False, raw_bytes=False):
    """
    统一的 HTTP GET 请求，带重试、随机 UA、连接池复用。
    返回 (response_or_data, error_string_or_None)
    """
    session = _get_session()
    h = {
        "User-Agent": _random_ua(),
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        h["Referer"] = referer
    try:
        resp = session.get(url, headers=h, timeout=timeout)
        if resp.status_code == 200:
            # 防御性 br 解压兜底：requests 仅在装了 brotli/brotlicffi 时自动解 brotli。
            # 部分服务器无视 Accept-Encoding 强制返回 br，这里手动补一刀，避免后续拿到压缩乱码。
            ce = resp.headers.get("Content-Encoding", "").lower()
            if ce == "br" and not _looks_decoded(resp.content):
                try:
                    import brotli as _brotli
                    resp._content = _brotli.decompress(resp.content)
                    resp.headers["Content-Encoding"] = "identity"
                except Exception:
                    pass
            if parse_json:
                try:
                    return resp.json(), None
                except ValueError as e:
                    return None, f"JSON parse error: {e}"
            if raw_bytes:
                return resp.content, None
            # 自动检测编码
            if resp.encoding and resp.encoding.lower() != "iso-8859-1":
                pass  # requests 已自动检测到正确编码
            else:
                # requests 未识别编码（默认 iso-8859-1），手动探测
                # 优先级：HTML meta charset → 尝试常见中文编码（容错解码）
                head = resp.content[:1024]
                meta_enc = None
                import re as _re
                m = _re.search(rb'charset=["\']?([\w-]+)', head, _re.I)
                if m:
                    meta_enc = m.group(1).decode("ascii", "ignore").lower()
                # 候选编码顺序：meta 声明优先，然后 utf-8 / gbk 系
                candidates = []
                for e in (meta_enc, "utf-8", "gb18030", "gbk", "gb2312"):
                    if e and e not in candidates and e != "iso-8859-1":
                        candidates.append(e)
                for enc in candidates:
                    try:
                        # 用 errors=ignore 容错：部分页面存在少量脏字节（如新浪首页），
                        # 严格 decode 会整体失败，但绝大多数字节仍是合法的。
                        sample = resp.content[:4096].decode(enc, "ignore")
                        # 简单校验：解码后应含中文字符或常见 HTML 标记
                        if sample.count("\ufffd") < 20 or enc == candidates[0]:
                            resp.encoding = enc
                            # 给 requests 打补丁：让其 text 也走容错解码
                            resp._content = resp.content.decode(enc, "ignore").encode(enc, "ignore")
                            break
                    except (UnicodeDecodeError, LookupError):
                        continue
            return resp, None
        return None, f"HTTP {resp.status_code}"
    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.ConnectionError as e:
        return None, f"connection error: {e}"
    except Exception as e:
        return None, str(e)

def make_result(items, is_realtime=True, note=None, status="success"):
    return {
        "status": status,
        "items": items,
        "is_realtime": is_realtime,
        "fetched_at": now_str(),
        "update_note": note or ("实时榜单" if is_realtime else "非实时更新"),
    }

def fail_result(msg="抓取失败", note=None):
    return {
        "status": "failed",
        "items": [],
        "is_realtime": False,
        "fetched_at": now_str(),
        "update_note": note or f"{msg}（{now_str()}）",
    }

def safe_fetch(key, fn, *args, **kwargs):
    """带缓存的安全抓取；失败时用 6h 内旧数据兜底（标注延迟）。返回 (result, is_failed)"""
    cached = get_cache(key)
    if cached:
        return cached, cached.get("status") == "failed"
    try:
        result = fn(*args, **kwargs)
        if result and result.get("status") == "success" and result.get("items"):
            set_cache(key, result)
            return result, False
        if result:
            stale = _stale_fallback(key)
            if stale:
                set_cache(key, stale)
                return stale, False
            set_cache(key, result)
            return result, True
    except Exception:
        traceback.print_exc()
    stale = _stale_fallback(key)
    if stale:
        set_cache(key, stale)
        return stale, False
    return fail_result(), True


# ══════════════════════════════════════════════════════════════
# 【综合新闻】
# ══════════════════════════════════════════════════════════════

# ── 微博热搜（强可靠）───────────────────────────────────
def _fetch_weibo():
    data, err = http_get(
        "https://weibo.com/ajax/side/hotSearch",
        referer="https://weibo.com/", parse_json=True, timeout=12
    )
    if err:
        return fail_result(f"微博（{err}）")
    raw = data.get("data", {}).get("realtime", [])
    items = []
    for i, item in enumerate(raw[:20], 1):
        title = item.get("word") or item.get("label_name", "")
        num   = item.get("num", "")
        label = item.get("label_name", "")
        hot   = label if label else (f"{int(num)//10000}万" if str(num).isdigit() else str(num))
        if title:
            items.append({"rank": i, "title": title,
                          "url": f"https://s.weibo.com/weibo?q={requests.utils.quote(title)}",
                          "hot": hot})
    return make_result(items, True) if items else fail_result("微博（无数据）")

def fetch_weibo():
    r, _ = safe_fetch("weibo", _fetch_weibo)
    return r

# ── 腾讯新闻（强可靠）───────────────────────────────────
def _fetch_tencent():
    data, err = http_get(
        "https://i.news.qq.com/gw/event/hot_ranking_list?offset=0&count=20&strategy=1",
        referer="https://news.qq.com/", parse_json=True, timeout=12
    )
    if err:
        return fail_result(f"腾讯新闻（{err}）")
    news_list = (data.get("idlist", [{}])[0].get("newslist", [])
                 or data.get("data", {}).get("hotRankingList", []))
    items = []
    for item in news_list[:20]:
        title = item.get("title") or item.get("hotTitle", "")
        if not title or "每10分钟更新" in title:   # 列表头占位条目
            continue
        url   = item.get("url") or item.get("articleUrl", "https://news.qq.com/")
        hot   = str(item.get("hotScore") or item.get("readCount", ""))
        items.append({"rank": len(items) + 1, "title": title, "url": url, "hot": hot})
    return make_result(items, True) if items else fail_result("腾讯新闻（无数据）")

def fetch_tencent():
    r, _ = safe_fetch("tencent", _fetch_tencent)
    return r

# ── 今日头条（多端点回退）──────────────────────────────────
def _fetch_toutiao():
    # 端点1: 头条热榜 JSON API（实测可用，无需鉴权）
    data, err = http_get(
        "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        referer="https://www.toutiao.com/", parse_json=True, timeout=12
    )
    if not err and data:
        raw = data.get("data", []) or []
        items = []
        for i, it in enumerate(raw[:20], 1):
            title = it.get("Title", "") or it.get("title", "")
            url = it.get("Url", "") or it.get("url", "")
            hot = it.get("HotValue", "") or it.get("hot_value", "")
            if hot and str(hot).isdigit():
                hot = f"{int(hot)//10000}万"
            if title:
                items.append({"rank": i, "title": title,
                              "url": url or "https://www.toutiao.com/",
                              "hot": str(hot)})
        if items:
            return make_result(items, True, "实时热榜")
    # 端点2: 第三方热榜镜像
    data, err = http_get(
        "https://tcmarket.cdn.bceutils.com/hot-list/toutiao-hot-search.json",
        accept="application/json", parse_json=True, timeout=12
    )
    if not err and data:
        raw = data if isinstance(data, list) else data.get("data", [])
        items = [{"rank": i+1, "title": it.get("title",""),
                  "url": it.get("url","https://www.toutiao.com/"), "hot": str(it.get("hot",""))}
                 for i, it in enumerate(raw[:20]) if it.get("title")]
        if items:
            return make_result(items, True, "实时热点")
    return fail_result("今日头条（暂不可用）")

def fetch_toutiao():
    r, _ = safe_fetch("toutiao", _fetch_toutiao)
    return r

# ── 网易新闻（多端点回退）─────────────────────────────────
def _fetch_wangyi():
    # 端点1: 网易 3g 触屏版 JSONP 接口（实测稳定，无需鉴权）
    import re
    resp, err = http_get(
        "https://3g.163.com/touch/reconstruct/article/list/BBM54PGAwangning/0-20.html",
        referer="https://news.163.com/", timeout=12
    )
    if not err:
        try:
            m = re.search(r"artiList\((.*)\)", resp.text, re.DOTALL)
            data = json.loads(m.group(1)) if m else {}
            raw = list(data.values())[0] if data else []
            items = []
            for i, it in enumerate(raw[:20], 1):
                title = it.get("title", "")
                docid = it.get("docid", "")
                url = it.get("url", "") or (f"https://m.163.com/news/article/{docid}.html" if docid else "https://news.163.com/")
                if title:
                    items.append({"rank": i, "title": title, "url": url, "hot": ""})
            if items:
                return make_result(items, True, "实时要闻")
        except (json.JSONDecodeError, AttributeError, IndexError):
            pass

    # 端点2: 网易新闻排行榜 HTML 兜底
    resp, err = http_get("https://news.163.com/rank/", referer="https://www.163.com/", timeout=15)
    if not err and len(resp.text) > 300:
        soup = BeautifulSoup(resp.text, "lxml")
        for sel in [".hot-title a", ".hotlist-title a", ".news_title a",
                    "h3 a", ".title a", ".item-headline a"]:
            links = soup.select(sel)[:20]
            items = []
            seen = set()
            for i, a in enumerate(links, 1):
                title = a.get_text(strip=True)
                href  = a.get("href", "")
                if title and len(title) > 6 and title not in seen:
                    seen.add(title)
                    if not href.startswith("http"):
                        href = "https://www.163.com" + href
                    items.append({"rank": i, "title": title, "url": href, "hot": ""})
                    if len(items) >= 20:
                        break
            if len(items) >= 5:
                return make_result(items, True, "实时热榜")

    return fail_result("网易（暂不可用）")

def fetch_wangyi():
    r, _ = safe_fetch("wangyi", _fetch_wangyi)
    return r

# ── 新浪新闻（多端点，改进编码处理）──────────────────────────
def _fetch_sina():
    # 端点1: 新浪新闻 RSS
    for url in [
        "https://rss.sina.com.cn/news/china/focus.xml",
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=20&page=1",
    ]:
        content, err = http_get(url, referer="https://news.sina.com.cn/", raw_bytes=True, timeout=12)
        if not err:
            for enc in ["utf-8", "gb2312", "gbk", "gb18030"]:
                try:
                    text = content.decode(enc)
                    soup = BeautifulSoup(text, "lxml")
                    items = []
                    for i, it in enumerate(soup.find_all("item")[:20]):
                        title_el = it.find("title")
                        if title_el:
                            title = title_el.get_text(strip=True)
                            link_el = it.find("link")
                            link = link_el.get_text(strip=True) if link_el else "https://news.sina.com.cn/"
                            if title:
                                items.append({"rank": i+1, "title": title, "url": link, "hot": ""})
                    if items:
                        return make_result(items, False, "RSS·非实时")
                    break
                except (UnicodeDecodeError, LookupError):
                    continue

    # 端点2: 新浪新闻首页 + 移动端兜底（新浪 PC 端偶发限流，移动端更稳）
    for page_url in ["https://news.sina.com.cn/", "https://news.sina.cn/"]:
        resp, err = http_get(page_url, referer="https://news.sina.com.cn/", timeout=15)
        if err:
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        # 新浪首页：新闻链接分布在 a.news-item / .news-title / 含 doc-id 的链接
        for sel in ["a.news-title", ".news-title a", "h1 a", "h2 a",
                    "a[href*='doc-']", "a[href*='sina.cn']",
                    ".a-news a", ".cont-title a"]:
            for a in soup.select(sel)[:30]:
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if (title and 8 < len(title) < 60 and title not in seen
                        and ("sina" in href or href.startswith("/news"))):
                    seen.add(title)
                    if not href.startswith("http"):
                        href = "https://news.sina.com.cn" + href
                    items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
                if len(items) >= 20:
                    break
            if len(items) >= 20:
                break
        if len(items) >= 5:
            return make_result(items[:20], True, "实时新闻")
    return fail_result("新浪新闻（暂不可用）")

def fetch_sina():
    r, _ = safe_fetch("sina", _fetch_sina)
    return r

# ── 人民日报 RSS ─────────────────────────────────────────
def _fetch_rmrb():
    # 注：该 RSS 已停更于 2025-06（人民网无更快的公开接口），内容偏旧但链接可直接打开文章页
    resp, err = http_get("http://www.people.com.cn/rss/politics.xml",
                         referer="https://www.people.com.cn/", timeout=15)
    if err:
        return fail_result(f"人民日报（{err}）")
    soup = BeautifulSoup(resp.text, "xml")
    items = []
    for i, it in enumerate(soup.find_all("item")[:20]):
        title_el = it.find("title")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link_el = it.find("link")
        link = link_el.get_text(strip=True) if link_el else ""
        # 链接缺失时兜底到站内搜索该标题，而不是跳首页
        if not link:
            link = f"http://search.people.cn/s/?keyword={requests.utils.quote(title)}"
        items.append({"rank": i + 1, "title": title, "url": link, "hot": ""})
    return make_result(items, False, "RSS·非实时更新") if items else fail_result("人民日报（无数据）")

def fetch_rmrb():
    r, _ = safe_fetch("rmrb", _fetch_rmrb)
    return r

# ── 央视新闻 ─────────────────────────────────────────────
def _fetch_cctv():
    # RSS 端点已下线，改抓新闻频道首页 HTML（实测编码兜底生效，可正确解析中文）
    return _html_list(
        "https://news.cctv.com/", "https://news.cctv.com/",
        selectors=[".title a", "h3 a", ".news-title a", ".lbd_img a",
                   ".cetitle a", ".word a", "ul.news-list a",
                   ".tl_main a", ".cbox a", "a[href*='/202']"],
        host_filter="cctv.com", abs_host="https://news.cctv.com",
        note="实时新闻",
    )

def fetch_cctv():
    r, _ = safe_fetch("cctv", _fetch_cctv)
    return r

# ── 新华社 ───────────────────────────────────────────────
def _fetch_xinhua():
    # RSS 已下线，改抓新华网世界频道 + 时政频道 HTML
    for page_url in ["https://www.news.cn/world/", "https://www.news.cn/politics/"]:
        r = _html_list(
            page_url, "https://www.news.cn/",
            selectors=["h3 a", ".news-title a", ".tit a", ".news a",
                       "a[href*='/news.cn/']", ".partList a", ".dataList a",
                       ".domPC_a a", "div.news a"],
            host_filter="news.cn", abs_host="https://www.news.cn",
            note="实时新闻",
        )
        if r.get("items"):
            return r
    return fail_result("新华社（暂不可用）")

def fetch_xinhua():
    r, _ = safe_fetch("xinhua", _fetch_xinhua)
    return r

# ── 澎湃新闻（多端点回退）─────────────────────────────────
def _fetch_pengpai():
    # 端点1: 澎湃首页右侧栏 cache API（实测可用，含 hotNews 热榜）
    data, err = http_get(
        "https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar",
        referer="https://www.thepaper.cn/", parse_json=True, timeout=15
    )
    if not err and data:
        d = data.get("data", {}) or {}
        raw = (d.get("hotNews", []) or d.get("editorHandpicked", [])
               or d.get("morningEveningNews", []))
        items = []
        for i, it in enumerate(raw[:20], 1):
            title = it.get("name", "") or it.get("title", "")
            cont_id = it.get("contId", "") or it.get("nodeId", "")
            if title:
                # newsDetail_detail_ 已失效（302 回首页），现为 newsDetail_forward_
                link = it.get("link", "") or (f"https://www.thepaper.cn/newsDetail_forward_{cont_id}" if cont_id
                                              else f"https://www.thepaper.cn/searchResult?keyword={requests.utils.quote(title)}")
                praise = it.get("praiseTimes", "")
                hot = f"{praise}" if praise else ""
                items.append({"rank": i, "title": title, "url": link, "hot": str(hot)})
        if items:
            return make_result(items, True, "实时热闻")
    # 端点2: 澎湃首页 HTML 兜底
    resp, err = http_get("https://www.thepaper.cn/", referer="https://www.thepaper.cn/", timeout=15)
    if not err:
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for sel in [".news_title a", ".article_title a", ".index_title a",
                    ".feed-title a", "h2 a", "h3 a", "a[href*='thepaper.cn/newsDetail']"]:
            if len(items) >= 20:
                break
            for a in soup.select(sel)[:30]:
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if title and len(title) > 8 and title not in seen:
                    seen.add(title)
                    if not href.startswith("http"):
                        href = "https://www.thepaper.cn" + href
                    items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
        if len(items) >= 5:
            return make_result(items[:20], True, "实时要闻")
    return fail_result("澎湃新闻（暂不可用）")

def fetch_pengpai():
    r, _ = safe_fetch("pengpai", _fetch_pengpai)
    return r

# ── 知乎热搜（API + HTML 回退）─────────────────────────────
def _fetch_zhihu():
    # 知乎热榜 API（2026-08 改版：分栏热榜端点为 api.zhihu.com/topstory/hot-lists/total）
    for url in [
        "https://api.zhihu.com/topstory/hot-lists/total?limit=20",
        "https://www.zhihu.com/api/v4/topstory/hot-lists?limit=20&desktop=true",
        "https://api.zhihu.com/topstory/hot-lists?limit=20",
        "https://www.zhihu.com/api/v3/feed/topstory/hot-lists?limit=20&desktop=true",
    ]:
        data, err = http_get(url, referer="https://www.zhihu.com/", parse_json=True,
                             accept="application/json, text/plain, */*", timeout=15)
        if not err and data:
            raw = data.get("data", []) or data.get("top_stories", []) or data.get("topstories", [])
            items = []
            for i, item in enumerate(raw[:20], 1):
                if isinstance(item, dict):
                    target = item.get("target", {}) or item
                    title = target.get("title") or target.get("question", {}).get("title", "")
                    qid = target.get("id", "") or target.get("question", {}).get("id", "")
                    metric = ((target.get("metrics_area") or {}).get("text")
                              or target.get("vote_count", "") or target.get("follower_count", "") or "")
                    if str(metric).isdigit() and int(metric) > 1000:
                        metric = f"{int(metric)//10000}万"
                    u = f"https://www.zhihu.com/question/{qid}" if qid else "https://www.zhihu.com/"
                else:
                    title = str(item); metric = ""; u = "https://www.zhihu.com/"
                if title:
                    items.append({"rank": i, "title": title, "url": u, "hot": str(metric)})
            if items:
                return make_result(items, True, "实时热搜")

    # 备用: 知乎热榜 HTML
    resp, err = http_get("https://www.zhihu.com/hot", referer="https://www.zhihu.com/", timeout=15)
    if not err:
        soup = BeautifulSoup(resp.text, "lxml")
        links = soup.select("div.HotItem-title a, div.List-itemText a, a[href*='/question/']")[:20]
        items = []
        seen = set()
        for a in links:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if title and len(title) > 5 and title not in seen:
                seen.add(title)
                if not href.startswith("http"):
                    href = "https://www.zhihu.com" + href
                items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if len(items) >= 20:
                break
        if items:
            return make_result(items, True, "实时热搜")

    # 终极兜底：尝试知乎日报 API（海外通常可访问）
    data, err = http_get(
        "https://news-at.zhihu.com/api/4/news/latest",
        referer="https://daily.zhihu.com/", parse_json=True, timeout=12
    )
    if not err and data:
        top_stories = data.get("top_stories", [])[:15]
        stories = data.get("stories", [])[:10]
        items = []
        for i, s in enumerate(top_stories + stories, 1):
            title = s.get("title", "")
            sid = s.get("id", "")
            if title:
                items.append({"rank": i, "title": title,
                              "url": f"https://daily.zhihu.com/story/{sid}" if sid else "https://daily.zhihu.com/",
                              "hot": ""})
        if items:
            return make_result(items[:20], False, "知乎日报·非实时")
    return fail_result("知乎（暂不可用）")

def fetch_zhihu():
    r, _ = safe_fetch("zhihu", _fetch_zhihu)
    return r

# ── B站排行榜（多端点回退，规避偶发限流）──────────────────
def _fetch_bilibili():
    def _parse_list(raw, note="实时榜单"):
        items = []
        for i, item in enumerate(raw[:20], 1):
            view = item.get("stat", {}).get("view", 0)
            bvid = item.get("bvid", "")
            items.append({"rank": i, "title": item.get("title", ""),
                          "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com/",
                          "hot": f"{int(view)//10000}万播放" if view else ""})
        return make_result(items, True, note) if items else None

    # 端点1: 全站排行榜（首选，含播放量）
    data, err = http_get(
        "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",
        referer="https://www.bilibili.com/", parse_json=True, timeout=10
    )
    if not err and data and data.get("code") == 0:
        r = _parse_list(data.get("data", {}).get("list", []))
        if r:
            return r
    # 端点2: 热门视频流（排行榜被限流时的兜底）
    data, err = http_get(
        "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1",
        referer="https://www.bilibili.com/", parse_json=True, timeout=10
    )
    if not err and data and data.get("code") == 0:
        r = _parse_list(data.get("data", {}).get("list", []), "热门视频")
        if r:
            return r
    # 端点3: 热门搜索榜（最后兜底）
    data, err = http_get(
        "https://app.bilibili.com/x/v2/search/trending/ranking?limit=20",
        referer="https://www.bilibili.com/", parse_json=True, timeout=10
    )
    if not err and data:
        raw = (data.get("data", {}).get("list", [])
               or data.get("result", []) or [])
        items = []
        for i, it in enumerate(raw[:20], 1):
            kw = it.get("keyword", "") or it.get("show_name", "") or it.get("name", "")
            if kw:
                items.append({"rank": i, "title": kw,
                              "url": f"https://search.bilibili.com/all?keyword={requests.utils.quote(kw)}",
                              "hot": str(it.get("hot_score", "") or "")})
        if items:
            return make_result(items, True, "热搜榜")
    return fail_result("B站（暂不可用）")

def fetch_bilibili():
    r, _ = safe_fetch("bilibili", _fetch_bilibili)
    return r

# ── 百度热搜 ─────────────────────────────────────────────
def _fetch_baidu():
    data, err = http_get(
        "https://top.baidu.com/api/board?platform=wise&tab=realtime",
        referer="https://top.baidu.com/", parse_json=True, timeout=12
    )
    if err:
        return fail_result(f"百度热搜（{err}）")
    cards = data.get("data", {}).get("cards", [])
    raw = []
    for c in cards:
        for block in c.get("content", []):
            inner = block.get("content", []) if isinstance(block, dict) else []
            if isinstance(inner, list):
                raw.extend(inner)
    items = []
    for i, it in enumerate(raw[:20], 1):
        word = it.get("word", "") or it.get("query", "")
        hot = it.get("hotScore", "") or it.get("hot", "")
        url = it.get("url", "") or it.get("rawUrl", "") or f"https://www.baidu.com/s?wd={requests.utils.quote(word)}"
        if word:
            items.append({"rank": i, "title": word, "url": url, "hot": str(hot)})
    return make_result(items, True, "实时热搜") if items else fail_result("百度热搜（无数据）")

def fetch_baidu():
    r, _ = safe_fetch("baidu", _fetch_baidu)
    return r

# ── 抖音热搜 ─────────────────────────────────────────────
def _fetch_douyin():
    data, err = http_get(
        "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
        referer="https://www.douyin.com/", parse_json=True, timeout=12
    )
    if err:
        return fail_result(f"抖音热搜（{err}）")
    raw = data.get("word_list", []) or data.get("data", {}).get("word_list", [])
    items = []
    for i, it in enumerate(raw[:20], 1):
        word = it.get("word", "")
        hot = it.get("hot_value", "")
        if word:
            hot_str = f"{int(hot)//10000}万" if str(hot).isdigit() else str(hot)
            items.append({"rank": i, "title": word,
                          "url": f"https://www.douyin.com/search/{requests.utils.quote(word)}",
                          "hot": hot_str})
    return make_result(items, True, "实时热搜") if items else fail_result("抖音热搜（无数据）")

def fetch_douyin():
    r, _ = safe_fetch("douyin", _fetch_douyin)
    return r


# ── 贴吧热议（v3 新增）──────────────────────────────────
def _fetch_tieba():
    data, err = http_get(
        "https://tieba.baidu.com/hottopic/browse/topicList",
        referer="https://tieba.baidu.com/", parse_json=True, timeout=12
    )
    if err:
        return fail_result(f"贴吧（{err}）")
    raw = (data.get("data", {}) or {}).get("bang_topic", {}).get("topic_list", []) or []
    items = []
    for i, it in enumerate(raw[:20], 1):
        title = it.get("topic_name", "")
        if not title:
            continue
        url = it.get("topic_url") or f"https://tieba.baidu.com/hottopic/browse/hottopic?topic_id={it.get('topic_id', '')}"
        d = int(it.get("discuss_num") or 0)
        items.append({"rank": i, "title": title, "url": url,
                      "hot": f"{d//10000}万讨论" if d else ""})
    return make_result(items, True, "热议话题") if items else fail_result("贴吧（无数据）")

def fetch_tieba():
    r, _ = safe_fetch("tieba", _fetch_tieba)
    return r


# ══════════════════════════════════════════════════════════════
# 【科技数码】
# ══════════════════════════════════════════════════════════════

def _rss(url, ref="", note="RSS·非实时更新", timeout=15):
    """RSS 抓取，支持 HTTP/HTTPS 双协议 + 多编码容错"""
    urls_to_try = [url]
    if url.startswith("https://"):
        urls_to_try.append(url.replace("https://", "http://", 1))
    elif url.startswith("http://"):
        urls_to_try.append(url.replace("http://", "https://", 1))
    for attempt_url in urls_to_try:
        content, err = http_get(attempt_url, referer=ref, raw_bytes=True, timeout=timeout)
        if err:
            continue
        for enc in ["utf-8", "gb2312", "gbk", "gb18030", "utf-16"]:
            try:
                text = content.decode(enc)
                soup = BeautifulSoup(text, "xml")
                items = []
                for i, it in enumerate(soup.find_all("item")[:20]):
                    title_el = it.find("title")
                    link_el = it.find("link")
                    if title_el:
                        title = title_el.get_text(strip=True)
                        link = link_el.get_text(strip=True) if link_el else (ref or "")
                        items.append({"rank": i+1, "title": title, "url": link, "hot": ""})
                if items:
                    return make_result(items, False, note)
                break
            except (UnicodeDecodeError, LookupError):
                continue
    return fail_result(f"RSS·{note}（暂不可用）")

def _html_list(url, ref, selectors, note="实时资讯", limit=20,
               min_len=6, host_filter=None, abs_host=None, realtime=True):
    """
    通用 HTML 列表抓取：从指定页面按 CSS 选择器抽 (标题,链接)。
    - selectors: 候选 CSS 选择器列表，依次尝试，命中即返回
    - host_filter: 仅保留 href 含此关键字的链接（如域名片段）
    - abs_host: 相对链接补全为绝对链接时的主机（如 https://news.cctv.com）
    返回 make_result 或 fail_result
    """
    resp, err = http_get(url, referer=ref, timeout=15)
    if err:
        return fail_result(f"（{err}）")
    soup = BeautifulSoup(resp.text, "lxml")
    seen = set()
    items = []
    for sel in selectors:
        # 注意：不能对 select 结果做 [:N] 截断——某些站点（如财新）前几十个匹配是
        # 短文本导航链接，真正标题在后面。先全量收集，再按 min_len 过滤。
        for a in soup.select(sel):
            if len(items) >= limit:
                break
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if not title or len(title) < min_len or title in seen:
                continue
            if host_filter and host_filter not in href and not href.startswith("/"):
                continue
            if href and not href.startswith("http"):
                href = (abs_host or ref).rstrip("/") + "/" + href.lstrip("/")
            if title and href:
                seen.add(title)
                items.append({"rank": len(items) + 1, "title": title, "url": href, "hot": ""})
        if len(items) >= limit:
            break
    if len(items) >= 5:
        return make_result(items[:limit], realtime, note)
    return fail_result(f"{note}（暂不可用）")

def _fetch_36kr():
    # gateway API 已全面 500，改用 RSS feed + 首页 HTML 兜底
    # （注：36kr 的 Cloudflare 防护在部分网络下会拦 RSS，失败时走兜底/延迟保底）
    r = _rss("https://36kr.com/feed", "https://36kr.com/", "RSS·资讯")
    if r and r.get("items"):
        return r

    # 兜底: 36kr 首页 HTML
    resp, err = http_get("https://36kr.com/", referer="https://36kr.com/", timeout=15)
    if not err:
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for sel in [".article-title a", ".feed-title a", ".kr-shadow-content a",
                    "h3 a", ".item-title a", "a[href*='36kr.com/p/']"]:
            if len(items) >= 20:
                break
            for a in soup.select(sel)[:35]:
                title = a.get_text(strip=True)
                href  = a.get("href","")
                if title and len(title) > 6 and title not in seen:
                    seen.add(title)
                    if not href.startswith("http"):
                        href = "https://36kr.com" + href
                    items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
        if items:
            return make_result(items, True, "实时资讯")
    return fail_result("36氪（暂不可用）")

def fetch_36kr():
    r, _ = safe_fetch("36kr", _fetch_36kr)
    return r

def _fetch_huxiu():
    # 虎嗅 RSS（部分网络可通过 WAF；挂起时 6s 硬超时快速失败，不走全局重试 session）
    # → 雷锋网 RSS → 雷锋网 HTML → 虎嗅首页兜底
    import requests as _rq
    try:
        resp = _rq.get("https://www.huxiu.com/rss/0.xml",
                       headers={"User-Agent": _random_ua(), "Accept": "*/*"}, timeout=6)
        if resp.ok:
            soup = BeautifulSoup(resp.text, "xml")
            items = [{"rank": i + 1, "title": it.find("title").get_text(strip=True),
                      "url": it.find("link").get_text(strip=True) if it.find("link") else "https://www.huxiu.com/",
                      "hot": ""}
                     for i, it in enumerate(soup.find_all("item")[:20]) if it.find("title")]
            if items:
                return make_result(items, False, "RSS·虎嗅")
    except Exception:
        pass
    r = _rss("https://www.leiphone.com/feed", "https://www.leiphone.com/", "RSS·雷锋网", timeout=12)
    if r and r.get("items"):
        return r

    r = _html_list(
        "https://www.leiphone.com/", "https://www.leiphone.com/",
        selectors=["a[href*='leiphone.com/']", "h3 a", ".article-title a", "h2 a"],
        host_filter="leiphone.com", abs_host="https://www.leiphone.com",
        note="科技资讯·雷锋网", min_len=10,
    )
    if r.get("items"):
        return r
    # 兜底：尝试虎嗅首页（偶尔 WAF 放行时可用）
    resp, err = http_get("https://www.huxiu.com/", referer="https://www.huxiu.com/", timeout=15)
    if not err:
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for sel in [".article-title a", ".b-a-title a", "h3 a", "a[href*='huxiu.com/article/']"]:
            for a in soup.select(sel)[:40]:
                title = a.get_text(strip=True)
                href  = a.get("href","")
                if title and len(title) > 6 and title not in seen:
                    seen.add(title)
                    if not href.startswith("http"):
                        href = "https://www.huxiu.com" + href
                    items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if len(items) >= 20:
                break
        if len(items) >= 5:
            return make_result(items[:20], True, "实时资讯")
    return fail_result("虎嗅（暂不可用）")

def fetch_huxiu():
    r, _ = safe_fetch("huxiu", _fetch_huxiu)
    return r

# ── 掘金热榜（v3 新增）──────────────────────────────────
def _fetch_juejin():
    data, err = http_get(
        "https://api.juejin.cn/content_api/v1/content/article_rank?category_id=1&type=hot",
        referer="https://juejin.cn/", parse_json=True, timeout=12
    )
    if err:
        return fail_result(f"掘金（{err}）")
    items = []
    for i, it in enumerate((data.get("data") or [])[:20], 1):
        c = it.get("content", {}) or {}
        title = c.get("title", "")
        if not title:
            continue
        hot = (it.get("content_counter") or {}).get("hot_rank") or ""
        items.append({"rank": i, "title": title,
                      "url": f"https://juejin.cn/post/{c['content_id']}" if c.get("content_id") else "https://juejin.cn/",
                      "hot": str(hot)})
    return make_result(items, True, "热榜") if items else fail_result("掘金（无数据）")

def fetch_juejin():
    r, _ = safe_fetch("juejin", _fetch_juejin)
    return r

# ── 开源中国（v3 新增，RSS）──────────────────────────────
def fetch_oschina():
    r, _ = safe_fetch("oschina", _rss,
                      "https://www.oschina.net/news/rss", "https://www.oschina.net/", "RSS·开源资讯")
    return r

# ── 钛媒体（v3 新增，RSS）────────────────────────────────
def fetch_tmtpost():
    r, _ = safe_fetch("tmtpost", _rss,
                      "https://www.tmtpost.com/feed", "https://www.tmtpost.com/", "RSS·钛媒体")
    return r

def fetch_ifanr():
    r, _ = safe_fetch("ifanr", _rss, "https://www.ifanr.com/feed", "https://www.ifanr.com/")
    return r

def fetch_sspai():
    r, _ = safe_fetch("sspai", _rss, "https://sspai.com/feed", "https://sspai.com/")
    return r

def fetch_ithome():
    r, _ = safe_fetch("ithome", _rss, "https://www.ithome.com/rss/", "https://www.ithome.com/")
    return r

# ── GitHub Trending ──────────────────────────────────────
def _fetch_github():
    # GitHub 国内访问不稳定，且全局 session 带重试会放大超时（3 次重试 ≈ 40s）。
    # 这里用独立的无重试请求，单端点 5s 超时，失败立即 fallback，总耗时可控在 10s 内。
    import requests as _rq
    _gh_sess = _rq.Session()
    ua = _random_ua()
    # 端点1: GitHub Trending 页面（网络可达时优先，时效性强）
    try:
        r = _gh_sess.get(
            "https://github.com/trending?since=daily&spoken_language_code=zh",
            headers={"User-Agent": ua, "Referer": "https://github.com/",
                     "Accept-Encoding": "gzip, deflate, br"},
            timeout=5
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "lxml")
            repos = soup.select("article.Box-row")[:20]
            items = []
            for i, repo in enumerate(repos, 1):
                h2    = repo.find("h2")
                stars = repo.find("a", href=lambda x: x and "/stargazers" in x)
                if h2:
                    title = " ".join(h2.get_text(strip=True).split())
                    link  = "https://github.com" + h2.find("a")["href"] if h2.find("a") else "https://github.com/"
                    hot   = stars.get_text(strip=True).replace("\n","").strip() if stars else ""
                    items.append({"rank": i, "title": title, "url": link, "hot": hot})
            if items:
                return make_result(items, False, "日榜·非实时更新")
    except Exception:
        pass
    # 端点2: GitHub Search API（trending 不可达时的兜底，国内通常可达）
    try:
        r = _gh_sess.get(
            "https://api.github.com/search/repositories?q=created:>2026-06-01+language:python&sort=stars&order=desc&per_page=20",
            headers={"User-Agent": ua, "Accept": "application/json",
                     "Accept-Encoding": "gzip"},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            raw = data.get("items", [])
            items = [{"rank": i, "title": repo.get("full_name", ""),
                      "url": repo.get("html_url", "https://github.com/"),
                      "hot": f"{repo.get('stargazers_count', 0)} stars"}
                     for i, repo in enumerate(raw[:20], 1) if repo.get("full_name")]
            if items:
                return make_result(items, False, "热门仓库·API")
    except Exception:
        pass
    return fail_result("GitHub（暂不可用）")

def fetch_github():
    r, _ = safe_fetch("github", _fetch_github)
    return r


# ══════════════════════════════════════════════════════════════
# 【娱乐影视】
# ══════════════════════════════════════════════════════════════

# ── 豆瓣电影（强可靠）──────────────────────────────────
def _fetch_douban():
    data, err = http_get(
        "https://movie.douban.com/j/search_subjects?type=movie&tag=%E7%83%AD%E9%97%A8&sort=recommend&page_limit=20&page_start=0",
        referer="https://movie.douban.com/", parse_json=True, timeout=12
    )
    if err:
        return fail_result(f"豆瓣电影（{err}）")
    raw = data.get("subjects", [])
    items = [{"rank": i+1, "title": it.get("title",""),
              "url": it.get("url","https://movie.douban.com/"),
              "hot": f"评分 {it.get('rate','')}"}
             for i, it in enumerate(raw[:20])]
    return make_result(items, True, "实时热门电影") if items else fail_result("豆瓣电影（无数据）")

def fetch_douban():
    r, _ = safe_fetch("douban", _fetch_douban)
    return r

# ── 猫眼电影 ────────────────────────────────────────────
def _fetch_maoyan():
    # board/4 经典榜、board/7 热映口碑榜（结构改版，电影标题在 /films/ 链接里）
    for board in ["7", "4", "6"]:
        resp, err = http_get(f"https://www.maoyan.com/board/{board}",
                             referer="https://www.maoyan.com/", timeout=15)
        if err:
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for a in soup.select("a[href*='/films/']"):
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if title and 1 < len(title) < 30 and title not in seen:
                seen.add(title)
                items.append({"rank": len(items) + 1, "title": title,
                              "url": "https://www.maoyan.com" + href, "hot": ""})
            if len(items) >= 20:
                break
        if len(items) >= 5:
            note = "热映口碑榜" if board == "7" else ("经典榜" if board == "4" else "期待榜")
            return make_result(items[:20], True, note)
    return fail_result("猫眼电影（无数据）")

def fetch_maoyan():
    r, _ = safe_fetch("maoyan", _fetch_maoyan)
    return r

# ── 微博娱乐热搜 ───────────────────────────────────────
def _fetch_weibo_ent():
    data, err = http_get(
        "https://weibo.com/ajax/side/hotSearch",
        referer="https://weibo.com/", parse_json=True, timeout=12
    )
    if err:
        return fail_result(f"微博娱乐（{err}）")
    raw = data.get("data", {}).get("realtime", [])
    items = []
    rank = 1
    for item in raw:
        if rank > 15:
            break
        cat = str(item.get("category","") or item.get("label",""))
        word = item.get("word","")
        if "娱乐" in cat or "影视" in cat or "明星" in cat:
            num = item.get("num","")
            hot = f"{int(num)//10000}万" if str(num).isdigit() else str(num)
            items.append({"rank": rank, "title": word,
                          "url": f"https://s.weibo.com/weibo?q={requests.utils.quote(word)}",
                          "hot": hot})
            rank += 1
    if not items:
        for i, item in enumerate(raw[:10], 1):
            word = item.get("word","")
            if word:
                items.append({"rank": i, "title": word,
                              "url": f"https://s.weibo.com/weibo?q={requests.utils.quote(word)}",
                              "hot": ""})
    return make_result(items, True) if items else fail_result("微博娱乐（无数据）")

def fetch_weibo_ent():
    r, _ = safe_fetch("weibo_ent", _fetch_weibo_ent)
    return r

# ── 新浪娱乐 ────────────────────────────────────────────
def _fetch_sina_ent():
    # 端点1: RSS
    for url in [
        "https://rss.sina.com.cn/news/ent/yule.xml",
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=1686&num=20&page=1",
    ]:
        content, err = http_get(url, referer="https://ent.sina.com.cn/", raw_bytes=True, timeout=12)
        if not err:
            for enc in ["utf-8", "gb2312", "gbk", "gb18030"]:
                try:
                    soup = BeautifulSoup(content.decode(enc), "xml")
                    items = []
                    for i, it in enumerate(soup.find_all("item")[:20]):
                        title_el = it.find("title")
                        if title_el:
                            title = title_el.get_text(strip=True)
                            link_el = it.find("link")
                            link = link_el.get_text(strip=True) if link_el else "https://ent.sina.com.cn/"
                            items.append({"rank": i+1, "title": title, "url": link, "hot": ""})
                    if items:
                        return make_result(items, False, "RSS·非实时")
                    break
                except (UnicodeDecodeError, LookupError):
                    continue

    # 端点2: 新浪娱乐首页 HTML（item / ty-card-tt class 含标题）
    for page_url in ["https://ent.sina.com.cn/", "https://ent.sina.cn/"]:
        resp, err = http_get(page_url, referer="https://ent.sina.com.cn/", timeout=15)
        if err:
            continue
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for sel in [".ty-card-tt a", ".item a", "h2 a", "h3 a",
                    ".news-title a", ".article-title a", "a[href*='doc-']",
                    "a[href*='sina.cn']"]:
            for a in soup.select(sel)[:30]:
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if title and 6 < len(title) < 70 and title not in seen and "sina" in href:
                    seen.add(title)
                    items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
                if len(items) >= 20:
                    break
            if len(items) >= 20:
                break
        if len(items) >= 5:
            return make_result(items[:20], True, "实时娱乐")
    return fail_result("新浪娱乐（暂不可用）")

def fetch_sina_ent():
    r, _ = safe_fetch("sina_ent", _fetch_sina_ent)
    return r

# ── 凤凰娱乐 ────────────────────────────────────────────
def _fetch_ifeng_ent():
    # RSS 已下线，改抓凤凰娱乐首页 HTML（h3 / index_text 为标题链接）
    return _html_list(
        "https://ent.ifeng.com/", "https://ent.ifeng.com/",
        selectors=["h3 a", ".index_text_02_qj-1v a", ".index_text_04_YVFpW a",
                   ".style_vBlack_FKLqo a", "h2 a", ".article-title a",
                   "a[href*='/c/']"],
        host_filter="ifeng.com", abs_host="https://ent.ifeng.com",
        note="实时娱乐",
    )

def fetch_ifeng_ent():
    r, _ = safe_fetch("ifeng_ent", _fetch_ifeng_ent)
    return r


# ══════════════════════════════════════════════════════════════
# 【财经商业】
# ══════════════════════════════════════════════════════════════

def _fetch_caixin():
    # RSS 已失效，财新首页含静态新闻链接，多频道抓取
    for page_url in ["https://www.caixin.com/", "https://economy.caixin.com/", "https://finance.caixin.com/"]:
        r = _html_list(
            page_url, "https://www.caixin.com/",
            selectors=["a[href*='caixin.com/']"],
            host_filter="caixin.com", abs_host="https://www.caixin.com",
            note="实时资讯", min_len=12,
        )
        if r.get("status") == "success" and len(r.get("items", [])) >= 5:
            return r
    return fail_result("财新（暂不可用）")

def fetch_caixin():
    r, _ = safe_fetch("caixin", _fetch_caixin)
    return r

def _fetch_yicai():
    # RSS 已下线，改抓第一财经新闻页 HTML（f-db class 为列表项）
    return _html_list(
        "https://www.yicai.com/news/", "https://www.yicai.com/",
        selectors=[".f-db a", "h2 a", "h3 a", ".news-title a",
                   ".article-title a", "a[href*='/news/']"],
        host_filter="yicai.com", abs_host="https://www.yicai.com",
        note="实时资讯",
    )

def fetch_yicai():
    r, _ = safe_fetch("yicai", _fetch_yicai)
    return r

def _fetch_jiemian():
    # RSS 已下线，改抓界面首页 HTML（.statistical_link 为列表链接）
    return _html_list(
        "https://www.jiemian.com/", "https://www.jiemian.com/",
        selectors=["a.statistical_link", ".statistical_link",
                   "h2 a", "h3 a", ".article-title a", "a[href*='/article/']"],
        host_filter="jiemian.com", abs_host="https://www.jiemian.com",
        note="实时资讯",
    )

def fetch_jiemian():
    r, _ = safe_fetch("jiemian", _fetch_jiemian)
    return r

# ── 华尔街见闻 ─────────────────────────────────────────────
def _fetch_wallstreet():
    # 实测可用：api-one-wscn.awtmt.com 的快讯接口
    for url in [
        "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=20",
        "https://api-one.wallstcn.com/apiv1/content/articles?channel=global-channel&limit=20",
    ]:
        data, err = http_get(url, referer="https://wallstreetcn.com/",
                             accept="application/json", parse_json=True, timeout=15)
        if not err and data:
            raw = (data.get("data", {}).get("items", [])
                   or data.get("data", [])
                   or data.get("results", []))
            items = []
            for i, item in enumerate(raw[:20], 1):
                content = (item.get("title", "") or item.get("content_text", "")
                           or item.get("description", "") or item.get("summary", ""))
                if content:
                    content = content.strip().split("\n")[0][:60]
                    uri = item.get("uri", "") or ""
                    lid = item.get("id", "")
                    link = (f"https://wallstreetcn.com{uri}" if uri
                            else f"https://wallstreetcn.com/live/livenews/{lid}" if lid
                            else "https://wallstreetcn.com/")
                    items.append({"rank": i, "title": content, "url": link, "hot": ""})
            if items:
                return make_result(items, True, "实时快讯")
    return fail_result("华尔街见闻（暂不可用）")

def fetch_wallstreet():
    r, _ = safe_fetch("wallstreet", _fetch_wallstreet)
    return r

# ── 东方财富快讯（替代雪球——雪球需登录 Cookie 不可用）──────────────
def _fetch_xueqiu():
    import re
    resp, err = http_get(
        "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html",
        referer="https://www.eastmoney.com/", timeout=12
    )
    if err:
        return fail_result(f"东方财富（{err}）")
    try:
        m = re.search(r"ajaxResult\s*=\s*(\{.*\})", resp.text, re.DOTALL)
        data = json.loads(m.group(1)) if m else {}
        raw = data.get("LivesList", []) or data.get("data", [])
        items = []
        for i, it in enumerate(raw[:20], 1):
            title = it.get("title", "") or it.get("digest", "")
            url = it.get("url_w", "") or it.get("url", "") or "https://www.eastmoney.com/"
            if title:
                items.append({"rank": i, "title": title, "url": url, "hot": ""})
        if items:
            return make_result(items, True, "实时快讯·东财")
    except (json.JSONDecodeError, AttributeError):
        pass
    return fail_result("东方财富（暂不可用）")

def fetch_xueqiu():
    r, _ = safe_fetch("xueqiu", _fetch_xueqiu)
    return r


# ══════════════════════════════════════════════════════════════
# 【军事国际】
# ══════════════════════════════════════════════════════════════

def _fetch_guancha():
    # RSS 已下线，改抓观察者网首页 HTML（h4 为文章标题）
    return _html_list(
        "https://www.guancha.cn/", "https://www.guancha.cn/",
        selectors=["h4 a", "h3 a", ".article-title a", ".art-title a",
                   ".headline-title a", "a[href*='.shtml']"],
        host_filter="guancha.cn", abs_host="https://www.guancha.cn",
        note="实时资讯",
    )

def fetch_guancha():
    r, _ = safe_fetch("guancha", _fetch_guancha)
    return r

def _fetch_huanqiu():
    # RSS 已下线，改用环球网 JSON API（实测可用）
    data, err = http_get(
        "https://www.huanqiu.com/api/list?node=%22/hqmh%22&offset=0&limit=20",
        referer="https://www.huanqiu.com/", parse_json=True, timeout=12
    )
    if not err and data:
        raw = data.get("list", []) or data.get("items", [])
        items = []
        for i, it in enumerate(raw[:20], 1):
            title = it.get("title", "")
            aid = it.get("aid", "") or it.get("url", "")
            if title:
                url = aid if str(aid).startswith("http") else f"https://www.huanqiu.com/article/{aid}"
                items.append({"rank": i, "title": title, "url": url, "hot": ""})
        if items:
            return make_result(items, True, "实时资讯")
    # HTML 兜底
    return _html_list("https://www.huanqiu.com/", "https://www.huanqiu.com/",
                      selectors=["a.news-title", "h3 a", "h4 a", ".article-title a",
                                 "a.author-name", ".news-link", "a[href*='/article/']"],
                      host_filter="huanqiu.com", abs_host="https://www.huanqiu.com",
                      note="实时资讯")

def fetch_huanqiu():
    r, _ = safe_fetch("huanqiu", _fetch_huanqiu)
    return r

# ── 参考消息 → 用新华社世界频道代替 ──────────────────────
def _fetch_cankaoxiaoxi():
    # 原 RSS 已下线，改抓新华网世界频道 HTML（国际新闻）
    return _html_list(
        "https://www.news.cn/world/", "https://www.news.cn/",
        selectors=["h3 a", ".news-title a", ".tit a", "a[href*='/news.cn/']",
                   ".partList a", ".dataList a", ".domPC_a a"],
        host_filter="news.cn", abs_host="https://www.news.cn",
        note="国际新闻·非实时",
    )

def fetch_cankaoxiaoxi():
    r, _ = safe_fetch("cankaoxiaoxi", _fetch_cankaoxiaoxi)
    return r


# ══════════════════════════════════════════════════════════════
# 【体育】
# ══════════════════════════════════════════════════════════════

# ── 虎扑（多端点回退）──────────────────────────────────────
import re as _re_mod
_HUPU_NOISE = _re_mod.compile(r"(下载|打开|安装).{0,6}(App|APP|客户端)|虎扑APP")

def _fetch_hupu():
    for url in ["https://bbs.hupu.com/all", "https://www.hupu.com/"]:
        resp, err = http_get(url, referer="https://www.hupu.com/", timeout=15)
        if not err:
            soup = BeautifulSoup(resp.text, "lxml")
            seen = set()
            items = []
            for sel in [
                ".post-title a", ".title a", "h3 a",
                ".item-title a", ".bbs-sl-web-post-body a",
                ".floor-content-title a", "a[href*='bbs.hupu.com']",
                ".topic-title a", ".trending-title a",
            ]:
                if len(items) >= 40:
                    break
                for a in soup.select(sel)[:60]:
                    title = a.get_text(strip=True)
                    href  = a.get("href", "")
                    if (title and len(title) > 6 and title not in seen
                            and ("hupu.com" in href or href.startswith("/"))):
                        seen.add(title)
                        if not href.startswith("http"):
                            href = "https://bbs.hupu.com" + href
                        items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            items = [it for it in items if not _HUPU_NOISE.search(it["title"])][:20]
            for i, it in enumerate(items, 1):
                it["rank"] = i
            if len(items) >= 5:
                return make_result(items, True, "实时热帖")

    # NBA 热帖
    resp, err = http_get("https://bbs.hupu.com/nba", referer="https://bbs.hupu.com/", timeout=15)
    if not err:
        soup = BeautifulSoup(resp.text, "lxml")
        items = []
        seen = set()
        for sel in ["h3 a", ".title a", ".post-title a"]:
            for a in soup.select(sel)[:25]:
                title = a.get_text(strip=True)
                href  = a.get("href","")
                if title and len(title) > 6 and title not in seen:
                    seen.add(title)
                    if not href.startswith("http"):
                        href = "https://bbs.hupu.com" + href
                    items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
                if len(items) >= 20:
                    break
        if items:
            return make_result(items, True, "NBA热帖")
    return fail_result("虎扑（暂不可用）")

def fetch_hupu():
    r, _ = safe_fetch("hupu", _fetch_hupu)
    return r

# ── 懂球帝 ─────────────────────────────────────────────
def _fetch_dongqiudi():
    # 懂球帝主站反爬严重（移动版精简页 / API 403），改用直播吧移动版（GBK 编码）作为体育资讯源
    resp, err = http_get("https://m.zhibo8.com/", referer="https://m.zhibo8.com/",
                         timeout=15, raw_bytes=True)
    if err:
        return fail_result(f"体育资讯（{err}）")
    # 直播吧为 GBK 编码
    text = None
    for enc in ["utf-8", "gb18030", "gbk"]:
        try:
            text = resp.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not text:
        return fail_result("体育资讯（编码错误）")
    soup = BeautifulSoup(text, "lxml")
    seen = set()
    items = []
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if title and len(title) > 8 and "/news/" in href and title not in seen:
            seen.add(title)
            if not href.startswith("http"):
                href = "https://m.zhibo8.com" + href
            items.append({"rank": len(items) + 1, "title": title, "url": href, "hot": ""})
        if len(items) >= 20:
            break
    if len(items) >= 5:
        return make_result(items[:20], True, "体育资讯·直播吧")
    return fail_result("懂球帝（暂不可用）")

def fetch_dongqiudi():
    r, _ = safe_fetch("dongqiudi", _fetch_dongqiudi)
    return r

# ── 央视体育 ───────────────────────────────────────
def _fetch_cctv_sports():
    # RSS 已下线，改抓体育频道 HTML
    return _html_list(
        "https://sports.cctv.com/", "https://sports.cctv.com/",
        selectors=[".title a", "h3 a", ".news-title a", ".word a",
                   ".cetitle a", "a[href*='/202']"],
        host_filter="cctv.com", abs_host="https://sports.cctv.com",
        note="体育新闻·非实时",
    )

def fetch_cctv_sports():
    r, _ = safe_fetch("cctv_sports", _fetch_cctv_sports)
    return r


# ══════════════════════════════════════════════════════════════
# 【用户认证】（保持不变）
# ══════════════════════════════════════════════════════════════

def hash_password(pw):
    return hashlib.sha256((pw + SECRET_KEY).encode()).hexdigest()

def make_token(user_id, username):
    if not JWT_AVAILABLE:
        import base64
        payload = json.dumps({"user_id": user_id, "username": username, "exp": time.time() + 86400 * 30})
        return base64.b64encode(payload.encode()).decode()
    payload = {"user_id": user_id, "username": username, "exp": time.time() + 86400 * 30}
    return pyjwt.encode(payload, SECRET_KEY, algorithm="HS256")

def verify_token(token):
    if not token:
        return None
    try:
        if JWT_AVAILABLE:
            payload = pyjwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        else:
            import base64
            payload = json.loads(base64.b64decode(token.encode()).decode())
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None

def get_current_user():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return verify_token(auth[7:])
    return verify_token(request.cookies.get("token") or request.args.get("token"))


# ─── 平台注册表 ─────────────────────────────────────────────
FETCHERS = {
    "weibo":       fetch_weibo,
    "tencent":     fetch_tencent,
    "toutiao":     fetch_toutiao,
    "baidu":       fetch_baidu,
    "douyin":      fetch_douyin,
    "tieba":       fetch_tieba,
    "wangyi":      fetch_wangyi,
    "sina":        fetch_sina,
    "rmrb":        fetch_rmrb,
    "cctv":        fetch_cctv,
    "xinhua":      fetch_xinhua,
    "pengpai":     fetch_pengpai,
    "zhihu":       fetch_zhihu,
    "bilibili":    fetch_bilibili,
    "36kr":        fetch_36kr,
    "huxiu":       fetch_huxiu,
    "juejin":      fetch_juejin,
    "oschina":     fetch_oschina,
    "tmtpost":     fetch_tmtpost,
    "ifanr":       fetch_ifanr,
    "sspai":       fetch_sspai,
    "ithome":      fetch_ithome,
    "github":      fetch_github,
    "douban":      fetch_douban,
    "maoyan":      fetch_maoyan,
    "weibo_ent":   fetch_weibo_ent,
    "sina_ent":    fetch_sina_ent,
    "ifeng_ent":   fetch_ifeng_ent,
    "caixin":      fetch_caixin,
    "yicai":       fetch_yicai,
    "jiemian":     fetch_jiemian,
    "wallstreet":  fetch_wallstreet,
    "xueqiu":      fetch_xueqiu,
    "guancha":     fetch_guancha,
    "huanqiu":     fetch_huanqiu,
    "cankaoxiaoxi": fetch_cankaoxiaoxi,
    "hupu":        fetch_hupu,
    "dongqiudi":   fetch_dongqiudi,
    "cctv_sports": fetch_cctv_sports,
}

CATEGORIES = {
    "综合":    ["weibo","tencent","baidu","douyin","tieba","toutiao","wangyi","sina","pengpai","zhihu","bilibili","rmrb","cctv","xinhua"],
    "科技":    ["36kr","huxiu","juejin","oschina","tmtpost","ifanr","sspai","ithome","github"],
    "娱乐":    ["douban","maoyan","weibo_ent","sina_ent","ifeng_ent"],
    "财经":    ["caixin","yicai","jiemian","wallstreet","xueqiu"],
    "军事国际": ["guancha","huanqiu","cankaoxiaoxi"],
    "体育":    ["hupu","dongqiudi","cctv_sports"],
}

PLATFORM_NAMES = {
    "weibo": "微博热搜", "tencent": "腾讯新闻", "toutiao": "今日头条",
    "baidu": "百度热搜", "douyin": "抖音热搜", "tieba": "贴吧热议",
    "wangyi": "网易新闻", "sina": "新浪新闻", "rmrb": "人民日报",
    "cctv": "央视新闻", "xinhua": "新华社", "pengpai": "澎湃新闻",
    "zhihu": "知乎热搜", "bilibili": "B站排行", "36kr": "36氪",
    "huxiu": "虎嗅·雷锋网", "juejin": "掘金热榜", "oschina": "开源中国",
    "tmtpost": "钛媒体", "ifanr": "爱范儿", "sspai": "少数派",
    "ithome": "IT之家", "github": "GitHub趋势", "douban": "豆瓣电影",
    "maoyan": "猫眼电影", "weibo_ent": "微博娱乐", "sina_ent": "新浪娱乐",
    "ifeng_ent": "凤凰娱乐", "caixin": "财新", "yicai": "第一财经",
    "jiemian": "界面新闻", "wallstreet": "华尔街见闻", "xueqiu": "东方财富",
    "guancha": "观察者网", "huanqiu": "环球时报", "cankaoxiaoxi": "参考消息",
    "hupu": "虎扑", "dongqiudi": "懂球帝", "cctv_sports": "央视体育",
}

# 底层抓取函数映射（跳过缓存，供后台刷新线程使用）
RAW_FETCHERS = {
    "weibo":       _fetch_weibo,
    "tencent":     _fetch_tencent,
    "toutiao":     _fetch_toutiao,
    "baidu":       _fetch_baidu,
    "douyin":      _fetch_douyin,
    "tieba":       _fetch_tieba,
    "wangyi":      _fetch_wangyi,
    "sina":        _fetch_sina,
    "rmrb":        _fetch_rmrb,
    "cctv":        _fetch_cctv,
    "xinhua":      _fetch_xinhua,
    "pengpai":     _fetch_pengpai,
    "zhihu":       _fetch_zhihu,
    "bilibili":    _fetch_bilibili,
    "36kr":        _fetch_36kr,
    "huxiu":       _fetch_huxiu,
    "juejin":      _fetch_juejin,
    "oschina":     lambda: _rss("https://www.oschina.net/news/rss", "https://www.oschina.net/", "RSS·开源资讯"),
    "tmtpost":     lambda: _rss("https://www.tmtpost.com/feed", "https://www.tmtpost.com/", "RSS·钛媒体"),
    "ifanr":       lambda: _rss("https://www.ifanr.com/feed", "https://www.ifanr.com/"),
    "sspai":       lambda: _rss("https://sspai.com/feed", "https://sspai.com/"),
    "ithome":      lambda: _rss("https://www.ithome.com/rss/", "https://www.ithome.com/"),
    "github":      _fetch_github,
    "douban":      _fetch_douban,
    "maoyan":      _fetch_maoyan,
    "weibo_ent":   _fetch_weibo_ent,
    "sina_ent":    _fetch_sina_ent,
    "ifeng_ent":   _fetch_ifeng_ent,
    "caixin":      _fetch_caixin,
    "yicai":       _fetch_yicai,
    "jiemian":     _fetch_jiemian,
    "wallstreet":  _fetch_wallstreet,
    "xueqiu":      _fetch_xueqiu,
    "guancha":     _fetch_guancha,
    "huanqiu":     _fetch_huanqiu,
    "cankaoxiaoxi": _fetch_cankaoxiaoxi,
    "hupu":        _fetch_hupu,
    "dongqiudi":   _fetch_dongqiudi,
    "cctv_sports": _fetch_cctv_sports,
}

# ─── 后台定时刷新 ─────────────────────────────────────────────
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "300"))  # 默认 5 分钟

_bg_refresh_running = False

def force_refresh_platform(pid):
    """强制刷新单个平台（跳过缓存，直接调用底层抓取函数）"""
    raw_fn = RAW_FETCHERS.get(pid)
    if not raw_fn:
        return
    try:
        result = raw_fn()
        if result and result.get("status") == "success" and result.get("items"):
            set_cache(pid, result)
    except Exception:
        pass

def refresh_all_platforms():
    """后台刷新所有平台（分批并行，避免同时打太多请求）"""
    all_pids = list(RAW_FETCHERS.keys())
    batch_size = 8
    for i in range(0, len(all_pids), batch_size):
        batch = all_pids[i:i+batch_size]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {executor.submit(force_refresh_platform, pid): pid for pid in batch}
            try:
                for future in concurrent.futures.as_completed(futures, timeout=60):
                    try:
                        future.result()
                    except Exception:
                        pass
            except concurrent.futures.TimeoutError:
                # 超时的任务不管了，下一轮再试
                pass

def start_background_refresh():
    """启动后台定时刷新线程（每个 gunicorn worker 调用一次）"""
    global _bg_refresh_running
    if _bg_refresh_running:
        return
    _bg_refresh_running = True

    def _loop():
        # 启动后先等 30 秒再开始第一次刷新（等 worker 完全就绪）
        time.sleep(30)
        while True:
            try:
                refresh_all_platforms()
            except Exception:
                traceback.print_exc()
            time.sleep(REFRESH_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()

# ─── 路由 ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/news/<platform>")
def get_news(platform):
    fn = FETCHERS.get(platform)
    if not fn:
        return jsonify({"error": f"unknown platform: {platform}"}), 404
    data = fn()
    return jsonify({"platform": platform, **data})

@app.route("/api/news/batch")
def get_batch():
    category   = request.args.get("category", "")
    platforms  = request.args.get("platforms", "")
    force      = request.args.get("refresh", "0") == "1"

    if category and category in CATEGORIES:
        ids = CATEGORIES[category]
    elif platforms:
        ids = [p.strip() for p in platforms.split(",") if p.strip() in FETCHERS]
    else:
        ids = list(FETCHERS.keys())

    result      = {}
    failed_list = []

    # 强制刷新模式：跳过缓存，直接调底层抓取函数
    fetcher_map = RAW_FETCHERS if force else FETCHERS

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(ids), 10)) as executor:
        futures = {executor.submit(fetcher_map[pid]): pid for pid in ids if pid in fetcher_map}
        try:
            for future in concurrent.futures.as_completed(futures, timeout=60):
                pid = futures.pop(future)
                try:
                    data = future.result()
                    if force and data and data.get("status") == "success" and data.get("items"):
                        set_cache(pid, data)
                    result[pid] = data
                    if data.get("status") == "failed" or not data.get("items"):
                        failed_list.append({
                            "platform": pid,
                            "name": PLATFORM_NAMES.get(pid, pid),
                            "note": data.get("update_note", "抓取失败"),
                        })
                except Exception:
                    result[pid] = fail_result()
                    failed_list.append({
                        "platform": pid,
                        "name": PLATFORM_NAMES.get(pid, pid),
                        "note": fail_result().get("update_note"),
                    })
        except concurrent.futures.TimeoutError:
            for pid in futures.values():
                r = fail_result("请求超时")
                set_cache(pid, r)
                result[pid] = r
                failed_list.append({
                    "platform": pid,
                    "name": PLATFORM_NAMES.get(pid, pid),
                    "note": "请求超时",
                })

    return jsonify({**result, "_meta": {
        "failed": failed_list,
        "failed_count": len(failed_list),
        "total_count": len(ids),
        "cached": not force,
        "last_refresh": now_str(),
    }})

@app.route("/api/categories")
def get_categories():
    return jsonify(CATEGORIES)

@app.route("/api/platforms")
def get_platforms():
    """返回平台名称和状态信息，供前端折叠区使用"""
    return jsonify(PLATFORM_NAMES)

@app.route("/api/ping")
def ping():
    return jsonify({"ok": True, "ts": now_str(), "server_time": now_full()})

@app.route("/api/health")
def health():
    """健康检查：统计各平台缓存状态，便于部署后监控可用率"""
    now = time.time()
    fresh_ok, stale_ok, failed = 0, 0, 0
    detail = {}
    with _lock:
        for pid in FETCHERS:
            item = _cache.get(pid)
            if not item:
                detail[pid] = "uncached"; continue
            age = now - item.get("ts", 0)
            data = item.get("data", {})
            ok = data.get("status") == "success" and bool(data.get("items"))
            if not ok:
                failed += 1; detail[pid] = "failed"
            elif age < CACHE_TTL * 2:
                fresh_ok += 1; detail[pid] = f"fresh({int(age)}s)"
            else:
                stale_ok += 1; detail[pid] = f"stale({int(age)}s)"
    return jsonify({
        "ok": True,
        "platforms_total": len(FETCHERS),
        "fresh_ok": fresh_ok,
        "stale_ok": stale_ok,
        "failed": failed,
        "uncached": len(FETCHERS) - fresh_ok - stale_ok - failed,
        "cache_ttl": CACHE_TTL,
        "server_time": now_full(),
        "detail": detail,
    })

@app.route("/api/refresh")
def trigger_refresh():
    """手动触发全平台刷新（异步执行，立即返回）"""
    category = request.args.get("category", "")
    if category and category in CATEGORIES:
        pids = CATEGORIES[category]
    else:
        pids = list(RAW_FETCHERS.keys())

    def _refresh_batch():
        for i in range(0, len(pids), 8):
            batch = pids[i:i+8]
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = {executor.submit(force_refresh_platform, pid): pid for pid in batch}
                for future in concurrent.futures.as_completed(futures, timeout=45):
                    try:
                        future.result()
                    except Exception:
                        pass

    t = threading.Thread(target=_refresh_batch, daemon=True)
    t.start()
    return jsonify({"ok": True, "refreshing": len(pids), "ts": now_str()})

# ─── 用户认证接口 ─────────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if len(username) < 2 or len(username) > 20:
        return jsonify({"error": "用户名长度2-20"}), 400
    if len(password) < 6:
        return jsonify({"error": "密码至少6位"}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (username, password_hash) VALUES (?,?)",
                  (username, hash_password(password)))
        user_id = c.lastrowid
        conn.commit()
        token = make_token(user_id, username)
        return jsonify({"token": token, "username": username, "user_id": user_id})
    except sqlite3.IntegrityError:
        return jsonify({"error": "用户名已存在"}), 400
    finally:
        conn.close()

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    row = c.execute("SELECT id, password_hash FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row or row[1] != hash_password(password):
        return jsonify({"error": "用户名或密码错误"}), 401
    token = make_token(row[0], username)
    return jsonify({"token": token, "username": username, "user_id": row[0]})

@app.route("/api/auth/me")
def me():
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401
    return jsonify({"username": user["username"], "user_id": user["user_id"]})

# ─── 收藏接口 ─────────────────────────────────────────────────

@app.route("/api/favorites", methods=["GET"])
def get_favorites():
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, title, url, platform, saved_at FROM favorites WHERE user_id=? ORDER BY saved_at DESC LIMIT 200",
        (user["user_id"],)
    ).fetchall()
    conn.close()
    return jsonify([{"id": r[0], "title": r[1], "url": r[2], "platform": r[3], "saved_at": r[4]} for r in rows])

@app.route("/api/favorites", methods=["POST"])
def add_favorite():
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    url   = (data.get("url")   or "").strip()
    platform = data.get("platform", "")
    if not title or not url:
        return jsonify({"error": "参数不完整"}), 400
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT OR IGNORE INTO favorites (user_id, title, url, platform) VALUES (?,?,?,?)",
                     (user["user_id"], title, url, platform))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()

@app.route("/api/favorites/<int:fid>", methods=["DELETE"])
def del_favorite(fid):
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM favorites WHERE id=? AND user_id=?", (fid, user["user_id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ─── 浏览记录 ─────────────────────────────────────────────────

@app.route("/api/history", methods=["GET"])
def get_history():
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, title, url, platform, viewed_at FROM history WHERE user_id=? ORDER BY viewed_at DESC LIMIT 200",
        (user["user_id"],)
    ).fetchall()
    conn.close()
    return jsonify([{"id": r[0], "title": r[1], "url": r[2], "platform": r[3], "viewed_at": r[4]} for r in rows])

@app.route("/api/history", methods=["POST"])
def add_history():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False})
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    url   = (data.get("url")   or "").strip()
    platform = data.get("platform", "")
    if not title or not url:
        return jsonify({"ok": False})
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO history (user_id, title, url, platform) VALUES (?,?,?,?)",
                     (user["user_id"], title, url, platform))
        conn.execute("""DELETE FROM history WHERE user_id=? AND id NOT IN (
            SELECT id FROM history WHERE user_id=? ORDER BY viewed_at DESC LIMIT 500
        )""", (user["user_id"], user["user_id"]))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()

@app.route("/api/history", methods=["DELETE"])
def clear_history():
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM history WHERE user_id=?", (user["user_id"],))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ─── 启动 ─────────────────────────────────────────────────────
# ─── 启动时立即开启后台刷新 ────────────────────────────────
# gunicorn 环境下每个 worker 都会执行此模块，需要各自启动刷新线程
start_background_refresh()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("=" * 60)
    print("  热点聚合 v2.3 已启动")
    print(f"  访问地址: http://127.0.0.1:{port}")
    print(f"  平台数量: {len(FETCHERS)} 个")
    print(f"  后台刷新: 每 {REFRESH_INTERVAL}s 自动更新缓存")
    print("=" * 60)
    app.run(debug=False, host="0.0.0.0", port=port)

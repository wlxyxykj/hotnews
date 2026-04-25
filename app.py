"""
热点聚合工具 v2.2 - Flask 后端
策略：能抓的全力抓，抓不到的诚实标记「不可用」，不伪造数据
"""

import os, time, threading, traceback, hashlib, json, sqlite3
from datetime import datetime, timezone, timedelta
import concurrent.futures

import requests
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

# ─── 工具函数 ──────────────────────────────────────────
def now_str():
    """返回北京时间 HH:MM 格式"""
    return datetime.now(BEIJING_TZ).strftime("%H:%M")

def now_full():
    """返回完整北京时间字符串"""
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")

# 更强 headers 池，轮流使用降低被封概率
HEADERS_POOL = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
    },
    {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    },
]

import random
def headers(ref=None):
    h = dict(random.choice(HEADERS_POOL))
    if ref:
        h["Referer"] = ref
    return h

def make_result(items, is_realtime=True, note=None, status="success"):
    return {
        "status": status,
        "items": items,
        "is_realtime": is_realtime,
        "fetched_at": now_str(),
        "update_note": note or ("实时榜单" if is_realtime else "非实时更新")
    }

def fail_result(msg="抓取失败", note=None):
    return {
        "status": "failed",
        "items": [],
        "is_realtime": False,
        "fetched_at": now_str(),
        "update_note": note or f"{msg}（{now_str()}）"
    }

def safe_fetch(key, fn, *args, **kwargs):
    """带缓存的安全抓取；返回 (result, is_failed)"""
    cached = get_cache(key)
    if cached:
        return cached, cached.get("status") == "failed"
    try:
        result = fn(*args, **kwargs)
        if result and result.get("status") == "success" and result.get("items"):
            set_cache(key, result)
            return result, False
        # 有 items 但 status="failed" 或 items 为空
        if result:
            set_cache(key, result)
            return result, True
    except Exception:
        traceback.print_exc()
    return fail_result(), True


# ══════════════════════════════════════════════════════════════
# 【综合新闻】
# ══════════════════════════════════════════════════════════════

# ── 微博热搜（强可靠）───────────────────────────────────
def _fetch_weibo():
    resp = requests.get(
        "https://weibo.com/ajax/side/hotSearch",
        headers=headers("https://weibo.com/"),
        timeout=10
    )
    resp.raise_for_status()
    raw = resp.json().get("data", {}).get("realtime", [])
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
    return make_result(items, True)

def fetch_weibo():
    r, _ = safe_fetch("weibo", _fetch_weibo)
    return r

# ── 腾讯新闻（强可靠）───────────────────────────────────
def _fetch_tencent():
    resp = requests.get(
        "https://i.news.qq.com/gw/event/hot_ranking_list?offset=0&count=20&strategy=1",
        headers=headers("https://news.qq.com/"),
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    news_list = (data.get("idlist", [{}])[0].get("newslist", [])
                 or data.get("data", {}).get("hotRankingList", []))
    items = []
    for i, item in enumerate(news_list[:20], 1):
        title = item.get("title") or item.get("hotTitle", "")
        url   = item.get("url") or item.get("articleUrl", "https://news.qq.com/")
        hot   = str(item.get("hotScore") or item.get("readCount", ""))
        if title:
            items.append({"rank": i, "title": title, "url": url, "hot": hot})
    return make_result(items, True)

def fetch_tencent():
    r, _ = safe_fetch("tencent", _fetch_tencent)
    return r

# ── 今日头条（热榜页 + 多 selector HTML 解析）───────────────
def _fetch_toutiao():
    # 端点1: 头条热榜页面（新版）
    try:
        resp = requests.get("https://www.toutiao.com/hot-list/hot-search/",
                          headers={
                              "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                              "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                              "Accept-Language": "zh-CN,zh;q=0.9",
                              "Referer": "https://www.toutiao.com/",
                          }, timeout=12)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")
            seen = set()
            items = []
            for sel in [
                ".article-title a", "h3 a", ".hot-title a",
                ".trending-title a", ".feed-card-title a",
                "a[href*='toutiao.com/i']", ".item-title a",
            ]:
                if len(items) >= 20:
                    break
                for a in soup.select(sel)[:30]:
                    title = a.get_text(strip=True)
                    href = a.get("href", "")
                    if title and len(title) > 5 and title not in seen:
                        seen.add(title)
                        if not href.startswith("http"):
                            href = "https://www.toutiao.com" + href
                        items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
                    if len(items) >= 20:
                        break
            if len(items) >= 5:
                return make_result(items, True, "实时热点")
    except Exception:
        pass

    # 端点2: 头条主页（备用）
    try:
        resp = requests.get("https://www.toutiao.com/",
                          headers={
                              "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                              "Accept-Language": "zh-CN,zh;q=0.9",
                          }, timeout=10)
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for a in soup.select("h3 a, .article-title a, a[href*='/i']")[:25]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if title and len(title) > 5 and title not in seen:
                seen.add(title)
                if not href.startswith("http"):
                    href = "https://www.toutiao.com" + href
                items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if len(items) >= 20:
                break
        if items:
            return make_result(items, True, "实时热点")
    except Exception:
        pass

    # 端点3: 尝试第三方镜像
    try:
        resp = requests.get("https://tcmarket.cdn.bceutils.com/hot-list/toutiao-hot-search.json",
                          headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            raw = data if isinstance(data, list) else data.get("data", [])
            items = [{"rank": i+1, "title": it.get("title",""),
                      "url": it.get("url","https://www.toutiao.com/"), "hot": str(it.get("hot",""))}
                     for i, it in enumerate(raw[:20]) if it.get("title")]
            if items:
                return make_result(items, True, "实时热点")
    except Exception:
        pass

    return fail_result("今日头条（暂不可用）")

def fetch_toutiao():
    r, _ = safe_fetch("toutiao", _fetch_toutiao)
    return r

# ── 网易新闻（真正的热榜接口）─────────────────────────────
def _fetch_wangyi():
    # 端点1: 网易新闻热搜 JSON 接口
    for url in [
        "https://news.163.com/rank/",
    ]:
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.163.com/",
            }, timeout=10)
            if resp.status_code == 200 and len(resp.text) > 300:
                resp.encoding = "utf-8"
                soup = BeautifulSoup(resp.text, "lxml")
                # 尝试多种热榜 selector（针对 news.163.com/rank/ 页面结构）
                for sel in [".hot-title a", ".hotlist-title a", ".news_title a",
                            "h3 a", ".title a", ".item-headline a"]:
                    links = soup.select(sel)[:20]
                    items = []
                    seen = set()
                    for i, a in enumerate(links, 1):
                        title = a.get_text(strip=True)
                        href  = a.get("href", "")
                        if title and len(title) > 4 and title not in seen:
                            seen.add(title)
                            if not href.startswith("http"):
                                href = "https://www.163.com" + href
                            items.append({"rank": i, "title": title, "url": href, "hot": ""})
                            if len(items) >= 20:
                                break
                    if items:
                        return make_result(items, True, "实时热榜")
        except Exception:
            pass
    
    # 端点2: 网易热搜专用页（temp.163.com 或 news.163.com/special）
    try:
        resp = requests.get("https://news.163.com/special/00804JVA/news_hot_list.js",
                          headers={
                              "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                              "Referer": "https://news.163.com/",
                              "Accept": "*/*",
                          }, timeout=10)
        if resp.status_code == 200:
            text = resp.text
            # 尝试解析 JSONP 回调
            try:
                import re
                # 尝试提取 JSON 数据
                json_str = text
                # 有些接口返回的是 define() 包裹的，需要提取
                match = re.search(r'\((.*?)\)', text, re.DOTALL)
                if match:
                    json_str = match.group(1)
                data = json.loads(json_str)
                raw = data if isinstance(data, list) else data.get("data", [])
                items = []
                for i, item in enumerate(raw[:20], 1):
                    title = item.get("title", "") or item.get("docTitle", "") or item.get("Keywords", "")
                    url = item.get("url", "") or item.get("docurl", "")
                    hot = item.get("hotValue", "") or item.get("hot", "")
                    if title:
                        if not url:
                            url = "https://www.163.com/"
                        items.append({"rank": i, "title": title, "url": url, "hot": str(hot)})
                if items:
                    return make_result(items, True, "实时热榜")
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    
    # 端点3: 尝试解析 163.com 主页的热榜区块
    try:
        resp = requests.get("https://www.163.com/",
                          headers={
                              "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                              "Accept-Language": "zh-CN,zh;q=0.9",
                          }, timeout=10)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        # 尝试多种 selector 找热搜条目
        for sel in [".hot-title a", ".news-title a", ".hotnews a", "a.hot"]:
            for a in soup.select(sel)[:15]:
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if title and len(title) > 5 and title not in seen:
                    seen.add(title)
                    if not href.startswith("http"):
                        href = "https://www.163.com" + href
                    items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
                if len(items) >= 15:
                    break
        if len(items) >= 5:
            return make_result(items, True, "实时热榜")
    except Exception:
        pass
    
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
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN",
                "Referer": "https://news.sina.com.cn/"}, timeout=10)
            if resp.status_code == 200:
                # 尝试多种编码
                content = resp.content
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
                        break  # 成功解析但没数据，也跳出
                    except (UnicodeDecodeError, LookupError):
                        continue
        except Exception:
            pass
    # 端点2: 新浪新闻首页
    try:
        resp = requests.get("https://news.sina.com.cn/",
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                   "Accept-Language": "zh-CN"}, timeout=10)
        content = resp.content
        # 尝试多种编码解析
        text = None
        for enc in ["utf-8", "gbk", "gb2312", "gb18030"]:
            try:
                text = content.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if text:
            soup = BeautifulSoup(text, "lxml")
            seen = set()
            items = []
            for a in soup.select("h1 a, h2 a, .news-title a, a[href*='sina.com.cn']"):
                title = a.get_text(strip=True)
                href  = a.get("href","")
                if (title and len(title) > 8 and title not in seen
                        and ("sina.com.cn" in href or href.startswith("/news"))):
                    seen.add(title)
                    if not href.startswith("http"):
                        href = "https://news.sina.com.cn" + href
                    items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if items:
                return make_result(items, True, "实时新闻")
    except Exception:
        pass
    return fail_result("新浪新闻（暂不可用）")

def fetch_sina():
    r, _ = safe_fetch("sina", _fetch_sina)
    return r

# ── 人民日报 RSS（可靠）─────────────────────────────────
def _fetch_rmrb():
    resp = requests.get("http://www.people.com.cn/rss/politics.xml",
                        headers=headers("https://www.people.com.cn/"), timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    items = [{"rank": i+1, "title": it.find("title").get_text(strip=True),
              "url": it.find("link").get_text(strip=True) if it.find("link") else "https://www.people.com.cn/",
              "hot": ""}
             for i, it in enumerate(soup.find_all("item")[:20]) if it.find("title")]
    return make_result(items, False, "RSS·非实时更新")

def fetch_rmrb():
    r, _ = safe_fetch("rmrb", _fetch_rmrb)
    return r

# ── 央视新闻 RSS（可靠）────────────────────────────────
def _fetch_cctv():
    resp = requests.get("https://news.cctv.com/rss/china.xml",
                        headers=headers("https://news.cctv.com/"), timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    items = [{"rank": i+1, "title": it.find("title").get_text(strip=True),
              "url": it.find("link").get_text(strip=True) if it.find("link") else "https://news.cctv.com/",
              "hot": ""}
             for i, it in enumerate(soup.find_all("item")[:20]) if it.find("title")]
    return make_result(items, False, "RSS·非实时更新")

def fetch_cctv():
    r, _ = safe_fetch("cctv", _fetch_cctv)
    return r

# ── 新华社 RSS（可靠）──────────────────────────────────
def _fetch_xinhua():
    resp = requests.get("https://www.news.cn/rss/politics.xml",
                        headers=headers("https://www.news.cn/"), timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    items = [{"rank": i+1, "title": it.find("title").get_text(strip=True),
              "url": it.find("link").get_text(strip=True) if it.find("link") else "https://www.news.cn/",
              "hot": ""}
             for i, it in enumerate(soup.find_all("item")[:20]) if it.find("title")]
    return make_result(items, False, "RSS·非实时更新")

def fetch_xinhua():
    r, _ = safe_fetch("xinhua", _fetch_xinhua)
    return r

# ── 澎湃新闻（API + 多种 HTML selector）────────────────────────
def _fetch_pengpai():
    # 端点1: 澎湃新版 API（v2）
    try:
        resp = requests.get(
            "https://api.thepaper.cn/v2/list/news?channel=要闻&limit=20&page=1",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Referer": "https://www.thepaper.cn/",
                "Origin": "https://www.thepaper.cn",
            }, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            raw = data.get("data", {}).get("list", []) or data.get("list", []) or data.get("stories", [])
            items = [{"rank": i+1, "title": it.get("title",""),
                      "url": it.get("url","https://www.thepaper.cn/") or f"https://www.thepaper.cn/detail/{it.get('id','')}",
                      "hot": ""}
                     for i, it in enumerate(raw[:20]) if it.get("title")]
            if items:
                return make_result(items, True, "实时要闻")
    except Exception:
        pass

    # 端点2: 澎湃列表页（新版 HTML，多 selector 组合）
    for page_url in [
        "https://www.thepaper.cn/list/25433",  # 要闻
        "https://www.thepaper.cn/list/25435",  # 视频
        "https://www.thepaper.cn/",
    ]:
        try:
            resp = requests.get(page_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.thepaper.cn/",
            }, timeout=10)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                soup = BeautifulSoup(resp.text, "lxml")
                seen = set()
                items = []
                # 尽可能多的 selector 组合
                for sel in [
                    ".news_title a", ".article_title a", ".index_title a",
                    ".con a", ".txt a", ".item-title a", ".feed-title a",
                    "h2 a", "h3 a", ".n-title a", ".card-title a",
                    "a[href*='thepaper.cn/content/']", "a[href*='thepaper.cn/news/']",
                ]:
                    if len(items) >= 20:
                        break
                    for a in soup.select(sel)[:30]:
                        title = a.get_text(strip=True)
                        href  = a.get("href", "")
                        if (title and len(title) > 8 and title not in seen
                                and ("thepaper" in href or href.startswith("/"))):
                            seen.add(title)
                            if not href.startswith("http"):
                                href = "https://www.thepaper.cn" + href
                            items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
                if len(items) >= 5:
                    return make_result(items[:20], True, "实时要闻")
        except Exception:
            pass
    return fail_result("澎湃新闻（暂不可用）")

def fetch_pengpai():
    r, _ = safe_fetch("pengpai", _fetch_pengpai)
    return r

# ── 知乎热搜（主用热榜 API，更可靠）────────────────────────
def _fetch_zhihu():
    # 知乎热榜 API（无需登录）
    for url, note in [
        ("https://www.zhihu.com/api/v4/topstory/hot-lists?limit=20&desktop=true", "实时热搜"),
        ("https://api.zhihu.com/topstory/hot-lists?limit=20", "实时热搜"),
    ]:
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://www.zhihu.com/",
            }, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                raw = data.get("data", []) or data.get("top_stories", []) or data.get("topstories", [])
                items = []
                for i, item in enumerate(raw[:20], 1):
                    if isinstance(item, dict):
                        target = item.get("target", {}) or item
                        title = target.get("title") or target.get("question", {}).get("title", "")
                        qid = target.get("id", "") or target.get("question", {}).get("id", "")
                        metric = target.get("vote_count", "") or target.get("follower_count", "") or ""
                        if str(metric).isdigit() and int(metric) > 1000:
                            metric = f"{int(metric)//10000}万"
                        u = f"https://www.zhihu.com/question/{qid}" if qid else "https://www.zhihu.com/"
                    else:
                        title = str(item); metric = ""; u = "https://www.zhihu.com/"
                    if title:
                        items.append({"rank": i, "title": title, "url": u, "hot": str(metric)})
                if items:
                    return make_result(items, True, note)
        except Exception:
            pass
    # 备用: 解析知乎热榜页面（桌面版）
    try:
        resp = requests.get("https://www.zhihu.com/hot", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }, timeout=12)
        if resp.status_code == 200:
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
    except Exception:
        pass
    return fail_result("知乎（暂不可用）")

def fetch_zhihu():
    r, _ = safe_fetch("zhihu", _fetch_zhihu)
    return r

# ── B站排行榜（强可靠）────────────────────────────────
def _fetch_bilibili():
    resp = requests.get(
        "https://api.bilibili.com/x/web-interface/ranking/v2",
        headers=headers("https://www.bilibili.com/"),
        timeout=10
    )
    resp.raise_for_status()
    raw = resp.json().get("data", {}).get("list", [])
    items = []
    for i, item in enumerate(raw[:20], 1):
        view = item.get("stat", {}).get("view", 0)
        items.append({"rank": i, "title": item.get("title", ""),
                      "url": f"https://www.bilibili.com/video/{item.get('bvid','')}",
                      "hot": f"{int(view)//10000}万播放" if view else ""})
    return make_result(items, True)

def fetch_bilibili():
    r, _ = safe_fetch("bilibili", _fetch_bilibili)
    return r


# ══════════════════════════════════════════════════════════════
# 【科技数码】
# ══════════════════════════════════════════════════════════════

def _rss(url, ref="", note="RSS·非实时更新"):
    """RSS 抓取，支持 HTTP/HTTPS 双协议 + 多编码容错"""
    # 尝试 HTTP/HTTPS 双协议
    for base_url in [url, url.replace("https://", "http://").replace("http://", "https://")]:
        try:
            resp = requests.get(base_url, headers=headers(ref), timeout=12)
            if resp.status_code != 200:
                continue
            # 尝试多种编码
            for enc in ["utf-8", "gb2312", "gbk", "gb18030", "utf-16"]:
                try:
                    text = resp.content.decode(enc)
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
                    break  # 解码成功但没数据也退出
                except (UnicodeDecodeError, LookupError):
                    continue
        except Exception:
            continue
    return fail_result(note or "RSS·非实时更新")

def fetch_36kr():
    # 端点1: 直接解析36kr首页（主用，更可靠）
    try:
        resp = requests.get("https://36kr.com/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://36kr.com/",
        }, timeout=12)
        if resp.status_code == 200:
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "lxml")
            seen = set()
            items = []
            for sel in [
                ".article-title a", ".feed-title a", ".kr-shadow-content a",
                "h3 a", ".item-title a", ".news-title a",
                "a[href*='36kr.com/p/']",
            ]:
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
    except Exception:
        pass
    # 端点2: RSS 保底
    try:
        resp = requests.get("https://36kr.com/feed", headers={
            "User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml,application/xml,*/*",
            "Accept-Language": "zh-CN"}, timeout=10)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "xml")
        items = [{"rank": i+1, "title": it.find("title").get_text(strip=True),
                  "url": it.find("link").get_text(strip=True) if it.find("link") else "https://36kr.com/",
                  "hot": ""}
                 for i, it in enumerate(soup.find_all("item")[:20]) if it.find("title")]
        if items:
            return make_result(items, False, "RSS·非实时")
    except Exception:
        pass
    return fail_result("36氪（暂不可用）")

def fetch_huxiu():
    # 端点1: 虎嗅首页 HTML（主用，更可靠）
    try:
        resp = requests.get("https://www.huxiu.com/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.huxiu.com/",
        }, timeout=12)
        if resp.status_code == 200:
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.text, "lxml")
            seen = set()
            items = []
            for sel in [
                ".article-title a", ".b-a-title a", ".t--lg a",
                "h3 a", ".title-text a", ".mod-art-title a",
                "a[href*='huxiu.com/article/']",
            ]:
                if len(items) >= 20:
                    break
                for a in soup.select(sel)[:30]:
                    title = a.get_text(strip=True)
                    href  = a.get("href","")
                    if title and len(title) > 6 and title not in seen:
                        seen.add(title)
                        if not href.startswith("http"):
                            href = "https://www.huxiu.com" + href
                        items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if items:
                return make_result(items, True, "实时资讯")
    except Exception:
        pass
    # 端点2: RSS 保底
    try:
        resp = requests.get("https://www.huxiu.com/rss/0.xml",
                          headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN"}, timeout=10)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "xml")
        items = [{"rank": i+1, "title": it.find("title").get_text(strip=True),
                  "url": it.find("link").get_text(strip=True) if it.find("link") else "https://www.huxiu.com/", "hot": ""}
                 for i, it in enumerate(soup.find_all("item")[:20]) if it.find("title")]
        if items:
            return make_result(items, False, "RSS·非实时")
    except Exception:
        pass
    return fail_result("虎嗅（暂不可用）")

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
    resp = requests.get(
        "https://github.com/trending?since=daily&spoken_language_code=zh",
        headers=headers("https://github.com/"),
        timeout=12
    )
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")
    repos = soup.select("article.Box-row")[:20]
    items = []
    for i, repo in enumerate(repos, 1):
        h2    = repo.find("h2")
        desc  = repo.find("p")
        stars = repo.find("a", href=lambda x: x and "/stargazers" in x)
        if h2:
            title = " ".join(h2.get_text(strip=True).split())
            link  = "https://github.com" + h2.find("a")["href"] if h2.find("a") else "https://github.com/"
            hot   = stars.get_text(strip=True).replace("\n","").strip() if stars else ""
            items.append({"rank": i, "title": title, "url": link, "hot": hot})
    return make_result(items, False, "日榜·非实时更新")

def fetch_github():
    r, _ = safe_fetch("github", _fetch_github)
    return r


# ══════════════════════════════════════════════════════════════
# 【娱乐影视】
# ══════════════════════════════════════════════════════════════

# ── 豆瓣电影（强可靠）──────────────────────────────────
def _fetch_douban():
    resp = requests.get(
        "https://movie.douban.com/j/search_subjects?type=movie&tag=%E7%83%AD%E9%97%A8&sort=recommend&page_limit=20&page_start=0",
        headers=headers("https://movie.douban.com/"),
        timeout=10
    )
    resp.raise_for_status()
    raw = resp.json().get("subjects", [])
    items = [{"rank": i+1, "title": it.get("title",""),
              "url": it.get("url","https://movie.douban.com/"),
              "hot": f"评分 {it.get('rate','')}"}
             for i, it in enumerate(raw[:20])]
    return make_result(items, True, "实时热门电影")

def fetch_douban():
    r, _ = safe_fetch("douban", _fetch_douban)
    return r

# ── 猫眼电影（可靠）────────────────────────────────────
def _fetch_maoyan():
    resp = requests.get(
        "https://www.maoyan.com/board/4",
        headers=headers("https://www.maoyan.com/"),
        timeout=10
    )
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")
    movies = soup.select(".movie-item-info")[:20]
    items = []
    for i, m in enumerate(movies, 1):
        title_el = m.find("p", class_="name")
        score_el = m.find("p", class_="score")
        link_el  = m.find("a")
        if title_el:
            link = ("https://www.maoyan.com" + link_el["href"]
                    if link_el and link_el.get("href") else "https://www.maoyan.com/")
            items.append({"rank": i, "title": title_el.get_text(strip=True),
                          "url": link, "hot": score_el.get_text(strip=True) if score_el else ""})
    return make_result(items, False, "非实时更新")

def fetch_maoyan():
    r, _ = safe_fetch("maoyan", _fetch_maoyan)
    return r

# ── 微博娱乐热搜 ───────────────────────────────────────
def _fetch_weibo_ent():
    resp = requests.get(
        "https://weibo.com/ajax/side/hotSearch",
        headers=headers("https://weibo.com/"),
        timeout=10
    )
    resp.raise_for_status()
    raw = resp.json().get("data", {}).get("realtime", [])
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
    return make_result(items, True)

def fetch_weibo_ent():
    r, _ = safe_fetch("weibo_ent", _fetch_weibo_ent)
    return r

# ── 新浪娱乐 RSS（多编码 + HTML 兜底）────────────────────
def _fetch_sina_ent():
    # 端点1: RSS（多编码）
    for url in [
        "https://rss.sina.com.cn/news/ent/yule.xml",
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=1686&num=20&page=1",
    ]:
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "zh-CN",
                "Referer": "https://ent.sina.com.cn/",
            }, timeout=10)
            if resp.status_code == 200:
                for enc in ["utf-8", "gb2312", "gbk", "gb18030"]:
                    try:
                        soup = BeautifulSoup(resp.content.decode(enc), "xml")
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
        except Exception:
            pass
    # 端点2: 新浪娱乐首页 HTML
    try:
        resp = requests.get("https://ent.sina.com.cn/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://ent.sina.com.cn/",
        }, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for a in soup.select("h2 a, h3 a, .news-title a, .article-title a")[:25]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if title and len(title) > 6 and title not in seen:
                seen.add(title)
                if not href.startswith("http"):
                    href = "https://ent.sina.com.cn" + href
                items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if len(items) >= 20:
                break
        if items:
            return make_result(items, True, "实时娱乐")
    except Exception:
        pass
    return fail_result("新浪娱乐（暂不可用）")

def fetch_sina_ent():
    r, _ = safe_fetch("sina_ent", _fetch_sina_ent)
    return r

# ── 凤凰娱乐 RSS ───────────────────────────────────────
def _fetch_ifeng_ent():
    # 端点1: RSS
    r = _rss("https://rss.ifeng.com/ent.xml", "https://ent.ifeng.com/", "RSS·非实时")
    if r and r.get("items"):
        return r
    # 端点2: 凤凰娱乐首页 HTML
    try:
        resp = requests.get("https://ent.ifeng.com/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://ent.ifeng.com/",
        }, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for a in soup.select("h2 a, h3 a, .article-title a, .news-title a")[:25]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if title and len(title) > 6 and title not in seen:
                seen.add(title)
                if not href.startswith("http"):
                    href = "https://ent.ifeng.com" + href
                items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if len(items) >= 20:
                break
        if items:
            return make_result(items, True, "实时娱乐")
    except Exception:
        pass
    return fail_result("凤凰娱乐（暂不可用）")

def fetch_ifeng_ent():
    r, _ = safe_fetch("ifeng_ent", _fetch_ifeng_ent)
    return r


# ══════════════════════════════════════════════════════════════
# 【财经商业】
# ══════════════════════════════════════════════════════════════

def _fetch_caixin():
    # 端点1: RSS
    r = _rss("https://www.caixin.com/rss/latest.xml", "https://www.caixin.com/", "RSS·非实时")
    if r and r.get("items"):
        return r
    # 端点2: 财新首页 HTML
    try:
        resp = requests.get("https://www.caixin.com/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.caixin.com/",
        }, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for a in soup.select("h2 a, h3 a, .article-title a, a[href*='caixin.com']")[:25]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if title and len(title) > 8 and title not in seen:
                seen.add(title)
                if not href.startswith("http"):
                    href = "https://www.caixin.com" + href
                items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if len(items) >= 20:
                break
        if items:
            return make_result(items, True, "实时资讯")
    except Exception:
        pass
    return fail_result("财新（暂不可用）")

def fetch_caixin():
    r, _ = safe_fetch("caixin", _fetch_caixin)
    return r

def _fetch_yicai():
    # 端点1: RSS
    r = _rss("https://www.yicai.com/rss", "https://www.yicai.com/", "RSS·非实时")
    if r and r.get("items"):
        return r
    # 端点2: 第一财经首页 HTML
    try:
        resp = requests.get("https://www.yicai.com/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.yicai.com/",
        }, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for a in soup.select("h2 a, h3 a, .article-title a, .news-title a")[:25]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if title and len(title) > 8 and title not in seen:
                seen.add(title)
                if not href.startswith("http"):
                    href = "https://www.yicai.com" + href
                items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if len(items) >= 20:
                break
        if items:
            return make_result(items, True, "实时资讯")
    except Exception:
        pass
    return fail_result("第一财经（暂不可用）")

def fetch_yicai():
    r, _ = safe_fetch("yicai", _fetch_yicai)
    return r

def _fetch_jiemian():
    # 端点1: RSS
    r = _rss("https://www.jiemian.com/lists/rss.html", "https://www.jiemian.com/", "RSS·非实时")
    if r and r.get("items"):
        return r
    # 端点2: 界面新闻首页 HTML
    try:
        resp = requests.get("https://www.jiemian.com/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.jiemian.com/",
        }, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for a in soup.select("h2 a, h3 a, .article-title a, .news-title a")[:25]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if title and len(title) > 8 and title not in seen:
                seen.add(title)
                if not href.startswith("http"):
                    href = "https://www.jiemian.com" + href
                items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if len(items) >= 20:
                break
        if items:
            return make_result(items, True, "实时资讯")
    except Exception:
        pass
    return fail_result("界面新闻（暂不可用）")

def fetch_jiemian():
    r, _ = safe_fetch("jiemian", _fetch_jiemian)
    return r

# ── 华尔街见闻 ─────────────────────────────────────────────
def _fetch_wallstreet():
    # 端点1: RSS（最可靠）
    r = _rss("https://wallstreetcn.com/rss", "https://wallstreetcn.com/", "RSS·非实时")
    if r and r.get("items"):
        return r
    # 端点2: 华尔街见闻新版 API
    for url in [
        "https://wallstreetcn.com/api/v2/lives/hot?limit=20&platform=pc",
        "https://api-pub.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=20",
    ]:
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://wallstreetcn.com/",
                "Origin": "https://wallstreetcn.com",
            }, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                raw = (data.get("data", {}).get("items", [])
                      or data.get("data", [])
                      or data.get("results", []))
                items = []
                for i, item in enumerate(raw[:20], 1):
                    content = item.get("content_text","") or item.get("title","") or item.get("summary","")
                    if content:
                        content = content[:60].strip()
                        items.append({"rank": i, "title": content,
                                      "url": f"https://wallstreetcn.com/articles/{item.get('id','')}",
                                      "hot": ""})
                if items:
                    return make_result(items, True, "实时快讯")
        except Exception:
            pass
    # 端点3: 主页 HTML 备用
    try:
        resp = requests.get("https://wallstreetcn.com/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for a in soup.select("h2 a, h3 a, .article-title a")[:20]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if title and len(title) > 8 and title not in seen:
                seen.add(title)
                if not href.startswith("http"):
                    href = "https://wallstreetcn.com" + href
                items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if len(items) >= 20:
                break
        if items:
            return make_result(items, True, "实时资讯")
    except Exception:
        pass
    return fail_result("华尔街见闻（暂不可用）")

def fetch_wallstreet():
    r, _ = safe_fetch("wallstreet", _fetch_wallstreet)
    return r

# ── 雪球（Cookie 限制）─────────────────────────────────
def _fetch_xueqiu():
    # 雪球需要 Cookie，直接返回不可用比返回过期数据更好
    return fail_result("雪球（需登录 Cookie，暂时不可用）")

def fetch_xueqiu():
    r, _ = safe_fetch("xueqiu", _fetch_xueqiu)
    return r


# ══════════════════════════════════════════════════════════════
# 【军事国际】
# ══════════════════════════════════════════════════════════════

def _fetch_guancha():
    # 端点1: RSS
    r = _rss("https://www.guancha.cn/rss.xml", "https://www.guancha.cn/", "RSS·非实时")
    if r and r.get("items"):
        return r
    # 端点2: 观察者网首页 HTML
    try:
        resp = requests.get("https://www.guancha.cn/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.guancha.cn/",
        }, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for a in soup.select("h3 a, h4 a, .article-title a, .art-title a, .title a")[:25]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if title and len(title) > 6 and title not in seen:
                seen.add(title)
                if not href.startswith("http"):
                    href = "https://www.guancha.cn" + href
                items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if len(items) >= 20:
                break
        if items:
            return make_result(items, True, "实时资讯")
    except Exception:
        pass
    return fail_result("观察者网（暂不可用）")

def fetch_guancha():
    r, _ = safe_fetch("guancha", _fetch_guancha)
    return r

def _fetch_huanqiu():
    # 端点1: RSS
    r = _rss("https://www.huanqiu.com/rss", "https://www.huanqiu.com/", "RSS·非实时")
    if r and r.get("items"):
        return r
    # 端点2: 环球网首页 HTML
    try:
        resp = requests.get("https://www.huanqiu.com/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.huanqiu.com/",
        }, timeout=12)
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()
        items = []
        for a in soup.select("h3 a, h4 a, .article-title a, .title a")[:25]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if title and len(title) > 6 and title not in seen:
                seen.add(title)
                if not href.startswith("http"):
                    href = "https://www.huanqiu.com" + href
                items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
            if len(items) >= 20:
                break
        if items:
            return make_result(items, True, "实时资讯")
    except Exception:
        pass
    return fail_result("环球时报（暂不可用）")

def fetch_huanqiu():
    r, _ = safe_fetch("huanqiu", _fetch_huanqiu)
    return r

# ── 参考消息 → 用新华社世界频道代替 ──────────────────────
def _fetch_cankaoxiaoxi():
    return _rss("https://www.news.cn/rss/world.xml", "https://www.news.cn/", "RSS·非实时")

def fetch_cankaoxiaoxi():
    r, _ = safe_fetch("cankaoxiaoxi", _fetch_cankaoxiaoxi)
    return r


# ══════════════════════════════════════════════════════════════
# 【体育】
# ══════════════════════════════════════════════════════════════

# ── 虎扑（新版页面解析，多 selector 兜底）───────────────────────────────
def _fetch_hupu():
    # 端点1: 虎扑首页热帖（全面多 selector）
    for url in ["https://bbs.hupu.com/all", "https://www.hupu.com/"]:
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.hupu.com/",
            }, timeout=12)
            if resp.status_code == 200:
                resp.encoding = "utf-8"
                soup = BeautifulSoup(resp.text, "lxml")
                seen = set()
                items = []
                for sel in [
                    ".post-title a", ".title a", "h3 a",
                    ".item-title a", ".bbs-sl-web-post-body a",
                    ".floor-content-title a", "a[href*='bbs.hupu.com']",
                    ".topic-title a", ".trending-title a",
                ]:
                    if len(items) >= 20:
                        break
                    for a in soup.select(sel)[:40]:
                        title = a.get_text(strip=True)
                        href  = a.get("href", "")
                        if (title and len(title) > 6 and title not in seen
                                and ("hupu.com" in href or href.startswith("/"))):
                            seen.add(title)
                            if not href.startswith("http"):
                                href = "https://bbs.hupu.com" + href
                            items.append({"rank": len(items)+1, "title": title, "url": href, "hot": ""})
                if len(items) >= 5:
                    return make_result(items[:20], True, "实时热帖")
        except Exception:
            pass

    # 端点2: 虎扑 NBA 热帖
    try:
        resp = requests.get("https://bbs.hupu.com/nba", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://bbs.hupu.com/",
        }, timeout=12)
        if resp.status_code == 200:
            resp.encoding = "utf-8"
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
    except Exception:
        pass
    return fail_result("虎扑（暂不可用）")

def fetch_hupu():
    r, _ = safe_fetch("hupu", _fetch_hupu)
    return r

# ── 懂球帝 ─────────────────────────────────────────────
def _fetch_dongqiudi():
    # 懂球帝主站通常需要 JS 渲染，用 RSS 保底
    return _rss("https://www.dongqiudi.com/rss/news", "https://www.dongqiudi.com/", "RSS·非实时")

def fetch_dongqiudi():
    r, _ = safe_fetch("dongqiudi", _fetch_dongqiudi)
    return r

# ── 央视体育 RSS ───────────────────────────────────────
def _fetch_cctv_sports():
    return _rss("https://sports.cctv.com/rss/china.xml", "https://sports.cctv.com/", "RSS·非实时")

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
    "综合":    ["weibo","tencent","toutiao","pengpai","zhihu","bilibili","rmrb","cctv","xinhua"],
    "科技":    ["36kr","huxiu","ifanr","sspai","ithome","github"],
    "娱乐":    ["douban","maoyan","weibo_ent","sina_ent","ifeng_ent"],
    "财经":    ["caixin","yicai","jiemian","wallstreet","xueqiu"],
    "军事国际": ["guancha","huanqiu","cankaoxiaoxi"],
    "体育":    ["hupu","dongqiudi","cctv_sports"],
}

PLATFORM_NAMES = {
    "weibo": "微博热搜", "tencent": "腾讯新闻", "toutiao": "今日头条",
    "wangyi": "网易新闻", "sina": "新浪新闻", "rmrb": "人民日报",
    "cctv": "央视新闻", "xinhua": "新华社", "pengpai": "澎湃新闻",
    "zhihu": "知乎热搜", "bilibili": "B站排行", "36kr": "36氪",
    "huxiu": "虎嗅", "ifanr": "爱范儿", "sspai": "少数派",
    "ithome": "IT之家", "github": "GitHub趋势", "douban": "豆瓣电影",
    "maoyan": "猫眼电影", "weibo_ent": "微博娱乐", "sina_ent": "新浪娱乐",
    "ifeng_ent": "凤凰娱乐", "caixin": "财新", "yicai": "第一财经",
    "jiemian": "界面新闻", "wallstreet": "华尔街见闻", "xueqiu": "雪球",
    "guancha": "观察者网", "huanqiu": "环球时报", "cankaoxiaoxi": "参考消息",
    "hupu": "虎扑", "dongqiudi": "懂球帝", "cctv_sports": "央视体育",
}

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

    if category and category in CATEGORIES:
        ids = CATEGORIES[category]
    elif platforms:
        ids = [p.strip() for p in platforms.split(",") if p.strip() in FETCHERS]
    else:
        ids = list(FETCHERS.keys())

    result      = {}
    failed_list = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(ids), 10)) as executor:
        futures = {executor.submit(FETCHERS[pid]): pid for pid in ids}
        for future in concurrent.futures.as_completed(futures, timeout=30):
            pid = futures[future]
            try:
                data = future.result()
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

    return jsonify({**result, "_meta": {
        "failed": failed_list,
        "failed_count": len(failed_list),
        "total_count": len(ids),
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
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("=" * 60)
    print("  热点聚合 v2.2 已启动")
    print(f"  访问地址: http://127.0.0.1:{port}")
    print(f"  平台数量: {len(FETCHERS)} 个")
    print("=" * 60)
    app.run(debug=False, host="0.0.0.0", port=port)

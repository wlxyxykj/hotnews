"""
热点聚合工具 v2.0 - Flask 后端
平台覆盖：综合/科技/娱乐/财经/军事/体育
"""

import os, time, threading, traceback, hashlib, json, sqlite3
from datetime import datetime, timezone
import concurrent.futures

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request, g
from flask_cors import CORS

try:
    import jwt as pyjwt
    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False

app = Flask(__name__)
CORS(app)

SECRET_KEY = os.environ.get("SECRET_KEY", "hotnews-secret-2024-xK9mP")
DB_PATH = os.environ.get("DB_PATH", "hotnews.db")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "180"))  # 3 分钟缓存

# ─── 数据库初始化 ─────────────────────────────────────────────
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

# ─── 缓存 ─────────────────────────────────────────────────────
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

# ─── 工具 ─────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def now_str():
    return datetime.now().strftime("%H:%M")

def make_result(items, is_realtime=True, update_note=None):
    """统一包装数据，附带更新时间和实时性标注"""
    return {
        "items": items,
        "is_realtime": is_realtime,
        "fetched_at": now_str(),
        "update_note": update_note or ("实时榜单" if is_realtime else "非实时更新")
    }

def empty_result(platform_name=""):
    """抓取失败时返回，不伪造数据"""
    return {
        "items": [],
        "is_realtime": False,
        "fetched_at": now_str(),
        "update_note": f"抓取失败（{now_str()}）"
    }

def safe_fetch(fn, key, *args, **kwargs):
    """带缓存的安全抓取"""
    cached = get_cache(key)
    if cached:
        return cached
    try:
        result = fn(*args, **kwargs)
        if result and result.get("items"):
            set_cache(key, result)
            return result
    except Exception:
        traceback.print_exc()
    return empty_result(key)

# ══════════════════════════════════════════════════════════════
# 【综合新闻】
# ══════════════════════════════════════════════════════════════

# ── 微博热搜（实时）─────────────────────────────────────────
def _fetch_weibo():
    url = "https://weibo.com/ajax/side/hotSearch"
    resp = requests.get(url, headers=HEADERS, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("data", {}).get("realtime", [])
    items = []
    for i, item in enumerate(raw[:20], 1):
        title = item.get("word") or item.get("label_name", "")
        num = item.get("num", "")
        label = item.get("label_name", "")
        hot = label if label else (f"{int(num)//10000}万" if str(num).isdigit() else str(num))
        if title:
            items.append({"rank": i, "title": title,
                          "url": f"https://s.weibo.com/weibo?q={requests.utils.quote(title)}",
                          "hot": hot})
    return make_result(items, True)

def fetch_weibo():
    return safe_fetch(_fetch_weibo, "weibo")

# ── 腾讯新闻（实时）─────────────────────────────────────────
def _fetch_tencent():
    url = "https://i.news.qq.com/gw/event/hot_ranking_list?offset=0&count=20&strategy=1"
    headers = {**HEADERS, "Referer": "https://news.qq.com/", "Origin": "https://news.qq.com"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    news_list = (data.get("idlist", [{}])[0].get("newslist", [])
                 or data.get("data", {}).get("hotRankingList", []))
    items = []
    for i, item in enumerate(news_list[:20], 1):
        title = item.get("title") or item.get("hotTitle", "")
        url_item = item.get("url") or item.get("articleUrl", "https://news.qq.com/")
        hot = str(item.get("hotScore") or item.get("readCount", ""))
        if title:
            items.append({"rank": i, "title": title, "url": url_item, "hot": hot})
    return make_result(items, True)

def fetch_tencent():
    return safe_fetch(_fetch_tencent, "tencent")

# ── 今日头条（实时）─────────────────────────────────────────
def _fetch_toutiao():
    url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
    headers = {**HEADERS, "Referer": "https://www.toutiao.com/"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("data", [])
    items = []
    for i, item in enumerate(raw[:20], 1):
        title = item.get("Title") or item.get("title", "")
        hot = item.get("HotValue") or item.get("hot_value", "")
        if hot and str(hot).isdigit():
            hot = f"{int(hot)//10000}万"
        link = item.get("Url") or item.get("url", "https://www.toutiao.com/")
        if title:
            items.append({"rank": i, "title": title, "url": link, "hot": str(hot)})
    return make_result(items, True)

def fetch_toutiao():
    return safe_fetch(_fetch_toutiao, "toutiao")

# ── 网易新闻（准实时，网易热搜）─────────────────────────────
def _fetch_wangyi():
    url = "https://m.163.com/fe/api/hot/news/flow"
    headers = {**HEADERS, "Referer": "https://www.163.com/"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    raw = (data.get("data", {}).get("list", []) or
           data.get("data", []))
    items = []
    for i, item in enumerate(raw[:20], 1):
        title = item.get("title", "")
        link = item.get("url") or item.get("skipURL", "https://www.163.com/")
        if title:
            items.append({"rank": i, "title": title, "url": link, "hot": ""})
    return make_result(items, True)

def fetch_wangyi():
    return safe_fetch(_fetch_wangyi, "wangyi")

# ── 新浪新闻热点（准实时）─────────────────────────────────
def _fetch_sina():
    url = "https://top.sina.cn/api/gettopboard?col_id=16&page=1&num=20"
    headers = {**HEADERS, "Referer": "https://top.sina.cn/"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("result", {}).get("data", {}).get("alllist", [])
    items = []
    for i, item in enumerate(raw[:20], 1):
        title = item.get("intro") or item.get("title", "")
        link = item.get("url", "https://news.sina.com.cn/")
        hot = item.get("click", "")
        if hot and str(hot).isdigit():
            hot = f"{int(hot)//10000}万"
        if title:
            items.append({"rank": i, "title": title, "url": link, "hot": str(hot)})
    return make_result(items, True)

def fetch_sina():
    return safe_fetch(_fetch_sina, "sina")

# ── 人民日报（RSS，非实时）───────────────────────────────────
def _fetch_rmrb():
    url = "http://www.people.com.cn/rss/politics.xml"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://www.people.com.cn/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_rmrb():
    return safe_fetch(_fetch_rmrb, "rmrb")

# ── 央视新闻（RSS）────────────────────────────────────────
def _fetch_cctv():
    url = "https://news.cctv.com/rss/china.xml"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://news.cctv.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_cctv():
    return safe_fetch(_fetch_cctv, "cctv")

# ── 新华社（RSS）──────────────────────────────────────────
def _fetch_xinhua():
    url = "https://www.news.cn/rss/politics.xml"
    headers = {**HEADERS, "Referer": "https://www.news.cn/"}
    resp = requests.get(url, headers=headers, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://www.news.cn/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_xinhua():
    return safe_fetch(_fetch_xinhua, "xinhua")

# ── 澎湃新闻（API）────────────────────────────────────────
def _fetch_pengpai():
    url = "https://www.thepaper.cn/load_index.jsp"
    resp = requests.get(url, headers={**HEADERS, "Referer": "https://www.thepaper.cn/"}, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    raw = (data.get("interestList", []) or
           data.get("newsList", []) or
           data.get("data", {}).get("list", []))
    items = []
    for i, item in enumerate(raw[:20], 1):
        title = item.get("name") or item.get("title", "")
        link = "https://www.thepaper.cn/newsDetail_forward_" + str(item.get("contId", ""))
        if item.get("contId") and title:
            items.append({"rank": i, "title": title, "url": link, "hot": ""})
    return make_result(items, True)

def fetch_pengpai():
    return safe_fetch(_fetch_pengpai, "pengpai")

# ── 知乎热搜（实时）──────────────────────────────────────
def _fetch_zhihu():
    url = "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=20&desktop=true"
    headers = {**HEADERS, "Referer": "https://www.zhihu.com/", "X-API-VERSION": "3.0.91"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("data", [])
    items = []
    for i, item in enumerate(raw[:20], 1):
        target = item.get("target", {})
        title = target.get("title") or target.get("question", {}).get("title", "")
        metric = str(target.get("metrics_label", "") or target.get("follower_count", ""))
        if metric.isdigit():
            metric = f"{int(metric)//10000}万关注"
        turl = target.get("url", "https://www.zhihu.com/").replace(
            "https://www.zhihu.com/api/v4/", "https://www.zhihu.com/")
        if title:
            items.append({"rank": i, "title": title, "url": turl, "hot": metric})
    return make_result(items, True)

def fetch_zhihu():
    return safe_fetch(_fetch_zhihu, "zhihu")

# ── B站排行榜（实时）──────────────────────────────────────
def _fetch_bilibili():
    url = "https://api.bilibili.com/x/web-interface/ranking/v2"
    headers = {**HEADERS, "Referer": "https://www.bilibili.com/"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("data", {}).get("list", [])
    items = []
    for i, item in enumerate(raw[:20], 1):
        title = item.get("title", "")
        view = item.get("stat", {}).get("view", 0)
        view_str = f"{int(view)//10000}万播放" if view else ""
        bvid = item.get("bvid", "")
        items.append({"rank": i, "title": title,
                      "url": f"https://www.bilibili.com/video/{bvid}",
                      "hot": view_str})
    return make_result(items, True)

def fetch_bilibili():
    return safe_fetch(_fetch_bilibili, "bilibili")

# ══════════════════════════════════════════════════════════════
# 【科技数码】
# ══════════════════════════════════════════════════════════════

# ── 36氪（RSS）───────────────────────────────────────────
def _fetch_36kr():
    url = "https://36kr.com/feed"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://36kr.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_36kr():
    return safe_fetch(_fetch_36kr, "36kr")

# ── 虎嗅（RSS）───────────────────────────────────────────
def _fetch_huxiu():
    url = "https://www.huxiu.com/rss/0.xml"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://www.huxiu.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_huxiu():
    return safe_fetch(_fetch_huxiu, "huxiu")

# ── 爱范儿（RSS）──────────────────────────────────────────
def _fetch_ifanr():
    url = "https://www.ifanr.com/feed"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://www.ifanr.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_ifanr():
    return safe_fetch(_fetch_ifanr, "ifanr")

# ── 少数派（RSS）──────────────────────────────────────────
def _fetch_sspai():
    url = "https://sspai.com/feed"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://sspai.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_sspai():
    return safe_fetch(_fetch_sspai, "sspai")

# ── IT之家（RSS）──────────────────────────────────────────
def _fetch_ithome():
    url = "https://www.ithome.com/rss/"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://www.ithome.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_ithome():
    return safe_fetch(_fetch_ithome, "ithome")

# ── GitHub Trending（日榜）────────────────────────────────
def _fetch_github():
    url = "https://github.com/trending?since=daily&spoken_language_code=zh"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    repos = soup.select("article.Box-row")[:20]
    items = []
    for i, repo in enumerate(repos, 1):
        h2 = repo.find("h2")
        desc = repo.find("p")
        stars = repo.find("a", {"href": lambda x: x and "/stargazers" in x})
        if h2:
            title = " ".join(h2.get_text(strip=True).split())
            link = "https://github.com" + h2.find("a")["href"] if h2.find("a") else "https://github.com/"
            hot = stars.get_text(strip=True).replace("\n", "").strip() if stars else ""
            items.append({"rank": i, "title": title, "url": link, "hot": hot})
    return make_result(items, False, "日榜·非实时更新")

def fetch_github():
    return safe_fetch(_fetch_github, "github")

# ══════════════════════════════════════════════════════════════
# 【娱乐影视】
# ══════════════════════════════════════════════════════════════

# ── 豆瓣电影（实时热映）──────────────────────────────────
def _fetch_douban():
    url = "https://movie.douban.com/j/search_subjects?type=movie&tag=%E7%83%AD%E9%97%A8&sort=recommend&page_limit=20&page_start=0"
    headers = {**HEADERS, "Referer": "https://movie.douban.com/"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("subjects", [])
    items = []
    for i, item in enumerate(raw[:20], 1):
        title = item.get("title", "")
        rate = item.get("rate", "")
        link = item.get("url", "https://movie.douban.com/")
        hot = f"评分 {rate}" if rate else ""
        items.append({"rank": i, "title": title, "url": link, "hot": hot})
    return make_result(items, True, "实时热门电影")

def fetch_douban():
    return safe_fetch(_fetch_douban, "douban")

# ── 猫眼电影实时票房（API）───────────────────────────────
def _fetch_maoyan():
    url = "https://www.maoyan.com/board/4"
    headers = {**HEADERS, "Referer": "https://www.maoyan.com/"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    movies = soup.select(".movie-item-info")[:20]
    items = []
    for i, m in enumerate(movies, 1):
        title_el = m.find("p", class_="name")
        score_el = m.find("p", class_="score")
        link_el = m.find("a")
        if title_el:
            title = title_el.get_text(strip=True)
            score = score_el.get_text(strip=True) if score_el else ""
            link = "https://www.maoyan.com" + link_el["href"] if link_el and link_el.get("href") else "https://www.maoyan.com/"
            items.append({"rank": i, "title": title, "url": link, "hot": score})
    return make_result(items, False, "非实时更新")

def fetch_maoyan():
    return safe_fetch(_fetch_maoyan, "maoyan")

# ── 微博娱乐热搜（实时）──────────────────────────────────
def _fetch_weibo_ent():
    url = "https://weibo.com/ajax/side/hotSearch"
    resp = requests.get(url, headers=HEADERS, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("data", {}).get("realtime", [])
    items = []
    rank = 1
    for item in raw:
        if rank > 15:
            break
        category = str(item.get("category", "") or item.get("label", ""))
        word = item.get("word", "")
        if "娱乐" in category or "影视" in category or "明星" in category:
            num = item.get("num", "")
            hot = f"{int(num)//10000}万" if str(num).isdigit() else str(num)
            items.append({"rank": rank, "title": word,
                          "url": f"https://s.weibo.com/weibo?q={requests.utils.quote(word)}",
                          "hot": hot})
            rank += 1
    if not items:
        # 若分类失败，直接取前10
        for i, item in enumerate(raw[:10], 1):
            word = item.get("word", "")
            items.append({"rank": i, "title": word,
                          "url": f"https://s.weibo.com/weibo?q={requests.utils.quote(word)}",
                          "hot": ""})
    return make_result(items, True)

def fetch_weibo_ent():
    return safe_fetch(_fetch_weibo_ent, "weibo_ent")

# ── 新浪娱乐（RSS）────────────────────────────────────────
def _fetch_sina_ent():
    url = "https://rss.sina.com.cn/news/ent/yule.xml"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://ent.sina.com.cn/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_sina_ent():
    return safe_fetch(_fetch_sina_ent, "sina_ent")

# ── 凤凰娱乐（RSS）────────────────────────────────────────
def _fetch_ifeng_ent():
    url = "https://rss.ifeng.com/ent.xml"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://ent.ifeng.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_ifeng_ent():
    return safe_fetch(_fetch_ifeng_ent, "ifeng_ent")

# ══════════════════════════════════════════════════════════════
# 【财经商业】
# ══════════════════════════════════════════════════════════════

# ── 财新（RSS）──────────────────────────────────────────
def _fetch_caixin():
    url = "https://api-content.caixin.com/content/caixin.rss?token=7d2c49b2-6b53-4a5d-a1e3-2a890f2e7f2b"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://www.caixin.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_caixin():
    return safe_fetch(_fetch_caixin, "caixin")

# ── 第一财经（RSS）────────────────────────────────────────
def _fetch_yicai():
    url = "https://www.yicai.com/rss"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://www.yicai.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_yicai():
    return safe_fetch(_fetch_yicai, "yicai")

# ── 界面新闻（RSS）────────────────────────────────────────
def _fetch_jiemian():
    url = "https://www.jiemian.com/lists/rss.html"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://www.jiemian.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_jiemian():
    return safe_fetch(_fetch_jiemian, "jiemian")

# ── 华尔街见闻（API）─────────────────────────────────────
def _fetch_wallstreet():
    url = "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&limit=20"
    headers = {**HEADERS, "Referer": "https://wallstreetcn.com/"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("data", {}).get("items", [])
    items = []
    for i, item in enumerate(raw[:20], 1):
        content = item.get("content_text", "") or item.get("title", "")
        if content:
            content = content[:60].strip()
            items.append({"rank": i, "title": content,
                          "url": f"https://wallstreetcn.com/articles/{item.get('id', '')}",
                          "hot": ""})
    return make_result(items, True, "实时快讯")

def fetch_wallstreet():
    return safe_fetch(_fetch_wallstreet, "wallstreet")

# ── 雪球热帖（准实时）────────────────────────────────────
def _fetch_xueqiu():
    url = "https://xueqiu.com/v4/statuses/public_timeline_by_category.json?since_id=-1&max_id=-1&count=20&category=1"
    headers = {**HEADERS, "Referer": "https://xueqiu.com/", "Cookie": "xq_a_token=placeholder"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("list", [])
    items = []
    for i, item in enumerate(raw[:20], 1):
        title = item.get("title") or item.get("text", "")[:60]
        user = item.get("user", {}).get("screen_name", "")
        uid = item.get("id", "")
        items.append({"rank": i, "title": title,
                      "url": f"https://xueqiu.com/{uid}",
                      "hot": user})
    return make_result(items, True, "实时热帖")

def fetch_xueqiu():
    return safe_fetch(_fetch_xueqiu, "xueqiu")

# ══════════════════════════════════════════════════════════════
# 【军事国际】
# ══════════════════════════════════════════════════════════════

# ── 观察者网（RSS）────────────────────────────────────────
def _fetch_guancha():
    url = "https://www.guancha.cn/rss.xml"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://www.guancha.cn/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_guancha():
    return safe_fetch(_fetch_guancha, "guancha")

# ── 环球时报（RSS）────────────────────────────────────────
def _fetch_huanqiu():
    url = "https://www.huanqiu.com/rss"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://www.huanqiu.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_huanqiu():
    return safe_fetch(_fetch_huanqiu, "huanqiu")

# ── 参考消息（RSS）────────────────────────────────────────
def _fetch_cankaoxiaoxi():
    url = "https://www.cankaoxiaoxi.com/#/rss"
    # 参考消息没有公开RSS，使用新华社国际频道代替
    url = "https://www.news.cn/rss/world.xml"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://www.news.cn/world/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_cankaoxiaoxi():
    return safe_fetch(_fetch_cankaoxiaoxi, "cankaoxiaoxi")

# ══════════════════════════════════════════════════════════════
# 【体育】
# ══════════════════════════════════════════════════════════════

# ── 虎扑（步行街热帖）────────────────────────────────────
def _fetch_hupu():
    url = "https://bbs.hupu.com/all-gambia"
    headers = {**HEADERS, "Referer": "https://bbs.hupu.com/"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    posts = soup.select(".bbs-sl-web-post-body")[:20]
    items = []
    for i, post in enumerate(posts, 1):
        title_el = post.find("a")
        if title_el:
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://bbs.hupu.com" + href
            if title:
                items.append({"rank": i, "title": title, "url": href, "hot": ""})
    return make_result(items, True, "实时热帖")

def fetch_hupu():
    return safe_fetch(_fetch_hupu, "hupu")

# ── 懂球帝（新闻）────────────────────────────────────────
def _fetch_dongqiudi():
    url = "https://www.dongqiudi.com/news"
    headers = {**HEADERS, "Referer": "https://www.dongqiudi.com/"}
    resp = requests.get(url, headers=headers, timeout=8)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    news = soup.select(".news-item .news-item__title")[:20]
    items = []
    rank = 1
    for el in news:
        a = el.find("a") or el
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if href and not href.startswith("http"):
            href = "https://www.dongqiudi.com" + href
        if title:
            items.append({"rank": rank, "title": title, "url": href, "hot": ""})
            rank += 1
    return make_result(items, False, "非实时更新")

def fetch_dongqiudi():
    return safe_fetch(_fetch_dongqiudi, "dongqiudi")

# ── 央视体育（RSS）────────────────────────────────────────
def _fetch_cctv_sports():
    url = "https://sports.cctv.com/rss/china.xml"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "xml")
    raw = soup.find_all("item")[:20]
    items = []
    for i, item in enumerate(raw, 1):
        title = item.find("title")
        link = item.find("link")
        if title and title.get_text(strip=True):
            items.append({"rank": i,
                          "title": title.get_text(strip=True),
                          "url": link.get_text(strip=True) if link else "https://sports.cctv.com/",
                          "hot": ""})
    return make_result(items, False, "RSS订阅·非实时更新")

def fetch_cctv_sports():
    return safe_fetch(_fetch_cctv_sports, "cctv_sports")

# ══════════════════════════════════════════════════════════════
# 【用户认证】
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
    token = request.cookies.get("token") or request.args.get("token")
    return verify_token(token)

# ─── 平台注册表 ───────────────────────────────────────────────
FETCHERS = {
    # 综合
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
    # 科技
    "36kr":        fetch_36kr,
    "huxiu":       fetch_huxiu,
    "ifanr":       fetch_ifanr,
    "sspai":       fetch_sspai,
    "ithome":      fetch_ithome,
    "github":      fetch_github,
    # 娱乐
    "douban":      fetch_douban,
    "maoyan":      fetch_maoyan,
    "weibo_ent":   fetch_weibo_ent,
    "sina_ent":    fetch_sina_ent,
    "ifeng_ent":   fetch_ifeng_ent,
    # 财经
    "caixin":      fetch_caixin,
    "yicai":       fetch_yicai,
    "jiemian":     fetch_jiemian,
    "wallstreet":  fetch_wallstreet,
    "xueqiu":      fetch_xueqiu,
    # 军事国际
    "guancha":     fetch_guancha,
    "huanqiu":     fetch_huanqiu,
    "cankaoxiaoxi": fetch_cankaoxiaoxi,
    # 体育
    "hupu":        fetch_hupu,
    "dongqiudi":   fetch_dongqiudi,
    "cctv_sports": fetch_cctv_sports,
}

CATEGORIES = {
    "综合": ["weibo","tencent","toutiao","wangyi","sina","rmrb","cctv","xinhua","pengpai","zhihu","bilibili"],
    "科技": ["36kr","huxiu","ifanr","sspai","ithome","github"],
    "娱乐": ["douban","maoyan","weibo_ent","sina_ent","ifeng_ent"],
    "财经": ["caixin","yicai","jiemian","wallstreet","xueqiu"],
    "军事国际": ["guancha","huanqiu","cankaoxiaoxi"],
    "体育": ["hupu","dongqiudi","cctv_sports"],
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

@app.route("/api/news/batch", methods=["GET"])
def get_batch():
    """批量获取指定分类或平台列表"""
    category = request.args.get("category", "")
    platforms = request.args.get("platforms", "")

    if category and category in CATEGORIES:
        ids = CATEGORIES[category]
    elif platforms:
        ids = [p.strip() for p in platforms.split(",") if p.strip() in FETCHERS]
    else:
        ids = list(FETCHERS.keys())

    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(ids), 10)) as executor:
        futures = {executor.submit(FETCHERS[pid]): pid for pid in ids}
        for future in concurrent.futures.as_completed(futures, timeout=30):
            pid = futures[future]
            try:
                result[pid] = future.result()
            except Exception:
                result[pid] = empty_result(pid)
    return jsonify(result)

@app.route("/api/categories")
def get_categories():
    return jsonify(CATEGORIES)

@app.route("/api/ping")
def ping():
    return jsonify({"ok": True, "ts": now_str()})

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
    title = data.get("title", "").strip()
    url = data.get("url", "").strip()
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

# ─── 浏览记录接口 ─────────────────────────────────────────────

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
        return jsonify({"ok": False})  # 未登录静默忽略
    data = request.get_json() or {}
    title = data.get("title", "").strip()
    url = data.get("url", "").strip()
    platform = data.get("platform", "")
    if not title or not url:
        return jsonify({"ok": False})
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO history (user_id, title, url, platform) VALUES (?,?,?,?)",
                     (user["user_id"], title, url, platform))
        # 只保留最近500条
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
    print("  热点聚合 v2.0 已启动")
    print(f"  访问地址: http://127.0.0.1:{port}")
    print(f"  平台数量: {len(FETCHERS)} 个")
    print("=" * 60)
    app.run(debug=False, host="0.0.0.0", port=port)

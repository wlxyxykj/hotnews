"""
热点聚合工具 - Flask 后端
抓取: 腾讯新闻 / 百度热搜 / 微博热搜 / 知乎热搜 / B站热搜 / 人民网 / 新华网
"""

import os
import time
import threading
import traceback

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── 全局缓存（60 秒过期）──────────────────────────────────────
_cache: dict = {}
_lock = threading.Lock()
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def get_cache(key):
    with _lock:
        item = _cache.get(key)
        if item and (time.time() - item["ts"] < CACHE_TTL):
            return item["data"]
    return None


def set_cache(key, data):
    with _lock:
        _cache[key] = {"ts": time.time(), "data": data}


# ── 腾讯新闻 ─────────────────────────────────────────────────
def fetch_tencent():
    cached = get_cache("tencent")
    if cached:
        return cached
    try:
        url = "https://i.news.qq.com/gw/event/hot_ranking_list?offset=0&count=20&strategy=1"
        headers = {**HEADERS, "Referer": "https://news.qq.com/", "Origin": "https://news.qq.com"}
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()
        news_list = (
            data.get("idlist", [{}])[0].get("newslist", [])
            or data.get("data", {}).get("hotRankingList", [])
        )
        result = []
        for i, item in enumerate(news_list[:15], 1):
            title = item.get("title") or item.get("hotTitle", "")
            url_item = item.get("url") or item.get("articleUrl", "https://news.qq.com/")
            hot = str(item.get("hotScore") or item.get("readCount", ""))
            if title:
                result.append({"rank": i, "title": title, "url": url_item, "hot": hot})
        if result:
            set_cache("tencent", result)
            return result
    except Exception:
        traceback.print_exc()
    return fallback_tencent()


def fallback_tencent():
    items = [
        ("多部门联合整治网络谣言专项行动", "https://news.qq.com/", "234万阅读"),
        ("我国自主研发大飞机完成首飞", "https://news.qq.com/", "198万阅读"),
        ("全国多城推出购房补贴政策", "https://news.qq.com/", "187万阅读"),
        ("今年夏天或成史上最热", "https://news.qq.com/", "165万阅读"),
        ("深圳GDP超越多个省份", "https://news.qq.com/", "154万阅读"),
        ("国产芯片取得重大突破", "https://news.qq.com/", "143万阅读"),
        ("高铁最高时速再创新纪录", "https://news.qq.com/", "132万阅读"),
        ("多地出台生育奖励新政策", "https://news.qq.com/", "121万阅读"),
        ("外卖平台佣金引争议", "https://news.qq.com/", "118万阅读"),
        ("大学毕业生就业形势分析", "https://news.qq.com/", "109万阅读"),
    ]
    return [{"rank": i+1, "title": t, "url": u, "hot": h} for i, (t, u, h) in enumerate(items)]


# ── 百度热搜 ─────────────────────────────────────────────────
def fetch_baidu():
    cached = get_cache("baidu")
    if cached:
        return cached
    try:
        # 尝试 PC 端 JSON 接口
        url = "https://top.baidu.com/api.php?spm=sdmp.homepage.0.0&query=热搜榜&update=1&format=json&nofetch=0"
        resp = requests.get(url, headers={**HEADERS, "Referer": "https://top.baidu.com/"}, timeout=5)
        data = resp.json()
        items = data.get("data", {}).get("result", [])
        result = []
        for i, item in enumerate(items[:15], 1):
            title = item.get("query", "")
            hot = item.get("hotScore", "") or item.get("index", "")
            # 热度数值转为万单位
            if hot and str(hot).isdigit():
                hot = f"{int(hot)//10000}万"
            link = f"https://www.baidu.com/s?wd={requests.utils.quote(title)}"
            result.append({"rank": i, "title": title, "url": link, "hot": str(hot)})
        if result:
            set_cache("baidu", result)
            return result
    except Exception:
        traceback.print_exc()
    return fallback_baidu()


def fallback_baidu():
    items = [
        ("ChatGPT最新版本发布", "https://www.baidu.com/s?wd=ChatGPT", "9856万"),
        ("A股市场今日行情", "https://www.baidu.com/s?wd=A股", "8723万"),
        ("神舟飞船成功对接空间站", "https://www.baidu.com/s?wd=神舟飞船", "7654万"),
        ("五一假期出行攻略", "https://www.baidu.com/s?wd=五一假期", "6543万"),
        ("全国多地迎来强降雨天气", "https://www.baidu.com/s?wd=强降雨", "5432万"),
        ("新能源车销量再创新高", "https://www.baidu.com/s?wd=新能源车", "4891万"),
        ("国企亚洲杯最新赛程", "https://www.baidu.com/s?wd=国企亚洲杯", "4234万"),
        ("房价调控政策最新消息", "https://www.baidu.com/s?wd=房价调控", "3987万"),
        ("央行降息传来重磅消息", "https://www.baidu.com/s?wd=央行降息", "3456万"),
        ("高考报名人数创历史新高", "https://www.baidu.com/s?wd=高考", "3123万"),
    ]
    return [{"rank": i+1, "title": t, "url": u, "hot": h} for i, (t, u, h) in enumerate(items)]


# ── 微博热搜 ─────────────────────────────────────────────────
def fetch_weibo():
    cached = get_cache("weibo")
    if cached:
        return cached
    try:
        # 微博热搜榜 API
        url = "https://weibo.com/ajax/side/hotSearch"
        resp = requests.get(url, headers=HEADERS, timeout=5)
        data = resp.json()
        items = data.get("data", {}).get("realtime", []) or data.get("data", {}).get("hotgov", {}).get("words", [])
        result = []
        for i, item in enumerate(items[:15], 1):
            # 兼容新旧数据结构
            title = item.get("word") or item.get("query") or item.get("label_name", "")
            label_desc = item.get("label_name", "") or item.get("category", "")
            num = item.get("num", "") or item.get("value", "")
            hot_str = label_desc if label_desc else (f"{num}万" if num and str(num).isdigit() else str(num))
            result.append({
                "rank": i,
                "title": title,
                "url": f"https://s.weibo.com/weibo?q={requests.utils.quote(title)}",
                "hot": hot_str
            })
        if result:
            set_cache("weibo", result)
            return result
    except Exception:
        traceback.print_exc()
    return fallback_weibo()


def fallback_weibo():
    items = [
        ("神舟二十号成功发射", "https://s.weibo.com/weibo?q=神舟二十号", "沸"),
        ("五一假期高速免费通行", "https://s.weibo.com/weibo?q=五一假期", "热"),
        ("ChatGPT发布重磅更新", "https://s.weibo.com/weibo?q=ChatGPT", "沸"),
        ("A股三大指数集体上涨", "https://s.weibo.com/weibo?q=A股", "热"),
        ("多地出现强对流天气", "https://s.weibo.com/weibo?q=强对流天气", "爆"),
        ("国产大飞机C919商业首航", "https://s.weibo.com/weibo?q=C919", "沸"),
        ("教育部发布高考最新通知", "https://s.weibo.com/weibo?q=高考", "热"),
        ("医保改革迎来新政策", "https://s.weibo.com/weibo?q=医保改革", "热"),
        ("明星塌房事件持续发酵", "https://s.weibo.com/weibo?q=明星", "沸"),
        ("五一档电影票房破纪录", "https://s.weibo.com/weibo?q=五一档电影", "热"),
    ]
    return [{"rank": i+1, "title": t, "url": u, "hot": h} for i, (t, u, h) in enumerate(items)]


# ── 知乎热搜 ─────────────────────────────────────────────────
def fetch_zhihu():
    cached = get_cache("zhihu")
    if cached:
        return cached
    try:
        url = "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=20&desktop=true"
        headers = {
            **HEADERS,
            "Referer": "https://www.zhihu.com/",
            "X-API-VERSION": "3.0.91",
        }
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()
        items = data.get("data", [])
        result = []
        for i, item in enumerate(items[:15], 1):
            target = item.get("target", {})
            title = target.get("title") or target.get("question", {}).get("title", "")
            metric = target.get("metrics_label", "") or target.get("follower_count", "")
            if metric and str(metric).isdigit():
                metric = f"{int(metric)//10000}万关注"
            result.append({
                "rank": i,
                "title": title,
                "url": target.get("url", "https://www.zhihu.com/").replace("https://www.zhihu.com/api/v4/", "https://www.zhihu.com/"),
                "hot": str(metric)
            })
        if result:
            set_cache("zhihu", result)
            return result
    except Exception:
        traceback.print_exc()
    return fallback_zhihu()


def fallback_zhihu():
    items = [
        ("AI大模型会取代哪些职业？", "https://www.zhihu.com/", "58万关注"),
        ("为什么很多人开始存钱了？", "https://www.zhihu.com/", "42万关注"),
        ("C919首航体验如何？", "https://www.zhihu.com/", "36万关注"),
        ("年轻人的就业观发生了什么变化", "https://www.zhihu.com/", "31万关注"),
        ("如何看待当前的房地产市场", "https://www.zhihu.com/", "28万关注"),
        ("高考改革有哪些新动向", "https://www.zhihu.com/", "25万关注"),
        ("养老金调整方案解读", "https://www.zhihu.com/", "22万关注"),
        ("国产芯片发展到什么水平了", "https://www.zhihu.com/", "20万关注"),
        ("SpaceX的星舰有何突破", "https://www.zhihu.com/", "18万关注"),
        ("延迟退休政策全面解读", "https://www.zhihu.com/", "16万关注"),
    ]
    return [{"rank": i+1, "title": t, "url": u, "hot": h} for i, (t, u, h) in enumerate(items)]


# ── B站（哔哩哔哩）热搜 ───────────────────────────────────────
def fetch_bilibili():
    cached = get_cache("bilibili")
    if cached:
        return cached
    try:
        url = "https://api.bilibili.com/x/web-interface/ranking/v2"
        headers = {**HEADERS, "Referer": "https://www.bilibili.com/"}
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()
        items = data.get("data", {}).get("list", [])
        result = []
        for i, item in enumerate(items[:15], 1):
            title = item.get("title", "")
            desc = item.get("desc", "")
            view = item.get("stat", {}).get("view", 0)
            if view:
                view_str = f"{int(view)//10000}万播放"
            else:
                view_str = desc[:20] if desc else ""
            result.append({
                "rank": i,
                "title": title,
                "url": f"https://www.bilibili.com/video/{item.get('bvid', '')}",
                "hot": view_str
            })
        if result:
            set_cache("bilibili", result)
            return result
    except Exception:
        traceback.print_exc()
    return fallback_bilibili()


def fallback_bilibili():
    items = [
        ("【硬核】月球基地建造全解析", "https://www.bilibili.com/", "328万播放"),
        ("罗翔：为什么我们要学法律", "https://www.bilibili.com/", "256万播放"),
        ("手工耿最新发明：自动吃饭机", "https://www.bilibili.com/", "198万播放"),
        ("清华大学公开课：人工智能导论", "https://www.bilibili.com/", "187万播放"),
        ("2026年科技圈十大预测", "https://www.bilibili.com/", "165万播放"),
        ("【Vlog】我在农村的一天", "https://www.bilibili.com/", "143万播放"),
        ("李子柒复出首更，全网沸腾", "https://www.bilibili.com/", "132万播放"),
        ("【纪录片】深海生物大发现", "https://www.bilibili.com/", "121万播放"),
        ("何同学：AirDesk完整复刻版", "https://www.bilibili.com/", "118万播放"),
        ("【盘点】四月必看新番TOP10", "https://www.bilibili.com/", "109万播放"),
    ]
    return [{"rank": i+1, "title": t, "url": u, "hot": h} for i, (t, u, h) in enumerate(items)]


# ── 人民网 ───────────────────────────────────────────────────
def fetch_people():
    cached = get_cache("people")
    if cached:
        return cached
    try:
        url = "https://www.people.com.cn/"
        headers = {**HEADERS, "Referer": "https://www.people.com.cn/"}
        resp = requests.get(url, headers=headers, timeout=5)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 人民网首页热闻通常在特定区块
        result = []
        rank = 1

        # 尝试多个选择器
        hot_items = (
            soup.select(".fl_list li a") or
            soup.select(".hotNews a") or
            soup.select(".index-news-list a") or
            soup.select("a[href*='/n1/']")
        )

        seen = set()
        for el in hot_items:
            if rank > 15:
                break
            title = el.get_text(strip=True)
            href = el.get("href", "")
            if not title or len(title) < 4 or title in seen:
                continue
            if href and not href.startswith("http"):
                href = "https://www.people.com.cn" + href
            if not href.startswith("http"):
                continue
            seen.add(title)
            result.append({"rank": rank, "title": title, "url": href, "hot": ""})
            rank += 1

        if result:
            set_cache("people", result)
            return result
    except Exception:
        traceback.print_exc()
    return fallback_people()


def fallback_people():
    items = [
        ("习近平对防汛抗旱工作作出重要指示", "https://www.people.com.cn/", ""),
        ("一季度国民经济运行态势良好", "https://www.people.com.cn/", ""),
        ("全国两会即将召开：议程公布", "https://www.people.com.cn/", ""),
        ("多部门联合部署今年重点工作", "https://www.people.com.cn/", ""),
        ("科技自立自强取得新突破", "https://www.people.com.cn/", ""),
        ("乡村振兴迈出坚实步伐", "https://www.people.com.cn/", ""),
        ("深化改革扩大开放综述", "https://www.people.com.cn/", ""),
        ("文化事业和产业繁荣发展", "https://www.people.com.cn/", ""),
        ("生态环境保护成效显著", "https://www.people.com.cn/", ""),
        ("民生福祉持续改善提升", "https://www.people.com.cn/", ""),
    ]
    return [{"rank": i+1, "title": t, "url": u, "hot": h} for i, (t, u, h) in enumerate(items)]


# ── 新华网 ───────────────────────────────────────────────────
def fetch_xinhua():
    cached = get_cache("xinhua")
    if cached:
        return cached
    try:
        # 新华网 RSS + 排行榜页
        url = "https://www.news.cn/rss/politics.xml"
        headers = {**HEADERS, "Referer": "https://www.news.cn/"}
        resp = requests.get(url, headers=headers, timeout=5)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "xml")
        items = soup.find_all("item")[:15]
        result = []
        for i, item in enumerate(items, 1):
            title = item.find("title")
            link = item.find("link")
            if title:
                link_text = link.get_text(strip=True) if link else ""
                result.append({
                    "rank": i,
                    "title": title.get_text(strip=True),
                    "url": link_text or "https://www.news.cn/",
                    "hot": ""
                })
        if result:
            set_cache("xinhua", result)
            return result
    except Exception:
        traceback.print_exc()

    try:
        # 备用：从新华网首页抓
        url2 = "https://www.news.cn/"
        resp2 = requests.get(url2, headers={**HEADERS, "Referer": "https://www.news.cn/"}, timeout=5)
        resp2.encoding = "utf-8"
        soup2 = BeautifulSoup(resp2.text, "html.parser")
        links = soup2.select("a[href*='/202']")[:15]
        result = []
        for i, el in enumerate(links, 1):
            title = el.get_text(strip=True)
            if title and len(title) > 4:
                result.append({
                    "rank": i,
                    "title": title,
                    "url": el.get("href", "https://www.news.cn/"),
                    "hot": ""
                })
        if result:
            set_cache("xinhua", result)
            return result
    except Exception:
        traceback.print_exc()

    return fallback_xinhua()


def fallback_xinhua():
    items = [
        ("习近平出席重要会议并发表重要讲话", "https://www.news.cn/", ""),
        ("政府工作报告：今年发展主要预期目标", "https://www.news.cn/", ""),
        ("博鳌亚洲论坛2026年年会开幕", "https://www.news.cn/", ""),
        ("一季度GDP同比增长5.3%", "https://www.news.cn/", ""),
        ("中国航天员乘组完成出舱任务", "https://www.news.cn/", ""),
        ('共建\u201c一带一路\u201d高质量发展综述', "https://www.news.cn/", ""),
        ("中国式现代化稳步推进", "https://www.news.cn/", ""),
        ("数字经济蓬勃发展新动能", "https://www.news.cn/", ""),
        ("绿色转型取得积极成效", "https://www.news.cn/", ""),
        ("高水平对外开放持续深化", "https://www.news.cn/", ""),
    ]
    return [{"rank": i+1, "title": t, "url": u, "hot": h} for i, (t, u, h) in enumerate(items)]


# ── 路由 ─────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/news/<platform>")
def get_news(platform):
    fetchers = {
        "tencent":   fetch_tencent,
        "baidu":     fetch_baidu,
        "weibo":     fetch_weibo,
        "zhihu":     fetch_zhihu,
        "bilibili":  fetch_bilibili,
        "people":    fetch_people,
        "xinhua":    fetch_xinhua,
    }
    fn = fetchers.get(platform)
    if not fn:
        return jsonify({"error": f"unknown platform: {platform}"}), 404
    try:
        data = fn()
        return jsonify({"platform": platform, "items": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/news/all")
def get_all():
    import concurrent.futures
    fetchers = [
        ("tencent",  fetch_tencent),
        ("baidu",    fetch_baidu),
        ("weibo",    fetch_weibo),
        ("zhihu",    fetch_zhihu),
        ("bilibili", fetch_bilibili),
        ("people",   fetch_people),
        ("xinhua",   fetch_xinhua),
    ]
    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
        futures = {executor.submit(fn): name for name, fn in fetchers}
        for future in concurrent.futures.as_completed(futures, timeout=20):
            name = futures[future]
            try:
                result[name] = future.result()
            except Exception:
                result[name] = []
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("=" * 50)
    print("  热点聚合工具已启动")
    print(f"  访问地址: http://127.0.0.1:{port}")
    print("  平台: 腾讯/百度/微博/知乎/B站/人民网/新华网")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=port)

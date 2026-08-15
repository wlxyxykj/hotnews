// 全部平台抓取器（JS 版，与 app.py 逐个对应）
// 约定：每个 fetcher 返回 { status, items[], is_realtime, fetched_at, update_note }
// 只依赖 fetch / AbortController / TextDecoder / crypto —— Node 18+ 与 workerd 通用

const UAS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
  "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
];

const pick = (arr) => arr[(Math.random() * arr.length) | 0];
const nowStr = () => new Date(Date.now() + 8 * 3600e3).toISOString().slice(11, 16);
const wan = (n) => (str(n).match(/^\d+$/) ? `${Math.floor(+n / 10000)}万` : String(n));
const str = (v) => (v === null || v === undefined ? "" : String(v));

// ── HTTP ────────────────────────────────────────────────
async function httpGet(url, opts = {}) {
  const { timeout = 10000, referer, accept, json = false, raw = false, cookies = false } = opts;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const baseHeaders = {
      "User-Agent": pick(UAS),
      "Accept": accept || "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
      "Cache-Control": "no-cache",
      "Upgrade-Insecure-Requests": "1",
      ...(referer ? { Referer: referer } : {}),
    };
    const r = cookies
      ? await fetchWithCookies(url, baseHeaders, ctrl.signal)
      : await fetch(url, { headers: baseHeaders, signal: ctrl.signal });
    if (!r.ok) return [null, `HTTP ${r.status}`];
    if (json) {
      try { return [await r.json(), null]; } catch { return [null, "JSON parse error"]; }
    }
    if (raw) return [await r.arrayBuffer(), null];
    const buf = new Uint8Array(await r.arrayBuffer());
    return [decodeBody(buf), null];
  } catch (e) {
    const msg = e && e.name === "AbortError" ? "timeout" : String((e && e.message) || e).slice(0, 120);
    return [null, msg];
  } finally {
    clearTimeout(timer);
  }
}

// 带 Cookie 的手动重定向（部分站点如猫眼用 Set-Cookie 自跳转做校验，
// fetch 不自动管理 cookie 会死循环；等价于 requests.Session 的行为）
async function fetchWithCookies(url, headers, signal) {
  const jar = new Map();
  let current = url;
  for (let i = 0; i < 5; i++) {
    const cookieStr = [...jar.entries()].map(([k, v]) => `${k}=${v}`).join("; ");
    const r = await fetch(current, {
      headers: { ...headers, ...(cookieStr ? { Cookie: cookieStr } : {}) },
      redirect: "manual",
      signal,
    });
    const setList = typeof r.headers.getSetCookie === "function" ? r.headers.getSetCookie() : [];
    for (const c of setList || []) {
      const pair = c.split(";")[0];
      const eq = pair.indexOf("=");
      if (eq > 0) jar.set(pair.slice(0, eq).trim(), pair.slice(eq + 1).trim());
    }
    if (r.status >= 300 && r.status < 400) {
      const loc = r.headers.get("location");
      if (loc) { current = new URL(loc, current).href; continue; }
    }
    return r;
  }
  throw new Error("redirect loop");
}

// 编码探测：meta charset → 非 utf-8 则尝试对应 TextDecoder（workerd 不支持 gbk 时回退）
function decodeBody(bytes) {
  let enc = "utf-8";
  let ascii = "";
  for (let i = 0; i < Math.min(bytes.length, 1024); i++) {
    const c = bytes[i];
    if (c < 128) ascii += String.fromCharCode(c);
  }
  const m = ascii.match(/charset=["']?([\w-]+)/i);
  if (m && !/utf-?8/i.test(m[1])) enc = m[1].toLowerCase();
  try {
    return new TextDecoder(enc, { fatal: false }).decode(bytes);
  } catch {
    try { return new TextDecoder("utf-8").decode(bytes); } catch { return ascii; }
  }
}

// ── HTML 工具（替代 BeautifulSoup）─────────────────────
const NAMED_ENT = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", middot: "·", hellip: "…", mdash: "—", ndash: "–", ldquo: "“", rdquo: "”", lsquo: "‘", rsquo: "’" };
function decodeEntities(s) {
  return String(s)
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => { try { return String.fromCodePoint(parseInt(h, 16)); } catch { return ""; } })
    .replace(/&#(\d+);/g, (_, d) => { try { return String.fromCodePoint(+d); } catch { return ""; } })
    .replace(/&([a-zA-Z][a-zA-Z0-9]*);/g, (mm, name) => NAMED_ENT[name.toLowerCase()] ?? mm);
}
const stripTags = (s) => String(s).replace(/<[^>]*>/g, "");

// 通用锚点抽取（对应 Python 的 _html_list）：
// 扫描全部 <a href>，按 hostFilter / minLen / maxLen 过滤，相对链接用 absHost 补全
function extractAnchors(html, opts = {}) {
  const { hostFilter, minLen = 6, limit = 20, absHost = "", maxLen = 80, hrefIncludes } = opts;
  const out = [];
  const seen = new Set();
  const re = /<a\b[^>]*?href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = re.exec(html)) !== null && out.length < limit) {
    let href = (m[1] || m[2] || m[3] || "").trim();
    if (href.startsWith("//")) href = "https:" + href;
    const title = decodeEntities(stripTags(m[4] || "")).replace(/\s+/g, " ").trim();
    if (!title || title.length < minLen || title.length > maxLen * 2.2) continue;
    if (seen.has(title)) continue;
    if (hrefIncludes && !hrefIncludes.some((p) => href.includes(p))) continue;
    if (hostFilter) {
      const isAbsOk = href.includes(hostFilter);
      const isRel = href.startsWith("/");
      if (!isAbsOk && !isRel) continue;
    }
    if (href.startsWith("/") || !/^https?:/i.test(href)) {
      href = (absHost || "").replace(/\/+$/, "") + (/^\//.test(href) ? href : "/" + href);
    }
    if (!/^https?:/i.test(href)) continue;
    seen.add(title);
    out.push({ title, url: decodeEntities(href) });
  }
  return out;
}

function htmlList(url, ref, opts, note = "实时资讯", realtime = true) {
  // 返回 Promise<result>（供上层 await）
  return (async () => {
    const [html, err] = await httpGet(url, { referer: ref, timeout: 12000 });
    if (err) return fail(`${note}（${err}）`);
    const anchors = extractAnchors(html, { absHost: ref, ...opts });
    if (anchors.length >= 5) {
      return ok(anchors.map((a, i) => ({ rank: i + 1, title: a.title, url: a.url, hot: "" })), realtime, note);
    }
    return fail(`${note}（暂不可用）`);
  })();
}

// ── RSS / Atom（对应 Python 的 _rss）────────────────────
function matchTag(block, tag) {
  const m = block.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, "i"));
  if (!m) return "";
  return decodeEntities(m[1].replace(/<!\[CDATA\[|\]\]>/g, "").trim());
}
function rssParse(xml, limit = 20, note = "RSS·非实时更新") {
  const items = [];
  let blocks = xml.match(/<item[\s>][\s\S]*?<\/item>/gi) || [];
  if (!blocks.length) blocks = xml.match(/<entry[\s>][\s\S]*?<\/entry>/gi) || [];
  for (const b of blocks) {
    if (items.length >= limit) break;
    const title = matchTag(b, "title");
    let link = matchTag(b, "link");
    if (!link) {
      const hm = b.match(/<link[^>]*href=["']([^"']+)["']/i);
      link = hm ? hm[1] : "";
    }
    if (title && link) items.push({ rank: items.length + 1, title, url: decodeEntities(link), hot: "" });
  }
  return items.length ? ok(items, false, note) : fail(`RSS（暂不可用·${note}）`);
}
async function fetchRss(url, ref, note) {
  const urls = [url];
  if (url.startsWith("https://")) urls.push(url.replace("https://", "http://", 1));
  else if (url.startsWith("http://")) urls.push(url.replace("http://", "https://", 1));
  for (const u of urls) {
    const [content, err] = await httpGet(u, { referer: ref, raw: true, timeout: 12000 });
    if (err || !content) continue;
    const bytes = new Uint8Array(content);
    let text;
    try { text = new TextDecoder("utf-8").decode(bytes); } catch { continue; }
    const r = rssParse(text, 20, note || "RSS·非实时更新");
    if (r.status === "success") return r;
  }
  return fail(`RSS（暂不可用）`);
}

// ── 结果工厂（字段与 Python make_result/fail_result 完全一致）──
function ok(items, realtime = true, note) {
  return {
    status: "success",
    items,
    is_realtime: realtime,
    fetched_at: nowStr(),
    update_note: note || (realtime ? "实时榜单" : "非实时更新"),
  };
}
function fail(msg = "抓取失败") {
  return { status: "failed", items: [], is_realtime: false, fetched_at: nowStr(), update_note: `${msg}（${nowStr()}）` };
}

// ══════════════════════════════════════════════════════
// 【综合】
// ══════════════════════════════════════════════════════

async function fetchWeibo() {
  const [data, err] = await httpGet("https://weibo.com/ajax/side/hotSearch", { referer: "https://weibo.com/", json: true, timeout: 12000 });
  if (err || !data) return fail(`微博（${err || "无数据"}）`);
  const raw = (data.data && data.data.realtime) || [];
  const items = [];
  raw.slice(0, 20).forEach((it, i) => {
    const title = it.word || it.label_name || "";
    if (!title) return;
    const label = it.label_name || "";
    const hot = label || (str(it.num).match(/^\d+$/) ? `${Math.floor(+it.num / 10000)}万` : str(it.num));
    items.push({ rank: i + 1, title, url: `https://s.weibo.com/weibo?q=${encodeURIComponent(title)}`, hot: str(hot) });
  });
  return items.length ? ok(items, true) : fail("微博（无数据）");
}

async function fetchTencent() {
  const [data, err] = await httpGet(
    "https://i.news.qq.com/gw/event/hot_ranking_list?offset=0&count=20&strategy=1",
    { referer: "https://news.qq.com/", json: true, timeout: 12000 }
  );
  if (err || !data) return fail(`腾讯新闻（${err || "无数据"}）`);
  const list = (data.idlist && data.idlist[0] && data.idlist[0].newslist) || (data.data && data.data.hotRankingList) || [];
  const items = [];
  list.slice(0, 20).forEach((it) => {
    const title = it.title || it.hotTitle || "";
    if (!title || /每10分钟更新/.test(title)) return; // 列表头占位条目
    items.push({ rank: items.length + 1, title, url: it.url || it.articleUrl || "https://news.qq.com/", hot: str(it.hotScore || it.readCount || "") });
  });
  return items.length ? ok(items, true) : fail("腾讯新闻（无数据）");
}

async function fetchToutiao() {
  // 端点1: 头条热榜 JSON
  let [data] = await httpGet("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc", { referer: "https://www.toutiao.com/", json: true, timeout: 12000 });
  if (data) {
    const raw = data.data || [];
    const items = [];
    raw.slice(0, 20).forEach((it, i) => {
      const title = it.Title || it.title || "";
      if (!title) return;
      let hot = str(it.HotValue || it.hot_value || "");
      if (/^\d+$/.test(hot)) hot = `${Math.floor(+hot / 10000)}万`;
      items.push({ rank: i + 1, title, url: it.Url || it.url || "https://www.toutiao.com/", hot });
    });
    if (items.length) return ok(items, true, "实时热榜");
  }
  // 端点2: 第三方热榜镜像
  [data] = await httpGet("https://tcmarket.cdn.bceutils.com/hot-list/toutiao-hot-search.json", { accept: "application/json", json: true, timeout: 12000 });
  if (data) {
    const raw = Array.isArray(data) ? data : data.data || [];
    const items = raw.slice(0, 20).filter((it) => it.title)
      .map((it, i) => ({ rank: i + 1, title: it.title, url: it.url || "https://www.toutiao.com/", hot: str(it.hot || "") }));
    if (items.length) return ok(items, true, "实时热点");
  }
  return fail("今日头条（暂不可用）");
}

async function fetchBaidu() {
  const [data, err] = await httpGet("https://top.baidu.com/api/board?platform=wise&tab=realtime", { referer: "https://top.baidu.com/", json: true, timeout: 12000 });
  if (err || !data) return fail(`百度热搜（${err || "无数据"}）`);
  const raw = [];
  for (const c of (data.data && data.data.cards) || []) {
    for (const block of c.content || []) {
      const inner = block && block.content;
      if (Array.isArray(inner)) raw.push(...inner);
    }
  }
  const items = [];
  raw.slice(0, 20).forEach((it, i) => {
    const word = it.word || it.query || "";
    if (!word) return;
    items.push({
      rank: i + 1, title: word,
      url: it.url || it.rawUrl || `https://www.baidu.com/s?wd=${encodeURIComponent(word)}`,
      hot: str(it.hotScore || it.hot || ""),
    });
  });
  return items.length ? ok(items, true, "实时热搜") : fail("百度热搜（无数据）");
}

async function fetchDouyin() {
  const [data, err] = await httpGet("https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/", { referer: "https://www.douyin.com/", json: true, timeout: 12000 });
  if (err || !data) return fail(`抖音热搜（${err || "无数据"}）`);
  const raw = (data.word_list) || (data.data && data.data.word_list) || [];
  const items = [];
  raw.slice(0, 20).forEach((it, i) => {
    const word = it.word || "";
    if (!word) return;
    items.push({ rank: i + 1, title: word, url: `https://www.douyin.com/search/${encodeURIComponent(word)}`, hot: wan(it.hot_value) });
  });
  return items.length ? ok(items, true, "实时热搜") : fail("抖音热搜（无数据）");
}

// ── 贴吧热议（新增）──────────────────────────────────────
async function fetchTieba() {
  const [data, err] = await httpGet("https://tieba.baidu.com/hottopic/browse/topicList", { referer: "https://tieba.baidu.com/", json: true, timeout: 12000 });
  if (err || !data) return fail(`贴吧（${err || "无数据"}）`);
  const raw = (data.data && data.data.bang_topic && data.data.bang_topic.topic_list) || [];
  const items = [];
  raw.slice(0, 20).forEach((it, i) => {
    const title = it.topic_name || "";
    if (!title) return;
    let url = str(it.topic_url || "");
    if (!url) url = `https://tieba.baidu.com/hottopic/browse/hottopic?topic_id=${it.topic_id}`;
    const d = +it.discuss_num || 0;
    items.push({ rank: i + 1, title, url, hot: d ? `${Math.floor(d / 10000)}万讨论` : "" });
  });
  return items.length ? ok(items, true, "热议话题") : fail("贴吧（无数据）");
}

async function fetchWangyi() {
  // 端点1: 3g 触屏版 JSONP
  const [text, err] = await httpGet("https://3g.163.com/touch/reconstruct/article/list/BBM54PGAwangning/0-20.html", { referer: "https://news.163.com/", timeout: 12000 });
  if (!err && text) {
    const m = text.match(/artiList\(([\s\S]*)\)/);
    if (m) {
      try {
        const data = JSON.parse(m[1]);
        const raw = Object.values(data)[0] || [];
        const items = [];
        raw.slice(0, 20).forEach((it, i) => {
          const title = it.title || "";
          if (!title) return;
          const url = it.url || (it.docid ? `https://m.163.com/news/article/${it.docid}.html` : "https://news.163.com/");
          items.push({ rank: i + 1, title, url, hot: "" });
        });
        if (items.length) return ok(items, true, "实时要闻");
      } catch { /* 落到端点2 */ }
    }
  }
  // 端点2: 排行页 HTML
  return htmlList("https://news.163.com/rank/", "https://www.163.com/",
    { hostFilter: "163.com", minLen: 7, hrefIncludes: ["/article/", "news.163.com"] }, "实时热榜");
}

async function fetchSina() {
  // 端点1: RSS
  const rss = await fetchRss("https://rss.sina.com.cn/news/china/focus.xml", "https://news.sina.com.cn/", "RSS·非实时");
  if (rss.status === "success") return rss;
  // 端点2: 滚动新闻 JSON API
  const [data] = await httpGet("https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=20&page=1", { referer: "https://news.sina.com.cn/", json: true, timeout: 12000 });
  if (data && data.result && data.result.data) {
    const items = [];
    data.result.data.slice(0, 20).forEach((it, i) => {
      if (it.title) items.push({ rank: i + 1, title: it.title, url: it.url || "https://news.sina.com.cn/", hot: "" });
    });
    if (items.length) return ok(items, true, "实时新闻");
  }
  // 端点3: 首页 HTML
  for (const pageUrl of ["https://news.sina.com.cn/", "https://news.sina.cn/"]) {
    const r = await htmlList(pageUrl, "https://news.sina.com.cn/", { hostFilter: "sina", minLen: 8, limit: 20 }, "实时新闻");
    if (r.status === "success") return r;
  }
  return fail("新浪新闻（暂不可用）");
}

async function fetchPengpai() {
  // 端点1: 首页右侧栏 cache API
  const [data] = await httpGet("https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar", { referer: "https://www.thepaper.cn/", json: true, timeout: 12000 });
  if (data && data.data) {
    const d = data.data;
    const raw = d.hotNews || d.editorHandpicked || d.morningEveningNews || [];
    const items = [];
    raw.slice(0, 20).forEach((it, i) => {
      const title = it.name || it.title || "";
      if (!title) return;
      const contId = it.contId || it.nodeId || "";
      // 注意 URL 格式：newsDetail_detail_ 已失效（302 回首页），现在是 newsDetail_forward_
      const link = it.link || (contId ? `https://www.thepaper.cn/newsDetail_forward_${contId}` : `https://www.thepaper.cn/searchResult?id=${contId || ""}&keyword=${encodeURIComponent(title)}`);
      items.push({ rank: i + 1, title, url: link, hot: str(it.praiseTimes || "") });
    });
    if (items.length) return ok(items, true, "实时热闻");
  }
  // 端点2: 首页 HTML
  return htmlList("https://www.thepaper.cn/", "https://www.thepaper.cn/",
    { hostFilter: "thepaper.cn", hrefIncludes: ["newsDetail"], minLen: 8 }, "实时要闻");
}

// ── 知乎热榜（2026-08 端点已改版：api.zhihu.com/topstory/hot-lists/total）──
async function fetchZhihu() {
  const endpoints = [
    "https://api.zhihu.com/topstory/hot-lists/total?limit=20",
    "https://www.zhihu.com/api/v4/topstory/hot-lists?limit=20&desktop=true",
    "https://api.zhihu.com/topstory/hot-lists?limit=20",
  ];
  for (const url of endpoints) {
    const [data] = await httpGet(url, { referer: "https://www.zhihu.com/", accept: "application/json, text/plain, */*", json: true, timeout: 12000 });
    if (!data) continue;
    const raw = data.data || data.top_stories || data.topstories || [];
    const items = [];
    raw.slice(0, 20).forEach((item, i) => {
      if (!item || typeof item !== "object") return;
      const target = item.target || item;
      const title = target.title || (target.question && target.question.title) || "";
      if (!title) return;
      const qid = target.id || (target.question && target.question.id) || "";
      const area = target.metrics_area && target.metrics_area.text;
      const metric = area || str(target.vote_count || target.follower_count || "");
      items.push({
        rank: i + 1, title,
        url: qid ? `https://www.zhihu.com/question/${qid}` : "https://www.zhihu.com/",
        hot: str(metric),
      });
    });
    if (items.length) return ok(items, true, "实时热搜");
  }
  // 兜底: 知乎日报
  const [data] = await httpGet("https://news-at.zhihu.com/api/4/news/latest", { referer: "https://daily.zhihu.com/", json: true, timeout: 12000 });
  if (data) {
    const stories = (data.top_stories || []).slice(0, 15).concat((data.stories || []).slice(0, 10));
    const items = [];
    stories.forEach((s, i) => {
      if (s.title) items.push({ rank: i + 1, title: s.title, url: s.id ? `https://daily.zhihu.com/story/${s.id}` : "https://daily.zhihu.com/", hot: "" });
    });
    if (items.length) return ok(items.slice(0, 20), false, "知乎日报·非实时");
  }
  return fail("知乎（暂不可用）");
}

async function fetchBilibili() {
  const mk = (raw, note) => {
    const items = [];
    raw.slice(0, 20).forEach((it, i) => {
      const view = (it.stat && it.stat.view) || 0;
      items.push({
        rank: i + 1, title: it.title || "",
        url: it.bvid ? `https://www.bilibili.com/video/${it.bvid}` : "https://www.bilibili.com/",
        hot: view ? `${Math.floor(view / 10000)}万播放` : "",
      });
    });
    return items.length ? ok(items, true, note) : null;
  };
  // 端点1: 全站排行榜
  let [data] = await httpGet("https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all", { referer: "https://www.bilibili.com/", json: true, timeout: 10000 });
  if (data && data.code === 0) {
    const r = mk((data.data && data.data.list) || [], "实时榜单");
    if (r) return r;
  }
  // 端点2: 热门视频流
  [data] = await httpGet("https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1", { referer: "https://www.bilibili.com/", json: true, timeout: 10000 });
  if (data && data.code === 0) {
    const r = mk((data.data && data.data.list) || [], "热门视频");
    if (r) return r;
  }
  // 端点3: 热搜榜
  [data] = await httpGet("https://app.bilibili.com/x/v2/search/trending/ranking?limit=20", { referer: "https://www.bilibili.com/", json: true, timeout: 10000 });
  if (data) {
    const raw = (data.data && data.data.list) || data.result || [];
    const items = [];
    raw.slice(0, 20).forEach((it, i) => {
      const kw = it.keyword || it.show_name || it.name || "";
      if (!kw) return;
      items.push({ rank: i + 1, title: kw, url: `https://search.bilibili.com/all?keyword=${encodeURIComponent(kw)}`, hot: str(it.hot_score || "") });
    });
    if (items.length) return ok(items, true, "热搜榜");
  }
  return fail("B站（暂不可用）");
}

// 人民日报：RSS（人民网无更快公开接口，内容偏旧但链接可直开文章页；
// 链接缺失时兜底到站内搜索该标题，而不是跳首页）
async function fetchRmrb() {
  const r = await fetchRss("http://www.people.com.cn/rss/politics.xml", "https://www.people.com.cn/", "RSS·非实时更新");
  if (r.status === "success" && Array.isArray(r.items)) {
    r.items = r.items.map((it) => ({
      ...it,
      url: it.url && !/people\.com\.cn\/?$/.test(it.url)
        ? it.url
        : `http://search.people.cn/s/?keyword=${encodeURIComponent(it.title)}`,
    }));
  }
  return r;
}

async function fetchCctv() {
  return htmlList("https://news.cctv.com/", "https://news.cctv.com/",
    { hostFilter: "cctv.com", minLen: 8 }, "实时新闻");
}

async function fetchXinhua() {
  for (const page of ["https://www.news.cn/world/", "https://www.news.cn/politics/"]) {
    const r = await htmlList(page, "https://www.news.cn/", { hostFilter: "news.cn", minLen: 8 }, "实时新闻");
    if (r.status === "success") return r;
  }
  return fail("新华社（暂不可用）");
}

// ══════════════════════════════════════════════════════
// 【科技】
// ══════════════════════════════════════════════════════

async function fetch36kr() {
  const r = await fetchRss("https://36kr.com/feed", "https://36kr.com/", "RSS·资讯");
  if (r.status === "success") return r;
  return htmlList("https://36kr.com/", "https://36kr.com/", { hostFilter: "36kr.com", hrefIncludes: ["/p/"], minLen: 7 }, "实时资讯");
}

// 虎嗅槽位：虎嗅 RSS（海外出口常可通过 WAF）→ 雷锋网 RSS → 雷锋网 HTML
async function fetchHuxiu() {
  const [hxXml] = await httpGet("https://www.huxiu.com/rss/0.xml", { referer: "https://www.huxiu.com/", raw: true, timeout: 6000 });
  if (hxXml) {
    try {
      const r = rssParse(new TextDecoder("utf-8").decode(new Uint8Array(hxXml)), 20, "RSS·虎嗅");
      if (r.status === "success") return r;
    } catch { /* 落到雷锋网 */ }
  }
  const lp = await fetchRss("https://www.leiphone.com/feed", "https://www.leiphone.com/", "RSS·雷锋网");
  if (lp.status === "success") return lp;
  return htmlList("https://www.leiphone.com/", "https://www.leiphone.com/", { hostFilter: "leiphone.com", minLen: 10 }, "科技资讯·雷锋网");
}

// ── 掘金热榜（新增）──────────────────────────────────────
async function fetchJuejin() {
  const [data, err] = await httpGet("https://api.juejin.cn/content_api/v1/content/article_rank?category_id=1&type=hot", { referer: "https://juejin.cn/", json: true, timeout: 12000 });
  if (err || !data) return fail(`掘金（${err || "无数据"}）`);
  const raw = data.data || [];
  const items = [];
  raw.slice(0, 20).forEach((it, i) => {
    const c = it.content || {};
    const title = c.title || "";
    if (!title) return;
    const hot = (it.content_counter && it.content_counter.hot_rank) || 0;
    items.push({ rank: i + 1, title, url: c.content_id ? `https://juejin.cn/post/${c.content_id}` : "https://juejin.cn/", hot: hot ? `${hot}` : "" });
  });
  return items.length ? ok(items, true, "热榜") : fail("掘金（无数据）");
}

// ── 开源中国（新增，RSS）─────────────────────────────────
async function fetchOschina() {
  return fetchRss("https://www.oschina.net/news/rss", "https://www.oschina.net/", "RSS·开源资讯");
}

// ── 钛媒体（新增，RSS）───────────────────────────────────
async function fetchTmtpost() {
  return fetchRss("https://www.tmtpost.com/feed", "https://www.tmtpost.com/", "RSS·钛媒体");
}

// ── V2EX（新增，仅云端——本地网络通常不可达）────────────────
async function fetchV2ex() {
  const [data, err] = await httpGet("https://www.v2ex.com/api/topics/hot.json", { referer: "https://www.v2ex.com/", json: true, timeout: 12000 });
  if (err || !Array.isArray(data)) return fail(`V2EX（${err || "无数据"}）`);
  const items = data.slice(0, 20).map((t, i) => ({
    rank: i + 1, title: t.title || "",
    url: t.url || `https://www.v2ex.com/t/${t.id}`,
    hot: t.replies ? `${t.replies}回复` : "",
  })).filter((it) => it.title);
  return items.length ? ok(items, true, "热门主题") : fail("V2EX（无数据）");
}

async function fetchIfanr() {
  return fetchRss("https://www.ifanr.com/feed", "https://www.ifanr.com/", "RSS·非实时更新");
}
async function fetchSspai() {
  return fetchRss("https://sspai.com/feed", "https://sspai.com/", "RSS·非实时更新");
}
async function fetchIthome() {
  return fetchRss("https://www.ithome.com/rss/", "https://www.ithome.com/", "RSS·非实时更新");
}

// GitHub：单端点短超时 + API 兜底（避免全局重试放大超时）
async function fetchGithub() {
  const ua = pick(UAS);
  try {
    const [html] = await httpGet("https://github.com/trending?since=daily&spoken_language_code=zh", { referer: "https://github.com/", timeout: 6000 });
    if (html) {
      const blocks = html.match(/<article[^>]*class="[^"]*Box-row[\s\S]*?<\/article>/gi) || [];
      const items = [];
      blocks.slice(0, 20).forEach((b, i) => {
        const h2 = b.match(/<h2[\s\S]*?<a[^>]*href="([^"]+)"[\s\S]*?<\/h2>/i);
        if (!h2) return;
        const name = stripTags(h2[0]).replace(/\s+/g, " ").trim();
        const starA = b.match(/href="[^"]*\/stargazers[^"]*"[^>]*>\s*([\d,]+)/i);
        items.push({ rank: i + 1, title: name, url: `https://github.com${h2[1]}`, hot: starA ? `${starA[1].replace(/,/g, "")}★` : "" });
      });
      if (items.length) return ok(items, false, "日榜·非实时更新");
    }
  } catch { /* 落到 API */ }
  try {
    const [data] = await httpGet(
      "https://api.github.com/search/repositories?q=created:%3E2026-06-01+language:python&sort=stars&order=desc&per_page=20",
      { referer: "https://github.com/", accept: "application/json", json: true, timeout: 6000 }
    );
    if (data && data.items) {
      const items = data.items.slice(0, 20).filter((r) => r.full_name)
        .map((r, i) => ({ rank: i + 1, title: r.full_name, url: r.html_url || "https://github.com/", hot: `${r.stargazers_count || 0}★` }));
      if (items.length) return ok(items, false, "热门仓库·API");
    }
  } catch { /* ignore */ }
  return fail("GitHub（暂不可用）");
}

// ══════════════════════════════════════════════════════
// 【娱乐】
// ══════════════════════════════════════════════════════

async function fetchDouban() {
  const [data, err] = await httpGet(
    "https://movie.douban.com/j/search_subjects?type=movie&tag=%E7%83%AD%E9%97%A8&sort=recommend&page_limit=20&page_start=0",
    { referer: "https://movie.douban.com/", json: true, timeout: 12000 }
  );
  if (err || !data) return fail(`豆瓣电影（${err || "无数据"}）`);
  const items = (data.subjects || []).slice(0, 20).map((it, i) => ({
    rank: i + 1, title: it.title || "", url: it.url || "https://movie.douban.com/",
    hot: it.rate ? `评分 ${it.rate}` : "",
  })).filter((it) => it.title);
  return items.length ? ok(items, true, "实时热门电影") : fail("豆瓣电影（无数据）");
}

async function fetchMaoyan() {
  const boardNotes = { "7": "热映口碑榜", "4": "经典榜", "6": "期待榜" };
  for (const board of ["7", "4", "6"]) {
    const [html, err] = await httpGet(`https://www.maoyan.com/board/${board}`, { referer: "https://www.maoyan.com/", timeout: 12000, cookies: true });
    if (err) continue;
    const anchors = extractAnchors(html, { hostFilter: "maoyan.com", hrefIncludes: ["/films/"], minLen: 2, maxLen: 30, limit: 20, absHost: "https://www.maoyan.com" });
    if (anchors.length >= 5) return ok(anchors.map((a, i) => ({ rank: i + 1, title: a.title, url: a.url, hot: "" })), true, boardNotes[board]);
  }
  return fail("猫眼电影（无数据）");
}

async function fetchWeiboEnt() {
  const [data, err] = await httpGet("https://weibo.com/ajax/side/hotSearch", { referer: "https://weibo.com/", json: true, timeout: 12000 });
  if (err || !data) return fail(`微博娱乐（${err || "无数据"}）`);
  const raw = (data.data && data.data.realtime) || [];
  const items = [];
  for (const item of raw) {
    if (items.length >= 15) break;
    const cat = str(item.category || item.label || "");
    const word = item.word || "";
    if (!word) continue;
    if (!/娱乐|影视|明星/.test(cat)) continue;
    items.push({ rank: items.length + 1, title: word, url: `https://s.weibo.com/weibo?q=${encodeURIComponent(word)}`, hot: wan(item.num) });
  }
  if (!items.length) {
    raw.slice(0, 10).forEach((item, i) => {
      if (item.word) items.push({ rank: i + 1, title: item.word, url: `https://s.weibo.com/weibo?q=${encodeURIComponent(item.word)}`, hot: "" });
    });
  }
  return items.length ? ok(items, true) : fail("微博娱乐（无数据）");
}

async function fetchSinaEnt() {
  const rss = await fetchRss("https://rss.sina.com.cn/news/ent/yule.xml", "https://ent.sina.com.cn/", "RSS·非实时");
  if (rss.status === "success") return rss;
  const [data] = await httpGet("https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=1686&num=20&page=1", { referer: "https://ent.sina.com.cn/", json: true, timeout: 12000 });
  if (data && data.result && data.result.data) {
    const items = [];
    data.result.data.slice(0, 20).forEach((it, i) => {
      if (it.title) items.push({ rank: i + 1, title: it.title, url: it.url || "https://ent.sina.com.cn/", hot: "" });
    });
    if (items.length) return ok(items, true, "实时娱乐");
  }
  for (const page of ["https://ent.sina.com.cn/", "https://ent.sina.cn/"]) {
    const r = await htmlList(page, "https://ent.sina.com.cn/", { hostFilter: "sina", minLen: 7 }, "实时娱乐");
    if (r.status === "success") return r;
  }
  return fail("新浪娱乐（暂不可用）");
}

async function fetchIfengEnt() {
  return htmlList("https://ent.ifeng.com/", "https://ent.ifeng.com/",
    { hostFilter: "ifeng.com", hrefIncludes: ["/c/"], minLen: 8 }, "实时娱乐");
}

// ══════════════════════════════════════════════════════
// 【财经】
// ══════════════════════════════════════════════════════

async function fetchCaixin() {
  for (const page of ["https://www.caixin.com/", "https://economy.caixin.com/", "https://finance.caixin.com/"]) {
    const r = await htmlList(page, "https://www.caixin.com/", { hostFilter: "caixin.com", minLen: 12 }, "实时资讯");
    if (r.status === "success") return r;
  }
  return fail("财新（暂不可用）");
}

async function fetchYicai() {
  return htmlList("https://www.yicai.com/news/", "https://www.yicai.com/",
    { hostFilter: "yicai.com", hrefIncludes: ["/news/"], minLen: 8 }, "实时资讯");
}

async function fetchJiemian() {
  return htmlList("https://www.jiemian.com/", "https://www.jiemian.com/",
    { hostFilter: "jiemian.com", hrefIncludes: ["/article/"], minLen: 8 }, "实时资讯");
}

async function fetchWallstreet() {
  for (const url of [
    "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=20",
    "https://api-one.wallstcn.com/apiv1/content/articles?channel=global-channel&limit=20",
  ]) {
    const [data] = await httpGet(url, { referer: "https://wallstreetcn.com/", accept: "application/json", json: true, timeout: 12000 });
    if (!data) continue;
    const raw = (data.data && (data.data.items || data.data)) || data.results || [];
    if (!Array.isArray(raw)) continue;
    const items = [];
    raw.slice(0, 20).forEach((item, i) => {
      let content = item.title || item.content_text || item.description || item.summary || "";
      if (!content) return;
      content = String(content).trim().split("\n")[0].slice(0, 60);
      const uri = item.uri || "";
      const lid = item.id || "";
      const link = uri ? `https://wallstreetcn.com${uri}` : (lid ? `https://wallstreetcn.com/live/livenews/${lid}` : "https://wallstreetcn.com/");
      items.push({ rank: i + 1, title: content, url: link, hot: "" });
    });
    if (items.length) return ok(items, true, "实时快讯");
  }
  return fail("华尔街见闻（暂不可用）");
}

// 东方财富快讯（替代雪球——槽位名沿用 xueqiu 保持兼容）
async function fetchXueqiu() {
  const [text, err] = await httpGet("https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html", { referer: "https://www.eastmoney.com/", timeout: 12000 });
  if (err || !text) return fail(`东方财富（${err || "无数据"}）`);
  const m = text.match(/ajaxResult\s*=\s*(\{[\s\S]*\})/);
  if (!m) return fail("东方财富（解析失败）");
  try {
    const data = JSON.parse(m[1]);
    const raw = data.LivesList || data.data || [];
    const items = [];
    raw.slice(0, 20).forEach((it, i) => {
      const title = it.title || it.digest || "";
      if (!title) return;
      items.push({ rank: i + 1, title, url: it.url_w || it.url || "https://www.eastmoney.com/", hot: "" });
    });
    if (items.length) return ok(items, true, "实时快讯·东财");
  } catch { /* ignore */ }
  return fail("东方财富（暂不可用）");
}

// ══════════════════════════════════════════════════════
// 【军事国际】
// ══════════════════════════════════════════════════════

async function fetchGuancha() {
  return htmlList("https://www.guancha.cn/", "https://www.guancha.cn/",
    { hostFilter: "guancha.cn", hrefIncludes: [".shtml"], minLen: 8 }, "实时资讯");
}

async function fetchHuanqiu() {
  const [data] = await httpGet('https://www.huanqiu.com/api/list?node=%22/hqmh%22&offset=0&limit=20', { referer: "https://www.huanqiu.com/", json: true, timeout: 12000 });
  if (data) {
    const raw = data.list || data.items || [];
    const items = [];
    raw.slice(0, 20).forEach((it, i) => {
      const title = it.title || "";
      if (!title) return;
      const aid = it.aid || it.url || "";
      const url = /^https?:/.test(str(aid)) ? str(aid) : `https://www.huanqiu.com/article/${aid}`;
      items.push({ rank: i + 1, title, url, hot: "" });
    });
    if (items.length) return ok(items, true, "实时资讯");
  }
  return htmlList("https://www.huanqiu.com/", "https://www.huanqiu.com/",
    { hostFilter: "huanqiu.com", hrefIncludes: ["/article/"], minLen: 8 }, "实时资讯");
}

async function fetchCankaoxiaoxi() {
  return htmlList("https://www.news.cn/world/", "https://www.news.cn/",
    { hostFilter: "news.cn", minLen: 8 }, "国际新闻·非实时", false);
}

// ══════════════════════════════════════════════════════
// 【体育】
// ══════════════════════════════════════════════════════

async function fetchHupu() {
  const noise = /(下载|打开|安装).{0,6}(App|APP|客户端)|虎扑APP/i;
  for (const url of ["https://bbs.hupu.com/all", "https://www.hupu.com/"]) {
    const r = await htmlList(url, "https://www.hupu.com/", { hostFilter: "hupu.com", minLen: 7, limit: 40 }, "实时热帖");
    if (r.status === "success") {
      const items = r.items.filter((it) => !noise.test(it.title)).slice(0, 20).map((it, i) => ({ ...it, rank: i + 1 }));
      if (items.length >= 5) return ok(items, true, "实时热帖");
    }
  }
  return htmlList("https://bbs.hupu.com/nba", "https://bbs.hupu.com/", { hostFilter: "hupu.com", minLen: 7 }, "NBA热帖");
}

// 直播吧（GBK 编码；workerd 的 TextDecoder 不支持 gbk 时云端不可用，本地 Python 版正常）
async function fetchDongqiudi() {
  const [buf, err] = await httpGet("https://m.zhibo8.com/", { referer: "https://m.zhibo8.com/", raw: true, timeout: 12000 });
  if (err || !buf) return fail(`体育资讯（${err}）`);
  const bytes = new Uint8Array(buf);
  let text = null;
  for (const enc of ["gb18030", "gbk", "utf-8"]) {
    try { text = new TextDecoder(enc, { fatal: true }).decode(bytes); break; } catch { continue; }
  }
  if (!text) return fail("体育资讯（编码不支持）");
  const anchors = extractAnchors(text, { hrefIncludes: ["/news/"], minLen: 8, limit: 20, absHost: "https://m.zhibo8.com" });
  if (anchors.length >= 5) return ok(anchors.map((a, i) => ({ rank: i + 1, title: a.title, url: a.url, hot: "" })), true, "体育资讯·直播吧");
  return fail("懂球帝（暂不可用）");
}

async function fetchCctvSports() {
  return htmlList("https://sports.cctv.com/", "https://sports.cctv.com/",
    { hostFilter: "cctv.com", hrefIncludes: ["/202"], minLen: 8 }, "体育新闻·非实时", false);
}

// ── 导出（键名与 app.py FETCHERS 一致）─────────────────
export const FETCHERS = {
  weibo: fetchWeibo,
  tencent: fetchTencent,
  toutiao: fetchToutiao,
  baidu: fetchBaidu,
  douyin: fetchDouyin,
  tieba: fetchTieba,
  wangyi: fetchWangyi,
  sina: fetchSina,
  rmrb: fetchRmrb,
  cctv: fetchCctv,
  xinhua: fetchXinhua,
  pengpai: fetchPengpai,
  zhihu: fetchZhihu,
  bilibili: fetchBilibili,
  "36kr": fetch36kr,
  huxiu: fetchHuxiu,
  juejin: fetchJuejin,
  oschina: fetchOschina,
  tmtpost: fetchTmtpost,
  v2ex: fetchV2ex,
  ifanr: fetchIfanr,
  sspai: fetchSspai,
  ithome: fetchIthome,
  github: fetchGithub,
  douban: fetchDouban,
  maoyan: fetchMaoyan,
  weibo_ent: fetchWeiboEnt,
  sina_ent: fetchSinaEnt,
  ifeng_ent: fetchIfengEnt,
  caixin: fetchCaixin,
  yicai: fetchYicai,
  jiemian: fetchJiemian,
  wallstreet: fetchWallstreet,
  xueqiu: fetchXueqiu,
  guancha: fetchGuancha,
  huanqiu: fetchHuanqiu,
  cankaoxiaoxi: fetchCankaoxiaoxi,
  hupu: fetchHupu,
  dongqiudi: fetchDongqiudi,
  cctv_sports: fetchCctvSports,
};

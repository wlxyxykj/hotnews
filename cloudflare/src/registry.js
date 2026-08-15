// 平台注册表 —— 与 app.py 的 FETCHERS/CATEGORIES 保持一致（云端多一个 v2ex）
// 数据流向：index.js 路由 → fetchers.js 抓取 → KV 缓存

export const PLATFORM_NAMES = {
  // 综合
  weibo: "微博热搜", tencent: "腾讯新闻", toutiao: "今日头条",
  baidu: "百度热搜", douyin: "抖音热搜", tieba: "贴吧热议",
  wangyi: "网易新闻", sina: "新浪新闻", rmrb: "人民日报",
  cctv: "央视新闻", xinhua: "新华社", pengpai: "澎湃新闻",
  zhihu: "知乎热搜", bilibili: "B站排行",
  // 科技
  "36kr": "36氪", huxiu: "虎嗅·雷锋网", juejin: "掘金热榜",
  oschina: "开源中国", tmtpost: "钛媒体", v2ex: "V2EX",
  ifanr: "爱范儿", sspai: "少数派", ithome: "IT之家", github: "GitHub趋势",
  // 娱乐
  douban: "豆瓣电影", maoyan: "猫眼电影", weibo_ent: "微博娱乐",
  sina_ent: "新浪娱乐", ifeng_ent: "凤凰娱乐",
  // 财经
  caixin: "财新", yicai: "第一财经", jiemian: "界面新闻",
  wallstreet: "华尔街见闻", xueqiu: "东方财富",
  // 军事国际
  guancha: "观察者网", huanqiu: "环球时报", cankaoxiaoxi: "参考消息",
  // 体育
  hupu: "虎扑", dongqiudi: "懂球帝", cctv_sports: "央视体育",
};

export const CATEGORIES = {
  "综合":     ["weibo","tencent","baidu","douyin","tieba","toutiao","wangyi","sina","pengpai","zhihu","bilibili","rmrb","cctv","xinhua"],
  "科技":     ["36kr","huxiu","juejin","oschina","tmtpost","v2ex","ifanr","sspai","ithome","github"],
  "娱乐":     ["douban","maoyan","weibo_ent","sina_ent","ifeng_ent"],
  "财经":     ["caixin","yicai","jiemian","wallstreet","xueqiu"],
  "军事国际": ["guancha","huanqiu","cankaoxiaoxi"],
  "体育":     ["hupu","dongqiudi","cctv_sports"],
};

// Cron 每轮必刷的实时热搜（纯 JSON 接口、单端点、快）
export const REALTIME_CORE = ["weibo", "baidu", "douyin", "toutiao", "zhihu", "bilibili", "tencent", "tieba"];

// 其余平台分 4 组轮换，配合每 3 分钟一次的 cron，
// 每平台最长约 12~15 分钟刷新一次；每轮子请求 ≤ 16 平台 × 平均 1.5 端点 < 50 限额
export const ROTATION_GROUPS = [
  ["wangyi", "sina", "pengpai", "rmrb", "cctv", "xinhua", "caixin", "yicai"],
  ["36kr", "huxiu", "juejin", "oschina", "tmtpost", "ifanr", "sspai", "ithome"],
  ["douban", "maoyan", "weibo_ent", "sina_ent", "ifeng_ent", "jiemian", "wallstreet", "xueqiu"],
  ["v2ex", "github", "guancha", "huanqiu", "cankaoxiaoxi", "hupu", "dongqiudi", "cctv_sports"],
];

export const ALL_PLATFORMS = Object.keys(PLATFORM_NAMES);

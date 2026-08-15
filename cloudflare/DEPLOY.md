# Cloudflare 部署与运维手册

> 本项目已成功部署过一次（2026-08-14）。本文记录完整流程、**实际踩过的坑**、以及日常更新运维方法。
> 实际部署的账号/ID 等私密信息记录在《部署记录-勿提交.md》（已 gitignore，不进仓库）。

---

## 一、线上架构一览

```
用户（全球任意位置）
  │
  ▼  https://hotnews.<你的子域>.workers.dev
Cloudflare Worker（全球 300+ 边缘节点常驻，无冷启动）
  ├── /api/*        → 数据接口（读 KV 缓存，毫秒级返回）
  ├── 其它路径       → 静态首页（Workers Assets，CDN 分发）
  │
  └── Cron 每 3 分钟 → 抓取一批平台写回 KV（8 个实时榜每轮必刷，
                       其余 32 个平台分 4 组轮换，全量约 12~15 分钟一轮）
```

首次部署后无需任何手动操作：数据预热、失败回退（6 小时内旧数据标注「延迟」）全部自动。

## 🔁 附带福利：Render 保活

Cron 每轮执行完数据预热后，会顺带 GET 一次 `KEEPALIVE_URL`（默认 `https://hotnews-top.onrender.com/api/ping`）——
即 **Render 免费版 15 分钟休眠的问题由本 Worker 自动解决**，无需再注册 UptimeRobot 之类的监控服务。

- 换成其它要保活的服务：`npx wrangler secret put KEEPALIVE_URL` 或改 `wrangler.jsonc` 的 `vars`
- 关闭保活：把 `KEEPALIVE_URL` 设为空字符串 `""`
- 前端也内置了页面打开期间每 4.5 分钟一次的 `/api/ping`，双保险

---

## 二、首次部署流程（实测 5 分钟）

**前置**：Node.js 18+；注册 Cloudflare 免费账号（https://dash.cloudflare.com/sign-up ，无需绑卡）。

```powershell
cd cloudflare
npm install          # 仅装 wrangler 命令行工具
npx wrangler login   # 浏览器授权
npm run setup        # 一键：建 KV → 自动填 ID → 同步前端 → 部署
```

`npm run setup` 脚本（scripts/deploy.mjs）会依次完成并中文提示：

1. 检查登录状态
2. 创建 KV 命名空间，把 ID 自动写进 `wrangler.jsonc`（只创建一次，重跑不会重复建）
3. 从 `templates/index.html` 同步最新前端到 `public/`
4. `wrangler deploy` 部署，输出线上地址

### ⚠️ 踩过的坑（重要，别人最容易翻车的地方）

| 坑 | 现象 | 解决 |
|---|---|---|
| **项目路径含 `&`**（如 `tool&note`） | `npx wrangler` 报 `'xxx\node_modules\.bin\' 不是内部或外部命令`、`Cannot find module ...wrangler.js` | 删除 `node_modules` 后 npx 会改用全局缓存（路径无 `&`）：`Remove-Item -Recurse -Force node_modules`。**根治**：把仓库 clone 到无特殊字符的路径，如 `C:\dev\hotnews` |
| npm 拦截依赖安装脚本 | `install-scripts blocked: esbuild / workerd` | 同上——删 `node_modules` 用全局缓存版 wrangler 即可，不影响部署 |
| 首次部署要求注册子域名 | 交互式提问 `What would you like your workers.dev subdomain to be?` | 这是**账号级唯一前缀**（终身一次），全网唯一，建议「名字+数字」。输入可用的后回车继续即可 |
| 子域名不可用 | `Subdomain is unavailable` | 被别人占了，换一个（如 `zengjy-2026`） |
| **大陆无法直连 `*.workers.dev`** | 浏览器打不开 / DNS 解析超时或解析到假 IP | 这是 workers.dev 整域被 DNS 污染的已知现状，**不是部署失败**（海外正常）。对策见下节 |

## ⚠️ 大陆访问 workers.dev 的三种方案

`*.workers.dev` 在大陆被污染，部署本身是成功的（海外节点正常、Cron 照常预热数据）。按需选择：

| 方案 | 做法 | 适合 |
|---|---|---|
| **A. 绑定自定义域名（推荐）** | 买个域名（约 ¥10/年）接入 Cloudflare，Worker 绑定后走自己的域名，大陆一般可直连。步骤：控制台 → Workers → hotnews → Settings → **Domains & Routes** → Add → Custom domain，填 `news.你的域名.com`，自动配 HTTPS | 要长期给自己/朋友用 |
| B. 开代理访问 | 浏览器走代理后直接打开 workers.dev 地址即可 | 自己临时看一眼 |
| C. 继续用本地版 | `python app.py`，数据同一套 | 不想折腾域名 |

---

## 三、日常运维

### 更新代码后重新部署（最常用）

```powershell
cd cloudflare
npm run setup        # 或 npm run deploy，两者等价
```

改动 Python 版（app.py）**不需要**重新部署——云端跑的是 `cloudflare/src/` 里的 JS 版；两边 API 兼容但代码独立，改了抓取逻辑记得两边同步改。

### 查看线上日志（调试抓取问题）

```powershell
npx wrangler tail
```

实时滚动线上 Worker 的输出，Ctrl+C 停止。

### 健康检查

```powershell
curl https://<你的地址>/api/health
```

返回每个平台的状态：`fresh`（新鲜）/ `stale`（延迟保底）/ `failed` / `uncached`。

### 手动触发一轮预热（不想等 Cron 时）

```powershell
curl https://<你的地址>/api/refresh
```

### 修改通信密钥（建议做）

默认密钥是公开的内置值，改为随机串（影响账号令牌签名，与本地版一致则账号互通）：

```powershell
npx wrangler secret put SECRET_KEY
# 输入一串随机字符回车，然后 npm run setup 重新部署生效
```

> 改密钥后已登录用户的令牌会失效，需重新登录。

### （可选）启用账号 / 收藏功能（D1 数据库）

不配置 D1 时新闻功能完整，仅「登录/收藏/历史」提示未启用。启用：

```powershell
npx wrangler d1 create hotnews
# 把输出的 database_id 填进 wrangler.jsonc（取消 d1_databases 三行注释）
npx wrangler d1 execute hotnews --remote --file schema.sql
npm run setup
```

### （可选）绑定自己的域名

你有任意域名托管在 Cloudflare 时，在控制台 Worker → Settings → Domains & Routes 添加，自动配 HTTPS。

---

## 四、费用与额度

免费计划（Workers Free）对本项目绰绰有余：

| 资源 | 本项目消耗 | 免费限额 |
|---|---|---|
| 请求数 | ≈ 1 万次/天 | 100,000 次/天 |
| Cron 子请求 | 每轮 ≤ 40 | 50 次/轮 |
| KV 读/写 | ≈ 访问量 / 480 次每天 | 100,000 读 / 1,000 写每天 |

日 PV 超过 ~2 万再考虑 $5/月 付费计划。

---

## 五、常用控制台入口

登录 https://dash.cloudflare.com 后：

| 功能 | 位置 |
|---|---|
| Worker 概览 / 实时日志 / 版本回滚 | Workers & Pages → hotnews |
| Cron 执行历史 | hotnews → Settings → Trigger Events |
| KV 数据查看 | Storage & Databases → KV → hotnews-CACHE |
| 子域名管理 | Workers & Pages → Subdomain |
| D1 数据库（若启用） | Storage & Databases → D1 → hotnews |

---

## 六、故障排查速查

| 症状 | 原因 / 处理 |
|---|---|
| 打开是 Cloudflare 错误页 | `npm run setup` 看部署报错；`npx wrangler tail` 看实时日志 |
| 某平台一直「不可用」 | 目标站点改版或被 WAF 拦：本地跑 `node test/fetchers.test.mjs` 定位，改 `src/fetchers.js` 后重新部署 |
| 数据长时间不更新 | 检查 Cron：控制台 Trigger Events 里应有每 3 分钟的执行记录；`curl /api/health` 看 `detail` |
| 懂球帝云端不可用 | 已知限制：直播吧为 GBK 编码，Workers 运行时不支持（本地 Python 版正常） |
| V2EX 云端不可用 | 检查 `npx wrangler tail`；该源需要海外出口，Worker 本身就在海外，一般正常 |

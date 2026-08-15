# 热点聚合 · Cloudflare 部署指南

把热点聚合部署到 Cloudflare Workers：**全球边缘节点、无冷启动、免费额度足够个人使用**（Render 免费层的休眠/冷启动问题彻底解决）。

整个部署约 **5 分钟**。有两种方式，任选其一。

---

## 方式一：网页控制台部署（无需命令行）

### 1. 创建 KV 命名空间（存新闻缓存）

1. 打开 https://dash.cloudflare.com → 左侧 **Storage & Databases** → **KV** → **Create namespace**
2. 名称填 `hotnews-cache`，创建后**复制 Namespace ID**（一串 32 位十六进制）

### 2. 部署 Worker 代码

1. 左侧 **Compute (Workers)** → **Create** → **Create Worker**
2. 名称填 `hotnews` → **Deploy**（先用默认代码占位）
3. 点 **Edit code（编辑代码）**，把本目录 `src/index.js`、`src/fetchers.js`、`src/registry.js` 三个文件的内容分别粘贴为三个模块文件（左侧文件树 `+ Add module file`）
4. 点右上 **Deploy**
5. **设置 → Bindings → Add**：
   - `KV Namespace`：变量名填 `CACHE`，选择第 1 步创建的 `hotnews-cache`
   - `Assets`：变量名 `ASSETS`（见下）
6. **设置 → Trigger Events（触发事件）→ Cron Triggers** 添加：`*/3 * * * *`（每 3 分钟预热缓存）

> 静态首页：控制台方式略麻烦——建议直接用方式二（一行命令搞定代码+静态页+绑定）。
> 若坚持控制台：把 `public/index.html` 的内容粘贴到 Worker 里新增一个路由，或用 Cloudflare Pages 托管静态页后把 API 请求指向 Worker 域名。

### 3. （可选）启用账号/收藏功能（D1 数据库）

1. **Storage & Databases → D1 → Create database**，名称 `hotnews`，复制 Database ID
2. 在 D1 控制台 **Console** 标签页粘贴运行 `schema.sql` 的全部 SQL
3. Worker **设置 → Bindings → Add → D1 Database**：变量名 `DB`，选择 `hotnews`

不配置 D1 也能用：所有新闻功能正常，仅「登录/收藏/历史」会提示未启用。

---

## 方式二：命令行部署（推荐，一条命令）

### 前置

- 安装 Node.js 18+（https://nodejs.org）
- 注册 Cloudflare 账号

### 步骤

```bash
# 1. 进入目录，安装依赖
cd cloudflare
npm install

# 2. 登录 Cloudflare（会打开浏览器授权）
npx wrangler login

# 3. 创建 KV 命名空间，记下输出里的 id
npx wrangler kv namespace create CACHE
# 输出示例: id = "abcd1234...ef"

# 4. 把 id 填进 wrangler.jsonc 的 kv_namespaces → "id"

# 5. （可选，启用账号/收藏）创建 D1 并建表：
npx wrangler d1 create hotnews
#    把输出的 database_id 填进 wrangler.jsonc（取消 d1_databases 段注释）
npx wrangler d1 execute hotnews --remote --file schema.sql

# 6. 部署！
npm run deploy

# 7.（可选）设置自定义 SECRET_KEY（与本地 Python 版一致则账号互通）
npx wrangler secret put SECRET_KEY
```

部署成功会输出 `https://hotnews.<你的子域>.workers.dev`，打开即可。

> **首次打开稍慢**（边缘缓存为空，现场抓一次数据）。Cron 每 3 分钟预热一批平台，
> 几分钟后所有平台都会命中缓存，全球任何位置访问都是 **<100ms 级响应**。

---

## 本地开发与测试

```bash
cd cloudflare
npm install
npm run dev          # 启动本地 Worker（http://127.0.0.1:8787）
npm test             # 实测全部 40 个平台抓取器（打印成功率）
```

手动触发一次 Cron 预热（本地开发不自动跑 cron）：

```bash
curl "http://127.0.0.1:8787/cdn-cgi/local/scheduled"
```

---

## 免费额度核算（Workers Free 计划）

| 资源 | 本项目消耗 | 免费限额 |
|---|---|---|
| 请求数 | 用户访问 + Cron ≈ 1 万次/天 | 100,000 次/天 |
| 单次子请求 | Cron 每轮 ≤ 40 | 50 次/请求 |
| KV 读 | ≈ 用户批请求数 | 100,000 次/天 |
| KV 写 | ≈ 480 次/天 | 1,000 次/天 |
| D1 | 仅登录用户读写，极低 | 500 万读/天 |

个人使用绰绰有余；若日 PV 超过 2 万再考虑 $5/月 付费计划。

---

## 架构说明

```
用户 ──► Worker（边缘，全球 300+ 节点）
          ├── /api/*  → 路由处理
          │     ├── 请求平台数据 → 内存缓存(60s) → KV（Cron 每 3 分钟预热）
          │     ├── 缓存 miss → 现场抓取（并发≤8）→ 回写 KV
          │     ├── 抓取失败 → 返回 6h 内旧数据（标注「数据延迟」）
          │     └── /api/auth|favorites|history → D1（可选绑定）
          └── 其它路径 → 静态资源 public/index.html（单文件 SPA）

Cron（每 3 分钟）─► 刷新 8 个实时热搜 + 轮换组（其余平台分 4 组）
                    每平台最长约 15 分钟刷新一次，用户请求永远命中缓存
```

- **API 与 Flask 版（app.py）逐字段兼容**，前端同一份 `templates/index.html`
  （`npm run deploy` 会自动从 `../templates/` 同步最新 UI）。
- 认证令牌兼容：HS256 JWT + `sha256(密码+SECRET_KEY)`，两边 `SECRET_KEY` 一致则账号互通。
- 已知云端限制：直播吧（懂球帝槽位）页面为 GBK 编码，Workers 运行时不支持 GBK 解码，
  云端该平台会显示「不可用」（本地 Python 版正常）；V2EX 在大陆本地无法访问，
  但 Worker 从海外边缘节点抓取，云端可用。

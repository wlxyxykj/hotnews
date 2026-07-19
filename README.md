# 热点聚合

实时聚合 **30+ 平台** 的热点榜单，涵盖综合新闻、科技、娱乐、财经、军事、体育六大分类。

**在线体验：** [https://hotnews-top.onrender.com](https://hotnews-top.onrender.com)

### 支持平台

| 分类 | 平台 |
|---|---|
| 综合 | 微博热搜、腾讯新闻、百度热搜、抖音热搜、今日头条、网易新闻、新浪新闻、澎湃新闻、知乎热搜、B站排行、人民日报、央视新闻、新华社 |
| 科技 | 36氪、虎嗅、爱范儿、少数派、IT之家、GitHub趋势 |
| 娱乐 | 豆瓣电影、猫眼电影、微博娱乐、新浪娱乐、凤凰娱乐 |
| 财经 | 财新、第一财经、界面新闻、华尔街见闻、东方财富 |
| 军事国际 | 观察者网、环球时报、参考消息 |
| 体育 | 虎扑、懂球帝、央视体育 |

---

## 快速开始

### 方式一：本地运行（推荐）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动
python app.py

# 3. 浏览器访问 http://127.0.0.1:5000
```

### 方式二：一键启动（Windows）

双击 `start.bat`，自动创建虚拟环境、安装依赖并打开浏览器。

### 方式三：桌面应用（Windows）

`dist/HotNews.exe` 是打包好的桌面应用，双击运行，无需安装 Python。

如需重新打包：

```bash
pip install pyinstaller pywebview
pyinstaller 热点聚合.spec
```

---

## API 文档

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/news/<platform>` | GET | 获取单个平台热点（如 `/api/news/weibo`） |
| `/api/news/batch?category=综合` | GET | 批量获取某分类下所有平台热点 |
| `/api/categories` | GET | 获取所有分类及包含的平台列表 |
| `/api/platforms` | GET | 获取所有平台名称和状态 |
| `/api/health` | GET | 健康检查，返回各平台缓存命中率 |
| `/api/refresh` | POST | 手动触发全平台数据刷新（异步） |
| `/api/ping` | GET | 服务存活检测 |

### 用户功能

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/auth/register` | POST | 注册（`{"username":"...","password":"..."}`） |
| `/api/auth/login` | POST | 登录，返回 JWT token |
| `/api/auth/me` | GET | 获取当前用户信息（需 Bearer token） |
| `/api/favorites` | GET/POST | 查看/添加收藏 |
| `/api/favorites/<id>` | DELETE | 删除收藏 |
| `/api/history` | GET/POST/DELETE | 查看/记录/清空浏览历史 |

---

## 部署

### Render（免费，推荐）

1. Fork 本仓库到 GitHub
2. 在 [render.com](https://render.com) 新建 Web Service，选择 Python 环境
3. 配置：
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 90`
4. 部署完成后获得 `https://xxx.onrender.com` 永久地址

> 免费版 30 分钟无访问会自动休眠，首次访问需等待冷启动（约 10 秒）。

### 阿里云 ECS（Linux）

```bash
# 将项目上传至 /opt/hotnews，然后执行：
sudo bash deploy/setup.sh
```

脚本会自动完成：系统依赖安装 → Python 虚拟环境 → systemd 服务 → Nginx 反向代理 → HTTPS 证书。

---

## 配置

通过环境变量自定义：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `5000` | 服务端口 |
| `SECRET_KEY` | 内置默认值 | JWT 签名密钥（生产环境务必修改） |
| `DB_PATH` | `hotnews.db` | SQLite 数据库路径 |
| `CACHE_TTL` | `180` | 缓存有效期（秒） |
| `SCRAPE_PROXY` | 无 | 爬虫代理（如 `http://127.0.0.1:7890`） |

---

## 技术栈

- **后端**: Flask + SQLite
- **爬虫**: requests + BeautifulSoup + lxml
- **前端**: 原生 HTML/CSS/JS（SPA，无框架）
- **桌面端**: pywebview + PyInstaller
- **部署**: Gunicorn + Nginx

## 项目结构

```
hotnews/
├── app.py              # Flask 后端 + 30+ 平台爬虫
├── desktop.py          # 桌面应用启动器
├── templates/
│   └── index.html      # 前端 SPA 页面
├── requirements.txt    # Python 依赖
├── start.bat           # Windows 一键启动脚本
├── Procfile            # Heroku/Render 进程配置
├── deploy/
│   ├── setup.sh        # ECS 一键部署脚本
│   ├── hotnews.service # systemd 服务定义
│   └── nginx-hotnews.conf  # Nginx 配置
├── dist/
│   └── HotNews.exe     # 打包好的 Windows 桌面应用
└── 热点聚合.spec        # PyInstaller 打包配置
```

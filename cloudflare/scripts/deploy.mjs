// 一键部署脚本：创建 KV 缓存 → 自动把 id 填进 wrangler.jsonc → 同步前端 → 部署
// 用法：npm run setup
// 前置：已注册 Cloudflare 账号，且已执行过 npx wrangler login
import { execSync } from "node:child_process";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = dirname(dirname(fileURLToPath(import.meta.url))); // cloudflare/
const cfgPath = join(root, "wrangler.jsonc");
const PLACEHOLDER = "REPLACE_WITH_YOUR_KV_NAMESPACE_ID";

const log = (s) => console.log(s);
const run = (cmd) => execSync(cmd, { cwd: root, stdio: ["inherit", "pipe", "inherit"] }).toString();

// 0) 登录检查
log("① 检查 Cloudflare 登录状态…");
let whoami = "";
try {
  whoami = run("npx wrangler whoami");
} catch {
  log("\n❌ 尚未登录。请先执行下面命令，浏览器里点「Allow」授权后重试：\n\n    npx wrangler login\n");
  process.exit(1);
}
const email = (whoami.match(/[\w.+-]+@[\w.-]+/) || [""])[0];
if (!email) {
  log("\n❌ 未能获取账号信息。请先执行：npx wrangler login\n");
  process.exit(1);
}
log(`   已登录：${email}`);

// 1) 创建 KV（仅当配置里还是占位符时）
let cfg = fs.readFileSync(cfgPath, "utf8");
if (cfg.includes(PLACEHOLDER)) {
  log("\n② 创建 KV 缓存库（首次运行）…");
  let out = "";
  try {
    out = run("npx wrangler kv namespace create CACHE");
  } catch {
    log("\n❌ 创建 KV 失败，请把上面的报错发给开发者。也可以手动创建：\n");
    log("   npx wrangler kv namespace create CACHE\n");
    log("   然后把输出里的 id 填进 wrangler.jsonc 替换 " + PLACEHOLDER + "\n");
    process.exit(1);
  }
  const id = (out.match(/[0-9a-f]{32}/i) || [])[0];
  if (!id) {
    log("\n⚠️  未能自动解析 namespace id，请手动操作：");
    log("   1. 执行 npx wrangler kv namespace list 查看列表");
    log("   2. 把对应 title 的 id 填进 wrangler.jsonc 替换 " + PLACEHOLDER + "\n");
    process.exit(1);
  }
  cfg = cfg.replace(PLACEHOLDER, id);
  fs.writeFileSync(cfgPath, cfg);
  log(`   ✅ 已创建并填入 id: ${id}`);
} else {
  log("\n② KV 已配置，跳过创建");
}

// 2) 同步最新前端 + 静态资源（默认背景图 / robots / sitemap）
fs.copyFileSync(join(root, "..", "templates", "index.html"), join(root, "public", "index.html"));
fs.cpSync(join(root, "..", "static"), join(root, "public", "static"), { recursive: true });
fs.copyFileSync(join(root, "..", "static", "robots.txt"), join(root, "public", "robots.txt"));
fs.copyFileSync(join(root, "..", "static", "sitemap.xml"), join(root, "public", "sitemap.xml"));
log("\n③ 已同步最新前端页面和静态资源");

// 3) 部署
log("\n④ 部署到 Cloudflare 边缘网络…\n");
try {
  execSync("npx wrangler deploy", { cwd: root, stdio: "inherit" });
} catch {
  log("\n❌ 部署失败。常见原因：");
  log("   - wrangler.jsonc 里的 KV id 未替换 → 重跑本脚本");
  log("   - 账号未验证邮箱 → 去 dash.cloudflare.com 完成验证\n");
  process.exit(1);
}

log("\n🎉 部署完成！首次打开稍慢（边缘缓存冷启动会现场抓一次数据），");
log("   Cron 每 3 分钟自动预热，几分钟后全球访问都是毫秒级响应。");
log("   提示：Cron 触发器已写入配置，无需手动设置。\n");
log("   可选：启用账号/收藏功能（D1 数据库）见 cloudflare/README.md；");
log("   修改通信密钥：npx wrangler secret put SECRET_KEY\n");

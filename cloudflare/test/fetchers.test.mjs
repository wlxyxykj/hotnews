// 全平台抓取器实测（Node 18+ 原生运行，与 workerd 同款 fetch）
// 用法：node cloudflare/test/fetchers.test.mjs [--quick]
import { FETCHERS } from "../src/fetchers.js";

const quick = process.argv.includes("--quick");
const QUICK_SET = ["weibo", "tencent", "toutiao", "baidu", "douyin", "tieba", "zhihu", "bilibili", "juejin", "oschina", "tmtpost", "pengpai", "huxiu"];
const pids = quick ? QUICK_SET : Object.keys(FETCHERS);

// 已知本地网络不可达（V2EX 大陆被墙；GBK 需环境支持）——失败时不计入失败率
const LOCAL_KNOWN_ISSUES = new Set(["v2ex"]);

const results = [];
const t0 = Date.now();

async function runOne(pid) {
  const start = Date.now();
  try {
    const r = await FETCHERS[pid]();
    const items = (r && r.items) || [];
    results.push({
      pid,
      ok: r.status === "success" && items.length >= 3,
      count: items.length,
      realtime: r.is_realtime,
      note: r.update_note || "",
      sample: items[0] ? String(items[0].title).slice(0, 38) : "",
      ms: Date.now() - start,
    });
  } catch (e) {
    results.push({ pid, ok: false, count: 0, note: `EXC ${String(e.message || e).slice(0, 80)}`, sample: "", ms: Date.now() - start });
  }
}

// 分组并发（每组 6，避免瞬时打满连接）
for (let i = 0; i < pids.length; i += 6) {
  await Promise.all(pids.slice(i, i + 6).map(runOne));
}

results.sort((a, b) => Number(b.ok) - Number(a.ok) || a.pid.localeCompare(b.pid));
for (const r of results) {
  console.log(
    `${r.ok ? "✅" : "❌"} ${r.pid.padEnd(13)} ${String(r.count).padStart(2)}条 ${String(r.ms).padStart(5)}ms  ${r.ok ? (r.sample || "").padEnd(40) : ""} ${r.ok ? (r.realtime ? "⚡" : "🕒") : r.note}`
  );
}
const real = results.filter((r) => !LOCAL_KNOWN_ISSUES.has(r.pid));
const pass = real.filter((r) => r.ok).length;
console.log(`\n总计 ${results.length} 个平台：成功 ${pass}/${real.length}（${Math.round((pass / real.length) * 100)}%）· 耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);
if (pass / real.length < 0.85) {
  console.log("❌ 成功率低于 85%，请检查失败平台");
  process.exit(1);
}
console.log("✅ 成功率达标（≥85%）");

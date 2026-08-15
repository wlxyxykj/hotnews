// 热点聚合 · Cloudflare Worker 入口
// 职责：/api/* 路由 + KV 缓存（Cron 每 3 分钟预热）+ D1 账号/收藏 + 静态资源
// API 响应结构与 Flask 版（app.py）逐字段兼容

import { FETCHERS } from "./fetchers.js";
import { PLATFORM_NAMES, CATEGORIES, REALTIME_CORE, ROTATION_GROUPS, ALL_PLATFORMS } from "./registry.js";

const KV_KEY = "all";
const STALE_TTL = 6 * 3600 * 1000;   // 失败时允许返回 6h 内的旧数据（标注"数据延迟"）
const MEM_TTL = 60 * 1000;           // isolate 内存缓存，减少 KV 读

const jsonHeaders = {
  "Content-Type": "application/json; charset=utf-8",
  "Access-Control-Allow-Origin": "*",
  "Cache-Control": "no-store",
};
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: jsonHeaders });
const nowStr = () => new Date(Date.now() + 8 * 3600e3).toISOString().slice(11, 16);
const nowFull = () => new Date(Date.now() + 8 * 3600e3).toISOString().replace("T", " ").slice(0, 19);

// ── 缓存 Blob（KV 单键 + isolate 内存）──────────────────
let _mem = { blob: null, ts: 0 };

async function loadBlob(env, { skipMem = false } = {}) {
  if (!skipMem && _mem.blob && Date.now() - _mem.ts < MEM_TTL) return _mem.blob;
  let blob = null;
  if (env.CACHE) {
    try {
      const raw = await env.CACHE.get(KV_KEY);
      if (raw) blob = JSON.parse(raw);
    } catch { /* KV 异常时当空处理 */ }
  }
  blob = normalizeBlob(blob);
  _mem = { blob, ts: Date.now() };
  return blob;
}

async function saveBlob(env, blob) {
  _mem = { blob, ts: Date.now() };
  if (env.CACHE) {
    try { await env.CACHE.put(KV_KEY, JSON.stringify(blob)); } catch { /* 写失败不影响响应 */ }
  }
}

function normalizeBlob(b) {
  if (!b || typeof b !== "object" || !b.platforms) return { v: 3, platforms: {}, ptr: 0 };
  return b;
}

// ── 单平台刷新（含失败→旧数据保底）─────────────────────
async function refreshOne(pid, prev) {
  const fn = FETCHERS[pid];
  let res = null;
  if (fn) {
    try { res = await fn(); } catch { res = null; }
  }
  const good = res && res.status === "success" && Array.isArray(res.items) && res.items.length;
  if (good) return { data: res, ok: true, ts: Date.now() };

  if (prev && prev.ok && prev.data && prev.data.items && Date.now() - prev.ts < STALE_TTL) {
    return {
      data: {
        ...prev.data,
        is_realtime: false,
        update_note: `上次成功 ${prev.data.fetched_at} · 数据延迟`,
      },
      ok: true, stale: true, ts: prev.ts,
    };
  }
  return { data: (res && res.status === "failed" && res.items ? res : { status: "failed", items: [], is_realtime: false, fetched_at: nowStr(), update_note: "抓取失败" }), ok: false, ts: Date.now() };
}

// 简单并发池
async function mapLimit(arr, limit, fn) {
  const out = new Array(arr.length);
  let i = 0;
  const workers = Array.from({ length: Math.min(limit, arr.length) }, async () => {
    while (i < arr.length) {
      const idx = i++;
      out[idx] = await fn(arr[idx], idx);
    }
  });
  await Promise.all(workers);
  return out;
}

async function refreshInto(blob, pids, concurrency = 6) {
  await mapLimit(pids, concurrency, async (pid) => {
    blob.platforms[pid] = await refreshOne(pid, blob.platforms[pid]);
  });
}

// ── Cron：每轮刷实时核心 + 一个轮换组 ───────────────────
async function cronRefresh(env) {
  const blob = await loadBlob(env, { skipMem: true });
  const group = ROTATION_GROUPS[blob.ptr % ROTATION_GROUPS.length] || [];
  await refreshInto(blob, [...REALTIME_CORE, ...group]);
  blob.ptr = (blob.ptr + 1) % ROTATION_GROUPS.length;
  await saveBlob(env, blob);
  // 顺带保活 Render 免费实例（15 分钟无流量会休眠，冷启动约 30 秒）。
  // 每 3 分钟 ping 一次即可常驻；设 KEEPALIVE_URL="" 可关闭，或指向其它需保活的地址。
  const keepalive = env.KEEPALIVE_URL !== undefined ? env.KEEPALIVE_URL : "https://hotnews-top.onrender.com/api/ping";
  if (keepalive) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 30000);
      const r = await fetch(keepalive, { signal: ctrl.signal });
      clearTimeout(timer);
      console.log(`keepalive: ${keepalive} → HTTP ${r.status}`);
    } catch (e) {
      console.log(`keepalive: ${keepalive} → ${String(e && e.message || e).slice(0, 80)}`);
    }
  }
}

// ── 请求期取数：优先缓存，缺失/强制时现场抓 ─────────────
async function resolvePlatforms(env, ctx, ids, force) {
  const blob = await loadBlob(env);
  const results = {};
  const need = [];
  for (const pid of ids) {
    const entry = blob.platforms[pid];
    if (!force && entry && entry.data) results[pid] = entry.data;
    else need.push(pid);
  }
  if (need.length) {
    await refreshInto(blob, need, 8);
    for (const pid of need) results[pid] = blob.platforms[pid] ? blob.platforms[pid].data : { status: "failed", items: [], is_realtime: false, fetched_at: nowStr(), update_note: "未知平台" };
    ctx.waitUntil(saveBlob(env, blob));  // 与 cron 可能竞争，丢一次更新无碍（下轮补上）
  }
  return { results, blob };
}

// ══════════════════════════════════════════════════════
// 认证（HS256 JWT + SHA-256 口令散列，与 Flask 版互通）
// ══════════════════════════════════════════════════════
const te = (s) => new TextEncoder().encode(s);
const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const b64urlJson = (obj) => b64url(te(JSON.stringify(obj)));

async function hmacSign(input, secret) {
  const key = await crypto.subtle.importKey("raw", te(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, te(input));
  return b64url(sig);
}
async function sha256Hex(s) {
  const d = await crypto.subtle.digest("SHA-256", te(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
async function makeToken(env, userId, username) {
  const secret = (env.SECRET_KEY) || "hotnews-secret-2024-xK9mP";
  const header = b64urlJson({ alg: "HS256", typ: "JWT" });
  const payload = b64urlJson({ user_id: userId, username, exp: Math.floor(Date.now() / 1000) + 86400 * 30 });
  return `${header}.${payload}.${await hmacSign(`${header}.${payload}`, secret)}`;
}
async function verifyToken(env, token) {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const secret = (env.SECRET_KEY) || "hotnews-secret-2024-xK9mP";
  const expect = await hmacSign(`${parts[0]}.${parts[1]}`, secret);
  if (expect !== parts[2]) return null;
  try {
    const payload = JSON.parse(atob(parts[1].replace(/-/g, "+").replace(/_/g, "/")));
    if ((payload.exp || 0) * 1000 < Date.now()) return null;
    return payload;
  } catch { return null; }
}
async function currentUser(env, request) {
  const auth = request.headers.get("Authorization") || "";
  if (auth.startsWith("Bearer ")) return verifyToken(env, auth.slice(7));
  const url = new URL(request.url);
  return verifyToken(env, request.headers.get("Cookie")?.match(/token=([^;]+)/)?.[1] || url.searchParams.get("token"));
}
const hashPassword = async (env, pw) => sha256Hex(pw + ((env.SECRET_KEY) || "hotnews-secret-2024-xK9mP"));

// ══════════════════════════════════════════════════════
// 路由
// ══════════════════════════════════════════════════════
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS" } });
    if (!path.startsWith("/api/")) {
      return env.ASSETS ? env.ASSETS.fetch(request) : json({ error: "no assets" }, 500);
    }

    try {
      if (path === "/api/news/batch") return await handleBatch(request, env, ctx, url);
      if (path.startsWith("/api/news/")) return await handleSingle(path.slice("/api/news/".length), env, ctx);
      if (path === "/api/categories") return json(CATEGORIES);
      if (path === "/api/platforms") return json(PLATFORM_NAMES);
      if (path === "/api/ping") return json({ ok: true, ts: nowStr(), server_time: nowFull() });
      if (path === "/api/health") return await handleHealth(env);
      if (path === "/api/refresh") return await handleRefresh(env, ctx, url);
      if (path.startsWith("/api/auth/")) return await handleAuth(path, request, env);
      if (path.startsWith("/api/favorites")) return await handleFavorites(path, request, env);
      if (path.startsWith("/api/history")) return await handleHistory(path, request, env);
      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: String((e && e.message) || e).slice(0, 200) }, 500);
    }
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(cronRefresh(env));
  },
};

async function handleBatch(request, env, ctx, url) {
  const category = url.searchParams.get("category") || "";
  const platformsParam = url.searchParams.get("platforms") || "";
  const force = url.searchParams.get("refresh") === "1";

  let ids;
  if (category && CATEGORIES[category]) ids = CATEGORIES[category];
  else if (platformsParam) ids = platformsParam.split(",").map((s) => s.trim()).filter((p) => FETCHERS[p]);
  else ids = ALL_PLATFORMS;

  const { results } = await resolvePlatforms(env, ctx, ids, force);

  const failed = [];
  for (const pid of ids) {
    const d = results[pid];
    if (!d || d.status === "failed" || !d.items || !d.items.length) {
      failed.push({ platform: pid, name: PLATFORM_NAMES[pid] || pid, note: (d && d.update_note) || "抓取失败" });
    }
  }
  return json({
    ...results,
    _meta: {
      failed,
      failed_count: failed.length,
      total_count: ids.length,
      cached: !force,
      last_refresh: nowStr(),
    },
  });
}

async function handleSingle(pid, env, ctx) {
  if (!FETCHERS[pid]) return json({ error: `unknown platform: ${pid}` }, 404);
  const { results } = await resolvePlatforms(env, ctx, [pid], false);
  return json({ platform: pid, ...results[pid] });
}

async function handleHealth(env) {
  const blob = await loadBlob(env);
  const detail = {};
  let freshOk = 0, staleOk = 0, failed = 0, uncached = 0;
  const now = Date.now();
  for (const pid of ALL_PLATFORMS) {
    const e = blob.platforms[pid];
    if (!e) { detail[pid] = "uncached"; uncached++; continue; }
    const goodData = e.ok && e.data && e.data.items && e.data.items.length;
    if (!goodData) { detail[pid] = "failed"; failed++; continue; }
    const age = Math.floor((now - e.ts) / 1000);
    if (e.stale) { detail[pid] = `stale(${Math.floor(age / 60)}m)`; staleOk++; }
    else if (age < 900) { detail[pid] = `fresh(${age}s)`; freshOk++; }
    else { detail[pid] = `stale(${Math.floor(age / 60)}m)`; staleOk++; }
  }
  return json({
    ok: true,
    platforms_total: ALL_PLATFORMS.length,
    fresh_ok: freshOk,
    stale_ok: staleOk,
    failed,
    uncached,
    ptr: blob.ptr,
    server_time: nowFull(),
    detail,
  });
}

async function handleRefresh(env, ctx, url) {
  const category = url.searchParams.get("category") || "";
  // 手动刷新 = 实时核心 + 当前轮换组（控制单次子请求 < 50）
  const blob = await loadBlob(env);
  let pids = [...REALTIME_CORE, ...(ROTATION_GROUPS[blob.ptr % ROTATION_GROUPS.length] || [])];
  if (category && CATEGORIES[category]) pids = CATEGORIES[category];
  ctx.waitUntil((async () => {
    const b = await loadBlob(env, { skipMem: true });
    await refreshInto(b, pids);
    await saveBlob(env, b);
  })());
  return json({ ok: true, refreshing: pids.length, ts: nowStr() });
}

// ── 认证 ──────────────────────────────────────────────
async function handleAuth(path, request, env) {
  if (!env.DB) return json({ error: "云端版未启用账号功能：请按部署文档绑定 D1 数据库" }, 501);
  if (path === "/api/auth/register" && request.method === "POST") {
    const data = await request.json().catch(() => ({}));
    const username = String(data.username || "").trim();
    const password = String(data.password || "");
    if (!username || !password) return json({ error: "用户名和密码不能为空" }, 400);
    if (username.length < 2 || username.length > 20) return json({ error: "用户名长度2-20" }, 400);
    if (password.length < 6) return json({ error: "密码至少6位" }, 400);
    const hash = await hashPassword(env, password);
    try {
      const r = await env.DB.prepare("INSERT INTO users (username, password_hash) VALUES (?, ?) RETURNING id").bind(username, hash).first();
      return json({ token: await makeToken(env, r.id, username), username, user_id: r.id });
    } catch (e) {
      if (String(e.message).includes("UNIQUE")) return json({ error: "用户名已存在" }, 400);
      throw e;
    }
  }
  if (path === "/api/auth/login" && request.method === "POST") {
    const data = await request.json().catch(() => ({}));
    const username = String(data.username || "").trim();
    const password = String(data.password || "");
    const row = await env.DB.prepare("SELECT id, password_hash FROM users WHERE username = ?").bind(username).first();
    const hash = await hashPassword(env, password);
    if (!row || row.password_hash !== hash) return json({ error: "用户名或密码错误" }, 401);
    return json({ token: await makeToken(env, row.id, username), username, user_id: row.id });
  }
  if (path === "/api/auth/me") {
    const user = await currentUser(env, request);
    if (!user) return json({ error: "未登录" }, 401);
    return json({ username: user.username, user_id: user.user_id });
  }
  return json({ error: "not found" }, 404);
}

// ── 收藏 ──────────────────────────────────────────────
async function handleFavorites(path, request, env) {
  if (!env.DB) return json({ error: "云端版未启用账号功能：请按部署文档绑定 D1 数据库" }, 501);
  const user = await currentUser(env, request);
  if (!user) return json({ error: "未登录" }, 401);

  if (request.method === "GET") {
    const { results } = await env.DB.prepare(
      "SELECT id, title, url, platform, saved_at FROM favorites WHERE user_id = ? ORDER BY saved_at DESC LIMIT 200"
    ).bind(user.user_id).all();
    return json(results || []);
  }
  if (request.method === "POST") {
    const data = await request.json().catch(() => ({}));
    const title = String(data.title || "").trim();
    const link = String(data.url || "").trim();
    if (!title || !link) return json({ error: "参数不完整" }, 400);
    await env.DB.prepare("INSERT OR IGNORE INTO favorites (user_id, title, url, platform) VALUES (?, ?, ?, ?)")
      .bind(user.user_id, title, link, String(data.platform || "")).run();
    return json({ ok: true });
  }
  if (request.method === "DELETE") {
    const fid = Number(path.split("/").pop());
    if (Number.isFinite(fid)) {
      await env.DB.prepare("DELETE FROM favorites WHERE id = ? AND user_id = ?").bind(fid, user.user_id).run();
      return json({ ok: true });
    }
  }
  return json({ error: "not found" }, 404);
}

// ── 浏览历史 ──────────────────────────────────────────
async function handleHistory(path, request, env) {
  if (!env.DB) return json({ error: "云端版未启用账号功能：请按部署文档绑定 D1 数据库" }, 501);
  const user = await currentUser(env, request);
  if (!user) return json({ error: "未登录" }, 401);

  if (request.method === "GET") {
    const { results } = await env.DB.prepare(
      "SELECT id, title, url, platform, viewed_at FROM history WHERE user_id = ? ORDER BY viewed_at DESC LIMIT 200"
    ).bind(user.user_id).all();
    return json(results || []);
  }
  if (request.method === "POST") {
    const data = await request.json().catch(() => ({}));
    const title = String(data.title || "").trim();
    const link = String(data.url || "").trim();
    if (!title || !link) return json({ ok: false });
    await env.DB.batch([
      env.DB.prepare("INSERT INTO history (user_id, title, url, platform) VALUES (?, ?, ?, ?)")
        .bind(user.user_id, title, link, String(data.platform || "")),
      env.DB.prepare(`DELETE FROM history WHERE user_id = ? AND id NOT IN (
        SELECT id FROM history WHERE user_id = ? ORDER BY viewed_at DESC LIMIT 500
      )`).bind(user.user_id, user.user_id),
    ]);
    return json({ ok: true });
  }
  if (request.method === "DELETE") {
    await env.DB.prepare("DELETE FROM history WHERE user_id = ?").bind(user.user_id).run();
    return json({ ok: true });
  }
  return json({ error: "not found" }, 404);
}

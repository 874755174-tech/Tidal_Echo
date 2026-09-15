/* 设置页「模型 / effort / 上下文阈值」—— 前端（web/index.html）实测
 * ============================================================================
 *
 * 为什么需要这个脚本
 * ------------------
 * 后端那侧已经有 131 项验收（tools/providers_check.py），但那只证明**接口对**。
 * 2026-09-15 这轮把设置页三个**假按键**接成了真的，逻辑全在前端：
 *
 *   1. 模型按钮必须是**从允许列表渲染**的（原来那 4 个是硬编码的假按键）
 *   2. 选中 = 真的 `PUT /app/ext/settings`；**失败要回滚**，不能留下假的选中态
 *   3. 拉不到允许列表就**明说拉不到**（绝不 paint 假状态 —— 这是这张卡片的底线）
 *   4. effort / 上下文阈值要**跟着库走**（不是跟着 localStorage）
 *
 * 这三条**只有把 index.html 真跑起来**才验得到。做法同 session_ui_check.mjs：
 *   · 用 jsdom 载入真实 index.html（不 mock DOM，mock 的是网络）
 *   · 造一个假 fetch，按路径返回后端应该给的数据，并记录每次调用
 *   · 直接调页面的函数 / 点 DOM 节点，断言界面与请求
 *
 * 跑法：
 *   NODE_PATH=<workspace>/node_modules node tools/model_ui_check.mjs
 * 结果写 tools/model_ui_report.txt
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// jsdom 装在 WorkBuddy 托管 Node 的隔离 workspace 里（不污染用户环境）。
// ESM 的 import 解析**不吃 NODE_PATH**，所以这里用绝对路径动态 import。
const JSDOM_HOME =
  process.env.JSDOM_HOME ||
  "C:/Users/86187/.workbuddy/binaries/node/workspace/node_modules/jsdom/lib/api.js";
const jsdomMod = await import(pathToFileURL(JSDOM_HOME).href);
const { JSDOM, VirtualConsole } = jsdomMod;

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.dirname(HERE);
const HTML_PATH = path.join(REPO, "web", "index.html");

const results = [];
function chk(name, ok, detail = "") {
  results.push([name, !!ok, detail]);
}

// ── 假后端 ──────────────────────────────────────────────────────────────────
const calls = [];                 // "METHOD /url"（用于断言"发了没发"）
const putBodies = [];             // 每次 PUT /app/ext/settings 的 body

let providersState = null;        // null = 让 /app/ext/providers 失败一次
let settingsState = {
  provider_id: null, model_id: null,
  effort: null, context_keep: null, context_trigger: null,
};
let putShouldFail = false;

function jsonResponse(obj, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => obj,
    text: async () => JSON.stringify(obj),
  };
}

const OK_CATALOG = {
  ok: true,
  default_provider: "relay",
  selected: { provider_id: null, model_id: null },
  providers: [
    { id: "deepseek", label: "DeepSeek", format: "openai", models: ["deepseek-chat"],
      default_model: "deepseek-chat", available: false, key_masked: null,
      needs_base: false, note: "" },
    { id: "relay", label: "中转站", format: "openai",
      models: ["claude-opus-4-6", "claude-opus-4-6-thinking", "claude-sonnet-4-6"],
      default_model: "claude-opus-4-6", available: true, key_masked: "sk-re…3333",
      needs_base: false, note: "" },
  ],
};

const fakeFetch = async (url, opts = {}) => {
  const u = String(url);
  const method = (opts.method || "GET").toUpperCase();
  calls.push(`${method} ${u}`);

  // ⚠️ probe 必须排在 providers 前面匹配（它的路径包含 /app/ext/providers）
  if (u.includes("/app/ext/providers/probe")) {
    return jsonResponse({ ok: true, provider_id: "relay", model: "claude-opus-4-6",
                          ms: 321, sample: "pong", usage: {} });
  }
  if (u.includes("/app/ext/providers")) {
    if (!providersState) return jsonResponse({ error: "boom" }, 500);
    return jsonResponse(providersState);
  }
  if (u.includes("/app/ext/settings")) {
    if (method === "PUT") {
      const body = JSON.parse(opts.body || "{}");
      putBodies.push(body);
      if (putShouldFail) {
        return jsonResponse({ ok: false, reason: "invalid",
                              detail: "model_id 'x' 不在 relay 的允许列表里" }, 400);
      }
      Object.assign(settingsState, body);
      return jsonResponse({ ok: true, changed: Object.keys(body), settings: { ...settingsState } });
    }
    return jsonResponse({ ...settingsState, extra: {}, source: "db" });
  }
  if (u.includes("/app/sessions/manage")) {
    return jsonResponse({ active_session: "sess-A", sessions: [{ id: "sess-A", title: "A", count: 1 }] });
  }
  if (u.includes("/app/sessions")) return jsonResponse({ active_session: "", sessions: [] });
  if (u.includes("/app/history")) return jsonResponse({ messages: [] });
  return jsonResponse({});
};

// ── 载入真实 index.html ─────────────────────────────────────────────────────
const html = fs.readFileSync(HTML_PATH, "utf-8");
const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "http://localhost/chat/",
  virtualConsole: new VirtualConsole(),
});
const { window } = dom;
window.fetch = fakeFetch;
window.matchMedia =
  window.matchMedia ||
  (() => ({ matches: false, addListener() {}, removeListener() {},
            addEventListener() {}, removeEventListener() {} }));
window.localStorage.setItem("companion_secret", "test-secret");

await new Promise((r) => setTimeout(r, 300));

const $ = (sel) => window.document.querySelector(sel);
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// 页面用模块内变量存 secret；灌进去（并确认 USE_MOCK 是 false）
window.eval('try { secret = "test-secret"; } catch(e) {}');
chk("页面已进入「已登录 + 非 mock」状态",
  window.eval("typeof secret") === "string" && window.eval("USE_MOCK") === false,
  `secret=${window.eval("typeof secret")} USE_MOCK=${window.eval("USE_MOCK")}`);

// ══════════════════════════════════════════════════════════════════════════════
// 1. 拉不到允许列表时 —— 必须"说拉不到"，绝不 paint 假状态
// ══════════════════════════════════════════════════════════════════════════════
providersState = null;                       // 让这次拉取 500
await window.eval("refreshModelCard(true)");
await delay(120);

chk("🔴 拉不到允许列表 → 供应商区是空的（不是硬编码假按钮）",
  ($("#modelProvSeg")?.querySelectorAll("button").length || 0) === 0,
  `按钮数=${$("#modelProvSeg")?.querySelectorAll("button").length}`);
chk("🔴 拉不到允许列表 → 模型区也是空的",
  ($("#modelSeg")?.querySelectorAll("button").length || 0) === 0, "");
chk("🔴 拉不到允许列表 → 提示里明说「读不到」",
  ($("#modelHint")?.textContent || "").includes("读不到"),
  $("#modelHint")?.textContent);
chk("拉不到时提示标成 bad（会被染成警示色）",
  $("#modelHint")?.dataset.kind === "bad", $("#modelHint")?.dataset.kind);
chk("🔴 界面里没有任何硬编码的假模型名（Opus 4.7 / Fable 5 这类）",
  !/Opus 4\.[678]|Fable 5/.test($("#modelProvSeg")?.innerHTML + $("#modelSeg")?.innerHTML),
  "");

// ══════════════════════════════════════════════════════════════════════════════
// 2. 拉到了 —— 按允许列表渲染，不可用的供应商 disabled
// ══════════════════════════════════════════════════════════════════════════════
providersState = OK_CATALOG;
await window.eval("refreshModelCard(true)");
await delay(120);

const provBtns = Array.from($("#modelProvSeg")?.querySelectorAll("button") || []);
chk("供应商按钮来自允许列表（2 个）", provBtns.length === 2,
  provBtns.map((b) => b.dataset.val).join(","));
chk("选中项落在服务端给的默认供应商（relay）",
  provBtns.filter((b) => b.classList.contains("active")).map((b) => b.dataset.val).join(",") === "relay",
  provBtns.map((b) => `${b.dataset.val}${b.classList.contains("active") ? "*" : ""}`).join(","));
const deepseekBtn = provBtns.find((b) => b.dataset.val === "deepseek");
chk("🔴 不可用的供应商（没配 key）按钮 disabled", !!deepseekBtn && deepseekBtn.disabled === true,
  String(deepseekBtn?.disabled));
chk("不可用供应商的 title 说明了原因（还没配密钥）",
  (deepseekBtn?.title || "").includes("密钥"), deepseekBtn?.title);

const modelBtns = Array.from($("#modelSeg")?.querySelectorAll("button") || []);
chk("模型按钮列出 relay 的 3 个模型（含 thinking 那个）",
  modelBtns.map((b) => b.dataset.val).join(",")
  === "claude-opus-4-6,claude-opus-4-6-thinking,claude-sonnet-4-6",
  modelBtns.map((b) => b.dataset.val).join(","));
chk("模型默认选中 service 给的 default_model",
  modelBtns.filter((b) => b.classList.contains("active")).map((b) => b.dataset.val).join(",")
  === "claude-opus-4-6", "");
chk("提示里报了当前选中（provider / model）",
  ($("#modelHint")?.textContent || "").includes("relay")
  && ($("#modelHint")?.textContent || "").includes("claude-opus-4-6"),
  $("#modelHint")?.textContent);

// ══════════════════════════════════════════════════════════════════════════════
// 3. 点供应商 → 只切界面，**不发 PUT**（避免库里留下一对不匹配的组合）
// ══════════════════════════════════════════════════════════════════════════════
putBodies.length = 0;
window.eval('MODEL_UI.selected = { provider_id: "relay", model_id: "" };');
const beforePuts = putBodies.length;
deepseekBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await delay(80);
chk("🔴 点「不可用」的供应商按钮无效（disabled 挡住）", putBodies.length === beforePuts,
  `PUT 次数=${putBodies.length}`);

// ══════════════════════════════════════════════════════════════════════════════
// 4. 点模型 → 真的 PUT，且 body 带 provider_id + model_id
// ══════════════════════════════════════════════════════════════════════════════
const thinkBtn = Array.from($("#modelSeg")?.querySelectorAll("button") || [])
  .find((b) => b.dataset.val === "claude-opus-4-6-thinking");
chk("找得到 thinking 那个模型按钮", !!thinkBtn, "");
putBodies.length = 0;
thinkBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await delay(150);

chk("🔴 点模型 → 发出了 PUT /app/ext/settings",
  calls.some((c) => c.startsWith("PUT") && c.includes("/app/ext/settings")),
  calls.slice(-3).join(" | "));
chk("🔴 PUT body 同时带 provider_id 与 model_id（成对存，不会留半对）",
  putBodies.length === 1 && putBodies[0].provider_id === "relay"
  && putBodies[0].model_id === "claude-opus-4-6-thinking",
  JSON.stringify(putBodies[0]));
chk("🔴 落库成功后界面把新模型标成选中",
  ($("#modelSeg")?.querySelector("button.active")?.dataset.val) === "claude-opus-4-6-thinking",
  $("#modelSeg")?.querySelector("button.active")?.dataset.val);
chk("🔴 成功后 hint 变成「已存 …」且标成 good",
  ($("#modelHint")?.textContent || "").includes("已存") && $("#modelHint")?.dataset.kind === "good",
  `${$("#modelHint")?.textContent} / ${$("#modelHint")?.dataset.kind}`);

// ══════════════════════════════════════════════════════════════════════════════
// 5. PUT 失败 → 必须回滚，并把原因写在提示上（不许"看着像存了"）
// ══════════════════════════════════════════════════════════════════════════════
putShouldFail = true;
const sonnetBtn = Array.from($("#modelSeg")?.querySelectorAll("button") || [])
  .find((b) => b.dataset.val === "claude-sonnet-4-6");
putBodies.length = 0;
sonnetBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await delay(150);

chk("失败路径确实发了 PUT", putBodies.length === 1, `PUT 次数=${putBodies.length}`);
chk("🔴 PUT 400 → 提示里写出服务端给的原因（不是笼统的「失败」）",
  ($("#modelHint")?.textContent || "").includes("不在 relay 的允许列表里"),
  $("#modelHint")?.textContent);
chk("🔴 失败后提示标成 bad", $("#modelHint")?.dataset.kind === "bad",
  $("#modelHint")?.dataset.kind);
chk("🔴 失败后选中态回滚（仍是上一个成功的 thinking 模型，没留下假的 sonnet 选中）",
  ($("#modelSeg")?.querySelector("button.active")?.dataset.val) === "claude-opus-4-6-thinking",
  $("#modelSeg")?.querySelector("button.active")?.dataset.val);
putShouldFail = false;

// ══════════════════════════════════════════════════════════════════════════════
// 6. 「验证模型名」按钮 → 真发一次 probe
// ══════════════════════════════════════════════════════════════════════════════
calls.length = 0;
$("#modelProbeRow").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await delay(150);
chk("🔴 点「验证模型名」→ POST /app/ext/providers/probe",
  calls.some((c) => c.startsWith("POST") && c.includes("/app/ext/providers/probe")),
  calls.join(" | "));
chk("🔴 探测通过 → hint 说「模型名没问题」",
  ($("#modelHint")?.textContent || "").includes("模型名没问题"), $("#modelHint")?.textContent);
chk("探测结果写到按钮右侧（通了 · Nms）",
  ($("#modelProbeValue")?.textContent || "").includes("通了"),
  $("#modelProbeValue")?.textContent);

// ══════════════════════════════════════════════════════════════════════════════
// 7. effort / 上下文阈值：界面跟着**库**走（不是 localStorage）
// ══════════════════════════════════════════════════════════════════════════════
// 先在 localStorage 里放一个"旧值"，证明界面不会被它带着跑
window.localStorage.setItem("companion_pick_context", "5");
settingsState.effort = "xhigh";
settingsState.context_keep = 120000;
settingsState.context_trigger = 200000;
await window.eval("refreshModelCard(true)");
await delay(150);

const effActive = Array.from($("#effortSeg")?.querySelectorAll("button") || [])
  .filter((b) => b.classList.contains("active")).map((b) => b.dataset.val);
chk("🔴 effort 选中态按库里的值回填（xhigh）", effActive.join(",") === "xhigh", effActive.join(","));
chk("effort 提示写着已存的值", ($("#effortHint")?.textContent || "").includes("xhigh"),
  $("#effortHint")?.textContent);
chk("🔴 上下文滑块按库里的 context_keep 回填（120k → 60%）",
  Math.abs(Number($("#contextSlider")?.value) - 60) < 1.5,
  `slider=${$("#contextSlider")?.value}（localStorage 里放的是 5，没被它带跑）`);
chk("滑块左侧标签跟着算出「保留120k」",
  ($("#contextKeepLabel")?.textContent || "").includes("120k"),
  $("#contextKeepLabel")?.textContent);

// 点 effort → 落库
putBodies.length = 0;
const lowBtn = Array.from($("#effortSeg")?.querySelectorAll("button") || [])
  .find((b) => b.dataset.val === "low");
lowBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await delay(150);
chk("🔴 点 effort → PUT settings 带 effort",
  putBodies.length === 1 && putBodies[0].effort === "low", JSON.stringify(putBodies[0]));

// 拖滑块 → 停手后落库（带 context_keep + context_trigger）
putBodies.length = 0;
$("#contextSlider").value = "80";
$("#contextSlider").dispatchEvent(new window.Event("input", { bubbles: true }));
await delay(900);                            // 代码里是 700ms 防抖
chk("🔴 拖完滑块停手 → PUT settings 带 context_keep=160000",
  putBodies.length === 1 && putBodies[0].context_keep === 160000,
  JSON.stringify(putBodies[0]));
chk("同时带上了 context_trigger=200000",
  putBodies.length === 1 && putBodies[0].context_trigger === 200000,
  JSON.stringify(putBodies[0]));

// ══════════════════════════════════════════════════════════════════════════════
// 8. 打开设置页会去对齐一次（openProfile 里挂的 refreshModelCard）
// ══════════════════════════════════════════════════════════════════════════════
window.eval("MODEL_UI.loaded = false;");
calls.length = 0;
window.eval("openProfile()");
await delay(200);
chk("🔴 打开设置页 → 自动拉一次允许列表（不用手动刷新）",
  calls.some((c) => c.includes("GET") && c.includes("/app/ext/providers")),
  calls.join(" | "));
chk("打开设置页也拉了一次 settings 对齐 effort/上下文",
  calls.some((c) => c.includes("GET") && c.includes("/app/ext/settings")),
  calls.join(" | "));

// ══════════════════════════════════════════════════════════════════════════════
// 汇总
// ══════════════════════════════════════════════════════════════════════════════
const passed = results.filter(([, ok]) => ok).length;
const lines = [`设置页模型/参数前端验收：${passed}/${results.length} 通过`];
for (const [name, ok, detail] of results) {
  lines.push(`[${ok ? "PASS" : "FAIL"}] ${name}${!ok && detail ? `   [${detail}]` : ""}`);
}
const out = lines.join("\n") + "\n";
fs.writeFileSync(path.join(HERE, "model_ui_report.txt"), out, "utf-8");
console.log(`设置页模型/参数前端验收：${passed}/${results.length}`);
for (const l of lines.slice(1)) if (l.startsWith("[FAIL]")) console.log(l);
process.exit(passed === results.length ? 0 : 1);

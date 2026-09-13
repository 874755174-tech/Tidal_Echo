/* 会话归档 / 删除 / 改名 —— 前端（web/index.html）实测
 * ============================================================================
 *
 * 为什么需要这个脚本
 * ------------------
 * 后端 sessions_manage.py 已经有 40 项验收（tools/sessions_manage_check.py），
 * 但那只证明**接口对**。Lily 的需求是**界面上的行为**：
 *
 *   1. 会话条目上有「归档 / 删除」入口
 *   2. 删除前必须**二次确认**
 *   3. 删除后**自动切到仍存在的会话，或回到主页**
 *   4. 改名要能改（含 __legacy__ 这种虚拟会话）
 *
 * 这些只有真正把 index.html 跑起来才能验。做法：
 *   · 用 jsdom 载入真实 index.html（不 mock DOM，mock 的是网络）
 *   · 造一个假 fetch，按请求路径返回后端应该给的数据
 *   · 直接调用页面里的函数 / 点 DOM 节点，断言结果
 *
 * 跑法：
 *   NODE_PATH=<workspace>/node_modules node tools/session_ui_check.mjs
 * 结果写 tools/session_ui_report.txt
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

// ── 假后端：按路径返回数据，并记录调用 ──────────────────────────────────────
const calls = [];
let sessionsState = [
  { id: "__legacy__", title: "旧主线 / Desktop 记录", count: 2, since_id: 0 },
  { id: "sess-A", title: "关于房子的讨论", count: 5, since_id: 0 },
  { id: "sess-B", title: "新对话", count: 3, since_id: 0 },
];
let archivedState = [];

function jsonResponse(obj, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => obj,
    text: async () => JSON.stringify(obj),
  };
}

const fakeFetch = async (url, opts = {}) => {
  const u = String(url);
  const method = (opts.method || "GET").toUpperCase();
  calls.push(`${method} ${u}`);

  if (u.includes("/app/sessions/manage/archived")) {
    return jsonResponse({ sessions: archivedState });
  }
  if (u.includes("/app/sessions/manage/count")) {
    const sid = decodeURIComponent((u.split("session_id=")[1] || "").split("&")[0]);
    const s = sessionsState.find((x) => x.id === sid);
    return jsonResponse({ ok: true, count: s ? s.count : 0 });
  }
  if (u.includes("/app/sessions/manage/archive")) {
    const body = JSON.parse(opts.body || "{}");
    const sid = body.session_id;
    const on = body.archived !== false;
    const idx = sessionsState.findIndex((x) => x.id === sid);
    if (on) {
      if (idx >= 0) archivedState.push(sessionsState.splice(idx, 1)[0]);
    } else {
      const j = archivedState.findIndex((x) => x.id === sid);
      if (j >= 0) sessionsState.push(archivedState.splice(j, 1)[0]);
    }
    return jsonResponse({ ok: true, archived: on });
  }
  if (u.includes("/app/sessions/manage/purge")) {
    const body = JSON.parse(opts.body || "{}");
    sessionsState = sessionsState.filter((x) => x.id !== body.session_id);
    return jsonResponse({ ok: true, deleted: true });
  }
  if (u.includes("/app/sessions/manage/rename")) {
    const body = JSON.parse(opts.body || "{}");
    const s = sessionsState.find((x) => x.id === body.session_id);
    if (s) s.title = body.title;
    return jsonResponse({ ok: true, title: body.title });
  }
  if (u.includes("/app/sessions/manage")) {
    return jsonResponse({ active_session: sessionsState[0]?.id || "", sessions: sessionsState });
  }
  if (u.includes("/app/sessions")) {
    return jsonResponse({ active_session: "", sessions: sessionsState });
  }
  if (u.includes("/app/history")) {
    return jsonResponse({ messages: [] });
  }
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
  (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

// 让页面进入"已登录 + 非 mock"的状态
window.localStorage.setItem("companion_secret", "test-secret");
window.localStorage.setItem("companion_api_session_pick", "__legacy__");

// 等脚本执行完
await new Promise((r) => setTimeout(r, 300));

const $ = (sel) => window.document.querySelector(sel);

// 把 secret 灌进页面（页面用模块内变量，不是每次读 localStorage）
window.eval(`
  try { secret = "test-secret"; } catch(e) {}
  try { USE_MOCK = false; } catch(e) {}
`);

// ══════════════════════════════════════════════════════════════════════════════
// 1. 新端点可用时，列表来自 /app/sessions/manage
// ══════════════════════════════════════════════════════════════════════════════
await window.eval("loadSessions()");
await new Promise((r) => setTimeout(r, 120));

chk("调用了 /app/sessions/manage（不再依赖 AI 身体）",
  calls.some((c) => c.includes("GET") && c.includes("/app/sessions/manage") && !c.includes("archived") && !c.includes("count")),
  calls.slice(-6).join(" | "));

const listHtml = $("#sessionList")?.innerHTML || "";
chk("列表里出现了 sess-A", listHtml.includes("sess-A"), "");
chk("列表里出现了 sess-B", listHtml.includes("sess-B"), "");
chk("🔴 会话条目有「归档」按钮", listHtml.includes("data-archive="), "");
chk("🔴 会话条目有「删除」按钮", listHtml.includes("data-delete="), "");
chk("会话条目仍有「改名」按钮", listHtml.includes("data-rename="), "");
chk("🔴 虚拟会话 __legacy__ 也有改名按钮（原版这里没有）",
  /data-rename="__legacy__"/.test(listHtml), "");
chk("条目右侧用的是新的 .session-acts 容器", listHtml.includes("session-acts"), "");
chk("消息数显示成「N 条消息」", listHtml.includes("条消息") || /\\d+ 条消息/.test(listHtml), "");

// ══════════════════════════════════════════════════════════════════════════════
// 2. 二次确认弹层：删除必须先弹确认，取消则什么都不做
// ══════════════════════════════════════════════════════════════════════════════
const beforeDel = sessionsState.length;
const delBtn = window.document.querySelector('button[data-delete="sess-B"]');
chk("能找到 sess-B 的删除按钮", !!delBtn, "");

if (delBtn) {
  delBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 80));

  const mask = $("#confirmMask");
  chk("🔴 点删除后弹出了二次确认层", mask && !mask.hidden, mask ? `hidden=${mask.hidden}` : "没找到 confirmMask");
  const bodyText = ($("#confirmBody")?.textContent || "") + ($("#confirmNote")?.textContent || "");
  chk("确认层里写了会话名", bodyText.includes("新对话") || bodyText.includes("sess-B"), bodyText.slice(0, 80));
  chk("🔴 确认层里写了「将删除 N 条消息」（真的去查了 count）",
    /3 条消息|3 条/.test(bodyText), bodyText.slice(0, 120));
  chk("确认层说明了「归档可以留后路」", bodyText.includes("归档"), bodyText.slice(0, 120));
  chk("确认按钮文案是「删除」", ($("#confirmOk")?.textContent || "").includes("删除"),
    $("#confirmOk")?.textContent);
  chk("危险按钮带了 danger 样式", ($("#confirmOk")?.className || "").includes("danger"),
    $("#confirmOk")?.className);

  // 点「取消」→ 什么都不该发生
  $("#confirmCancel").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 80));
  chk("🔴 点「取消」后确认层关闭", $("#confirmMask").hidden, "");
  chk("🔴 点「取消」后会话数没变（没有误删）",
    sessionsState.length === beforeDel, `${sessionsState.length} vs ${beforeDel}`);
  chk("点「取消」后没有发出 purge 请求",
    !calls.some((c) => c.includes("purge")), calls.join(" | ").slice(-200));
}

// ══════════════════════════════════════════════════════════════════════════════
// 3. 二次确认后真的删除，且自动切到仍存在的会话
// ══════════════════════════════════════════════════════════════════════════════
// 先把当前会话切成 sess-B，这样"删掉当前会话"的场景才被覆盖
await window.eval('activateSession("sess-B", { reload:false })');
await new Promise((r) => setTimeout(r, 100));
chk("已把当前会话切到 sess-B",
  window.eval("activeApiSession") === "sess-B", String(window.eval("activeApiSession")));

const delBtn2 = window.document.querySelector('button[data-delete="sess-B"]');
if (delBtn2) {
  delBtn2.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 80));
  // 确认删除
  $("#confirmOk").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));

  chk("🔴 确认后发出了 purge 请求", calls.some((c) => c.includes("purge")), "");
  chk("🔴 sess-B 已从后端列表消失",
    !sessionsState.some((s) => s.id === "sess-B"), JSON.stringify(sessionsState.map((s) => s.id)));
  const after = window.eval("activeApiSession");
  chk("🔴 删除当前会话后自动切到了仍存在的会话", after !== "sess-B" && after !== "", `现在=${after}`);
  const listHtml2 = $("#sessionList")?.innerHTML || "";
  chk("界面列表里也没有 sess-B 了", !listHtml2.includes('data-session="sess-B"'), "");
}

// ══════════════════════════════════════════════════════════════════════════════
// 4. 归档：可逆，且从主列表移到归档区
// ══════════════════════════════════════════════════════════════════════════════
const nBeforeArch = sessionsState.length;
const archBtn = window.document.querySelector('button[data-archive="sess-A"]');
chk("能找到 sess-A 的归档按钮", !!archBtn, "");
if (archBtn) {
  archBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 80));
  chk("归档也会先弹二次确认", !$("#confirmMask").hidden, "");
  chk("归档的确认按钮不带 danger（它是安全操作）",
    !($("#confirmOk")?.className || "").includes("danger"), $("#confirmOk")?.className);
  $("#confirmOk").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));

  chk("🔴 归档后 sess-A 离开主列表",
    !sessionsState.some((s) => s.id === "sess-A"), JSON.stringify(sessionsState.map((s) => s.id)));
  chk("🔴 归档后 sess-A 进入归档区",
    archivedState.some((s) => s.id === "sess-A"), JSON.stringify(archivedState.map((s) => s.id)));
  chk("🔴 归档**没有**删除任何会话（总数不变）",
    sessionsState.length + archivedState.length === nBeforeArch + 0, `${sessionsState.length}+${archivedState.length} vs ${nBeforeArch}`);
  chk("归档后归档入口显示了", !$("#sessionArchWrap").hidden, "");
}

// ══════════════════════════════════════════════════════════════════════════════
// 5. 从归档区恢复
// ══════════════════════════════════════════════════════════════════════════════
// 展开归档区
$("#sessionArchToggle")?.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await new Promise((r) => setTimeout(r, 120));
const archHtml = $("#sessionArchBody")?.innerHTML || "";
chk("归档区列出了 sess-A", archHtml.includes("sess-A"), archHtml.slice(0, 100));
chk("归档区有「恢复」按钮", archHtml.includes("data-unarchive="), "");

const unBtn = window.document.querySelector('button[data-unarchive="sess-A"]');
if (unBtn) {
  unBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 200));
  chk("🔴 恢复后 sess-A 回到主列表",
    sessionsState.some((s) => s.id === "sess-A"), JSON.stringify(sessionsState.map((s) => s.id)));
  chk("恢复后归档区里没有 sess-A 了",
    !archivedState.some((s) => s.id === "sess-A"), JSON.stringify(archivedState.map((s) => s.id)));
}

// ══════════════════════════════════════════════════════════════════════════════
// 6. 改名走新接口（虚拟会话也能改）
// ══════════════════════════════════════════════════════════════════════════════
const origPrompt = window.prompt;
window.prompt = () => "我给它起的新名字";
const beforeRenameCalls = calls.length;
await window.eval('renameSession("__legacy__")');
await new Promise((r) => setTimeout(r, 150));
window.prompt = origPrompt;

const renameCalls = calls.slice(beforeRenameCalls);
chk("🔴 改名走的是 /app/sessions/manage/rename",
  renameCalls.some((c) => c.includes("/app/sessions/manage/rename")), renameCalls.join(" | "));
chk("🔴 改名没有走 PATCH /app/sessions/{id}（那条对 __legacy__ 必然 404）",
  !renameCalls.some((c) => c.startsWith("PATCH")), renameCalls.join(" | "));
const legacy = sessionsState.find((s) => s.id === "__legacy__");
chk("🔴 虚拟会话 __legacy__ 的名字真的改了",
  legacy && legacy.title === "我给它起的新名字", legacy ? legacy.title : "找不到");

// ══════════════════════════════════════════════════════════════════════════════
// 7. 全部删完 → 回到主页（空态）
// ══════════════════════════════════════════════════════════════════════════════
// 只留一条，然后删掉它
sessionsState = [{ id: "sess-only", title: "最后一个", count: 1, since_id: 0 }];
await window.eval("loadSessions()");
await new Promise((r) => setTimeout(r, 120));
await window.eval('activateSession("sess-only", { reload:false })');
await new Promise((r) => setTimeout(r, 100));

const onlyBtn = window.document.querySelector('button[data-delete="sess-only"]');
if (onlyBtn) {
  onlyBtn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 80));
  $("#confirmOk").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 250));

  chk("🔴 删掉最后一个会话后回落到 __legacy__（回到主页）",
    window.eval("activeApiSession") === "__legacy__",
    String(window.eval("activeApiSession")));
  chk("删光后会话面板关掉了", $("#sessionPop").hidden, "");
}

// ══════════════════════════════════════════════════════════════════════════════
// 汇总
// ══════════════════════════════════════════════════════════════════════════════
const passed = results.filter(([, ok]) => ok).length;
const lines = [`会话前端（归档/删除/改名）验收：${passed}/${results.length} 通过`];
for (const [name, ok, detail] of results) {
  lines.push(`[${ok ? "PASS" : "FAIL"}] ${name}${!ok && detail ? `   [${detail}]` : ""}`);
}
const out = lines.join("\n") + "\n";
fs.writeFileSync(path.join(HERE, "session_ui_report.txt"), out, "utf-8");
console.log(`会话前端验收：${passed}/${results.length}`);
for (const l of lines.slice(1)) if (l.startsWith("[FAIL]")) console.log(l);
process.exit(passed === results.length ? 0 : 1);

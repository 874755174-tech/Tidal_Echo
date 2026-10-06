/* Tidal Echo — service worker (offline shell + Web Push).
   IMPORTANT: bump CACHE on every front-end change, or installed clients keep the
   old shell (the precached index.html won't refresh until the SW reinstalls).

   🔴 2026-10-05 · 这个坑真的踩了：改了选图交互、push 上去了，但她手机上还是"选图即发"。
   根因就是 CACHE 停在 v8 没换 —— 老客户端 precache 里的旧 index.html 一直活着，
   而 navigate 走 network-first 只在**刷新**时才换新壳；iOS PWA 从后台唤回来根本不刷新。
   ⇒ 以后改 web/index.html 必须同时换这一行。检查：线上 sw.js 的 CACHE 名对不对。
   ⚠️ 同理：`PRECACHE` 里加页面（2026-10-06 加 tides.html）也归这一行管 ——
      独立页没进 precache 时走的是下面 fetch 分支的"先缓存后网络"，
      她**第二次**打开就会拿到第一次缓存下来的旧壳（老壳缓存的第二个坑）。 */
const CACHE = "kael-home-v10-tides";
/* 壳版本号：跟 index.html 里的 SHELL_VERSION 必须一致。
   前端拿它跟 SW 的 VERSION 比，对不上就说明「你手上是旧壳」，当场提示刷新。 */
const VERSION = "2026-10-06-tides";
const AI_NAME = "Claude";          // push-title fallback; keep in sync with index.html CONFIG.AI_NAME
const PRECACHE = [
  "./index.html",
  "./tides.html",                  // 行迹页：独立页，不 precache 就会吃到旧壳
  "./chat-light.webp", "./chat-harbor.webp",
  "./menu-light.webp", "./menu-harbor.webp",
  "./avatar-sea.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(PRECACHE))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/relay/")) return;          // never intercept the API / SSE
  if (e.request.mode === "navigate") {
    // network-first for the page → an online reload always gets the latest index.html
    e.respondWith(fetch(e.request, { cache: "reload" }).catch(() => caches.match("./index.html")));
    return;
  }
  if (e.request.method === "GET" && url.origin === location.origin) {
    e.respondWith(
      caches.match(e.request).then((r) => {
        if (r) return r;
        return fetch(e.request).then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return res;
        });
      })
    );
  }
});

// 前端问「你是谁、什么版本」→ 拿它跟自己比，不一致就提示刷新（见 index.html SHELL_VERSION）。
// 前端用 MessageChannel 传了 port2，这里通过 e.ports[0] 回话；无 port 时兜底回给窗口本身。
self.addEventListener("message", (e) => {
  if (e.data && e.data.type === "KAEL_SW_VERSION") {
    const reply = { type: "KAEL_SW_VERSION", version: VERSION, cache: CACHE };
    if (e.ports && e.ports[0]) {
      e.ports[0].postMessage(reply);
    } else if (e.source && e.source.postMessage) {
      e.source.postMessage(reply);
    }
  }
});

// ── Web Push (VAPID) ──────────────────────────────
// The relay sends a push when the AI replies and no PWA tab is holding the stream;
// here we surface it on the lock screen.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; }
  catch (_) { d = { body: (e.data && e.data.text && e.data.text()) || "" }; }
  const title = d.title || AI_NAME;                        // backend sends RELAY_AI_NAME as title
  const body  = d.body  || "你有一条新消息";
  const tag   = d.id ? ("companion-" + d.id) : "companion-msg";
  e.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag,
      renotify: true,
      icon:  "./icon-192.png",
      badge: "./icon-192.png",
      vibrate: [80, 40, 80],
      data: { url: d.url || "./" },
    })
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || "./";
  e.waitUntil(
    // matchAll only returns clients this SW controls (our own scope), so focus the first one.
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cls) => {
      for (const c of cls) {
        if ("focus" in c){ c.postMessage({ type: "backfill" }); return c.focus(); }
      }
      return self.clients.openWindow ? self.clients.openWindow(target) : null;
    })
  );
});

// Karya Browser Link - service worker.
// Connects to the Karya app on this PC (ws://127.0.0.1:<port>/ext, with the install's secret token) and runs page
// actions for it. Karya only works in its own tabs (shown in a "Karya" tab group); it can't see your other tabs.
const VERSION = chrome.runtime.getManifest().version;
let ws = null;
let connecting = false;
let keepAlive = null;
let retryTimer = null;
let lastError = "Not connected yet.";
let failures = 0;
let nextTryAt = 0;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Chrome refuses tab changes for a moment while the user drags a tab ("Tabs cannot be edited right now"): wait, retry.
async function retryTabs(fn) {
  for (let i = 0; ; i++) {
    try { return await fn(); } catch (e) {
      if (i < 15 && /cannot be edited right now|dragging a tab/i.test(String(e && e.message || e))) { await sleep(400); continue; }
      throw e;
    }
  }
}

// ------------------------------------------------------------------ settings
async function readConfig() {
  let file = {};
  try {
    const r = await fetch(chrome.runtime.getURL("config.json"), { cache: "no-store" });
    if (r.ok) file = await r.json();  // written by Karya each time it starts
  } catch (e) { /* no config.json: the user can paste the token in the popup */ }
  const stored = await chrome.storage.local.get(["port", "token"]);
  return { port: Number(file.port || stored.port || 8765), token: String(file.token || stored.token || "") };
}

// ------------------------------------------------------------------ connection
function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

async function connect() {
  if (connecting || (ws && ws.readyState <= WebSocket.OPEN)) return;
  connecting = true;
  try {
    const { port, token } = await readConfig();
    if (!token) { lastError = "No pairing token yet: start Karya once, or paste the token here."; return; }
    const sock = new WebSocket(`ws://127.0.0.1:${port}/ext?token=${encodeURIComponent(token)}`);
    let opened = false;
    ws = sock;
    sock.onopen = () => {
      opened = true;
      lastError = "";
      send({ type: "hello", version: VERSION, ua: navigator.userAgent });
      clearInterval(keepAlive);
      keepAlive = setInterval(() => send({ type: "ping" }), 20000);  // keeps this worker (and the link) alive
      setStatusBadge();
    };
    sock.onmessage = (ev) => onMessage(ev.data);
    sock.onclose = (ev) => {
      if (ws === sock) ws = null;
      clearInterval(keepAlive);
      if (ev.code === 1008) lastError = "Karya refused the link (wrong token). Restart Karya, then press Reconnect.";
      else if (opened) lastError = "Karya stopped or restarted.";
      else lastError = `Karya isn't running (nothing on port ${port}).`;
      setStatusBadge();
      failures = opened ? 0 : failures + 1;
      // Back off while Karya is closed: each failed try shows up as an error line in chrome://extensions.
      scheduleRetry(opened ? 1500 : Math.min(60000, 5000 * 2 ** Math.min(failures, 4)));
    };
  } catch (e) {
    lastError = String(e && e.message || e);
    scheduleRetry(5000);
  } finally {
    connecting = false;
  }
}

function scheduleRetry(ms) {
  clearTimeout(retryTimer);
  nextTryAt = Date.now() + ms;
  retryTimer = setTimeout(connect, ms);
}

chrome.alarms.create("karya-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => {  // wakes the worker; connects only when the back-off allows
  if (a.name === "karya-reconnect" && Date.now() >= nextTryAt) connect();
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();

async function setStatusBadge(attention) {
  const on = ws && ws.readyState === WebSocket.OPEN;
  await chrome.action.setBadgeBackgroundColor({ color: attention ? "#d93025" : "#5b4bdb" });
  await chrome.action.setBadgeText({ text: attention ? "!" : "" });
  await chrome.action.setTitle({ title: attention ? `Karya needs you: ${attention}`.slice(0, 200)
    : on ? "Karya Browser Link: connected" : `Karya Browser Link: ${lastError}` });
}

// ------------------------------------------------------------------ Karya's tabs
async function state() {
  const s = await chrome.storage.session.get(["tabs", "current", "groups"]);
  return { tabs: s.tabs || [], current: s.current ?? null, groups: s.groups || {} };
}
async function save(s) { await chrome.storage.session.set(s); }

async function addTab(tabId, makeCurrent = true) {
  const s = await state();
  if (!s.tabs.includes(tabId)) s.tabs.push(tabId);
  if (makeCurrent) s.current = tabId;
  await save(s);
  groupTab(tabId).catch(() => {});
}

async function groupTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  const s = await state();
  let groupId = s.groups[tab.windowId];
  try {
    if (groupId != null) await chrome.tabGroups.get(groupId); else throw new Error("none");
    await retryTabs(() => chrome.tabs.group({ tabIds: [tabId], groupId }));
  } catch (e) {
    groupId = await retryTabs(() => chrome.tabs.group({ tabIds: [tabId], createProperties: { windowId: tab.windowId } }));
    await retryTabs(() => chrome.tabGroups.update(groupId, { title: "Karya", color: "purple" }));
    s.groups[tab.windowId] = groupId;
    await save({ groups: s.groups });
  }
}

async function currentTab() {
  const s = await state();
  if (s.current != null) {
    try { await chrome.tabs.get(s.current); return s.current; } catch (e) { /* closed */ }
  }
  for (const id of s.tabs.slice().reverse()) {
    try { await chrome.tabs.get(id); await save({ current: id }); return id; } catch (e) { /* closed */ }
  }
  return null;
}

async function requireTab() {
  const id = await currentTab();
  if (id == null) throw new Error("Karya has no tab open in your Chrome yet. Use browser_open first.");
  return id;
}

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const s = await state();
  if (!s.tabs.includes(tabId)) return;
  s.tabs = s.tabs.filter((t) => t !== tabId);
  if (s.current === tabId) s.current = s.tabs.length ? s.tabs[s.tabs.length - 1] : null;
  await save(s);
  send({ type: "event", text: "A Karya tab was closed." });
});

chrome.tabs.onCreated.addListener(async (tab) => {  // a page in Karya's tab opened a new tab: follow it
  const s = await state();
  if (tab.openerTabId != null && s.tabs.includes(tab.openerTabId)) {
    await addTab(tab.id, true);
    send({ type: "event", text: "A new tab opened from the Karya tab and is now active." });
  }
});

async function targetWindow() {
  try {
    const w = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
    if (w && w.id != null && w.type === "normal") return w.id;
  } catch (e) { /* no window */ }
  return null;
}

async function waitLoad(tabId, timeout = 30000) {
  const end = Date.now() + timeout;
  await sleep(250);
  while (Date.now() < end) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab.status === "complete") return tab;
    } catch (e) { throw new Error("The Karya tab was closed."); }
    await sleep(200);
  }
  return chrome.tabs.get(tabId);
}

// ------------------------------------------------------------------ page scripting
async function frames(tabId) {
  const res = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: () => ({ w: window.innerWidth, h: window.innerHeight, top: window === window.top }),
  });
  return res.filter((r) => r.result && (r.result.top || (r.result.w >= 80 && r.result.h >= 40)))
    .sort((a, b) => (a.frameId === 0 ? -1 : b.frameId === 0 ? 1 : a.frameId - b.frameId)).map((r) => r.frameId);
}

async function inFrame(tabId, frameId, func, args = []) {
  await chrome.scripting.executeScript({ target: { tabId, frameIds: [frameId] }, files: ["page.js"] });
  const [r] = await chrome.scripting.executeScript({ target: { tabId, frameIds: [frameId] }, func, args });
  return r ? r.result : null;
}

function explain(e) {
  const m = String(e && e.message || e);
  if (/cannot access|cannot be scripted|extensions gallery|chrome:\/\//i.test(m))
    return "Chrome doesn't let extensions work on this page (chrome:// pages and the Web Store). Open a normal website.";
  if (/no frame with id|frame.*removed/i.test(m)) return "The page changed while Karya was working on it. Take a new browser_snapshot.";
  return m;
}

// ------------------------------------------------------------------ real mouse input (boards, canvas)
// Sites like chess.com and lichess ignore simulated clicks. For click_at / drag / move_piece the extension sends real
// mouse events through Chrome's debugger. Chrome shows "started debugging this browser" while it's attached; Karya
// detaches 20 seconds after the last such action. If attaching isn't possible, the simulated events are used.
const REAL_MOUSE_OPS = new Set(["click_at", "drag", "move_piece"]);
let dbgTab = null;
let dbgTimer = null;

if (chrome.debugger) {
  chrome.debugger.onDetach.addListener((source) => { if (source.tabId === dbgTab) dbgTab = null; });
}

async function debuggerOn(tabId) {
  if (dbgTab === tabId) return;
  if (dbgTab != null) { try { await chrome.debugger.detach({ tabId: dbgTab }); } catch (e) { /* already gone */ } dbgTab = null; }
  await chrome.debugger.attach({ tabId }, "1.3");
  dbgTab = tabId;
}

function debuggerIdle() {
  clearTimeout(dbgTimer);
  dbgTimer = setTimeout(async () => {
    const t = dbgTab;
    dbgTab = null;
    if (t != null) { try { await chrome.debugger.detach({ tabId: t }); } catch (e) { /* already gone */ } }
  }, 20000);
}

async function realMouse(tabId, steps) {
  await debuggerOn(tabId);
  try {
    for (const s of steps) {
      if (s.wait) { await sleep(s.wait); continue; }
      await chrome.debugger.sendCommand({ tabId }, "Input.dispatchMouseEvent", s);
    }
  } finally {
    debuggerIdle();
  }
}

const clickSteps = (x, y, count = 1) => [
  { type: "mouseMoved", x, y, button: "none" },
  { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: count },
  { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: count },
];

function dragSteps(x1, y1, x2, y2, n = 12) {
  const steps = [{ type: "mouseMoved", x: x1, y: y1, button: "none" },
                 { type: "mousePressed", x: x1, y: y1, button: "left", buttons: 1, clickCount: 1 }];
  for (let i = 1; i <= n; i++) {
    steps.push({ type: "mouseMoved", x: x1 + (x2 - x1) * i / n, y: y1 + (y2 - y1) * i / n, button: "left", buttons: 1 },
               { wait: 15 });
  }
  steps.push({ type: "mouseReleased", x: x2, y: y2, button: "left", buttons: 0, clickCount: 1 });
  return steps;
}

// Returns the result, or null to use the simulated events instead.
async function realMouseAct(tabId, op, args) {
  const plan = await inFrame(tabId, 0, (o, a) => window.__karya.plan(o, a), [op, args]);
  if (!plan || plan.ok === false) return plan || null;   // e.g. "no piece on a3": a real answer, not a fallback
  try {
    if (op === "click_at") {
      const [x, y] = plan.points[0];
      await realMouse(tabId, args.double ? [...clickSteps(x, y, 1), ...clickSteps(x, y, 2)] : clickSteps(x, y));
      return { ok: true, at: [Math.round(x), Math.round(y)], target: plan.target, real: true };
    }
    if (op === "drag") {
      const [[x1, y1], [x2, y2]] = plan.points;
      await realMouse(tabId, dragSteps(x1, y1, x2, y2, Math.max(4, Math.min(40, Number(args.steps) || 12))));
      return { ok: true, from: [Math.round(x1), Math.round(y1)], to: [Math.round(x2), Math.round(y2)], real: true };
    }
    // move_piece: click-click first, then a drag; pick the promotion piece if the site asks
    const check = async () => {
      await sleep(450);
      let c = await inFrame(tabId, 0, (p) => window.__karya.checkMove(p), [plan]);
      if (c && c.promotion) {
        await realMouse(tabId, clickSteps(c.promotion[0], c.promotion[1]));
        await sleep(350);
        c = await inFrame(tabId, 0, (p) => window.__karya.checkMove(p), [plan]);
      }
      return c || { moved: false, placement: "" };
    };
    await realMouse(tabId, [...clickSteps(plan.from[0], plan.from[1]), { wait: 150 }, ...clickSteps(plan.to[0], plan.to[1])]);
    let result = await check();
    if (!result.moved) {
      await realMouse(tabId, dragSteps(plan.from[0], plan.from[1], plan.to[0], plan.to[1]));
      result = await check();
    }
    if (result.moved) return { ok: true, moved: `${plan.from_sq}-${plan.to_sq}`, placement: result.placement, real: true };
    return { ok: false, error: `the move ${plan.from_sq}-${plan.to_sq} didn't happen (not your turn, an illegal move, or ` +
                               `the game isn't running). Board now: ${result.placement}` };
  } catch (e) {
    return null;   // the debugger can't attach here (DevTools open, another debugger...): simulated events
  }
}

// ------------------------------------------------------------------ operations Karya asks for
const ops = {
  async ping() { return { pong: true, version: VERSION }; },

  async open({ url, new_tab }) {
    let tabId = await currentTab();
    if (new_tab || tabId == null) {
      const windowId = await targetWindow();
      const tab = windowId == null ? (await chrome.windows.create({ url, focused: true })).tabs[0]
        : await retryTabs(() => chrome.tabs.create({ url, active: true, windowId }));
      tabId = tab.id;
      await addTab(tabId, true);
    } else {
      await retryTabs(() => chrome.tabs.update(tabId, { url }));  // don't pull the user away from the tab they're using
    }
    const tab = await waitLoad(tabId);
    return { url: tab.url, title: tab.title };
  },

  async settle({ ms = 600 }) {
    const tabId = await requireTab();
    const tab = await waitLoad(tabId, 12000);
    await sleep(Math.min(Number(ms) || 0, 5000));
    return { url: tab.url, title: tab.title };
  },

  async snapshot({ next = 1, reset = false, max = 400, text_chars = 2000 }) {
    const tabId = await requireTab();
    const items = [];
    for (const frameId of await frames(tabId)) {
      if (items.length >= max) break;
      let res = null;
      try {
        res = await inFrame(tabId, frameId, (a) => window.__karya.snapshot(a), [{ next, reset, max: max - items.length }]);
      } catch (e) { if (frameId === 0) throw e; continue; }
      if (!res) continue;
      const list = Array.isArray(res) ? res : (res.items || []);
      for (const it of list) it.frame = frameId;
      items.push(...list);
      next = Array.isArray(res) ? next + list.length : res.next;
    }
    let text = "";
    try { text = await inFrame(tabId, 0, () => (document.body ? document.body.innerText : "")) || ""; } catch (e) { /* ignore */ }
    const tab = await chrome.tabs.get(tabId);
    return { url: tab.url, title: tab.title, items, next, text: text.slice(0, Math.max(0, Number(text_chars) || 0)) };
  },

  async type_native({ frame = 0, id, value = "", clear = true, pick = true, keep_focus = false }) {
    // Type into a field the way a person does: Chrome's own key input (trusted events). Frameworks like Workday's
    // ignore values set from page script and kept marking filled fields invalid (2026-10-09). Main frame only.
    if (!chrome.debugger || frame !== 0) return { ok: false, error: "native typing unavailable here" };
    const tabId = await requireTab();
    const focus = await inFrame(tabId, 0, (o, a) => window.__karya.act(o, a), ["focusfield", { id }]);
    if (!focus || focus.ok === false) return focus || { ok: false, error: "no field" };
    await debuggerOn(tabId);
    try {
      const send = (params) => chrome.debugger.sendCommand({ tabId }, "Input.dispatchKeyEvent", params);
      if (clear && focus.had) {     // select everything in the field and delete it, as Ctrl+A, Backspace would
        await send({ type: "rawKeyDown", key: "a", code: "KeyA", windowsVirtualKeyCode: 65, modifiers: 2, commands: ["selectAll"] });
        await send({ type: "keyUp", key: "a", code: "KeyA", windowsVirtualKeyCode: 65, modifiers: 2 });
        await send({ type: "rawKeyDown", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 });
        await send({ type: "keyUp", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 });
      }
      if (String(value)) await chrome.debugger.sendCommand({ tabId }, "Input.insertText", { text: String(value) });
    } finally {
      debuggerIdle();
    }
    return await inFrame(tabId, 0, (o, a) => window.__karya.act(o, a),
      ["afterfill", { id, value: String(value), clear, pick, keep_focus }]) || { ok: true };
  },

  async upload_native({ frame = 0, id, path }) {
    // Put a file from this PC into the page's file input the way Chrome does when a person picks it: a trusted
    // change event. Sites like Instagram ignore files set from page script (2026-10-07: the reel never loaded).
    if (!chrome.debugger || frame !== 0) return { ok: false, error: "native upload unavailable here" };
    const tabId = await requireTab();
    const marked = await inFrame(tabId, 0, (o, a) => window.__karya.act(o, a), ["upload", { id, mark: true }]);
    if (!marked || marked.ok === false) return marked || { ok: false, error: "no file field" };
    await debuggerOn(tabId);
    try {
      const found = await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", {
        expression: `(() => { const walk = (root) => { const f = root.querySelector('[data-karya-upload="1"]');
          if (f) return f; for (const e of root.querySelectorAll('*')) { if (e.shadowRoot) { const r = walk(e.shadowRoot);
          if (r) return r; } } return null; }; return walk(document); })()`,
        returnByValue: false,
      });
      const objectId = found && found.result && found.result.objectId;
      if (!objectId) return { ok: false, error: "the file field went away" };
      await chrome.debugger.sendCommand({ tabId }, "DOM.setFileInputFiles", { files: [path], objectId });
      await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", {
        expression: "document.querySelectorAll('[data-karya-upload]').forEach((e) => e.removeAttribute('data-karya-upload'))",
      }).catch(() => null);
      return { ok: true, native: true };
    } finally {
      debuggerIdle();
    }
  },

  async act({ frame = 0, op, args = {} }) {
    const tabId = await requireTab();
    if (frame === 0 && REAL_MOUSE_OPS.has(op) && chrome.debugger) {
      const real = await realMouseAct(tabId, op, args);
      if (real) return real;
    }
    const result = await inFrame(tabId, frame, (o, a) => window.__karya.act(o, a), [op, args]) || {};
    if (result.open_url) {  // a link that opens a new tab: open it ourselves, next to Karya's tab
      const opener = await chrome.tabs.get(tabId);
      const tab = await retryTabs(() => chrome.tabs.create({ url: result.open_url, active: true, windowId: opener.windowId,
        openerTabId: tabId, index: opener.index + 1 }));
      await addTab(tab.id, true);
      await waitLoad(tab.id);
      result.new_tab = true;
    }
    return result;
  },

  async text({ max_chars = 8000 }) {
    const tabId = await requireTab();
    const parts = [];
    for (const frameId of await frames(tabId)) {
      try {
        const t = await inFrame(tabId, frameId, () => (document.body ? document.body.innerText : "")) || "";
        if (t.trim().length > 40 || frameId === 0) parts.push(t);
      } catch (e) { /* frame went away */ }
    }
    const tab = await chrome.tabs.get(tabId);
    return { url: tab.url, title: tab.title, text: parts.join("\n\n").slice(0, Number(max_chars) || 8000) };
  },

  async screenshot() {
    const tabId = await requireTab();
    let tab = await chrome.tabs.get(tabId);
    if (!tab.active) { await retryTabs(() => chrome.tabs.update(tabId, { active: true })); await sleep(400); tab = await chrome.tabs.get(tabId); }
    const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    return { png: dataUrl.split(",")[1] };
  },

  async back() {
    const tabId = await requireTab();
    await retryTabs(() => chrome.tabs.goBack(tabId));
    const tab = await waitLoad(tabId, 15000);
    return { url: tab.url, title: tab.title };
  },

  async tabs({ action = "list", index = null }) {
    const s = await state();
    const live = [];
    for (const id of s.tabs) { try { live.push(await chrome.tabs.get(id)); } catch (e) { /* closed */ } }
    const current = await currentTab();
    if (action === "switch" || action === "close") {
      if (index == null || index < 0 || index >= live.length) throw new Error(`tab index must be 0..${live.length - 1}`);
      const tab = live[index];
      if (action === "close") await retryTabs(() => chrome.tabs.remove(tab.id));
      else { await save({ current: tab.id }); await retryTabs(() => chrome.tabs.update(tab.id, { active: true })); }
      return { ok: true };
    }
    return { tabs: live.map((t, i) => ({ index: i, title: (t.title || "").slice(0, 80), url: (t.url || "").slice(0, 150), active: t.id === current })) };
  },

  async close_tabs() {
    const s = await state();
    for (const id of s.tabs) { try { await retryTabs(() => chrome.tabs.remove(id)); } catch (e) { /* already closed */ } }
    await save({ tabs: [], current: null });
    return { ok: true };
  },

  async attention({ on, text }) { await setStatusBadge(on ? (text || "approval needed") : null); return { ok: true }; },
};

async function onMessage(raw) {
  let msg;
  try { msg = JSON.parse(raw); } catch (e) { return; }
  if (!msg || !msg.method) return;  // pong
  const op = ops[msg.method];
  if (msg.id == null) { if (op) op(msg.params || {}).catch(() => {}); return; }  // notification
  if (!op) { send({ id: msg.id, error: `unknown operation ${msg.method}` }); return; }
  try {
    send({ id: msg.id, result: await op(msg.params || {}) });
  } catch (e) {
    send({ id: msg.id, error: explain(e) });
  }
}

// ------------------------------------------------------------------ popup
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    if (msg.type === "status") {
      const cfg = await readConfig();
      const s = await state();
      return { connected: !!(ws && ws.readyState === WebSocket.OPEN), error: lastError, port: cfg.port,
        hasToken: !!cfg.token, tabs: s.tabs.length };
    }
    if (msg.type === "save") {
      const update = { port: Number(msg.port) || 8765 };
      if (String(msg.token || "").trim()) update.token = String(msg.token).trim();
      await chrome.storage.local.set(update);
      if (ws) ws.close();
      await sleep(200);
      connect();
      return { ok: true };
    }
    if (msg.type === "reconnect") { if (ws) ws.close(); else connect(); return { ok: true }; }
    if (msg.type === "adopt") {  // let Karya use the tab the user is looking at
      const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (!tab || !/^https?:/i.test(tab.url || "")) return { ok: false, error: "Open a normal website tab first." };
      await addTab(tab.id, true);
      send({ type: "event", text: `The user handed this tab to Karya: ${tab.title || tab.url}` });
      return { ok: true, title: tab.title };
    }
    return { ok: false };
  })().then(reply);
  return true;  // async reply
});

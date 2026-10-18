(() => {
  "use strict";
  const params = new URLSearchParams(location.search);
  if (params.get("app") === "1") sessionStorage.setItem("karya-app", "1");   // opened as Karya's own window
  const inApp = sessionStorage.getItem("karya-app") === "1";
  if (params.get("token")) {
    localStorage.setItem("karya-token", params.get("token"));
    history.replaceState(null, "", "/");
  }
  const token = localStorage.getItem("karya-token") || "";
  const UI_VERSION = (document.querySelector('meta[name="karya-ui"]') || {}).content || "";

  const $ = (id) => document.getElementById(id);
  const chat = $("chat"), input = $("input"), form = $("chat-form"), sendBtn = $("send-btn"), stopBtn = $("stop-btn");
  const statusLine = $("status-line"), dot = $("conn-dot"), providerLabel = $("provider-label"), autoMode = $("auto-mode"), keepGoing = $("keep-going");
  const aiPill = $("ai-pill"), notice = $("notice");
  let ws = null, busy = false, activity = null, retry = 0, quitting = false;
  const steps = new Map();

  // ---------- safe markdown ----------
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function inline(text) {
    let h = esc(text);
    h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    h = h.replace(/(^|[^*\w])\*(?!\s)([^*]+?)\*(?!\w)/g, "$1<em>$2</em>");
    h = h.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    h = h.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
    return h;
  }
  function renderMarkdown(src) {
    const blocks = [];
    src = String(src || "").replace(/```[\w-]*\n?([\s\S]*?)```/g, (_, code) => { blocks.push(code); return `\n@@CODE${blocks.length - 1}@@\n`; });
    const lines = src.split("\n");
    let html = "", list = null, i = 0;
    const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
    const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
    while (i < lines.length) {
      const line = lines[i];
      let m = line.match(/^@@CODE(\d+)@@$/);
      if (m) { closeList(); html += `<pre><code>${esc(blocks[+m[1]])}</code></pre>`; i++; continue; }
      if (isRow(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        closeList();
        const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
        html += '<div class="table-wrap"><table><thead><tr>' + cells(line).map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
        i += 2;
        while (i < lines.length && isRow(lines[i])) { html += "<tr>" + cells(lines[i]).map((c) => `<td>${c}</td>`).join("") + "</tr>"; i++; }
        html += "</tbody></table></div>";
        continue;
      }
      if ((m = line.match(/^\s*[-*\u2022]\s+(.*)$/))) { if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; i++; continue; }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; i++; continue; }
      closeList();
      if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { const lvl = Math.min(m[1].length + 1, 5); html += `<h${lvl}>${inline(m[2])}</h${lvl}>`; i++; continue; }
      if (line.trim()) html += `<p>${inline(line)}</p>`;
      i++;
    }
    closeList();
    return html;
  }

  // ---------- rendering ----------
  const scroll = () => { chat.scrollTop = chat.scrollHeight; };
  const hideWelcome = () => { const w = $("welcome"); if (w) w.hidden = true; };

  function addMessage(role, text, meta) {
    hideWelcome();
    activity = null;
    const row = document.createElement("div");
    row.className = `msg ${role}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (role === "user") bubble.textContent = text;
    else bubble.innerHTML = renderMarkdown(text);
    if (meta) { const m = document.createElement("div"); m.className = "meta"; m.textContent = meta; bubble.appendChild(m); }
    row.appendChild(bubble);
    chat.appendChild(row);
    scroll();
  }

  function activityBox() {
    if (!activity) {
      hideWelcome();
      activity = document.createElement("div");
      activity.className = "activity";
      activity.setAttribute("aria-label", "What Karya is doing");
      chat.appendChild(activity);
    }
    return activity;
  }

  function addStep(ev) {
    const d = document.createElement("details");
    d.className = "step run";
    const s = document.createElement("summary");
    const state = document.createElement("span"); state.className = "state"; state.textContent = "\u25CF ";
    const name = document.createElement("span"); name.className = "name"; name.textContent = ev.name;
    const text = document.createElement("span"); text.textContent = " \u2014 " + (ev.summary || "");
    s.append(state, name, text);
    if (ev.risk && ev.risk !== "safe") { const b = document.createElement("span"); b.className = "badge"; b.textContent = ev.risk; s.appendChild(b); }
    const pre = document.createElement("pre"); pre.textContent = "Running...";
    d.append(s, pre);
    activityBox().appendChild(d);
    steps.set(ev.id, { d, state, pre });
    scroll();
  }

  function finishStep(ev) {
    const st = steps.get(ev.id);
    if (!st) return;
    st.d.className = "step " + (ev.ok ? "ok" : "fail");
    st.state.textContent = ev.denied ? "\u2716 denied " : ev.ok ? "\u2714 " : "\u2716 ";
    st.pre.textContent = ev.preview || "";
  }

  // ---------- bots: each run gets its own card, so their steps never mix with the chat's ----------
  const botCards = new Map();
  function botCard(ev) {
    let card = botCards.get(ev.run);
    if (card) return card;
    hideWelcome();
    const box = document.createElement("details"); box.className = "bot-card"; box.open = false;
    box.setAttribute("aria-label", `${ev.bot} is working`);
    const sum = document.createElement("summary");
    const title = document.createElement("span"); title.className = "bot-title"; title.textContent = `${ev.bot} is working: `;
    const task = document.createElement("span"); task.className = "bot-task"; task.textContent = (ev.task || "").slice(0, 160);
    const state = document.createElement("span"); state.className = "bot-state typing"; state.textContent = "";
    const stop = document.createElement("button"); stop.type = "button"; stop.className = "ghost small"; stop.textContent = "Stop";
    stop.setAttribute("aria-label", `Stop ${ev.bot}`);
    stop.addEventListener("click", (e) => { e.preventDefault(); send({ type: "stop_bot", agent: ev.agent_id }); stop.disabled = true; });
    sum.append(title, task, " ", state, " ", stop);
    const steps2 = document.createElement("div"); steps2.className = "bot-steps";
    box.append(sum, steps2);
    chat.appendChild(box);
    card = { box, title, state, stop, steps: steps2, items: new Map(), bot: ev.bot };
    botCards.set(ev.run, card);
    scroll();
    return card;
  }
  function botEvent(ev) {
    if (!["tool_call", "tool_result", "note", "status", "assistant", "error"].includes(ev.type)) return false;
    const card = botCard(ev);
    if (ev.type === "status") { card.state.textContent = ev.text || ""; return true; }
    if (ev.type === "assistant") return true;              // the report comes with bot_done
    if (ev.type === "tool_call") {
      const line = document.createElement("div"); line.className = "step run";
      line.textContent = `\u25CF ${ev.name} \u2014 ${ev.summary || ""}`;
      card.items.set(ev.id, line); card.steps.appendChild(line); return true;
    }
    if (ev.type === "tool_result") {
      const line = card.items.get(ev.id);
      if (line) { line.className = "step " + (ev.ok ? "ok" : "fail"); line.textContent = line.textContent.replace(/^\u25CF/, ev.ok ? "\u2714" : "\u2716"); }
      return true;
    }
    const line = document.createElement("div"); line.className = ev.type === "error" ? "step fail" : "note"; line.textContent = ev.text || "";
    card.steps.appendChild(line);
    return true;
  }
  function botDone(ev) {
    const card = botCard(ev);
    card.title.textContent = `${ev.bot} ${ev.ok === false ? "stopped" : "finished"}: `;
    card.state.textContent = ""; card.state.classList.remove("typing"); card.stop.remove();
    botCards.delete(ev.run);
    const row = document.createElement("div"); row.className = "msg assistant bot-report";
    const bubble = document.createElement("div"); bubble.className = "bubble";
    bubble.innerHTML = renderMarkdown(ev.report || "(no report)");
    const meta = document.createElement("div"); meta.className = "meta"; meta.textContent = `from ${ev.bot}`;
    bubble.appendChild(meta); row.appendChild(bubble);
    card.box.after(row);
    scroll();
  }

  function addNote(text) {
    const n = document.createElement("div");
    n.className = "note";
    n.textContent = text;
    activityBox().appendChild(n);
    scroll();
  }

  function addConfirm(ev) {
    if (document.querySelector(`.confirm[data-id="${CSS.escape(ev.id)}"]`)) return;
    const box = document.createElement("div");
    box.className = "confirm" + (ev.risk === "critical" ? " critical" : "");
    box.dataset.id = ev.id;
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-label", "Approval needed");
    const h = document.createElement("h4");
    h.textContent = (ev.bot ? `${ev.bot} asks: ` : "") + (ev.risk === "critical" ? "Approval needed (sends/posts/submits or can't be undone)" : "Approval needed");
    const pre = document.createElement("pre"); pre.textContent = ev.summary || ev.tool;
    const buttons = document.createElement("div"); buttons.className = "buttons";
    const yes = document.createElement("button"); yes.type = "button"; yes.className = "approve"; yes.textContent = "Approve";
    const no = document.createElement("button"); no.type = "button"; no.className = "deny"; no.textContent = "Deny";
    const answer = (approved) => {
      send({ type: "confirm", id: ev.id, approved });
      buttons.replaceChildren();
      const r = document.createElement("span"); r.className = "result";
      r.textContent = approved ? "\u2714 Approved" : "\u2716 Denied";
      r.style.color = approved ? "var(--ok)" : "var(--bad)";
      buttons.appendChild(r);
    };
    yes.onclick = () => answer(true);
    no.onclick = () => answer(false);
    buttons.append(yes, no);
    box.append(h, pre, buttons);
    activityBox().appendChild(box);
    scroll();
    yes.focus();
  }

  function setBusy(value) {
    busy = value;
    stopBtn.hidden = !value;
    sendBtn.disabled = value;
    if (!value) { statusLine.textContent = ""; statusLine.classList.remove("typing"); }
  }

  // ---------- socket ----------
  function send(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); }

  function connect() {
    if (!token) {
      statusLine.textContent = "Missing access token. Start Karya with start.bat and use the link it opens.";
      return;
    }
    ws = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(token)}&ui=${encodeURIComponent(UI_VERSION)}`);
    ws.onopen = () => { retry = 0; dot.classList.add("on"); loadStatus(); };
    ws.onclose = async () => {
      dot.classList.remove("on");
      if (quitting) return;                       // Quit Karya: it stopped on purpose
      try {
        const r = await fetch(`/api/status?token=${encodeURIComponent(token)}`);
        if (r.status === 401) {
          statusLine.textContent = "This link is not valid for the running Karya. Close this tab and open Karya again with start.bat.";
          return;
        }
      } catch (e) { /* server down: keep retrying */ }
      statusLine.textContent = "Disconnected - reconnecting...";
      setTimeout(connect, Math.min(1000 * 2 ** retry++, 10000));
    };
    ws.onmessage = (msg) => {
      const ev = JSON.parse(msg.data);
      if (ev.run && ev.type !== "confirm" && ev.type !== "ask" && botEvent(ev)) return;
      switch (ev.type) {
        case "history":
          sessionStorage.removeItem("karya-reloads");
          chat.querySelectorAll(".msg,.activity").forEach((n) => n.remove());
          activity = null;
          (ev.messages || []).forEach((m) => addMessage(m.role, m.text));
          if (!(ev.messages || []).length) { const w = $("welcome"); if (w) w.hidden = false; }
          autoMode.checked = !!ev.auto_mode;
          keepGoing.checked = !!ev.keep_going;
          setBusy(!!ev.busy);
          botCards.clear();
          (ev.bots || []).forEach((b) => botCard({ run: b.run, bot: b.bot, agent_id: b.agent_id, task: b.task }));
          break;
        case "bot_started": botCard(ev); loadStatus(); break;
        case "bot_done": botDone(ev); loadStatus(); break;
        case "busy": setBusy(ev.value); break;
        case "user": addMessage("user", ev.text, ev.via ? `from ${ev.via}` : ""); break;
        case "status": statusLine.textContent = ev.text; statusLine.classList.add("typing"); break;
        case "note": addNote(ev.text); break;
        case "tool_call": addStep(ev); statusLine.textContent = `Using ${ev.name}`; break;
        case "tool_result": finishStep(ev); break;
        case "confirm": addConfirm(ev); statusLine.textContent = "Waiting for your approval"; statusLine.classList.remove("typing"); break;
        case "assistant": addMessage("assistant", ev.text, ev.provider ? `via ${ev.provider}` : ""); break;
        case "error": addMessage("error", ev.text); break;
        case "mode": autoMode.checked = !!ev.auto_mode; break;
        case "keep_going": keepGoing.checked = !!ev.on; break;
        case "ask": addAsk(ev); statusLine.textContent = ev.kind === "credentials" || ev.site ? `Waiting for the ${ev.site} login` : "Waiting for your answer"; statusLine.classList.remove("typing"); break;
        case "confirm_done": settleCard(".confirm", ev.id, ev.approved ? "\u2714 Approved" : "\u2716 Denied", ev.approved); break;
        case "ask_done": settleCard(".ask", ev.id, ev.answered ? "\u2714 Saved securely" : "\u2716 Skipped", ev.answered); break;
        case "browser_link": loadStatus(); break;
        case "reload": {
          const n = Number(sessionStorage.getItem("karya-reloads") || 0);
          if (n < 2) { sessionStorage.setItem("karya-reloads", String(n + 1)); location.reload(); }
          else addMessage("error", "Karya was updated. Press F5 to load the new version of this page.");
          break;
        }
      }
    };
  }

  // ---------- status: the header, the notice, Setup and the first-run guide all follow it ----------
  const statusListeners = new Set();
  let lastStatus = null, guideChecked = false;
  const AI_NAMES = { kiro: "Kiro", claude: "Claude", codex: "ChatGPT", groq: "Groq", gemini: "Gemini", openai: "OpenAI",
    anthropic: "Claude API", openrouter: "OpenRouter", deepseek: "DeepSeek", mistral: "Mistral", xai: "Grok",
    together: "Together", cerebras: "Cerebras", ollama: "Offline AI", custom: "Your API" };
  function aiLabel(s) {
    if (!s.ai_ready) return "No AI connected";
    const list = s.providers || [];
    const first = list.find((p) => !p.startsWith("ollama:")) || list[0] || "";
    const [name, ...rest] = first.split(":");
    const model = rest.join(":");
    return `AI: ${AI_NAMES[name] || name}${model && model !== "auto" ? " \u00b7 " + model : ""}`;
  }
  function onStatus(fn) {
    statusListeners.add(fn);
    if (lastStatus) { try { fn(lastStatus); } catch (e) { /* one view failing never breaks the rest */ } }
  }

  async function loadStatus() {
    let s;
    try {
      const r = await fetch(`/api/status?token=${encodeURIComponent(token)}`);
      s = await r.json();
    } catch (e) { providerLabel.textContent = "status unavailable"; return; }
    lastStatus = s;
    providerLabel.textContent = aiLabel(s);
    aiPill.classList.toggle("warn", !s.ai_ready);
    notice.hidden = !!s.ai_ready || !s.onboarded;
    statusListeners.forEach((fn) => { try { fn(s); } catch (e) { /* one view failing never breaks the rest */ } });
    if (!guideChecked) { guideChecked = true; if (!s.onboarded) openGuide(1); }
    clearTimeout(loadStatus.poll);
    if (["starting", "opening", "needs_qr"].includes((s.whatsapp || {}).state)) loadStatus.poll = setTimeout(loadStatus, 4000);
  }

  // ---------- small helpers for the Setup sections ----------
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const button = (text, onClick, cls = "ghost small", label = "") => {
    const b = el("button", cls, text); b.type = "button"; if (label) b.setAttribute("aria-label", label); b.addEventListener("click", onClick); return b;
  };
  const link = (text, href) => { const a = el("a", "", text); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a; };
  async function api(path, body) {
    const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const r = await fetch(`${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  const option = (title, text) => {
    const box = el("div", "option");
    const body = el("div", "option-body");
    box.append(el("h4", "", title), el("p", "hint", text), body);
    return { box, body };
  };
  function fieldRow(labelText, controls, extra) {
    const row = el("div", "field");
    const wrap = el("div", "controls");
    (Array.isArray(controls) ? controls : [controls]).forEach((c) => { if (c) wrap.append(c); });
    if (extra) wrap.append(extra);
    row.append(el("span", "field-label", labelText), wrap);
    return row;
  }
  function toast(text) {
    const t = el("div", "toast", text); t.setAttribute("role", "status");
    document.body.appendChild(t);
    setTimeout(() => t.classList.add("show"), 20);
    setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 400); }, 4000);
  }

  function extensionSteps(folder) {
    const ol = document.createElement("ol"); ol.className = "ext-steps";
    const step = (...nodes) => { const li = document.createElement("li"); li.append(...nodes); ol.appendChild(li); };
    const code = (t) => { const c = document.createElement("code"); c.textContent = t; return c; };
    step("In Chrome, open ", code("chrome://extensions"), " (paste it in the address bar).");
    step("Turn on ", code("Developer mode"), " (top right).");
    const copy = document.createElement("button"); copy.type = "button"; copy.className = "ghost small"; copy.textContent = "Copy folder path";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(folder); copy.textContent = "Copied \u2714"; } catch (e) { copy.textContent = "Copy failed: select the path"; }
    });
    step("Click ", code("Load unpacked"), " and choose this folder: ", code(folder), " ", copy);
    step("Done. It connects by itself whenever Karya is running (this line turns green).");
    return ol;
  }

  function describeCheck(c) {
    if (!c) return "";
    if (!c.ok) return `\u2716 ${c.title || c.provider}: ${c.error || "not working"}`;
    const tpm = c.tokens_per_minute ? ` (${c.tokens_per_minute.toLocaleString()} tokens/min)` : "";
    return `\u2714 ${c.title || c.provider} works with ${c.model}${tpm}: ${c.summary || ""}`;
  }

  // ---------- your AI: one shared view of what's connected (Setup and the guide both show it) ----------
  const AI = {
    ov: null, views: new Set(),
    async refresh() {   // what's connected first (instant), then the sign-in checks (a few seconds)
      try { this.set(await api("/api/ai?logins=0")); } catch (e) { /* the views keep what they had */ }
      try { this.set(await api("/api/ai")); } catch (e) { /* the views keep what they had */ }
    },
    set(ov) { if (!ov) return; this.ov = { ...(this.ov || {}), ...ov }; this.views.forEach((v) => v(this.ov)); },
    patch(name, value) { this.ov = this.ov || {}; this.ov[name] = { ...(this.ov[name] || {}), ...value }; this.views.forEach((v) => v(this.ov)); },
  };
  const modelLists = {};
  const loginTimers = {};
  function pollLogin(name, onDone, onFail) {
    clearInterval(loginTimers[name]);
    const started = Date.now();
    loginTimers[name] = setInterval(async () => {
      if (Date.now() - started > 10 * 60 * 1000) { clearInterval(loginTimers[name]); return; }
      try {
        const d = await api("/api/ai", { action: "login_status", provider: name });
        AI.patch(name, { progress: d });
        if (d.state === "done") { clearInterval(loginTimers[name]); onDone(d); AI.refresh(); loadStatus(); }
        else if (d.state === "failed" || d.state === "idle") { clearInterval(loginTimers[name]); onFail(d); AI.refresh(); }
      } catch (e) { clearInterval(loginTimers[name]); onFail({ error: e.message }); }
    }, 2500);
  }

  function connectBox(opts = {}) {
    const box = el("section", "connect-box"); box.setAttribute("aria-label", "Connect your AI");
    const msg = el("div", "form-msg"); msg.setAttribute("role", "status");
    const list = el("div", "ai-list");
    const say = (text) => { msg.replaceChildren(); if (text) msg.appendChild(el("div", "", text)); };
    const showChecks = (checks, first) => {
      say(first || "");
      (checks || []).forEach((c) => msg.appendChild(el("div", c.ok ? "ok" : "missing", describeCheck(c))));
    };
    const fail = (e) => say("\u2716 " + ((e && (e.message || e.error)) || e));
    const done = (d, text) => { showChecks((d || {}).checks, text); AI.set((d || {}).overview); AI.refresh(); loadStatus(); };
    const connected = () => (((AI.ov || {}).connected) || []).map((r) => r.name);

    // paste any key
    const keyOpt = option("Paste an AI key", "OpenAI, Claude, Gemini, Groq, OpenRouter, DeepSeek, Mistral, xAI or Kiro: Karya works out which one it is.");
    const keyForm = el("form", "key-form"); keyForm.autocomplete = "off";
    const keyInput = el("input"); keyInput.type = "password"; keyInput.autocomplete = "off"; keyInput.spellcheck = false;
    keyInput.placeholder = "Paste your key here"; keyInput.setAttribute("aria-label", "Paste an AI key");
    const keyBtn = el("button", "", "Connect"); keyBtn.type = "submit";
    keyForm.append(keyInput, keyBtn);
    const choose = el("div", "choose-service"); choose.hidden = true;
    const free = el("p", "hint");
    free.append("No key yet? Free ones: ", link("Gemini", "https://aistudio.google.com/apikey"), " \u00b7 ",
      link("Groq", "https://console.groq.com/keys"), " \u00b7 ", link("OpenRouter", "https://openrouter.ai/keys"));
    keyOpt.body.append(keyForm, choose, free);
    const sendKey = async (service) => {
      const key = keyInput.value.trim();
      if (!key) { keyInput.focus(); return; }
      choose.hidden = true; say("Checking the key...");
      try {
        const d = await api("/api/ai", { action: "key", key, service: service || "" });
        if (d.choose) {
          const titles = {}; (((AI.ov || {}).services) || []).forEach((s) => { titles[s.name] = s.title; });
          choose.replaceChildren(el("span", "", "Which service is this key from? "));
          d.choose.forEach((name) => choose.append(button(titles[name] || name, () => sendKey(name), "ghost small")));
          choose.hidden = false; say(""); return;
        }
        keyInput.value = "";
        done(d, `\u2714 ${d.title} connected.`);
      } catch (e) { fail(e); }
    };
    keyForm.addEventListener("submit", (e) => { e.preventDefault(); sendKey(""); });

    // plans you already pay for, and the free offline model
    const kiro = option("Kiro", "Your Kiro plan. Sign in with Google, GitHub or AWS Builder ID: no key needed.");
    const claude = option("Claude", "Your Claude plan (Pro, Max or Team) through Claude Code.");
    const gpt = option("ChatGPT", "Your ChatGPT plan (Plus, Pro or Business) through the Codex app.");
    const offline = option("Free, on this PC", "A small AI that runs on your PC with Ollama: free and private, slow without a graphics card.");
    const progressLine = (p) => {
      const line = el("div", "hint");
      if (!p || p.state !== "waiting") return line;
      line.append("Finish signing in on the page that opened in your browser. ");
      if (p.code) line.append("Check it shows this code: ", el("strong", "", p.code), ". ");
      if (p.url) line.append(link("Open the sign-in page", p.url));
      return line;
    };
    const startLogin = (name, method, label) => async () => {
      say(`Opening the ${label} sign-in page...`);
      try {
        AI.patch(name, { progress: await api("/api/ai", { action: "login", provider: name, method: method || "" }) });
        say("");
        pollLogin(name, (d) => showChecks(d.checks, `\u2714 Signed in. ${label} is now Karya's first AI.`),
          (d) => say(`\u2716 Sign-in didn't finish${d.error ? ": " + d.error : ""}. Try again.`));
      } catch (e) { fail(e); }
    };
    const use = (action, label) => async () => {
      say(`Connecting ${label}...`);
      try { done(await api("/api/ai", { action }), `\u2714 ${label} is connected.`); } catch (e) { fail(e); }
    };
    const checking = (b) => b.append(el("span", "hint", "Checking..."));
    function fillKiro(ov) {
      const info = ov.kiro || {}, b = kiro.body;
      b.replaceChildren();
      if (!ov.kiro && !connected().includes("kiro")) checking(b);
      else if (connected().includes("kiro")) b.append(el("span", "ok", info.key ? "\u2714 Connected with your Kiro key" : "\u2714 Signed in"));
      else if (info.installed === false) b.append("First install the Kiro CLI: ", link("kiro.dev/downloads", "https://kiro.dev/downloads"));
      else {
        const row = el("div", "option-actions");
        [["google", "Google"], ["github", "GitHub"], ["builder", "AWS Builder ID"]].forEach(([m, label]) =>
          row.append(button(label, startLogin("kiro", m, "Kiro"), "small", `Sign in to Kiro with ${label}`)));
        b.append(row);
      }
      b.append(progressLine(info.progress));
    }
    function fillClaude(ov) {
      const info = ov.claude || {}, b = claude.body;
      b.replaceChildren();
      if (!ov.claude && !connected().includes("claude")) checking(b);
      else if (connected().includes("claude")) b.append(el("span", "ok", "\u2714 Connected"));
      else if (info.installed === false) b.append("First install Claude Code: ", link("claude.com/claude-code", "https://claude.com/claude-code"));
      else if (info.signed_in) {
        b.append(button("Use Claude", use("use_claude", "Claude"), "small", "Use Claude"),
          el("span", "hint", ` Claude Code is signed in on this PC (${info.method || "signed in"}).`));
      } else b.append(button("Sign in with Claude", startLogin("claude", "", "Claude"), "small", "Sign in with Claude"));
      b.append(progressLine(info.progress));
    }
    function fillGpt(ov) {
      const info = ov.codex || {}, b = gpt.body;
      b.replaceChildren();
      if (!ov.codex && !connected().includes("codex")) checking(b);
      else if (connected().includes("codex")) b.append(el("span", "ok", "\u2714 Connected"));
      else if (info.installed === false) {
        const cmd = "npm install -g @openai/codex";
        b.append("First install the Codex app (needs ", link("Node.js", "https://nodejs.org"), "): ", el("code", "", cmd), " ",
          button("Copy", async (e) => { try { await navigator.clipboard.writeText(cmd); e.target.textContent = "Copied \u2714"; } catch (err) { e.target.textContent = "Select and copy"; } }, "ghost small", "Copy the install command"));
      } else if (info.signed_in) {
        b.append(button("Use my ChatGPT plan", use("use_codex", "Your ChatGPT plan"), "small", "Use my ChatGPT plan"),
          el("span", "hint", " Codex is already signed in on this PC."));
      } else {
        const row = el("div", "option-actions");
        row.append(button("Sign in with ChatGPT", startLogin("codex", "", "ChatGPT"), "small", "Sign in with ChatGPT"),
          button("use a code instead", startLogin("codex", "device", "ChatGPT"), "ghost small", "Sign in to ChatGPT with a code"));
        b.append(row);
      }
      b.append(progressLine(info.progress));
    }
    let ollamaTimer = null;
    function pollOllama() {
      clearInterval(ollamaTimer);
      ollamaTimer = setInterval(async () => {
        try {
          const st = await api("/api/ai", { action: "ollama_status" });
          AI.set({ ollama: st });
          const state = (st.download || {}).state;
          if (state !== "downloading") {
            clearInterval(ollamaTimer);
            if (state === "done") { say("\u2714 The offline AI is ready."); AI.refresh(); loadStatus(); }
          }
        } catch (e) { clearInterval(ollamaTimer); }
      }, 2500);
    }
    function fillOffline(ov) {
      const info = ov.ollama || {}, b = offline.body, dl = info.download || {};
      b.replaceChildren();
      if (!ov.ollama) checking(b);
      else if (info.ready && connected().includes("ollama")) b.append(el("span", "ok", `\u2714 Ready (${info.model}). Karya uses it when your other AIs are busy, or on its own.`));
      else if (info.ready) b.append(button("Use it", use("use_ollama", "The offline AI"), "small", "Use the offline AI"));
      else if (dl.state === "downloading") b.append(el("span", "", `Downloading the model: ${dl.percent || 0}% (${dl.detail || "working"})...`));
      else if (info.installed === false) {
        b.append("First install Ollama (free): ", link("ollama.com/download", "https://ollama.com/download"), " ",
          button("Check again", () => AI.refresh(), "ghost small", "Check for Ollama again"));
      } else if (info.installed) {
        b.append(button("Download the free model (about 3 GB)", async () => {
          try { AI.set({ ollama: await api("/api/ai", { action: "ollama_download" }) }); pollOllama(); } catch (e) { fail(e); }
        }, "small", "Download the free offline model"));
        if (dl.state === "failed") b.append(el("div", "missing", `\u2716 ${dl.error || "The download stopped."} Try again.`));
      }
    }
    function modelPicker(r) {
      const pick = el("select"); pick.setAttribute("aria-label", `Model for ${r.title}`);
      const add = (id, label) => { const o = el("option", "", label || id); o.value = id; pick.appendChild(o); return o; };
      add(r.model, r.model);
      pick.value = r.model;
      modelLists[r.name] = modelLists[r.name] || api("/api/ai", { action: "models", provider: r.name }).catch(() => ({}));
      modelLists[r.name].then((d) => {
        (d.models || []).forEach((m) => { if (m.id !== r.model) add(m.id, m.description ? `${m.name}: ${m.description}` : m.name); });
        add("__other", "Another model (type its name)...");
      });
      pick.addEventListener("change", async () => {
        let model = pick.value;
        if (model === "__other") { model = (prompt(`Model name for ${r.title}:`) || "").trim(); if (!model) { pick.value = r.model; return; } }
        say(`Switching ${r.title} to ${model}...`);
        try { done(await api("/api/ai", { action: "model", provider: r.name, model }), `\u2714 ${r.title} now uses ${model}.`); }
        catch (e) { fail(e); pick.value = r.model; }
      });
      return pick;
    }
    function renderList(ov) {
      const offlineReady = !!(ov.ollama || {}).ready;
      // the offline AI is listed only once its model is downloaded (the "Free, on this PC" card covers the rest)
      const rows = (ov.connected || []).filter((r) => r.name !== "ollama" || offlineReady);
      list.replaceChildren();
      if (!ov.connected) { list.appendChild(el("p", "hint", "Checking your AI...")); return; }
      if (!rows.length) list.appendChild(el("p", "missing", "\u26A0 No AI connected yet. Choose one below."));
      rows.forEach((r, i) => {
        const row = el("div", "ai-row");
        row.append(el("span", "ai-name", `${i + 1}. ${r.title}`),
          el("span", "ai-how", r.how === "sign-in" ? "signed in" : r.how === "offline" ? "on this PC" : r.how));
        row.appendChild(modelPicker(r));
        if (i > 0) row.appendChild(button("Use first", async () => {
          try { const d = await api("/api/ai", { action: "first", provider: r.name }); AI.set(d.overview); loadStatus(); say(`\u2714 ${r.title} goes first now.`); } catch (e) { fail(e); }
        }, "ghost small", `Use ${r.title} first`));
        row.appendChild(button("Remove", async () => {
          if (!confirm(`Remove ${r.title} from Karya?`)) return;
          try { const d = await api("/api/ai", { action: "remove", provider: r.name }); AI.set(d.overview); AI.refresh(); loadStatus(); say(`Removed ${r.title}.`); } catch (e) { fail(e); }
        }, "ghost small", `Remove ${r.title}`));
        const check = lastStatus && lastStatus.checks && lastStatus.checks[r.name];
        if (check) row.append(el("span", "ai-check " + (check.ok ? "ok" : "missing"), check.ok ? "\u2714 works" : `\u2716 ${check.error || "not working"}`));
        list.appendChild(row);
      });
    }
    const render = (ov) => { renderList(ov); fillKiro(ov); fillClaude(ov); fillGpt(ov); fillOffline(ov); };
    const options = el("div", "options");
    options.append(keyOpt.box, kiro.box, claude.box, gpt.box, offline.box);
    if (opts.guide) box.append(list, options, msg);
    else box.append(el("h3", "", "Your AI"), el("p", "hint", "Karya thinks with these, in this order, and moves to the next one when one is busy or out of credits."),
      list, el("h3", "", "Add an AI"), el("p", "hint", "One is enough. A free key takes two minutes; a plan you already pay for needs only a sign-in."), options, msg);
    AI.views.add(render);
    render(AI.ov || {});
    AI.refresh();
    return box;
  }

  // ---------- settings forms (each tab saves only its own fields) ----------
  let settingsData = null;
  const loadSettings = () => settingsData || (settingsData = fetch(`/api/settings?token=${encodeURIComponent(token)}`).then((r) => r.json()));
  function fillValues(data, inputs, secrets, defaults) {
    const values = (data && data.values) || {};
    Object.entries(inputs).forEach(([key, input]) => {
      const v = values[key] || {};
      if (input.tagName === "SELECT") {
        input.value = [...input.options].some((o) => o.value === v.value) ? v.value : ((defaults || {})[key] || input.options[0].value);
        return;
      }
      if (v.value) input.value = v.value;
      if (secrets.has(key) && v.set) input.placeholder = "saved \u2714 (type to replace)";
    });
  }
  const submitSettings = (inputs, secrets, msg, after) => async (e) => {
    e.preventDefault();
    const body = {};
    Object.entries(inputs).forEach(([key, input]) => { const v = input.value.trim(); if (v || !secrets.has(key)) body[key] = v; });
    msg.textContent = "Saving...";
    try {
      const r = await fetch(`/api/settings?token=${encodeURIComponent(token)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const d = await r.json();
      if (!r.ok) { msg.textContent = "Error: " + (d.error || r.status); return; }
      msg.replaceChildren(el("div", "ok", "Saved \u2714"));
      (d.checks || []).forEach((c) => msg.appendChild(el("div", c.ok ? "ok" : "missing", describeCheck(c))));
      secrets.forEach((key) => { if (inputs[key].value) { inputs[key].value = ""; inputs[key].placeholder = "saved \u2714 (type to replace)"; } });
      settingsData = null;
      if (after) after(d);
      loadStatus();
    } catch (err) { msg.textContent = "Error: " + err; }
  };
  function settingsForm(title, intro, fields) {
    const box = el("form", "settings"); box.autocomplete = "off";
    if (title) box.append(el("h3", "", title));
    if (intro) box.append(el("p", "hint", intro));
    const inputs = {}, secrets = new Set(), defaults = {};
    const rows = el("div", "fields");
    fields.forEach((f) => {
      let c;
      if (f.options) {
        c = el("select");
        f.options.forEach(([v, t]) => { const o = el("option", "", t); o.value = v; c.appendChild(o); });
        defaults[f.key] = f.def;
      } else {
        c = el("input"); c.type = f.secret ? "password" : "text"; c.autocomplete = "off"; c.spellcheck = false;
        c.placeholder = f.placeholder || (f.secret ? "not set" : "");
      }
      c.setAttribute("aria-label", f.label);
      inputs[f.key] = c;
      if (f.secret) secrets.add(f.key);
      rows.appendChild(fieldRow(f.label, c, f.link ? link(f.link[0], f.link[1]) : null));
    });
    const save = el("button", "", "Save"); save.type = "submit";
    const msg = el("div", "form-msg"); msg.setAttribute("role", "status");
    const actions = el("div", "form-actions"); actions.append(save, msg);
    box.append(rows, actions);
    loadSettings().then((d) => fillValues(d, inputs, secrets, defaults)).catch(() => {});
    box.addEventListener("submit", submitSettings(inputs, secrets, msg));
    return box;
  }
  function providersForm() {
    const fold = el("details", "advanced");
    fold.append(el("summary", "", "Every AI service, one by one (advanced: keys, models, order)"));
    const box = el("form", "settings"); box.autocomplete = "off";
    const prov = el("div", "providers");
    const inputs = {}, secrets = new Set();
    const mk = (key, secret, placeholder, label, cls) => {
      const i = el("input"); i.type = secret ? "password" : "text"; i.autocomplete = "off"; i.spellcheck = false;
      i.placeholder = placeholder || (secret ? "not set" : ""); i.setAttribute("aria-label", label);
      if (cls) i.className = cls;
      inputs[key] = i; if (secret) secrets.add(key);
      return i;
    };
    loadSettings().then((data) => {
      (data.providers || []).forEach((p) => {
        prov.appendChild(fieldRow(p.title, [mk(p.key_env, true, "API key", `${p.title} API key`),
          mk(p.model_env, false, p.default_model === "auto" ? "model: auto" : `model: ${p.default_model}`, `${p.title} model (optional)`, "model"),
          link(p.free ? "get key (free plan)" : "get key", p.signup)]));
      });
      prov.appendChild(fieldRow("Any other OpenAI-compatible API", [mk("CUSTOM_BASE_URL", false, "https://your-endpoint/v1", "Custom base URL"),
        mk("CUSTOM_API_KEY", true, "API key", "Custom API key"), mk("CUSTOM_MODEL", false, "model name", "Custom model", "model")]));
      prov.appendChild(fieldRow("Order to use them in", [mk("LLM_PROVIDERS", false, "optional, e.g. anthropic,groq,ollama", "Provider order")]));
      fillValues(data, inputs, secrets);
    }).catch(() => {});
    const save = el("button", "", "Save & check keys"); save.type = "submit";
    const msg = el("div", "form-msg"); msg.setAttribute("role", "status");
    const actions = el("div", "form-actions"); actions.append(save, msg);
    box.append(prov, actions);
    box.addEventListener("submit", submitSettings(inputs, secrets, msg, () => { Object.keys(modelLists).forEach((k) => delete modelLists[k]); AI.refresh(); }));
    fold.appendChild(box);
    return fold;
  }

  // ---------- about you ----------
  const PROFILE_FIELDS = [
    ["name", "Your name", "e.g. Asha Rao", "text", "name"],
    ["email", "Your email", "e.g. asha@example.com", "email", "email"],
    ["location", "City and country", "e.g. Pune, India", "text", "address-level2"],
    ["phone", "Phone number (for job forms and WhatsApp)", "e.g. +91 98765 43210", "tel", "tel"],
    ["target_roles", "Jobs you want (optional)", "e.g. product manager, business analyst", "text", "off"],
  ];
  function profileBox(opts = {}) {
    const box = el("form", "profile-box"); box.autocomplete = "on";
    const inputs = {};
    const rows = el("div", "fields");
    PROFILE_FIELDS.forEach(([key, label, placeholder, type, auto]) => {
      const i = el("input"); i.type = type; i.placeholder = placeholder; i.autocomplete = auto; i.setAttribute("aria-label", label);
      inputs[key] = i;
      rows.appendChild(fieldRow(label, i));
    });
    const msg = el("div", "form-msg"); msg.setAttribute("role", "status");
    if (!opts.guide) box.append(el("h3", "", "About you"), el("p", "hint", "Karya fills these in on job applications and emails. They stay on this PC."));
    box.append(rows);
    const save = async () => {
      const profile = {};
      Object.entries(inputs).forEach(([k, i]) => { if (i.value.trim()) profile[k] = i.value.trim(); });
      if (!Object.keys(profile).length) return true;
      try { await api("/api/onboarding", { action: "profile", profile }); msg.textContent = "\u2714 Saved."; loadStatus(); return true; }
      catch (e) { msg.textContent = "\u2716 " + e.message; return false; }
    };
    if (!opts.guide) {
      const b = el("button", "", "Save"); b.type = "submit";
      const actions = el("div", "form-actions"); actions.append(b, msg);
      box.append(actions);
    } else box.append(msg);
    box.addEventListener("submit", async (e) => { e.preventDefault(); await save(); });
    api("/api/onboarding").then((st) => Object.entries(st.profile || {}).forEach(([k, v]) => { if (inputs[k] && !inputs[k].value) inputs[k].value = v; })).catch(() => {});
    box.save = save;
    return box;
  }
  function resumeBox() {
    const box = el("div", "resume-box");
    const state = el("p", "resume-state");
    const file = el("input"); file.type = "file"; file.accept = ".pdf,.docx,.txt,.md"; file.hidden = true;
    file.setAttribute("aria-label", "Your resume file");
    const drop = el("div", "drop");
    drop.append(button("Choose your resume", () => file.click(), "", "Choose your resume file"), el("span", "hint", " or drop a PDF or Word file here"));
    const msg = el("div", "form-msg"); msg.setAttribute("role", "status");
    let timer = null;
    const show = (st) => {
      const imp = st.resume_import || {};
      if (imp.state === "reading") state.textContent = "Reading your resume...";
      else if (imp.state === "done") state.textContent = "\u2714 Karya read your resume: it knows your experience, skills and projects.";
      else if (imp.state === "later") state.textContent = "\u2714 Saved. " + (imp.error || "");
      else if (imp.state === "failed") state.textContent = "\u2716 " + (imp.error || "Karya couldn't read it.");
      else state.textContent = st.resume ? `\u2714 Your resume: ${st.resume_file || "saved"}` : "No resume yet. Karya tailors it for every job you apply to.";
      state.className = "resume-state " + (imp.state === "failed" ? "missing" : (st.resume || imp.state === "later") ? "ok" : "");
      clearTimeout(timer);
      if (imp.state === "reading") timer = setTimeout(refresh, 2000);
    };
    const refresh = () => api("/api/onboarding").then(show).catch(() => {});
    const upload = (f) => {
      if (!f) return;
      if (f.size > 8 * 1024 * 1024) { msg.textContent = "\u2716 That file is bigger than 8 MB."; return; }
      const reader = new FileReader();
      reader.onload = async () => {
        msg.textContent = `Uploading ${f.name}...`;
        try {
          show(await api("/api/onboarding", { action: "resume", filename: f.name, data: String(reader.result).split(",")[1] || "" }));
          msg.textContent = ""; loadStatus();
        } catch (e) { msg.textContent = "\u2716 " + e.message; }
      };
      reader.readAsDataURL(f);
    };
    file.addEventListener("change", () => upload(file.files[0]));
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files[0]); });
    box.append(el("h4", "", "Your resume"), state, drop, file, msg);
    refresh();
    return box;
  }

  function phoneBox() {
    const box = el("section", "phone-box"); box.setAttribute("aria-label", "Give Karya tasks from WhatsApp");
    const state = el("p", "phone-state");
    const num = el("input"); num.type = "tel"; num.autocomplete = "tel";
    num.placeholder = "Your WhatsApp number with country code (only if Karya doesn't know it)";
    num.setAttribute("aria-label", "Your WhatsApp number with country code");
    const msg = el("div", "form-msg"); msg.setAttribute("role", "status");
    const go = button("Connect WhatsApp", async () => {
      go.disabled = true; msg.textContent = "Opening WhatsApp Web in its own window...";
      try { box.update(await api("/api/phone", { action: "connect", number: num.value.trim() })); msg.textContent = ""; loadStatus(); }
      catch (e) { msg.textContent = "\u2716 " + e.message; }
      go.disabled = false;
    }, "", "Connect WhatsApp");
    const off = button("Disconnect", async () => {
      try { box.update(await api("/api/phone", { action: "disconnect" })); loadStatus(); } catch (e) { msg.textContent = "\u2716 " + e.message; }
    }, "ghost small", "Disconnect WhatsApp");
    const row = el("div", "phone-row"); row.append(num, go, off);
    // WhatsApp opens by itself only when the user wants that; otherwise only when they open it
    const atStart = el("label", "toggle-row");
    const atStartBox = el("input"); atStartBox.type = "checkbox"; atStartBox.setAttribute("aria-label", "Open WhatsApp when Karya starts");
    atStart.append(atStartBox, " Open WhatsApp when Karya starts, and again if its window closes. When this is off, WhatsApp opens only when you click Open WhatsApp, or ask Karya or your AI app to connect WhatsApp");
    let savingAtStart = false;
    atStartBox.addEventListener("change", async () => {
      savingAtStart = true;
      try {
        await api("/api/settings", { WHATSAPP_AT_START: atStartBox.checked ? "true" : "false" });
        msg.textContent = atStartBox.checked ? "WhatsApp now opens by itself when Karya starts." : "WhatsApp now opens only when you open it.";
      } catch (e) { msg.textContent = "\u2716 " + e.message; atStartBox.checked = !atStartBox.checked; }
      savingAtStart = false;
    });
    const auto = el("label", "toggle-row");
    const autoBox = el("input"); autoBox.type = "checkbox"; autoBox.setAttribute("aria-label", "Start Karya when I sign in to Windows");
    auto.append(autoBox, " Start Karya when I sign in to Windows, so WhatsApp tasks and bots work without opening Karya (it waits minimized in the taskbar)");
    autoBox.addEventListener("change", async () => {
      try {
        const d = await api("/api/autostart", { on: autoBox.checked }); autoBox.checked = !!d.on;
        msg.textContent = d.on ? "Karya now starts by itself when you sign in to Windows." : "Karya won't start with Windows.";
      } catch (e) { msg.textContent = "\u2716 " + e.message; autoBox.checked = !autoBox.checked; }
    });
    box.setAutostart = (st) => { auto.hidden = !(st && st.supported); autoBox.checked = !!(st && st.on); };
    box.update = (wa) => {
      const st = wa.state || "off";
      const text = {
        off: wa.detail || "Not linked yet.",
        starting: "Opening WhatsApp Web...",
        opening: "Opening your \"Message yourself\" chat...",
        needs_qr: "Scan the code in the WhatsApp window: on your phone open WhatsApp > Settings > Linked devices > Link a device.",
        ready: "\u2714 Linked. Message yourself on WhatsApp to give Karya a task. STATUS, STOP and HELP work too.",
        error: "\u2716 " + (wa.detail || "WhatsApp didn't start."),
      }[st] || wa.detail || st;
      state.textContent = text; state.className = "phone-state " + (st === "ready" ? "ok" : st === "error" ? "missing" : "");
      num.hidden = !(st === "off" || st === "error") || !!wa.opened_before; go.hidden = !(st === "off" || st === "error"); off.hidden = st === "off";
      go.textContent = wa.opened_before ? "Open WhatsApp" : "Connect WhatsApp"; go.setAttribute("aria-label", go.textContent);
      if (!savingAtStart && "at_start" in wa) atStartBox.checked = !!wa.at_start;
    };
    box.append(el("h3", "", "Tasks from your phone (WhatsApp)"),
      el("p", "hint", "Message yourself on WhatsApp (your own \"Message yourself\" chat). Karya does the task, asks you there before it sends, posts or submits, and reports back. Only that chat is read, and passwords never go over WhatsApp. Karya reads WhatsApp only while its WhatsApp window is open. To take tasks from your phone after a restart, turn on both options below."),
      state, row, atStart, auto, msg);
    box.update({ state: "off" });
    onStatus((s) => { box.update(s.whatsapp || {}); box.setAutostart(s.autostart); });
    return box;
  }

  function agentsBox() {
    const box = el("section", "agents-box"); box.setAttribute("aria-label", "Your bots");
    const list = el("ul", "agent-list");
    const msg = el("div", "form-msg"); msg.setAttribute("role", "status");
    const act = async (body) => {
      try { const d = await api("/api/agents", body); box.update(d.agents || []); if (d.note) msg.textContent = d.note; return true; }
      catch (e) { msg.textContent = "\u2716 " + e.message; return false; }
    };
    box.update = (agents) => {
      list.replaceChildren();
      if (!agents.length) { list.appendChild(el("li", "hint", "No bots yet. Make one below, or ask Karya in the chat.")); return; }
      agents.forEach((a) => {
        const li = el("li");
        const head = el("div", "agent-title", `${a.name} \u00b7 ${a.when}${a.enabled ? "" : " (paused)"}`);
        if (a.working) head.append(" ", el("span", "ok", `\u25CF working: ${(a.doing || "").slice(0, 90)}`));
        const give = el("form", "give-task"); give.autocomplete = "off";
        const input = el("input"); input.placeholder = `Give ${a.name} a task...`; input.setAttribute("aria-label", `Task for ${a.name}`);
        const go = el("button", "small", "Give task"); go.type = "submit";
        give.append(input, go);
        give.addEventListener("submit", async (e) => {
          e.preventDefault(); const task = input.value.trim(); if (!task) { input.focus(); return; }
          if (await act({ action: "assign", agent: a.id, task })) input.value = "";
        });
        const actions = el("div", "agent-actions");
        if (a.working) actions.append(button("Stop", () => act({ action: "stop", agent: a.id }), "ghost small", `Stop ${a.name}`));
        if (!a.working) actions.append(button("Do its job now", () => act({ action: "run", agent: a.id }), "ghost small", `Run ${a.name} now`));
        if (a.when !== "works when you give it a task") actions.append(button(a.enabled ? "Pause schedule" : "Resume schedule",
          () => act({ action: a.enabled ? "pause" : "resume", agent: a.id }), "ghost small", `${a.enabled ? "Pause" : "Resume"} ${a.name}`));
        actions.append(button("Delete", () => { if (confirm(`Delete the bot "${a.name}" and its notes?`)) act({ action: "delete", agent: a.id }); }, "ghost small", `Delete ${a.name}`));
        li.append(head, el("div", "hint", a.task),
          el("div", "hint", (a.enabled && a.next_run && a.when !== "works when you give it a task" ? `Next run: ${a.next_run}` : "") +
            (a.last_report ? `${a.enabled && a.when !== "works when you give it a task" ? " \u00b7 " : ""}Last report: ${a.last_report}` : "")),
          give, actions);
        list.appendChild(li);
      });
    };
    // make a bot
    const make = el("form", "make-bot"); make.autocomplete = "off";
    const name = el("input"); name.placeholder = "Name, e.g. Maya"; name.setAttribute("aria-label", "Bot name"); name.maxLength = 40;
    const job = el("input"); job.placeholder = "Its job, e.g. find product manager jobs in India that fit me and apply"; job.setAttribute("aria-label", "What the bot does");
    const when = el("select"); when.setAttribute("aria-label", "When it works");
    [["on_demand", "When I give it a task"], ["daily", "Every day at..."], ["every", "Every few hours"]].forEach(([v, label]) => { const o = el("option", "", label); o.value = v; when.appendChild(o); });
    const time = el("input"); time.type = "time"; time.value = "09:00"; time.setAttribute("aria-label", "Time of day"); time.hidden = true;
    const hours = el("input"); hours.type = "number"; hours.min = "1"; hours.max = "24"; hours.value = "4"; hours.setAttribute("aria-label", "Every how many hours"); hours.hidden = true;
    when.addEventListener("change", () => { time.hidden = when.value !== "daily"; hours.hidden = when.value !== "every"; });
    const add = el("button", "", "Make bot"); add.type = "submit";
    make.append(name, job, when, time, hours, add);
    make.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!name.value.trim() || !job.value.trim()) { (name.value.trim() ? job : name).focus(); return; }
      if (await act({ action: "create", name: name.value.trim(), task: job.value.trim(), when: when.value, time: time.value, hours: hours.value })) {
        name.value = ""; job.value = "";
      }
    });
    // keep the PC awake
    const awake = el("label", "toggle-row");
    const awakeBox = el("input"); awakeBox.type = "checkbox";
    awake.append(awakeBox, " Keep this PC awake while bots have a schedule or are working, or WhatsApp is linked (the screen can still turn off; closing the lid can still sleep)");
    awakeBox.addEventListener("change", async () => {
      try { await api("/api/settings", { KARYA_KEEP_AWAKE: awakeBox.checked ? "true" : "false" }); msg.textContent = awakeBox.checked ? "The PC stays awake for your bots." : "Normal sleep settings."; }
      catch (e) { msg.textContent = "\u2716 " + e.message; awakeBox.checked = !awakeBox.checked; }
    });
    box.setAwake = (on) => { awakeBox.checked = !!on; };
    box.append(el("h3", "", "Your bots"),
      el("p", "hint", "Named helpers that work in the background while you keep chatting, like Grok Bots or Dots. A bot can do anything Karya can. Give one a task here, or write \"@Name task\" in the chat or on WhatsApp. It reports back and remembers what it did."),
      list, el("h4", "", "Make a bot"), make, awake, msg);
    box.update([]);
    onStatus((s) => { box.update(s.agents || []); box.setAwake(s.keep_awake); });
    return box;
  }

  function mcpBox() {
    const box = document.createElement("div"); box.className = "mcp-box";
    const h = document.createElement("h3"); h.textContent = "Add Karya to your AI apps";
    const intro = document.createElement("p"); intro.className = "hint";
    intro.textContent = "One click adds Karya to an app on this PC. Restart the app afterwards, then ask it things like \"use Karya to find PM jobs and apply\". " +
      "Karya still checks everything and asks you (in Karya's window, or inside the app) before it sends, posts, submits or pays.";
    const body = document.createElement("details"); body.className = "advanced";
    const bodySum = document.createElement("summary"); bodySum.textContent = "Add it by hand (other apps, or if a button doesn't work)";
    body.appendChild(bodySum);
    const appsList = document.createElement("ul"); appsList.className = "mcp-apps";
    const appsMsg = document.createElement("div"); appsMsg.className = "form-msg"; appsMsg.setAttribute("role", "status");
    box.append(h, intro, appsList, appsMsg, body);
    const loadApps = async () => {
      try {
        const d = await api("/api/mcp/apps");
        appsList.replaceChildren();
        (d.apps || []).filter((a) => a.installed || a.state !== "no").forEach((a) => {
          const li = el("li");
          const said = { connected: "\u2714 Karya added", other: "has Karya from another folder", no: "not added yet",
            unreadable: "its config file has comments or errors, so Karya won't touch it: add it by hand below" }[a.state] || a.state;
          li.append(el("span", "", `${a.name}: `), el("span", a.state === "connected" ? "ok" : "", said));
          if (a.state === "no" || a.state === "other") {
            li.append(" ", button(a.state === "other" ? "Use this Karya" : "Add Karya", async (e) => {
              e.target.disabled = true; appsMsg.textContent = `Adding Karya to ${a.name}...`;
              try { const r = await api("/api/mcp/apps", { app: a.id }); appsMsg.textContent = `\u2714 Added to ${a.name}. ${r.next || ""}`; }
              catch (err) { appsMsg.textContent = "\u2716 " + err.message; }
              loadApps();
            }, "small", `Add Karya to ${a.name}`));
          }
          appsList.appendChild(li);
        });
        if (!appsList.children.length) appsList.appendChild(el("li", "hint", "No AI apps found on this PC. For others, use the steps below."));
      } catch (e) { appsList.replaceChildren(el("li", "missing", "Couldn't check your AI apps: " + e.message)); }
    };
    loadApps();
    const block = (title, text) => {
      const wrap = document.createElement("div"); wrap.className = "mcp-snippet";
      const t = document.createElement("div"); t.className = "mcp-title"; t.textContent = title;
      const pre = document.createElement("pre"); pre.textContent = text;
      const copy = document.createElement("button"); copy.type = "button"; copy.className = "ghost small"; copy.textContent = "Copy";
      copy.setAttribute("aria-label", `Copy: ${title}`);
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(text); copy.textContent = "Copied \u2714"; } catch (e) { copy.textContent = "Select and copy"; }
        setTimeout(() => { copy.textContent = "Copy"; }, 2500);
      });
      wrap.append(t, pre, copy); body.appendChild(wrap);
    };
    fetch(`/api/mcp/setup?token=${encodeURIComponent(token)}`).then((r) => r.json()).then((d) => {
      const files = document.createElement("ul"); files.className = "mcp-files";
      (d.files || []).forEach(([app, path]) => { const li = document.createElement("li"); li.textContent = `${app}: ${path}`; files.appendChild(li); });
      const p = document.createElement("p"); p.textContent = "Apps with a config file - add this to the file:";
      body.append(p, files);
      block("Claude Desktop, Cursor, Kiro, Windsurf, OpenClaw (JSON)", d.standard || "");
      block("VS Code (.vscode/mcp.json)", d.vscode || "");
      (d.cli || []).forEach(([name, cmd]) => block(`${name} (run once in a terminal)`, cmd));
      block("Codex config.toml (gives you time to approve)", d.codex_toml || "");
      const status = document.createElement("p"); status.className = "hint";
      status.textContent = (d.enabled ? `On: ${d.default_tools} tools shared by default.` : "Switched off (see the settings above).") +
        (d.last_client && d.last_client.name ? ` Last used by ${d.last_client.name} at ${d.last_client.time}.` : "");
      body.appendChild(status);
    }).catch(() => { body.textContent = "Couldn't load the MCP settings."; });
    return box;
  }

  function accountsBox() {
    const box = document.createElement("div"); box.className = "accounts";
    const h = document.createElement("h3"); h.textContent = "Saved logins";
    const note = document.createElement("p"); note.className = "hint";
    note.textContent = "Passwords are encrypted on this PC with Windows and never go to the AI. Karya types them only on the site they belong to.";
    const list = document.createElement("ul"); list.className = "account-list";
    const refresh = async () => {
      list.replaceChildren();
      try {
        const r = await fetch(`/api/accounts?token=${encodeURIComponent(token)}`);
        const d = await r.json();
        if (!d.accounts || !d.accounts.length) { const li = document.createElement("li"); li.textContent = "No saved accounts yet."; list.appendChild(li); }
        (d.accounts || []).forEach((a) => {
          const li = document.createElement("li");
          const t = document.createElement("span"); t.textContent = `${a.site}: ${a.username || "(no username)"}${a.has_password ? " \u2022 password saved" : ""}`;
          const del = document.createElement("button"); del.type = "button"; del.className = "ghost small"; del.textContent = "Remove";
          del.setAttribute("aria-label", `Remove saved account for ${a.site}`);
          del.onclick = async () => {
            if (!confirm(`Remove the saved login for ${a.site}?`)) return;
            await fetch(`/api/accounts?token=${encodeURIComponent(token)}&site=${encodeURIComponent(a.site)}`, { method: "DELETE" });
            refresh();
          };
          li.append(t, del); list.appendChild(li);
        });
      } catch (e) { /* ignore */ }
    };
    const form = document.createElement("form"); form.className = "account-form";
    const mk = (ph, type, label) => { const i = document.createElement("input"); i.placeholder = ph; i.type = type; i.autocomplete = "off"; i.setAttribute("aria-label", label); return i; };
    const site = mk("site, e.g. linkedin.com (or 'email')", "text", "Site");
    const user = mk("username or email", "text", "Username");
    const pass = mk("password", "password", "Password");
    const add = document.createElement("button"); add.type = "submit"; add.textContent = "Save account";
    form.append(site, user, pass, add);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!site.value.trim()) return;
      await fetch(`/api/accounts?token=${encodeURIComponent(token)}`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site: site.value.trim(), username: user.value.trim(), password: pass.value }) });
      site.value = ""; user.value = ""; pass.value = "";
      refresh();
    });
    refresh();
    box.append(h, note, list, form);
    return box;
  }

  // ---------- Setup: one window with tabs ----------
  const TABS = [
    ["ai", "AI", "What Karya thinks with"],
    ["you", "About you", "Your details and resume"],
    ["phone", "Phone & bots", "WhatsApp and your bots"],
    ["apps", "Other AI apps", "Claude Code, Cursor, Codex..."],
    ["browser", "Browser & logins", "Your Chrome, saved logins"],
    ["more", "Safety & more", "Approvals, limits, extras"],
  ];
  const panels = {};
  let setupBuilt = false, currentTab = "ai";
  const linkTo = (text, tab) => button(text, () => showTab(tab), "link", text);
  function aiTab() {
    const tip = el("p", "hint tip");
    tip.append("Using Cursor, Claude Code, Codex or VS Code? You can also add Karya to them in ", linkTo("Other AI apps", "apps"),
      ": then you chat there and their AI uses Karya's tools.");
    return [connectBox(), tip, providersForm()];
  }
  function youTab() {
    return [profileBox(), resumeBox(), settingsForm("Email for sending",
      "Karya sends and reads email with your Gmail address and a Gmail App Password (a separate password only for Karya). Without them, Karya uses Gmail in its browser.",
      [{ key: "EMAIL_ADDRESS", label: "Gmail address (to send/read email)", placeholder: "you@gmail.com" },
       { key: "EMAIL_APP_PASSWORD", label: "Gmail App Password", secret: true, link: ["make one", "https://myaccount.google.com/apppasswords"] }])];
  }
  function appsTab() {
    const two = el("section", "explain");
    const ul = el("ul", "plain");
    ul.append(el("li", "", "As Karya's brain (the AI tab): you chat here, the app's plan does the thinking and Karya does the work."),
      el("li", "", "Inside the app (below): you chat in Claude Code, Cursor, Codex, Kiro or VS Code, and their AI uses Karya's tools: the browser, your logins, email, jobs and resumes."));
    two.append(el("h3", "", "Two ways to use Karya with other AI apps"), ul);
    return [two, mcpBox(), settingsForm("Access", "", [
      { key: "KARYA_MCP_ENABLED", label: "Let other AI apps use Karya", options: [["true", "Yes (Karya still asks before sending, posting or paying)"], ["false", "No, switched off"]], def: "true" },
      { key: "KARYA_MCP_TOOLS", label: "Tools they get (empty = the default set; or e.g. browser,email, or all)" }])];
  }
  function browserTab() {
    const box = el("section", "browser-box");
    const state = el("p", "browser-state");
    const steps = el("div");
    box.append(el("h3", "", "Your Chrome"), el("p", "hint", "Karya can work in your everyday Chrome, with the logins you already have, through a small extension. Without it, Karya uses its own Chrome window."), state, steps);
    onStatus((s) => {
      const bl = s.browser_link || {};
      steps.replaceChildren();
      if (s.browser_mode === "karya") { state.textContent = "Karya uses its own Chrome window (change it below)."; state.className = "browser-state ok"; }
      else if (bl.connected) {
        state.textContent = `\u2714 Your ${bl.browser || "Chrome"} is connected. Karya works in its own tab there (the purple "Karya" tab group).` +
          (bl.needs_reload ? " An update is ready: open chrome://extensions and press the reload button on Karya Browser Link." : "");
        state.className = "browser-state ok";
      } else {
        state.textContent = "Not linked yet. Add the extension once, it takes a minute:"; state.className = "browser-state missing";
        steps.appendChild(extensionSteps(s.extension_dir || ""));
      }
    });
    return [box, settingsForm("Which browser Karya uses", "", [{ key: "BROWSER_MODE", label: "Browser Karya uses", def: "auto",
      options: [["auto", "Your Chrome when the extension is connected, else Karya's own window"], ["chrome", "Only your Chrome (Karya Browser Link)"], ["karya", "Only Karya's own Chrome window"]] }]),
      accountsBox()];
  }
  // ---------- the Karya app: Start menu, desktop icon, start with Windows, Quit ----------
  function appToggle(label, change) {
    const row = el("label", "toggle-row");
    const cb = el("input"); cb.type = "checkbox"; cb.setAttribute("aria-label", label);
    row.append(cb, " " + label);
    cb.addEventListener("change", async () => {
      cb.disabled = true;
      try { await change(cb.checked); loadStatus(); }
      catch (e) { cb.checked = !cb.checked; toast("\u2716 " + e.message); }
      cb.disabled = false;
    });
    return { row, cb };
  }
  function appBox() {
    const box = el("section", "app-box");
    const toggles = el("div", "app-toggles");
    const start = appToggle("In the Start menu: press the Windows key, type Karya and press Enter", (on) => api("/api/app", { start_menu: on }));
    const desk = appToggle("An icon on the desktop", (on) => api("/api/app", { desktop: on }));
    const auto = appToggle("Start Karya when I sign in to Windows, so WhatsApp tasks and bots work without opening it", (on) => api("/api/autostart", { on }));
    toggles.append(start.row, desk.row, auto.row);
    box.append(el("h3", "", "The Karya app"),
      el("p", "hint", "Karya opens in its own window, like the other apps on your PC."), toggles,
      el("p", "hint", "Closing Karya's window doesn't stop it: WhatsApp tasks and your bots keep working in the background. Quit Karya stops everything until you open it again."),
      button("Quit Karya", () => quitKarya(), "ghost quit", "Quit Karya"));
    onStatus((s) => {
      const a = s.app || {};
      toggles.hidden = !a.supported;
      start.cb.checked = !!a.start_menu; desk.cb.checked = !!a.desktop; auto.cb.checked = !!(s.autostart || {}).on;
    });
    return box;
  }
  async function quitKarya() {
    const s = lastStatus || {};
    const working = s.busy || (s.bots_running || []).length;
    if (!confirm(working ? "Karya is working on something right now. Quit anyway? Everything stops until you open Karya again."
                         : "Quit Karya? WhatsApp tasks and your bots stop until you open Karya again.")) return;
    let answer;
    try { answer = await api("/api/quit", {}); } catch (e) { toast("\u2716 " + e.message); return; }
    quitting = true;
    closeSetup();
    showStopped(!!(answer && answer.start_menu));
    setTimeout(() => { try { window.close(); } catch (e) { /* a normal browser tab can't close itself */ } }, 800);
  }
  function showStopped(startMenu) {
    const root = $("guide");
    guide = null;
    root.replaceChildren();
    root.setAttribute("aria-label", "Karya has stopped");
    const card = el("div", "guide-card stopped-card");
    const logo = el("img", "guide-logo"); logo.src = "/static/logo.svg"; logo.alt = ""; logo.width = 44; logo.height = 44;
    card.append(logo, el("h2", "", "Karya has stopped"), el("p", "hint", startMenu
      ? "Open it again from the Start menu: press the Windows key, type Karya and press Enter."
      : "Open it again with start.bat in your Karya folder."));
    root.appendChild(card);
    root.hidden = false;
  }

  function moreTab() {
    const approvals = settingsForm("Approvals", "Karya asks before it sends, posts, submits, pays or deletes, unless you change it here.", [
      { key: "AUTO_SUBMIT_PICKED", label: "When Karya applies to jobs you pick", def: "false", options: [["false", "Ask me before each Submit"], ["true", "Submit the jobs I pick without asking (checks still run)"]] },
      { key: "KARYA_FULL_ACCESS", label: "Full access (autopilot)", def: "false", options: [["false", "Off: ask before sending, posting, applying, paying, deleting"], ["true", "On: do everything without asking (payments and account deletes still ask)"]] },
      { key: "KARYA_FULL_ACCESS_PAYMENTS", label: "In full access, payments and account deletes", def: "false", options: [["false", "Still ask me (recommended)"], ["true", "Do them without asking too (risky)"]] },
      { key: "KARYA_REUSE_LOGIN", label: "Reuse one login across sites", def: "false", options: [["false", "No, ask me for each site"], ["true", "Yes, use my main login to sign in and sign up"]] }]);
    const limits = settingsForm("Daily limits", "They keep your accounts from looking like spam.", [
      { key: "KARYA_MAX_EMAILS_PER_DAY", label: "Most emails per day", placeholder: "40" },
      { key: "KARYA_MAX_POSTS_PER_DAY", label: "Most social posts per day", placeholder: "10" },
      { key: "KARYA_MAX_APPLICATIONS_PER_DAY", label: "Most job applications per day", placeholder: "30" }]);
    const sites = settingsForm("Websites", "Optional: Karya can publish the websites it builds for you.", [
      { key: "VERCEL_TOKEN", label: "Vercel token", secret: true, link: ["get one", "https://vercel.com/account/tokens"] }]);
    const again = el("section", "guide-again");
    again.append(el("h3", "", "Welcome guide"), el("p", "hint", "The three steps Karya shows the first time: your AI, about you, extras."),
      button("Show the welcome guide again", async () => { try { await api("/api/onboarding", { action: "reset" }); } catch (e) { /* still show it */ } closeSetup(); openGuide(1); }, "ghost", "Show the welcome guide again"));
    return [appBox(), approvals, limits, sites, again];
  }
  function showTab(id) {
    currentTab = id;
    Object.entries(panels).forEach(([key, p]) => {
      const on = key === id;
      p.tab.setAttribute("aria-selected", on ? "true" : "false");
      p.tab.tabIndex = on ? 0 : -1;
      p.view.hidden = !on;
    });
  }
  function buildSetup() {
    const root = $("setup-panel");
    root.replaceChildren();
    const card = el("div", "setup-card");
    const head = el("div", "setup-head");
    head.append(el("h2", "", "Setup"), button("\u2715", () => closeSetup(), "ghost icon", "Close Setup"));
    const body = el("div", "setup-body");
    const tabs = el("div", "setup-tabs"); tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-orientation", "vertical");
    const views = el("div", "setup-views");
    TABS.forEach(([id, label, sub], index) => {
      const tab = el("button", "setup-tab"); tab.type = "button"; tab.id = `tab-${id}`;
      tab.setAttribute("role", "tab"); tab.setAttribute("aria-controls", `view-${id}`);
      const dot = el("span", "tab-dot"); dot.setAttribute("aria-hidden", "true");
      tab.append(dot, el("span", "tab-label", label), el("span", "tab-sub", sub));
      tab.addEventListener("click", () => showTab(id));
      tab.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
        e.preventDefault();
        const nextId = TABS[(index + (e.key === "ArrowDown" ? 1 : TABS.length - 1)) % TABS.length][0];
        showTab(nextId); panels[nextId].tab.focus();
      });
      tabs.appendChild(tab);
      const view = el("section", "setup-view"); view.id = `view-${id}`;
      view.setAttribute("role", "tabpanel"); view.setAttribute("aria-labelledby", tab.id); view.hidden = true;
      views.appendChild(view);
      panels[id] = { tab, view, dot };
    });
    body.append(tabs, views);
    card.append(head, body);
    root.appendChild(card);
    root.addEventListener("click", (e) => { if (e.target === root) closeSetup(); });
    panels.ai.view.append(...aiTab());
    panels.you.view.append(...youTab());
    panels.phone.view.append(phoneBox(), agentsBox());
    panels.apps.view.append(...appsTab());
    panels.browser.view.append(...browserTab());
    panels.more.view.append(...moreTab());
    setupBuilt = true;
    onStatus((s) => {
      const mark = (id, state) => { panels[id].dot.className = "tab-dot" + (state ? " " + state : ""); };
      mark("ai", s.ai_ready ? "ok" : "bad");
      mark("you", s.you && s.you.name && s.you.resume ? "ok" : "warn");
      mark("phone", (s.whatsapp || {}).state === "ready" ? "ok" : "");
      mark("browser", (s.browser_link || {}).connected || s.browser_mode === "karya" ? "ok" : "warn");
    });
  }
  function openSetup(tab) {
    if (!setupBuilt) buildSetup();
    $("setup-panel").hidden = false;
    showTab(tab || currentTab);
    panels[currentTab].tab.focus();
  }
  function closeSetup() { $("setup-panel").hidden = true; }

  // ---------- the first-run guide: what someone sees right after downloading Karya ----------
  let guide = null;
  function openGuide(step) {
    if (!guide) guide = buildGuide();
    $("guide").hidden = false;
    guide.go(step || 1);
  }
  function extrasList() {
    const wrap = el("div", "extras");
    const appLine = el("div", "extra");
    const appText = el("p", "hint");
    const deskIcon = appToggle("Also put a Karya icon on my desktop", (on) => api("/api/app", { desktop: on }));
    appLine.append(el("h4", "", "Open Karya like any app"), appText, deskIcon.row);
    const wa = el("div", "extra");
    const waState = el("span", "hint");
    const waBtn = button("Connect WhatsApp", async () => {
      waBtn.disabled = true; waState.textContent = " Opening WhatsApp Web in its own window...";
      try {
        const d = await api("/api/phone", { action: "connect" });
        if (d.state === "error") waState.textContent = " \u2716 " + (d.detail || "It didn't start.");
      } catch (e) { waState.textContent = " \u2716 " + e.message; }
      waBtn.disabled = false; loadStatus();
    }, "small", "Connect WhatsApp");
    wa.append(el("h4", "", "Tasks from your phone"), el("p", "hint", "Message yourself on WhatsApp: Karya does the task, asks you there before anything risky and reports back."), waBtn, waState);
    const auto = el("label", "toggle-row");
    const autoBox = el("input"); autoBox.type = "checkbox"; autoBox.setAttribute("aria-label", "Start Karya when I sign in to Windows");
    auto.append(autoBox, " Start Karya when I sign in to Windows, so WhatsApp tasks and bots work without opening it");
    autoBox.addEventListener("change", async () => {
      try { const d = await api("/api/autostart", { on: autoBox.checked }); autoBox.checked = !!d.on; }
      catch (e) { autoBox.checked = !autoBox.checked; toast("\u2716 " + e.message); }
    });
    const chrome = el("div", "extra");
    const chromeState = el("p", "hint");
    const chromeSteps = el("div");
    chrome.append(el("h4", "", "Your own Chrome"), el("p", "hint", "Karya can work in your everyday Chrome, with your logins, through a small extension."), chromeState, chromeSteps);
    onStatus((s) => {
      const st = (s.whatsapp || {}).state || "off";
      if (st !== "off" || !waState.textContent.includes("\u2716")) {
        waState.textContent = { ready: " \u2714 Linked.", needs_qr: " Scan the code in the WhatsApp window: on your phone open WhatsApp > Settings > Linked devices > Link a device.",
          starting: " Opening WhatsApp Web...", opening: " Opening your chat...", error: " \u2716 " + ((s.whatsapp || {}).detail || "") }[st] || waState.textContent;
      }
      waBtn.hidden = st === "ready";
      const app = s.app || {};
      appLine.hidden = !app.supported;
      appText.textContent = app.start_menu
        ? "Karya is in your Start menu: press the Windows key, type Karya and press Enter. Closing its window doesn't stop it, so WhatsApp tasks and bots keep working."
        : "Add Karya to the Start menu in Setup > Safety & more, then open it by typing Karya in Windows search.";
      deskIcon.cb.checked = !!app.desktop;
      auto.hidden = !(s.autostart && s.autostart.supported);
      autoBox.checked = !!(s.autostart && s.autostart.on);
      chromeSteps.replaceChildren();
      if ((s.browser_link || {}).connected) chromeState.textContent = "\u2714 Connected.";
      else { chromeState.textContent = ""; chromeSteps.appendChild(extensionSteps(s.extension_dir || "")); }
    });
    wrap.append(appLine, wa, auto, chrome);
    return wrap;
  }
  function buildGuide() {
    const root = $("guide");
    root.replaceChildren();
    const card = el("div", "guide-card");
    const top = el("div", "guide-top");
    const brand = el("div", "guide-brand");
    const logo = el("img", "guide-logo"); logo.src = "/static/logo.svg"; logo.alt = ""; logo.width = 44; logo.height = 44;
    const words = el("div");
    words.append(el("h2", "", "Welcome to Karya"), el("p", "hint", "Three quick steps. You can skip any of them and change everything later in Setup."));
    brand.append(logo, words);
    const close = () => { root.hidden = true; };
    top.append(brand, button("Skip setup", async () => {
      close();
      try { await api("/api/onboarding", { action: "skip" }); } catch (e) { /* closed anyway */ }
      loadStatus();
    }, "ghost small", "Skip setup"));
    const progress = el("ol", "guide-progress");
    ["Your AI", "About you", "Extras"].forEach((name, i) => progress.appendChild(el("li", "", `${i + 1}. ${name}`)));
    const s1 = el("section", "guide-step");
    s1.append(el("h3", "", "Connect an AI"),
      el("p", "hint", "Karya needs an AI to think with. Pick any one: a free key, a plan you already pay for, or the free offline model. You can add more later."),
      connectBox({ guide: true }));
    const s2 = el("section", "guide-step");
    const prof = profileBox({ guide: true });
    s2.append(el("h3", "", "About you"), el("p", "hint", "Karya fills these in on job applications and emails. They stay on this PC."), prof, resumeBox());
    const s3 = el("section", "guide-step");
    s3.append(el("h3", "", "Extras (all optional)"), el("p", "hint", "Turn these on now or any time in Setup."), extrasList());
    const steps = [s1, s2, s3];
    const nav = el("div", "guide-nav");
    const back = button("Back", () => go(current - 1), "ghost", "Back");
    const next = el("button", "guide-next", "Next"); next.type = "button";
    nav.append(back, next);
    card.append(top, progress, ...steps, nav);
    root.appendChild(card);
    let current = 1;
    const label = () => current === 3 ? "Finish" : current === 1 && !(lastStatus && lastStatus.ai_ready) ? "Skip for now" : "Next";
    function go(n) {
      current = Math.max(1, Math.min(3, n));
      steps.forEach((s, i) => { s.hidden = i !== current - 1; });
      const skipped = (i) => i === 0 && !(lastStatus && lastStatus.ai_ready);
      [...progress.children].forEach((li, i) => {
        li.className = i + 1 === current ? "now" : i + 1 < current ? (skipped(i) ? "skipped" : "done") : "";
      });
      back.hidden = current === 1;
      next.textContent = label();
      card.scrollTop = 0;
    }
    next.addEventListener("click", async () => {
      if (current === 2 && !(await prof.save())) return;
      if (current < 3) { go(current + 1); return; }
      close();
      try { await api("/api/onboarding", { action: "done" }); } catch (e) { /* closed anyway */ }
      loadStatus();
      toast("You're set. Ask Karya anything below.");
      input.focus();
    });
    onStatus(() => { next.textContent = label(); });
    return { go };
  }

  function settleCard(selector, id, text, ok) {
    const card = document.querySelector(`${selector}[data-id="${CSS.escape(id)}"]`);
    if (!card || card.querySelector(".result")) return;
    card.querySelectorAll("input, select, .q-row, .auto-submit, .buttons").forEach((n) => n.remove());
    const r = document.createElement("span"); r.className = "result"; r.textContent = text;
    r.style.color = ok ? "var(--ok)" : "var(--bad)";
    card.appendChild(r);
  }

  function addJobsCard(ev) {
    if (document.querySelector(`.ask[data-id="${CSS.escape(ev.id)}"]`)) return;
    const box = document.createElement("form");
    box.className = "confirm ask jobs-card"; box.dataset.id = ev.id;
    box.setAttribute("aria-label", "Choose jobs to apply to");
    const h = document.createElement("h4"); h.textContent = (ev.bot ? `${ev.bot} asks: ` : "") + "Pick the jobs to apply to";
    const p = document.createElement("pre"); p.textContent = (ev.note ? ev.note + "\n" : "") + "Nothing is sent until you approve each final Submit. Skipped companies won't be shown again.";
    const list = document.createElement("div"); list.className = "job-list";
    const anySuggested = (ev.jobs || []).some((j) => j.suggested);
    (ev.jobs || []).forEach((j, n) => {
      const row = document.createElement("div"); row.className = "job-row";
      const pick = document.createElement("input"); pick.type = "checkbox"; pick.value = j.id; pick.checked = anySuggested ? !!j.suggested : n < 3; pick.className = "pick";
      pick.setAttribute("aria-label", `Apply to ${j.title} at ${j.company}`);
      const info = document.createElement("div"); info.className = "job-info";
      const title = document.createElement("a"); title.href = j.url; title.target = "_blank"; title.rel = "noopener noreferrer";
      title.textContent = `${j.title} \u2014 ${j.company || ""}`;
      const meta = document.createElement("div"); meta.className = "meta";
      meta.textContent = [j.match ? `${j.match}% match` : "", j.location, j.salary, j.apply_via, j.why].filter(Boolean).join(" \u00b7 ");
      info.append(title, meta);
      const skip = document.createElement("label"); skip.className = "skip";
      const skipBox = document.createElement("input"); skipBox.type = "checkbox"; skipBox.value = j.company || ""; skipBox.className = "skip-company";
      skipBox.addEventListener("change", () => { if (skipBox.checked) pick.checked = false; });
      skip.append(skipBox, document.createTextNode(" skip company"));
      row.append(pick, info, skip); list.appendChild(row);
    });
    const buttons = document.createElement("div"); buttons.className = "buttons";
    const autoWrap = document.createElement("label"); autoWrap.className = "auto-submit";
    const autoBox = document.createElement("input"); autoBox.type = "checkbox"; autoBox.checked = !!ev.auto_submit;
    autoWrap.append(autoBox, document.createTextNode(" Submit these without asking me each time (Karya still checks every form, never guesses your answers, and never submits a job twice)"));
    const ok = document.createElement("button"); ok.type = "submit"; ok.className = "approve"; ok.textContent = "Apply to selected";
    const no = document.createElement("button"); no.type = "button"; no.className = "deny"; no.textContent = "None of these";
    box.addEventListener("submit", (e) => {
      e.preventDefault();
      const picked = [...box.querySelectorAll(".pick:checked")].map((i) => i.value);
      const skip_companies = [...new Set([...box.querySelectorAll(".skip-company:checked")].map((i) => i.value).filter(Boolean))];
      send({ type: "ask_reply", id: ev.id, picked, skip_companies, auto_submit: autoBox.checked });
      settleCard(".ask", ev.id, `\u2714 ${picked.length} picked` + (skip_companies.length ? `, skipping ${skip_companies.join(", ")}` : "") +
        (autoBox.checked ? " \u2014 submitting without asking" : ""), true);
    });
    no.onclick = () => { send({ type: "ask_reply", id: ev.id, cancel: true }); settleCard(".ask", ev.id, "\u2716 None picked", false); };
    buttons.append(ok, no);
    box.append(h, p, list, autoWrap, buttons);
    activityBox().appendChild(box);
    scroll();
  }

  function addQuestionsCard(ev) {
    if (document.querySelector(`.ask[data-id="${CSS.escape(ev.id)}"]`)) return;
    const box = document.createElement("form");
    box.className = "confirm ask questions-card"; box.dataset.id = ev.id; box.autocomplete = "off";
    box.setAttribute("aria-label", "Karya needs your answers");
    const h = document.createElement("h4"); h.textContent = ev.bot ? `${ev.bot} needs your answers` : "Karya needs your answers";
    const p = document.createElement("pre");
    p.textContent = (ev.reason ? ev.reason + "\n" : "") + "Only you know these, so Karya won't guess them. They're saved on this PC for your next applications. Leave a box empty to skip it.";
    const fields = [];
    (ev.questions || []).forEach((q, n) => {
      const row = document.createElement("div"); row.className = "q-row";
      const id = `q-${ev.id}-${n}`;
      const label = document.createElement("label"); label.htmlFor = id; label.textContent = q.q;
      const input = document.createElement("input"); input.type = "text"; input.value = q.value || "";
      if (q.hint) input.placeholder = q.hint;
      if (q.options && q.options.length) {  // suggestions to pick, or type your own answer
        const list = document.createElement("datalist"); list.id = `q-${ev.id}-${n}-opts`;
        q.options.forEach((o) => { const opt = document.createElement("option"); opt.value = o; list.appendChild(opt); });
        input.setAttribute("list", list.id);
        row.appendChild(list);
        if (!q.hint) input.placeholder = `e.g. ${q.options[0]}`;
      }
      input.id = id;
      fields.push([q.q, input]);
      row.append(label, input); box.appendChild(row);
    });
    const buttons = document.createElement("div"); buttons.className = "buttons";
    const ok = document.createElement("button"); ok.type = "submit"; ok.className = "approve"; ok.textContent = "Send answers";
    const no = document.createElement("button"); no.type = "button"; no.className = "deny"; no.textContent = "Skip";
    box.addEventListener("submit", (e) => {
      e.preventDefault();
      const answers = {};
      fields.forEach(([q, input]) => { const v = input.value.trim(); if (v) answers[q] = v; });
      send({ type: "ask_reply", id: ev.id, answers });
      settleCard(".ask", ev.id, `\u2714 ${Object.keys(answers).length} answer(s) sent`, true);
    });
    no.onclick = () => { send({ type: "ask_reply", id: ev.id, cancel: true }); settleCard(".ask", ev.id, "\u2716 Skipped", false); };
    buttons.append(ok, no);
    box.prepend(h, p);
    box.appendChild(buttons);
    activityBox().appendChild(box);
    scroll();
    const first = fields.find(([, input]) => !input.value);
    (first ? first[1] : ok).focus();
  }

  function addCodeCard(ev) {
    if (document.querySelector(`.ask[data-id="${CSS.escape(ev.id)}"]`)) return;
    const box = document.createElement("form");
    box.className = "confirm ask code-card"; box.dataset.id = ev.id; box.autocomplete = "off";
    box.setAttribute("aria-label", "Login code needed");
    const h = document.createElement("h4"); h.textContent = (ev.bot ? `${ev.bot} asks: ` : "") + `Login code for ${ev.site || "the site"}`;
    const p = document.createElement("pre");
    p.textContent = (ev.reason ? ev.reason + "\n" : "") + "Type the code here and Karya enters it on the page. It isn't saved.";
    const input = document.createElement("input"); input.inputMode = "numeric"; input.autocomplete = "one-time-code";
    input.placeholder = "the code, e.g. 123456"; input.setAttribute("aria-label", `Login code for ${ev.site || "the site"}`);
    const buttons = document.createElement("div"); buttons.className = "buttons";
    const ok = document.createElement("button"); ok.type = "submit"; ok.className = "approve"; ok.textContent = "Enter code";
    const no = document.createElement("button"); no.type = "button"; no.className = "deny"; no.textContent = "Skip";
    const done = (text, color) => { buttons.replaceChildren(); const r = document.createElement("span"); r.className = "result"; r.textContent = text; r.style.color = color; buttons.appendChild(r); input.disabled = true; };
    box.addEventListener("submit", (e) => {
      e.preventDefault();
      const code = input.value.trim(); if (!code) { input.focus(); return; }
      send({ type: "ask_reply", id: ev.id, code }); input.value = ""; done("\u2714 Sent to the page", "var(--ok)");
    });
    no.onclick = () => { send({ type: "ask_reply", id: ev.id, cancel: true }); done("\u2716 Skipped", "var(--bad)"); };
    buttons.append(ok, no);
    box.append(h, p, input, buttons);
    activityBox().appendChild(box);
    scroll();
    input.focus();
  }

  function addAsk(ev) {
    if (ev.kind === "jobs") return addJobsCard(ev);
    if (ev.kind === "questions") return addQuestionsCard(ev);
    if (ev.kind === "code") return addCodeCard(ev);
    if (document.querySelector(`.ask[data-id="${CSS.escape(ev.id)}"]`)) return;
    const box = document.createElement("form");
    box.className = "confirm ask"; box.dataset.id = ev.id; box.autocomplete = "off";
    box.setAttribute("aria-label", "Login needed");
    const h = document.createElement("h4"); h.textContent = `Login needed for ${ev.site}`;
    const p = document.createElement("pre"); p.textContent = (ev.reason ? ev.reason + "\n" : "") + "Saved encrypted on this PC. The AI never sees the password.";
    const user = document.createElement("input"); user.placeholder = "username or email"; user.value = ev.username || ""; user.setAttribute("aria-label", "Username or email");
    const pass = document.createElement("input"); pass.type = "password"; pass.placeholder = "password"; pass.setAttribute("aria-label", "Password");
    const buttons = document.createElement("div"); buttons.className = "buttons";
    const ok = document.createElement("button"); ok.type = "submit"; ok.className = "approve"; ok.textContent = "Save & continue";
    const no = document.createElement("button"); no.type = "button"; no.className = "deny"; no.textContent = "Skip";
    const done = (text, color) => { box.replaceChildren(h); const r = document.createElement("span"); r.className = "result"; r.textContent = text; r.style.color = color; box.appendChild(r); };
    box.addEventListener("submit", (e) => {
      e.preventDefault();
      send({ type: "ask_reply", id: ev.id, username: user.value.trim(), password: pass.value });
      pass.value = "";
      done("\u2714 Saved securely", "var(--ok)");
    });
    no.onclick = () => { send({ type: "ask_reply", id: ev.id, cancel: true }); done("\u2716 Skipped", "var(--bad)"); };
    buttons.append(ok, no);
    box.append(h, p, user, pass, buttons);
    activityBox().appendChild(box);
    scroll();
    (user.value ? pass : user).focus();
  }

  // ---------- links: in Karya's own window, web links open in your normal browser (with your logins) ----------
  const openOutside = (e) => {
    if (!inApp || e.defaultPrevented || (e.type === "auxclick" && e.button !== 1)) return;
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a) return;
    let url;
    try { url = new URL(a.getAttribute("href"), location.href); } catch (err) { return; }
    if (!/^https?:$/.test(url.protocol) || url.origin === location.origin) return;
    e.preventDefault();
    api("/api/open", { url: url.href }).catch(() => window.open(url.href, "_blank", "noopener"));
  };
  document.addEventListener("click", openOutside);
  document.addEventListener("auxclick", openOutside);

  // ---------- inputs ----------
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    const toBot = text.startsWith("@");     // "@Maya ..." goes to a bot: the chat itself stays free
    if (!text || (busy && !toBot)) return;
    addMessage("user", text);
    send({ type: "chat", text });
    input.value = "";
    input.style.height = "auto";
    if (!toBot) setBusy(true);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; });
  stopBtn.addEventListener("click", () => send({ type: "stop" }));
  $("new-chat").addEventListener("click", () => { if (confirm("Start a new chat? The current conversation will be cleared.")) send({ type: "reset" }); });
  $("setup-btn").addEventListener("click", () => openSetup());
  aiPill.addEventListener("click", () => openSetup("ai"));
  $("notice-btn").addEventListener("click", () => openSetup("ai"));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("setup-panel").hidden) closeSetup(); });
  autoMode.addEventListener("change", () => send({ type: "set_mode", auto: autoMode.checked }));
  keepGoing.addEventListener("change", () => send({ type: "set_keep_going", on: keepGoing.checked }));
  document.querySelectorAll(".idea").forEach((c) => c.addEventListener("click", () => {
    input.value = c.dataset.prompt || c.textContent; input.focus(); input.dispatchEvent(new Event("input"));
  }));

  window.KaryaMarkdown = renderMarkdown;  // exposed for automated safety tests
  connect();
})();

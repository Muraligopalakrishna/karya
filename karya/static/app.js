(() => {
  "use strict";
  const params = new URLSearchParams(location.search);
  if (params.get("token")) {
    localStorage.setItem("karya-token", params.get("token"));
    history.replaceState(null, "", "/");
  }
  const token = localStorage.getItem("karya-token") || "";
  const UI_VERSION = (document.querySelector('meta[name="karya-ui"]') || {}).content || "";

  const $ = (id) => document.getElementById(id);
  const chat = $("chat"), input = $("input"), form = $("chat-form"), sendBtn = $("send-btn"), stopBtn = $("stop-btn");
  const statusLine = $("status-line"), dot = $("conn-dot"), providerLabel = $("provider-label"), autoMode = $("auto-mode"), keepGoing = $("keep-going");
  let ws = null, busy = false, activity = null, retry = 0;
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
    h.textContent = ev.risk === "critical" ? "Approval needed (sends/posts/submits or can't be undone)" : "Approval needed";
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
          break;
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

  async function loadStatus() {
    try {
      const r = await fetch(`/api/status?token=${encodeURIComponent(token)}`);
      const s = await r.json();
      providerLabel.textContent = s.providers && s.providers.length ? `brain: ${s.providers[0]}` : "no AI configured";
      renderSetup(s);
      if (!s.providers || !s.providers.length || s.providers.every((p) => p.startsWith("ollama"))) $("setup-panel").hidden = false;
    } catch (e) { providerLabel.textContent = "status unavailable"; }
  }

  function renderSetup(s) {
    const panel = $("setup-panel");
    let statusBox = panel.querySelector(".status-box");
    if (!statusBox) {  // build the forms once; later status updates must not wipe what the user typed
      panel.replaceChildren();
      statusBox = document.createElement("div"); statusBox.className = "status-box";
      panel.append(statusBox, connectBox(), phoneBox(), agentsBox(), mcpBox(), settingsForm(), accountsBox());
    }
    const phone = panel.querySelector(".phone-box"), agentList = panel.querySelector(".agents-box");
    if (phone && phone.update) phone.update(s.whatsapp || {});
    if (agentList && agentList.update) agentList.update(s.agents || []);
    clearTimeout(renderSetup.poll);
    if (["starting", "opening", "needs_qr"].includes((s.whatsapp || {}).state)) renderSetup.poll = setTimeout(loadStatus, 4000);
    statusBox.replaceChildren();
    const h = document.createElement("h3"); h.textContent = "Status";
    const ul = document.createElement("ul");
    const item = (ok, text) => { const li = document.createElement("li"); li.className = ok ? "ok" : "missing"; li.textContent = (ok ? "\u2714 " : "\u26A0 ") + text; ul.appendChild(li); };
    const cloud = (s.providers || []).filter((p) => !p.startsWith("ollama"));
    item(cloud.length > 0, cloud.length ? `AI brain: ${s.providers.join(" \u2192 ")}` : "AI brain: only the slow offline model. Add any API key below (free or paid).");
    Object.values(s.checks || {}).forEach((c) => item(!!c.ok, describeCheck(c).replace(/^[\u2714\u2716] /, "")));
    item(!!s.email, s.email ? `Email: ${s.email}` : "Email: not set (Gmail address + App Password below), or Karya will ask when it needs it.");
    item(!!s.resume, s.resume ? `Resume: ${s.resume}` : "Resume: not set. Add the path below, or ask Karya to build one with you.");
    item(!!s.vercel, s.vercel ? "Website deploys: Vercel token set" : "Website deploys: optional Vercel token.");
    statusBox.append(h, ul);
    const bl = s.browser_link || {};
    if (s.browser_mode === "karya") item(true, "Browser: Karya's own Chrome window (change it below).");
    else if (bl.connected) item(true, `Your ${bl.browser || "Chrome"}: connected. Karya works in its own tab there (purple "Karya" tab group).` +
      (bl.needs_reload ? " An update is ready: click \u21bb on Karya Browser Link in chrome://extensions when convenient (it works either way)." : ""));
    else {
      item(false, "Your Chrome: not linked yet. Karya uses its own window until you add the Karya Browser Link extension (one time, 1 minute):");
      statusBox.appendChild(extensionSteps(s.extension_dir || ""));
    }
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

  const OTHER_FIELDS = [
    ["EMAIL_ADDRESS", "Gmail address (to send/read email)", false],
    ["EMAIL_APP_PASSWORD", "Gmail App Password (myaccount.google.com/apppasswords)", true],
    ["RESUME_PATH", "Resume file path, e.g. C:\\Users\\you\\Documents\\resume.pdf", false],
    ["VERCEL_TOKEN", "Vercel token (optional, for publishing websites)", true],
  ];

  function describeCheck(c) {
    if (!c) return "";
    if (!c.ok) return `\u2716 ${c.title || c.provider}: ${c.error || "not working"}`;
    const tpm = c.tokens_per_minute ? ` (${c.tokens_per_minute.toLocaleString()} tokens/min)` : "";
    return `\u2714 ${c.title || c.provider} works with ${c.model}${tpm}: ${c.summary || ""}`;
  }

  // ---------- small helpers for the Setup sections ----------
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const button = (text, onClick, cls = "ghost small", label = "") => {
    const b = el("button", cls, text); b.type = "button"; if (label) b.setAttribute("aria-label", label); b.addEventListener("click", onClick); return b;
  };
  const link = (text, href) => { const a = el("a", "", text); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a; };
  async function api(path, body) {
    const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const r = await fetch(`${path}?token=${encodeURIComponent(token)}`, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  // ---------- Connect your AI: paste any key, or sign in with a subscription, then pick the model ----------
  function connectBox() {
    const box = el("section", "connect-box"); box.setAttribute("aria-label", "Connect your AI");
    const msg = el("div", "form-msg"); msg.setAttribute("role", "status");
    const list = el("div", "ai-list");
    const services = {};
    let logins = {};
    const timers = {};
    const say = (text) => { msg.replaceChildren(); if (text) msg.appendChild(el("div", "", text)); };
    const showChecks = (checks, first) => {
      say(first || "");
      (checks || []).forEach((c) => msg.appendChild(el("div", c.ok ? "ok" : "missing", describeCheck(c))));
    };
    const fail = (e) => say("\u2716 " + (e.message || e));

    // paste a key
    const keyForm = el("form", "key-form"); keyForm.autocomplete = "off";
    const keyInput = el("input"); keyInput.type = "password"; keyInput.autocomplete = "off"; keyInput.spellcheck = false;
    keyInput.placeholder = "Paste any AI key: OpenAI, Claude, Gemini, Groq, OpenRouter, DeepSeek, Mistral, xAI, Kiro...";
    keyInput.setAttribute("aria-label", "Paste an AI key");
    const keyBtn = el("button", "", "Connect"); keyBtn.type = "submit";
    keyForm.append(keyInput, keyBtn);
    const choose = el("div", "choose-service"); choose.hidden = true;
    const free = el("p", "hint");
    free.append("No key yet? Free ones: ", link("Gemini", "https://aistudio.google.com/apikey"), " \u00b7 ",
      link("Groq", "https://console.groq.com/keys"), " \u00b7 ", link("OpenRouter", "https://openrouter.ai/keys"));
    const sendKey = async (service) => {
      const key = keyInput.value.trim();
      if (!key) { keyInput.focus(); return; }
      choose.hidden = true; say("Checking the key...");
      try {
        const d = await api("/api/ai", { action: "key", key, service: service || "" });
        if (d.choose) {
          choose.replaceChildren(el("span", "", "Which service is this key from? "));
          d.choose.forEach((name) => choose.append(button((services[name] || {}).title || name, () => sendKey(name), "ghost small")));
          choose.hidden = false; say(""); return;
        }
        keyInput.value = "";
        showChecks(d.checks, `\u2714 ${d.title} connected.`);
        render(d.overview); loadStatus();
      } catch (e) { fail(e); }
    };
    keyForm.addEventListener("submit", (e) => { e.preventDefault(); sendKey(""); });

    // subscriptions
    const kiroRow = el("div", "sub-row"), codexRow = el("div", "sub-row");
    const pollLogin = (name, rowFill) => {
      clearInterval(timers[name]);
      const started = Date.now();
      timers[name] = setInterval(async () => {
        if (Date.now() - started > 10 * 60 * 1000) { clearInterval(timers[name]); return; }
        try {
          const d = await api("/api/ai", { action: "login_status", provider: name });
          logins[name] = { ...(logins[name] || {}), progress: d };
          if (d.state === "done") {
            clearInterval(timers[name]);
            showChecks(d.checks, `\u2714 Signed in. ${name === "kiro" ? "Kiro" : "Your ChatGPT plan"} is now Karya's first AI.`);
            refresh(); loadStatus();
          } else if (d.state === "failed" || d.state === "idle") {
            clearInterval(timers[name]);
            say(`\u2716 Sign-in didn't finish${d.error ? ": " + d.error : ""}. Try again.`);
          }
          rowFill();
        } catch (e) { clearInterval(timers[name]); fail(e); }
      }, 2500);
    };
    const progressLine = (p) => {
      const line = el("div", "hint");
      if (!p || p.state !== "waiting") return line;
      line.append("Finish signing in on the page that opened in your browser. ");
      if (p.code) line.append("Check it shows this code: ", el("strong", "", p.code), ". ");
      if (p.url) line.append(link("Open the sign-in page", p.url));
      return line;
    };
    const fillKiro = () => {
      const info = logins.kiro || {};
      const on = (list.dataset.names || "").split(",").includes("kiro");
      kiroRow.replaceChildren(el("span", "sub-name", "Kiro subscription"));
      if (on) kiroRow.append(el("span", "ok", info.key ? "\u2714 connected with your Kiro key" : "\u2714 signed in"));
      else if (info.installed === false) kiroRow.append(el("span", "", "First install the Kiro CLI: "), link("kiro.dev/downloads", "https://kiro.dev/downloads"));
      else {
        kiroRow.append(el("span", "", "Sign in with: "));
        [["google", "Google"], ["github", "GitHub"], ["builder", "AWS Builder ID"]].forEach(([method, label]) =>
          kiroRow.append(button(label, async () => {
            say("Opening the Kiro sign-in page...");
            try { logins.kiro = { ...info, progress: await api("/api/ai", { action: "login", provider: "kiro", method }) }; fillKiro(); pollLogin("kiro", fillKiro); say(""); }
            catch (e) { fail(e); }
          }, "small", `Sign in to Kiro with ${label}`)));
      }
      kiroRow.append(progressLine(info.progress));
    };
    const fillCodex = () => {
      const info = logins.codex || {};
      const on = (list.dataset.names || "").split(",").includes("codex");
      codexRow.replaceChildren(el("span", "sub-name", "ChatGPT plan (Plus, Pro, Business) via Codex"));
      if (on) codexRow.append(el("span", "ok", "\u2714 connected"));
      else if (info.installed === false) {
        const cmd = "npm install -g @openai/codex";
        codexRow.append(el("span", "", "First install the Codex CLI (needs Node.js from nodejs.org): "), el("code", "", cmd), " ",
          button("Copy", async (e) => { try { await navigator.clipboard.writeText(cmd); e.target.textContent = "Copied \u2714"; } catch (err) { e.target.textContent = "Select and copy"; } }, "ghost small", "Copy the install command"));
      } else if (info.signed_in) {
        codexRow.append(button("Use my ChatGPT plan", async () => {
          say("Connecting your ChatGPT plan...");
          try { const d = await api("/api/ai", { action: "use_codex" }); showChecks(d.checks, "\u2714 Your ChatGPT plan is now Karya's first AI."); render(d.overview); loadStatus(); }
          catch (e) { fail(e); }
        }, "small", "Use my ChatGPT plan"), el("span", "hint", " Codex is already signed in on this PC."));
      } else {
        const go = (device) => async () => {
          say("Opening the ChatGPT sign-in page...");
          try { logins.codex = { ...info, progress: await api("/api/ai", { action: "login", provider: "codex", method: device ? "device" : "" }) }; fillCodex(); pollLogin("codex", fillCodex); say(""); }
          catch (e) { fail(e); }
        };
        codexRow.append(button("Sign in with ChatGPT", go(false), "small", "Sign in with ChatGPT"), " ",
          button("use a code instead", go(true), "ghost small", "Sign in to ChatGPT with a code"));
      }
      codexRow.append(progressLine(info.progress));
    };

    // the AIs Karya uses, in order, each with its model
    const render = (ov) => {
      (ov.services || []).forEach((s) => { services[s.name] = s; });
      if (ov.kiro) logins.kiro = ov.kiro;
      if (ov.codex) logins.codex = ov.codex;
      const rows = ov.connected || [];
      list.dataset.names = rows.map((r) => r.name).join(",");
      list.replaceChildren();
      if (!rows.some((r) => r.name !== "ollama")) list.appendChild(el("p", "missing", "\u26A0 No AI connected yet. Paste a key below or sign in with a subscription."));
      rows.forEach((r, i) => {
        const row = el("div", "ai-row");
        row.append(el("span", "ai-name", `${i + 1}. ${r.title}`),
          el("span", "ai-how", r.how === "sign-in" ? "signed in" : r.how === "offline" ? "offline (slow)" : r.how));
        const pick = el("select"); pick.setAttribute("aria-label", `Model for ${r.title}`);
        const add = (id, label) => { const o = el("option", "", label || id); o.value = id; pick.appendChild(o); return o; };
        add(r.model, r.model);
        pick.value = r.model;
        api("/api/ai", { action: "models", provider: r.name }).then((d) => {
          (d.models || []).forEach((m) => { if (m.id !== r.model) add(m.id, m.description ? `${m.name} \u2014 ${m.description}` : m.name); });
          add("__other", "Another model (type its name)...");
        }).catch(() => add("__other", "Another model (type its name)..."));
        pick.addEventListener("change", async () => {
          let model = pick.value;
          if (model === "__other") { model = (prompt(`Model name for ${r.title}:`) || "").trim(); if (!model) { pick.value = r.model; return; } }
          say(`Switching ${r.title} to ${model}...`);
          try { const d = await api("/api/ai", { action: "model", provider: r.name, model }); showChecks(d.checks, `\u2714 ${r.title} now uses ${model}.`); render(d.overview); loadStatus(); }
          catch (e) { fail(e); pick.value = r.model; }
        });
        row.appendChild(pick);
        if (i > 0) row.appendChild(button("Use first", async () => {
          try { const d = await api("/api/ai", { action: "first", provider: r.name }); render(d.overview); loadStatus(); say(`\u2714 ${r.title} goes first now.`); } catch (e) { fail(e); }
        }, "ghost small", `Use ${r.title} first`));
        if (r.name !== "ollama") row.appendChild(button("Remove", async () => {
          if (!confirm(`Remove ${r.title} from Karya?`)) return;
          try { const d = await api("/api/ai", { action: "remove", provider: r.name }); render(d.overview); loadStatus(); say(`Removed ${r.title}.`); } catch (e) { fail(e); }
        }, "ghost small", `Remove ${r.title}`));
        list.appendChild(row);
      });
      fillKiro(); fillCodex();
    };
    const refresh = () => api("/api/ai").then(render).catch(fail);

    box.append(el("h3", "", "Connect your AI"),
      el("p", "hint", "Karya needs an AI to think with. Paste any key you have, or sign in with a subscription you already pay for. Then pick the model. Karya uses them in this order and moves to the next one if one is busy."),
      list, keyForm, choose, free, el("h4", "", "Or use a subscription you already have"), kiroRow, codexRow, msg);
    refresh();
    return box;
  }

  // ---------- Your phone: tasks from WhatsApp ----------
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
    box.update = (wa) => {
      const st = wa.state || "off";
      const text = {
        off: "Not linked yet.",
        starting: "Opening WhatsApp Web...",
        opening: "Opening your \"Message yourself\" chat...",
        needs_qr: "Scan the code in the WhatsApp window: on your phone open WhatsApp > Settings > Linked devices > Link a device.",
        ready: "\u2714 Linked. Message yourself on WhatsApp to give Karya a task. STATUS, STOP and HELP work too.",
        error: "\u2716 " + (wa.detail || "WhatsApp didn't start."),
      }[st] || wa.detail || st;
      state.textContent = text; state.className = "phone-state " + (st === "ready" ? "ok" : st === "error" ? "missing" : "");
      num.hidden = !(st === "off" || st === "error"); go.hidden = !(st === "off" || st === "error"); off.hidden = st === "off";
    };
    box.append(el("h3", "", "Give Karya tasks from your phone (WhatsApp)"),
      el("p", "hint", "Message yourself on WhatsApp (your own \"Message yourself\" chat): Karya does the task, asks you there before it sends, posts or submits, and reports back. Only that one chat is read. Logins and passwords never go over WhatsApp. It works while Karya runs on this PC; its WhatsApp window must stay open (it can sit behind other windows)."),
      state, row, msg);
    box.update({ state: "off" });
    return box;
  }

  // ---------- Background agents ----------
  function agentsBox() {
    const box = el("section", "agents-box"); box.setAttribute("aria-label", "Background agents");
    const list = el("ul", "agent-list");
    box.update = (agents) => {
      list.replaceChildren();
      if (!agents.length) { list.appendChild(el("li", "hint", "No agents yet.")); return; }
      agents.forEach((a) => {
        const li = el("li");
        const act = (action, label) => button(label, async () => {
          if (action === "delete" && !confirm(`Delete the agent "${a.name}"?`)) return;
          try { box.update((await api("/api/agents", { action, agent: a.id })).agents || []); } catch (e) { alert(e.message); }
        }, "ghost small", `${label}: ${a.name}`);
        const actions = el("div", "agent-actions");
        actions.append(act("run", "Run now"), act(a.enabled ? "pause" : "resume", a.enabled ? "Pause" : "Resume"), act("delete", "Delete"));
        li.append(el("div", "agent-title", `${a.name} \u00b7 ${a.when}${a.enabled ? "" : " (paused)"}`), el("div", "hint", a.task),
          el("div", "hint", (a.enabled ? `Next run: ${a.next_run}` : "Paused") + (a.last_report ? ` \u00b7 Last report: ${a.last_report}` : "")), actions);
        list.appendChild(li);
      });
    };
    box.append(el("h3", "", "Background agents"),
      el("p", "hint", "Tasks Karya does by itself on a schedule (while it's running) and reports on, here and on WhatsApp. Make one by asking Karya, for example: \"every morning at 9 find new PM jobs in Hyderabad and apply to the best 3\" or \"check gold sentiment twice a day and tell me\"."),
      list);
    box.update([]);
    return box;
  }

  function settingsForm() {
    const box = document.createElement("form");
    box.className = "settings";
    box.autocomplete = "off";
    const h = document.createElement("h3"); h.textContent = "Settings (saved only on this PC)";
    const intro = document.createElement("p"); intro.className = "hint";
    intro.textContent = "Add any AI key you have, free or paid. Karya checks each key, learns its limits and adapts: small free plans get small requests, big paid plans run at full speed.";
    const provBox = document.createElement("div"); provBox.className = "providers";
    const provFold = document.createElement("details"); provFold.className = "advanced";
    const provSum = document.createElement("summary"); provSum.textContent = "Every AI service, one by one (advanced: keys, models, order)";
    provFold.append(provSum, provBox);
    const otherBox = document.createElement("div");
    const inputs = {};
    const secrets = new Set();
    const mkInput = (key, secret, placeholder) => {
      const input = document.createElement("input"); input.type = secret ? "password" : "text"; input.autocomplete = "off"; input.spellcheck = false;
      input.placeholder = placeholder || (secret ? "not set" : "");
      inputs[key] = input; if (secret) secrets.add(key);
      return input;
    };
    const row = (labelText, ...controls) => {
      const r = document.createElement("div"); r.className = "field";
      const span = document.createElement("span"); span.textContent = labelText;
      const wrap = document.createElement("div"); wrap.className = "controls"; wrap.append(...controls);
      r.append(span, wrap); return r;
    };
    fetch(`/api/settings?token=${encodeURIComponent(token)}`).then((r) => r.json()).then((data) => {
      const values = data.values || {};
      (data.providers || []).forEach((p) => {
        const key = mkInput(p.key_env, true, "API key");
        key.setAttribute("aria-label", `${p.title} API key`);
        const model = mkInput(p.model_env, false, p.default_model === "auto" ? "model: auto" : `model: ${p.default_model}`);
        model.setAttribute("aria-label", `${p.title} model (optional)`);
        model.className = "model";
        const link = document.createElement("a"); link.href = p.signup; link.target = "_blank"; link.rel = "noopener noreferrer";
        link.textContent = p.free ? "get key (free plan)" : "get key";
        provBox.appendChild(row(p.title, key, model, link));
      });
      const cBase = mkInput("CUSTOM_BASE_URL", false, "https://your-endpoint/v1"); cBase.setAttribute("aria-label", "Custom base URL");
      const cKey = mkInput("CUSTOM_API_KEY", true, "API key"); cKey.setAttribute("aria-label", "Custom API key");
      const cModel = mkInput("CUSTOM_MODEL", false, "model name"); cModel.setAttribute("aria-label", "Custom model"); cModel.className = "model";
      provBox.appendChild(row("Any other OpenAI-compatible API", cBase, cKey, cModel));
      const order = mkInput("LLM_PROVIDERS", false, "optional, e.g. anthropic,groq,ollama");
      order.setAttribute("aria-label", "Provider order");
      provBox.appendChild(row("Order to use them in", order));
      OTHER_FIELDS.forEach(([key, label, secret]) => {
        const input = mkInput(key, secret);
        input.setAttribute("aria-label", label);
        otherBox.appendChild(row(label, input));
      });
      const mode = document.createElement("select"); mode.setAttribute("aria-label", "Which browser Karya uses");
      [["auto", "Your Chrome when the extension is connected, else Karya's own window"], ["chrome", "Only your Chrome (Karya Browser Link)"],
       ["karya", "Only Karya's own Chrome window"]].forEach(([v, t]) => { const o = document.createElement("option"); o.value = v; o.textContent = t; mode.appendChild(o); });
      inputs.BROWSER_MODE = mode;
      otherBox.appendChild(row("Browser Karya uses", mode));
      const autoSubmit = document.createElement("select"); autoSubmit.setAttribute("aria-label", "When Karya applies to jobs you pick");
      [["false", "Ask me before each Submit"], ["true", "Submit the jobs I pick without asking (checks still run)"]]
        .forEach(([v, t]) => { const o = document.createElement("option"); o.value = v; o.textContent = t; autoSubmit.appendChild(o); });
      inputs.AUTO_SUBMIT_PICKED = autoSubmit;
      otherBox.appendChild(row("When Karya applies to jobs you pick", autoSubmit));
      const fullAccess = document.createElement("select"); fullAccess.setAttribute("aria-label", "Full access (do everything without asking)");
      [["false", "Off - ask before sending, posting, applying, paying, deleting"],
       ["true", "Full access - do everything without asking (checks still run; payments & account-deletes still ask)"]]
        .forEach(([v, t]) => { const o = document.createElement("option"); o.value = v; o.textContent = t; fullAccess.appendChild(o); });
      inputs.KARYA_FULL_ACCESS = fullAccess;
      otherBox.appendChild(row("Full access (autopilot)", fullAccess));
      const payFloor = document.createElement("select"); payFloor.setAttribute("aria-label", "In full access, also allow payments and account deletes");
      [["false", "No - still ask me before paying or deleting an account (recommended)"],
       ["true", "Yes - in full access, also pay and delete without asking (risky)"]]
        .forEach(([v, t]) => { const o = document.createElement("option"); o.value = v; o.textContent = t; payFloor.appendChild(o); });
      inputs.KARYA_FULL_ACCESS_PAYMENTS = payFloor;
      otherBox.appendChild(row("In full access, payments & account deletes", payFloor));
      const reuse = document.createElement("select"); reuse.setAttribute("aria-label", "Reuse one login across sites");
      [["false", "No - ask me for each site's login"],
       ["true", "Yes - reuse my primary login to sign in and create accounts without asking"]]
        .forEach(([v, t]) => { const o = document.createElement("option"); o.value = v; o.textContent = t; reuse.appendChild(o); });
      inputs.KARYA_REUSE_LOGIN = reuse;
      otherBox.appendChild(row("Reuse one login across sites", reuse));
      const mcpOn = document.createElement("select"); mcpOn.setAttribute("aria-label", "Let other AI apps and CLIs use Karya (MCP)");
      [["true", "Yes - apps I add Karya to can use it (approvals still apply)"], ["false", "No - switched off"]]
        .forEach(([v, t]) => { const o = document.createElement("option"); o.value = v; o.textContent = t; mcpOn.appendChild(o); });
      inputs.KARYA_MCP_ENABLED = mcpOn;
      otherBox.appendChild(row("Let other AI apps and CLIs use Karya (MCP)", mcpOn));
      [["KARYA_MCP_TOOLS", "Tools they get (empty = the default 33; or e.g. browser,email - or all)"],
       ["KARYA_MAX_EMAILS_PER_DAY", "Most emails per day (default 40)"],
       ["KARYA_MAX_POSTS_PER_DAY", "Most social posts per day (default 10)"],
       ["KARYA_MAX_APPLICATIONS_PER_DAY", "Most job applications per day (default 30)"]].forEach(([key, label]) => {
        const input = mkInput(key, false); input.setAttribute("aria-label", label);
        otherBox.appendChild(row(label, input));
      });
      Object.entries(values).forEach(([key, v]) => {
        const input = inputs[key]; if (!input) return;
        if (v.value) input.value = v.value;
        if (secrets.has(key) && v.set) input.placeholder = "saved \u2714 (type to replace)";
      });
      if (!mode.value) mode.value = "auto";
      if (!["true", "false"].includes(autoSubmit.value)) autoSubmit.value = "false";
      if (!["true", "false"].includes(mcpOn.value)) mcpOn.value = "true";
      [fullAccess, payFloor, reuse].forEach((sel) => { if (!["true", "false"].includes(sel.value)) sel.value = "false"; });
    }).catch(() => {});
    const save = document.createElement("button"); save.type = "submit"; save.textContent = "Save & check keys";
    const msg = document.createElement("div"); msg.className = "form-msg"; msg.setAttribute("role", "status");
    intro.textContent = "Email, resume, browser and safety settings. (To add an AI, use Connect your AI above.)";
    box.append(h, intro, provFold, otherBox, save, msg);
    box.addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = {};
      Object.entries(inputs).forEach(([key, input]) => { const v = input.value.trim(); if (v || !secrets.has(key)) body[key] = v; });
      msg.textContent = "Saving and checking keys...";
      try {
        const r = await fetch(`/api/settings?token=${encodeURIComponent(token)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const d = await r.json();
        if (!r.ok) { msg.textContent = "Error: " + (d.error || r.status); return; }
        msg.replaceChildren();
        const done = document.createElement("div"); done.textContent = "Saved \u2714"; msg.appendChild(done);
        (d.checks || []).forEach((c) => { const line = document.createElement("div"); line.className = c.ok ? "ok" : "missing"; line.textContent = describeCheck(c); msg.appendChild(line); });
        secrets.forEach((key) => { if (inputs[key].value) { inputs[key].value = ""; inputs[key].placeholder = "saved \u2714 (type to replace)"; } });
        loadStatus();
      } catch (err) { msg.textContent = "Error: " + err; }
    });
    return box;
  }

  function mcpBox() {
    const box = document.createElement("div"); box.className = "mcp-box";
    const h = document.createElement("h3"); h.textContent = "Use Karya from other AI apps and CLIs (MCP)";
    const intro = document.createElement("p"); intro.className = "hint";
    intro.textContent = "Claude Desktop, Cursor, Kiro, VS Code, Windsurf, OpenClaw, Claude Code, Codex, Gemini CLI and other MCP apps " +
      "can use Karya's browser, email, job and posting tools with their own AI. Karya still runs every check and asks you " +
      "before sending, posting, submitting or paying. The Chrome extension is optional: without it Karya uses its own Chrome window.";
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
        if (!appsList.children.length) appsList.appendChild(el("li", "hint", "No AI apps found on this PC. Use the steps below for others."));
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
    const h = document.createElement("h3"); h.textContent = "Accounts (passwords are encrypted on this PC and never sent to the AI)";
    const list = document.createElement("ul"); list.className = "account-list";
    const refresh = async () => {
      list.replaceChildren();
      try {
        const r = await fetch(`/api/accounts?token=${encodeURIComponent(token)}`);
        const d = await r.json();
        if (!d.accounts || !d.accounts.length) { const li = document.createElement("li"); li.textContent = "No saved accounts yet."; list.appendChild(li); }
        (d.accounts || []).forEach((a) => {
          const li = document.createElement("li");
          const t = document.createElement("span"); t.textContent = `${a.site} \u2014 ${a.username || "(no username)"}${a.has_password ? " \u2022 password saved" : ""}`;
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
    box.append(h, list, form);
    return box;
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
    const h = document.createElement("h4"); h.textContent = "Pick the jobs to apply to";
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
    const h = document.createElement("h4"); h.textContent = "Karya needs your answers";
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

  function addAsk(ev) {
    if (ev.kind === "jobs") return addJobsCard(ev);
    if (ev.kind === "questions") return addQuestionsCard(ev);
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

  // ---------- inputs ----------
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    addMessage("user", text);
    send({ type: "chat", text });
    input.value = "";
    input.style.height = "auto";
    setBusy(true);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; });
  stopBtn.addEventListener("click", () => send({ type: "stop" }));
  $("new-chat").addEventListener("click", () => { if (confirm("Start a new chat? The current conversation will be cleared.")) send({ type: "reset" }); });
  $("setup-btn").addEventListener("click", () => { const p = $("setup-panel"); p.hidden = !p.hidden; });
  autoMode.addEventListener("change", () => send({ type: "set_mode", auto: autoMode.checked }));
  keepGoing.addEventListener("change", () => send({ type: "set_keep_going", on: keepGoing.checked }));
  document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => { input.value = c.textContent; input.focus(); }));

  window.KaryaMarkdown = renderMarkdown;  // exposed for automated safety tests
  connect();
})();

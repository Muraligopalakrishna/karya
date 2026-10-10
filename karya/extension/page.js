// Karya page helper. Injected into web pages by the Karya Browser Link extension, and by Karya's own browser.
// It lists the elements an agent can use (with the question each field belongs to) and performs actions on them.
// It never makes network requests and never stores anything.
(() => {
  const K = {};
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // Pages built from web components (SmartRecruiters, Salesforce, many new ATS forms) keep their fields inside
  // shadow roots, often nested several deep. Every lookup searches all of them, so a field the snapshot listed is
  // always found again (2026-10-07: SmartRecruiters' City, phone and date fields all came back "gone").
  function shadowRoots(root, out) {
    out = out || [];
    for (const el of (root || document).querySelectorAll("*")) {
      if (el.shadowRoot) { out.push(el.shadowRoot); shadowRoots(el.shadowRoot, out); }
    }
    return out;
  }
  const deepAll = (sel, root) => {
    const base = root || document;
    const out = [...base.querySelectorAll(sel)];
    for (const r of shadowRoots(base)) out.push(...r.querySelectorAll(sel));
    return out;
  };
  const deepOne = (sel, root) => {
    const base = root || document;
    const direct = base.querySelector(sel);
    if (direct) return direct;
    for (const r of shadowRoots(base)) { const found = r.querySelector(sel); if (found) return found; }
    return null;
  };
  // The element a node sits in, crossing out of a shadow root to its host.
  const parentOf = (n) => n && (n.parentElement || (n.getRootNode && n.getRootNode() instanceof ShadowRoot ? n.getRootNode().host : null));
  const SEL = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[role=button],[role=link],' +
    '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=menuitemcheckbox],[role=option],[role=switch],' +
    '[role=combobox],[role=textbox],[role=searchbox],[role=slider],[contenteditable=""],[contenteditable="true"],' +
    '[onclick],[tabindex]:not([tabindex="-1"])';
  const CONTROL = 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]),select,textarea,' +
    '[role=combobox],[role=textbox],[contenteditable=""],[contenteditable="true"]';
  const LABELISH = 'legend, label, [class*="question" i], [class*="label" i], [class*="title" i], h3, h4, p';

  const isFile = (el) => el.tagName === "INPUT" && el.type === "file";
  const isOption = (el) => el.type === "radio" || el.type === "checkbox";
  function shown(el) {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return !(r.width < 3 || r.height < 3 || s.visibility === "hidden" || s.display === "none" || el.closest('[aria-hidden="true"]'));
  }
  // Radio buttons and checkboxes are often drawn by their label while the real input is hidden (LinkedIn Easy Apply,
  // many job forms). Such an input still counts: the user sees and clicks its label.
  const visibleLabel = (el) => (el.labels ? [...el.labels] : []).find((l) => shown(l)) || null;
  const usable = (el) => shown(el) || isFile(el) || (el.tagName === "INPUT" && isOption(el) && !!visibleLabel(el));
  function isControl(el) {
    return ["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName) || el.isContentEditable ||
      /^(combobox|textbox|searchbox)$/.test(el.getAttribute("role") || "");
  }
  function isCombo(el) {
    return el.getAttribute("role") === "combobox" || /list|both/.test(el.getAttribute("aria-autocomplete") || "") ||
      el.getAttribute("aria-haspopup") === "listbox" || (el.tagName === "INPUT" && el.hasAttribute("list")) ||
      /^(start typing|search for|type to search)/i.test(el.getAttribute("placeholder") || "");
  }
  function isSearch(el) {
    const text = [el.getAttribute("placeholder"), el.getAttribute("aria-label"), el.getAttribute("name"), el.type].join(" ");
    return el.type === "search" || el.getAttribute("role") === "searchbox" || /\bsearch\b|\bquery\b|^q$/i.test(text);
  }
  // A dropdown built from a text box (react-select on Greenhouse, Lever, Ashby) shows the chosen option in a sibling
  // element, not in the box: read it there, staying inside this one field.
  function comboValue(el) {
    let node = el.parentElement;
    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
      const boxes = [...node.querySelectorAll("input:not([type=hidden]),select,textarea")].filter((c) => c !== el);
      if (boxes.length) break;
      const one = node.querySelector('[class*="single-value" i], [class*="singleValue" i]');
      if (one && clean(one.innerText)) return clean(one.innerText);
      const many = [...node.querySelectorAll('[class*="multi-value__label" i], [class*="multiValue" i] [class*="label" i]')]
        .map((m) => clean(m.innerText)).filter(Boolean);
      if (many.length) return many.join(", ");
    }
    return "";
  }
  const shortButton = (el) => (el.tagName === "BUTTON" || el.getAttribute("role") === "button" || el.getAttribute("role") === "radio") &&
    clean(el.innerText).length > 0 && clean(el.innerText).length <= 24;

  // The question a field (or a short Yes/No style button) belongs to: the label/legend/title in the nearest wrapper
  // that holds no other visible question field. Options of the same radio/checkbox group don't count.
  const NOT_A_QUESTION = /^\d+\s*\/\s*\d+|\bof \d+ characters?\b|^(invalid|error|please (enter|select|fill|provide)|this field is required|required)\b/i;
  function questionNode(el) {
    const option = isOption(el) || shortButton(el);
    let node = parentOf(el);
    for (let depth = 0; node && depth < 7; depth++, node = parentOf(node)) {
      if (node.tagName === "FORM" || node === document.body) break;
      const others = [...node.querySelectorAll(CONTROL)].filter((c) => c !== el && usable(c) && !(option && isOption(c)));
      if (others.length) break;
      for (const lab of node.querySelectorAll(LABELISH)) {
        if (lab.contains(el) || lab.querySelector("input,select,textarea,button")) continue;  // an option's own label
        if (lab.tagName === "LABEL" && lab.control) {
          if (lab.control === el ? option : (option || !isOption(lab.control))) continue;  // answer texts / other fields
        }
        const t = clean(lab.innerText);
        if (t && t.length < 240 && t !== clean(el.innerText) && !NOT_A_QUESTION.test(t)) return lab;
      }
    }
    return null;
  }
  // Question text as people read it: "Q Q Required" (visible + screen-reader copy) -> "Q *".
  function tidyQ(text) {
    let t = clean(text).replace(/\s*\brequired\b\s*$/i, " *");
    const twice = t.match(/^(.{4,}?)\s+\1(\s*\*)?$/);
    if (twice) t = twice[1] + (twice[2] || "");
    return clean(t);
  }
  const questionOf = (el) => { const n = questionNode(el); return n ? tidyQ(n.innerText) : ""; };
  const markedRequired = (text) => /\*\s*$|\(required\)\s*$|\brequired\s*$/i.test(text || "");
  // A label marks its field as required with a trailing *, a "required" class, or a CSS-drawn asterisk.
  function starred(node) {
    if (!node) return false;
    if (markedRequired(tidyQ(node.innerText))) return true;
    if (/required/i.test(node.getAttribute("class") || "") || node.querySelector('[class*="required" i]')) return true;
    for (const pseudo of ["::after", "::before"]) {
      const content = getComputedStyle(node, pseudo).content;
      if (content && content.includes("*")) return true;
    }
    return false;
  }
  const isRequired = (c) => c.required || c.getAttribute("aria-required") === "true" ||
    starred(c.labels && c.labels[0]) || starred(questionNode(c));
  const isPressed = (b) => (b.getAttribute("aria-pressed") || b.getAttribute("aria-checked")) === "true" ||
    /(^|[\s_-])(active|selected|checked|pressed|chosen)([\s_-]|$)/i.test(b.getAttribute("class") || "");
  function labelOf(el) {
    let t = el.getAttribute("aria-label") || "";
    if (!t && el.labels && el.labels.length) t = el.labels[0].innerText;
    const lb = el.getAttribute("aria-labelledby");
    if (!t && lb) {
      const rootNode = el.getRootNode && el.getRootNode();
      const first = lb.split(" ")[0];
      const n = (rootNode && rootNode.getElementById ? rootNode.getElementById(first) : null) || document.getElementById(first);
      if (n) t = n.innerText;
    }
    if (!t && isControl(el) && !(el.tagName === "INPUT" && ["submit", "button", "reset"].includes(el.type))) t = questionOf(el);
    if (!t && !["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName)) t = el.innerText;
    if (!t) t = el.getAttribute("placeholder") || el.getAttribute("title") || el.getAttribute("alt") || "";
    if (!t && el.tagName === "INPUT" && ["submit", "button", "reset"].includes(el.type)) t = el.value;
    if (!t && el.querySelector) { const i = el.querySelector("img[alt],[aria-label]"); if (i) t = i.getAttribute("alt") || i.getAttribute("aria-label"); }
    if (!t) t = el.getAttribute("name") || el.id || "";
    return tidyQ(t).slice(0, 90);
  }

  // Element ids are stable: an element keeps its number across snapshots and numbers are never reused (Karya passes
  // the next free number). So several actions planned from one snapshot still hit the right elements.
  K.snapshot = (args) => {
    args = args || {};
    const legacy = args.next == null && args.start != null;  // the extension's older background script
    let next = Math.max(1, Number(args.next || args.start) || 1);
    if (legacy && window !== window.top) next += 5000;  // it numbers frames from a shared count: keep frames apart
    const max = args.max || 150;
    if (window !== window.top && (window.innerWidth < 80 || window.innerHeight < 40)) return legacy ? [] : { items: [], next };
    const cands = [], seen = new Set(), tagged = [];
    const collect = (root) => {
      root.querySelectorAll("[data-jid]").forEach((e) => tagged.push(e));
      root.querySelectorAll(SEL).forEach((e) => { if (!seen.has(e)) { seen.add(e); cands.push(e); } });
      root.querySelectorAll("*").forEach((e) => { if (e.shadowRoot) collect(e.shadowRoot); });
    };
    collect(document);
    if (args.reset) tagged.forEach((e) => e.removeAttribute("data-jid"));  // numbers from an earlier Karya session
    else tagged.forEach((e) => { const n = Number(e.getAttribute("data-jid")); if (n >= next) next = n + 1; });
    const vh = window.innerHeight || 800, vw = window.innerWidth || 1200;
    const vis = [];
    for (const el of cands) {
      if (!usable(el)) continue;
      const parent = el.parentElement && el.parentElement.closest("a[href],button,[role=button]");
      if (parent && seen.has(parent) && !["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) &&
          (parent.innerText || "").trim() === (el.innerText || "").trim()) continue;
      vis.push({ el, r: (shown(el) ? el : visibleLabel(el) || el).getBoundingClientRect() });
    }
    const inView = (r) => r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
    vis.sort((a, b) => (inView(b.r) - inView(a.r)) || (a.r.top - b.r.top) || (a.r.left - b.r.left));
    const out = [];
    for (const { el, r } of vis) {
      if (out.length >= max) break;
      let id = Number(el.getAttribute("data-jid"));
      if (!id) { id = next++; el.setAttribute("data-jid", String(id)); }
      const tag = el.tagName.toLowerCase();
      const it = { id, tag, label: labelOf(el) };
      const role = el.getAttribute("role") || (isCombo(el) ? "combobox" : "");
      if (role) it.role = role;
      const type = el.getAttribute("type"); if (type) it.type = type.toLowerCase();
      if (tag === "input" || tag === "textarea") {
        if (isOption(el)) it.checked = el.checked;
        else if (it.type === "password") it.value = el.value ? "***" : "";
        else if (it.type !== "file") it.value = clean(el.value).slice(0, 80);
        else if (el.files && el.files.length) it.value = clean(el.files[0].name).slice(0, 80);
        if (el.placeholder) it.placeholder = clean(el.placeholder).slice(0, 60);
        if (el.name) it.name = el.name.slice(0, 40);
        if (el.maxLength > 0 && el.maxLength <= 12) it.maxlength = el.maxLength;
        if (/one-time-code/i.test(el.getAttribute("autocomplete") || "") ||
            /\b(otp|one[- ]?time|verification code|security code|confirmation code|login code|passcode|enter (the )?code)\b/i
              .test([it.label, el.placeholder, el.name, el.id].join(" "))) it.code = true;
        if (el.required || el.getAttribute("aria-required") === "true") it.required = true;
        if (it.type === "file") {   // Greenhouse labels both uploads "Attach": id="resume" / id="cover_letter" tell them apart
          if (el.id) it.key = el.id.slice(0, 40);
          if (el.accept) it.accept = el.accept.slice(0, 60);
        }
        if (!it.value && (role === "combobox" || isCombo(el))) {
          const chosen = comboValue(el);
          if (chosen) it.value = chosen.slice(0, 80);
        }
      }
      if (isControl(el) || shortButton(el)) {
        const q = questionOf(el);
        if (q && q !== it.label) it.question = q.slice(0, 160);
        if (isControl(el) && !it.required && isRequired(el)) it.required = true;
      }
      if (el.isContentEditable) { it.editable = true; it.value = clean(el.innerText).slice(0, 80); }
      if (tag === "select") {
        const o = el.options[el.selectedIndex]; it.value = o ? clean(o.text) : "";
        it.options = Array.from(el.options).slice(0, 300).map((o) => clean(o.text).slice(0, 40));
        if (el.required) it.required = true;
      }
      if (tag === "a") it.href = (el.getAttribute("href") || "").slice(0, 100);
      const ac = el.getAttribute("aria-checked") || el.getAttribute("aria-pressed");
      if (ac === "true" || ac === "false") it.checked = ac === "true";
      else if (it.question && shortButton(el) && isPressed(el)) it.checked = true;
      if (el.getAttribute("aria-invalid") === "true") it.invalid = true;
      if (el.disabled || el.getAttribute("aria-disabled") === "true") it.disabled = true;
      if (!inView(r)) it.offscreen = true;
      it.y = Math.round(r.top + (window.scrollY || 0));   // where it is on the page (blocks of a form go top to bottom)
      it.x = Math.round(r.left + (window.scrollX || 0));
      out.push(it);
    }
    // Drawn areas (canvas, SVG, game boards, maps) can't be clicked by label: list them so Karya can click or drag
    // at positions inside them. A chess board also gets its position.
    for (const el of drawnAreas()) {
      if (out.length >= max) break;
      let id = Number(el.getAttribute("data-jid"));
      if (!id) { id = next++; el.setAttribute("data-jid", String(id)); }
      if (out.some((o) => o.id === id)) continue;
      const r = el.getBoundingClientRect();
      const it = { id, tag: el.tagName.toLowerCase(), label: labelOf(el).slice(0, 60), area: true,
                   size: `${Math.round(r.width)}x${Math.round(r.height)}`,
                   box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)] };
      const board = readBoard(el);
      if (board) {
        it.board = "chess"; it.site = board.site; it.flipped = board.flipped; it.placement = board.placement;
        it.pieces = board.pieces;
      }
      if (!inView(r)) it.offscreen = true;
      out.push(it);
    }
    return legacy ? out : { items: out, next };
  };

  // ---------------------------------------------------------------- drawn areas and game boards
  const BOARD_SEL = "wc-chess-board, chess-board, cg-board, #board-single, .board-layout-chessboard .board";
  function drawnAreas() {
    const found = [];
    const add = (el) => {
      if (found.includes(el) || found.some((f) => f.contains(el) || el.contains(f))) return;
      const r = el.getBoundingClientRect();
      if (r.width >= 100 && r.height >= 100 && shown(el)) found.push(el);
    };
    document.querySelectorAll(BOARD_SEL).forEach(add);
    document.querySelectorAll("canvas, svg, [role=application]").forEach((el) => {
      if (el.tagName === "svg" && el.closest("button, a")) return;  // icons
      add(el);
    });
    return found.slice(0, 8);
  }
  const PIECE = { pawn: "p", knight: "n", bishop: "b", rook: "r", queen: "q", king: "k" };
  function readBoard(el) {
    const board = el.matches && el.matches(BOARD_SEL) ? el : (el.querySelector && el.querySelector(BOARD_SEL));
    if (!board) return null;
    const pieces = {};
    let site = "", flipped = false;
    board.querySelectorAll(".piece").forEach((p) => {  // chess.com: class "piece wp square-52" = white pawn on e2
      const cls = String(p.className && p.className.baseVal != null ? p.className.baseVal : p.className);
      const kind = (cls.match(/\b([wb][prnbqk])\b/) || [])[1];
      const sq = cls.match(/\bsquare-(\d)(\d)\b/);
      if (kind && sq) {
        site = "chess.com";
        pieces["abcdefgh"[Number(sq[1]) - 1] + sq[2]] = kind[0] === "w" ? kind[1].toUpperCase() : kind[1];
      }
    });
    if (site) flipped = /\bflipped\b/.test(String(board.className || "")) || !!board.closest(".flipped");
    else {
      const wrap = board.closest(".cg-wrap");
      const r = board.getBoundingClientRect();
      flipped = !!(wrap && /orientation-black/.test(wrap.className));
      board.querySelectorAll("piece").forEach((p) => {  // lichess: <piece class="white pawn" style="transform: translate(x, y)">
        const m = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)/.exec(p.style.transform || "");
        const parts = String(p.className).split(/\s+/);
        const name = parts.find((c) => PIECE[c]);
        const white = parts.includes("white");
        if (!m || !name || parts.includes("ghost")) return;
        let col = Math.round(Number(m[1]) / (r.width / 8)), row = Math.round(Number(m[2]) / (r.height / 8));
        if (flipped) { col = 7 - col; row = 7 - row; }
        if (col < 0 || col > 7 || row < 0 || row > 7) return;
        site = "lichess";
        pieces["abcdefgh"[col] + (8 - row)] = white ? PIECE[name].toUpperCase() : PIECE[name];
      });
    }
    if (!Object.keys(pieces).length) return null;
    const rows = [];
    for (let rank = 8; rank >= 1; rank--) {
      let row = "", empty = 0;
      for (const f of "abcdefgh") {
        const pc = pieces[f + rank];
        if (pc) { if (empty) { row += empty; empty = 0; } row += pc; } else empty++;
      }
      rows.push(row + (empty || ""));
    }
    return { site, flipped, pieces, placement: rows.join("/"), el: board };
  }
  K.board = (a) => {
    const el = a && a.id != null ? byId(a.id) : null;
    const b = readBoard(el || document.querySelector(BOARD_SEL) || document.body);
    if (!b) return { found: false };
    const r = b.el.getBoundingClientRect();
    const { el: _, ...info } = b;
    return { found: true, ...info, box: [r.left, r.top, r.width, r.height] };
  };
  K.promotionBox = (a) => {  // where to click to choose the promotion piece, if the site shows a picker
    const kind = (String((a && a.kind) || "q")[0] || "q").toLowerCase(), color = (a && a.color) === "b" ? "b" : "w";
    const pick = document.querySelector(`.promotion-window .promotion-piece.${color}${kind}, .promotion-piece.${color}${kind}`) ||
      document.querySelector(`#promotion-choice piece.${{ q: "queen", r: "rook", b: "bishop", n: "knight" }[kind] || "queen"}`);
    if (!pick || !shown(pick)) return null;
    const r = pick.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  };

  // ---------------------------------------------------------------- pointer actions at a position
  function pointerAt(type, x, y, buttons) {
    const target = document.elementFromPoint(x, y) || document.body;
    const o = { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y, screenX: x, screenY: y,
                button: 0, buttons, pointerId: 1, pointerType: "mouse", isPrimary: true, width: 1, height: 1, pressure: buttons ? 0.5 : 0 };
    const [p, m] = { down: ["pointerdown", "mousedown"], move: ["pointermove", "mousemove"], up: ["pointerup", "mouseup"] }[type];
    target.dispatchEvent(new PointerEvent(p, o));
    target.dispatchEvent(new MouseEvent(m, o));
    return target;
  }
  function pointFor(a, xKey, yKey) {
    if (a.id == null) return [Number(a[xKey]), Number(a[yKey])];  // page pixels (CSS px of this frame)
    const el = byId(a.id);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return [r.left + Number(a[xKey]) * r.width, r.top + Number(a[yKey]) * r.height];  // fractions of the element
  }
  async function clickAt(x, y, double) {
    const t = pointerAt("down", x, y, 1);
    await sleep(40);
    pointerAt("up", x, y, 0);
    t.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y }));
    if (double) t.dispatchEvent(new MouseEvent("dblclick", { bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y }));
    return t;
  }
  async function dragTo(x1, y1, x2, y2, steps) {
    pointerAt("down", x1, y1, 1);
    for (let i = 1; i <= steps; i++) {
      await sleep(15);
      pointerAt("move", x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps, 1);
    }
    await sleep(30);
    pointerAt("up", x2, y2, 0);
  }
  function squareCenter(board, sq, flipped) {
    const r = board.getBoundingClientRect();
    const f = "abcdefgh".indexOf(sq[0]), rank = Number(sq[1]);
    const col = flipped ? 7 - f : f, row = flipped ? rank - 1 : 8 - rank;
    return [r.left + (col + 0.5) * r.width / 8, r.top + (row + 0.5) * r.height / 8];
  }
  // Real mouse input (the extension's debugger): where to click, without clicking. Sites like chess.com and lichess
  // ignore simulated clicks, so the extension sends real ones at these points and then checks the result here.
  function boardFor(a) {
    return readBoard((a && a.id != null && byId(a.id)) || document.querySelector(BOARD_SEL) || document.body);
  }
  // Something on top (a pop-up, cookie banner, sign-up modal) would catch the click instead of the board/area.
  function coverAt(target, points) {
    for (const [x, y] of points) {
      const hit = document.elementFromPoint(x, y);
      if (!hit || target.contains(hit) || hit.contains(target)) continue;
      const box = hit.closest('[role=dialog], [aria-modal=true], [class*="modal" i], [class*="popup" i], ' +
        '[class*="overlay" i], [class*="banner" i], [class*="consent" i]') || hit;
      return clean(box.innerText || labelOf(box)).slice(0, 100) || box.tagName.toLowerCase();
    }
    return null;
  }
  const coveredError = (what, cover) => `something on the page is covering the ${what} ("${cover}"), so the click ` +
    "would hit it instead. Close it first (browser_snapshot shows its buttons, or browser_press Escape), then try again.";
  K.coverAt = (a) => {
    const el = (a && a.id != null && byId(a.id)) || document.querySelector(BOARD_SEL);
    return el ? coverAt(el, (a && a.points) || []) : null;
  };
  K.planMove = (a) => K.plan("move_piece", a);
  K.plan = (op, a) => {
    a = a || {};
    if (op === "click_at" || op === "drag") {
      if (a.id != null && !byId(a.id)) return { ok: false, error: `Element [${a.id}] is gone (the page changed). Take a new browser_snapshot.` };
      if (a.id != null) byId(a.id).scrollIntoView({ block: "center", inline: "center" });
      const p1 = pointFor(a, "x", "y");
      if (!p1 || p1.some((v) => !Number.isFinite(v))) return { ok: false, error: "x and y must be numbers" };
      if (a.id != null) {
        const cover = coverAt(byId(a.id), [p1]);
        if (cover) return { ok: false, error: coveredError("area", cover) };
      }
      if (op === "click_at") return { ok: true, points: [p1], target: labelOf(document.elementFromPoint(p1[0], p1[1]) || document.body).slice(0, 60) };
      const p2 = pointFor(a, "to_x", "to_y");
      if (!p2 || p2.some((v) => !Number.isFinite(v))) return { ok: false, error: "to_x and to_y must be numbers" };
      return { ok: true, points: [p1, p2] };
    }
    if (op === "move_piece") {
      const start = boardFor(a);
      if (!start) return { ok: false, error: "there's no chess board Karya can read on this page" };
      const from = String(a.from || "").toLowerCase(), to = String(a.to || "").toLowerCase();
      if (!/^[a-h][1-8]$/.test(from) || !/^[a-h][1-8]$/.test(to)) return { ok: false, error: "squares look like e2 and e4" };
      const piece = start.pieces[from];
      if (!piece) return { ok: false, error: `there's no piece on ${from} (board: ${start.placement})` };
      start.el.scrollIntoView({ block: "center" });
      const fromPt = squareCenter(start.el, from, start.flipped), toPt = squareCenter(start.el, to, start.flipped);
      const cover = coverAt(start.el, [fromPt, toPt]);
      if (cover) return { ok: false, error: coveredError("board", cover) };
      return { ok: true, from_sq: from, to_sq: to, id: a.id, kind: String(a.promotion || "q")[0].toLowerCase(),
               color: piece === piece.toUpperCase() ? "w" : "b", from: fromPt, to: toPt };
    }
    return { ok: false, error: `can't plan ${op}` };
  };
  K.checkMove = (p) => {
    const b = boardFor(p);
    return { moved: !!(b && !b.pieces[p.from_sq] && b.pieces[p.to_sq]), placement: b ? b.placement : "",
             promotion: K.promotionBox({ kind: p.kind, color: p.color }) };
  };

  async function movePiece(a) {
    const start = readBoard((a.id != null && byId(a.id)) || document.querySelector(BOARD_SEL) || document.body);
    if (!start) return { ok: false, error: "there's no chess board Karya can read on this page" };
    const from = String(a.from || "").toLowerCase(), to = String(a.to || "").toLowerCase();
    if (!/^[a-h][1-8]$/.test(from) || !/^[a-h][1-8]$/.test(to)) return { ok: false, error: "squares look like e2 and e4" };
    if (!start.pieces[from]) return { ok: false, error: `there's no piece on ${from} (board: ${start.placement})` };
    start.el.scrollIntoView({ block: "center" });
    const [x1, y1] = squareCenter(start.el, from, start.flipped), [x2, y2] = squareCenter(start.el, to, start.flipped);
    const cover = coverAt(start.el, [[x1, y1], [x2, y2]]);
    if (cover) return { ok: false, error: coveredError("board", cover) };
    const moved = () => { const now = readBoard(start.el); return now && !now.pieces[from] && now.pieces[to] ? now : null; };
    const promote = async () => {
      const kind = (String(a.promotion || "q")[0] || "q").toLowerCase(), color = start.pieces[from] === start.pieces[from].toUpperCase() ? "w" : "b";
      const pick = document.querySelector(`.promotion-window .promotion-piece.${color}${kind}, .promotion-piece.${color}${kind}`) ||
        document.querySelector(`#promotion-choice piece.${{ q: "queen", r: "rook", b: "bishop", n: "knight" }[kind] || "queen"}`);
      if (pick) { const r = pick.getBoundingClientRect(); await clickAt(r.left + r.width / 2, r.top + r.height / 2); await sleep(300); }
    };
    await clickAt(x1, y1); await sleep(150); await clickAt(x2, y2); await sleep(400); await promote();
    let after = moved();
    if (!after) { await dragTo(x1, y1, x2, y2, 12); await sleep(400); await promote(); after = moved(); }
    if (!after) return { ok: false, error: `the move ${from}-${to} didn't happen (not your turn, an illegal move, or the game isn't running). Board now: ${(readBoard(start.el) || start).placement}` };
    return { ok: true, moved: `${from}-${to}`, placement: after.placement };
  }

  K.text = () => (document.body ? document.body.innerText : "");

  // Required fields that are still empty, in the form that contains element `id` (or the whole page).
  K.formCheck = (args) => {
    const el = args && args.id != null ? byId(args.id) : null;
    const root = (el && el.closest("form")) || document;
    const empty = [], invalid = [], groups = new Map();
    deepAll(CONTROL, root).forEach((c) => {
      if (!usable(c) || c.disabled) return;  // file inputs and styled choices are often hidden behind their label
      const q = clean(questionOf(c) || labelOf(c)).slice(0, 90) || "(unnamed field)";
      if (c.getAttribute("aria-invalid") === "true") invalid.push(q);
      if (!isRequired(c)) return;
      if (isOption(c)) {  // a group counts as answered when any option is ticked
        const key = c.type === "radio" && c.name ? "r:" + c.name : "q:" + q;
        groups.set(key, { q, done: (groups.get(key) || {}).done || c.checked });
        return;
      }
      if (isFile(c)) { if (!c.files || !c.files.length) empty.push(q); return; }
      if (c.tagName === "SELECT") {
        const o = c.options[c.selectedIndex];
        if (!c.value || !o || /^(select|choose|please|--|-)/i.test(clean(o.text))) empty.push(q);
        return;
      }
      if (!clean(c.isContentEditable ? c.innerText : c.value)) empty.push(q);
    });
    groups.forEach((g) => { if (!g.done) empty.push(g.q); });
    // Required questions answered with buttons (Yes / No): answered when one of them is pressed.
    const choices = new Map();
    deepAll("button, [role=button], [role=radio]", root).forEach((b) => {
      if (!shown(b) || !shortButton(b)) return;
      const node = questionNode(b);
      if (!node || !starred(node)) return;
      const g = choices.get(node) || { q: tidyQ(node.innerText).slice(0, 90), n: 0, done: false };
      g.n += 1;
      g.done = g.done || isPressed(b);
      choices.set(node, g);
    });
    choices.forEach((g) => { if (g.n >= 2 && !g.done) empty.push(g.q); });
    return { empty: [...new Set(empty)].slice(0, 25), invalid: [...new Set(invalid)].slice(0, 25) };
  };

  // Everything filled in on the form that contains element `id`: shown to the user before they approve Submit.
  K.formValues = (args) => {
    const el = args && args.id != null ? byId(args.id) : null;
    const root = (el && el.closest("form")) || document;
    const out = [];
    deepAll(CONTROL, root).forEach((c) => {
      if (!usable(c) || c.disabled || c.type === "password") return;
      const q = clean(questionOf(c) || labelOf(c)).replace(/\s*[✱*]\s*$/, "").slice(0, 100) || "(field)";
      let v = "";
      if (isOption(c)) {
        if (!c.checked) return;
        const own = clean((c.labels && c.labels[0] && c.labels[0].innerText) || c.value);
        v = own && own !== q ? own : "ticked";
      } else if (isFile(c)) v = c.files && c.files.length ? c.files[0].name : "";
      else if (c.tagName === "SELECT") { const o = c.options[c.selectedIndex]; v = o && c.value ? clean(o.text) : ""; }
      else v = clean(c.isContentEditable ? c.innerText : c.value);
      if (v) out.push({ q, v: v.slice(0, 200) });
    });
    deepAll("button, [role=button], [role=radio]", root).forEach((b) => {
      if (!shown(b) || !shortButton(b) || !isPressed(b)) return;
      const node = questionNode(b);
      if (node) out.push({ q: tidyQ(node.innerText).replace(/\s*[✱*]\s*$/, "").slice(0, 100), v: clean(b.innerText) });
    });
    return out.slice(0, 40);
  };

  // ---------------------------------------------------------------- actions
  const byId = (id) => deepOne(`[data-jid="${id}"]`);
  function clickEl(el) {
    el.scrollIntoView({ block: "center", inline: "center" });
    const r = el.getBoundingClientRect();
    const o = { bubbles: true, cancelable: true, composed: true, view: window, button: 0, buttons: 1,
      clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    el.dispatchEvent(new PointerEvent("pointerdown", { ...o, pointerType: "mouse", isPrimary: true }));
    el.dispatchEvent(new MouseEvent("mousedown", o));
    if (el.focus) el.focus({ preventScroll: true });
    el.dispatchEvent(new PointerEvent("pointerup", { ...o, pointerType: "mouse", isPrimary: true, buttons: 0 }));
    el.dispatchEvent(new MouseEvent("mouseup", { ...o, buttons: 0 }));
    if (typeof el.click === "function") el.click();
    else el.dispatchEvent(new MouseEvent("click", { ...o, buttons: 0 }));
  }
  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    el.focus();
    setter.call(el, value);  // the prototype setter: frameworks like React notice the change
    el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  }
  function setEditable(el, value, clear) {
    el.focus();
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    if (!clear) range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
    if (!document.execCommand("insertText", false, value)) {
      el.textContent = clear ? value : el.textContent + value;
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    }
  }
  const OPTION_SEL = '[role="option"], [role="listbox"] li, .pac-item, [data-option-index], .dropdown-location, ' +
    '[class*="dropdown-results" i] > *, [class*="autocomplete" i] li, [class*="suggestion" i] li, [class*="suggestions" i] > div';
  const optionNodes = () => deepAll(OPTION_SEL)
    .filter((o) => shown(o) && clean(o.innerText) && clean(o.innerText).length <= 120 && !o.querySelector("input,select,textarea"));
  // The option that really is what was asked for: exact, starts with it as whole words ("Hyderabad" ->
  // "Hyderabad, Telangana, India"), or contains every typed word ("Hyderabad, India" -> "Hyderabad, Telangana, India").
  // "India" never picks "Indianapolis" or some Indian city: no good match -> null, and Karya shows the list instead.
  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const wordRe = (w) => new RegExp(`(^|[^\\p{L}\\p{N}])${escapeRe(w)}($|[^\\p{L}\\p{N}])`, "u");
  function bestMatch(texts, value) {
    const want = clean(value).toLowerCase();
    if (!want) return -1;
    const low = texts.map((t) => clean(t).toLowerCase());
    let i = low.indexOf(want);
    if (i >= 0) return i;
    const whole = wordRe(want);
    i = low.findIndex((t) => t.startsWith(want) && whole.test(t));
    if (i >= 0) return i;
    const words = want.split(/[\s,/()-]+/).filter((w) => w.length > 1);
    if (words.length > 1) return low.findIndex((t) => words.every((w) => wordRe(w).test(t)));
    return -1;
  }
  function bestOption(opts, value) {
    const i = bestMatch(opts.map((o) => o.innerText), value);
    return i >= 0 ? opts[i] : null;
  }
  // Karya's own browser clicks suggestions with a real mouse: mark the best one for it.
  K.listOptions = (args) => {
    const el = byId(args.id);
    const box = ownMenu(el);
    const nodes = box ? deepAll('[role="option"], [class*="option" i], li', box).filter((o) => shown(o)) : optionNodes();
    const texts = nodes.map((o) => clean(o.innerText).slice(0, 60)).filter((t) => t && !/^no options?\b/i.test(t));
    return [...new Set(texts)].slice(0, 30);
  };
  // Custom dropdowns (Instahyre, react-select, SmartRecruiters' spl-select...) draw their options only once they're
  // open, often as plain list items: the options are what appears after opening it, in its own menu if it names one.
  const MENU_SEL = OPTION_SEL + ', [role=menuitem], [role=menuitemradio], [role=listbox] > *, [role=menu] li, ' +
    '[class*="menu" i] li, [class*="dropdown" i] li, [class*="option" i], [class*="choice" i] li, ' +
    '[class*="choices" i] > *, [class*="select" i] li, [class*="list" i] > li';
  const menuish = (o, el) => shown(o) && o !== el && !(el && o.contains(el)) && !o.querySelector("input,select,textarea") &&
    clean(o.innerText) && clean(o.innerText).length <= 80;
  const menuNodes = (el, root) => deepAll(MENU_SEL, root).filter((o) => menuish(o, el));
  function ownMenu(el) {
    if (!el) return null;
    const rootNode = el.getRootNode ? el.getRootNode() : document;
    for (const attr of ["aria-controls", "aria-owns"]) {
      const id = el.getAttribute(attr) || (el.querySelector && (el.querySelector(`[${attr}]`) || {}).getAttribute &&
                                           el.querySelector(`[${attr}]`).getAttribute(attr));
      if (!id) continue;
      const box = (rootNode.getElementById && rootNode.getElementById(id)) || document.getElementById(id) ||
                  deepOne(`#${CSS.escape(id)}`);
      if (box) return box;
    }
    return null;
  }
  function leafOptions(nodes) {   // one node per option: the outermost of nested matches, first of equal texts
    const set = new Set(nodes);
    const seen = new Set();
    return nodes.filter((o) => {
      for (let p = parentOf(o); p; p = parentOf(p)) if (set.has(p) && clean(p.innerText) === clean(o.innerText)) return false;
      const t = clean(o.innerText).toLowerCase();
      if (seen.has(t)) return false;
      seen.add(t);
      return true;
    });
  }
  async function openMenu(el) {
    const before = new Set(menuNodes(el));
    clickEl(el);
    if (isControl(el)) el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", code: "ArrowDown", keyCode: 40,
                                                                         bubbles: true, composed: true }));
    for (let i = 0; i < 12; i++) {
      await sleep(i ? 150 : 250);
      const box = ownMenu(el);
      let found = box ? deepAll('[role="option"], [class*="option" i], li', box).filter((o) => menuish(o, el)) : [];
      if (!found.length) found = menuNodes(el).filter((o) => !before.has(o));
      // never options that were on the page already: they belong to another field (KLA listed its phone code)
      if (found.length) return leafOptions(found);
    }
    return [];
  }
  function closeMenu(el) {
    if (isControl(el)) { el.blur(); return; }
    if ((el.getAttribute("aria-expanded") || "") === "true" || menuNodes(el).length) clickEl(el);  // a toggle: click again
  }
  K.openOptions = async (a) => {
    const el = byId(a.id);
    if (!el) return { ok: false, error: `Element [${a.id}] is gone (the page changed). Take a new browser_snapshot.` };
    const opts = await openMenu(el);
    const texts = opts.map((o) => clean(o.innerText).slice(0, 60)).filter((t) => t && !/^no options?\b/i.test(t));
    closeMenu(el);
    return { ok: true, options: texts.slice(0, 40) };
  };
  K.markOption = (args) => {
    document.querySelectorAll("[data-karya-opt]").forEach((e) => e.removeAttribute("data-karya-opt"));
    const opts = optionNodes();
    if (!opts.length) return { count: 0 };
    const best = bestOption(opts, args.value);
    if (!best) return { count: opts.length, options: opts.slice(0, 8).map((o) => clean(o.innerText).slice(0, 60)) };
    best.setAttribute("data-karya-opt", "1");
    return { count: opts.length, text: clean(best.innerText).slice(0, 80) };
  };
  // After typing into a combobox (e.g. a "Location: Start typing..." field), choose the matching suggestion.
  K.pick = async (args) => {
    const rounds = args.quick ? 8 : 20;
    for (let i = 0; i < rounds; i++) {
      await sleep(150);
      const opts = optionNodes();
      if (opts.length) {
        const best = bestOption(opts, args.value);
        if (best) { const text = clean(best.innerText); clickEl(best); await sleep(200); return { picked: text.slice(0, 80) }; }
        if (i > Math.min(8, rounds - 2)) return { picked: null, options: opts.slice(0, 8).map((o) => clean(o.innerText).slice(0, 60)) };
      }
    }
    return { picked: null };
  };
  // Fields that usually want a suggestion picked even though the page doesn't mark them as a combobox.
  // "Email Address" and "Country Phone Code" are not places (Workday's email field once waited 45 s for suggestions).
  const placeLike = (el) => {
    const text = [labelOf(el), el.getAttribute("name"), el.getAttribute("id"), el.getAttribute("class")].join(" ");
    if (el.type === "email" || el.type === "tel" || /e-?mail|phone|mobile|\bcode\b|zip|postal|pin ?code/i.test(text)) return false;
    return /location|city|town|address|country|region/i.test(text);
  };

  async function selectOption(el, option) {
    if (el.tagName !== "SELECT" && el.querySelector) {
      const inner = el.querySelector("select");  // the id of a question block that holds the dropdown
      if (inner) el = inner;
    }
    if (el.tagName === "SELECT") {
      const opts = [...el.options];
      let i = opts.findIndex((x) => x.value === option && x.value !== "");
      if (i < 0) i = bestMatch(opts.map((x) => x.text), option);
      const o = i >= 0 ? opts[i] : null;
      if (!o) return { ok: false, error: `no option "${option}". Options: ${opts.slice(0, 20).map((x) => clean(x.text)).join(" | ")}` };
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set.call(el, o.value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, picked: clean(o.text) };
    }
    if (isControl(el) && !el.getAttribute("role") && el.tagName !== "INPUT") {
      return { ok: false, error: `[${el.getAttribute("data-jid")}] isn't a dropdown` };
    }
    // An option is a short piece of text that isn't (and doesn't hold) a form field or the dropdown itself.
    const opts = await openMenu(el);
    let best = bestOption(opts, option);
    if (!best && isControl(el) && el.tagName === "INPUT") {   // a searchable dropdown: type to narrow it, then pick
      setNative(el, String(option));
      const res = await K.pick({ value: option, quick: false });
      if (res.picked) return { ok: true, picked: res.picked };
    }
    if (best) { const text = clean(best.innerText); clickEl(best); await sleep(200); return { ok: true, picked: text.slice(0, 80) }; }
    const seen = opts.map((o) => clean(o.innerText).slice(0, 50)).filter(Boolean);
    closeMenu(el);
    return { ok: false, error: seen.length ? `no option "${option}" in that dropdown. Its options: ${seen.slice(0, 25).join(" | ")}`
                                           : `couldn't open that dropdown's list (no options appeared)` };
  }

  // Choosing in a dropdown by its id (Karya's own browser uses this too): a <select>, or a custom one opened and
  // picked like a person would.
  K.chooseOption = async (a) => {
    const el = byId(a.id);
    if (!el) return { ok: false, error: `Element [${a.id}] is gone (the page changed). Take a new browser_snapshot.` };
    return await selectOption(el, String(a.option));
  };

  function press(el, key) {
    const target = el || document.activeElement || document.body;
    const parts = String(key).split("+");
    const name = parts.pop();
    const mods = parts.map((p) => p.toLowerCase());
    const codes = { Enter: 13, Escape: 27, Tab: 9, ArrowDown: 40, ArrowUp: 38, ArrowLeft: 37, ArrowRight: 39, Backspace: 8, " ": 32, Space: 32 };
    const o = { key: name === "Space" ? " " : name, code: name, keyCode: codes[name] || 0, which: codes[name] || 0,
      bubbles: true, cancelable: true, composed: true, ctrlKey: mods.includes("control") || mods.includes("ctrl"),
      shiftKey: mods.includes("shift"), altKey: mods.includes("alt"), metaKey: mods.includes("meta") };
    const notCancelled = target.dispatchEvent(new KeyboardEvent("keydown", o));
    target.dispatchEvent(new KeyboardEvent("keypress", o));
    target.dispatchEvent(new KeyboardEvent("keyup", o));
    if (name === "Enter" && notCancelled && target.tagName === "INPUT" && target.form) {  // like a real Enter key
      try {
        if (target.form.requestSubmit) target.form.requestSubmit(); else target.form.submit();
      } catch (e) {  // e.g. the page's own pattern="" is invalid in this Chrome version
        return { ok: false, error: "The form didn't accept Enter (" + String(e && e.message || e).slice(0, 120) + "). Click its Next/Submit button instead." };
      }
    }
    return { ok: true };
  }

  // Tick a radio button / checkbox like a person: click it, or its label when the real input is hidden (LinkedIn).
  // If the page still didn't take it, set it the way frameworks notice. Reports what is selected afterwards.
  function choose(el, want) {
    if (el.checked !== want) {
      const label = visibleLabel(el);
      clickEl(!shown(el) && label ? label : el);
    }
    if (el.checked !== want) {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked").set.call(el, want);
      el.dispatchEvent(new Event("click", { bubbles: true }));
      el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    if (el.checked !== want) return { ok: false, error: `the page didn't accept ${want ? "selecting" : "clearing"} "${labelOf(el)}"` };
    return { ok: true, checked: el.checked, picked: labelOf(el) };
  }

  // A click that would open the system file picker: a file input, its label, or an "Upload / Attach" button that
  // sits next to one. Chrome refuses those without a real person's click.
  function opensFilePicker(el) {
    if (isFile(el)) return true;
    if (el.tagName === "LABEL" && el.control && isFile(el.control)) return true;
    const text = clean(el.innerText || el.getAttribute("aria-label") || el.value || "");
    if (!/\b(upload|attach|browse|choose file|select file|replace)\b/i.test(text) || text.length > 60) return false;
    const href = el.closest("a[href]") && el.closest("a[href]").getAttribute("href");
    if (href && href !== "#" && !href.startsWith("javascript")) return false;
    let node = el.parentElement;
    for (let d = 0; node && d < 4; d++, node = node.parentElement) {
      if (node.querySelector && node.querySelector("input[type=file]")) return true;
    }
    return false;
  }

  function fileInputNear(el) {
    if (el && isFile(el)) return el;
    let node = el;
    for (let d = 0; node && d < 8; d++, node = parentOf(node)) {
      const found = node.querySelector && (node.querySelector("input[type=file]") ||
                                           (node.shadowRoot && deepOne("input[type=file]", node.shadowRoot)));
      if (found) return found;
    }
    return deepOne("input[type=file]");
  }

  // ---------------------------------------------------------------- posts on a social feed (read only)
  // What people wrote on an X / Twitter search or profile page: text, author, time, link and counts. Used by
  // market_sentiment to read what traders say; it only reads what the page already shows.
  const countOf = (label, word) => {
    const m = (label || "").match(new RegExp("([\\d.,]+)\\s*([KMB])?\\s+" + word, "i"));
    if (!m) return 0;
    const n = parseFloat(m[1].replace(/,/g, ""));
    return Math.round(n * ({ K: 1e3, M: 1e6, B: 1e9 }[(m[2] || "").toUpperCase()] || 1));
  };
  K.socialPosts = () => {
    const out = [];
    if (/(^|\.)linkedin\.com$/i.test(location.hostname)) {
      // Two layouts: the older feed (data-urn="urn:li:activity:...") and the newer one (2026), where a post is a
      // list item marked componentkey="update-card..." with its text in data-testid="expandable-text-box".
      const CARD = '[data-urn*="urn:li:activity"], [data-urn*="urn:li:ugcPost"], [data-id*="urn:li:activity"], ' +
                   '[componentkey^="update-card"]';
      const BODY = '[data-testid="expandable-text-box"], .update-components-text, .feed-shared-update-v2__description, ' +
                   '.feed-shared-inline-show-more-text, [data-test-id="main-feed-activity-card__commentary"]';
      const seenText = new Set();
      for (const box of document.querySelectorAll(CARD)) {
        if (box.parentElement && box.parentElement.closest(CARD)) continue;       // a post shared inside another
        const body = box.querySelector(BODY);
        const text = body ? clean(body.innerText) : "";
        if (!text || seenText.has(text.slice(0, 120))) continue;
        seenText.add(text.slice(0, 120));
        const urn = box.getAttribute("data-urn") || box.getAttribute("data-id") ||
                    ((box.outerHTML.match(/urn:li:(activity|ugcPost):\d+/) || [""])[0]);
        const who = [...box.querySelectorAll('a[href*="/in/"], a[href*="/company/"]')].find((a) => clean(a.innerText));
        const actor = box.querySelector(".update-components-actor__title, .update-components-actor__name");
        const author = clean(who ? who.innerText : (actor ? actor.innerText : "")).split("•")[0].trim();
        const lines = (box.innerText || "").split("\n").map(clean).filter(Boolean).slice(0, 14);
        const when = (lines.find((l) => /^\d+\s*(s|m|h|d|w|mo|yr|y)\b/i.test(l)) || "").split("•")[0].trim();
        const profile = who ? who.href.split("?")[0] : "";
        // The newer layout has no link to the post itself (only behind its menu): link the author's activity,
        // where the post is listed.
        const url = urn ? `https://www.linkedin.com/feed/update/${urn}/`
                        : (/\/in\//.test(profile) ? profile.replace(/\/?$/, "/") + "recent-activity/all/" : profile);
        let likes = 0;
        for (const e of box.querySelectorAll("a, button, span")) {
          const m = clean(e.innerText).match(/^(\d[\d,]*)\s+reactions?\b/i);
          if (m) { likes = parseInt(m[1].replace(/,/g, ""), 10) || 0; break; }
        }
        out.push({ author: author.slice(0, 80), text: text.slice(0, 700), time: when, url, likes, replies: 0,
                   reposts: 0, views: 0, ad: /\bPromoted\b/i.test(lines.slice(0, 8).join(" ")) });
      }
      const login = !out.length && /\/(login|authwall|checkpoint|uas\/login|signup)/.test(location.pathname);
      return { site: location.hostname, posts: out, login };
    }
    if (!/(^|\.)(x|twitter)\.com$/i.test(location.hostname)) return { site: location.hostname, posts: out };
    for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
      const body = art.querySelector('[data-testid="tweetText"]');
      const text = body ? clean(body.innerText) : "";
      if (!text) continue;
      const t = art.querySelector("time");
      const link = t && t.closest('a[href*="/status/"]');
      const who = art.querySelector('[data-testid="User-Name"]');
      const group = art.querySelector('[role="group"][aria-label]');
      const label = group ? group.getAttribute("aria-label") : "";
      out.push({
        author: who ? ((who.innerText.match(/@[A-Za-z0-9_]+/) || [""])[0]) : "",
        text: text.slice(0, 700),
        time: t ? t.getAttribute("datetime") || "" : "",
        url: link ? link.href.split("?")[0] : "",
        replies: countOf(label, "repl"), reposts: countOf(label, "repost"), likes: countOf(label, "like"),
        views: countOf(label, "view"),
        ad: /\bAd\b|Promoted/.test(clean((art.querySelector('[data-testid="placementTracking"]') || {}).innerText || "")),
      });
    }
    const login = !out.length && (/\/(i\/flow\/)?login/.test(location.pathname) ||
      !!document.querySelector('[data-testid="loginButton"], a[href="/login"]'));
    return { site: location.hostname, posts: out, login };
  };

  // A whole page for crawl_site: its text and the links on it. Only reads.
  K.readPage = (a) => {
    a = a || {};
    const links = [];
    const seen = new Set();
    for (const el of document.querySelectorAll("a[href]")) {
      const href = el.href;
      if (!/^https?:/i.test(href) || seen.has(href)) continue;
      seen.add(href);
      links.push({ url: href, text: clean(el.innerText || el.getAttribute("aria-label") || "").slice(0, 80) });
      if (links.length >= 600) break;
    }
    const body = document.body ? document.body.innerText : "";
    const posts = (K.socialPosts().posts || []).filter((p) => !p.ad).slice(0, 40);
    return { url: location.href, title: document.title, text: body.slice(0, a.max || 60000), links, posts };
  };

  // ---------------------------------------------------------------- pop-ups
  // Nags that are never part of a task (the user, 2026-10-11: "Are you still looking for a job?" sat on the page).
  const NAG = /still looking for (a )?(new )?(job|work|role|opportunit)|actively looking|are you (still )?open to (work|new)|save (your )?log ?in info|remember (this|your) (device|login)\?|turn on (push |desktop )?notifications|allow notifications|enable notifications|get (the|our) app|download (the|our) app|open in (the )?app|use the app|continue in (the )?app|install (the|our) app|subscribe to (our )?newsletter|sign up for (our )?newsletter|rate (us|your experience)|how (likely|was your)|take (a|our) (short |quick )?survey|share your feedback|complete your profile|update your profile|add (a|your) (profile )?(photo|picture)|enable location|use your location|we use cookies|uses cookies|cookie (settings|preferences|policy|consent|notice)|accept (all )?cookies|personali[sz]ed ads|choose your cookie/i;
  const DISMISS = /^(x|×|✕|✖|close|close dialog|dismiss|not now|no,? thanks|no thank you|maybe later|later|ask me later|remind me later|skip|skip for now|cancel|got it|ok,? got it|okay|i'?ll do (it|this) later|not interested|no|continue without|reject( all)?|decline|necessary only|only necessary|essential only|use necessary cookies only|accept( all)?( cookies)?|allow( all)?( cookies)?|i agree|agree|ok)$/i;
  const DISMISS_RANK = (t) => { const l = t.toLowerCase();
    if (/^(x|×|✕|✖|close|close dialog|dismiss)$/.test(l)) return 1;
    if (/not now|no,? thanks|no thank you|maybe later|later|skip|not interested|continue without|cancel|^no$/.test(l)) return 2;
    if (/reject|decline|necessary|essential/.test(l)) return 3;
    if (/got it|okay|^ok$/.test(l)) return 4;
    return 5; };   // accept / agree: cookie banners that only offer that
  function popupBoxes() {
    const vh = window.innerHeight || 800, vw = window.innerWidth || 1200;
    const found = [];
    for (const box of deepAll('[role=dialog], [role=alertdialog], [aria-modal=true], dialog[open], [class*="modal" i], [class*="popup" i], [class*="overlay" i], [class*="dialog" i], [id*="cookie" i], [class*="cookie" i], [class*="consent" i], [class*="banner" i]')) {
      if (!shown(box) || found.some((f) => f.contains(box))) continue;
      const style = getComputedStyle(box), r = box.getBoundingClientRect();
      const floating = ["fixed", "sticky"].includes(style.position) || box.matches('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]');
      if (!floating || r.width < 120 || r.height < 30 || r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) continue;
      if (clean(box.innerText).length < 6) continue;
      for (let i = found.length - 1; i >= 0; i--) if (box.contains(found[i])) found.splice(i, 1);
      found.push(box);
    }
    return found;
  }
  function dismissOf(box) {
    const btns = [...box.querySelectorAll('button, [role=button], a[role=button], [aria-label], input[type=button], input[type=submit]')]
      .filter((b) => shown(b));
    let best = null, rank = 99;
    for (const b of btns) {
      const t = clean(b.getAttribute("aria-label") || b.innerText || b.value || b.getAttribute("title") || "");
      if (!t || t.length > 40 || !DISMISS.test(t)) continue;
      const k = DISMISS_RANK(t);
      if (k < rank) { best = b; rank = k; }
    }
    return best;
  }
  K.popups = () => popupBoxes().map((box) => {
    const close = dismissOf(box);
    return { text: clean(box.innerText).slice(0, 160), nag: NAG.test(clean(box.innerText).slice(0, 600)),
             close: close ? clean(close.getAttribute("aria-label") || close.innerText || close.value || "").slice(0, 30) : "",
             closeId: close ? Number(close.getAttribute("data-jid")) || null : null };
  });
  // Close only the nags (never a dialog that could be part of the task, like Easy Apply or a post composer).
  K.closePopups = () => {
    const closed = [];
    for (const box of popupBoxes()) {
      const text = clean(box.innerText);
      if (!NAG.test(text.slice(0, 600))) continue;
      if (box.querySelector('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea, select, [contenteditable=true]') &&
          !/cookie|consent/i.test(text.slice(0, 300))) continue;    // it asks for typing: leave it to the agent
      const btn = dismissOf(box);
      if (!btn) continue;
      btn.click();
      closed.push({ text: text.slice(0, 90), button: clean(btn.getAttribute("aria-label") || btn.innerText || btn.value || "") });
    }
    return { closed };
  };

  // ---------------------------------------------------------------- Gmail search results (for login codes)
  K.gmailRows = () => {
    if (/accounts\.google\.com/.test(location.host) || /ServiceLogin|signin/i.test(location.href)) return { signed_out: true, rows: [] };
    const rows = [];
    for (const tr of document.querySelectorAll(GMAIL_ROWS)) {
      const who = tr.querySelector('[email]');
      const subj = tr.querySelector('.bog, .bqe, [data-thread-id] span, .y6 span');
      const snip = tr.querySelector('.y2');
      const when = tr.querySelector('td.xW span[title], .xW span[title], span[title][aria-label]');
      rows.push({ email: who ? who.getAttribute("email") || "" : "", name: who ? clean(who.innerText) : "",
                  subject: subj ? clean(subj.innerText) : "", snippet: snip ? clean(snip.innerText).replace(/^[-\s]+/, "") : "",
                  when: when ? when.getAttribute("title") || "" : "" });
      if (rows.length >= 15) break;
    }
    return { rows };
  };

  const GMAIL_ROWS = 'tr.zA, [role=main] tr[jscontroller]';
  // Open one of the search results (the code is often only inside the email, not in its subject).
  K.gmailOpen = (a) => {
    const row = document.querySelectorAll(GMAIL_ROWS)[Number((a || {}).index) || 0];
    if (!row) return { ok: false };
    const target = row.querySelector('.bog, .y6, td.xY, [role=link]') || row;
    for (const type of ["mousedown", "mouseup", "click"]) {
      target.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
    }
    return { ok: true };
  };
  // The open email: its sender, time and text (the newest message of the conversation).
  K.gmailMessage = () => {
    const bodies = [...document.querySelectorAll('.a3s')].filter((b) => clean(b.innerText));
    if (!bodies.length) return {};
    const senders = [...document.querySelectorAll('.gD[email]')];
    const times = [...document.querySelectorAll('.g3[title]')];
    return { text: (bodies[bodies.length - 1].innerText || "").slice(0, 6000),
             email: senders.length ? senders[senders.length - 1].getAttribute("email") : "",
             when: times.length ? times[times.length - 1].getAttribute("title") : "" };
  };

  K.act = async (op, a) => {
    a = a || {};
    if (op === "gmailopen") return K.gmailOpen(a);
    if (op === "gmailmessage") return K.gmailMessage(a);
    if (op === "posts") return K.socialPosts(a);
    if (op === "popups") return K.popups(a);
    if (op === "closepopups") return K.closePopups(a);
    if (op === "gmailrows") return K.gmailRows(a);
    if (op === "readpage") return K.readPage(a);
    if (op === "openoptions") return await K.openOptions(a);
    if (op === "formcheck") return K.formCheck(a);
    if (op === "formvalues") return K.formValues(a);
    if (op === "mark") return K.markOption(a);
    if (op === "board") return K.board(a);
    if (op === "move_piece") return await movePiece(a);
    if (op === "click_at" || op === "drag") {
      if (a.id != null && !byId(a.id)) return { ok: false, error: `Element [${a.id}] is gone (the page changed). Take a new browser_snapshot.` };
      if (a.id != null) byId(a.id).scrollIntoView({ block: "center", inline: "center" });
      const p1 = pointFor(a, "x", "y");
      if (!p1 || p1.some((v) => !Number.isFinite(v))) return { ok: false, error: "x and y must be numbers" };
      if (a.id != null) {
        const cover = coverAt(byId(a.id), [p1]);
        if (cover) return { ok: false, error: coveredError("area", cover) };
      }
      if (op === "click_at") {
        const t = await clickAt(p1[0], p1[1], a.double);
        return { ok: true, at: p1.map(Math.round), target: labelOf(t).slice(0, 60) };
      }
      const p2 = pointFor(a, "to_x", "to_y");
      if (!p2 || p2.some((v) => !Number.isFinite(v))) return { ok: false, error: "to_x and to_y must be numbers" };
      await dragTo(p1[0], p1[1], p2[0], p2[1], Math.max(4, Math.min(40, Number(a.steps) || 12)));
      return { ok: true, from: p1.map(Math.round), to: p2.map(Math.round) };
    }
    const el = a.id != null ? byId(a.id) : null;
    if (a.id != null && !el) return { ok: false, error: `Element [${a.id}] is gone (the page changed). Take a new browser_snapshot.` };
    if (el && a.expect && a.expect !== el.tagName.toLowerCase()) {
      return { ok: false, error: `Element [${a.id}] isn't the ${a.expect} it was (the page changed). Take a new browser_snapshot.` };
    }
    if (op === "click") {
      // Pages may not open new tabs from a script click: hand such links to the extension instead.
      const link = el.closest("a[href]");
      if (link && link.target === "_blank" && /^https?:/i.test(link.href)) return { ok: true, open_url: link.href };
      if (el.tagName === "INPUT" && isOption(el)) return choose(el, el.type === "radio" ? true : !el.checked);
      if (opensFilePicker(el)) {  // Chrome only opens the file picker for a real person's click
        return { ok: false, error: "That button opens the file picker. Use browser_upload with this element id and the file path instead (Karya attaches the file directly)." };
      }
      clickEl(el); if (a.double) clickEl(el);
      return { ok: true };
    }
    // Typing done by Chrome itself (background.js type_native): page script only focuses the field before and checks
    // it after. Workday and similar forms ignore values set from script and kept every field "invalid" (2026-10-09).
    if (op === "focusfield") {
      const kind = (el.getAttribute("type") || "text").toLowerCase();
      if (!["INPUT", "TEXTAREA"].includes(el.tagName) || el.disabled || el.readOnly ||
          ["file", "checkbox", "radio", "hidden", "date", "month", "week", "time", "datetime-local", "color", "range",
           "submit", "button", "reset", "image"].includes(kind)) return { ok: false, error: "not a plain text field" };
      el.scrollIntoView({ block: "center", inline: "center" });
      el.focus({ preventScroll: true });
      const root = el.getRootNode ? el.getRootNode() : document;
      const focused = (root.activeElement || document.activeElement) === el;
      return { ok: focused, error: focused ? undefined : "couldn't focus that field", had: String(el.value || "").length };
    }
    if (op === "afterfill") {
      const want = String(a.value), now = String(el.value || "");
      const digits = (s) => s.replace(/\D/g, "");
      const same = (x, y) => x === y || x.replace(/\s+/g, "") === y.replace(/\s+/g, "") ||
        (digits(y).length >= 6 && digits(x) === digits(y));          // a phone mask reformats the digits
      const took = a.clear === false ? now.endsWith(want) || now.includes(want) : same(now, want);
      if (!took) return { ok: true, took: false, now: now.slice(0, 80) };
      const combo = isCombo(el);
      if ((combo || placeLike(el)) && !a.secret && a.pick !== false && !isSearch(el)) {
        const res = await K.pick({ value: a.value, quick: !combo });
        if (combo || res.picked || res.options) return { ok: true, took: true, combo: true, picked: res.picked, options: res.options };
      }
      if (!a.keep_focus && !combo) el.blur();  // many forms validate a field when it loses focus
      return { ok: true, took: true };
    }
    if (op === "set") {
      if (el.tagName === "SELECT" || (!["INPUT", "TEXTAREA"].includes(el.tagName) && !el.isContentEditable &&
          el.getAttribute("role") === "combobox")) return await selectOption(el, String(a.value));
      if (el.isContentEditable || (el.getAttribute("role") === "textbox" && !["INPUT", "TEXTAREA"].includes(el.tagName))) {
        setEditable(el, String(a.value), a.clear !== false);
        return { ok: true };
      }
      if (!["INPUT", "TEXTAREA"].includes(el.tagName)) return { ok: false, error: `[${a.id}] is a ${el.tagName.toLowerCase()}, not a text field` };
      setNative(el, a.clear === false ? el.value + String(a.value) : String(a.value));
      const combo = isCombo(el);
      if ((combo || placeLike(el)) && !a.secret && a.pick !== false && !isSearch(el)) {
        const res = await K.pick({ value: a.value, quick: !combo });
        if (combo || res.picked || res.options) return { ok: true, combo: true, picked: res.picked, options: res.options };
      }
      if (!a.keep_focus && !combo) el.blur();  // many forms validate a field when it loses focus
      return { ok: true };
    }
    if (op === "select") return await selectOption(el, String(a.option));
    if (op === "check") {
      const want = a.checked !== false;
      if (el.tagName === "INPUT" && isOption(el)) return choose(el, want);
      const now = (el.getAttribute("aria-checked") || el.getAttribute("aria-pressed")) === "true";
      if (now !== want) clickEl(el);
      return { ok: true };
    }
    if (op === "upload") {
      const input = fileInputNear(el);
      if (!input) return { ok: false, error: "there is no file upload field on this page" };
      if (a.mark) {           // Chrome itself will put the file in (a trusted change, like a person choosing it)
        deepAll("[data-karya-upload]").forEach((e) => e.removeAttribute("data-karya-upload"));
        input.setAttribute("data-karya-upload", "1");
        return { ok: true, marked: true };
      }
      const bytes = Uint8Array.from(atob(a.b64), (c) => c.charCodeAt(0));
      const dt = new DataTransfer();
      dt.items.add(new File([bytes], a.name, { type: a.mime || "application/octet-stream" }));
      input.files = dt.files;
      input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true, name: a.name };
    }
    if (op === "press") return press(el, a.key || "Enter");
    if (op === "scroll") {
      if (a.direction === "top") window.scrollTo(0, 0);
      else if (a.direction === "bottom") window.scrollTo(0, document.body.scrollHeight);
      else window.scrollBy(0, (a.direction === "up" ? -1 : 1) * window.innerHeight * 0.85 * (Number(a.pages) || 1));
      return { ok: true };
    }
    return { ok: false, error: `unknown action ${op}` };
  };

  window.__karya = K;
})();

// SPDX-License-Identifier: GPL-3.0-or-later
/* K1000 phone page. One websocket to the box; the box's Controller is the truth (the same one the
   knobs drive): this page only shows what the box pushes and sends small, validated requests.

   Safety rules carried here (the box enforces them again):
   - Values change only from explicit controls: +/- buttons (click = released on the button, so a
     scroll never fires one), the EQ dots (a drag must START on a dot), hold-to-act buttons.
   - Mute and "band flat" need a hold; the live-EQ reset needs a confirm.
   - The MUTE hold acts on what the finger MEANT when it went down: a hold that starts as "hold to
     mute" always sends a mute, whatever happens meanwhile. An unmute carries the mute generation
     (mute_seq) the page showed, and is dropped here (and refused by the box) if the mute changed
     during the hold: someone else's emergency mute is never undone.
   - A mute is confirmed by the box's ack (buzz), never by the send itself.
   - Drags are coalesced: at most one message per SEND_MS, the last value always sent.
   - No link = every control disabled and the page says so.
   - The page knows its own build (stamped by the box) and reloads itself once when the box was
     updated, so a cached app never runs against a newer box.
   - The phone mic (Phase 4, docs/11-phone-mic.md) is opt-in per visit (a tap on MIC), local (the
     audio never leaves the phone) and read-only: it draws behind the curve and never changes a
     value by itself; its peak marker only lets "notch here" land on the peak. */
"use strict";
(() => {
  const BANDS = ["live_peq.0", "live_peq.1", "live_peq.2", "live_peq.3", "live_lowshelf", "live_highshelf"];
  const SHORT = ["P1", "P2", "P3", "P4", "LS", "HS"];
  const LABELS = ["PEQ 1", "PEQ 2", "PEQ 3", "PEQ 4", "LO SHELF", "HI SHELF"];
  const KIND = ["peak", "peak", "peak", "peak", "low", "high"];
  // The box sends its RANGES in "hello"; these only cover the first paint.
  const RANGES = {
    "live_peq.freq": { min: 20, max: 20000 }, "live_peq.gain": { min: -20, max: 6 }, "live_peq.q": { min: 0.3, max: 16 },
    "live_lowshelf.freq": { min: 30, max: 1000 }, "live_lowshelf.gain": { min: -12, max: 6 }, "live_lowshelf.q": { min: 0.4, max: 1.5 },
    "live_highshelf.freq": { min: 1000, max: 16000 }, "live_highshelf.gain": { min: -12, max: 6 }, "live_highshelf.q": { min: 0.4, max: 1.5 },
  };
  const BUILD = "__K1000_BUILD__";            // stamped by the box (web/server.py load_static)
  const UNSTAMPED = "__K1000_" + "BUILD__";    // (split: the stamp replaces every whole mark)
  const PROTOCOL = 1;
  const SEND_MS = 50;          // drag coalescing
  const PING_MS = 2000;        // app-level ping (a browser cannot see websocket pings)
  const DEAD_MS = 6500;        // nothing heard for this long = link lost
  const CONNECT_MS = 4000;     // a connect attempt that hangs this long is abandoned
  const ECHO_MS = 450;         // after a drag, local values win for this long
  const LONG_MS = 550;         // long-press on empty graph = notch here
  const MUTE_MS = 600, UNMUTE_MS = 1200, FLAT_MS = 600;
  const ACK_MS = 1500;         // no ack for a mute/notch this long = say so
  const FS = 48000;
  const G_TOP = 9, G_BOT = -21; // graph gain span (dB)

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const body = document.body;

  let ws = null, open = false, lastRx = 0, retryMs = 500, retryTimer = 0, connectTimer = 0;
  let S = null, M = null, H = null;      // state, meters, hello
  let link = "nolink";                   // ok | nolink | off | busy | old
  let sel = clampInt(load("k1000.sel", 0), 0, 5);
  let tab = load("k1000.tab", "main") === "live" ? "live" : "main";
  const echo = new Array(6).fill(0);     // until when local band values win (ms epoch)
  const local = new Array(6).fill(null); // optimistic band values while dragging
  const stats = { sent: 0, t: {}, rxState: 0, connects: 0, hist: [], draws: 0 };
  let mutePend = null;                   // {want, timer}: a mute sent, its ack not seen yet
  let outdated = "";                     // the box's build when it differs from this page's
  let behind = false;                    // ...and a reload did not bring the new page
  const holds = new Set();               // hold-to-act buttons with a finger on them
  window.K1000 = { stats, state: () => S, meters: () => M, linked: () => open, sel: () => sel, link: () => link,
    build: BUILD, mutePending: () => !!mutePend, dragging: () => (drag ? { i: drag.i, wait: !!drag.wait } : null) };

  // ------------------------------------------------------------------ small helpers
  function load(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } }
  function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } }
  function clampInt(v, lo, hi) { v = Number.isInteger(v) ? v : lo; return Math.max(lo, Math.min(hi, v)); }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function num(v) { return typeof v === "number" && isFinite(v); }
  function minus(s) { return s.replace("-", "−"); }
  function fmtDb(v, signed = true) {
    if (!num(v)) return "--";
    if (Math.abs(v) < 0.05) return "0.0";
    return minus((signed && v > 0 ? "+" : "") + v.toFixed(1));
  }
  function fmtHz(v) { if (!num(v)) return "--"; return v >= 10000 ? (v / 1000).toFixed(1) + "k" : v >= 1000 ? (v / 1000).toFixed(2) + "k" : v.toFixed(0); }
  function fmtFreq(v) { if (!num(v)) return "--"; return v >= 1000 ? fmtHz(v).slice(0, -1) + " kHz" : v.toFixed(0) + " Hz"; }
  function fmtQ(v) { if (!num(v)) return "--"; return v >= 10 ? v.toFixed(1) : v.toFixed(2); }
  function sig3(v) { if (!num(v) || v <= 0) return v; const p = Math.pow(10, 2 - Math.floor(Math.log10(v))); return Math.round(v * p) / p; }
  function range(i, p) {
    const key = BANDS[i].replace(/\.\d$/, "") + "." + p;
    return (H && H.ranges && H.ranges[key]) || RANGES[key];
  }
  function band(i) {
    if (local[i] && Date.now() < echo[i]) return local[i];
    const b = S && S.bands && S.bands[i];
    return b ? { f: b.f, g: b.g, q: b.q } : null;
  }
  function canEdit() { return open && link === "ok" && S !== null; }
  let toastTimer = 0;
  function toast(text, ms = 2200) {
    const t = $("#toast"); t.textContent = text; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, ms);
  }
  function buzz(ms) { try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) { /* no vibration */ } }
  function sget(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function sset(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* private mode */ } }

  // ------------------------------------------------------------------ the link
  function wsUrl() { return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws"; }

  function connect() {
    clearTimeout(retryTimer); retryTimer = 0;
    if (ws) return;
    let sock;
    try { sock = new WebSocket(wsUrl()); } catch (e) { schedule(); return; }
    ws = sock;
    connectTimer = setTimeout(() => { if (ws === sock && !open) { drop(); schedule(); } }, CONNECT_MS);
    sock.onopen = () => {
      if (ws !== sock) return;
      clearTimeout(connectTimer);
      open = true; link = "ok"; lastRx = Date.now(); retryMs = 500; stats.connects++;
      psOn = false; syncProgspec();
      wake(); render();
    };
    sock.onmessage = (ev) => { if (ws !== sock) return; lastRx = Date.now(); onMessage(ev.data); };
    sock.onclose = (ev) => {
      if (ws !== sock) return;
      const code = ev.code;
      drop();
      if (code === 4003) link = "off";
      else if (code === 4002) link = "busy";
      else link = "nolink";
      render();
      schedule(code >= 4000 ? 3000 : 0);
    };
    sock.onerror = () => { /* onclose follows */ };
  }

  function drop() {
    clearTimeout(connectTimer);
    if (ws) {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      try { ws.close(); } catch (e) { /* already closed */ }
    }
    ws = null; open = false; psOn = false;
    if (link === "ok") link = "nolink";
    flushCancel();
    if (drag && drag.wait) notchDone(false, null, "Link lost: the notch was not set");
    if (mutePend) muteDone(false, null, "Link lost: use B2 on the box");
    render();
  }

  function schedule(min = 0) {
    if (retryTimer) return;
    const ms = Math.max(min, retryMs);
    retryMs = Math.min(retryMs * 2, 3000);
    retryTimer = setTimeout(() => { retryTimer = 0; connect(); }, ms);
  }

  setInterval(() => {
    if (!open) return;
    if (Date.now() - lastRx > DEAD_MS) { drop(); schedule(); return; }
    send({ t: "ping", ts: Date.now() }, true);
  }, PING_MS);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (open && Date.now() - lastRx > DEAD_MS) drop();
    if (!ws) { retryMs = 500; clearTimeout(retryTimer); retryTimer = 0; connect(); }
    wake();
  });
  window.addEventListener("online", () => { if (!ws) { retryMs = 500; connect(); } });

  function send(obj, quiet) {
    if (!open || !ws) { if (!quiet) toast("Not connected to K1000"); return false; }
    try { ws.send(JSON.stringify(obj)); } catch (e) { return false; }
    stats.sent++; stats.t[obj.t] = (stats.t[obj.t] || 0) + 1;
    return true;
  }

  function onMessage(data) {
    let m;
    try { m = JSON.parse(data); } catch (e) { return; }
    if (!m || typeof m !== "object") return;
    switch (m.t) {
      case "hello":
        H = m;
        if (m.phone_on === false) link = "off";
        checkBuild(m);
        break;
      case "state":
        S = m; stats.rxState++;
        systemChanged(m);
        stats.hist.push([Date.now(), m.master_db]); if (stats.hist.length > 60) stats.hist.shift();
        link = m.phone_on === false ? "off" : link === "old" ? "old" : "ok";
        render();
        break;
      case "m":
        M = m; drawMeters();
        break;
      case "ps":
        onProgram(m);
        break;
      case "ack":
        if (m.id === "mute") muteDone(true, m.v, "");
        else if (m.id === "notch" && m.v && Number.isInteger(m.v.s)) notchDone(true, m.v, "");
        break;
      case "err":
        if (m.id === "mute") muteDone(false, m.v, String(m.why || "refused"));
        else if (m.id === "notch") notchDone(false, null, String(m.why || "refused"));
        else toast(String(m.why || "refused"));
        break;
      case "bye":
        if (m.code === 4003 || m.phone_on === false) link = "off";
        else if (m.code === 4002) link = "busy";
        render();
        break;
      default:
        break;
    }
  }

  // ------------------------------------------------------------------ rendering
  const el = {
    link: $("#link"), linkTxt: $("#link span"), eng: $("#engine"), engTxt: $("#engine span"),
    dev: $("#device"), pill: $("#muted-pill"), banners: $("#banners"),
    master: $("#master-val"), bal: $("#bal-val"), balSub: $("#bal-sub"),
    lc: $("#lc-switch"), lcTxt: $("#lc-switch span"), lcVal: $("#lc-val"),
    muteLabel: $("#mute-label"), slots: $("#slots"), slotName: $("#slot-name"), slotState: $("#slot-state"),
    pf: $("#p-freq"), pg: $("#p-gain"), pq: $("#p-q"),
  };

  function chip(node, txt, cls, label) {
    node.className = "chip " + cls; txt.textContent = label;
  }

  function banners() {
    const out = [];
    if (link === "old") {
      out.push(["amber", "This page is out of date", "Close K1000 (swipe it away) and open it again."]);
    } else if (outdated) {
      out.push(["grey", "K1000 was updated: reloading the page…", ""]);
    } else if (behind) {
      out.push(["grey", "K1000 was updated", "Close K1000 (swipe it away) and open it again for the new page."]);
    }
    if (link === "off") {
      out.push(["amber", "Phone control is OFF on the box", "Turn it on at the box: SETUP › Phone. The knobs are in charge."]);
    } else if (link === "busy") {
      out.push(["amber", "K1000 already has the maximum number of phones", "Close the page on another phone. Retrying…"]);
    } else if (!open) {
      out.push(["red", "Not connected to K1000 — join the K1000 Wi-Fi",
        "Scan the Wi-Fi QR on the box (SETUP › Phone). Phone in airplane mode + Wi-Fi on. Retrying…"]);
    }
    if (open && S) {
      const eng = S.engine;
      if (S.device_present === false) {
        out.push(["orange", "NO DEVICE — " + (S.device || "audio interface") + " not found", "No audio. Check the USB cable and power."]);
      } else if (eng === null || /^(inactive|stalled)$/i.test(eng || "")) {
        out.push(["red", "ENGINE DOWN", "CamillaDSP is not processing: no audio. Restarting automatically."]);
      }
      if (S.message) out.push(["amber", S.message, ""]);
      if (S.drift && S.drift.length) out.push(["grey", "Changed outside K1000", S.drift.join(" · ")]);
    }
    const html = out.map(([cls, t, small]) => {
      const d = document.createElement("div");
      d.className = "banner " + cls;
      d.textContent = t;
      if (small) { const s = document.createElement("small"); s.textContent = small; d.appendChild(s); }
      return d;
    });
    el.banners.replaceChildren(...html);
  }

  function render() {
    body.classList.toggle("nolink", !open && link !== "off");
    body.classList.toggle("off", link === "off" || link === "old");
    const muted = !!(S && S.muted);
    body.classList.toggle("muted", muted);
    // status bar
    if (open && link === "ok") chip(el.link, el.linkTxt, "ok", "LINK");
    else if (link === "off") chip(el.link, el.linkTxt, "warn", "PHONE OFF");
    else if (link === "busy") chip(el.link, el.linkTxt, "warn", "BUSY");
    else if (link === "old") chip(el.link, el.linkTxt, "warn", "OLD PAGE");
    else chip(el.link, el.linkTxt, "bad", "NO LINK");
    if (open && S) {
      const e = S.engine;
      if (e === null) chip(el.eng, el.engTxt, "bad", "NO ENGINE");
      else if (!e || /^unknown$/i.test(e)) chip(el.eng, el.engTxt, "unk", "ENGINE ?");
      else if (/^running$/i.test(e)) chip(el.eng, el.engTxt, "ok", "RUNNING");
      else if (/^(paused|starting)$/i.test(e)) chip(el.eng, el.engTxt, "warn", e.toUpperCase());
      else chip(el.eng, el.engTxt, "bad", e.toUpperCase());
      const prof = (S.profile || "?").toUpperCase();
      el.dev.textContent = prof + (S.device ? " · " + S.device : "") + (S.device_present === false ? " MISSING" : S.device_present === null ? " ?" : "");
      el.dev.className = "dev" + (S.device_present === false ? " missing" : S.device_present === null ? " unknown" : "");
    } else {
      chip(el.eng, el.engTxt, "unk", "ENGINE ?");
      el.dev.textContent = " "; el.dev.className = "dev";
    }
    el.pill.hidden = !muted;
    banners();
    // MAIN
    el.master.textContent = S ? fmtDb(S.master_db) : "--";
    const b = S ? S.balance_db : null;
    el.bal.textContent = fmtDb(b);
    el.balSub.textContent = !num(b) ? "" : Math.abs(b) < 0.05 ? "subs = tops"
      : "subs " + fmtDb(b / 2) + " · tops " + fmtDb(-b / 2);
    const lcOn = !!(S && S.lowcut_on);
    el.lc.setAttribute("aria-checked", lcOn ? "true" : "false");
    el.lcTxt.textContent = S ? (lcOn ? "ON" : "OFF") : "--";
    el.lcVal.textContent = S ? fmtHz(S.lowcut_hz) : "--";
    el.lcVal.parentElement.classList.toggle("off-value", !lcOn);
    el.muteLabel.textContent = mutePend ? (mutePend.want ? "MUTING…" : "UNMUTING…")
      : holdUnmutes() ? "MUTED · HOLD TO UNMUTE" : muted ? "HOLD TO KEEP MUTED" : "HOLD TO MUTE";
    // LIVE
    renderSlots();
    drawEq();
    drawMeters();   // no link = empty meters, never the last levels frozen
  }

  function renderSlots() {
    if (!el.slots.children.length) {
      SHORT.forEach((name, i) => {
        const bt = document.createElement("button");
        bt.textContent = name; bt.dataset.slot = String(i);
        bt.addEventListener("click", () => select(i));
        el.slots.appendChild(bt);
      });
    }
    Array.from(el.slots.children).forEach((bt, i) => {
      const v = band(i);
      const g = v && num(v.g) ? v.g : 0;
      bt.classList.toggle("cut", g <= -0.05);
      bt.classList.toggle("boost", g >= 0.05);
      bt.classList.toggle("sel", i === sel);
    });
    const v = band(sel);
    el.slotName.textContent = LABELS[sel];
    el.pf.textContent = v ? fmtHz(v.f) : "--";
    el.pg.textContent = v ? fmtDb(v.g) : "--";
    el.pq.textContent = v ? fmtQ(v.q) : "--";
    const g = v && num(v.g) ? v.g : 0;
    el.slotState.textContent = Math.abs(g) < 0.05 ? "flat (inactive)" : g < 0 ? "cut" : "boost";
  }

  function select(i) {
    sel = clampInt(i, 0, 5); save("k1000.sel", sel);
    renderSlots(); drawEq();
  }

  // ------------------------------------------------------------------ meters
  const meterEls = $$("#meters .m").map((m) => ({ mask: m.querySelector(".bar b"), clip: m.querySelector("em") }));
  function drawMeters() {
    const l = open && M && Array.isArray(M.l) ? M.l : [-60, -60, -60, -60];
    const c = open && M && Array.isArray(M.c) ? M.c : [false, false, false, false];
    meterEls.forEach((m, i) => {
      const frac = clamp(((num(l[i]) ? l[i] : -60) + 60) / 60, 0, 1);
      m.mask.style.width = ((1 - frac) * 100).toFixed(1) + "%";
      m.clip.classList.toggle("clip", !!c[i]);
    });
  }

  // ------------------------------------------------------------------ the EQ graph
  const cv = $("#eq");
  const ctx = cv.getContext("2d");
  let GW = 0, GH = 0;
  const PAD = { l: 34, r: 12, t: 12, b: 22 };
  function sizeCanvas() {
    const r = cv.getBoundingClientRect();
    if (r.width < 10) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    GW = r.width; GH = r.height;
    drawEq();
  }
  const xOf = (f) => PAD.l + (Math.log10(f / 20) / 3) * (GW - PAD.l - PAD.r);
  const fOf = (x) => 20 * Math.pow(10, 3 * (x - PAD.l) / (GW - PAD.l - PAD.r));
  const yOf = (g) => PAD.t + (G_TOP - g) / (G_TOP - G_BOT) * (GH - PAD.t - PAD.b);
  const gOf = (y) => G_TOP - (y - PAD.t) / (GH - PAD.t - PAD.b) * (G_TOP - G_BOT);

  function coeffs(kind, f0, g, q) {
    const A = Math.pow(10, g / 40), w0 = 2 * Math.PI * f0 / FS, c = Math.cos(w0), s = Math.sin(w0);
    const al = s / (2 * q);
    if (kind === "peak") return [1 + al * A, -2 * c, 1 - al * A, 1 + al / A, -2 * c, 1 - al / A];
    const sq = 2 * Math.sqrt(A) * al;
    if (kind === "low") {
      return [A * ((A + 1) - (A - 1) * c + sq), 2 * A * ((A - 1) - (A + 1) * c), A * ((A + 1) - (A - 1) * c - sq),
        (A + 1) + (A - 1) * c + sq, -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - sq];
    }
    return [A * ((A + 1) + (A - 1) * c + sq), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - sq),
      (A + 1) - (A - 1) * c + sq, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - sq];
  }
  function magDb(co, f) {
    const w = 2 * Math.PI * f / FS;
    const c1 = Math.cos(w), s1 = Math.sin(w), c2 = Math.cos(2 * w), s2 = Math.sin(2 * w);
    const nr = co[0] + co[1] * c1 + co[2] * c2, ni = -(co[1] * s1 + co[2] * s2);
    const dr = co[3] + co[4] * c1 + co[5] * c2, di = -(co[4] * s1 + co[5] * s2);
    return 10 * Math.log10((nr * nr + ni * ni) / (dr * dr + di * di));
  }
  let flash = null; // {x, until}
  function drawEq() {
    if (tab !== "live" || !GW) return;
    stats.draws++;
    const w = GW, h = GH;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#070707"; ctx.fillRect(0, 0, w, h);
    drawMicUnder(w, h);                 // the phone mic's trace: behind the grid and the curve
    ctx.font = "11px system-ui, Roboto, sans-serif";
    ctx.textBaseline = "top"; ctx.textAlign = "center";
    for (const f of [20, 30, 40, 50, 60, 70, 80, 90, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000, 20000]) {
      const x = Math.round(xOf(f)) + 0.5;
      const major = [100, 1000, 10000].includes(f);
      ctx.strokeStyle = major ? "#3a3a3a" : "#1c1c1c";
      ctx.beginPath(); ctx.moveTo(x, PAD.t); ctx.lineTo(x, h - PAD.b); ctx.stroke();
    }
    ctx.fillStyle = "#8a8a8a";
    for (const [f, t] of [[50, "50"], [100, "100"], [200, "200"], [500, "500"], [1000, "1k"], [2000, "2k"], [5000, "5k"], [10000, "10k"]]) {
      ctx.fillText(t, xOf(f), h - PAD.b + 5);
    }
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (const g of [6, 0, -6, -12, -18]) {
      const y = Math.round(yOf(g)) + 0.5;
      ctx.strokeStyle = g === 0 ? "#5a5a5a" : "#222";
      ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(w - PAD.r, y); ctx.stroke();
      ctx.fillStyle = g === 0 ? "#bbb" : "#777";
      ctx.fillText(g > 0 ? "+" + g : minus(String(g)), PAD.l - 5, y);
    }
    drawMicLabels(w, h);
    // the summed live-EQ response
    const cos = [];
    for (let i = 0; i < 6; i++) {
      const v = band(i);
      if (v && num(v.f) && num(v.g) && num(v.q) && Math.abs(v.g) >= 0.05) cos.push(coeffs(KIND[i], v.f, v.g, v.q));
    }
    const N = Math.max(80, Math.floor((w - PAD.l - PAD.r) / 2));
    const pts = [];
    for (let k = 0; k <= N; k++) {
      const x = PAD.l + k * (w - PAD.l - PAD.r) / N;
      const f = fOf(x);
      let g = 0;
      for (const co of cos) g += magDb(co, f);
      pts.push([x, yOf(clamp(g, G_BOT, G_TOP))]);
    }
    const y0 = yOf(0);
    ctx.beginPath(); ctx.moveTo(pts[0][0], y0);
    for (const [x, y] of pts) ctx.lineTo(x, y);
    ctx.lineTo(pts[pts.length - 1][0], y0); ctx.closePath();
    ctx.fillStyle = "rgba(255,184,0,0.16)"; ctx.fill();
    ctx.beginPath();
    pts.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.strokeStyle = S && open ? "#ffffff" : "#666"; ctx.lineWidth = 2.5; ctx.stroke(); ctx.lineWidth = 1;
    if (flash && Date.now() < flash.until) {
      ctx.strokeStyle = "#ffb800"; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(flash.x, PAD.t); ctx.lineTo(flash.x, h - PAD.b); ctx.stroke(); ctx.lineWidth = 1;
    }
    drawPeakMarker();
    // the dots: unselected first, the selected one on top
    const order = [0, 1, 2, 3, 4, 5].filter((i) => i !== sel).concat([sel]);
    ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.font = "bold 12px system-ui, Roboto, sans-serif";
    for (const i of order) {
      const v = band(i);
      if (!v || !num(v.f) || !num(v.g)) continue;
      const x = xOf(v.f), y = yOf(clamp(v.g, G_BOT, G_TOP));
      const active = Math.abs(v.g) >= 0.05;
      const r = i === sel ? 17 : active ? 14 : 10;
      ctx.beginPath(); ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.fillStyle = !active ? "#202020" : v.g < 0 ? "#ff5a00" : "#28dc46";
      ctx.fill();
      ctx.lineWidth = i === sel ? 3 : 1.5;
      ctx.strokeStyle = i === sel ? "#ffffff" : active ? "#000" : "#5a5a5a";
      ctx.stroke(); ctx.lineWidth = 1;
      ctx.fillStyle = active ? "#000" : i === sel ? "#ddd" : "#8a8a8a";
      ctx.font = (r < 12 ? "bold 10px" : "bold 12px") + " system-ui, Roboto, sans-serif";
      ctx.fillText(SHORT[i], x, y + 0.5);
    }
  }

  function pos(e) { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
  // Grabbable: the active bands and the selected one. Flat bands are parked on the 0 dB line and
  // must not swallow a long-press meant as "notch here" near their frequency.
  function grabbable(i) { const v = band(i); return !!v && num(v.f) && num(v.g) && (i === sel || Math.abs(v.g) >= 0.05); }
  function hit(p) {
    let best = -1, bestD = 30;
    for (let i = 0; i < 6; i++) {
      if (!grabbable(i)) continue;
      const v = band(i);
      const d = Math.hypot(xOf(v.f) - p.x, yOf(clamp(v.g, G_BOT, G_TOP)) - p.y) - (i === sel ? 4 : 0);
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  }
  function capture(node, id) { try { node.setPointerCapture(id); } catch (e) { /* synthetic or ended pointer */ } }

  // coalesced sender: at most one message per SEND_MS, always the latest
  let pend = null, pendTimer = 0, lastSent = 0;
  function queue(msg) {
    pend = msg;
    if (pendTimer) return;
    const wait = Math.max(0, SEND_MS - (performance.now() - lastSent));
    pendTimer = setTimeout(flush, wait);
  }
  function flush() {
    clearTimeout(pendTimer); pendTimer = 0;
    if (!pend) return;
    send(pend, true); pend = null; lastSent = performance.now();
  }
  function flushCancel() { clearTimeout(pendTimer); pendTimer = 0; pend = null; }

  let drag = null, lp = null;
  cv.addEventListener("pointerdown", (e) => {
    if (!canEdit() || drag || lp) return;
    const p = pos(e);
    const i = hit(p);
    capture(cv, e.pointerId);
    e.preventDefault();
    if (i >= 0) {
      const v = band(i);
      select(i);
      drag = { i, id: e.pointerId, dx: xOf(v.f) - p.x, dy: yOf(clamp(v.g, G_BOT, G_TOP)) - p.y, q: v.q };
      local[i] = { f: v.f, g: v.g, q: v.q }; echo[i] = Infinity;
      return;
    }
    const id = e.pointerId;
    lp = { id, x: p.x, y: p.y, timer: setTimeout(() => notchHere(p.x, p.y, id), LONG_MS) };
  });
  cv.addEventListener("pointermove", (e) => {
    const p = pos(e);
    if (drag && e.pointerId === drag.id && !drag.ended) {
      const i = drag.i;
      const fr = range(i, "freq"), gr = range(i, "gain");
      const f = sig3(clamp(fOf(p.x + drag.dx), fr.min, fr.max));
      const g = Math.round(clamp(gOf(p.y + drag.dy), gr.min, gr.max) * 2) / 2;
      const cur = local[i];
      if (cur && cur.f === f && cur.g === g) return;
      local[i] = { f, g, q: drag.q };
      if (!drag.wait) queue({ t: "band", s: i, f, g });   // a new notch: only once the box took it
      renderSlots(); drawEq();
    } else if (lp && e.pointerId === lp.id && Math.hypot(p.x - lp.x, p.y - lp.y) > 12) {
      clearTimeout(lp.timer); lp = null;
    }
  });
  function endDrag() {
    if (!drag) return;
    flush();
    echo[drag.i] = Date.now() + ECHO_MS;
    clearTimeout(drag.timer);
    drag = null;
    setTimeout(() => { renderSlots(); drawEq(); }, ECHO_MS + 20);
  }
  function endPointer(e) {
    if (drag && e.pointerId === drag.id) {
      if (drag.wait) drag.ended = true;     // the finger left before the box took the notch
      else endDrag();
    }
    if (lp && e.pointerId === lp.id) { clearTimeout(lp.timer); lp = null; }
  }
  cv.addEventListener("pointerup", endPointer);
  cv.addEventListener("pointercancel", endPointer);
  cv.addEventListener("contextmenu", (e) => e.preventDefault());

  // "Notch here": the first flat PEQ goes to this frequency at the notch cut and Q, and the SAME
  // finger keeps it: sliding on moves the new notch (no lift and re-grab). The page predicts the
  // slot (the box's own rule) and names it; the box takes it only if it is still flat, and the
  // drag's band messages wait for that ack, so they can never move a band someone else just set.
  function notchHere(x, y, pid) {
    lp = null;
    if (!canEdit() || drag) return;
    const fx = x;                       // where the finger is (the drag keeps this offset)
    const pk = peakNear(x, y);          // a long-press on the mic's peak pointer notches AT the peak
    if (pk) x = xOf(pk);
    const hz = sig3(clamp(fOf(x), 20, 20000));
    flash = { x, until: Date.now() + 700 };
    setTimeout(drawEq, 720);
    buzz(40);
    let s = -1;
    for (let i = 0; i < 4; i++) { const v = band(i); if (v && num(v.g) && Math.abs(v.g) < 0.05) { s = i; break; } }
    if (s < 0) { send({ t: "notch", hz, id: "notch" }); drawEq(); return; }   // the box says "all 4 in use"
    const n = (H && H.notch) || {};
    const g = num(n.gain) ? n.gain : -6, q = num(n.q) ? n.q : 8;
    if (!send({ t: "notch", hz, s, id: "notch" })) { drawEq(); return; }
    local[s] = { f: hz, g, q }; echo[s] = Infinity;
    sel = s; save("k1000.sel", sel);
    drag = { i: s, id: pid, dx: xOf(hz) - fx, dy: yOf(clamp(g, G_BOT, G_TOP)) - y, q, wait: true, ended: false, peak: !!pk,
      timer: setTimeout(() => notchDone(false, null, "No answer from K1000 for the notch"), ACK_MS) };
    renderSlots(); drawEq();
  }
  function notchDone(ok, v, why) {
    const d = drag && drag.wait ? drag : null;
    const onPeak = !!(d && d.peak);
    if (d) clearTimeout(d.timer);
    if (!ok) {
      if (d) { local[d.i] = null; echo[d.i] = 0; drag = null; renderSlots(); drawEq(); }
      toast(why);
      return;
    }
    if (d && d.i === v.s) {
      d.wait = false;
      const cur = local[d.i];
      if (cur && (cur.f !== v.f || cur.g !== v.g)) queue({ t: "band", s: d.i, f: cur.f, g: cur.g });
      if (d.ended) endDrag();
    } else {
      select(v.s);
    }
    toast("Notch " + SHORT[v.s] + (onPeak ? " on the peak, " : " at ") + fmtFreq(v.f) + " · " + fmtDb(v.g) + " dB · slide to move it", 1800);
  }

  // ------------------------------------------------------------------ the phone mic (Phase 4)
  // docs/11-phone-mic.md. Step 1, RTA: the phone's own mic spectrum behind the EQ curve, with
  // processing requested off, uncalibrated, never sent anywhere. Step 2, RESPONSE: the mic's
  // average minus the box's PROGRAM average ("ps" frames, asked for with "progspec" only while
  // this view is open), both on the engine's grid with the engine's own estimator (rta.js), each
  // averaged over 10/20 s so time alignment does not matter. The mic runs only on LIVE with the
  // page visible, after a tap on MIC; MIC again, leaving LIVE or hiding the page stops every track.
  const RT = window.K1000RTA || null;
  const RTA_MS = 66;                 // <= ~15 fps
  const RTA_SPAN = G_TOP - G_BOT;    // the RTA's dB per division = the EQ grid's (6 dB a line):
                                     // a 12 dB ring stands two lines tall, like a 12 dB cut
  const RTA_HEAD = 5;                // dB from the tracked loudest band to the top of the plot
  const LOW_HZ = 100;                // phone mics are weak below: shaded, no peak marker there
  const RESP_MS = 200;               // mic frames for the response: 5 per second
  const SNAP_R = 24;                 // px: a long-press this close to the drawn peak pointer notches AT the peak
  const SLOW_MS = 8;                 // steady RTA analysis slower than this per frame: fall back to FFT 8192
  const WARM = 20;                   // frames of a session not timed for that (JIT, first buffers)
  const SMOOTH = [12, 6, 3];
  const PS_GRID = [10, 20000, 133];  // the box's grid (progspec.py); its frames say which they use
  const DISP = RT ? RT.logPoints(20, 20000, 48) : new Float64Array(0);
  const rta = {
    mode: load("k1000.rta.mode", "rta") === "response" ? "response" : "rta",
    speed: load("k1000.rta.speed", "slow") === "fast" ? "fast" : "slow",
    frac: SMOOTH.includes(load("k1000.rta.frac", 12)) ? load("k1000.rta.frac", 12) : 12,
    win: load("k1000.rta.win", 20) === 10 ? 10 : 20,
    frozen: false, busy: false, msg: "", frames: 0, skipped: 0, slow: false,
    disp: null, hold: null, top: null, peak: null, resp: null, respSpan: 0, progWhy: "", psAt: 0,
    eqKey: "", shown: "",
  };
  const resp = RT ? { key: "", grid: null, table: null, fft: null, win: null, scratch: null, pw: null,
    bands: null, mic: new RT.Window(21), prog: new RT.Window(21) } : null;
  let mic = null, micGen = 0, rafId = 0, lastFrame = 0, lastResp = 0, psOn = false, lastTrack = null;
  const rel = {
    mic: $("#mic"), hint: $("#rta-hint"), opts: $("#rta-opts"), mode: $("#rta-mode"), speed: $("#rta-speed"),
    smooth: $("#rta-smooth"), hold: $("#rta-hold"), freeze: $("#rta-freeze"), win: $("#resp-win"),
    reset: $("#resp-reset"), proc: $("#mic-proc"), msg: $("#rta-msg"),
  };

  // The K1000 icon opens the page as an app (display: standalone): no address bar, so no site icon
  // to tap. There the way back after a "Don't allow" is Chrome's own settings.
  function standalone() {
    try { return ["standalone", "fullscreen"].some((m) => window.matchMedia("(display-mode: " + m + ")").matches); } catch (e) { return false; }
  }
  function micError(e) {
    const n = e && e.name;
    if (n === "NotAllowedError" || n === "SecurityError" || n === "PermissionDeniedError") {
      return standalone() ? "Mic blocked: Chrome › Settings › Site settings › Microphone › " + location.hostname
        : "Mic blocked: allow it (address bar icon › Permissions)";
    }
    if (n === "NotFoundError" || n === "DevicesNotFoundError" || n === "OverconstrainedError") return "No microphone found on this phone";
    if (n === "NotReadableError" || n === "TrackStartError" || n === "AbortError") return "The mic is busy: another app has it";
    return "Mic unavailable (" + (n || "error") + ")";
  }

  async function micStart() {
    if (mic || rta.busy) return;
    rta.msg = "";
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!RT) rta.msg = "The mic analysis did not load: reload the page";
    else if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      rta.msg = "The mic needs the HTTPS page (https://" + location.hostname + "/)";
    } else if (!AC) rta.msg = "No Web Audio in this browser: no mic analysis";
    if (rta.msg) { renderRta(); return; }
    const gen = ++micGen;
    rta.busy = true; renderRta();
    let ac = null, stream = null;
    try {
      ac = new AC();                    // created inside the tap: allowed to run
      stream = await navigator.mediaDevices.getUserMedia({ video: false, audio: {
        echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 } });
      if (gen !== micGen) throw Object.assign(new Error("cancelled"), { name: "Cancelled" });
      const track = stream.getAudioTracks()[0];
      if (!track) throw Object.assign(new Error("no audio track"), { name: "NotFoundError" });
      if (ac.state !== "running") await ac.resume();
      if (gen !== micGen) throw Object.assign(new Error("cancelled"), { name: "Cancelled" });
      const src = ac.createMediaStreamSource(stream);
      const an = ac.createAnalyser();
      an.fftSize = 16384; an.smoothingTimeConstant = 0;
      src.connect(an);                  // never to ac.destination: nothing is played back
      const st = typeof track.getSettings === "function" ? track.getSettings() : {};
      const keys = ["echoCancellation", "noiseSuppression", "autoGainControl"];
      mic = { stream, track, ctx: ac, src, an, proc: keys.filter((k) => st[k] === true),
        settings: { echoCancellation: st.echoCancellation, noiseSuppression: st.noiseSuppression,
          autoGainControl: st.autoGainControl, sampleRate: st.sampleRate, channelCount: st.channelCount } };
      lastTrack = track;
      micBuffers();
      track.addEventListener("ended", () => {
        if (mic && mic.track === track) { micStop(); rta.msg = "The mic stopped (taken by another app?)"; renderRta(); }
      });
      rta.busy = false;
      lastFrame = 0; lastResp = 0;
      rafId = requestAnimationFrame(rtaLoop);
      syncProgspec();
    } catch (e) {
      try { if (stream) stream.getTracks().forEach((t) => t.stop()); } catch (x) { /* already stopped */ }
      try { if (ac) ac.close().catch(() => {}); } catch (x) { /* closed */ }
      if (gen === micGen) { rta.busy = false; rta.msg = micError(e); }
    }
    renderRta(); drawEq();
  }

  // (re)allocate for the analyser's current FFT size
  function micBuffers() {
    const m = mic, an = m.an, n = an.frequencyBinCount;
    m.binHz = m.ctx.sampleRate / an.fftSize;
    m.fbuf = new Float32Array(n); m.pow = new Float64Array(n); m.tbuf = new Float32Array(an.fftSize);
    m.ball = new RT.Ballistics(n);
    m.sm = new Float64Array(DISP.length); m.dispDb = new Float64Array(DISP.length);
    m.det = new Float64Array(DISP.length); m.detDb = new Float64Array(DISP.length);
    m.band = new Float64Array(DISP.length);
    m.level = m.level || new RT.LevelTracker({ riseTau: 0.15, fallDbPerS: 1 });
    m.peaks = m.peaks || new RT.PeakTracker({ persistS: 1.5, tolOct: 1 / 12, releaseS: 0.6 });
    m.frames = 0; m.cost = 0;           // this session's frames at this FFT size (the warm-up)
    rta.hold = null;
    resp.key = "";                      // the response bands follow the FFT size too
  }

  function micStop() {
    micGen++;
    rta.busy = false;
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    if (mic) {
      const m = mic;
      mic = null;
      try { m.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* already stopped */ }
      try { m.src.disconnect(); } catch (e) { /* gone */ }
      try { m.ctx.close().catch(() => {}); } catch (e) { /* closed */ }
    }
    rta.disp = rta.hold = rta.peak = rta.resp = rta.top = null;
    rta.frozen = false; rta.respSpan = 0; rta.progWhy = "";
    if (resp) { resp.mic.clear(); resp.prog.clear(); }
    syncProgspec();
    renderRta(); drawEq();
  }

  document.addEventListener("visibilitychange", () => { if (document.hidden) micStop(); });
  window.addEventListener("pagehide", () => micStop());

  // One requestAnimationFrame loop. FREEZE: nothing analysed, nothing redrawn (drawn once on the
  // tap). RTA: analyse + draw at <= 15 fps. RESPONSE: a mic frame every RESP_MS, and the view is
  // recomputed and redrawn only when a mic or program frame came in.
  function rtaLoop(now) {
    rafId = 0;
    if (!mic) return;
    rafId = requestAnimationFrame(rtaLoop);
    if (rta.frozen) return;
    const t = performance.now();
    if (rta.mode === "response") {
      if (t - lastResp < RESP_MS) return;
      if (drag) { rta.skipped++; return; }  // the band moves: the average restarts anyway
      lastResp = t;
      try { respFrame(t / 1000); } catch (e) { rta.msg = "Mic analysis failed: " + e.message; micStop(); }
      return;
    }
    if (now - lastFrame < RTA_MS) return;
    if (drag) { rta.skipped++; return; }    // a finger is moving a band: the curve comes first
    const dt = lastFrame ? Math.min(0.5, (now - lastFrame) / 1000) : RTA_MS / 1000;
    lastFrame = now;
    try { analyse(t / 1000, dt); } catch (e) { rta.msg = "Mic analysis failed: " + e.message; micStop(); return; }
    drawEq();
  }

  function analyse(t, dt) {
    const m = mic, t0 = performance.now();
    m.an.getFloatFrequencyData(m.fbuf);
    RT.fromDb(m.fbuf, m.pow);
    m.ball.update(m.pow, dt);
    rta.frames++; m.frames++;
    RT.smoothFracOct(rta.speed === "fast" ? m.ball.fast : m.ball.slow, m.binHz, DISP, rta.frac, m.sm);
    const d = RT.bandDb(RT.toDb(m.sm, m.dispDb), DISP, 1000, m.band);   // band power: pink reads flat
    rta.disp = d;
    if (!rta.hold || rta.hold.length !== d.length) rta.hold = Float64Array.from(d); else RT.peakHold(rta.hold, d);
    let lvl = -Infinity;
    for (let i = 0; i < DISP.length; i++) if (DISP[i] >= 40 && DISP[i] <= 16000 && d[i] > lvl) lvl = d[i];
    rta.top = m.level.update(lvl + RTA_HEAD, dt);
    // the peak detector: SLOW, 1/24 oct, whatever the display shows
    RT.smoothFracOct(m.ball.slow, m.binHz, DISP, 24, m.det);
    rta.peak = m.peaks.update(RT.findPeak(RT.toDb(m.det, m.detDb), DISP, { fMin: LOW_HZ, fMax: 16000 }), t);
    // a slow phone: halve the FFT. Only steady frames count (not the first WARM of a session).
    if (m.frames <= WARM) return;
    const cost = performance.now() - t0;
    m.cost = m.cost ? m.cost + 0.1 * (cost - m.cost) : cost;
    if (m.an.fftSize > 8192 && m.frames > 2 * WARM && m.cost > SLOW_MS) {
      m.an.fftSize = 8192; rta.slow = true; micBuffers();
    }
  }
  // leaving or entering a view: the RTA's averages, hold, level and marker start afresh
  function rtaRestart() {
    rta.disp = rta.hold = rta.top = rta.peak = null;
    lastFrame = 0; lastResp = 0;
    if (!mic) return;
    mic.ball.reset(); mic.level.reset(); mic.peaks.reset();
    mic.frames = 0; mic.cost = 0;
  }

  // ------------------------------------------------------------------ RESPONSE (step 2)
  function respGrid(g) {
    const key = g.join(",");
    if (!mic || key === resp.key) return;
    const rate = mic.ctx.sampleRate;
    const grid = RT.engineGrid(g[0], g[1], g[2]);
    const n = Math.min(RT.engineFftLen(rate, grid.min), mic.an.fftSize);
    resp.key = key; resp.grid = grid;
    resp.perOct = 1 / Math.log2(grid.ratio);
    resp.fft = new RT.FFT(n); resp.win = RT.hann(n);
    resp.scratch = { re: new Float64Array(n), im: new Float64Array(n) };
    resp.pw = new Float64Array(n / 2 + 1); resp.bands = new Float64Array(grid.n);
    resp.table = RT.bandTable(grid, rate / n, n / 2 + 1);
    respReset();
  }
  function respReset() {
    if (!resp) return;
    resp.mic.clear(); resp.prog.clear();
    rta.resp = null; rta.respSpan = 0;
  }
  // while K1000 is muted the mic hears the room, not the PA: nothing goes into the average
  function respPaused() { return !!(S && S.muted); }
  function respFrame(t) {
    if (!resp.key) respGrid(PS_GRID);
    rta.frames++;
    if (!respPaused()) {
      mic.an.getFloatTimeDomainData(mic.tbuf);
      const p = RT.hannPower(mic.tbuf, resp.fft, resp.win, resp.scratch, resp.pw);
      resp.mic.push(t, RT.bandPeaks(p, resp.table, resp.bands));
    }
    respUpdate(t);
  }
  function respUpdate(t) {
    respCompute(t);
    renderRtaMsg();
    if (!drag) drawEq();
  }
  function respCompute(t) {
    const mm = resp.mic.mean(t, rta.win), pm = resp.prog.mean(t, rta.win);
    rta.respSpan = mm && pm ? Math.min(mm.span, pm.span, rta.win) : 0;
    if (!mm || !pm || mm.mean.length !== pm.mean.length) { rta.resp = null; return; }
    const r = RT.response(mm.mean, pm.mean, resp.grid.f);
    r.raw = r.db;
    r.db = RT.smoothDb(r.db, resp.perOct / rta.frac);   // 1/12 (as is), 1/6 or 1/3 octave, exactly
    r.mic = mm.count; r.prog = pm.count;
    rta.resp = r;
  }
  // a "ps" frame from the box: its program spectrum, L and R power-averaged, on its grid
  function onProgram(m) {
    rta.psAt = performance.now();
    if (!mic || rta.mode !== "response" || !RT) return;
    const g = m.grid;
    if (!Array.isArray(g) || g.length !== 3 || !g.every(num) || !(g[0] > 0 && g[1] > g[0] && g[2] >= 2 && g[2] <= 4096)) return;
    respGrid(g);
    if (rta.frozen) return;
    if (!Array.isArray(m.m) || m.m.length !== g[2]) { rta.progWhy = String(m.why || "no data"); renderRtaMsg(); return; }
    rta.progWhy = "";
    if (respPaused()) return;
    const p = new Float64Array(g[2]);
    for (let i = 0; i < p.length; i++) p[i] = num(m.m[i]) ? RT.dbToPow(m.m[i]) : 0;
    const t = performance.now() / 1000;
    resp.prog.push(t, p);
    respUpdate(t);
  }
  // the box's program frames flow only while this view is open: tell the box on every change
  function syncProgspec() {
    const want = !!mic && rta.mode === "response" && tab === "live";
    if (!open || !ws) { psOn = false; return; }
    if (want !== psOn && send({ t: "progspec", on: want }, true)) psOn = want;
  }
  // The response includes what K1000 does after the program tap (docs/11 §5): when the live EQ,
  // the balance, the Lo-Cut or the profile change, the average starts again. The master only
  // moves the whole trace, which the 0 dB offset absorbs.
  function systemChanged(st) {
    const k = JSON.stringify([st.bands || null, st.balance_db, st.lowcut_on, st.lowcut_hz, st.profile]);
    if (k === rta.eqKey) return;
    if (rta.eqKey && mic && rta.mode === "response") respReset();
    rta.eqKey = k;
  }

  // ------------------------------------------------------------------ drawing (inside drawEq)
  const yR = (db) => PAD.t + (rta.top - db) / RTA_SPAN * (GH - PAD.t - PAD.b);
  function traceTo(xs, ys, from, to) { for (let i = from; i < to; i++) ctx.lineTo(xs(i), ys(i)); }
  function drawMicUnder(w, h) {
    if (!mic) return;
    const top = PAD.t, bot = h - PAD.b, cy = (y) => Math.max(top, Math.min(bot, y));
    if (rta.mode === "rta" && rta.disp && rta.top !== null) {
      const xs = (i) => xOf(DISP[i]), ys = (i) => cy(yR(rta.disp[i]));
      ctx.beginPath(); ctx.moveTo(xs(0), bot); traceTo(xs, ys, 0, DISP.length); ctx.lineTo(xs(DISP.length - 1), bot); ctx.closePath();
      ctx.fillStyle = "rgba(0,140,165,0.30)"; ctx.fill();
      ctx.beginPath(); ctx.moveTo(xs(0), ys(0)); traceTo(xs, ys, 1, DISP.length);
      ctx.strokeStyle = "rgba(0,200,235,0.80)"; ctx.lineWidth = 1.25; ctx.stroke();
      if (rta.hold) {
        const hy = (i) => cy(yR(rta.hold[i]));
        ctx.beginPath(); ctx.moveTo(xs(0), hy(0)); traceTo(xs, hy, 1, DISP.length);
        ctx.strokeStyle = "rgba(140,235,255,0.50)"; ctx.setLineDash([2, 3]); ctx.lineWidth = 1; ctx.stroke(); ctx.setLineDash([]);
      }
      ctx.lineWidth = 1;
    }
    if (rta.mode === "response" && rta.resp && !rta.resp.quiet) {
      const f = resp.grid.f, d = rta.resp.db, y0 = yOf(0);
      ctx.strokeStyle = "#35d6ff"; ctx.lineWidth = 2;
      ctx.fillStyle = "rgba(0,190,255,0.16)";
      let i = 0;
      while (i < f.length) {                              // one filled run per gap-free stretch
        while (i < f.length && (!Number.isFinite(d[i]) || f[i] < 20)) i++;
        const a = i;
        while (i < f.length && Number.isFinite(d[i])) i++;
        if (i - a < 2) continue;
        const xs = (k) => xOf(Math.min(20000, f[k])), ys = (k) => cy(yOf(d[k]));
        ctx.beginPath(); ctx.moveTo(xs(a), y0); traceTo(xs, ys, a, i); ctx.lineTo(xs(i - 1), y0); ctx.closePath(); ctx.fill();
        ctx.beginPath(); ctx.moveTo(xs(a), ys(a)); traceTo(xs, ys, a + 1, i); ctx.stroke();
      }
      ctx.lineWidth = 1;
    }
    // below 100 Hz the phone mic is weak: a dark veil OVER its traces (the grid and the EQ curve
    // are drawn after it, so they stay bright), and a dashed edge just inside it
    const xl = xOf(LOW_HZ);
    ctx.fillStyle = "rgba(0,0,0,0.5)";
    ctx.fillRect(PAD.l, top, xl - PAD.l, bot - top);
    ctx.strokeStyle = "rgba(255,184,0,0.45)"; ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(Math.round(xl) - 1.5, top); ctx.lineTo(Math.round(xl) - 1.5, bot); ctx.stroke();
    ctx.setLineDash([]);
  }
  function respNote() {
    if (!mic || rta.mode !== "response") return "";
    if (!open) return "No link: the response needs the box";
    if (respPaused()) return "MUTED: response paused";
    if (rta.progWhy === "no audio data" || (rta.resp && rta.resp.quiet)) return "PLAY MUSIC: the program is too quiet";
    if (rta.progWhy) return "Program spectrum: " + rta.progWhy;
    if (!rta.psAt || performance.now() - rta.psAt > 3000) return "Waiting for the box's program…";
    return "";
  }
  function drawMicLabels(w, h) {
    if (!mic) return;
    ctx.save();
    ctx.textAlign = "left"; ctx.textBaseline = "top"; ctx.font = "bold 10px system-ui, Roboto, sans-serif";
    ctx.fillStyle = "#4fcbe6";
    const tail = (rta.mode === "rta" ? (rta.speed === "fast" ? " · FAST" : "")
      : " · " + Math.round(rta.respSpan) + "/" + rta.win + " s") + (rta.frozen ? " · FROZEN" : "");
    let label = (rta.mode === "rta" ? "PHONE MIC · uncalibrated" : "RESPONSE · uncalibrated mic − program") + tail;
    if (rta.mode !== "rta" && ctx.measureText(label).width > w - PAD.l - PAD.r - 8) label = "RESPONSE · uncalibrated" + tail;
    ctx.fillText(label, PAD.l + 4, PAD.t + 3);
    ctx.font = "9px system-ui, Roboto, sans-serif"; ctx.fillStyle = "rgba(200,200,200,0.8)"; ctx.textBaseline = "bottom";
    ctx.fillText("low conf.", PAD.l + 3, h - PAD.b - 3);
    // the response's notes sit on the graph (the chip row stays one line)
    const note = respNote();
    if (note && /^PLAY MUSIC/.test(note)) {
      ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.font = "bold 20px system-ui, Roboto, sans-serif";
      ctx.fillStyle = "rgba(255,184,0,0.9)";
      ctx.fillText("PLAY MUSIC", (PAD.l + w - PAD.r) / 2, PAD.t + (h - PAD.t - PAD.b) * 0.16);   // above 0 dB: clear of the cuts
    } else if (note) {
      ctx.textBaseline = "top"; ctx.font = "bold 11px system-ui, Roboto, sans-serif"; ctx.fillStyle = "rgba(255,184,0,0.95)";
      ctx.fillText(note, PAD.l + 4, PAD.t + 16);
    }
    ctx.restore();
  }
  // where the peak pointer is drawn (its tip), or null. FREEZE keeps it: that is when it is read.
  function markerAt() {
    const pk = mic && rta.mode === "rta" ? rta.peak : null;
    if (!pk || !rta.disp || rta.top === null || !GW) return null;
    const i = clampInt(Math.round(Math.log2(pk.f / DISP[0]) * 48), 0, DISP.length - 1);
    return { f: pk.f, x: xOf(pk.f), y: Math.max(PAD.t + 26, Math.min(GH - PAD.b, yR(rta.disp[i]))) };
  }
  function drawPeakMarker() {
    const mk = markerAt();
    if (!mk) return;
    const { x, y } = mk;
    ctx.save();
    ctx.fillStyle = "#00c8ff"; ctx.strokeStyle = "#000"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, y - 2); ctx.lineTo(x - 6, y - 12); ctx.lineTo(x + 6, y - 12); ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.font = "bold 11px system-ui, Roboto, sans-serif"; ctx.textBaseline = "bottom";
    const txt = fmtHz(mk.f);
    const tw = ctx.measureText(txt).width;
    ctx.textAlign = "left";
    const tx = clamp(x - tw / 2, PAD.l + 2, GW - PAD.r - tw - 2);
    ctx.fillText(txt, tx, y - 13);
    ctx.restore();
  }
  // A long-press ON the pointer (within SNAP_R px of its middle, both ways): "notch here" lands on
  // the peak. Anywhere else it lands under the finger, as always. The marker never acts by itself.
  function peakNear(x, y) {
    const mk = markerAt();
    return mk && Math.hypot(x - mk.x, y - (mk.y - 10)) <= SNAP_R ? mk.f : 0;
  }

  // ------------------------------------------------------------------ the chip row
  // The line under the chip row: why the mic is off (denied, no mic, taken). The response's own
  // notes (PLAY MUSIC, MUTED, waiting...) are drawn on the graph, so the row never grows with MIC on.
  function renderRtaMsg() {
    const text = mic ? "" : rta.msg;
    if (text === rta.shown) return;
    rta.shown = text;
    rel.msg.textContent = text; rel.msg.hidden = !text;
  }
  function renderRta() {
    const on = !!mic, isResp = rta.mode === "response";
    rel.mic.setAttribute("aria-pressed", on ? "true" : "false");
    rel.mic.textContent = rta.busy ? "MIC…" : "MIC";
    rel.opts.hidden = !on;
    rel.hint.hidden = on || rta.busy;
    rel.mode.setAttribute("aria-pressed", isResp ? "true" : "false");
    rel.speed.hidden = isResp; rel.speed.textContent = rta.speed === "fast" ? "FAST" : "SLOW";
    rel.smooth.textContent = "1/" + rta.frac;
    rel.hold.hidden = isResp;
    rel.freeze.setAttribute("aria-pressed", rta.frozen ? "true" : "false");
    rel.win.hidden = !isResp; rel.win.textContent = rta.win + " s";
    rel.reset.hidden = !isResp;
    rel.proc.hidden = !(on && mic.proc.length);
    rel.proc.title = on && mic.proc.length ? "Still on: " + mic.proc.join(", ") : "";
    renderRtaMsg();
  }
  rel.mic.addEventListener("click", () => { if (mic || rta.busy) { micStop(); rta.msg = ""; renderRta(); } else micStart(); });
  rel.mode.addEventListener("click", () => {
    rta.mode = rta.mode === "response" ? "rta" : "response"; save("k1000.rta.mode", rta.mode);
    respReset(); rtaRestart(); rta.progWhy = ""; rta.psAt = 0;
    syncProgspec(); renderRta(); drawEq();
  });
  rel.speed.addEventListener("click", () => { rta.speed = rta.speed === "fast" ? "slow" : "fast"; save("k1000.rta.speed", rta.speed); rta.hold = null; renderRta(); });
  rel.smooth.addEventListener("click", () => {
    rta.frac = SMOOTH[(SMOOTH.indexOf(rta.frac) + 1) % SMOOTH.length]; save("k1000.rta.frac", rta.frac);
    rta.hold = null;
    if (rta.resp && rta.resp.raw && resp.perOct) rta.resp.db = RT.smoothDb(rta.resp.raw, resp.perOct / rta.frac);
    renderRta(); drawEq();
  });
  rel.hold.addEventListener("click", () => { rta.hold = null; drawEq(); });
  rel.freeze.addEventListener("click", () => {
    rta.frozen = !rta.frozen;
    if (!rta.frozen) { lastFrame = 0; lastResp = 0; }
    renderRta(); drawEq();
  });
  rel.win.addEventListener("click", () => { rta.win = rta.win === 20 ? 10 : 20; save("k1000.rta.win", rta.win); renderRta(); });
  rel.reset.addEventListener("click", () => { respReset(); drawEq(); });

  Object.assign(window.K1000, {
    rta: () => ({
      on: !!mic, busy: rta.busy, mode: rta.mode, speed: rta.speed, frac: rta.frac, win: rta.win, frozen: rta.frozen,
      msg: rta.msg || respNote(), line: rel.msg.hidden ? "" : rel.msg.textContent, marker: markerAt(), frames: rta.frames, skipped: rta.skipped, slow: rta.slow,
      fft: mic ? mic.an.fftSize : 0, rate: mic ? mic.ctx.sampleRate : 0, track: mic ? mic.track.readyState : null,
      proc: mic ? mic.proc.slice() : null, settings: mic ? mic.settings : null, peak: rta.peak, top: rta.top,
      hold: !!rta.hold, span: RTA_SPAN, paused: respPaused(), ps: psOn, progWhy: rta.progWhy, respSpan: rta.respSpan,
      resp: rta.resp ? { quiet: rta.resp.quiet, offset: rta.resp.offset, progRef: rta.resp.progRef, valid: rta.resp.valid,
        mic: rta.resp.mic, prog: rta.resp.prog, f: Array.from(resp.grid.f), db: Array.from(rta.resp.db, (v) => (Number.isFinite(v) ? v : null)) } : null,
    }),
    lastTrack: () => (lastTrack ? lastTrack.readyState : null),
  });
  renderRta();

  // ------------------------------------------------------------------ controls
  $$("[data-nudge]").forEach((b) => b.addEventListener("click", () => {
    if (!canEdit()) return;
    let f = b.dataset.nudge;
    if (f.startsWith("@")) f = BANDS[sel] + f.slice(1);
    send({ t: "nudge", f, d: parseInt(b.dataset.d, 10) });
  }));

  el.lc.addEventListener("click", () => {
    if (!canEdit()) return;
    send({ t: "set", f: "lowcut_on", v: !S.lowcut_on });
  });

  // A hold-to-act button. begin() runs when the finger goes down and returns the hold's context
  // ({ms, valid?, why?, ...}) or null (no hold); act(ctx) runs when the hold completes. valid(),
  // when given, is checked every frame and at the end: false = the hold is cancelled (with `why`).
  function holdButton(node, begin, act) {
    const bar = node.querySelector("b");
    let timer = 0, raf = 0, t0 = 0, ms = 0, id = null, ctx = null;
    const stop = () => { clearTimeout(timer); cancelAnimationFrame(raf); timer = 0; raf = 0; id = null; ctx = null; bar.style.width = "0"; holds.delete(node); };
    const broken = () => ctx && ctx.valid && !ctx.valid();
    const cancel = () => { const why = ctx && ctx.why; stop(); if (why) { toast(why, 2600); buzz([30, 60, 30]); } };
    const tick = () => {
      if (broken()) { cancel(); return; }
      bar.style.width = (Math.min(1, (performance.now() - t0) / ms) * 100).toFixed(1) + "%";
      raf = requestAnimationFrame(tick);
    };
    node.style.touchAction = "none";
    node.addEventListener("pointerdown", (e) => {
      if (!canEdit() || id !== null) return;
      const c = begin();
      if (!c) return;
      id = e.pointerId; capture(node, id); ctx = c; holds.add(node);
      t0 = performance.now(); ms = c.ms;
      timer = setTimeout(() => {
        if (broken()) { cancel(); return; }
        const done = ctx; stop(); act(done);
      }, ms);
      raf = requestAnimationFrame(tick);
    });
    const release = (e) => {
      if (e.pointerId !== id) return;
      const early = timer !== 0 && performance.now() - t0 < ms;
      stop();
      if (early && e.type === "pointerup") toast("Hold to confirm", 1200);
    };
    node.addEventListener("pointerup", release);
    node.addEventListener("pointercancel", release);
    node.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  // MUTE: the intent is fixed when the finger goes down (see the header). What a hold means is
  // the box's call (S.hold, B2's rule): while K1000 itself holds Main muted on its way in (boot
  // ramp, setup guard), a hold KEEPS it muted, it never unmutes.
  function holdUnmutes() { return !!S && (S.hold ? S.hold === "unmute" : S.muted === true); }
  holdButton($("#mute"), () => {
    if (!S || mutePend) return null;
    const want = !holdUnmutes(), seq = S.mute_seq;
    if (want) return { ms: MUTE_MS, want, seq };
    return { ms: UNMUTE_MS, want, seq, why: "The mute changed meanwhile: look, then hold again",
      valid: () => holdUnmutes() && S.mute_seq === seq };
  }, (c) => sendMute(c.want, c.seq));
  function sendMute(want, seq) {
    const msg = { t: "mute", on: want, id: "mute" };
    if (!want) msg.seq = seq;
    if (!send(msg, true)) { toast("Not connected: use B2 on the box", 3000); buzz([30, 60, 30, 60, 30]); return; }
    mutePend = { want, timer: setTimeout(() => muteDone(false, null, "No answer from K1000: use B2 on the box"), ACK_MS) };
    render();
  }
  function muteDone(ok, v, why) {
    if (!mutePend) { if (!ok && why) toast(why, 3000); return; }
    clearTimeout(mutePend.timer);
    mutePend = null;
    if (ok) buzz(v ? 80 : 30);
    else { toast(why, 3000); buzz([30, 60, 30, 60, 30]); }
    render();
  }
  holdButton($("#band-flat"), () => ({ ms: FLAT_MS }), () => {
    send({ t: "band", s: sel, g: 0, id: "flat" });
  });

  const modal = $("#confirm");
  $("#reset-live").addEventListener("click", () => { if (canEdit()) modal.hidden = false; });
  $("#confirm-no").addEventListener("click", () => { modal.hidden = true; });
  $("#confirm-yes").addEventListener("click", () => {
    modal.hidden = true;
    send({ t: "reset_live", confirm: true, id: "reset" });
  });

  function showTab(t) {
    tab = t; save("k1000.tab", t);
    $$("#tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === t ? "true" : "false"));
    $("#page-main").hidden = t !== "main";
    $("#page-live").hidden = t !== "live";
    if (t !== "live") micStop();        // the mic runs on LIVE only
    if (t === "live") sizeCanvas();
  }
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  window.addEventListener("resize", sizeCanvas);
  // the graph's box also changes with the layout (rotation, the chip row wrapping)
  if (window.ResizeObserver) new ResizeObserver(() => { if (tab === "live") sizeCanvas(); }).observe(cv);

  // ------------------------------------------------------------------ box updated -> reload once
  // The service worker serves the cached shell first (instant start, offline): the first launch
  // after a box update would run the OLD page. hello.build says so; the page then fetches the new
  // worker and reloads itself once, when no finger is on a control.
  function checkBuild(m) {
    if (BUILD === UNSTAMPED || typeof m.build !== "string" || !m.build) return;
    const protoOk = m.v === PROTOCOL;
    if (m.build === BUILD && protoOk) { outdated = ""; behind = false; return; }
    if (sget("k1000.reload") === m.build) {           // already reloaded once for this build
      if (!protoOk) link = "old";
      outdated = ""; behind = true;
      return;
    }
    outdated = m.build;
    if (!protoOk) link = "old";
    render();
    reloadWhenIdle();
  }
  let reloadArmed = false;
  async function reloadWhenIdle() {
    if (reloadArmed) return;
    reloadArmed = true;
    let swapped = false;
    try {
      if ("serviceWorker" in navigator && navigator.serviceWorker.controller) {
        const reg = await navigator.serviceWorker.getRegistration();
        const changed = new Promise((res) => navigator.serviceWorker.addEventListener("controllerchange", () => res(true), { once: true }));
        if (reg) await reg.update();
        swapped = await Promise.race([changed, new Promise((res) => setTimeout(() => res(false), 4000))]);
      }
    } catch (e) { /* reload anyway: the cache was refreshed in the background */ }
    const go = () => {
      if (drag || lp || mutePend || holds.size) { setTimeout(go, 300); return; }
      sset("k1000.reload", outdated || "?");
      location.reload();
    };
    go();
    return swapped;
  }

  // ------------------------------------------------------------------ app-ness
  let installEvt = null;
  window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installEvt = e; $("#install").hidden = false; });
  $("#install").addEventListener("click", async () => {
    if (!installEvt) return;
    installEvt.prompt();
    try { await installEvt.userChoice; } catch (e) { /* dismissed */ }
    installEvt = null; $("#install").hidden = true;
  });
  window.addEventListener("appinstalled", () => { $("#install").hidden = true; });

  let wakeLock = null;
  async function wake() {
    if (!("wakeLock" in navigator) || document.hidden || wakeLock) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
    } catch (e) { wakeLock = null; }
  }
  document.addEventListener("pointerdown", wake, { passive: true });

  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => { /* still works as a page */ });
  }

  showTab(tab);
  render();
  drawMeters();
  connect();
})();

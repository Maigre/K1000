// SPDX-License-Identifier: GPL-3.0-or-later
/* K1000 phone page. One websocket to the box; the box's Controller is the truth (the same one the
   knobs drive): this page only shows what the box pushes and sends small, validated requests.

   Safety rules carried here (the box enforces them again):
   - Values change only from explicit controls: +/- buttons (click = released on the button, so a
     scroll never fires one), the EQ dots (a drag must START on a dot), hold-to-act buttons.
   - Mute and "band flat" need a hold; the live-EQ reset and the Lo-Cut switch need a confirm
     (the box's SETUP asks for a second push for both; the box keeps an undo of the reset).
   - The MUTE hold acts on what the finger MEANT when it went down: a hold that starts as "hold to
     mute" always sends a mute, whatever happens meanwhile. An unmute carries the mute generation
     (mute_seq) the page showed, and is dropped here (and refused by the box) if the mute changed
     during the hold: someone else's emergency mute is never undone.
   - A mute is confirmed by the box's ack (buzz), never by the send itself.
   - Drags are coalesced: at most one message per SEND_MS, the last value always sent.
   - No link = every control disabled and the page says so.
   - The page knows its own build (stamped by the box) and reloads itself once when the box was
     updated, so a cached app never runs against a newer box. */
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
  const stats = { sent: 0, t: {}, rxState: 0, connects: 0, hist: [] };
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
    ws = null; open = false;
    if (link === "ok") link = "nolink";
    flushCancel();
    if (drag && drag.wait) notchDone(false, null, "Link lost: the notch was not set");
    if (mutePend) muteDone(false, null, "Link lost: use B4 on the box");
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
        stats.hist.push([Date.now(), m.master_db]); if (stats.hist.length > 60) stats.hist.shift();
        link = m.phone_on === false ? "off" : link === "old" ? "old" : "ok";
        render();
        break;
      case "m":
        M = m; drawMeters();
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
      : holdUnmutes() ? "MUTED" : muted ? "KEEP MUTED" : "MUTE";   // a hold, always (a tap says so)
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
    const w = GW, h = GH;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#070707"; ctx.fillRect(0, 0, w, h);
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
    drag = { i: s, id: pid, dx: xOf(hz) - x, dy: yOf(clamp(g, G_BOT, G_TOP)) - y, q, wait: true, ended: false,
      timer: setTimeout(() => notchDone(false, null, "No answer from K1000 for the notch"), ACK_MS) };
    renderSlots(); drawEq();
  }
  function notchDone(ok, v, why) {
    const d = drag && drag.wait ? drag : null;
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
    toast("Notch " + SHORT[v.s] + " at " + fmtFreq(v.f) + " · " + fmtDb(v.g) + " dB · slide to move it", 1800);
  }

  // ------------------------------------------------------------------ controls
  $$("[data-nudge]").forEach((b) => b.addEventListener("click", () => {
    if (!canEdit()) return;
    let f = b.dataset.nudge;
    if (f.startsWith("@")) f = BANDS[sel] + f.slice(1);
    send({ t: "nudge", f, d: parseInt(b.dataset.d, 10) });
  }));

  // Lo-Cut on/off: confirmed, as on the box (SETUP > Lo-Cut, second push). The value is fixed
  // when the switch is tapped: a confirm that comes late still asks for what was shown.
  el.lc.addEventListener("click", () => {
    if (!canEdit() || !S) return;
    const on = !S.lowcut_on;
    ask(on ? "Lo-Cut on?" : "Lo-Cut off?",
      on ? "The tops' 12 dB/oct high-pass comes back at " + fmtHz(S.lowcut_hz) + " Hz."
        : "The tops get their full low end (the high-pass is parked at 10 Hz).",
      on ? "Lo-Cut on" : "Lo-Cut off", () => send({ t: "set", f: "lowcut_on", v: on }));
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
  // the box's call (S.hold, B4's rule): while K1000 itself holds Main muted on its way in (boot
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
    if (!send(msg, true)) { toast("Not connected: use B4 on the box", 3000); buzz([30, 60, 30, 60, 30]); return; }
    mutePend = { want, timer: setTimeout(() => muteDone(false, null, "No answer from K1000: use B4 on the box"), ACK_MS) };
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

  // One confirm dialog for the actions that need one (live-EQ reset, Lo-Cut on/off).
  const modal = $("#confirm");
  let onYes = null;
  function ask(q, sub, yes, fn) {
    $("#confirm-q").textContent = q;
    $("#confirm-sub").textContent = sub;
    $("#confirm-yes").textContent = yes;
    onYes = fn;
    modal.hidden = false;
  }
  $("#reset-live").addEventListener("click", () => {
    if (!canEdit()) return;
    ask("All bands flat?", "The 4 bells and both shelves go back to 0 dB and their default " +
      "frequencies, smoothly. The box can undo it for 5 minutes, or until the next EQ cut " +
      "(SETUP › LIVE EQ › Undo reset).", "All flat",
      () => send({ t: "reset_live", confirm: true, id: "reset" }));
  });
  $("#confirm-no").addEventListener("click", () => { modal.hidden = true; onYes = null; });
  $("#confirm-yes").addEventListener("click", () => {
    modal.hidden = true;
    const fn = onYes;
    onYes = null;
    if (fn && canEdit()) fn();
  });

  function showTab(t) {
    tab = t; save("k1000.tab", t);
    $$("#tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === t ? "true" : "false"));
    $("#page-main").hidden = t !== "main";
    $("#page-live").hidden = t !== "live";
    if (t === "live") sizeCanvas();
  }
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  window.addEventListener("resize", sizeCanvas);

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

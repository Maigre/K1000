// SPDX-License-Identifier: GPL-3.0-or-later
// K1000 tune page (docs/06-phone.md §11): paste a REW / Open Sound Meter / EqualizerAPO filter
// list, preview it on the 8 system PEQ bands, apply it through K1000, undo it.
// The box does everything that matters: this page sends the TEXT, the box reads it (again at
// Apply), clamps it and applies it like a knob. Server text is only ever put in textContent.
"use strict";
(() => {
  const $ = (s) => document.querySelector(s);
  const SVGNS = "http://www.w3.org/2000/svg";
  const W = 640, H = 260, PADL = 34, PADR = 8, PADT = 10, PADB = 22;
  const F_LO = 20, F_HI = 20000;
  let ws = null, now = null, curveNow = null, preview = null, undo = null, busy = false;
  let leftS = 0, leftAt = 0, closedWhy = "", timer = null, seq = 0;

  const fmtHz = (f) => f >= 1000 ? (f / 1000).toFixed(f >= 10000 ? 1 : 2).replace(/\.?0+$/, "") + " kHz" : Math.round(f) + " Hz";
  const fmtDb = (g) => (g > 0 ? "+" : g < 0 ? "−" : "") + Math.abs(g).toFixed(1) + " dB";
  const band = (b) => b.gain === 0 ? "flat" : `${fmtHz(b.freq)}  ${fmtDb(b.gain)}  Q ${(+b.q).toFixed(2)}`;

  function link(ok, text) {
    const el = $("#link");
    el.className = "chip " + (ok ? "ok" : "bad");
    el.querySelector("span").textContent = text;
  }

  function showLeft() {
    if (!leftS) { $("#left").textContent = closedWhy; return; }
    const s = Math.max(0, leftS - (Date.now() - leftAt) / 1000);
    const m = Math.ceil(s / 60 - 1e-9);
    $("#left").textContent = `LAPTOP ACCESS ${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")} LEFT`;
  }

  function msg(sel, lines, cls) {
    const el = $(sel);
    if (!lines || !lines.length) { el.hidden = true; el.textContent = ""; return; }
    el.hidden = false;
    el.textContent = Array.isArray(lines) ? lines.join("\n") : String(lines);
    if (cls) el.className = "msg " + cls;
  }

  function buttons() {
    const on = ws && ws.readyState === 1 && !busy && !closedWhy;
    $("#preview").disabled = !on || !$("#text").value.trim();
    $("#apply").disabled = !on || !(preview && preview.ok);
    $("#undo").disabled = !on || !undo;
    $("#undo").title = undo ? "Puts back: " + undo.filter((b) => b.gain !== 0).map(band).join(" · ") || "a flat EQ" : "";
  }

  // ------------------------------------------------------------------ graph
  const xOf = (f) => PADL + (W - PADL - PADR) * Math.log(f / F_LO) / Math.log(F_HI / F_LO);
  function el(name, attrs, text) {
    const e = document.createElementNS(SVGNS, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function draw() {
    const svg = $("#graph");
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const curves = [curveNow, preview && preview.curve].filter(Boolean);
    let lo = -12, hi = 6;
    for (const c of curves) for (const v of c.db) { lo = Math.min(lo, Math.floor(v / 3) * 3); hi = Math.max(hi, Math.ceil(v / 3) * 3); }
    const yOf = (db) => PADT + (H - PADT - PADB) * (hi - db) / (hi - lo);
    for (let db = lo; db <= hi; db += 3) {
      svg.appendChild(el("line", { x1: PADL, x2: W - PADR, y1: yOf(db), y2: yOf(db), class: db === 0 ? "zero" : "grid" }));
      if (db % 6 === 0) svg.appendChild(el("text", { x: 2, y: yOf(db) + 4 }, (db > 0 ? "+" : "") + db));
    }
    for (const f of [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]) {
      svg.appendChild(el("line", { x1: xOf(f), x2: xOf(f), y1: PADT, y2: H - PADB, class: "grid" }));
      svg.appendChild(el("text", { x: xOf(f) - 10, y: H - 6 }, f >= 1000 ? f / 1000 + "k" : String(f)));
    }
    const path = (c, cls) => {
      const d = c.f.map((f, i) => (i ? "L" : "M") + xOf(f).toFixed(1) + " " + yOf(c.db[i]).toFixed(1)).join(" ");
      svg.appendChild(el("path", { d, class: cls }));
    };
    if (curveNow) path(curveNow, "now");
    if (preview && preview.curve) path(preview.curve, "new");
  }

  function table() {
    const body = $("#bands tbody");
    while (body.firstChild) body.removeChild(body.firstChild);
    const next = preview && preview.ok ? preview.bands : null;
    for (let i = 0; i < 8; i++) {
      const tr = document.createElement("tr");
      const a = now ? now[i] : null, b = next ? next[i] : null;
      const cells = [String(i + 1), a ? band(a) : "?", b ? band(b) : "—"];
      cells.forEach((t, k) => {
        const td = document.createElement("td");
        td.textContent = t;
        if ((k === 1 && a && a.gain === 0) || (k === 2 && b && b.gain === 0)) td.className = "flat";
        if (k === 2 && a && b && band(a) !== band(b)) td.className = "changed";
        tr.appendChild(td);
      });
      body.appendChild(tr);
    }
  }

  function showImport(r) {
    msg("#error", r.ok ? [] : [r.why || r.error || "refused"], "bad");
    msg("#skipped", r.skipped && r.skipped.length ? ["Skipped:"].concat(r.skipped) : [], "warn");
    msg("#notes", r.notes && r.notes.length ? ["Notes:"].concat(r.notes) : [], "note");
    const n = (r.filters || []).length;
    $("#parsed").textContent = n ? `${n} peaking filter${n > 1 ? "s" : ""} read` : "";
  }

  // ------------------------------------------------------------------ the box
  function send(o) {
    if (!ws || ws.readyState !== 1) return false;
    o.id = ++seq;
    ws.send(JSON.stringify(o));
    return true;
  }

  function onMessage(m) {
    if (m.t === "hello" || m.t === "state") {
      now = m.current; curveNow = m.curve_now; undo = m.undo;
      if (typeof m.left_s === "number") { leftS = m.left_s; leftAt = Date.now(); }
    } else if (m.t === "left") {
      leftS = m.s; leftAt = Date.now();
    } else if (m.t === "preview") {
      preview = m; now = m.current; curveNow = m.curve_now;
      showImport(m);
    } else if (m.t === "applied") {
      busy = false;
      showImport(m);
      if (m.ok) {
        now = m.bands; curveNow = m.curve; undo = m.undo; preview = null;
        msg("#result", [m.saved ? "Applied and saved on the box." : "Applied (not saved yet: check the box)."], m.saved ? "ok" : "warn");
      } else {
        msg("#result", [m.why || "refused"], "bad");
      }
    } else if (m.t === "undone") {
      busy = false;
      if (m.ok) {
        now = m.bands; curveNow = m.curve; undo = null;
        msg("#result", [m.saved ? "Previous system EQ back, saved." : "Previous system EQ back (not saved yet)."], m.saved ? "ok" : "warn");
      } else {
        msg("#result", [m.why || "nothing to undo"], "bad");
      }
    } else if (m.t === "err") {
      busy = false;
      msg("#result", [m.why || "error"], "bad");
    } else if (m.t === "bye") {
      closedWhy = m.why || "closed";
    }
    draw(); table(); buttons(); showLeft();
  }

  function connect() {
    const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/tune/ws";
    try { ws = new WebSocket(url); } catch (e) { setTimeout(connect, 3000); return; }
    link(false, "CONNECTING");
    ws.onopen = () => { closedWhy = ""; link(true, "LINK"); buttons(); };
    ws.onmessage = (ev) => { try { onMessage(JSON.parse(ev.data)); } catch (e) { /* ignore */ } };
    ws.onclose = (ev) => {
      busy = false;
      if (ev.code === 4004) closedWhy = "LAPTOP ACCESS CLOSED ON THE BOX";
      link(false, ev.code === 4004 ? "CLOSED" : "NO LINK");
      leftS = 0; buttons(); showLeft();
      setTimeout(connect, 3000);
    };
  }

  $("#text").addEventListener("input", () => {
    buttons();
    clearTimeout(timer);
    timer = setTimeout(() => { if ($("#text").value.trim()) send({ t: "preview", text: $("#text").value }); }, 400);
  });
  $("#preview").addEventListener("click", () => send({ t: "preview", text: $("#text").value }));
  $("#apply").addEventListener("click", () => {
    if (!preview || !preview.ok) return;
    const n = preview.filters.length;
    if (!window.confirm(`Apply ${n} filter${n > 1 ? "s" : ""} to the K1000 system EQ? (Undo puts the current one back.)`)) return;
    busy = true; buttons();
    send({ t: "apply", text: $("#text").value });
  });
  $("#undo").addEventListener("click", () => {
    if (!window.confirm("Put back the system EQ the last Apply replaced?")) return;
    busy = true; buttons();
    send({ t: "undo" });
  });
  setInterval(showLeft, 15000);
  draw(); table();
  connect();
})();

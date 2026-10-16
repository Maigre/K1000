// SPDX-License-Identifier: GPL-3.0-or-later
/* The v2 lab's fake box: the page's files served the way the box serves them (same CSP, the
   build stamp, Service-Worker-Allowed), and a fake protocol-2 socket (Playwright's
   routeWebSocket) that replays a hello / state recorded from the real server (fixtures.json,
   record.py), answers like the box (acks, errors, the ALIGN lock, the mute generation, rta
   frames, RESPONSE's `pgm` windows) and RECORDS every frame the page sends. Used by e2e.mjs.

   RESPONSE (docs/14 `pgm`, the contract of k1000#t-010's phone mic): hello.pgm = {v: 1, n: 264,
   f0: 10, f1: 20000, rate, fft, est: "peak", win_s: 0.5} (opts.pgm null: an older box without
   it, "absent": an older box); `sub {pgm}` (independent of `rta`); while subscribed and live, one
   window per 0.5 s (not live: one frame per change of state, as the box does): the program
   polled at 10 Hz with the ENGINE's estimator (micmath.EngineEstimator: Hann, FFT 8192, the peak
   FFT bin per 1/24 oct band), power-averaged over the window, as base64 int16 of dBFS*100. The
   program is a WAV (opts.pgmWav: the same pink noise the fake mic plays) read on at real time,
   or a fixed pink-ish spectrum without one. pgmSt / pgmProg set the frames' st and prog. */

import crypto from "node:crypto";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { EngineEstimator, encodePgm, powToDb } from "../../k1000/web/static/v2/js/micmath.js";
import { readWav } from "./wav.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const BOX = path.resolve(HERE, "../..");
export const STATIC = path.join(BOX, "k1000/web/static");
export const LAB_BUILD = "1ab000000000";
const require = createRequire(import.meta.url);

// playwright: $K1000_PLAYWRIGHT (a path to the module), else node's own resolution
export function loadPlaywright() {
  const p = process.env.K1000_PLAYWRIGHT;
  return p ? require(p) : require("playwright");
}
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const clone = (x) => JSON.parse(JSON.stringify(x));
export function loadFixtures(file) {
  return JSON.parse(fs.readFileSync(file || process.env.K1000_V2_FIXTURES || path.join(HERE, "fixtures.json"), "utf8"));
}

// ------------------------------------------------------------------ static files, as the box serves them
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".webmanifest": "application/manifest+json", ".json": "application/json",
  ".png": "image/png", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8" };
export function csp(connect) {
  return "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
    `connect-src ${connect}; worker-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`;
}
export function startStatic(root = STATIC, build = LAB_BUILD) {
  const srv = http.createServer((req, res) => {
    let p = decodeURIComponent((req.url || "/").split("?")[0].split("#")[0]);
    if (p === "/v2") { res.writeHead(302, { Location: "/v2/" }); res.end(); return; }
    if (p === "/") p = "/index.html";
    else if (p === "/v2/") p = "/v2/index.html";
    const file = path.join(root, p);
    const ext = path.extname(file);
    if (!file.startsWith(root + path.sep) || p.split("/").some((s) => s.startsWith(".")) || !TYPES[ext] || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      res.writeHead(404, { "Content-Type": "text/plain" }); res.end("not found\n"); return;
    }
    let body = fs.readFileSync(file);
    if (p.startsWith("/v2/") && (ext === ".js" || ext === ".mjs")) body = Buffer.from(body.toString("utf8").split("__K1000_BUILD__").join(build));
    const etag = '"' + crypto.createHash("sha256").update(body).digest("hex").slice(0, 16) + '"';
    const h = { "ETag": etag, "Cache-Control": "no-cache" };
    if (ext === ".html") h["Content-Security-Policy"] = csp(`'self' ws://${req.headers.host}`);
    if (p === "/v2/sw.js") h["Service-Worker-Allowed"] = "/v2/";
    if (p.startsWith("/v2/") && ext === ".html" && req.headers["sec-fetch-mode"] === "navigate") h["Vary"] = "*";
    if (req.headers["if-none-match"] === etag) { res.writeHead(304, h); res.end(); return; }   // as the box: ETag + 304
    h["Content-Type"] = TYPES[ext];
    res.writeHead(200, h); res.end(body);
  });
  return new Promise((ok) => srv.listen(0, "127.0.0.1", () => ok({ port: srv.address().port, url: `http://127.0.0.1:${srv.address().port}`, close: () => new Promise((r) => srv.close(r)) })));
}

// ------------------------------------------------------------------ the fake box
const LOCKED = new Set(["align_delay_ms", "lowcut_hz"]);
export const PGM_HELLO = { v: 1, n: 264, f0: 10.0, f1: 20000.0, rate: 48000, fft: 8192, est: "peak", win_s: 0.5 };
export class FakeBox {
  constructor(fx, opts = {}) {
    this.fx = fx;
    this.hello = clone(fx.hello);
    this.hello.build = opts.build || LAB_BUILD;
    if (opts.pgm === "absent") delete this.hello.pgm;       // an older box: no RESPONSE
    else if (opts.pgm === null) this.hello.pgm = null;      // this box cannot serve it (spectrum disabled)
    else this.hello.pgm = Object.assign({}, PGM_HELLO, fx.hello.pgm || {}, opts.pgm || {});
    this.pgmSub = false;
    this.pgmTimer = 0;
    this.pgmSeq = 0;
    this.pgmSt = "live";         // the frames' st: live | waiting | off | unavailable | disabled
    this.pgmProg = true;         // the frames' prog: true | false | null
    this.pgmSent = 0;
    this.pgmSrc = opts.pgmWav ? readWav(opts.pgmWav) : null;
    this.pgmPos = 0;
    this.state = clone(fx.state);
    this.m = clone(fx.m);
    this.sent = [];              // every frame the page sent: {t, ...msg, at}
    this.sockets = [];
    this.connects = 0;
    this.accept = true;          // false: every new socket is closed at once
    this.silent = false;         // true: nothing answered, nothing sent (a dead link)
    this.dropMute = false;       // true: mutes get no answer
    this.unlocked = false;
    this.alignS = 30;            // the ALIGN window an unlock grants (s)
    this.rtaSub = false;
    this.rtaTimer = 0;
    this.seq = 0;
    this.peakHz = 3170;          // the PEAK of the fake spectrum (rtaFrame)
    this.grabs = [];             // what the notch helper did: {s, new}
    this.stepValue = opts.stepValue;   // the page's own arithmetic (fields.js), for acks
  }
  async attach(context) {
    await context.routeWebSocket(/\/v2\/ws$/, (ws) => this.onSocket(ws));
  }
  get ws() { return this.sockets[this.sockets.length - 1] || null; }
  onSocket(ws) {
    this.connects++;
    if (!this.accept) { setTimeout(() => ws.close({ code: 1011, reason: "lab: refused" }), 5); return; }
    this.sockets.push(ws);
    this.unlocked = false; this.rtaSub = false; this.setPgm(false);
    ws.onMessage((raw) => this.onMessage(ws, raw));
    ws.onClose(() => { const i = this.sockets.indexOf(ws); if (i >= 0) this.sockets.splice(i, 1); });
    const you = 1;
    this.out(ws, Object.assign(clone(this.hello), { you }));
    this.out(ws, this.state);
    this.out(ws, this.m);
  }
  out(ws, obj) { if (!this.silent && ws) { try { ws.send(JSON.stringify(obj)); } catch (e) { /* closed */ } } }
  broadcast() { for (const ws of this.sockets) this.out(ws, this.state); }
  push(patch) { Object.assign(this.state, patch); this.broadcast(); }
  meters(patch) { Object.assign(this.m, patch); for (const ws of this.sockets) this.out(ws, this.m); }
  setup(patch) { Object.assign(this.state.setup, patch); this.broadcast(); }
  pushHello(patch) { Object.assign(this.hello, patch); for (const ws of this.sockets) this.out(ws, Object.assign(clone(this.hello), { you: 1 })); }
  close(code = 1001) { for (const ws of this.sockets.slice()) { try { ws.close({ code, reason: "lab" }); } catch (e) { /* fine */ } } this.sockets = []; }
  ofType(t) { return this.sent.filter((m) => m.t === t); }
  clearSent() { this.sent = []; }
  async waitSent(pred, ms = 2000) {
    const t0 = Date.now();
    for (;;) {
      const m = this.sent.find(pred);
      if (m) return m;
      if (Date.now() - t0 > ms) return null;
      await sleep(15);
    }
  }
  ack(ws, id, v, extra) { if (id != null) this.out(ws, Object.assign({ t: "ack", id, v }, extra || {})); }
  err(ws, id, code, why, v) { const e = { t: "err", id: id == null ? null : id, code, why }; if (v !== undefined) e.v = v; this.out(ws, e); }
  value(path) {
    const m = /^(.*)\.(freq|gain|q)$/.exec(path);
    if (m) { const b = this.state.bands[this.hello.bands.indexOf(m[1])]; return b[{ freq: "f", gain: "g", q: "q" }[m[2]]]; }
    if (path in this.state && path !== "setup") return this.state[path];
    return this.state.setup[path];
  }
  setValue(path, v) {
    const m = /^(.*)\.(freq|gain|q)$/.exec(path);
    if (m) { this.state.bands[this.hello.bands.indexOf(m[1])][{ freq: "f", gain: "g", q: "q" }[m[2]]] = v; return; }
    if (path in this.state && path !== "setup") this.state[path] = v; else this.state.setup[path] = v;
  }
  onMessage(ws, raw) {
    let m;
    try { m = JSON.parse(raw); } catch (e) { return; }
    this.sent.push(Object.assign({ at: Date.now() }, m));
    if (this.silent) return;
    const id = m.id;
    switch (m.t) {
      case "ping": this.out(ws, { t: "pong", ts: typeof m.ts === "number" ? m.ts : null }); break;
      case "hi": this.ack(ws, id, { min_app: 0, app_ok: true }); break;
      case "nudge": {
        const caps = this.hello.nudge[m.f];
        if (!caps || !Number.isInteger(m.d) || !m.d || m.d < caps[0] || m.d > caps[1]) { this.err(ws, id, "BAD", "lab: bad nudge"); break; }
        if (LOCKED.has(m.f) && !this.unlocked) { this.err(ws, id, "LOCKED", "LOCKED · HOLD 0.8 s TO EDIT", this.value(m.f)); break; }
        // a SETUP number the box holds read-only (D-016: the AUX level with no spare input)
        if (this.state.readonly && this.state.readonly[m.f]) { this.err(ws, id, "READONLY", this.state.readonly[m.f], this.value(m.f)); break; }
        const key = m.f.replace(/\.\d+(?=\.|$)/, "");
        const v = this.stepValue ? this.stepValue(this.hello.fields[key], this.value(m.f), m.d, m.fine === true, m.f) : this.value(m.f);
        this.setValue(m.f, v);
        this.ack(ws, id, v, LOCKED.has(m.f) ? { unlock_s: this.alignS } : undefined);
        this.broadcast();
        break;
      }
      case "band": {
        const b = this.state.bands[m.s];
        if (!b) { this.err(ws, id, "BAD", "lab: s"); break; }
        if ("on" in m && ("f" in m || "q" in m)) { this.err(ws, id, "BAD", "band: on goes alone or with g only"); break; }
        if (typeof m.f === "number") b.f = m.f;
        if (typeof m.g === "number") b.g = m.g;
        if (typeof m.q === "number") b.q = m.q;
        if (typeof m.on === "boolean") b.on = m.on;
        const v = {};
        for (const k of ["f", "g", "q", "on"]) if (k in m) v[k] = b[k];
        this.ack(ws, id, v);
        this.state.pills = this.state.bands.some((x) => x.on === false) ? ["OUT " + this.state.bands.map((x, i) => (x.on === false ? this.hello.short[i] : null)).filter(Boolean).join(" ")] : [];
        this.broadcast();
        break;
      }
      case "mute": {
        if (this.dropMute) break;
        if (m.on === true) {
          this.state.muted = true; this.state.hold = "unmute"; this.state.mute_seq++; this.state.mute_src = "phone 1";
          this.out(ws, { t: "ack", id, v: true });
        } else if (m.on === false) {
          if (m.seq !== this.state.mute_seq) { this.out(ws, { t: "err", id, code: "STALE", why: "MUTE CHANGED meanwhile: unmute refused, look and hold again", v: this.state.muted }); break; }
          this.state.muted = false; this.state.hold = "mute"; this.state.mute_seq++; this.state.mute_src = "";
          this.out(ws, { t: "ack", id, v: false });
        }
        this.broadcast();
        break;
      }
      case "unlock": this.unlocked = true; this.ack(ws, id, { align_s: this.alignS }); break;
      case "lock": this.unlocked = false; this.ack(ws, id, { align_s: 0 }); break;
      case "choose": {
        const cur = this.state.setup[m.f];
        if (m.seen !== cur) { this.err(ws, id, "STALE", "CHANGED MEANWHILE", cur); break; }
        if (m.v === cur) { this.err(ws, id, "NOCHANGE", "NO CHANGE: ALREADY SET", cur); break; }
        if (this.state.readonly[m.f]) { this.err(ws, id, "READONLY", this.state.readonly[m.f], cur); break; }
        this.state.setup[m.f] = m.v;
        if (m.f === "lowcut_on") this.state.lowcut_on = m.v;
        this.ack(ws, id, m.v);
        this.broadcast();
        break;
      }
      case "act":
        if (m.a === "reset_live") { this.state.undo_reset_s = 300; this.ack(ws, id, { changed: true, undo_s: 300 }); this.broadcast(); }
        else if (m.a === "undo_reset") { if (!this.state.undo_reset_s) this.err(ws, id, "NOCHANGE", "NOTHING TO UNDO", { undo_s: 0 }); else { this.state.undo_reset_s = 0; this.ack(ws, id, { undo_s: 0 }); this.broadcast(); } }
        else if (m.a === "laptop_open") { this.state.laptop = { state: "open", s: 7200 }; this.ack(ws, id, { state: "opening", s: 0 }); this.broadcast(); }
        else if (m.a === "laptop_close") { this.state.laptop = { state: "closed", s: 0 }; this.ack(ws, id, { state: "closed", s: 0 }); this.broadcast(); }
        else this.err(ws, id, "BAD", "act");
        break;
      case "grab": {
        // the box's notch helper (control.py notch_helper, server.py _grab), select=False:
        // 1. a bell IN use (effective gain != 0) within 1/6 oct of the target: that bell, nothing
        //    written ("already there"); 2. else a free bell (stored gain 0, IN or OUT): `s` if it
        //    is one, else the free bell nearest the target: Q 8.65, f = the target, IN; 3. NOFREE
        const target = typeof m.hz === "number" ? m.hz : this.state.spectrum && this.state.spectrum.on !== false ? this.peakHz : null;
        const bells = this.hello.kind.map((k, i) => (k === "bell" ? i : -1)).filter((i) => i >= 0);
        const B = this.state.bands;
        const used = (i) => B[i].on !== false && B[i].g !== 0;
        let s = null, isNew = false;
        if (target != null) {
          const near = bells.filter(used).map((i) => [Math.abs(Math.log2(B[i].f / target)), i]).filter((x) => x[0] <= 1 / 6 + 1e-9).sort((p, q) => p[0] - q[0]);
          if (near.length) s = near[0][1];
        }
        if (s == null) {
          const free = bells.filter((i) => B[i].g === 0);
          if (!free.length) { this.err(ws, id, "NOFREE", "NO FREE BAND"); break; }
          s = free.includes(m.s) ? m.s : target != null ? free.slice().sort((p, q) => Math.abs(Math.log2(B[p].f / target)) - Math.abs(Math.log2(B[q].f / target)))[0] : free[0];
          isNew = true;
          B[s].q = 8.651; B[s].on = true;
          if (target != null) B[s].f = Math.round(target * 10) / 10;
        }
        this.grabs.push({ s, new: isNew });
        this.ack(ws, id, { s, f: B[s].f, q: B[s].q, g: B[s].g, on: B[s].on !== false, new: isNew, peak: target != null });
        this.broadcast();
        break;
      }
      case "sub": {
        // `rta` and `pgm` are independent keys; each one present must be a boolean
        const has = (k) => k in m;
        // (a box whose hello.pgm is null accepts `pgm` too, as the box does: its frames say disabled)
        if ((!has("rta") && !has("pgm")) || (has("rta") && typeof m.rta !== "boolean") || (has("pgm") && typeof m.pgm !== "boolean")) {
          this.err(ws, id, "BAD", "sub needs rta / pgm: true/false"); break;
        }
        const v = { st: "live" };
        if (has("rta")) {
          this.rtaSub = m.rta;
          v.rta = this.rtaSub;
          clearInterval(this.rtaTimer);
          if (this.rtaSub) this.rtaTimer = setInterval(() => { if (this.rtaSub) for (const w of this.sockets) this.out(w, this.rtaFrame()); }, 100);
        }
        if (has("pgm")) { this.setPgm(m.pgm); v.pgm = m.pgm; v.pgm_st = this.pgmState(); if (!has("rta")) delete v.st; }
        this.ack(ws, id, v);
        break;
      }
      default:
        this.err(ws, id, "UNKNOWN", "unknown message");
    }
  }
  // a box-like spectrum: pink-ish slope, a ring near 3.2 kHz, the PEAK on it
  rtaFrame() {
    const n = 264, b = new Uint8Array(n), hgt = new Uint8Array(n);
    for (let i = 0; i < n; i++) {
      const f = 10 * Math.pow(2000, i / (n - 1));
      let db = -30 - 4 * Math.log2(f / 100) + 14 * Math.exp(-Math.pow(Math.log2(f / 3170) / 0.05, 2) / 2) + Math.random() * 2;
      b[i] = Math.max(0, Math.min(255, Math.round((-24 - db) / 0.5)));
      hgt[i] = Math.max(0, b[i] - 4);
    }
    return { t: "rta", seq: ++this.seq, src: "PGM", st: "live", n, f0: 10, f1: 20000, hi: -24, lo: -90, peak_hz: this.peakHz,
      b: Buffer.from(b).toString("base64"), h: Buffer.from(hgt).toString("base64") };
  }
  // ------------------------------------------------------------ RESPONSE's program windows
  // the frames' st: pgmSt, or "disabled" on a box that cannot serve RESPONSE (hello.pgm null)
  pgmState() { return this.hello.pgm ? this.pgmSt : "disabled"; }
  setPgm(on) {
    this.pgmSub = !!on;
    clearInterval(this.pgmTimer); this.pgmTimer = 0;
    this.pgmSaid = null;
    if (!this.pgmSub) return;
    const tick = () => {
      if (!this.pgmSub) return;
      const st = this.pgmState();
      if (st !== "live" && this.pgmSaid === st) return;                    // not live: said once per change
      this.pgmSaid = st;
      for (const w of this.sockets) this.out(w, this.pgmFrame());
    };
    if (this.pgmState() !== "live") setTimeout(tick, 5);                 // at once to a new subscriber
    this.pgmTimer = setInterval(tick, 500);
  }
  // one 0.5 s window: 5 polls at 10 Hz, each the engine's estimator on the last FFT-length
  // samples (the WAV read on at real time), power-averaged
  pgmWindow() {
    const P = this.hello.pgm, n = P.n;
    if (!this.pgmSrc) {
      return Float64Array.from({ length: n }, (_, i) => { const f = P.f0 * Math.pow(P.f1 / P.f0, i / (n - 1)); return -40 - 3 * Math.log2(Math.max(f, 20) / 1000) + (Math.random() - 0.5); });
    }
    const { rate, x } = this.pgmSrc;
    if (!this.est || !this.est.matches(rate, P.f0, P.f1, n)) this.est = new EngineEstimator(rate, P.f0, P.f1, n);
    const N = this.est.n, hop = Math.round(rate / 10), buf = new Float32Array(N), acc = new Float64Array(n);
    for (let k = 0; k < 5; k++) {
      this.pgmPos = (this.pgmPos + hop) % x.length;
      for (let i = 0; i < N; i++) buf[i] = x[(this.pgmPos - N + i + x.length) % x.length];
      const b = this.est.bands(buf);
      for (let i = 0; i < n; i++) acc[i] += b[i] / 5;
    }
    return Array.from(acc, (p) => powToDb(p));
  }
  pgmFrame() {
    const P = this.hello.pgm;
    this.pgmSent++;
    const st = this.pgmState();
    if (st !== "live") return { t: "pgm", seq: this.pgmSeq, st, n: 0, m: null, k: 0, prog: null };
    const db = this.pgmWindow();
    return { t: "pgm", seq: ++this.pgmSeq, st: "live", n: P.n, f0: P.f0, f1: P.f1, k: 5, dt: 0.5, prog: this.pgmProg,
      m: encodePgm(db, (s) => Buffer.from(s, "binary").toString("base64")) };
  }
  stop() { clearInterval(this.rtaTimer); clearInterval(this.pgmTimer); }
}

// A show-like state (the prototype's values): cuts, an OUT-able band, a delay, a lowered master
export function showState(fx) {
  const st = clone(fx.state);
  st.master_db = -14; st.balance_db = 1.5; st.align_delay_ms = 1.25; st.lowcut_hz = 112;
  st.bands = [{ f: 250, g: -2, q: 1.4, on: true }, { f: 2440, g: -6, q: 8.651, on: true }, { f: 4000, g: 0, q: 4, on: true },
    { f: 6300, g: 0, q: 4, on: true }, { f: 120, g: -3, q: 0.707, on: true }, { f: 8000, g: 1.5, q: 0.707, on: true }];
  st.pills = ["MONO SUB"]; st.setup.mono_subs = true; st.phones = 1; st.profile = "m4"; st.device = "MOTU M4";
  st.box_band = 1;
  return st;
}

// ------------------------------------------------------------------ fingers (CDP touch: real pointer events, pointerType touch)
// two at once: hands(page).set({1: [x, y], 2: [x, y]}) puts exactly those fingers on the glass
// (a new id = touchStart, a moved one = touchMove, a missing one = touchEnd with the fingers
// that lift: what Chrome's Input.dispatchTouchEvent does with each list, probed)
export async function hands(page) {
  const cdp = await page.context().newCDPSession(page);
  let cur = {};
  const pts = (o) => Object.entries(o).map(([id, [x, y]]) => ({ x, y, id: Number(id) }));
  return {
    async set(next) {
      const gone = Object.keys(cur).filter((id) => !(id in next));
      if (gone.length) {                    // (touchEnd lists the fingers that lift)
        const lift = {}, keep = {};
        for (const id of Object.keys(cur)) (id in next ? keep : lift)[id] = cur[id];
        await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: pts(lift) });
        cur = keep;
      }
      const born = Object.keys(next).filter((id) => !(id in cur));
      const moved = Object.keys(next).some((id) => id in cur && (cur[id][0] !== next[id][0] || cur[id][1] !== next[id][1]));
      if (moved) {
        const m = {};
        for (const id of Object.keys(cur)) m[id] = next[id];
        await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: pts(m) });
        cur = m;
      }
      if (born.length) {
        const all = Object.assign({}, cur);
        for (const id of born) all[id] = next[id];
        await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: pts(all) });
        cur = all;
      }
    },
    async off() { await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] }); cur = {}; },
  };
}

// ------------------------------------------------------------------ a finger (CDP touch: real pointer events, pointerType touch)
export async function finger(page) {
  const cdp = await page.context().newCDPSession(page);
  const f = {
    x: 0, y: 0,
    async down(x, y) { f.x = x; f.y = y; await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y, id: 1 }] }); },
    async move(x, y) { f.x = x; f.y = y; await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y, id: 1 }] }); },
    async up() { await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] }); },
    // a straight slide of (dx, dy) over ms, in steps (each step's move is timed by the page's clock)
    async slide(dx, dy, ms, steps = Math.max(2, Math.round(ms / 16))) {
      const x0 = f.x, y0 = f.y;
      for (let i = 1; i <= steps; i++) { await sleep(ms / steps); await f.move(x0 + dx * i / steps, y0 + dy * i / steps); }
    },
    async tap(x, y, holdMs = 60) { await f.down(x, y); await sleep(holdMs); await f.up(); },
    async hold(x, y, ms) { await f.down(x, y); await sleep(ms); await f.up(); },
  };
  return f;
}
export async function center(page, sel) {
  const r = await page.locator(sel).first().boundingBox();
  if (!r) throw new Error("no box for " + sel);
  return { x: r.x + r.width / 2, y: r.y + r.height / 2, r };
}

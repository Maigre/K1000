// SPDX-License-Identifier: GPL-3.0-or-later
/* MEASURE mode (≋, DESIGN §6.1, §7) and RESPONSE ("walk the room").

   MEASURE: the ALIGN row becomes  MIC ● | RTA · RESPONSE | FREEZE | AVG 10/20 s | RESET ; the
   graph, the band row, the EQ tiles and the footer (MASTER, meters, MUTE) stay. ≋ (or Back,
   after the steps, the popover and a sheet) leaves it. Nothing here changes the sound.

   RESPONSE (docs/14 §3.6 `pgm`, docs/11-phone-mic.md §3): the phone mic minus the box's program,
   with the SAME estimator on the SAME grid (micmath.EngineEstimator: the engine's own, on
   hello.pgm's n / f0 / f1, FFT length = next pow2 >= mic rate / f0), each averaged in power over
   10 or 20 s of windows; 0 dB = the median of 200 Hz-5 kHz, smoothed for display (PGM ▾'s
   1/12 · 1/6 · 1/3 oct, 1/6 by default), nothing below 100 Hz or above 12 kHz (DESIGN §7: the
   phone's range). The box sends one program window per 0.5 s (`pgm`); the page pairs it with its
   own mic window over the same half second and keeps the pair only if nothing paused it: muted,
   no program (`prog` not true), the mic off or CLIPPING, no link, the box without a spectrum,
   FROZEN, the box ramping in. Any change to what the PA plays (a band's f / g / q / IN-OUT,
   balance, Lo-Cut, delay, polarity, mono subs, sub low-pass, profile, trims, audition, the system
   EQ set from /tune: state.sys_eq) restarts
   it, and the mic ticks of the next SETTLE_MS are not used (the change has to reach the mic
   first: the engine, the PA, the air, and the analyser's 171 ms of history). The master does not
   restart it: the program is taken before the master and the mic hears it after, so each mic
   tick is divided by the master's gain (micmath.MicHalf), and the ticks of the MASTER_SETTLE_MS
   after a master move or an unmute are not used (the engine's own volume ramp).

   `sub {pgm: true}` only while RESPONSE is shown on a visible page (no sheet over it, portrait);
   `sub {pgm: false}` when it is not; again after the box's `unsub {what: "pgm"}` or a reconnect.
   Shown only when the box's hello has `pgm` (an older box: "RESPONSE needs the box update"). */

import { K, UI, linked, render, savePrefs } from "./store.js";
import { on, request } from "./link.js";
import { resp as hook, rotated, syncHooks, syncSub } from "./rta.js";
import { M, allowed, clearErr, clipping as micClipping, frac, hooks as micHooks, notePgmProg, running as micRunning, setMic, why as micWhy } from "./mic.js";
import * as MM from "./micmath.js";
import { hap } from "./haptics.js";
import { toast } from "./view.js";
import { closeStepper } from "./stepper.js";
import { $, $$, num, now, setText } from "./util.js";

const MIN_SHOW_S = 2;                 // the line shows once 2 s of windows are in
const NOTE_MS = 2600;
export const SETTLE_MS = 400;         // after a restart: the change reaches the mic (engine, PA, air, 171 ms of analyser)
export const MASTER_SETTLE_MS = 800;  // after a master move or an unmute: the engine's volume ramp too
export const RESP_MAX_HZ = 12000;     // DESIGN §7: phone data 100 Hz-12 kHz
export const P = {
  subscribed: false, asked: false, failAt: 0, frames: 0, lastAt: 0, st: null, prog: null,
  added: 0, dropped: 0, restarts: 0, reason: "", last: null, note: "", noteUntil: 0, settled: 0, micRate: 0,
  restartAt: 0, firstAddMs: null,     // the lab: how long after the last restart its first window was kept
};
const avg = new MM.RespAvg(60);
let est = null, pending = null, lastTick = 0, lastKey = null, lastState = null, settleUntil = 0;
// the mic ticks before `ms` from now are not used (what is pending was heard before the change)
function settle(ms) { pending = null; settleUntil = Math.max(settleUntil, now() + ms); }
// the highest bin shown: 12 kHz, and never above 0.45 x the mic's rate (its bands above Nyquist
// would read the last FFT bin)
const maxHz = () => Math.min(RESP_MAX_HZ, P.micRate > 0 ? 0.45 * P.micRate : RESP_MAX_HZ);

// ------------------------------------------------------------------ what is shown
const isObj = (v) => !!v && typeof v === "object" && !Array.isArray(v);
// hello.pgm, checked: {n, f0, f1, win_s} or null (a box without RESPONSE)
export function pgmInfo() {
  const p = K.hello && K.hello.pgm;
  if (!isObj(p) || p.v !== 1 || !Number.isInteger(p.n) || p.n < 2 || p.n > 4096 || !(p.f0 > 0) || !(p.f1 > p.f0)) return null;
  return { n: p.n, f0: p.f0, f1: p.f1, win: num(p.win_s) && p.win_s > 0 ? p.win_s : 0.5 };
}
export const hasPgm = () => !!pgmInfo();
const respView = () => UI.measure && UI.prefs.mview === "resp";
export const respShown = () => respView() && hasPgm();
hook.shown = respShown;
const span = () => (+UI.prefs.avg === 10 ? 10 : 20);

// ------------------------------------------------------------------ the program windows
function pgmWanted() { return linked() && !document.hidden && !rotated() && !UI.sheet && respShown(); }
export function syncPgm() {
  if (!linked()) { P.asked = false; P.subscribed = false; return; }
  const want = pgmWanted();
  if (P.asked === want) return;
  if (want && now() - P.failAt < 5000) return;          // refused: not again at every tick
  P.asked = want;
  P.subscribed = want;
  if (!want) pending = null;
  request({ t: "sub", pgm: want }, (ok, r) => {
    if (!ok && r.code !== "LINK" && P.asked === want) { P.asked = !want; P.subscribed = false; P.failAt = now(); }
  }, 3000);
}
syncHooks.push(syncPgm);
on("unsub", (m) => {                    // lapsed (no ping for hello.rta.idle_s): again, if still wanted
  if (!m || m.what !== "pgm") return;
  P.asked = false; P.subscribed = false;
  syncPgm();
});
// a reconnect: a new subscription (the average is kept: a short drop mid-walk loses nothing, and a
// state that changed meanwhile restarts it as any change does)
on("lost", () => { P.asked = false; P.subscribed = false; pending = null; P.st = null; P.lastAt = 0; });

// What pauses RESPONSE now, as the page knows it by itself ("" = nothing)
function pauseLocal() {
  if (!linked()) return "NO LINK";
  if (!micRunning()) return "MIC OFF";
  if (K.state && K.state.muted) return "MUTED";
  if (micClipping()) return "MIC CLIPPING";
  if (K.state && K.state.ramping) return "BOX RAMPING IN";
  return "";
}
// the box's side: its spectrum state (docs/14 §3.6 `st`) and its program gate (`prog`)
function boxWords(st) {
  if (st === "waiting") return "BOX: SPECTRUM STARTING";
  if (K.state && K.state.spectrum && K.state.spectrum.on === false) return "BOX: SPECTRUM OFF (SETUP › LIVE EQ)";
  return "BOX: NO SPECTRUM";
}
// prog null = the box cannot tell (its gate off in k1000.yaml): not "present", so paused (contract)
const progWords = (prog) => (prog === false ? "NO PROGRAM" : "BOX: PROGRAM NOT KNOWN");
function pauseFor(d) {
  const l = pauseLocal();
  if (l) return l;
  if (d.st !== "live" || !d.db) return boxWords(d.st);
  if (d.prog !== true) return progWords(d.prog);
  return "";
}

on("pgm", (m) => {
  if (!m || typeof m !== "object") return;
  P.frames++; P.lastAt = now();
  const d = MM.decodePgm(m);
  P.st = d.st; P.prog = d.prog;
  notePgmProg(d.prog);
  const pend = pending;
  pending = null;
  if (!respShown() || UI.freeze) return;
  const why = pauseFor(d) || (pend && pend.bad) || "";
  P.reason = why;
  if (why) { P.dropped++; compute(); render(); return; }
  if (!pend || !pend.k || !pend.sameGrid(d.n, d.f0, d.f1)) { compute(); render(); return; }   // the mic's first half second, or settling
  const mic = pend.mean(), pgm = new Float64Array(d.n);
  for (let i = 0; i < d.n; i++) pgm[i] = MM.dbToPow(d.db[i]);
  if (avg.wins.length && avg.wins[0].mic.length !== d.n) avg.clear();
  avg.add(now() / 1000, mic, pgm, d.dt > 0 ? d.dt : (pgmInfo() || { win: 0.5 }).win);
  P.added++;
  if (P.firstAddMs == null && P.restartAt) P.firstAddMs = Math.round(now() - P.restartAt);
  compute();
  render();
});

// The mic's half of each window: the engine's estimator on the analyser's last samples, ~10 Hz
// (the box polls its engine at ~10 Hz), summed in power until the box's window closes.
micHooks.tick = (x, rate, t) => {
  if (!respShown() || UI.freeze) return;
  if (now() - lastTick < 90) return;
  lastTick = now();
  const g = pgmInfo();
  if (!g) return;
  P.micRate = rate;
  if (!est || !est.matches(rate, g.f0, g.f1, g.n)) { est = new MM.EngineEstimator(rate, g.f0, g.f1, g.n); pending = null; }
  if (now() < settleUntil) { P.settled++; return; }      // what the mic hears may still be the old sound
  const b = est.bands(x);
  if (!b) return;                                          // more samples than the analyser keeps (rate > 163.84 kHz)
  // the mic's half covers the box's window: a half older than the window (no box window came to
  // close it: the box's spectrum not live, a dropped window) starts again
  if (pending && now() - pending.t0 > g.win * 1000 + 300) pending = null;
  if (!pending) pending = new MM.MicHalf(g.n, g.f0, g.f1, now());
  const S = K.state;
  pending.add(b, S && num(S.master_db) ? S.master_db : 0);
  // a clipped block (near the PA: harmonics, a false rise in the highs): mic.js checked these
  // very samples just before this tick (M.clipAt), so pauseLocal says MIC CLIPPING and the half
  // is not kept
  const why = pauseLocal();
  if (why) pending.bad = why;
};

// ------------------------------------------------------------------ the average, shown
function compute() {
  const g = pgmInfo();
  const m = avg.mean(now() / 1000, span());
  if (!g || !m || m.mic.length !== g.n) { P.last = null; return; }
  const grid = MM.engineGrid(g.f0, g.f1, g.n);
  const r = MM.response(m.mic, m.pgm, grid.f, { maxHz: maxHz() });
  const perOct = (g.n - 1) / Math.log2(g.f1 / g.f0);
  P.last = { f: grid.f, db: MM.smoothDb(r.db, perOct / frac()), quiet: r.quiet, cover: m.cover, count: m.count, offset: r.offset };
}
export function restart(why) {
  avg.clear(); P.last = null; P.restarts++; P.restartAt = now(); P.firstAddMs = null;
  settle(SETTLE_MS);
  if (why) note("RESTARTED · " + why);
}
function note(t) { P.note = t; P.noteUntil = now() + NOTE_MS; render(); }

// Any change to what the PA plays restarts the average (the master does not)
function soundKey(S) {
  const set = Object.assign({}, S.setup || {});
  delete set.spectrum_on;
  const sys = Object.keys(S).filter((k) => /^(sys|system)_/.test(k)).sort().map((k) => [k, S[k]]);
  return JSON.stringify([(S.bands || []).map((b) => [b.f, b.g, b.q, b.on !== false]), S.balance_db, S.lowcut_on, S.lowcut_hz,
    S.align_delay_ms, S.profile, set, sys]);
}
function whatChanged(a, b) {
  if (!a) return "the sound changed";
  const j = (x) => JSON.stringify(x);
  if (j((a.bands || []).map((x) => [x.f, x.g, x.q, x.on])) !== j((b.bands || []).map((x) => [x.f, x.g, x.q, x.on]))) return "the live EQ changed";
  if (a.balance_db !== b.balance_db) return "BAL changed";
  if (a.lowcut_on !== b.lowcut_on || a.lowcut_hz !== b.lowcut_hz) return "the Lo-Cut changed";
  if (a.align_delay_ms !== b.align_delay_ms) return "DELAY changed";
  if (a.profile !== b.profile || (a.setup || {}).profile !== (b.setup || {}).profile) return "the profile changed";
  if (a.sys_eq !== b.sys_eq) return "the system EQ changed";
  return "SETUP changed";
}
on("state", (S) => {
  const k = soundKey(S);
  if (lastKey != null && k !== lastKey && (avg.wins.length || pending)) restart(whatChanged(lastState, S));
  // a master move or an unmute: no restart (the master is divided out), but what the mic heard
  // while the engine ramped is not used
  else if (lastState && (S.master_db !== lastState.master_db || (lastState.muted && !S.muted))) settle(MASTER_SETTLE_MS);
  lastKey = k; lastState = S;
});
on("hello", () => { const g = pgmInfo(); if (!g && avg.wins.length) restart(""); });

// ------------------------------------------------------------------ the RESPONSE line
// g: {xOf, yOf, top, bot, cyan}: on the EQ grid's dB scale, 0 dB = the median of 200 Hz-5 kHz
hook.draw = (cx, g) => {
  if (!respShown() || !P.last || P.last.quiet || P.last.cover < MIN_SHOW_S) return;
  const { f, db } = P.last;
  const y0 = g.yOf(0), cl = (y) => Math.max(g.top, Math.min(g.bot, y));
  const runs = [];
  let cur = null;
  for (let i = 0; i < f.length; i++) {
    if (f[i] < 100 || !Number.isFinite(db[i])) { cur = null; continue; }
    if (!cur) { cur = []; runs.push(cur); }
    cur.push([g.xOf(f[i]), cl(g.yOf(db[i]))]);
  }
  for (const r of runs) {
    if (r.length < 2) continue;
    cx.beginPath(); cx.moveTo(r[0][0], y0);
    for (const [x, y] of r) cx.lineTo(x, y);
    cx.lineTo(r[r.length - 1][0], y0); cx.closePath();
    cx.fillStyle = "rgba(0,200,255,.14)"; cx.fill();
    cx.beginPath();
    r.forEach(([x, y], i) => (i ? cx.lineTo(x, y) : cx.moveTo(x, y)));
    cx.strokeStyle = g.cyan; cx.lineWidth = 2.5; cx.stroke();
  }
};

// ------------------------------------------------------------------ the words
// The graph's note in MEASURE (and on LIVE when the mic is chosen but cannot run): [text, kind]
export function graphNote() {
  if (P.note && now() < P.noteUntil) return [P.note, "warn"];
  const mw = micWhy();
  if (!UI.measure) return mw ? [mw, "warn"] : micClipping() && micRunning() && micShownLive() ? [CLIP_WORDS, "warn"] : null;
  const g = pgmInfo();
  if (UI.prefs.mview === "resp") {
    if (!g) return [K.hello && K.hello.pgmKnown ? "RESPONSE: this box has no spectrum (k1000.yaml)" : "RESPONSE needs the box update", "warn"];
    const cover = Math.floor(avg.coverage(now() / 1000, span()));
    const kept = cover + " s of " + span();
    const r = status();
    if (r) return [(r === "MIC OFF" && mw ? mw : "PAUSED · " + r + (r === "NO PROGRAM" ? " · play music" : r === "MIC CLIPPING" ? " · too loud for the phone: step back from the speakers" : "")) + " · " + kept,
      r === "MUTED" || r === "NO LINK" ? "red" : "warn"];
    if (UI.freeze) return ["FROZEN · " + kept, "cy"];
    if (P.last && P.last.quiet) return ["PLAY MUSIC · the program is too quiet to compare · " + kept, "warn"];
    if (cover < MIN_SHOW_S) return ["RESPONSE · listening… " + kept + " · walk the room", "cy"];
    return ["RESPONSE · " + kept + " · uncalibrated mic − program", "cy"];
  }
  if (mw) return [mw, "warn"];
  if (micClipping() && micRunning()) return [CLIP_WORDS, "warn"];
  return ["PHONE MIC · " + (UI.prefs.mspeed === "fast" ? "FAST" : "SLOW") + " · " + fracWord() + " · uncalibrated" + (UI.freeze ? " · FROZEN" : ""), "cy"];
}
const fracWord = () => ({ 12: "1/12", 6: "⅙", 3: "⅓" })[frac()] + " oct";
const CLIP_WORDS = "MIC CLIPPING · too loud for the phone: step back from the speakers";
const micShownLive = () => UI.prefs.rta === "mic" || UI.prefs.rta === "both";
// RESPONSE's pause reason now ("" = adding windows)
export function status() {
  if (!respShown()) return "";
  const l = pauseLocal();
  if (l) return l;
  if (!P.st) return "";                                   // subscribed, the first window on its way
  if (P.st !== "live") return boxWords(P.st);              // said once per change: no frames meanwhile
  if (P.subscribed && now() - P.lastAt > 2500) return "BOX: NO SPECTRUM";   // live, but the windows stopped
  if (P.prog !== true) return progWords(P.prog);
  return "";
}

// ------------------------------------------------------------------ the mode and its row
export function setMeasure(on) {
  on = !!on;
  if (UI.measure === on) return;
  UI.measure = on;
  if (on) {
    clearErr();
    if (UI.host && $("#alignRow").contains(UI.host)) closeStepper(true);   // BAL / DELAY / LO-CUT steps: their tile goes
  }
  syncSub();
  render();
}
export function toggleMeasure() { setMeasure(!UI.measure); hap("tick"); }
export function initMeasure() {
  const row = $("#measRow");
  $("#mMic").addEventListener("click", () => {
    if (!allowed()) { toast(M.perm === "denied" ? "MIC BLOCKED: SETUP › THIS PHONE" : "ALLOW THE MIC: SETUP › THIS PHONE", "red", 2600); hap("reject"); return; }
    setMic(UI.prefs.mic === "off");
    hap("tick");
  });
  for (const b of $$("button[data-view]", row)) {
    b.addEventListener("click", () => {
      const v = b.dataset.view;
      if (v === "resp" && !hasPgm()) {
        toast(K.hello && K.hello.pgmKnown ? "RESPONSE: this box has no spectrum (k1000.yaml)" : "RESPONSE needs the box update", "red", 2600); hap("reject"); return;
      }
      UI.prefs.mview = v; savePrefs();
      if (v === "resp") compute();
      syncSub(); hap("tick"); render();
    });
  }
  $("#mFreeze").addEventListener("click", () => { UI.freeze = !UI.freeze; syncSub(); hap("tick"); render(); });
  $("#mAvg").addEventListener("click", () => { UI.prefs.avg = span() === 20 ? 10 : 20; savePrefs(); compute(); hap("tick"); render(); });
  $("#mReset").addEventListener("click", () => { restart(""); note("AVERAGE RESET · walk the room"); hap("tick"); render(); });
}
export function renderMeasure() {
  $("#alignRow").hidden = UI.measure;
  $("#measRow").hidden = !UI.measure;
  const b = $("#measBtn");
  b.setAttribute("aria-pressed", String(UI.measure));
  setText(b.querySelector("span"), UI.measure ? "≋ ✕" : "≋");
  b.setAttribute("aria-label", UI.measure ? "Leave MEASURE" : "MEASURE: the phone mic and RESPONSE");
  const gn = $("#gNote"), n = graphNote();
  gn.hidden = !n;
  if (n) { setText(gn, n[0]); gn.className = "gnote " + n[1]; }
  if (!UI.measure) return;
  const mk = $("#mMic");
  mk.className = "mk mic " + (micRunning() ? "on" : !allowed() ? (M.perm === "denied" ? "bad" : "no") : UI.prefs.mic === "off" ? "off" : "wait");
  mk.setAttribute("aria-label", "Phone mic: " + (micRunning() ? "on" : UI.prefs.mic === "off" ? "off" : "not running"));
  for (const v of $$("#measRow button[data-view]")) {
    v.setAttribute("aria-pressed", String(v.dataset.view === (UI.prefs.mview === "resp" ? "resp" : "rta")));
    v.classList.toggle("na", v.dataset.view === "resp" && !hasPgm());
  }
  $("#mFreeze").setAttribute("aria-pressed", String(UI.freeze));
  setText($("#mAvg span"), span() + " s");
  if (respShown() && !P.last) compute();
}
// for the lab and STATUS: read-only
export function measureView() {
  return { measure: UI.measure, view: UI.prefs.mview, pgm: hasPgm(), subscribed: P.subscribed, frames: P.frames, added: P.added, dropped: P.dropped,
    restarts: P.restarts, reason: status(), lastReason: P.reason, cover: avg.coverage(now() / 1000, span()), span: span(), settled: P.settled, maxHz: maxHz(), firstAddMs: P.firstAddMs,
    line: P.last ? { f: Array.from(P.last.f), db: Array.from(P.last.db, (v) => (Number.isFinite(v) ? v : null)), quiet: P.last.quiet, count: P.last.count } : null,
    note: graphNote() };
}

// SPDX-License-Identifier: GPL-3.0-or-later
/* The phone mic (DESIGN §7, docs/11-phone-mic.md): Chrome's getUserMedia into Web Audio, on this
   phone only. Audio never leaves the phone: nothing here touches the link.

   Permission (DESIGN §7, §9.15): asked ONLY from SETUP › THIS PHONE › Phone mic (allowFromSetup).
   LIVE, the PGM ▾ popover and MEASURE never call getUserMedia without a grant already known: a
   system dialog there would cover MUTE mid-show. Known = the browser's Permissions API says
   `granted`, or this page load got the mic from SETUP (the APK's WebView asks the app, which
   grants at once once Android's own permission is given). Without it they say
   `ALLOW THE MIC: SETUP › THIS PHONE`.

   Running: the tracks are open only while the mic is a shown source (PGM ▾ on PHONE MIC or BOTH,
   or MEASURE) and LIVE is in front: no sheet over it, page visible, portrait, the link not lost
   for LOST_LONG_MS. Otherwise the tracks are STOPPED (track.stop(), the AudioContext closed), and
   they come back without any prompt only while the grant holds and the source is still chosen.

   The line (RTA): AnalyserNode (FFT 16384, no smoothing), power-domain FAST / SLOW, smoothed
   1/12 · 1/6 · 1/3 oct, shown as band power (pink noise reads flat) on the EQ grid's dB scale,
   placed by a slow level tracker (uncalibrated: its loudest band 100 Hz-12 kHz sits at PLACE_DB),
   faint below 100 Hz. Its PEAK: the box's rule in its own instance (micmath.PeakRule) on the
   box's own numbers: the ENGINE's estimator (micmath.EngineEstimator, Hann 8192, the loudest FFT
   bin per 1/24 oct band, as CamillaDSP gives the box) on the analyser's last samples, so ENTER
   10 dB means on the phone what it means on the box (the analyser's mean power per band made the
   phone 3 dB more sensitive at 200 Hz and 5-8 dB less at 4-11 kHz); gated by the program (the
   meters' input peaks, or RESPONSE's pgm `prog`) and the mute. A frame whose last block reaches
   CLIP_ABS sets M.clipAt (said on the graph; RESPONSE drops its window).
   RESPONSE (measure.js) reads the same samples through `hooks.tick`. */

import { K, UI, linked, render, savePrefs } from "./store.js";
import { on } from "./link.js";
import { mic as hook, rotated, syncHooks } from "./rta.js";
import * as MM from "./micmath.js";
import { load, now, num, save } from "./util.js";

export const PLACE_DB = -3;            // where the line's loudest band sits on the EQ grid (dB)
const FFT_SIZE = 16384, FRAME_MS = 66, LOST_LONG_MS = 10000;
const DISP = MM.logPoints(20, 20000, 48);              // the line: 1/48 oct points
const PK = MM.engineGrid(10, 20000, 264);              // the PEAK: the box's 1/24 oct grid
const FRACS = [12, 6, 3];

export const M = {
  perm: "unknown",           // the Permissions API: unknown | prompt | granted | denied; or unsupported
  session: false,            // this page load got the mic from SETUP
  state: "off",              // off | starting | on
  err: "",                   // the last refusal or failure, in plain words
  settings: load("k1000v2.micinfo", null),   // what the browser kept at the last open (getSettings)
  rate: 0, frames: 0, starts: 0, stops: 0, suspended: false,
  gum: [],                   // every getUserMedia call: {from, sheet, tab, measure, pop, at}
  line: null,                // {db: band dB (unplaced), top: the tracked level}
  peak: null,                // the mic PEAK (Hz) or null
  prog: null,                // the PEAK's program gate now
  clipAt: 0,                 // the last frame whose last block clipped (now() ms), 0 = never
};
export const hooks = { tick: null };     // measure.js: (timeData, rate, t) every frame while on
let live = null, gen = 0, timer = 0, lostAt = 0, lastT = 0;
const ball = new MM.Ballistics(FFT_SIZE / 2);
const level = new MM.LevelTracker({ riseTau: 0.15, fallDbPerS: 1 });
const rule = new MM.PeakRule();
let detDb = null, estPk = null, detPow = null;
export const CLIP_SHOW_MS = 1500;      // "CLIPPING" stays said this long after the last clipped block
export const clipping = () => M.clipAt > 0 && now() - M.clipAt < CLIP_SHOW_MS;

// ------------------------------------------------------------------ what the page may do
export const allowed = () => M.perm === "granted" || M.session;
export const frac = () => (FRACS.includes(+UI.prefs.msmooth) ? +UI.prefs.msmooth : 6);
export const fast = () => UI.prefs.mspeed === "fast";
// the mic is a chosen source: MEASURE always (its RTA and RESPONSE views), LIVE when PGM ▾ says so
export const chosen = () => UI.prefs.mic !== "off" && (UI.measure || UI.prefs.rta === "mic" || UI.prefs.rta === "both");
const lostLong = () => !linked() && lostAt > 0 && now() - lostAt > LOST_LONG_MS;
export function wanted() {
  return allowed() && chosen() && !!K.hello && !document.hidden && !UI.sheet && !rotated() && !lostLong();
}
export const running = () => M.state === "on" && !!live;
// the RTA view of the line (LIVE, or MEASURE's RTA): the line and its PEAK are drawn
const rtaView = () => !UI.measure || UI.prefs.mview !== "resp";
// why the mic is not running although chosen, in the page's words (null: it runs, or not chosen)
export function why() {
  if (!chosen()) return null;
  if (M.perm === "unsupported") return M.err || "NO MIC IN THIS BROWSER";
  if (M.perm === "denied" && !M.session) return "MIC BLOCKED: SETUP › THIS PHONE";
  if (!allowed()) return "ALLOW THE MIC: SETUP › THIS PHONE";
  if (M.state === "starting") return "MIC STARTING…";
  if (M.err && M.state === "off") return M.err;
  if (M.state === "on" && M.suspended) return "MIC READY · tap anywhere to start it";
  return null;
}
function wantRate() {
  const H = K.hello;
  if (H && H.pgm && num(H.pgm.rate) && H.pgm.rate >= 8000 && H.pgm.rate <= 192000) return H.pgm.rate;
  return H && num(H.fs) && H.fs >= 8000 && H.fs <= 192000 ? H.fs : 48000;
}

// ------------------------------------------------------------------ the permission
if (typeof navigator !== "undefined" && navigator.permissions && navigator.permissions.query) {
  navigator.permissions.query({ name: "microphone" }).then((st) => {
    M.perm = st.state;
    st.onchange = () => {
      M.perm = st.state;
      if (st.state !== "granted") M.session = false;        // revoked: never again without SETUP
      else if (/BLOCKED/.test(M.err)) M.err = "";             // allowed again in the browser's settings
      sync(); render();
    };
    sync(); render();
  }).catch(() => { /* not in this browser: the SETUP grant alone counts */ });
}
function standalone() {
  try { return ["standalone", "fullscreen"].some((m) => matchMedia("(display-mode: " + m + ")").matches); } catch (e) { return false; }
}
function micError(e) {
  const n = e && e.name;
  if (n === "NotAllowedError" || n === "SecurityError" || n === "PermissionDeniedError") {
    return UI.native ? "MIC BLOCKED: long-press the K1000 icon › App info › Permissions › Microphone"
      : standalone() ? "MIC BLOCKED: Chrome › Settings › Site settings › Microphone › " + location.hostname
        : "MIC BLOCKED: the icon left of the address › Permissions › Microphone";
  }
  if (n === "NotFoundError" || n === "DevicesNotFoundError" || n === "OverconstrainedError") return "NO MIC FOUND ON THIS PHONE";
  if (n === "NotReadableError" || n === "TrackStartError" || n === "AbortError") return "THE MIC IS BUSY: another app has it";
  return "MIC UNAVAILABLE (" + (n || "error") + ")";
}

// ------------------------------------------------------------------ open / close
// fromSetup: the only call that may show the browser's (or Android's) dialog
async function open(fromSetup) {
  if (M.state !== "off") return;
  if (!fromSetup && !allowed()) return;                    // never a prompt outside SETUP
  const AC = typeof window !== "undefined" ? (window.AudioContext || window.webkitAudioContext) : null;
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    M.perm = "unsupported"; M.err = "THE MIC NEEDS THE HTTPS PAGE (https://" + location.hostname + "/v2/)"; render(); return;
  }
  if (!AC) { M.perm = "unsupported"; M.err = "NO WEB AUDIO IN THIS BROWSER"; render(); return; }
  const g = ++gen;
  M.state = "starting"; M.err = "";
  render();
  let stream = null, ac = null;
  const cancelled = () => Object.assign(new Error("cancelled"), { name: "Cancelled" });
  try {
    M.gum.push({ from: fromSetup ? "setup" : "auto", sheet: UI.sheet, tab: UI.tab, measure: UI.measure, pop: UI.popOpen, at: Math.round(now()) });
    if (M.gum.length > 50) M.gum.shift();
    stream = await navigator.mediaDevices.getUserMedia({ video: false, audio: {
      echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 } });
    const track = stream.getAudioTracks()[0];
    if (!track) throw Object.assign(new Error("no audio track"), { name: "NotFoundError" });
    M.session = true;
    const st = typeof track.getSettings === "function" ? track.getSettings() : {};
    M.settings = { echoCancellation: st.echoCancellation, noiseSuppression: st.noiseSuppression,
      autoGainControl: st.autoGainControl, sampleRate: st.sampleRate, channelCount: st.channelCount };
    save("k1000v2.micinfo", M.settings);
    if (fromSetup) { UI.prefs.mic = "on"; savePrefs(); }
    if (g !== gen) throw cancelled();
    if (!wanted()) throw cancelled();                     // allowed from SETUP: LIVE is covered, the tracks stop now
    const rate = wantRate();
    try { ac = new AC({ sampleRate: rate, latencyHint: "playback" }); } catch (e) { ac = new AC(); }
    let src;
    try { src = ac.createMediaStreamSource(stream); } catch (e) {
      try { ac.close(); } catch (x) { /* closed */ }
      ac = new AC(); src = ac.createMediaStreamSource(stream);
    }
    const an = ac.createAnalyser();
    an.fftSize = FFT_SIZE; an.smoothingTimeConstant = 0;
    src.connect(an);                                      // never to ac.destination: nothing is played
    if (ac.state !== "running") { try { await Promise.race([ac.resume(), new Promise((r) => setTimeout(r, 300))]); } catch (e) { /* a tap resumes it */ } }
    if (g !== gen) throw cancelled();
    live = { stream, track, ac, src, an, rate: ac.sampleRate, asked: rate, fbuf: new Float32Array(an.frequencyBinCount),
      pow: new Float64Array(an.frequencyBinCount), tbuf: new Float32Array(FFT_SIZE), binHz: ac.sampleRate / FFT_SIZE };
    M.rate = ac.sampleRate; M.starts++; M.suspended = ac.state !== "running";
    track.addEventListener("ended", () => {
      if (live && live.track === track) { close(); M.err = "THE MIC STOPPED (another app took it?)"; render(); }
    });
    ac.onstatechange = () => { if (live && live.ac === ac) { M.suspended = ac.state !== "running"; render(); } };
    M.state = "on";
    restartLine();
    lastT = 0;
    timer = setInterval(frame, FRAME_MS);
  } catch (e) {
    try { if (stream) stream.getTracks().forEach((t) => t.stop()); } catch (x) { /* stopped */ }
    try { if (ac) ac.close().catch(() => {}); } catch (x) { /* closed */ }
    if (g === gen) {
      M.state = "off";
      if (e && e.name !== "Cancelled") {
        M.err = micError(e);
        if (e.name === "NotAllowedError" || e.name === "SecurityError" || e.name === "PermissionDeniedError") { M.perm = "denied"; M.session = false; }
      }
    }
  }
  render();
}
function close() {
  gen++;
  clearInterval(timer); timer = 0;
  const L = live;
  live = null;
  if (L) {
    try { L.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* stopped */ }
    try { L.src.disconnect(); } catch (e) { /* gone */ }
    try { L.ac.close().catch(() => {}); } catch (e) { /* closed */ }
    M.stops++;
  }
  if (M.state !== "off") M.state = "off";
  M.suspended = false;
  if (!UI.freeze) { M.line = null; M.peak = null; }
  render();
}
// the line, its level and its PEAK start afresh (a new session, the view changed)
function restartLine() {
  ball.reset(); level.reset(); rule.reset();
  if (!UI.freeze) { M.line = null; M.peak = null; }
}
// Open or close to match wanted(). A failure (busy, ended, blocked) is not retried by itself:
// a user action (SETUP, PGM ▾, MIC ●) or the page coming back to the front clears it (clearErr).
export function sync() {
  if (M.state === "starting") return;                       // open() checks wanted() itself when the mic answers
  const want = wanted();
  if (want && M.state === "off" && !M.err) open(false);
  else if (!want && M.state !== "off") close();
  else if (live && live.asked !== wantRate()) close();     // the box's rate changed: reopened at the next sync
}
const BLOCKING = (e) => /BLOCKED|NO MIC|HTTPS|WEB AUDIO/.test(e);
export function clearErr() { if (M.err && !BLOCKING(M.err)) M.err = ""; }
syncHooks.push(sync);
// SETUP › THIS PHONE: the one place that asks
export function allowFromSetup() { M.err = ""; if (M.state === "off") open(true); }
export function setMic(on) {
  UI.prefs.mic = on ? "on" : "off"; savePrefs();
  if (!on) close(); else clearErr();
  sync(); render();
}

// ------------------------------------------------------------------ analysis
function frame() {
  const L = live;
  if (!L) return;
  if (L.ac.state !== "running") { M.suspended = true; return; }
  M.suspended = false;
  const t = now() / 1000;
  const dt = lastT ? Math.min(0.5, t - lastT) : FRAME_MS / 1000;
  lastT = t;
  if (UI.freeze || UI.dragging) return;                   // frozen: nothing analysed; a drag draws first
  M.frames++;
  // the samples: one read a frame, for the PEAK (the engine's estimator) and RESPONSE's tick
  L.an.getFloatTimeDomainData(L.tbuf);
  if (!estPk || !estPk.matches(L.rate, PK.min, PK.max, PK.n)) { estPk = new MM.EngineEstimator(L.rate, PK.min, PK.max, PK.n); detDb = null; rule.reset(); }
  if (MM.peakAbs(L.tbuf, estPk.n) >= MM.CLIP_ABS) M.clipAt = now();
  if (hooks.tick && UI.measure && UI.prefs.mview === "resp") {
    try { hooks.tick(L.tbuf, L.rate, t); } catch (e) { setTimeout(() => { throw e; }, 0); }
  }
  if (!rtaView()) return;
  L.an.getFloatFrequencyData(L.fbuf);
  MM.fromDb(L.fbuf, L.pow);
  ball.update(L.pow, dt);
  const sm = MM.smoothFracOct(fast() ? ball.fast : ball.slow, L.binHz, DISP, frac());
  const db = MM.bandDb(MM.toDb(sm), DISP, 1000);
  let top = -Infinity;
  for (let i = 0; i < DISP.length; i++) if (DISP[i] >= 100 && DISP[i] <= 12000 && db[i] > top) top = db[i];
  level.update(top, dt);
  M.line = { db, top: level.value };
  // the PEAK: this frame through the engine's estimator on the box's grid (the rule averages it
  // over 0.25 s itself), gated. No PEAK when the context cannot cover the search range (a rate
  // under 26.4 kHz: its bands above Nyquist would read the last FFT bin)
  detPow = estPk.bands(L.tbuf, detPow || undefined);
  M.prog = MM.programGate(M.prog, K.meters && K.meters.i, K.state ? K.state.muted : null, progHint());
  if (detPow && L.rate >= 2.2 * rule.search[1]) {
    detDb = MM.toDb(detPow, detDb || undefined);
    M.peak = fineHz(L, rule.update(PK.f, detDb, t, M.prog));
  } else M.peak = null;
  render();
}
// The PEAK's readout: the rule's 1/24 oct bin, refined on the FFT itself (SLOW): the loudest FFT
// bin within ±1/24 oct and a parabola through it and its neighbours (in dB). A 1 kHz tone reads
// 1.00k, not the grid's 990. The rule (where, when, how long) stays the box's.
function fineHz(L, hz) {
  if (!hz) return null;
  const P = ball.slow, bw = L.binHz;
  const lo = Math.max(1, Math.floor(hz * Math.pow(2, -1 / 24) / bw)), hi = Math.min(P.length - 2, Math.ceil(hz * Math.pow(2, 1 / 24) / bw));
  if (hi < lo) return hz;
  let k = lo;
  for (let i = lo; i <= hi; i++) if (P[i] > P[k]) k = i;
  const a = MM.powToDb(P[k - 1]), b = MM.powToDb(P[k]), c = MM.powToDb(P[k + 1]), den = a - 2 * b + c;
  const d = den < 0 ? Math.max(-0.5, Math.min(0.5, 0.5 * (a - c) / den)) : 0;
  return (k + d) * bw;
}
// RESPONSE's last pgm window says whether the program is present (fresher than the meters)
let pgmProg = { v: null, at: 0 };
export function notePgmProg(v) { pgmProg = { v, at: now() }; }
const progHint = () => (now() - pgmProg.at < 1500 ? pgmProg.v : null);

// ------------------------------------------------------------------ the graph's hooks
hook.shown = () => running() && rtaView() && !!M.line;
hook.peakHz = () => (hook.shown() ? M.peak : null);
hook.snap = (f) => {
  if (!detDb) return f;
  const bins = Float32Array.from(rule.avg ? Array.from(rule.avg, (p) => 10 * Math.log10(Math.max(p, 1e-30))) : detDb);
  let best = 0, bd = 1 / 3;
  for (let i = 3; i < bins.length - 3; i++) {
    if (PK.f[i] < 100 || bins[i] < bins[i - 1] || bins[i] < bins[i + 1]) continue;
    if (bins[i] - (bins[i - 3] + bins[i + 3]) / 2 < 3) continue;
    const d = Math.abs(Math.log2(PK.f[i] / f));
    if (d <= bd) { bd = d; best = PK.f[i]; }
  }
  return best || f;
};
// g: {xOf, yOf, top, bot, cyan}: the cyan line, faint and dashed below 100 Hz (the phone mic
// cannot say there), a dashed edge at 100 Hz
const K100 = DISP.findIndex((f) => f >= 100);
hook.draw = (cx, g) => {
  if (!hook.shown()) return;
  const ln = M.line, off = PLACE_DB - (num(ln.top) ? ln.top : 0);
  const y = (i) => Math.max(g.top, Math.min(g.bot, g.yOf(ln.db[i] + off)));
  const part = (from, to, alpha, w, dash) => {
    cx.beginPath();
    for (let i = from; i <= to; i++) { const xx = g.xOf(DISP[i]); if (i === from) cx.moveTo(xx, y(i)); else cx.lineTo(xx, y(i)); }
    cx.globalAlpha = alpha; cx.strokeStyle = g.cyan; cx.lineWidth = w; cx.setLineDash(dash || []);
    cx.stroke();
    cx.setLineDash([]); cx.globalAlpha = 1;
  };
  part(0, K100, 0.3, 1.5, [3, 3]);
  part(K100, DISP.length - 1, 0.95, 2, null);
  const x = Math.round(g.xOf(100)) + 0.5;
  cx.globalAlpha = 0.35; cx.strokeStyle = g.cyan; cx.lineWidth = 1; cx.setLineDash([2, 4]);
  cx.beginPath(); cx.moveTo(x, g.top); cx.lineTo(x, g.bot); cx.stroke();
  cx.setLineDash([]); cx.globalAlpha = 1;
};

// ------------------------------------------------------------------ the page's life
on("lost", () => { lostAt = now(); });
on("state", () => { lostAt = 0; });
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => { if (!document.hidden) clearErr(); sync(); });
  window.addEventListener("pagehide", () => close());
  // a context the browser kept suspended (no tap yet on this page load) starts with the next touch
  window.addEventListener("pointerdown", () => { if (live && live.ac.state !== "running") live.ac.resume().catch(() => {}); }, true);
}
export const micLine = () => (M.line ? { f: Array.from(DISP), db: Array.from(M.line.db), top: M.line.top } : null);

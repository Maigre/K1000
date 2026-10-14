// SPDX-License-Identifier: GPL-3.0-or-later
/* Unit checks of the phone-mic maths (k1000/web/static/v2/js/micmath.js), node only:
     node box/tools/v2lab/micunit.mjs
   The night/phone-rta branch's 18 tests (ported), the box's PEAK rule against the box's own
   test cases (tests/test_rta.py), the pgm frame codec, the RESPONSE windows and the program gate;
   the estimator against an independent numpy port of CamillaDSP's spectrum.rs
   (spectrum_ref.json, made by spectrum_ref.py), the mic PEAK on the engine's numbers, the master
   divided out of RESPONSE, its 100 Hz-12 kHz range, the clip flag.
   Prints one JSON object {ok, checks:[{name, ok, detail}]}; exit 1 on a failure. */

import fs from "node:fs";
import * as R from "../../k1000/web/static/v2/js/micmath.js";
import { makeSignal, RATE as WAV_RATE } from "./wav.mjs";

const checks = [];
const check = (name, ok, detail = "") => checks.push({ name, ok: !!ok, detail: ok ? "" : String(detail) });
const near = (a, b, tol) => Math.abs(a - b) <= tol;
const octs = (a, b) => Math.abs(Math.log2(a / b));
function run(name, fn) { try { fn(); } catch (e) { check(name + " (ran)", false, e && e.stack || e); } }

// a deterministic PRNG and Gaussian noise (Box-Muller), so the statistics checks never flake
function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
function gauss(r) { let u = 0; while (u === 0) u = r(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * r()); }
// pink noise frames, optionally through a gain curve gainDb(f), made in the frequency domain
function noiseFrame(n, rate, r, gainDb) {
  const fft = new R.FFT(n);
  const re = new Float64Array(n), im = new Float64Array(n);
  for (let k = 1; k < n / 2; k++) {
    const f = k * rate / n;
    const a = (1 / Math.sqrt(f)) * Math.pow(10, (gainDb ? gainDb(f) : 0) / 20);
    re[k] = gauss(r) * a; im[k] = gauss(r) * a;
    re[n - k] = re[k]; im[n - k] = -im[k];
  }
  for (let k = 0; k < n; k++) im[k] = -im[k];       // inverse FFT = conj(FFT(conj(X))) / n
  fft.transform(re, im);
  const x = new Float32Array(n);
  for (let k = 0; k < n; k++) x[k] = 3000 * re[k] / n;
  return x;
}

// ================================================================== the night branch's tests
run("logPoints", () => {
  const p = R.logPoints(20, 20000, 48);
  check("log points span 20 Hz-20 kHz at 48 per octave", p.length === Math.floor(Math.log2(1000) * 48) + 1 && near(p[0], 20, 1e-9) && p[p.length - 1] <= 20000 && p[p.length - 1] > 19700 && near(p[48], 40, 1e-9));
});
run("db", () => {
  const out = R.fromDb(new Float32Array([0, -10, -Infinity, NaN]));
  check("dB <-> power: silence (-Infinity) is zero power, the floor is -200 dB",
    near(out[0], 1, 1e-12) && near(out[1], 0.1, 1e-9) && out[2] === 0 && out[3] === 0 && R.powToDb(0) === -200 && near(R.powToDb(0.5), -3.0103, 1e-4));
});
run("ballistics", () => {
  const b = new R.Ballistics(1);
  b.update(new Float64Array([0]), 0.05);
  let t = 0;
  while (t < 0.125 - 1e-9) { b.update(new Float64Array([1]), 0.005); t += 0.005; }
  const fast1 = b.fast[0], slow1 = b.slow[0];
  while (t < 1.0 - 1e-9) { b.update(new Float64Array([1]), 0.005); t += 0.005; }
  check("ballistics: FAST 125 ms and SLOW 1 s time constants",
    near(fast1, 1 - Math.exp(-1), 0.01) && near(slow1, 1 - Math.exp(-0.125), 0.01) && near(b.slow[0], 1 - Math.exp(-1), 0.01),
    [fast1, slow1, b.slow[0]]);
  const c = new R.Ballistics(1);
  for (let i = 0; i < 4000; i++) c.update(new Float64Array([i % 2]), 0.01);
  check("ballistics average POWER: 1 and 0 alternating settles at -3 dB", near(R.powToDb(c.slow[0]), -3.01, 0.1), R.powToDb(c.slow[0]));
});
run("smooth", () => {
  const n = 8193, binHz = 48000 / 16384;
  const flat = new Float64Array(n).fill(2);
  const pts = R.logPoints(20, 20000, 48);
  let ok = true;
  for (const frac of [12, 6, 3]) { const s = R.smoothFracOct(flat, binHz, pts, frac); for (let i = 0; i < s.length; i++) ok = ok && near(s[i], 2, 1e-9); }
  check("fractional-octave smoothing keeps a flat spectrum flat", ok);
  const line = new Float64Array(n);
  const k = Math.round(2500 / binHz); line[k] = 1;
  const f0 = k * binHz;
  let ok2 = true;
  for (const frac of [12, 3]) {
    const s = R.smoothFracOct(line, binHz, [f0], frac);
    const width = f0 * (Math.pow(2, 1 / (2 * frac)) - Math.pow(2, -1 / (2 * frac))) / binHz;
    ok2 = ok2 && near(s[0], 1 / width, 1e-9);
  }
  check("smoothing spreads a line over its window", ok2);
  const ramp = new Float64Array(n); for (let i = 0; i < n; i++) ramp[i] = i;
  const s = R.smoothFracOct(ramp, binHz, [5.5 * binHz], 12);
  check("below a bin's width: a one-bin window, interpolating (no steps)", near(s[0], 5.5, 0.01), s[0]);
});
run("hold", () => {
  const h = Float64Array.from([1, 5, 3]);
  R.peakHold(h, [2, 4, 9]);
  check("peak hold keeps the max per bin", Array.from(h).join(",") === "2,5,9");
});
run("level", () => {
  const t = new R.LevelTracker({ riseTau: 0.15, fallDbPerS: 1 });
  t.update(-40, 0.066);
  let v = -40;
  for (let i = 0; i < 8; i++) v = t.update(-20, 0.066);
  const rose = v > -21;
  const top = v;
  for (let i = 0; i < 30; i++) v = t.update(-60, 0.1);
  const fell = near(v, top - 3, 1e-9);
  const before = v;
  check("the level tracker rises fast, falls 1 dB/s, ignores a bad frame", rose && fell && t.update(NaN, 0.1) === before, [top, v]);
});
run("fft", () => {
  const n = 64, r = rng(6);
  const x = Array.from({ length: n }, () => r() - 0.5);
  const re = Float64Array.from(x), im = new Float64Array(n);
  new R.FFT(n).transform(re, im);
  let ok = true;
  for (let k = 0; k < n; k++) {
    let a = 0, b = 0;
    for (let j = 0; j < n; j++) { a += x[j] * Math.cos(2 * Math.PI * j * k / n); b -= x[j] * Math.sin(2 * Math.PI * j * k / n); }
    ok = ok && near(re[k], a, 1e-9) && near(im[k], b, 1e-9);
  }
  let threw = false;
  try { new R.FFT(48); } catch (e) { threw = true; }
  check("FFT matches a direct DFT; a non-power of two is refused", ok && threw);
});
run("fftlen", () => {
  check("the engine's FFT length rule (spectrum.rs fft_length_for)",
    R.engineFftLen(48000, 10) === 8192 && R.engineFftLen(44100, 10) === 8192 && R.engineFftLen(48000, 20) === 4096 && R.engineFftLen(48000, 5) === 16384 && R.engineFftLen(48000, 0.01) === 262144);
});
run("grid", () => {
  const g = R.engineGrid(10, 20000, 264);
  let ok = near(g.f[0], 10, 1e-9) && near(g.f[263], 20000, 1e-6) && near(Math.log2(g.ratio), 1 / 24, 0.001);
  const t = R.bandTable(g, 48000 / 8192, 4097);
  for (let i = 0; i < g.n; i++) {
    const fc = g.f[i], sq = Math.sqrt(g.ratio);
    const fl = i === 0 ? 10 : fc / sq, fh = i === g.n - 1 ? 20000 : fc * sq;
    ok = ok && t.lo[i] === Math.floor(fl / (48000 / 8192)) && t.hi[i] === Math.min(Math.ceil(fh / (48000 / 8192)), 4096);
  }
  check("the box's grid (264 bins, 10 Hz-20 kHz, 1/24 oct) and its band edges", ok && t.lo[0] === t.lo[1]);
});
run("sine", () => {
  const n = 8192, rate = 48000, f0 = 1000;
  const x = Float32Array.from({ length: n }, (_, i) => Math.sin(2 * Math.PI * f0 * i / rate));
  const est = new R.EngineEstimator(rate, 10, 20000, 264);
  const b = est.bands(x);
  let best = 0;
  for (let i = 1; i < 264; i++) if (Math.abs(est.grid.f[i] - f0) < Math.abs(est.grid.f[best] - f0)) best = i;
  const db = R.powToDb(b[best]);
  const long = new Float32Array(2 * n); long.set(x, n);
  const b2 = est.bands(long);
  check("a full-scale sine reads ~0 dBFS through the engine's estimator; the LAST n samples are used", db > -3 && db < 1 && near(b2[best], b[best], 1e-9) && est.n === 8192, db);
});
run("bandDb", () => {
  const f = R.logPoints(20, 20000, 48);
  const density = Float64Array.from(f, (x) => -40 - 10 * Math.log10(x / 1000));
  const band = R.bandDb(density, f, 1000);
  let ok = band.every((v) => near(v, -40, 1e-9));
  const binHz = 48000 / 16384, n = 8192;
  const pow = Float64Array.from({ length: n }, (_, k) => (k ? 1 / (k * binHz) : 0));
  const b = R.bandDb(R.toDb(R.smoothFracOct(pow, binHz, f, 12)), f, 1000);
  const ref = b[f.findIndex((x) => x >= 1000)];
  f.forEach((x, i) => { if (x >= 40 && x <= 16000) ok = ok && near(b[i], ref, 0.3); });
  check("the line shows band power: pink noise (-3 dB/oct density) reads flat within 0.3 dB, 40 Hz-16 kHz", ok);
});
run("response", () => {
  const g = R.engineGrid(10, 20000, 264);
  const prog = Float64Array.from(g.f, (f) => R.dbToPow(f > 12000 ? -95 : -30 - 3 * Math.log2(f / 1000)));
  const mic = Float64Array.from(g.f, (f, i) => prog[i] * R.dbToPow(-17 + (Math.abs(Math.log2(f / 2500)) < 0.03 ? 9 : 0)));
  const r = R.response(mic, prog, g.f);
  const i25 = g.f.findIndex((f) => Math.abs(Math.log2(f / 2500)) < 0.03);
  check("response = mic minus program, 0 dB = the median of 200 Hz-5 kHz", !r.quiet && near(r.offset, -17, 1e-9) && near(r.db[i25], 9, 1e-9) && near(r.db[g.f.findIndex((f) => f > 1000)], 0, 1e-9), [r.offset, r.db[i25]]);
  check("response: gaps where the program has nothing, nothing below 100 Hz", g.f.every((f, i) => (f <= 12000 && f >= 100) || Number.isNaN(r.db[i])));
  const q = R.response(mic, Float64Array.from(g.f, () => R.dbToPow(-120)), g.f);
  check("response: the program too low = quiet, nothing drawn", q.quiet && Array.from(q.db).every(Number.isNaN));
  const sm = R.smoothDb(r.db, 4);
  check("response smoothing keeps the gaps", Number.isNaN(sm[263]) && Number.isFinite(sm[150]));
});
run("smoothDb", () => {
  const imp = new Float64Array(41).fill(0); imp[20] = 1;
  const s4 = R.smoothDb(imp, 4), s8 = R.smoothDb(imp, 8);
  check("smoothDb on the 24-per-octave grid: 1/6 oct = [½ 1 1 1 ½]/4, 1/3 oct = 8 bins",
    near(s4[20], 0.25, 1e-12) && near(s4[18], 0.125, 1e-12) && near(s4[17], 0, 1e-12) && near(s8[20], 0.125, 1e-12), [s4[20], s4[18], s8[20]]);
  let sum = 0, peak = 0;
  for (const v of R.smoothDb(imp, 24 / 6)) { sum += v; peak = Math.max(peak, v); }
  check("smoothDb keeps the area and its width is the one asked", near(sum, 1, 1e-9) && near(1 / peak, 4, 0.02));
  const ramp = Float64Array.from({ length: 30 }, (_, i) => (i === 10 ? NaN : i));
  const r6 = R.smoothDb(ramp, 2);
  check("smoothDb: a gap stays a gap, its edges renormalise, a ramp stays a ramp", Number.isNaN(r6[10]) && near(r6[9], (0.5 * 8 + 9) / 1.5, 1e-12) && near(r6[20], 20, 1e-12));
});
run("matched", () => {
  // The engine reports the PEAK FFT bin per band. On noise that reads high, more so in wide bands
  // (high frequencies): an energy estimator on the mic side would show a false treble rise. The
  // phone uses the same estimator on the same grid, so the bias cancels.
  const n = 8192, rate = 48000, frames = 60;
  const est = new R.EngineEstimator(rate, 10, 20000, 264), tab = est.table, g = est.grid;
  const bell = (f) => 6 * Math.exp(-0.5 * Math.pow(Math.log2(f / 2500) / 0.08, 2));
  const rm = rng(7), rp = rng(8);
  const avg = new R.RespAvg(1e9), bad = new R.RespAvg(1e9);
  for (let k = 0; k < frames; k++) {
    const xm = noiseFrame(n, rate, rm, bell);
    const pm = R.hannPower(xm, est.fft, est.win, est.scratch);
    const micPk = R.bandPeaks(pm, tab);
    const e = new Float64Array(g.n);
    for (let i = 0; i < g.n; i++) { let s = 0; for (let j = tab.lo[i]; j <= tab.hi[i]; j++) s += pm[j]; e[i] = s / (tab.hi[i] - tab.lo[i] + 1); }
    const pg = est.bands(noiseFrame(n, rate, rp));
    avg.add(k, micPk, pg, 0.5); bad.add(k, e, pg, 0.5);
  }
  const m = avg.mean(frames, 1e9), mb = bad.mean(frames, 1e9);
  const r = R.response(m.mic, m.pgm, g.f), rb = R.response(mb.mic, mb.pgm, g.f);
  const at = (rr, f) => R.smoothDb(rr.db, 4)[g.f.findIndex((x) => x >= f)];
  const flat = [300, 600, 1000, 6000, 10000].every((f) => Math.abs(at(r, f)) <= 1.5);
  check("matched estimators: mic minus program is flat on noise (±1.5 dB, 300 Hz-10 kHz), a +6 dB bell reads 4.5..7.5",
    flat && at(r, 2500) >= 4.5 && at(r, 2500) <= 7.5, [300, 600, 1000, 2500, 6000, 10000].map((f) => f + ":" + at(r, f).toFixed(2)).join(" "));
  // (on the box's 1/24-octave grid; the night branch's 1/12-octave grid tilted more than 3 dB)
  check("mismatched estimators (energy on the mic, peak on the program) tilt by more than 2 dB", at(rb, 300) - at(rb, 10000) > 2, (at(rb, 300) - at(rb, 10000)).toFixed(2));
});

// ================================================================== the box's PEAK rule (tests/test_rta.py)
const FREQS = R.engineGrid(10, 20000, 264).f;
const binOf = (hz) => { let b = 0; for (let i = 0; i < FREQS.length; i++) if (Math.abs(Math.log2(FREQS[i] / hz)) < Math.abs(Math.log2(FREQS[b] / hz))) b = i; return b; };
const background = (level = -50) => Float64Array.from(FREQS, (f, i) => level - 3 * Math.log2(Math.max(f, 100) / 1000) + 1.5 * Math.sin(i * 1.3));
const withPeak = (hz, above, base) => { const m = Float64Array.from(base || background()); m[binOf(hz)] += above; return m; };
function feed(b, frames, t0, n, program = null, dt = 0.1) {
  const seq = frames instanceof Float64Array ? Array(n || 1).fill(frames) : frames;
  let t = t0;
  seq.forEach((m, k) => { t = t0 + k * dt; b.update(FREQS, m, t, program); });
  return t;
}
// the box's search range (40 Hz-16 kHz) for its own cases; the phone's is 100 Hz-12 kHz
const boxRule = () => new R.PeakRule({ search: [40, 16000], floorDb: -80 });
run("peak", () => {
  let b = boxRule();
  const m = withPeak(2440, 20);
  const t = feed(b, m, 0, 15);
  const early = b.peakHz;
  b.update(FREQS, m, 1.5);
  check("PEAK: needs 1.5 s, then points at its bin (within 1/48 oct)", near(t, 1.4, 1e-9) && early === null && b.shown === binOf(2440) && octs(b.peakHz, FREQS[binOf(2440)]) < 1 / 48 && octs(b.peakHz, 2440) < 1 / 48, [early, b.peakHz]);
  // between two bins: the parabola puts the pointer's frequency between them
  const mid = Math.sqrt(FREQS[binOf(1000)] * FREQS[binOf(1000) + 1]);
  const two = Float64Array.from(background()); two[binOf(1000)] += 20; two[binOf(1000) + 1] += 20;
  const b2 = boxRule(); feed(b2, two, 0, 16);
  check("PEAK: a peak between two bins reads between them (the readout, not the rule)", b2.peakHz != null && octs(b2.peakHz, mid) < 1 / 96, [b2.peakHz, mid]);

  b = boxRule();
  feed(b, withPeak(2440, 20), 0, 16);
  const had = b.peakHz != null;
  let tt = feed(b, withPeak(2440, 8), 1.6, 50);
  const kept = b.peakHz != null;
  const t0 = tt + 0.1;
  feed(b, withPeak(2440, 2), t0, 3);
  const notAtOnce = b.peakHz != null;
  feed(b, withPeak(2440, 2), t0 + 0.3, 25);
  const gone = b.peakHz == null;
  feed(b, withPeak(5000, 8), t0 + 3, 30);
  check("PEAK: kept while 6 dB above (hysteresis), released after 0.5 s, +8 dB never enters", had && kept && notAtOnce && gone && b.peakHz == null);

  b = boxRule();
  feed(b, withPeak(2440, 20), 0, 16);
  const shown = b.peakHz, i = binOf(2440);
  feed(b, Array.from({ length: 20 }, (_, k) => withPeak(FREQS[i + (k % 2)], 20)), 1.6);
  check("PEAK: the pointer is steady on a one-bin jitter (moves only by 2 bins)", b.peakHz === shown);

  const cases = {
    low: Array(30).fill(withPeak(30, 25)),
    high: Array(30).fill(withPeak(18000, 25)),
    wanders: Array.from({ length: 30 }, (_, k) => withPeak(500 * Math.pow(2, k * 4 / 24), 20)),
    plateau: Array(30).fill(Float64Array.from(background(), (v, j) => v + (FREQS[j] >= 300 && FREQS[j] <= 2400 ? 12 : 0))),
  };
  const none = Object.entries(cases).filter(([, fr]) => { const r = boxRule(); feed(r, fr, 0); return r.peakHz != null; }).map(([k]) => k);
  check("PEAK: none below the range, above it, for a sweep, or for a +12 dB plateau", !none.length, none);

  const bur = background(-60); bur[binOf(60)] = -10; bur[binOf(3000)] = Math.min(bur[binOf(3000)] + 15, -46);
  b = boxRule(); feed(b, bur, 0, 30);
  check("PEAK: one buried 35 dB under the loudest bin is not a PEAK", b.peakHz == null || octs(b.peakHz, 3000) > 0.1);

  const silent = Float64Array.from(FREQS, (_, j) => -125 + Math.sin(j * 1.3)); silent[binOf(7300)] = -114;
  b = boxRule(); feed(b, silent, 0, 50);
  const off = new R.PeakRule({ search: [40, 16000], floorDb: -140 }); feed(off, silent, 0, 20);
  check("PEAK: none on a silent input under the level floor (the same tone with the floor off is one)", b.peakHz == null && off.peakHz != null);

  b = boxRule();
  feed(b, withPeak(2440, 20), 0, 30, false);
  const noProg = b.peakHz == null;
  for (let k = 30; k < 46; k++) b.update(FREQS, withPeak(2440, 20), k * 0.1, true);
  const withProg = b.peakHz != null;
  b.update(FREQS, withPeak(2440, 20), 4.6, false);
  const notYet = b.peakHz != null;
  let t2 = 4.6;
  while (b.peakHz != null && t2 < 10) { t2 = Math.round((t2 + 0.1) * 1000) / 1000; b.update(FREQS, withPeak(2440, 20), t2, false); }
  check("PEAK: the program gate: none without the program, gone within 0.5 s when it stops",
    noProg && withProg && notYet && b.peakHz == null && t2 - 4.6 <= 0.5 + 1e-9, [noProg, withProg, notYet, t2]);

  const ph = new R.PeakRule();
  check("PEAK: the phone's search range is 100 Hz-12 kHz", ph.search[0] === 100 && ph.search[1] === 12000);
  const p2 = new R.PeakRule();
  feed(p2, withPeak(14000, 25), 0, 30);
  const p3 = new R.PeakRule();
  feed(p3, withPeak(1000, 20), 0, 20);
  check("PEAK: the phone finds 1 kHz, never 14 kHz", p2.peakHz == null && p3.peakHz != null && octs(p3.peakHz, 1000) < 1 / 24);
});
run("gate", () => {
  check("program gate: muted = shut; the pgm window's own prog wins; meters' input peaks at -50 dBFS with 3 dB of hysteresis; nothing known = null",
    R.programGate(true, [-10, -10], true, true) === false && R.programGate(null, [-70, -70], false, true) === true &&
    R.programGate(null, [-49, -70], false, null) === true && R.programGate(null, [-52, -60], false, null) === false &&
    R.programGate(true, [-52, -60], false, null) === true && R.programGate(true, [-54, -60], false, null) === false &&
    R.programGate(null, null, false, null) === null && R.programGate(null, [], false) === null);
});

// ================================================================== pgm frames and the RESPONSE windows
run("pgm", () => {
  const db = Float64Array.from({ length: 264 }, (_, i) => -30 - i * 0.123 + (i === 7 ? -400 : 0));
  const m = R.encodePgm(db);
  const d = R.decodePgm({ t: "pgm", seq: 5, st: "live", n: 264, f0: 10, f1: 20000, k: 5, dt: 0.5, prog: true, m });
  let ok = d.st === "live" && d.n === 264 && d.k === 5 && d.dt === 0.5 && d.prog === true && d.seq === 5;
  for (let i = 0; i < 264; i++) ok = ok && near(d.db[i], i === 7 ? -327.68 : Math.round(db[i] * 100) / 100, 1e-9);
  check("pgm: base64 of n little-endian int16 = round(dBFS*100), clamped to int16, decodes back", ok, d.db && d.db.slice(0, 9).join(","));
  const w = R.decodePgm({ t: "pgm", seq: 6, st: "waiting", n: 0, m: null, k: 0, prog: null });
  const bad = R.decodePgm({ t: "pgm", st: "live", n: 264, f0: 10, f1: 20000, m: "AAAA" });
  const junk = R.decodePgm({ t: "pgm", st: "live", n: 264, f0: 10, f1: 20000, m: "%%%" });
  check("pgm: not live, short or broken = no data (st kept)", w.st === "waiting" && w.db === null && w.prog === null && bad.db === null && junk.db === null);
});
run("respavg", () => {
  const a = new R.RespAvg(60);
  const one = (v) => Float64Array.from([v, v]);
  for (let k = 0; k < 50; k++) a.add(k * 0.5, one(k), one(1), 0.5);
  const m20 = a.mean(25, 20), m10 = a.mean(25, 10);
  check("RESPONSE windows: the newest that cover 20 s (40 windows) or 10 s (20)", m20.count === 40 && m10.count === 20 && near(m20.mic[0], (10 + 49) / 2, 1e-9) && m20.cover === 20, [m20.count, m10.count, m20.mic[0]]);
  const b = new R.RespAvg(60);
  for (let k = 0; k < 10; k++) b.add(k * 0.5, one(1), one(1), 0.5);          // 5 s, then a pause...
  for (let k = 0; k < 10; k++) b.add(30 + k * 0.5, one(3), one(1), 0.5);     // ...and 5 s more
  const mb = b.mean(35, 20);
  check("RESPONSE windows: a pause holds the average (coverage 10 s of 20), not empties it", mb.count === 20 && mb.cover === 10 && near(mb.mic[0], 2, 1e-9), [mb.count, mb.cover]);
  check("RESPONSE windows: older than a minute are dropped (a long pause is a new measurement)", b.mean(85, 20).count === 10 && b.mean(200, 20) === null);
  b.clear();
  check("RESPONSE windows: clear() empties (a restart)", b.mean(35, 20) === null && b.coverage(35, 20) === 0);
});

// ================================================================== the review's fixes (2026-10-14)
// The engine's estimator against spectrum.rs re-done in numpy (not against micmath itself: the
// fake box's program windows use micmath, so the e2e alone is circular). The same signal on
// both sides: three tones and a little white noise from a 32-bit LCG, as f32.
function refSignal(n, rate) {
  let u = 12345;
  const x = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    u = (Math.imul(1664525, u) + 1013904223) >>> 0;
    const v = u / 4294967296 - 0.5;
    x[i] = 0.5 * Math.sin(2 * Math.PI * 997 * i / rate) + 0.01 * Math.sin(2 * Math.PI * 63.3 * i / rate) + 0.2 * Math.sin(2 * Math.PI * 12345 * i / rate) + 0.05 * v;
  }
  return x;
}
run("spectrumrs", () => {
  const ref = JSON.parse(fs.readFileSync(new URL("./spectrum_ref.json", import.meta.url), "utf8"));
  const rows = [];
  let ok = Object.keys(ref.rates).length === 2;
  for (const [rate, r] of Object.entries(ref.rates)) {
    const est = new R.EngineEstimator(+rate, ref.grid.f0, ref.grid.f1, ref.grid.n);
    const db = R.toDb(est.bands(refSignal(est.n, +rate)));
    let worst = 0;
    for (let i = 0; i < ref.grid.n; i++) if (r.db[i] > -120) worst = Math.max(worst, Math.abs(db[i] - r.db[i]));
    rows.push(rate + ": fft " + est.n + " / " + r.fft + ", worst " + worst.toFixed(4) + " dB");
    ok = ok && est.n === r.fft && r.db.length === 264 && worst <= 0.005;     // as built: 0.0001 dB; a Hann over n, not n - 1: 0.02
  }
  check("the engine's estimator matches spectrum.rs re-done in numpy (spectrum_ref.json) within 0.005 dB on every bin, 48 and 44.1 kHz", ok, rows.join("; "));
});

// The mic PEAK runs on the engine's numbers (mic.js: EngineEstimator, as the box): an 8 kHz tone
// 7 dB over its 1/24 oct band in pink noise is a PEAK there; the analyser's mean power per band
// (the first build's feed: Blackman 16384, smoothFracOct at 1/24) never makes it one, the box would
const PKG = R.engineGrid(10, 20000, 264);
function peakRun(x, rate, feed, hz) {
  const rule = new R.PeakRule();
  let hits = 0, frames = 0;
  for (let t = 0.4; t < 6.5; t += 0.066) {
    const end = Math.round(t * rate), p = rule.update(PKG.f, R.toDb(feed(x.subarray(end - 16384, end))), t, true);
    frames++;
    if (p && Math.abs(Math.log2(p / hz)) < 1 / 24) hits++;
  }
  return hits / frames;
}
run("peakest", () => {
  const x = makeSignal("tone", { hz: 8000, above: 7 });
  const est = new R.EngineEstimator(WAV_RATE, 10, 20000, 264);
  const N2 = 16384, fft = new R.FFT(N2), bw = new Float64Array(N2);
  for (let i = 0; i < N2; i++) bw[i] = 0.42 - 0.5 * Math.cos(2 * Math.PI * i / N2) + 0.08 * Math.cos(4 * Math.PI * i / N2);
  const analyser = (seg) => {
    const re = new Float64Array(N2), im = new Float64Array(N2);
    for (let i = 0; i < N2; i++) re[i] = seg[i] * bw[i];
    fft.transform(re, im);
    const p = new Float64Array(N2 / 2);
    for (let k = 0; k < N2 / 2; k++) p[k] = (re[k] * re[k] + im[k] * im[k]) / (N2 * N2);
    return R.smoothFracOct(p, WAV_RATE / N2, PKG.f, 24);
  };
  const eng = peakRun(x, WAV_RATE, (seg) => est.bands(seg), 8000), ana = peakRun(x, WAV_RATE, analyser, 8000);
  check("mic PEAK on the engine's estimator: an 8 kHz tone 7 dB over its 1/24 oct band is a PEAK (as on the box); on the analyser's mean power it never was",
    eng > 0.6 && ana === 0, { engine: eng.toFixed(2), analyser: ana.toFixed(2) });
});

// The master between the two: the program is taken before it, the mic hears after it. 20 windows,
// then the music gets +6 dB of bass AND the master goes +6 dB: divided out per tick
// (MicHalf.add(bands, master_db)), RESPONSE stays the room's (flat here); not divided, the bass
// reads ~1.3 dB high (the 0 dB median only absorbs a gain common to every window)
run("master", () => {
  const g = R.engineGrid(10, 20000, 264), n = g.n;
  const prog = (bass) => Float64Array.from(g.f, (f) => 1e-4 / Math.max(f, 20) * (f < 200 && bass ? 3.981 : 1));
  const fix = new R.RespAvg(1e9), raw = new R.RespAvg(1e9);
  for (let w = 0; w < 40; w++) {
    const second = w >= 20, master = second ? -8 : -14, p = prog(second), heard = Float64Array.from(p, (v) => v * R.dbToPow(master));
    const a = new R.MicHalf(n, 10, 20000), b = new R.MicHalf(n, 10, 20000);
    for (let k = 0; k < 5; k++) { a.add(heard, master); b.add(heard, 0); }
    fix.add(w, a.mean(), p, 0.5); raw.add(w, b.mean(), p, 0.5);
  }
  const at = (r, f) => r.db[g.f.findIndex((x) => x >= f)];
  const mf = fix.mean(40, 1e9), mr = raw.mean(40, 1e9);
  const rf = R.response(mf.mic, mf.pgm, g.f), rr = R.response(mr.mic, mr.pgm, g.f);
  check("RESPONSE: a master move during the average is divided out (flat within 0.05 dB); not divided, the bass would read > 1 dB high",
    Math.abs(at(rf, 120)) < 0.05 && Math.abs(at(rf, 3000)) < 0.05 && at(rr, 120) - at(rr, 3000) > 1, [at(rf, 120), at(rr, 120)].map((v) => v.toFixed(2)).join(" / "));
  const h = new R.MicHalf(264, 10, 20000);
  check("RESPONSE: a mic half and a pgm frame match their grid within 1e-6 (a float's last digit never skips every window)",
    h.sameGrid(264, 10, 20000.000000001) && h.sameGrid(264, 10.0000000001, 20000) && !h.sameGrid(264, 10, 20010) && !h.sameGrid(263, 10, 20000) && !h.sameGrid(264, 10, null));
});
run("respmax", () => {
  const g = R.engineGrid(10, 20000, 264);
  const p = Float64Array.from(g.f, (f) => 1e-4 / Math.max(f, 20)), m = Float64Array.from(p, (v) => v * 2);
  const r = R.response(m, p, g.f, { maxHz: 12000 }), all = R.response(m, p, g.f);
  check("RESPONSE: maxHz 12 kHz leaves nothing above it (DESIGN §7: the phone's range is 100 Hz-12 kHz); without it, up to 20 kHz",
    g.f.every((f, i) => (f >= 100 && f <= 12000) === Number.isFinite(r.db[i])) && Number.isFinite(all.db[263]));
});
run("clip", () => {
  const x = Float32Array.from([0.1, -0.995, 0.2, 0.5, -0.3]);
  check("clip flag: the largest |sample| of the last n (all by default), at CLIP_ABS 0.98", R.peakAbs(x) === Math.fround(0.995) && R.peakAbs(x, 3) === 0.5 && R.CLIP_ABS === 0.98 && R.peakAbs(x) >= R.CLIP_ABS && R.peakAbs(x, 3) < R.CLIP_ABS);
  const est = new R.EngineEstimator(48000, 10, 20000, 264);
  check("the estimator on fewer samples than its FFT (a rate the analyser cannot hold): null, never NaN", est.bands(new Float32Array(4096)) === null && est.bands(new Float32Array(8192)).every(Number.isFinite));
});

const ok = checks.every((c) => c.ok);
console.log(JSON.stringify({ ok, n: checks.length, checks }));
process.exit(ok ? 0 : 1);

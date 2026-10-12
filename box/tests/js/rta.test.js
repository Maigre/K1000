// SPDX-License-Identifier: GPL-3.0-or-later
// Unit tests for the phone-mic math (k1000/web/static/rta.js): `node --test box/tests/js/*.test.js`
// (also run by tests/test_rta_js.py when node is installed). No browser, no audio device.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const R = require(path.join(__dirname, "..", "..", "k1000", "web", "static", "rta.js"));

const near = (a, b, tol, what) => assert.ok(Math.abs(a - b) <= tol, `${what || ""} ${a} vs ${b} (±${tol})`);
const octs = (a, b) => Math.abs(Math.log2(a / b));

// a deterministic PRNG and Gaussian noise (Box-Muller), so the statistics tests never flake
function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
function gauss(r) { let u = 0; while (u === 0) u = r(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * r()); }

// pink-ish noise frames, optionally through a gain curve gainDb(f), made in the frequency domain
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
  for (let k = 0; k < n; k++) x[k] = 3000 * re[k] / n;   // about -40 dBFS per band at 1 kHz
  return x;
}

test("log points span 20 Hz-20 kHz at 48 per octave", () => {
  const p = R.logPoints(20, 20000, 48);
  assert.equal(p.length, Math.floor(Math.log2(1000) * 48) + 1);
  near(p[0], 20, 1e-9); assert.ok(p[p.length - 1] <= 20000 && p[p.length - 1] > 19700);
  near(p[48], 40, 1e-9, "one octave");
});

test("dB <-> power, silence (-Infinity) is zero power, the floor is -200 dB", () => {
  const out = R.fromDb(new Float32Array([0, -10, -Infinity, NaN]));
  near(out[0], 1, 1e-12); near(out[1], 0.1, 1e-9); assert.equal(out[2], 0); assert.equal(out[3], 0);
  assert.equal(R.powToDb(0), -200); near(R.powToDb(0.5), -3.0103, 1e-4);
});

test("ballistics average POWER with FAST 125 ms and SLOW 1 s time constants", () => {
  const b = new R.Ballistics(1);
  b.update(new Float64Array([0]), 0.05);                   // primed with silence
  let t = 0;
  while (t < 0.125 - 1e-9) { b.update(new Float64Array([1]), 0.005); t += 0.005; }
  near(b.fast[0], 1 - Math.exp(-1), 0.01, "FAST at one tau");
  near(b.slow[0], 1 - Math.exp(-0.125), 0.01, "SLOW at 125 ms");
  while (t < 1.0 - 1e-9) { b.update(new Float64Array([1]), 0.005); t += 0.005; }
  near(b.slow[0], 1 - Math.exp(-1), 0.01, "SLOW at one tau");
  // power domain: a steady alternation of 1 and 0 settles at 0.5 (-3 dB), not at the mean of dBs
  const c = new R.Ballistics(1);
  for (let i = 0; i < 4000; i++) c.update(new Float64Array([i % 2]), 0.01);
  near(R.powToDb(c.slow[0]), -3.01, 0.1);
});

test("fractional-octave smoothing keeps a flat spectrum flat and spreads a line over its window", () => {
  const n = 8193, binHz = 48000 / 16384;
  const flat = new Float64Array(n).fill(2);
  const pts = R.logPoints(20, 20000, 48);
  for (const frac of [12, 6, 3]) {
    const s = R.smoothFracOct(flat, binHz, pts, frac);
    for (let i = 0; i < s.length; i++) near(s[i], 2, 1e-9, `flat 1/${frac} @${pts[i].toFixed(0)}`);
  }
  // a single line at 2.5 kHz: smoothed at its own frequency = its power / the window in bins
  const line = new Float64Array(n);
  const k = Math.round(2500 / binHz); line[k] = 1;
  const f0 = k * binHz;
  for (const frac of [12, 3]) {
    const s = R.smoothFracOct(line, binHz, [f0], frac);
    const width = f0 * (Math.pow(2, 1 / (2 * frac)) - Math.pow(2, -1 / (2 * frac))) / binHz;
    near(s[0], 1 / width, 1e-9, `1/${frac}`);
  }
  // below a bin's width: a one-bin window, interpolating between neighbours (no steps)
  const ramp = new Float64Array(n); for (let i = 0; i < n; i++) ramp[i] = i;
  const s = R.smoothFracOct(ramp, binHz, [5.5 * binHz], 12);   // 1/12 oct = 0.32 bin there
  near(s[0], 5.5, 0.01);
});

test("peak hold keeps the max per bin", () => {
  const h = Float64Array.from([1, 5, 3]);
  R.peakHold(h, [2, 4, 9]);
  assert.deepEqual(Array.from(h), [2, 5, 9]);
});

test("the level tracker rises fast and falls at about 1 dB/s, without jumps", () => {
  const t = new R.LevelTracker({ riseTau: 0.15, fallDbPerS: 1 });
  t.update(-40, 0.066);
  let v = -40;
  for (let i = 0; i < 8; i++) v = t.update(-20, 0.066);            // ~0.5 s
  assert.ok(v > -21, `rose to ${v}`);
  const top = v;
  for (let i = 0; i < 30; i++) v = t.update(-60, 0.1);              // 3 s of a much quieter room
  near(v, top - 3, 1e-9, "fell 3 dB in 3 s");
  const before = v;
  v = t.update(NaN, 0.1);
  assert.equal(v, before, "a bad frame changes nothing");
});

function pinkTrace(pts, r, rippleDb) {
  return Float64Array.from(pts, (f) => -20 - 3 * Math.log2(f / 1000) + (r() - 0.5) * 2 * rippleDb);
}

test("findPeak: a narrow 10 dB ring on pink is found, its frequency within 1/48 oct", () => {
  const pts = R.logPoints(20, 20000, 48);
  const d = pinkTrace(pts, rng(1), 1);
  const i0 = Math.round(Math.log2(2500 / 20) * 48);
  d[i0] += 10; d[i0 - 1] += 5; d[i0 + 1] += 5;
  const p = R.findPeak(d, pts, { fMin: 100, fMax: 16000 });
  assert.ok(p, "found");
  assert.ok(octs(p.f, 2500) <= 1 / 48, `${p.f}`);
  assert.ok(p.prom >= 9 && p.prom <= 12, `prominence ${p.prom}`);
});

test("findPeak ignores broad humps, small bumps and anything below fMin", () => {
  const pts = R.logPoints(20, 20000, 48);
  const broad = pinkTrace(pts, rng(2), 0.3);
  for (let i = 0; i < pts.length; i++) broad[i] += 8 * Math.exp(-0.5 * Math.pow(Math.log2(pts[i] / 2000) / 0.25, 2));
  assert.equal(R.findPeak(broad, pts), null, "a 1/2-octave hump is not a ring");
  const small = pinkTrace(pts, rng(3), 0.3);
  const i1 = Math.round(Math.log2(3000 / 20) * 48); small[i1] += 4;
  assert.equal(R.findPeak(small, pts), null, "4 dB < 6 dB");
  const low = pinkTrace(pts, rng(4), 0.3);
  const i2 = Math.round(Math.log2(60 / 20) * 48); low[i2] += 15;
  assert.equal(R.findPeak(low, pts, { fMin: 100 }), null, "60 Hz is below the phone's range");
  // two rings: the more prominent wins
  const two = pinkTrace(pts, rng(5), 0.3);
  two[Math.round(Math.log2(800 / 20) * 48)] += 8;
  two[Math.round(Math.log2(4000 / 20) * 48)] += 14;
  assert.ok(octs(R.findPeak(two, pts).f, 4000) < 1 / 24);
});

test("the peak tracker needs 1.5 s of persistence, survives a stray frame, releases after 0.6 s", () => {
  const pt = new R.PeakTracker({ persistS: 1.5, tolOct: 1 / 12, releaseS: 0.6 });
  let out = null;
  for (let t = 0; t <= 1.4; t += 0.1) out = pt.update({ f: 2500 * (1 + 0.003 * Math.sin(t * 9)), prom: 10 }, t);
  assert.equal(out, null, "not yet at 1.4 s");
  out = pt.update({ f: 500, prom: 7 }, 1.45);                    // a stray frame elsewhere
  out = pt.update({ f: 2510, prom: 10 }, 1.55);
  assert.ok(out && octs(out.f, 2500) < 1 / 48, "confirmed at 1.55 s");
  out = pt.update(null, 1.9);
  assert.ok(out, "still there 0.35 s after the last sighting");
  out = pt.update(null, 2.3);
  assert.equal(out, null, "gone 0.75 s after");
  assert.equal(pt.update({ f: 2500, prom: 10 }, 2.4), null, "and it has to persist again");
});

test("FFT matches a direct DFT", () => {
  const n = 64, r = rng(6);
  const x = Array.from({ length: n }, () => r() - 0.5);
  const re = Float64Array.from(x), im = new Float64Array(n);
  new R.FFT(n).transform(re, im);
  for (let k = 0; k < n; k++) {
    let a = 0, b = 0;
    for (let j = 0; j < n; j++) { a += x[j] * Math.cos(2 * Math.PI * j * k / n); b -= x[j] * Math.sin(2 * Math.PI * j * k / n); }
    near(re[k], a, 1e-9); near(im[k], b, 1e-9);
  }
  assert.throws(() => new R.FFT(48));
});

test("the engine's FFT length rule (CamillaDSP spectrum.rs fft_length_for)", () => {
  assert.equal(R.engineFftLen(48000, 10), 8192);
  assert.equal(R.engineFftLen(44100, 10), 8192);
  assert.equal(R.engineFftLen(48000, 20), 4096);
  assert.equal(R.engineFftLen(48000, 5), 16384);
  assert.equal(R.engineFftLen(48000, 0.01), 262144);
});

test("the engine's grid and bins: 133 bins from 10 Hz to 20 kHz are ~1/12 octave", () => {
  const g = R.engineGrid(10, 20000, 133);
  near(g.f[0], 10, 1e-9); near(g.f[132], 20000, 1e-6);
  near(Math.log2(g.ratio), 1 / 12, 0.001);
  const t = R.bandTable(g, 48000 / 8192, 4097);
  for (let i = 0; i < g.n; i++) {
    const fc = g.f[i], sq = Math.sqrt(g.ratio);
    const fl = i === 0 ? 10 : fc / sq, fh = i === g.n - 1 ? 20000 : fc * sq;
    assert.equal(t.lo[i], Math.floor(fl / (48000 / 8192)));
    assert.equal(t.hi[i], Math.min(Math.ceil(fh / (48000 / 8192)), 4096));
  }
  // at the bottom, neighbours share their FFT bins (seen on the box: "neighbours repeat")
  assert.equal(t.lo[0], t.lo[1]);
});

test("a full-scale sine reads ~0 dBFS through the engine's estimator (as spectrum.rs's own test)", () => {
  const n = 8192, rate = 48000, f0 = 1000;
  const x = Float32Array.from({ length: n }, (_, i) => Math.sin(2 * Math.PI * f0 * i / rate));
  const fft = new R.FFT(n), win = R.hann(n);
  const p = R.hannPower(x, fft, win, { re: new Float64Array(n), im: new Float64Array(n) });
  const g = R.engineGrid(10, 20000, 133);
  const b = R.bandPeaks(p, R.bandTable(g, rate / n, n / 2 + 1));
  let best = 0;
  for (let i = 1; i < g.n; i++) if (Math.abs(g.f[i] - f0) < Math.abs(g.f[best] - f0)) best = i;
  const db = R.powToDb(b[best]);
  assert.ok(db > -3 && db < 1, `${db} dBFS`);
  // the last n samples of a longer buffer are used (the analyser's buffer is 16384)
  const long = new Float32Array(2 * n); long.set(x, n);
  const p2 = R.hannPower(long, fft, win, { re: new Float64Array(n), im: new Float64Array(n) });
  near(p2[Math.round(f0 * n / rate)], p[Math.round(f0 * n / rate)], 1e-9);
});

test("the sliding window averages power over the last W seconds only", () => {
  const w = new R.Window(20);
  w.push(0, [1, 1]); w.push(5, [3, 1]); w.push(12, [5, 1]);
  const m20 = w.mean(12, 20);
  assert.equal(m20.count, 3); near(m20.mean[0], 3, 1e-12); near(m20.span, 12, 1e-12);
  const m10 = w.mean(12, 10);
  assert.equal(m10.count, 2); near(m10.mean[0], 4, 1e-12);
  w.clear(); assert.equal(w.mean(12, 20), null);
  for (let t = 0; t < 60; t += 0.5) w.push(t, [1]);
  assert.ok(w.frames.length <= 44, `old frames dropped: ${w.frames.length}`);
});

test("response = mic minus program, 0 dB median over 200 Hz-5 kHz, gaps where the program is too low", () => {
  const g = R.engineGrid(10, 20000, 133);
  const prog = Float64Array.from(g.f, (f) => R.dbToPow(f > 12000 ? -95 : -30 - 3 * Math.log2(f / 1000)));
  const mic = Float64Array.from(g.f, (f, i) => prog[i] * R.dbToPow(-17 + (Math.abs(Math.log2(f / 2500)) < 0.05 ? 9 : 0)));
  const r = R.response(mic, prog, g.f);
  assert.equal(r.quiet, false);
  near(r.offset, -17, 1e-9);
  const i25 = g.f.findIndex((f) => Math.abs(Math.log2(f / 2500)) < 0.05);
  near(r.db[i25], 9, 1e-9, "the bump");
  near(r.db[g.f.findIndex((f) => f > 1000)], 0, 1e-9, "flat elsewhere");
  assert.ok(g.f.every((f, i) => f <= 12000 || Number.isNaN(r.db[i])), "no program, no response");
  // the program too low overall: quiet, nothing drawn
  const silent = Float64Array.from(g.f, () => R.dbToPow(-120));
  const q = R.response(mic, silent, g.f);
  assert.equal(q.quiet, true); assert.ok(Array.from(q.db).every(Number.isNaN));
  // smoothing in dB keeps the gaps
  const sm = R.smoothDb(r.db, 2);
  assert.ok(Number.isNaN(sm[132]) && Number.isFinite(sm[60]));
});

test("response smoothing is exactly 1/6 or 1/3 octave on the engine's grid, and keeps gaps", () => {
  const g = R.engineGrid(10, 20000, 133);
  const perOct = 1 / Math.log2(g.ratio);
  near(perOct, 12.04, 0.01, "bins per octave");
  // an impulse shows the weights: 1/6 oct = [0.5, 1, 0.5] / 2, 1/3 oct = [0.5, 1, 1, 1, 0.5] / 4
  const imp = new Float64Array(41).fill(0); imp[20] = 1;
  const s6 = R.smoothDb(imp, 2), s3 = R.smoothDb(imp, 4);
  near(s6[20], 0.5, 1e-12); near(s6[19], 0.25, 1e-12); near(s6[21], 0.25, 1e-12); near(s6[18], 0, 1e-12);
  near(s3[20], 0.25, 1e-12); near(s3[18], 0.125, 1e-12); near(s3[17], 0, 1e-12);
  // the equivalent width is the asked one: a unit step spreads over width bins
  for (const frac of [6, 3]) {
    const w = perOct / frac;
    const out = R.smoothDb(imp, w);
    let sum = 0, peak = 0;
    for (const v of out) { sum += v; peak = Math.max(peak, v); }
    near(sum, 1, 1e-9, "area kept"); near(1 / peak, w, 0.02, `1/${frac} oct width in bins`);
  }
  // 1/12 oct (width ~1 bin) changes nothing that matters; a gap stays a gap, its edges renormalise
  const ramp = Float64Array.from({ length: 30 }, (_, i) => (i === 10 ? NaN : i));
  const r12 = R.smoothDb(ramp, 1);
  assert.ok(Number.isNaN(r12[10]) && r12[5] === 5);
  const r6 = R.smoothDb(ramp, 2);
  assert.ok(Number.isNaN(r6[10])); near(r6[9], (0.5 * 8 + 9) / 1.5, 1e-12, "edge renormalised"); near(r6[20], 20, 1e-12, "a ramp stays a ramp");
});

test("the RTA display shows band power: pink noise (-3 dB/oct density) reads flat", () => {
  const f = R.logPoints(20, 20000, 48);
  const density = Float64Array.from(f, (x) => -40 - 10 * Math.log10(x / 1000));
  const band = R.bandDb(density, f, 1000);
  for (let i = 0; i < f.length; i++) near(band[i], -40, 1e-9, `${f[i].toFixed(0)} Hz`);
  // and through the real smoother: pink power density -> flat band power within 0.3 dB, 40 Hz-16 kHz
  const binHz = 48000 / 16384, n = 8192;
  const pow = Float64Array.from({ length: n }, (_, k) => (k ? 1 / (k * binHz) : 0));
  const sm = R.toDb(R.smoothFracOct(pow, binHz, f, 12));
  const b = R.bandDb(sm, f, 1000);
  const ref = b[f.findIndex((x) => x >= 1000)];
  f.forEach((x, i) => { if (x >= 40 && x <= 16000) near(b[i], ref, 0.3, `${x.toFixed(0)} Hz`); });
});

test("matched estimators: mic minus program is flat on noise, though the peak estimator is biased", (t) => {
  // The engine reports the PEAK FFT bin per band. On noise that reads high, more so in wide bands
  // (high frequencies): an energy estimator on the mic side would show a false treble rise. The
  // phone uses the same estimator on the same grid, so the bias cancels. Independent noise for
  // the mic and the program, as in the room (different time slices of the same music).
  const n = 8192, rate = 48000, frames = 80;
  const fft = new R.FFT(n), win = R.hann(n), sc = { re: new Float64Array(n), im: new Float64Array(n) };
  const g = R.engineGrid(10, 20000, 133), tab = R.bandTable(g, rate / n, n / 2 + 1);
  const bell = (f) => 6 * Math.exp(-0.5 * Math.pow(Math.log2(f / 2500) / 0.08, 2));   // the "room": +6 dB at 2.5 kHz
  const rm = rng(7), rp = rng(8);
  const micW = new R.Window(100), progW = new R.Window(100), micEnergy = new R.Window(100);
  for (let k = 0; k < frames; k++) {
    const pm = R.hannPower(noiseFrame(n, rate, rm, bell), fft, win, sc);
    micW.push(k, R.bandPeaks(pm, tab));
    const e = new Float64Array(g.n);                     // the band ENERGY (mean of its FFT bins)
    for (let i = 0; i < g.n; i++) { let s = 0; for (let j = tab.lo[i]; j <= tab.hi[i]; j++) s += pm[j]; e[i] = s / (tab.hi[i] - tab.lo[i] + 1); }
    micEnergy.push(k, e);
    progW.push(k, R.bandPeaks(R.hannPower(noiseFrame(n, rate, rp), fft, win, sc), tab));
  }
  const r = R.response(micW.mean(frames, 1000).mean, progW.mean(frames, 1000).mean, g.f);
  const at = (f) => r.db[g.f.findIndex((x) => x >= f)];
  t.diagnostic("matched: " + [300, 600, 1000, 2500, 6000, 10000].map((f) => f + " Hz " + at(f).toFixed(2)).join(", "));
  for (const f of [300, 600, 1000, 6000, 10000]) assert.ok(Math.abs(at(f)) <= 1.5, `flat at ${f} Hz: ${at(f).toFixed(2)} dB`);
  assert.ok(at(2500) >= 4.5 && at(2500) <= 7.5, `the +6 dB bell at 2.5 kHz reads ${at(2500).toFixed(2)} dB`);
  // ...and the mismatched pair (energy on the mic, peak on the program) tilts by several dB
  const bad = R.response(micEnergy.mean(frames, 1000).mean, progW.mean(frames, 1000).mean, g.f);
  const bi = (f) => bad.db[g.f.findIndex((x) => x >= f)];
  assert.ok(bi(300) - bi(10000) > 3, `mismatched estimators tilt: ${(bi(300) - bi(10000)).toFixed(1)} dB`);
});

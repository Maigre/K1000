// SPDX-License-Identifier: GPL-3.0-or-later
/* The phone-mic maths (DESIGN §7, docs/11-phone-mic.md): no DOM, no Web Audio, no network, so it
   runs the same under node (box/tools/v2lab/micunit.mjs, and the fake box builds its program
   windows with it) as in the page. Ported from the night/phone-rta branch's rta.js (its 18 tests
   ported to micunit.mjs), plus the box's PEAK rule and the RESPONSE windows of protocol 2.

   Three families:
   - The mic line (RTA): power-domain averaging (FAST 125 ms / SLOW 1 s),
     fractional-octave smoothing shown as BAND power (pink noise reads flat), a slow level tracker
     (the mic is uncalibrated: the line is placed, not measured).
   - The mic PEAK: the box's own rule (k1000/rta.py Ballistics._track_peak), in its own instance:
     a bin ENTER_DB above the median of the ±1 octave around it on a 0.25 s power average, within
     LOUD_DB of the loudest, persistent PERSIST_S; kept while EXIT_DB above, released RELEASE_S
     after; the pointer moves only by MOVE_BINS; no new PEAK while the program gate is shut.
     Never a raw argmax, which jumps with the music.
   - RESPONSE ("walk the room"): the engine's own spectrum estimator, re-implemented exactly
     (CamillaDSP v5 spectrum.rs: Hann window, FFT length = next power of 2 >= rate / min_freq,
     log-spaced bins whose edges are the geometric midpoints, each bin = the PEAK power of the FFT
     bins it spans). The box sends its program spectrum computed that way, averaged in power over
     0.5 s windows (`pgm` frames, docs/14); the phone computes its mic spectrum the same way on
     the same grid, so the estimator's bias (several dB on noise-like signals in wide bands)
     cancels in mic minus program. Both are averaged in power over 10 or 20 s of windows. */

export const FLOOR_DB = -200;
const DB = Math.LN10 / 10;

export function dbToPow(db) { return Number.isFinite(db) ? Math.exp(db * DB) : 0; }
export function powToDb(p) { return p > 0 ? Math.max(FLOOR_DB, 10 * Math.log10(p)) : FLOOR_DB; }

/* Float32Array of dB (AnalyserNode.getFloatFrequencyData; -Infinity for silence) -> linear power. */
export function fromDb(db, out) {
  out = out || new Float64Array(db.length);
  for (let i = 0; i < db.length; i++) out[i] = dbToPow(db[i]);
  return out;
}
export function toDb(pow, out) {
  out = out || new Float64Array(pow.length);
  for (let i = 0; i < pow.length; i++) out[i] = powToDb(pow[i]);
  return out;
}

/* Log-spaced display points: fLo * 2^(i / perOct) up to fHi. */
export function logPoints(fLo, fHi, perOct) {
  const n = Math.floor(Math.log2(fHi / fLo) * perOct + 1e-9) + 1;
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++) out[i] = fLo * Math.pow(2, i / perOct);
  return out;
}

/* Exponential averaging in the POWER domain (never in dB: the average of a ring and silence
   is half the ring's power, -3 dB, not half its dB). The first frame is taken as is. FAST and
   SLOW run side by side: the line shows one (PGM ▾ › FAST / SLOW). */
export const FAST_TAU = 0.125, SLOW_TAU = 1.0;
export class Ballistics {
  constructor(n) { this.alloc(n); this.primed = false; }
  alloc(n) { this.fast = new Float64Array(n); this.slow = new Float64Array(n); }
  reset() { this.primed = false; }
  update(pow, dt) {
    if (!this.primed || pow.length !== this.fast.length) {
      if (pow.length !== this.fast.length) this.alloc(pow.length);
      this.fast.set(pow); this.slow.set(pow); this.primed = true; return this;
    }
    const af = 1 - Math.exp(-dt / FAST_TAU), as = 1 - Math.exp(-dt / SLOW_TAU);
    const f = this.fast, s = this.slow;
    for (let i = 0; i < pow.length; i++) { const p = pow[i]; f[i] += af * (p - f[i]); s[i] += as * (p - s[i]); }
    return this;
  }
}

/* Fractional-octave smoothing: at each centre frequency, the mean power of the FFT bins within
   +-1/(2*frac) octave (frac = 12 -> 1/12 oct). Bin k is centred on k*binHz and spans half a bin
   each side; partial bins count pro rata, and the window is never narrower than one bin (at the
   bottom, where 1/12 oct is less than a bin, this interpolates between bins instead of drawing
   steps). Prefix sums: O(points), whatever the width. Returns linear power. */
let cumBuf = null;
export function smoothFracOct(pow, binHz, centres, frac, out) {
  const n = pow.length;
  out = out || new Float64Array(centres.length);
  const cum = cumBuf && cumBuf.length === n + 1 ? cumBuf : (cumBuf = new Float64Array(n + 1));
  cum[0] = 0;
  for (let k = 0; k < n; k++) cum[k + 1] = cum[k] + pow[k];
  const integ = (u) => {          // integral of the step function from u = 0 (bin 0's lower edge)
    if (u <= 0) return 0;
    if (u >= n) return cum[n];
    const k = Math.floor(u);
    return cum[k] + (u - k) * pow[k];
  };
  const half = Math.pow(2, 1 / (2 * frac));
  for (let i = 0; i < centres.length; i++) {
    const f = centres[i];
    let ua = f / half / binHz + 0.5, ub = f * half / binHz + 0.5;
    if (ub - ua < 1) { const c = (ua + ub) / 2; ua = c - 0.5; ub = c + 0.5; }
    ua = Math.max(0, ua); ub = Math.min(n, ub);
    out[i] = ub > ua ? (integ(ub) - integ(ua)) / (ub - ua) : 0;
  }
  return out;
}

export function peakHold(hold, cur) {
  for (let i = 0; i < cur.length; i++) if (!(hold[i] >= cur[i])) hold[i] = cur[i];
  return hold;
}

/* Where the uncalibrated line sits: rises fast (tau ~0.15 s) when the music gets louder, falls
   at most ~1 dB/s when it gets quieter, so the line never jumps and never zooms. */
export class LevelTracker {
  constructor(opts = {}) { this.riseTau = opts.riseTau || 0.15; this.fall = opts.fallDbPerS || 1; this.value = null; }
  reset() { this.value = null; }
  update(level, dt) {
    if (!Number.isFinite(level)) return this.value;
    if (this.value === null) this.value = level;
    else if (level > this.value) this.value += (level - this.value) * (1 - Math.exp(-dt / this.riseTau));
    else this.value = Math.max(level, this.value - this.fall * dt);
    return this.value;
  }
}

export function median(values) {
  const a = Array.from(values).filter(Number.isFinite).sort((x, y) => x - y);
  if (!a.length) return NaN;
  const m = a.length >> 1;
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
}

/* The RTA display shows BAND power, as a 1/n-octave RTA does: smoothFracOct gives the mean
   power density over a window as wide as a fixed fraction of an octave, i.e. proportional to f,
   so band power = density x f. In dB: + 10 log10(f / refHz). Pink noise (and most music) then
   reads flat instead of falling 3 dB per octave, and a line drawn at the EQ grid's own dB per
   division fits in it. */
export function bandDb(db, freqs, refHz, out) {
  const ref = refHz || 1000;
  out = out || new Float64Array(db.length);
  for (let i = 0; i < db.length; i++) out[i] = db[i] + 10 * Math.log10(freqs[i] / ref);
  return out;
}

// ------------------------------------------------------------------ the PEAK: the box's rule
/* k1000/rta.py Ballistics' PEAK, line for line, in its own instance (DESIGN §7): update() takes
   one frame of dB on a log grid (the mic's 1/24-octave density), averages it in power over
   avgTauS, and tracks the peak. `program`: true = present, false = absent (no new PEAK, a shown
   one released after RELEASE_S), null = unknown (the level floor alone gates). The search range
   is the phone's: 100 Hz-12 kHz (DESIGN §7), not the box's 40 Hz-16 kHz. */
export class PeakRule {
  constructor(opts = {}) {
    this.ENTER_DB = 10; this.EXIT_DB = 6; this.LOUD_DB = 30; this.PERSIST_S = 1.5; this.RELEASE_S = 0.5;
    this.CONTEXT_OCT = 1; this.TRACK_BINS = 2; this.MOVE_BINS = 2; this.FLOOR_EXIT_DB = 4;
    this.search = opts.search || [100, 12000];
    this.floorDb = opts.floorDb == null ? -140 : opts.floorDb;
    this.avgTauS = opts.avgTauS == null ? 0.25 : opts.avgTauS;
    this.reset();
  }
  reset() {
    this.freqs = null; this.avg = null; this.t = null;
    this.cand = null; this.peak = null; this.shown = null; this.lost = null;
    this.program = null; this.fine = null;
  }
  // the pointer's frequency: its bin refined by a parabola through the bin and its neighbours
  // (a 1 kHz tone between two 1/24 oct bins reads 1.00k, not 990), fixed while the pointer stays
  get peakHz() { return this.shown == null || !this.freqs ? null : this.fine; }
  setShown(j) {
    this.shown = j;
    const f = this.freqs, n = f.length, db = (i) => 10 * Math.log10(Math.max(this.avg[i], 1e-30));
    let d = 0;
    if (j > 0 && j < n - 1) {
      const a = db(j - 1), b = db(j), c = db(j + 1), den = a - 2 * b + c;
      if (den < 0) d = Math.max(-0.5, Math.min(0.5, 0.5 * (a - c) / den));
    }
    this.fine = n > 1 ? f[j] * Math.pow(f[n - 1] / f[0], d / (n - 1)) : f[j];
  }
  update(freqs, mags, now, program = null) {
    this.program = program;
    const n = mags.length;
    if (!this.freqs || this.freqs.length !== n || this.freqs[0] !== freqs[0] || this.freqs[n - 1] !== freqs[n - 1]) {
      this.reset();
      this.freqs = Float64Array.from(freqs);
      this.avg = new Float64Array(n);
      for (let i = 0; i < n; i++) this.avg[i] = Math.pow(10, Math.max(mags[i], -140) / 10);
      this.t = now;
    }
    const dt = Math.max(0, now - (this.t == null ? now : this.t));
    this.t = now;
    const a = this.avgTauS > 0 ? Math.exp(-dt / this.avgTauS) : 0;
    for (let i = 0; i < n; i++) this.avg[i] = a * this.avg[i] + (1 - a) * Math.pow(10, Math.max(mags[i], -140) / 10);
    const m = new Float64Array(n);
    for (let i = 0; i < n; i++) m[i] = 10 * Math.log10(Math.max(this.avg[i], 1e-30));
    this.track(m, now);
    return this.peakHz;
  }
  track(m, now) {
    const f = this.freqs, n = m.length;
    const perOct = n > 1 ? (n - 1) / Math.max(1e-9, Math.log2(f[n - 1] / f[0])) : 1;
    const half = Math.max(2, Math.round(perOct * this.CONTEXT_OCT));
    // the median of the ±1 octave around each bin, the ends padded with the edge value (np.pad edge)
    const prom = new Float64Array(n), win = new Float64Array(2 * half + 1);
    for (let i = 0; i < n; i++) {
      for (let j = -half; j <= half; j++) win[j + half] = m[Math.min(n - 1, Math.max(0, i + j))];
      win.sort();
      prom[i] = m[i] - win[half];
    }
    const inside = (i) => f[i] >= this.search[0] && f[i] <= this.search[1];
    let loudest = -Infinity, anyIn = false;
    for (let i = 0; i < n; i++) if (inside(i)) { anyIn = true; if (m[i] > loudest) loudest = m[i]; }
    if (!anyIn) { this.cand = this.peak = this.shown = this.lost = null; return; }
    const loud = (i) => m[i] >= loudest - this.LOUD_DB;
    const heard = this.program !== false;
    if (this.peak != null) {
      const lo = Math.max(0, this.peak - this.TRACK_BINS), hi = Math.min(n, this.peak + this.TRACK_BINS + 1);
      let j = lo;
      for (let k = lo; k < hi; k++) if (prom[k] > prom[j]) j = k;
      if (heard && prom[j] >= this.EXIT_DB && loud(j) && inside(j) && m[j] >= this.floorDb - this.FLOOR_EXIT_DB) {
        this.peak = j; this.lost = null;
        if (this.shown == null || Math.abs(j - this.shown) >= this.MOVE_BINS) this.setShown(j);
        return;
      }
      if (this.lost == null) this.lost = now;
      if (now - this.lost < this.RELEASE_S) return;
      this.peak = this.shown = this.lost = null;
      this.cand = null;
    }
    let best = -1;
    for (let i = 0; i < n; i++) {
      if (!(inside(i) && loud(i) && prom[i] >= this.ENTER_DB && m[i] >= this.floorDb)) continue;
      if (best < 0 || prom[i] > prom[best]) best = i;
    }
    if (!heard || best < 0) { this.cand = null; return; }
    const since = this.cand && Math.abs(this.cand[0] - best) <= this.TRACK_BINS ? this.cand[1] : now;
    this.cand = [best, since];
    if (now - since >= this.PERSIST_S) { this.peak = best; this.setShown(best); this.cand = null; this.lost = null; }
  }
}

/* The PEAK's program gate on the phone: the program present and not muted (DESIGN §7). The box's
   own gate is its capture RMS at -60 dBFS (rta.py PROGRAM_FLOOR_DB, 3 dB of hysteresis); on LIVE
   the page has the meters' input PEAKS (m.i, before any K1000 gain), about 10 dB over the RMS on
   music: present at -50 dBFS, kept down to -53. `pgmProg` (a fresh pgm window's own `prog`,
   RESPONSE only) wins when it is a boolean. Returns true / false / null (unknown). */
export const PROG_PEAK_DB = -50, PROG_HYST_DB = 3;
export function programGate(was, inPeaks, muted, pgmProg) {
  if (muted === true) return false;
  if (pgmProg === true || pgmProg === false) return pgmProg;
  if (!Array.isArray(inPeaks) || !inPeaks.length) return null;
  let top = -Infinity;
  for (const v of inPeaks) if (typeof v === "number" && Number.isFinite(v) && v > top) top = v;
  if (!Number.isFinite(top)) return null;
  return top >= PROG_PEAK_DB - (was === true ? PROG_HYST_DB : 0);
}

// ------------------------------------------------------------------ the engine's estimator
/* In-place iterative radix-2 complex FFT (n a power of two). */
export class FFT {
  constructor(n) {
    if (n < 2 || (n & (n - 1))) throw new Error("FFT size must be a power of two");
    this.n = n;
    this.rev = new Uint32Array(n);
    const bits = Math.log2(n);
    for (let i = 0; i < n; i++) { let r = 0; for (let b = 0; b < bits; b++) r |= ((i >> b) & 1) << (bits - 1 - b); this.rev[i] = r; }
    this.cos = new Float64Array(n / 2); this.sin = new Float64Array(n / 2);
    for (let i = 0; i < n / 2; i++) { this.cos[i] = Math.cos(2 * Math.PI * i / n); this.sin[i] = -Math.sin(2 * Math.PI * i / n); }
  }
  transform(re, im) {
    const n = this.n, rev = this.rev;
    for (let i = 0; i < n; i++) { const j = rev[i]; if (j > i) { let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; } }
    for (let size = 2; size <= n; size <<= 1) {
      const half = size >> 1, step = n / size;
      for (let s = 0; s < n; s += size) {
        for (let j = 0, k = 0; j < half; j++, k += step) {
          const a = s + j, b = a + half;
          const wr = this.cos[k], wi = this.sin[k];
          const xr = re[b] * wr - im[b] * wi, xi = re[b] * wi + im[b] * wr;
          re[b] = re[a] - xr; im[b] = im[a] - xi; re[a] += xr; im[a] += xi;
        }
      }
    }
  }
}

/* spectrum.rs fft_length_for: smallest power of two >= rate / minFreq (capped like the engine). */
export function engineFftLen(rate, minFreq) {
  const need = Math.ceil(rate / minFreq);
  let n = 1;
  while (n < need) n <<= 1;
  return Math.min(n, 262144);
}

/* spectrum.rs get_hann_window: 0.5 * (1 - cos(2 pi i / (n - 1))). */
export function hann(n) {
  const w = new Float64Array(n);
  for (let i = 0; i < n; i++) w[i] = 0.5 * (1 - Math.cos(2 * Math.PI * i / (n - 1)));
  return w;
}

/* One-sided power spectrum of the last fft.n samples, normalised as the engine does it
   (0 dBFS = a full-scale sine): 4 |X|^2 / (sum w)^2, DC and Nyquist without the 4.
   `scratch` = {re, im} Float64Arrays of fft.n, reused. Returns Float64Array(n / 2 + 1). */
export function hannPower(x, fft, win, scratch, out) {
  const n = fft.n;
  const re = scratch.re, im = scratch.im;
  const off = x.length - n;
  let wsum = scratch.wsum;
  if (!wsum) { wsum = 0; for (let i = 0; i < n; i++) wsum += win[i]; scratch.wsum = wsum; }
  for (let i = 0; i < n; i++) { re[i] = x[off + i] * win[i]; im[i] = 0; }
  fft.transform(re, im);
  const m = n / 2 + 1;
  out = out || new Float64Array(m);
  const inv = 1 / (wsum * wsum);
  for (let k = 0; k < m; k++) out[k] = (k === 0 || k === m - 1 ? 1 : 4) * (re[k] * re[k] + im[k] * im[k]) * inv;
  return out;
}

/* The engine's log grid (min, max, nBins) and, for one FFT resolution, which FFT bins each
   output bin takes its peak from (spectrum.rs compute_spectrum_from_signal). */
export function engineGrid(min, max, nBins) {
  const r = Math.pow(max / min, 1 / (nBins - 1));
  const f = new Float64Array(nBins);
  for (let i = 0; i < nBins; i++) f[i] = min * Math.pow(r, i);
  return { min, max, n: nBins, ratio: r, f };
}
export function bandTable(grid, binHz, nFft) {
  const sq = Math.sqrt(grid.ratio);
  const lo = new Int32Array(grid.n), hi = new Int32Array(grid.n);
  for (let i = 0; i < grid.n; i++) {
    const fc = grid.f[i];
    const fl = i === 0 ? grid.min : fc / sq, fh = i === grid.n - 1 ? grid.max : fc * sq;
    const kl = Math.floor(fl / binHz), kh = Math.min(Math.ceil(fh / binHz), nFft - 1);
    if (kl <= kh) { lo[i] = kl; hi[i] = kh; } else { const k = Math.min(Math.round(fc / binHz), nFft - 1); lo[i] = k; hi[i] = k; }
  }
  return { lo, hi, binHz, nFft, n: grid.n };
}
export function bandPeaks(power, table, out) {
  out = out || new Float64Array(table.n);
  for (let i = 0; i < table.n; i++) {
    let m = 0;
    for (let k = table.lo[i]; k <= table.hi[i]; k++) if (power[k] > m) m = power[k];
    out[i] = m;
  }
  return out;
}

/* A whole analyser for one rate and one grid: samples in, the engine's peak-per-band POWER out
   (the same numbers GetSpectrum gives, before its dB). */
export class EngineEstimator {
  constructor(rate, f0, f1, nBins) {
    this.rate = rate; this.n = engineFftLen(rate, f0);
    this.grid = engineGrid(f0, f1, nBins);
    this.fft = new FFT(this.n); this.win = hann(this.n);
    this.scratch = { re: new Float64Array(this.n), im: new Float64Array(this.n) };
    this.pw = new Float64Array(this.n / 2 + 1);
    this.table = bandTable(this.grid, rate / this.n, this.n / 2 + 1);
  }
  matches(rate, f0, f1, nBins) { return rate === this.rate && f0 === this.grid.min && f1 === this.grid.max && nBins === this.grid.n; }
  // null when x holds fewer than n samples (a rate above 163.84 kHz on the page's 16384-sample
  // analyser): never a spectrum of NaN
  bands(x, out) { return x.length < this.n ? null : bandPeaks(hannPower(x, this.fft, this.win, this.scratch, this.pw), this.table, out); }
}

/* The largest |sample| of the last n samples (all of x when n is not given): a clipped mic block
   reads ~1.0 (CLIP_ABS). */
export const CLIP_ABS = 0.98;
export function peakAbs(x, n) {
  let m = 0;
  for (let i = Math.max(0, x.length - (n || x.length)); i < x.length; i++) { const v = x[i] < 0 ? -x[i] : x[i]; if (v > m) m = v; }
  return m;
}

// ------------------------------------------------------------------ RESPONSE
/* A `pgm` frame (docs/14 §3.6): `m` = base64 of n little-endian int16 = round(dBFS * 100).
   -> {st, seq, n, f0, f1, k, dt, prog, db: Float64Array | null}. `atobFn` decodes base64 to a
   binary string (window.atob; node has one too). Anything odd: st kept, db null. */
export function decodePgm(m, atobFn) {
  const st = typeof m.st === "string" ? m.st : "off";
  const out = { st, seq: Number.isInteger(m.seq) ? m.seq : null, n: 0, f0: null, f1: null, k: Number.isInteger(m.k) ? m.k : 0,
    dt: typeof m.dt === "number" && Number.isFinite(m.dt) ? m.dt : 0, prog: m.prog === true ? true : m.prog === false ? false : null, db: null };
  const n = m.n | 0;
  if (st !== "live" || typeof m.m !== "string" || n < 2 || n > 4096 || !(m.f0 > 0) || !(m.f1 > m.f0)) return out;
  let bin;
  try { bin = (atobFn || atob)(m.m); } catch (e) { return out; }
  if (bin.length < 2 * n) return out;
  const db = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
    if (v >= 32768) v -= 65536;
    db[i] = v / 100;
  }
  return Object.assign(out, { n, f0: m.f0, f1: m.f1, db });
}
/* The other way (the fake box): dB -> the frame's `m` (round(dB * 100), clamped to int16). */
export function encodePgm(db, btoaFn) {
  let s = "";
  for (let i = 0; i < db.length; i++) {
    let v = Math.round((Number.isFinite(db[i]) ? db[i] : -327.68) * 100);
    v = Math.max(-32768, Math.min(32767, v));
    if (v < 0) v += 65536;
    s += String.fromCharCode(v & 255, v >> 8);
  }
  return (btoaFn || btoa)(s);
}

/* The phone's half of one RESPONSE window: the engine estimator's band powers of each mic tick,
   summed in power until the box's program window closes. Each tick is divided by the master's
   gain at that moment (gainDb = state.master_db): the box's program is taken BEFORE the master,
   the mic hears the PA AFTER it, so without this a master move during an average would weigh
   the windows before and after it differently, and a change in the music's balance between them
   would show as a false tilt (the 0 dB median only absorbs a gain common to every window).
   `bad` = why this half may not be kept ("" = fine). */
export class MicHalf {
  constructor(n, f0, f1, t0) { this.sum = new Float64Array(n); this.k = 0; this.n = n; this.f0 = f0; this.f1 = f1; this.t0 = t0 || 0; this.bad = ""; }
  add(bandsPow, gainDb) {
    const g = dbToPow(Number.isFinite(gainDb) ? gainDb : 0) || 1;
    for (let i = 0; i < this.n; i++) this.sum[i] += bandsPow[i] / g;
    this.k++;
  }
  mean() { const out = new Float64Array(this.n); for (let i = 0; i < this.n; i++) out[i] = this.k ? this.sum[i] / this.k : 0; return out; }
  // the same grid as a pgm frame's (f0 / f1 compared within 1e-6, relative: never silently
  // skipped for a float's last digit)
  sameGrid(n, f0, f1) { return n === this.n && relEq(f0, this.f0) && relEq(f1, this.f1); }
}
export const relEq = (a, b, tol = 1e-6) => typeof a === "number" && typeof b === "number" && Math.abs(a - b) <= tol * Math.max(Math.abs(a), Math.abs(b));

/* The RESPONSE average: pairs of windows (the box's program window and the phone's mic window
   over the same half second), kept only when both were taken while nothing paused it. mean()
   power-averages the newest windows that cover `spanS` seconds (10 or 20): a pause (mute, no
   program) holds the average, it does not empty it; windows older than maxAgeS are dropped (a
   long pause is a new measurement). */
export class RespAvg {
  constructor(maxAgeS = 60) { this.maxAgeS = maxAgeS; this.wins = []; }
  clear() { this.wins.length = 0; }
  add(t, mic, pgm, dt) {
    if (!mic || !pgm || mic.length !== pgm.length) return false;
    if (this.wins.length && this.wins[0].mic.length !== mic.length) this.clear();
    this.wins.push({ t, mic: Float64Array.from(mic), pgm: Float64Array.from(pgm), dt: dt > 0 ? dt : 0.5 });
    while (this.wins.length > 400) this.wins.shift();
    return true;
  }
  // the windows in use now (newest first, up to spanS of coverage, none older than maxAgeS)
  take(now, spanS) {
    const out = [];
    let cover = 0;
    for (let i = this.wins.length - 1; i >= 0 && cover < spanS - 1e-9; i--) {
      const w = this.wins[i];
      if (now - w.t > this.maxAgeS) break;
      out.push(w); cover += w.dt;
    }
    return { wins: out, cover: Math.min(cover, spanS) };
  }
  coverage(now, spanS) { return this.take(now, spanS).cover; }
  mean(now, spanS) {
    const { wins, cover } = this.take(now, spanS);
    if (!wins.length) return null;
    const n = wins[0].mic.length, mic = new Float64Array(n), pgm = new Float64Array(n);
    for (const w of wins) for (let i = 0; i < n; i++) { mic[i] += w.mic[i]; pgm[i] += w.pgm[i]; }
    for (let i = 0; i < n; i++) { mic[i] /= wins.length; pgm[i] /= wins.length; }
    return { mic, pgm, count: wins.length, cover };
  }
}

/* mic minus program, per bin, in dB. A bin counts only where the program has something to
   compare: no quieter than relDb under the program's own refLo-refHi median, above floorDb, and
   from minHz to maxHz (the page: 100 Hz-12 kHz, DESIGN §7: an uncalibrated phone mic says nothing
   useful below, and rolls off above). The result is shifted so
   that its median over refLo-refHi is 0 dB (the mic is uncalibrated and the master sits between
   the two). quiet = the program is too low to say anything. */
export function response(micPow, progPow, freqs, opts = {}) {
  const refLo = opts.refLo || 200, refHi = opts.refHi || 5000, minHz = opts.minHz == null ? 100 : opts.minHz;
  const maxHz = opts.maxHz == null ? Infinity : opts.maxHz;
  const relDb = opts.relDb === undefined ? -30 : opts.relDb;
  const floorDb = opts.floorDb === undefined ? -110 : opts.floorDb;
  const quietDb = opts.quietDb === undefined ? -80 : opts.quietDb;
  const n = freqs.length;
  const prog = toDb(progPow), micDb = toDb(micPow);
  const ref = [];
  for (let i = 0; i < n; i++) if (freqs[i] >= refLo && freqs[i] <= refHi) ref.push(prog[i]);
  const progRef = median(ref);
  const db = new Float64Array(n).fill(NaN);
  if (!Number.isFinite(progRef) || progRef < quietDb) return { db, offset: 0, progRef, quiet: true, valid: 0 };
  const inRef = [];
  let valid = 0;
  for (let i = 0; i < n; i++) {
    if (freqs[i] < minHz || freqs[i] > maxHz || prog[i] < progRef + relDb || prog[i] < floorDb || micDb[i] <= FLOOR_DB) continue;
    db[i] = micDb[i] - prog[i];
    valid++;
    if (freqs[i] >= refLo && freqs[i] <= refHi) inRef.push(db[i]);
  }
  if (inRef.length < 5) return { db: db.fill(NaN), offset: 0, progRef, quiet: true, valid: 0 };
  const offset = median(inRef);
  for (let i = 0; i < n; i++) if (Number.isFinite(db[i])) db[i] -= offset;
  return { db, offset, progRef, quiet: false, valid };
}

/* Moving average in dB over a window `width` bins wide, centred, skipping gaps (a gap stays a
   gap). The width may be fractional: the end bins count pro rata, so on a 24-bins-per-octave
   grid width = 4 is exactly 1/6 octave ([0.5, 1, 1, 1, 0.5] / 4). width <= 1: unchanged. */
export function smoothDb(db, width) {
  if (!(width > 1)) return Float64Array.from(db);
  const half = width / 2, reach = Math.ceil(half - 0.5);
  const w = [];
  for (let j = 0; j <= reach; j++) w.push(Math.max(0, Math.min(j + 0.5, half) - Math.max(j - 0.5, -half)));
  const out = new Float64Array(db.length).fill(NaN);
  for (let i = 0; i < db.length; i++) {
    if (!Number.isFinite(db[i])) continue;
    let s = 0, c = 0;
    for (let j = -reach; j <= reach; j++) {
      const k = i + j, wj = w[Math.abs(j)];
      if (k < 0 || k >= db.length || !wj || !Number.isFinite(db[k])) continue;
      s += wj * db[k]; c += wj;
    }
    out[i] = s / c;
  }
  return out;
}

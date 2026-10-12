// SPDX-License-Identifier: GPL-3.0-or-later
/* K1000 phone-mic analysis (Phase 4, docs/11-phone-mic.md): the math only. No DOM, no Web Audio,
   no network, so it runs the same under `node --test` (box/tests/js/) as in the page, where it is
   window.K1000RTA. app.js feeds it the AnalyserNode's data and draws what comes out.

   Two families:
   - The RTA (step 1): power-domain ballistics (FAST/SLOW), fractional-octave smoothing for the
     display (shown as band power: pink reads flat), peak hold, a slow level tracker (the mic is uncalibrated: the trace is placed, not
     measured), and a narrow-peak detector with persistence.
   - The response view (step 2): the engine's own spectrum estimator, re-implemented here exactly
     (CamillaDSP v5 spectrum.rs: Hann window, FFT length = next power of 2 >= rate / min_freq,
     log-spaced bins whose edges are the geometric midpoints, each bin = the PEAK power of the FFT
     bins it spans). The box sends the program spectrum computed that way; the phone computes its
     mic spectrum the same way on the same grid, so the estimator's bias (several dB on noise-like
     signals in wide bands) cancels in mic minus program. Both are averaged in power over a
     sliding 10/20 s window: long enough that time alignment does not matter. */
"use strict";
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.K1000RTA = api;
})(typeof self !== "undefined" ? self : this, function () {
  const DB = Math.LN10 / 10;
  const FLOOR_DB = -200;

  function dbToPow(db) { return Number.isFinite(db) ? Math.exp(db * DB) : 0; }
  function powToDb(p) { return p > 0 ? Math.max(FLOOR_DB, 10 * Math.log10(p)) : FLOOR_DB; }

  /* Float32Array of dB (AnalyserNode.getFloatFrequencyData; -Infinity for silence) -> linear power. */
  function fromDb(db, out) {
    out = out || new Float64Array(db.length);
    for (let i = 0; i < db.length; i++) out[i] = dbToPow(db[i]);
    return out;
  }
  function toDb(pow, out) {
    out = out || new Float64Array(pow.length);
    for (let i = 0; i < pow.length; i++) out[i] = powToDb(pow[i]);
    return out;
  }

  /* Log-spaced display points: fLo * 2^(i / perOct) up to fHi. */
  function logPoints(fLo, fHi, perOct) {
    const n = Math.floor(Math.log2(fHi / fLo) * perOct + 1e-9) + 1;
    const out = new Float64Array(n);
    for (let i = 0; i < n; i++) out[i] = fLo * Math.pow(2, i / perOct);
    return out;
  }

  /* Exponential averaging in the POWER domain (never in dB: the average of a ring and silence
     is half the ring's power, -3 dB, not half its dB). tau in seconds; the first frame is
     taken as is. FAST and SLOW run side by side: the display shows one, the detector uses SLOW. */
  const FAST_TAU = 0.125, SLOW_TAU = 1.0;
  class Ballistics {
    constructor(n) { this.fast = new Float64Array(n); this.slow = new Float64Array(n); this.primed = false; }
    reset() { this.primed = false; }
    update(pow, dt) {
      if (!this.primed || pow.length !== this.fast.length) {
        if (pow.length !== this.fast.length) { this.fast = new Float64Array(pow.length); this.slow = new Float64Array(pow.length); }
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
  function smoothFracOct(pow, binHz, centres, frac, out) {
    const n = pow.length;
    out = out || new Float64Array(centres.length);
    const cum = smoothFracOct._cum && smoothFracOct._cum.length === n + 1 ? smoothFracOct._cum : (smoothFracOct._cum = new Float64Array(n + 1));
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

  function peakHold(hold, cur) {
    for (let i = 0; i < cur.length; i++) if (!(hold[i] >= cur[i])) hold[i] = cur[i];
    return hold;
  }

  /* Where the uncalibrated trace sits: rises fast (tau ~0.15 s) when the music gets louder, falls
     at most ~1 dB/s when it gets quieter, so the trace never jumps and never zooms. */
  class LevelTracker {
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

  function median(values) {
    const a = Array.from(values).filter(Number.isFinite).sort((x, y) => x - y);
    if (!a.length) return NaN;
    const m = a.length >> 1;
    return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
  }

  /* The strongest NARROW peak of a dB trace on log points (freqs evenly spaced in log f):
     a local maximum standing >= minProm dB above the median of its +-octSpan octave neighbourhood
     and >= narrowDb above the trace at +-narrowOct on both sides (a broad hump is not a ring).
     The frequency is refined with a parabola through the top three points. Returns
     {f, db, prom, i} or null. */
  function findPeak(db, freqs, opts = {}) {
    const fMin = opts.fMin || 100, fMax = opts.fMax || 16000;
    const minProm = opts.minProm === undefined ? 6 : opts.minProm;
    const octSpan = opts.octSpan || 1, narrowOct = opts.narrowOct || 1 / 6, narrowDb = opts.narrowDb === undefined ? 3 : opts.narrowDb;
    const n = db.length;
    if (n < 5) return null;
    const perOct = 1 / Math.log2(freqs[1] / freqs[0]);
    const span = Math.max(2, Math.round(octSpan * perOct)), nw = Math.max(1, Math.round(narrowOct * perOct));
    let best = null;
    for (let i = 2; i < n - 2; i++) {
      const f = freqs[i];
      if (f < fMin || f > fMax) continue;
      const v = db[i];
      if (!Number.isFinite(v) || v <= FLOOR_DB) continue;
      if (!(v >= db[i - 1] && v > db[i + 1] && v >= db[i - 2] && v > db[i + 2])) continue;
      const lo = i - nw, hi = i + nw;
      if (lo < 0 || hi >= n || !(db[lo] <= v - narrowDb && db[hi] <= v - narrowDb)) continue;
      const med = median(db.subarray ? db.subarray(Math.max(0, i - span), Math.min(n, i + span + 1)) : db.slice(Math.max(0, i - span), i + span + 1));
      const prom = v - med;
      if (prom < minProm) continue;
      if (!best || prom > best.prom) best = { i, prom, db: v };
    }
    if (!best) return null;
    const i = best.i, a = db[i - 1], b = db[i], c = db[i + 1];
    const den = a - 2 * b + c;
    const d = den < 0 ? Math.max(-0.5, Math.min(0.5, 0.5 * (a - c) / den)) : 0;
    best.f = freqs[0] * Math.pow(2, (i + d) / perOct);
    return best;
  }

  /* A detected peak becomes a MARKER only once it has held (within tolOct) for persistS seconds;
     a stray frame elsewhere does not reset it; it goes away releaseS after its last sighting. */
  class PeakTracker {
    constructor(opts = {}) {
      this.persist = opts.persistS === undefined ? 1.5 : opts.persistS;
      this.tol = opts.tolOct || 1 / 12; this.release = opts.releaseS || 0.6;
      this.track = null;
    }
    reset() { this.track = null; }
    update(cand, t) {
      const tr = this.track;
      if (cand) {
        if (tr && Math.abs(Math.log2(cand.f / tr.f)) <= this.tol) {
          tr.f = tr.f * Math.pow(cand.f / tr.f, 0.3); tr.last = t; tr.prom = cand.prom;
        } else if (!tr || t - tr.last > this.release) {
          this.track = { f: cand.f, since: t, last: t, prom: cand.prom };
        }
      }
      if (this.track && t - this.track.last > this.release) this.track = null;
      const k = this.track;
      return k && t - k.since >= this.persist ? { f: k.f, prom: k.prom, age: t - k.since } : null;
    }
  }

  // ------------------------------------------------------------------ the engine's estimator
  /* In-place iterative radix-2 complex FFT (n a power of two). */
  class FFT {
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
  function engineFftLen(rate, minFreq) {
    const need = Math.ceil(rate / minFreq);
    let n = 1;
    while (n < need) n <<= 1;
    return Math.min(n, 262144);
  }

  /* spectrum.rs get_hann_window: 0.5 * (1 - cos(2 pi i / (n - 1))). */
  function hann(n) {
    const w = new Float64Array(n);
    for (let i = 0; i < n; i++) w[i] = 0.5 * (1 - Math.cos(2 * Math.PI * i / (n - 1)));
    return w;
  }

  /* One-sided power spectrum of the last fft.n samples, normalised as the engine does it
     (0 dBFS = a full-scale sine): 4 |X|^2 / (sum w)^2, DC and Nyquist without the 4.
     `scratch` = {re, im} Float64Arrays of fft.n, reused. Returns Float64Array(n / 2 + 1). */
  function hannPower(x, fft, win, scratch, out) {
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
  function engineGrid(min, max, nBins) {
    const r = Math.pow(max / min, 1 / (nBins - 1));
    const f = new Float64Array(nBins);
    for (let i = 0; i < nBins; i++) f[i] = min * Math.pow(r, i);
    return { min, max, n: nBins, ratio: r, f };
  }
  function bandTable(grid, binHz, nFft) {
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
  function bandPeaks(power, table, out) {
    out = out || new Float64Array(table.n);
    for (let i = 0; i < table.n; i++) {
      let m = 0;
      for (let k = table.lo[i]; k <= table.hi[i]; k++) if (power[k] > m) m = power[k];
      out[i] = m;
    }
    return out;
  }

  /* A sliding window of timestamped power frames; mean() averages the last winS seconds in power. */
  class Window {
    constructor(maxS) { this.maxS = maxS || 20; this.frames = []; }
    clear() { this.frames.length = 0; }
    push(t, pow) {
      this.frames.push({ t, p: Float64Array.from(pow) });
      while (this.frames.length && this.frames[0].t < t - this.maxS - 1) this.frames.shift();
    }
    mean(now, winS) {
      const from = now - winS;
      let count = 0, first = null, sum = null;
      for (const fr of this.frames) {
        if (fr.t < from || fr.t > now) continue;
        if (!sum) sum = new Float64Array(fr.p.length);
        if (fr.p.length !== sum.length) continue;
        for (let i = 0; i < sum.length; i++) sum[i] += fr.p[i];
        count++;
        if (first === null) first = fr.t;
      }
      if (!count) return null;
      for (let i = 0; i < sum.length; i++) sum[i] /= count;
      return { mean: sum, count, span: now - first };
    }
  }

  /* mic minus program, per bin, in dB. A bin counts only where the program has something to
     compare: no quieter than relDb under the program's own 200 Hz-5 kHz median and above floorDb.
     The result is shifted so that its median over refLo-refHi is 0 dB (the mic is uncalibrated
     and the master sits between the two). quiet = the program is too low to say anything. */
  function response(micPow, progPow, freqs, opts = {}) {
    const refLo = opts.refLo || 200, refHi = opts.refHi || 5000;
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
      if (prog[i] < progRef + relDb || prog[i] < floorDb || micDb[i] <= FLOOR_DB) continue;
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
     gap). The width may be fractional: the end bins count pro rata, so on the engine's grid
     (12.04 bins per octave) width = 12.04 / 6 is exactly 1/6 octave ([0.5, 1, 0.5] / 2) and
     12.04 / 3 exactly 1/3 octave ([0.5, 1, 1, 1, 0.5] / 4). width <= 1: unchanged. */
  function smoothDb(db, width) {
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

  /* The RTA display shows BAND power, as a 1/n-octave RTA does: smoothFracOct gives the mean
     power density over a window as wide as a fixed fraction of an octave, i.e. proportional to f,
     so band power = density x f. In dB: + 10 log10(f / refHz). Pink noise (and most music) then
     reads flat instead of falling 3 dB per octave, and a trace drawn at the EQ grid's own dB per
     division fits in it. */
  function bandDb(db, freqs, refHz, out) {
    const ref = refHz || 1000;
    out = out || new Float64Array(db.length);
    for (let i = 0; i < db.length; i++) out[i] = db[i] + 10 * Math.log10(freqs[i] / ref);
    return out;
  }

  return {
    FLOOR_DB, FAST_TAU, SLOW_TAU, dbToPow, powToDb, fromDb, toDb, logPoints, Ballistics, smoothFracOct,
    peakHold, LevelTracker, median, findPeak, PeakTracker, FFT, engineFftLen, hann, hannPower,
    engineGrid, bandTable, bandPeaks, Window, response, smoothDb, bandDb,
  };
});

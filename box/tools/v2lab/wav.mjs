// SPDX-License-Identifier: GPL-3.0-or-later
/* Test signals for the phone-mic e2e (made at run time, never committed: hub rule 1), 48 kHz mono
   16-bit WAV, 2^19 samples (10.9 s), made in the frequency domain so the loop Chrome's fake
   capture plays has no click:
   - pink: power ~ 1/f from 15 Hz (band power flat: the mic line reads flat);
   - tone: the same pink + a steady sine on an FFT bin near `hz`, `above` dB over the pink
     noise's power in the 1/24-octave band around it (the mic PEAK finds it);
   - tilt: the pink through a known tilt, `dbPerOct` dB per octave around 1 kHz (RESPONSE shows it).
   readWav() reads one back as Float32 samples (the fake box's program). */

import fs from "node:fs";
import { FFT } from "../../k1000/web/static/v2/js/micmath.js";

export const RATE = 48000, N = 1 << 19;

function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
function gauss(r) { let u = 0; while (u === 0) u = r(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * r()); }

// spec(k) -> [re, im] for bins 1..N/2-1 -> a real signal (inverse FFT)
function synth(spec) {
  const re = new Float64Array(N), im = new Float64Array(N);
  for (let k = 1; k < N / 2; k++) { const [a, b] = spec(k); re[k] = a; im[k] = b; re[N - k] = a; im[N - k] = -b; }
  for (let k = 0; k < N; k++) im[k] = -im[k];
  new FFT(N).transform(re, im);
  const x = new Float64Array(N);
  for (let k = 0; k < N; k++) x[k] = re[k] / N;
  return x;
}

export function makeSignal(kind, opts = {}) {
  const r = rng(opts.seed || 1000);
  const tilt = kind === "tilt" ? (opts.dbPerOct == null ? 2 : opts.dbPerOct) : 0;
  const amp = new Float64Array(N / 2);
  const X = [];
  for (let k = 1; k < N / 2; k++) {
    const f = k * RATE / N;
    let a = f < 15 ? 0 : 1 / Math.sqrt(f);
    if (tilt) a *= Math.pow(10, tilt * Math.log2(Math.min(16000, Math.max(50, f)) / 1000) / 20);
    amp[k] = a;
    X[k] = [gauss(r) * a, gauss(r) * a];
  }
  let x = synth((k) => X[k]);
  if (kind === "tone") {
    const hz = opts.hz || 1000, above = opts.above == null ? 15 : opts.above;
    const kk = Math.round(hz * N / RATE), f0 = kk * RATE / N;
    let pBand = 0;                                       // the noise's power in the 1/24 oct around f0
    for (let k = 1; k < N / 2; k++) { const f = k * RATE / N; if (f >= f0 * Math.pow(2, -1 / 48) && f < f0 * Math.pow(2, 1 / 48)) pBand += 2 * (X[k][0] ** 2 + X[k][1] ** 2) / (N * N); }
    const a = Math.sqrt(2 * pBand * Math.pow(10, above / 10));
    for (let i = 0; i < N; i++) x[i] += a * Math.sin(2 * Math.PI * kk * i / N);
  }
  let peak = 0;
  for (let i = 0; i < N; i++) peak = Math.max(peak, Math.abs(x[i]));
  const g = (opts.peak || 0.5) / peak;
  const out = new Float32Array(N);
  for (let i = 0; i < N; i++) out[i] = x[i] * g;
  return out;
}

export function writeWav(file, x) {
  const n = x.length, buf = Buffer.alloc(44 + 2 * n);
  buf.write("RIFF", 0); buf.writeUInt32LE(36 + 2 * n, 4); buf.write("WAVE", 8);
  buf.write("fmt ", 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(RATE, 24); buf.writeUInt32LE(RATE * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write("data", 36); buf.writeUInt32LE(2 * n, 40);
  for (let i = 0; i < n; i++) buf.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(x[i] * 32767))), 44 + 2 * i);
  fs.writeFileSync(file, buf);
  return file;
}
export function readWav(file) {
  const b = fs.readFileSync(file);
  let p = 12, data = null, rate = 0, ch = 1;
  while (p + 8 <= b.length) {
    const id = b.toString("ascii", p, p + 4), len = b.readUInt32LE(p + 4);
    if (id === "fmt ") { ch = b.readUInt16LE(p + 10); rate = b.readUInt32LE(p + 12); }
    if (id === "data") { data = b.subarray(p + 8, p + 8 + len); break; }
    p += 8 + len + (len & 1);
  }
  const n = Math.floor(data.length / 2 / ch), x = new Float32Array(n);
  for (let i = 0; i < n; i++) x[i] = data.readInt16LE(2 * i * ch) / 32768;
  return { rate, x };
}

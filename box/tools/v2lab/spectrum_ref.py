# SPDX-License-Identifier: GPL-3.0-or-later
"""Reference spectra for micunit.mjs, made OUTSIDE the page's code: CamillaDSP v5.0.0-beta.1
src/spectrum.rs (SpectrumComputer::compute_from_windowed, fft_length_for) re-done in numpy,
step for step and in f32 where the engine is f32, on a fixed test signal that micunit.mjs builds
the same way. The phone's estimator (k1000/web/static/v2/js/micmath.js EngineEstimator, which
RESPONSE and the mic PEAK run on) is then checked against an independent port of the engine,
not against itself (the fake box's program windows use micmath too).

    python tools/v2lab/spectrum_ref.py > tools/v2lab/spectrum_ref.json   (from box/, needs numpy)

Only for the lab: never imported by the box."""

from __future__ import annotations

import json
import math
import sys

import numpy as np

MIN_F, MAX_F, N_BINS = 10.0, 20000.0, 264          # the box's grid (rta.py, hello.pgm)
RATES = (48000, 44100)


def fft_length_for(min_freq: float, samplerate: int) -> int:
    """spectrum.rs: the smallest power of two >= samplerate / min_freq (capped at 262144)."""
    n = math.ceil(samplerate / min_freq)
    return min(1 << (n - 1).bit_length(), 262144)


def test_signal(n: int, rate: int) -> np.ndarray:
    """micunit.mjs refSignal(): three tones and a little white noise from a 32-bit LCG, f32."""
    u = 12345
    x = np.empty(n, dtype=np.float64)
    for i in range(n):
        u = (1664525 * u + 1013904223) & 0xFFFFFFFF
        v = u / 4294967296.0 - 0.5
        x[i] = (0.5 * math.sin(2 * math.pi * 997 * i / rate) + 0.01 * math.sin(2 * math.pi * 63.3 * i / rate)
                + 0.2 * math.sin(2 * math.pi * 12345 * i / rate) + 0.05 * v)
    return x.astype(np.float32)


def engine_spectrum(signal: np.ndarray, rate: int) -> list[float]:
    """compute_from_windowed: Hann (f64 trig, stored f32), window sum in f64, inv_w2 f32, the
    windowed signal f32, a real FFT, power = scale * |X|^2 * inv_w2 (scale 1 at DC and Nyquist,
    else 4), each bin = the PEAK power of the FFT bins from floor(f_low / res) to
    min(ceil(f_high / res), n_fft - 1), else the nearest bin; f_center advanced multiplicatively."""
    n = len(signal)
    i = np.arange(n, dtype=np.float64)
    window = (0.5 * (1.0 - np.cos(2.0 * math.pi * i / (n - 1)))).astype(np.float32)
    window_sum = float(np.sum(window.astype(np.float64)))
    inv_w2 = np.float32(1.0 / (window_sum * window_sum))
    windowed = (signal.astype(np.float32) * window).astype(np.float32)
    spec = np.fft.rfft(windowed)
    n_fft = len(spec)
    norm_sqr = (spec.real.astype(np.float32) ** 2 + spec.imag.astype(np.float32) ** 2).astype(np.float32)
    freq_res = rate / n
    log_ratio = (MAX_F / MIN_F) ** (1.0 / (N_BINS - 1))
    sqrt_log_ratio = math.sqrt(log_ratio)

    def power(k: int) -> np.float32:
        scale = np.float32(1.0 if k in (0, n_fft - 1) else 4.0)
        return np.float32(scale * norm_sqr[k] * inv_w2)

    out = []
    f_center = MIN_F
    for b in range(N_BINS):
        f_low = MIN_F if b == 0 else f_center / sqrt_log_ratio
        f_high = MAX_F if b == N_BINS - 1 else f_center * sqrt_log_ratio
        k_low = math.floor(f_low / freq_res)
        k_high = min(math.ceil(f_high / freq_res), n_fft - 1)
        if k_low <= k_high:
            peak = np.float32(0.0)
            for k in range(k_low, k_high + 1):
                peak = max(peak, power(k))
        else:
            peak = power(min(math.floor(f_center / freq_res + 0.5), n_fft - 1))   # Rust round(): half away from zero
        out.append(round(10.0 * math.log10(max(float(peak), 1e-30)), 4))
        f_center *= log_ratio
    return out


def main() -> int:
    ref = {"source": "CamillaDSP v5.0.0-beta.1 src/spectrum.rs, re-done in numpy (tools/v2lab/spectrum_ref.py)",
           "grid": {"f0": MIN_F, "f1": MAX_F, "n": N_BINS}, "signal": "micunit.mjs refSignal (3 tones + LCG noise, f32)",
           "rates": {}}
    for rate in RATES:
        n = fft_length_for(MIN_F, rate)
        ref["rates"][str(rate)] = {"fft": n, "db": engine_spectrum(test_signal(n, rate), rate)}
    json.dump(ref, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline check of m4loop.comb_latency on a simulated engine spectrum (CamillaDSP binning)."""
import sys, math
import numpy as np
sys.path.insert(0, sys.argv[1])
import m4loop as m

FS = 48000


def cdsp_spectrum(sig, min_f, max_f, n_bins):
    n = 1 << (math.ceil(FS / min_f) - 1).bit_length()
    w = np.hanning(n + 1)[:-1] if False else 0.5 * (1 - np.cos(2 * np.pi * np.arange(n) / (n - 1)))
    X = np.fft.rfft(sig[-n:] * w)
    res = FS / n
    p = 4 * np.abs(X) ** 2 / np.sum(w) ** 2
    lr = (max_f / min_f) ** (1 / (n_bins - 1))
    s = math.sqrt(lr)
    fc = min_f * lr ** np.arange(n_bins)
    out = np.empty(n_bins)
    for i, c in enumerate(fc):
        lo = min_f if i == 0 else c / s
        hi = max_f if i == n_bins - 1 else c * s
        kl, kh = int(math.floor(lo / res)), min(int(math.ceil(hi / res)), len(p) - 1)
        out[i] = p[kl:kh + 1].max()
    return fc, out


rng = np.random.default_rng(1)
for L_ms, g, pol in ((18.37, 0.30, 1), (16.9, 0.15, 1), (21.02, 0.3, -1)):
    n = 12 * FS
    X = np.fft.rfft(rng.standard_normal(n)); f = np.fft.rfftfreq(n, 1 / FS); f[0] = f[1]
    x = np.fft.irfft(X / np.sqrt(f), n); x /= x.std()
    d = L_ms / 1000 * FS
    # fractional delay by FFT
    Y = np.fft.rfft(x) * np.exp(-2j * np.pi * np.fft.rfftfreq(n, 1 / FS) * L_ms / 1000)
    ret = pol * g * np.fft.irfft(Y, n)
    noise = 1e-4 * rng.standard_normal((2, n))
    mix = (x + ret + noise[0] + noise[1]) / 4
    acc = None
    for k in range(8):
        seg = mix[: n - k * 16000]
        fl, p = cdsp_spectrum(seg, 1.5, 10500.0, 12000)
        acc = p if acc is None else acc + p
    tau, sign, snr = m.comb_latency(fl, acc / 8)
    print(f"true {L_ms} ms g {g} pol {pol}: found {tau:.3f} ms sign {sign:+.0f} snr {snr:.1f}")

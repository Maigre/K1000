#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""M4 loop test, run ON the box while K1000 runs live (speakers off).

Wiring: a source into one M4 line input (SRC: 2 = LINE IN 3, 3 = LINE IN 4), one M4 output (OUT
1-4) looped back into the OTHER line input (RET). The source is the box's own HiFiBerry output
(this script plays a 1 kHz sine, then pink noise) unless --ext (a phone plays it).

Reads only the engine's meters and spectra (CamillaDSP websocket); changes nothing in K1000.
- level: analog loop gain = RMS(RET) - RMS(SRC) (both line inputs have the same sensitivity);
  digital gain = playback RMS(OUT) - capture RMS(SRC); their difference is the M4's analog
  offset for that output (out full scale vs line-in full scale, plus the MONITOR knob on 1-2).
- routing: playback RMS of all four outputs (which carry the source).
- response: |H(f)| = spectrum(RET) / spectrum(SRC), 1/3 octave.
- latency: the capture average (SRC + RET) is a comb whose tooth spacing is 1/latency;
  found by a matched comb search over 200 Hz - 10 kHz. Latency = analog LINE IN -> OUT N.
"""
import argparse, json, math, os, subprocess, sys, time, wave
import numpy as np
import websockets.sync.client as wsc

FS = 48000
HB = "hw:sndrpihifiberry"
TMP = "/tmp/k1000-m4loop"


class Engine:
    def __init__(self):
        self.w = wsc.connect("ws://127.0.0.1:1234", max_size=None)

    def cmd(self, name, value=None):
        msg = {"command": name} if value is None else {"command": name, "value": value}
        self.w.send(json.dumps(msg))
        r = json.loads(self.w.recv())
        if r.get("result") != "Ok":
            raise RuntimeError(f"{name}: {r}")
        return r.get("value")


def write_wav(path, x):
    """x: float mono in [-1, 1] -> stereo S32_LE WAV (same signal on both HiFiBerry outputs)."""
    s = np.clip(x, -1.0, 1.0)
    i32 = (s * (2**31 - 1)).astype("<i4")
    st = np.repeat(i32[:, None], 2, axis=1)
    with wave.open(path, "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(4)
        f.setframerate(FS)
        f.writeframes(st.tobytes())


def make_signals():
    os.makedirs(TMP, exist_ok=True)
    sine, pink = f"{TMP}/sine1k.wav", f"{TMP}/pink.wav"
    if not os.path.exists(sine):
        t = np.arange(12 * FS) / FS
        a = 10 ** (-20 / 20) * math.sqrt(2)          # -20 dBFS RMS
        x = a * np.sin(2 * np.pi * 1000 * t)
        ramp = np.minimum(1, np.minimum(t, t[-1] - t) / 0.05)
        write_wav(sine, x * ramp)
    if not os.path.exists(pink):
        n = 40 * FS
        rng = np.random.default_rng(37)
        X = np.fft.rfft(rng.standard_normal(n))
        f = np.fft.rfftfreq(n, 1 / FS)
        f[0] = f[1]
        X /= np.sqrt(f)
        X[f < 15] = 0
        x = np.fft.irfft(X, n)
        x *= 10 ** (-18 / 20) / np.sqrt(np.mean(x**2))   # -18 dBFS RMS
        t = np.arange(n) / FS
        x *= np.minimum(1, np.minimum(t, t[-1] - t) / 0.1)
        write_wav(pink, x)
    return sine, pink


def play(path):
    return subprocess.Popen(["aplay", "-q", "-D", HB, path], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)


def db(x):
    return 10 * np.log10(np.maximum(x, 1e-30))


def spectrum(e, channel, min_f, max_f, n_bins, n_avg, gap=0.35):
    acc = None
    for _ in range(n_avg):
        v = e.cmd("GetSpectrum", {"side": "capture", "channel": channel, "min_freq": min_f,
                                  "max_freq": max_f, "n_bins": n_bins})
        p = 10 ** (np.asarray(v["magnitudes"], dtype=float) / 10)
        acc = p if acc is None else acc + p
        f = np.asarray(v["frequencies"], dtype=float)
        time.sleep(gap)
    return f, acc / n_avg


def comb_latency(f_log, p):
    """Matched comb search: the delay tau whose cos(2 pi f tau) best fits the detrended log
    spectrum. Returns (tau_ms, sign, snr)."""
    grid = np.arange(200.0, 10000.0, 0.5)
    m = np.interp(grid, f_log, db(p))
    k = int(300 / 0.5)                                   # remove the slope / EQ: 300 Hz mean
    kern = np.ones(k) / k
    trend = np.convolve(np.pad(m, (k // 2, k - k // 2 - 1), mode="edge"), kern, mode="valid")
    d = (m - trend) * np.hanning(len(m))
    # coarse: |FFT| over the frequency axis -> quefrency
    nfft = 1 << (len(d) * 4 - 1).bit_length()
    C = np.abs(np.fft.rfft(d, nfft))
    q = np.arange(len(C)) / (nfft * 0.5)                 # seconds
    sel = (q > 0.003) & (q < 0.080)
    i = np.argmax(np.where(sel, C, 0))
    snr = C[i] / np.median(C[sel])
    # fine: direct correlation around the coarse peak
    taus = np.arange(q[i] - 0.0004, q[i] + 0.0004, 0.000002)
    corr = np.array([np.dot(d, np.cos(2 * np.pi * grid * tau)) for tau in taus])
    j = np.argmax(np.abs(corr))
    return taus[j] * 1000, float(np.sign(corr[j])), float(snr)


def third_octave(f_log, p_ret, p_src):
    out = []
    for fc in [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]:
        sel = (f_log >= fc / 2**(1 / 6)) & (f_log < fc * 2**(1 / 6))
        if sel.any():
            out.append((fc, float(db(np.mean(p_ret[sel]) / np.mean(p_src[sel])))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=int, help="the M4 output looped back (1-4)")
    ap.add_argument("--src", type=int, default=2, help="capture channel of the source (2 = LINE IN 3)")
    ap.add_argument("--ret", type=int, default=3, help="capture channel of the return (3 = LINE IN 4)")
    ap.add_argument("--ext", action="store_true", help="external source (phone): play nothing")
    ap.add_argument("--avg", type=int, default=8)
    a = ap.parse_args()
    e = Engine()
    o = a.out - 1
    res = {"out": a.out, "src_ch": a.src, "ret_ch": a.ret}
    sine, pink = make_signals()

    # 1. level + routing, 1 kHz sine
    pr = None if a.ext else play(sine)
    time.sleep(2.5)
    cap = e.cmd("GetCaptureSignalRmsSince", 2.0)
    pb = e.cmd("GetPlaybackSignalRmsSince", 2.0)
    cpk = e.cmd("GetCaptureSignalPeakSince", 2.0)
    if pr:
        pr.terminate(); pr.wait()
    res["sine_capture_rms"] = [round(x, 2) for x in cap]
    res["sine_capture_peak"] = [round(x, 2) for x in cpk]
    res["sine_playback_rms"] = [round(x, 2) for x in pb]
    res["analog_loop_gain_db"] = round(cap[a.ret] - cap[a.src], 2)
    res["digital_gain_db"] = round(pb[o] - cap[a.src], 2)
    res["m4_analog_offset_db"] = round(res["analog_loop_gain_db"] - res["digital_gain_db"], 2)
    res["outputs_carrying_source"] = [i + 1 for i, x in enumerate(pb) if x > cap[a.src] - 40]

    # 2. response + latency, pink noise
    time.sleep(0.5)
    pr = None if a.ext else play(pink)
    time.sleep(3.0)
    f, p_src = spectrum(e, a.src, 10.0, 20000.0, 400, a.avg)
    f2, p_ret = spectrum(e, a.ret, 10.0, 20000.0, 400, a.avg)
    fl, p_all = spectrum(e, None, 1.5, 10500.0, 12000, a.avg)
    pn = e.cmd("GetCaptureSignalRmsSince", 2.0)
    if pr:
        pr.terminate(); pr.wait()
    res["noise_capture_rms"] = [round(x, 2) for x in pn]
    res["response_db_third_oct"] = [(fc, round(g, 2)) for fc, g in third_octave(f, p_ret, p_src)]
    tau, sign, snr = comb_latency(fl, p_all)
    res["latency_ms"] = round(tau, 3)
    res["return_polarity"] = "same" if sign > 0 else "inverted"
    res["comb_snr"] = round(snr, 1)
    lvl = e.cmd("GetBufferLevel")
    res["buffer_level_frames"] = lvl
    res["engine_buffers_ms"] = round((256 + lvl) / FS * 1000, 2)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()

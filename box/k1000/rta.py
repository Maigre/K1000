# SPDX-License-Identifier: GPL-3.0-or-later
"""The SHOW page's program spectrum (D-009): CamillaDSP's GetSpectrum, polled on K1000's own
second websocket, turned into a steady picture and a PEAK pointer.

Why it is shaped like this:

- **Its own connection, never the controller's.** `CamillaClient` serialises every command
  behind one lock, and a config upload holds that lock until its read-back: a spectrum poll
  must never queue in front of a mute, nor behind an upload. `SpectrumTask` owns a separate
  client, with a short command timeout; when that link is slow or down, the controller's link
  (mute, faders, adoption) does not notice.
- **Polled only when it is looked at.** At ~10 Hz, only while the controller says so
  (`wanted()`: SHOW displayed, SETUP > Spectrum ON, engine Running/Paused, device present).
  Not wanted for IDLE_CLOSE_S: the connection is closed too. Not wanted at all: no frame is
  kept, so a stale spectrum is never drawn when SHOW comes back.
- **The program, not the room.** Capture side, the profile's `input_map` channels (program L
  and R: m4 2,3; dev 0,1), one call per channel, combined as a POWER average
  (`eqgraph.combine_power`). Never `channel: null`: on the M4 it averages the waveforms of the
  mic and the spare input into the program.
- **Robust to the engine.** A fresh engine answers "No audio data available" to its first
  call (the call itself starts the analyser): retried quietly. A link down or restarting: the
  frame is cleared after STALE_S without data (never a frozen picture), and polling resumes by
  itself when the client reconnects. A refusal (a channel out of range on a wrong profile):
  logged once, state "unavailable", retried every RETRY_BAD_S.
- **The hand comes first.** `note_input()` (every knob/button event) holds the publishing of
  new frames for PAUSE_AFTER_INPUT_S: the first frame after a knob event is the knob's edit,
  not a spectrum redraw. Polling and the ballistics continue meanwhile.
- **A steady picture** (`Ballistics`): bars rise at once and fall at `fall_db_s`; the display
  range follows the loudest bin as a slow tracker (rises at once, falls SCALE_FALL_DB_S, no
  6 dB steps); PEAK is a narrow bin standing ENTER_DB above the median of the +-1 octave
  around it, not buried (within LOUD_DB of the loudest), persistent PERSIST_S, with hysteresis
  (kept while EXIT_DB above, released after RELEASE_S below, the pointer moves only when the
  peak moves by MOVE_BINS bins). It says PEAK, never FEEDBACK: a held note looks the same.

Frames are `eqgraph.RtaFrame` objects built fresh for every publish (their arrays are never
written again), so the render thread may keep one while the next is computed.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Callable, Iterable

import numpy as np

from .cdsp import CdspError, CdspUnavailable
from .ui.eqgraph import RtaFrame, combine_power

log = logging.getLogger(__name__)

MIN_FREQ = 10.0              # the FFT length follows fs / min_freq: 10 Hz -> 8192 points at 48 kHz
MAX_FREQ = 20000.0
BINS_PER_OCT = 24            # 1/24 octave: 264 bins from 10 Hz to 20 kHz (~5 kB per reply)
N_BINS = int(round(math.log2(MAX_FREQ / MIN_FREQ) * BINS_PER_OCT)) + 1
RATE_HZ = 10.0
COMMAND_TIMEOUT_S = 0.5      # a stuck engine drops this link quickly (the client reconnects)
PAUSE_AFTER_INPUT_S = 0.3
STALE_S = 1.0                # wanted but no fresh data this long: the frame is cleared
RETRY_BAD_S = 5.0            # after a refusal (wrong channel): try again this often
IDLE_CLOSE_S = 30.0          # not wanted this long: the second connection is closed
NO_DATA = "no audio data"    # the engine's answer before its analyser has data (any case)
FLOOR_DB = -140.0

# States (Controller.status.spectrum_state)
OFF, WAITING, LIVE, UNAVAILABLE = "off", "waiting", "live", "unavailable"


def spectrum_request(channel: int, n_bins: int = N_BINS) -> dict[str, Any]:
    """The GetSpectrum `value` for one capture channel (CamillaDSP 5.0.0-beta.1, probed)."""
    return {"side": "capture", "channel": int(channel), "min_freq": MIN_FREQ,
            "max_freq": MAX_FREQ, "n_bins": int(n_bins)}


def parse_spectrum(value: Any) -> tuple[np.ndarray, np.ndarray] | None:
    """{"frequencies": [...], "magnitudes": [...]} -> (freqs, dBFS) or None when unusable.
    A null/non-numeric magnitude (serde writes NaN as null) reads as the floor."""
    if not isinstance(value, dict):
        return None
    fr, mg = value.get("frequencies"), value.get("magnitudes")
    if not isinstance(fr, list) or not isinstance(mg, list) or len(fr) != len(mg) or len(fr) < 2:
        return None
    try:
        f = np.asarray([float(x) for x in fr], dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if not np.all(np.isfinite(f)) or f[0] <= 0 or np.any(np.diff(f) < 0):
        return None
    m = np.asarray([float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else FLOOR_DB
                    for x in mg], dtype=np.float64)
    m = np.where(np.isfinite(m), m, FLOOR_DB)
    return f, np.clip(m, FLOOR_DB, 40.0)


# =========================================================================== ballistics
class Ballistics:
    """Display ballistics and the PEAK detector over successive spectra (pure; `now` given).

    level: rises at once, falls `fall_db_s`. hold: per-bin peak held `hold_s`. Range: top =
    the loudest level (20 Hz..20 kHz) + `headroom_db`, as a slow tracker (rises at once, falls
    `scale_fall_db_s`), clamped to `top_range`; bottom = top - `span_db`. peak: see the module
    docstring; detected on a short power average (`avg_tau_s`) so one noisy frame neither
    starts nor ends a peak.
    """

    ENTER_DB = 10.0
    EXIT_DB = 6.0
    LOUD_DB = 30.0
    PERSIST_S = 1.5
    RELEASE_S = 0.5
    CONTEXT_OCT = 1.0
    TRACK_BINS = 2           # a peak (or a candidate) is the same one within this many bins
    MOVE_BINS = 2            # the pointer moves only when the peak moved this far
    SEARCH = (40.0, 16000.0)

    def __init__(self, *, fall_db_s: float = 24.0, hold_s: float = 3.0, span_db: float = 66.0,
                 headroom_db: float = 18.0, scale_fall_db_s: float = 1.0,
                 top_range: tuple[float, float] = (-60.0, 18.0), avg_tau_s: float = 0.25,
                 display: tuple[float, float] = (20.0, 20000.0)) -> None:
        self.fall_db_s, self.hold_s, self.span_db = fall_db_s, hold_s, span_db
        self.headroom_db, self.scale_fall_db_s = headroom_db, scale_fall_db_s
        self.top_range, self.avg_tau_s, self.display = top_range, avg_tau_s, display
        self.reset()

    def reset(self) -> None:
        self.freqs: np.ndarray | None = None
        self.level: np.ndarray | None = None
        self.hold: np.ndarray | None = None
        self._hold_t: np.ndarray | None = None
        self._avg_pow: np.ndarray | None = None
        self._t: float | None = None
        self.top_db: float | None = None
        self._cand: tuple[int, float] | None = None     # (bin, since)
        self._peak: int | None = None                   # tracked bin
        self._shown: int | None = None                  # the bin the pointer shows
        self._lost: float | None = None                 # when the tracked peak fell below EXIT

    # ------------------------------------------------------------------ update
    def update(self, freqs: Any, mags: Any, now: float) -> None:
        f = np.asarray(freqs, dtype=np.float64)
        m = np.maximum(np.asarray(mags, dtype=np.float64), FLOOR_DB)
        if self.freqs is None or self.level is None or len(f) != len(self.freqs) \
                or not np.allclose(f, self.freqs):
            self.reset()
            self.freqs, self.level = f.copy(), m.copy()
            self.hold, self._hold_t = m.copy(), np.full(len(m), now)
            self._avg_pow = 10.0 ** (m / 10.0)
            self._t = now
        dt = max(0.0, now - (self._t if self._t is not None else now))
        self._t = now
        self.level = np.maximum(m, self.level - self.fall_db_s * dt)
        assert self.hold is not None and self._hold_t is not None and self._avg_pow is not None
        up = m >= self.hold
        expired = (now - self._hold_t) > self.hold_s
        self.hold = np.where(up | expired, m, self.hold)
        self._hold_t = np.where(up | expired, now, self._hold_t)
        a = math.exp(-dt / self.avg_tau_s) if self.avg_tau_s > 0 else 0.0
        self._avg_pow = a * self._avg_pow + (1.0 - a) * 10.0 ** (m / 10.0)
        self._scale(dt)
        self._track_peak(10.0 * np.log10(np.maximum(self._avg_pow, 1e-30)), now)

    def _scale(self, dt: float) -> None:
        assert self.freqs is not None and self.level is not None
        vis = (self.freqs >= self.display[0]) & (self.freqs <= self.display[1])
        loud = float(np.max(self.level[vis])) if vis.any() else float(np.max(self.level))
        lo, hi = self.top_range
        target = min(hi, max(lo, loud + self.headroom_db))
        if self.top_db is None or target >= self.top_db:
            self.top_db = target                                    # rises at once
        else:
            self.top_db = max(target, self.top_db - self.scale_fall_db_s * dt)

    def _track_peak(self, m: np.ndarray, now: float) -> None:
        f = self.freqs
        assert f is not None
        n = len(m)
        per_oct = (n - 1) / max(1e-9, math.log2(f[-1] / f[0])) if n > 1 else 1.0
        half = max(2, int(round(per_oct * self.CONTEXT_OCT)))
        pad = np.pad(m, half, mode="edge")
        ctx = np.median(np.lib.stride_tricks.sliding_window_view(pad, 2 * half + 1), axis=1)
        prom = m - ctx
        inside = (f >= self.SEARCH[0]) & (f <= self.SEARCH[1])
        if not inside.any():
            self._cand = self._peak = self._shown = self._lost = None
            return
        loud = m >= float(np.max(m[inside])) - self.LOUD_DB
        if self._peak is not None:            # a shown peak: kept while EXIT_DB above (hysteresis)
            lo = max(0, self._peak - self.TRACK_BINS)
            hi = min(n, self._peak + self.TRACK_BINS + 1)
            j = lo + int(np.argmax(prom[lo:hi]))
            if prom[j] >= self.EXIT_DB and loud[j] and inside[j]:
                self._peak, self._lost = j, None
                if self._shown is None or abs(j - self._shown) >= self.MOVE_BINS:
                    self._shown = j
                return
            if self._lost is None:
                self._lost = now
            if now - self._lost < self.RELEASE_S:
                return
            self._peak = self._shown = self._lost = None
            self._cand = None
        ok = inside & loud & (prom >= self.ENTER_DB)
        if not ok.any():
            self._cand = None
            return
        i = int(np.argmax(np.where(ok, prom, -np.inf)))
        since = self._cand[1] if self._cand is not None and abs(self._cand[0] - i) <= self.TRACK_BINS \
            else now
        self._cand = (i, since)
        if now - since >= self.PERSIST_S:
            self._peak = self._shown = i
            self._cand, self._lost = None, None

    # ------------------------------------------------------------------ out
    @property
    def peak_hz(self) -> float | None:
        if self._shown is None or self.freqs is None or not 0 <= self._shown < len(self.freqs):
            return None
        return float(self.freqs[self._shown])

    def frame(self, source: str = "PGM") -> RtaFrame | None:
        """A new RtaFrame (fresh arrays: the caller may keep it while we go on)."""
        if self.freqs is None or self.level is None or self.top_db is None:
            return None
        top = float(self.top_db)
        return RtaFrame(self.freqs.copy(), self.level.copy(),
                        None if self.hold is None else self.hold.copy(),
                        top - self.span_db, top, self.peak_hz, source)


# =========================================================================== the task
def _default_client(url: str) -> Any:
    from .cdsp import CamillaClient
    return CamillaClient(url, timeout=COMMAND_TIMEOUT_S, ping_interval=5.0, backoff_max=2.0)


class SpectrumTask:
    """Polls GetSpectrum while `wanted()` and hands frames to `publish(frame_or_None, state)`.

    `channels()`: the capture channels of the program (read on every poll: a profile switch is
    followed). `client_factory(url)` builds the task's OWN client (a CamillaClient surface:
    connect(timeout), connected, command(name, value=...), close()). Never raises out of its
    loop; `stop()` cancels it and closes the client.
    """

    def __init__(self, url: str, *, wanted: Callable[[], bool], channels: Callable[[], Iterable[int]],
                 publish: Callable[[RtaFrame | None, str], None],
                 client_factory: Callable[[str], Any] | None = None, rate_hz: float = RATE_HZ,
                 n_bins: int = N_BINS, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Any] = asyncio.sleep,
                 ballistics: Ballistics | None = None) -> None:
        self.url = url
        self.wanted = wanted
        self.channels = channels
        self.publish = publish
        self._factory = client_factory or _default_client
        self.period = 1.0 / max(0.5, float(rate_hz))
        self.n_bins = int(n_bins)
        self._clock = clock
        self._sleep = sleep
        self.ballistics = ballistics or Ballistics()
        self.client: Any = None
        self.state = OFF
        self.frame: RtaFrame | None = None       # the last frame published
        self.polls = 0                           # GetSpectrum rounds that returned data
        self._task: asyncio.Task[None] | None = None
        self._pause_until = -math.inf
        self._last_data = -math.inf
        self._last_wanted = -math.inf
        self._retry_at = -math.inf
        self._pending = False                    # data computed but not published (pause)
        self._logged: set[Any] = set()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="k1000-spectrum")

    async def stop(self) -> None:
        t, self._task = self._task, None
        if t is not None:
            t.cancel()
            try:
                await t
            except BaseException:   # noqa: BLE001
                pass
        await self._close_client()

    def note_input(self) -> None:
        """A knob/button event: hold new frames for PAUSE_AFTER_INPUT_S (the edit draws first)."""
        self._pause_until = self._clock() + PAUSE_AFTER_INPUT_S

    # ------------------------------------------------------------------ loop
    async def _run(self) -> None:
        while True:
            t0 = self._clock()
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:   # noqa: BLE001 - the screen's spectrum must never kill k1000
                self._log_once(("step", type(e).__name__, str(e)), logging.ERROR,
                               "spectrum: %s: %s", type(e).__name__, e)
                self._clear(UNAVAILABLE)
                self._retry_at = self._clock() + RETRY_BAD_S
            await self._sleep(max(0.0, self.period - (self._clock() - t0)))

    async def step(self) -> None:
        """One round: poll if wanted (and allowed), publish if not paused."""
        now = self._clock()
        try:
            want = bool(self.wanted())
        except Exception:   # noqa: BLE001
            want = False
        if not want:
            if self.state != OFF or self.frame is not None:
                self._clear(OFF)
            if self.client is not None and now - self._last_wanted >= IDLE_CLOSE_S:
                await self._close_client()
            return
        self._last_wanted = now
        if self.state == OFF:
            self._set_state(WAITING)
        if now < self._retry_at:
            return
        client = await self._ensure_client()
        if client is None or not getattr(client, "connected", False):
            self._no_data(now)
            return
        chans = sorted({int(c) for c in self.channels()})
        if not chans:
            self._refused(now, "no program channel in the profile")
            return
        spectra: list[tuple[np.ndarray, np.ndarray]] = []
        for ch in chans:
            try:
                value = await client.command("GetSpectrum", value=spectrum_request(ch, self.n_bins))
            except CdspUnavailable:
                self._no_data(now)
                return
            except CdspError as e:
                if NO_DATA in str(e).lower():
                    self._no_data(now)           # a fresh engine: the call started the analyser
                    return
                self._refused(now, f"channel {ch}: {e}")
                return
            parsed = parse_spectrum(value)
            if parsed is None:
                self._refused(now, f"channel {ch}: unusable reply")
                return
            spectra.append(parsed)
        freqs = spectra[0][0]
        mags = [m for f, m in spectra if len(f) == len(freqs)]
        combined = mags[0] if len(mags) == 1 else combine_power(*mags)
        self.ballistics.update(freqs, combined, now)
        self.polls += 1
        self._last_data = now
        self._set_state(LIVE)
        self._pending = True
        if now >= self._pause_until:
            self._publish_now()

    # ------------------------------------------------------------------ helpers
    async def _ensure_client(self) -> Any:
        if self.client is None:
            try:
                self.client = self._factory(self.url)
            except Exception as e:   # noqa: BLE001
                self._log_once(("client", str(e)), logging.ERROR, "spectrum client: %s", e)
                return None
        try:
            await self.client.connect(timeout=0)     # starts the link; never waits for it
        except Exception as e:   # noqa: BLE001 - connect() does not raise for an absent engine
            self._log_once(("connect", str(e)), logging.WARNING, "spectrum connect: %s", e)
        return self.client

    async def _close_client(self) -> None:
        c, self.client = self.client, None
        if c is not None:
            try:
                await c.close()
            except Exception:   # noqa: BLE001
                pass

    def _publish_now(self) -> None:
        self._pending = False
        self.frame = self.ballistics.frame()
        self._emit()

    def _emit(self) -> None:
        try:
            self.publish(self.frame, self.state)
        except Exception as e:   # noqa: BLE001
            self._log_once(("publish", str(e)), logging.ERROR, "spectrum publish: %s", e)

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self._emit()

    def _clear(self, state: str) -> None:
        """No picture (never a frozen one): ballistics restart from the next data."""
        self.ballistics.reset()
        self._pending = False
        changed = self.frame is not None or state != self.state
        self.frame, self.state = None, state
        if changed:
            self._emit()

    def _no_data(self, now: float) -> None:
        if now - self._last_data >= STALE_S:
            self._clear(WAITING)

    def _refused(self, now: float, why: str) -> None:
        self._log_once(("refused", why), logging.WARNING, "spectrum unavailable (%s); retrying "
                       "every %g s", why, RETRY_BAD_S)
        self._retry_at = now + RETRY_BAD_S
        self._clear(UNAVAILABLE)

    def _log_once(self, key: Any, level: int, msg: str, *args: Any) -> None:
        if key not in self._logged:
            self._logged.add(key)
            log.log(level, msg, *args)

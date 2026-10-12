# SPDX-License-Identifier: GPL-3.0-or-later
"""The box's PROGRAM spectrum for the phone's response view (Phase 4 step 2, docs/11-phone-mic.md).

While at least one phone has its RESPONSE view open, this polls CamillaDSP's one-shot
``GetSpectrum`` on the capture side, once per program channel (the profile's ``input_map``),
power-averages them and hands one frame per round to the phone server, at most 2 rounds per
second. When nobody is listening there is no polling and no extra engine connection at all.

Why it is shaped like this:

- **Its own engine connection.** The control client serialises every command behind one lock and
  holds it through config read-backs: a spectrum request must never queue in front of a mute. A
  second ``CamillaClient`` (the same tested client, without hooks) opens with the first listener
  and closes with the last.
- **Never ``channel: null``.** The engine then averages the *waveforms* of every capture channel.
  On the M4 that mixes the measurement mic and the spare input into the program. Only the
  profile's own program channels are asked for, one call each, and their powers are averaged.
- **A fixed grid** (10 Hz to 20 kHz, 133 bins, about 1/12 octave). The engine's value per bin is
  the *peak* FFT bin in that band, not its energy, which reads several dB high on noise-like
  music at the top of the range. The phone computes its mic bands on the same grid with the same
  estimator (Hann window, the same FFT resolution, the peak bin per band), so that bias cancels
  in mic minus program.
- **Quiet failures.** A fresh engine answers ``No audio data available`` to the first call (the
  call is what arms its ring buffer). That, and an engine that is down, become a frame with a
  ``why`` and a retry on the next round, never an exception or a log flood. Any other refusal
  (a channel the device does not have) is logged once.
- **Swappable.** The phone server uses only ``set_active()``, ``close()`` and the ``on_frame``
  callback. UI v1's ``rta.py`` on main (one engine connection for the box's own RTA) can stand
  in for this module later with the same three.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from typing import Any, Callable, Sequence

from ..cdsp import DEFAULT_URL, CamillaClient, CdspError, CdspUnavailable

log = logging.getLogger("k1000.web.progspec")

GRID: tuple[float, float, int] = (10.0, 20000.0, 133)   # min_freq, max_freq, n_bins (~1/12 oct)
PERIOD_S = 0.5                    # one round (every program channel) per period: <= 2 Hz
CONNECT_S = 0.4                   # wait this long for the engine link per round, then retry
FLOOR_DB = -160.0                 # the engine floors silence at -300 dB; the phone needs no less
QUIET_ERRORS = ("no audio data", "insufficient data")
MAX_CHANNELS = 8


def combine_power(spectra: Sequence[Sequence[float]], floor_db: float = FLOOR_DB) -> list[float]:
    """Power average of per-channel dB spectra (L and R of the program): dB in, dB out, 0.1 dB.

    Power, not dB: the mean of -10 and -40 dB is -13 dB (the louder channel dominates, as the
    ear and the mic hear it), not -25."""
    if not spectra:
        return []
    n = len(spectra[0])
    out: list[float] = []
    for i in range(n):
        p = 0.0
        for s in spectra:
            v = float(s[i])
            p += 10.0 ** (max(v, floor_db) / 10.0)
        db = 10.0 * math.log10(p / len(spectra)) if p > 0 else floor_db
        out.append(round(max(floor_db, db), 1))
    return out


def program_channels(profile: Any) -> list[int]:
    """The profile's program channels (input_map L, R), deduplicated; [] when unusable."""
    imap = getattr(profile, "input_map", None)
    out: list[int] = []
    try:
        for ch in imap or ():
            if isinstance(ch, int) and not isinstance(ch, bool) and 0 <= ch < 64 and ch not in out:
                out.append(ch)
    except TypeError:
        return []
    return out[:MAX_CHANNELS]


def _magnitudes(value: Any, n: int) -> list[float] | None:
    """The reply's magnitudes when they are n finite-or-floored numbers, else None."""
    mags = value.get("magnitudes") if isinstance(value, dict) else None
    if not isinstance(mags, list) or len(mags) != n:
        return None
    out: list[float] = []
    for v in mags:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
            return None
        out.append(FLOOR_DB if v == -math.inf else min(60.0, float(v)))
    return out


class ProgramSpectrum:
    """Polls the capture-side spectrum of the program channels while `set_active(True)`.

    `on_frame(dict)` gets one phone message per round, never raises into this loop:
    `{"t": "ps", "grid": [min, max, n], "ch": [2, 3], "m": [dB...]}` or, when there is nothing to
    show, the same with `"m": None, "why": "no audio data" | "engine unavailable" | ...`.
    """

    def __init__(self, *, on_frame: Callable[[dict[str, Any]], None],
                 url_of: Callable[[], str] = lambda: DEFAULT_URL,
                 channels_of: Callable[[], Sequence[int]] = lambda: [],
                 client_factory: Callable[[str], Any] | None = None,
                 period_s: float = PERIOD_S, grid: tuple[float, float, int] = GRID,
                 connect_s: float = CONNECT_S) -> None:
        self.on_frame = on_frame
        self._url_of = url_of
        self._channels_of = channels_of
        self._factory = client_factory or (lambda url: CamillaClient(url, ping_interval=5.0))
        self.period_s = max(0.5, float(period_s))    # the phone protocol promises <= 2 Hz
        self.grid = grid
        self.connect_s = connect_s
        self._task: asyncio.Task[None] | None = None
        self._logged: set[str] = set()
        self.rounds = 0                  # polling rounds since start (tests, stats)
        self.calls = 0                   # GetSpectrum calls

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_active(self, on: bool) -> None:
        """Start polling (first listener) or stop it and close the engine link (last one gone).
        Synchronous and idempotent; call it from the event loop."""
        if on and not self.active:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="k1000-progspec")
        elif not on and self._task is not None:
            self._task.cancel()
            self._task = None

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    # ------------------------------------------------------------------ the loop
    def _frame(self, chans: list[int], m: list[float] | None, why: str = "") -> dict[str, Any]:
        lo, hi, n = self.grid
        out: dict[str, Any] = {"t": "ps", "grid": [lo, hi, n], "ch": chans, "m": m}
        if why:
            out["why"] = why
        return out

    def _emit(self, frame: dict[str, Any]) -> None:
        try:
            self.on_frame(frame)
        except Exception as e:  # noqa: BLE001 - a broken listener never stops the loop
            self._log_once("on_frame", f"program spectrum: frame handler failed: {type(e).__name__}: {e}")

    def _log_once(self, key: str, text: str) -> None:
        if key not in self._logged:
            self._logged.add(key)
            log.warning("%s", text)

    def _channels(self) -> list[int]:
        try:
            raw = list(self._channels_of())
        except Exception:  # noqa: BLE001
            return []
        out: list[int] = []
        for ch in raw:
            if isinstance(ch, int) and not isinstance(ch, bool) and 0 <= ch < 64 and ch not in out:
                out.append(ch)
        return out[:MAX_CHANNELS]

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        client = None
        try:
            try:
                client = self._factory(self._url_of() or DEFAULT_URL)
            except Exception as e:  # noqa: BLE001
                self._log_once("factory", f"program spectrum: no engine client ({type(e).__name__}: {e})")
                self._emit(self._frame([], None, "engine unavailable"))
                return
            log.info("program spectrum: on (a phone opened its response view)")
            while True:
                t0 = loop.time()
                self.rounds += 1
                try:
                    frame = await self._round(client)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - the loop survives anything
                    self._log_once(f"round:{type(e).__name__}",
                                   f"program spectrum: {type(e).__name__}: {e}")
                    frame = self._frame([], None, "error")
                self._emit(frame)
                await asyncio.sleep(max(0.05, self.period_s - (loop.time() - t0)))
        finally:
            if client is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(client.close(), 2.0)
                log.info("program spectrum: off")

    async def _round(self, client: Any) -> dict[str, Any]:
        chans = self._channels()
        if not chans:
            return self._frame([], None, "no program channels")
        if not getattr(client, "connected", False):
            if not await client.connect(timeout=self.connect_s):
                return self._frame(chans, None, "engine unavailable")
        lo, hi, n = self.grid
        spectra: list[list[float]] = []
        for ch in chans:
            self.calls += 1
            try:
                value = await client.command("GetSpectrum", value={
                    "side": "capture", "channel": ch, "min_freq": lo, "max_freq": hi, "n_bins": n})
            except CdspError as e:
                text = f"{e.result}: {e.message or ''}".lower()
                if any(q in text for q in QUIET_ERRORS):
                    return self._frame(chans, None, "no audio data")   # armed now: retried next round
                self._log_once(f"refused:{ch}", f"program spectrum: channel {ch} refused ({e})")
                return self._frame(chans, None, f"refused: {str(e.message or e.result)[:60]}")
            except CdspUnavailable:
                return self._frame(chans, None, "engine unavailable")
            mags = _magnitudes(value, n)
            if mags is None:
                self._log_once(f"shape:{ch}", f"program spectrum: unexpected reply for channel {ch}")
                return self._frame(chans, None, "bad reply")
            spectra.append(mags)
        return self._frame(chans, combine_power(spectra))


__all__ = ["GRID", "PERIOD_S", "ProgramSpectrum", "combine_power", "program_channels"]

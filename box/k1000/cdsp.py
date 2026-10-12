# SPDX-License-Identifier: GPL-3.0-or-later
"""Asyncio websocket client for CamillaDSP v5 (the only path from k1000 to the audio engine).

Why it is shaped like this:

* **One request in flight.** The v5 protocol has no request ids: a reply is matched to its
  command by order (and the ``reply`` name). A lock serialises callers, so the UI, the 1 Hz
  poller and the reconnect hook can all call in at once without crossing replies.
* **Unknown state means a fresh link.** If a reply does not arrive in time, or the caller is
  cancelled mid-request, a late reply could later be read as the answer to the *next* command
  (same name, wrong value). Rather than guess, the client drops the connection; the background
  loop reconnects and fires ``on_connect``, where the controller re-applies its state.
* **The engine may vanish at any time** (systemd restart, crash, profile switch). Commands then
  fail fast with :class:`CdspUnavailable` instead of blocking the UI; a background task
  reconnects with capped exponential backoff.
* **Never send garbage to the audio path.** Fader values are clamped (engine range plus a
  configurable safety ceiling), every ``gain`` written through ``set_value``/``patch_config``
  is clamped to a configurable gain ceiling (a whole config with an out-of-range gain is
  refused), and NaN/inf are refused before anything is sent. ``adjust_fader`` never raises a
  fader that sits below its floor (the engine's own min/max clamp would).
* **Config writes are confirmed.** CamillaDSP 5.0.0-beta.1 acknowledges a config write before
  it is active and computes the next write on the stale config, so back-to-back writes to two
  parameters can silently lose the first (measured on the box). ``set_value``,
  ``patch_config`` and ``set_config_yaml`` therefore hold the lock until a read-back shows
  the write active (usually one extra ~1 ms round trip). Faders are not affected.
  A whole-config upload that changes ``devices`` is also held until the engine has run
  steadily on them: the engine reports the new config (and even ``Running``) *before* it
  opens the new device, then stops or exits if the open fails (seen on the box).

Protocol facts (``websocket.md`` v5, proven on the box with 5.0.0-beta.1):
``{"command": X, ...fields}`` -> ``{"reply": X, "result": "Ok" | <ErrorName>, "value"?, "message"?}``.
Single-argument commands carry it in ``value`` (``GetConfigValue``, ``SetConfig``, ``PatchConfig``);
``SetConfigValue`` takes ``pointer`` + ``value``; fader commands take ``fader`` + ``value``.
A malformed or unknown command gets ``{"reply": "Invalid", "error": "..."}`` (no ``result``,
no command name), raised here as ``CdspError(name, "Invalid", error)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any, Awaitable, Callable, TypedDict

import yaml
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

DEFAULT_URL = "ws://127.0.0.1:1234"
COMMAND_TIMEOUT_S = 2.0
SETUP_TIMEOUT_S = 10.0  # whole-config uploads (setup actions), see set_config_yaml
# Config writes are confirmed by read-back (see CamillaClient._write_confirmed).
CONFIRM_TIMEOUT_S = 0.5
CONFIRM_POLL_S = 0.002
# After a device change, the engine must report Running/Paused for this long in a row before
# set_config_yaml returns. Measured on the box: a playback device that cannot be opened reads
# "Running" ~1 ms after SetConfig and kills the engine ~4 ms later; 0.5 s is a wide margin for
# an ALSA/USB open, and only setup actions pay it.
RUNNING_SETTLE_S = 0.5
RUNNING_POLL_S = 0.02
RUNNING_STATES = ("Running", "Paused")  # Paused = devices open, processing paused on silence

# Fader indices: Main is CamillaDSP's built-in master; Aux1..Aux4 drive Volume filters.
MAIN, AUX1, AUX2, AUX3, AUX4 = range(5)
FADER_COUNT = 5

# The engine clamps fader volumes to this range itself (websocket.md "SetFaderVolume").
ENGINE_FADER_MIN_DB = -150.0
ENGINE_FADER_MAX_DB = 50.0
# Client-side ceiling: a last-resort guard against a bug sending +50 dB to a PA. The controller
# clamps to state.RANGES first; this only has to be above every legitimate value (master,
# balance +b/2 on an Aux fader). The engine-side `volume_limit` / Volume `limit` also apply.
DEFAULT_FADER_MAX_DB = 12.0

# Filter/mixer gains: the engine accepts -150..+150 dB (ConfigValidationError beyond). Same
# idea as the fader ceiling: the controller clamps to state.RANGES (max +12 dB, input trim);
# this is the last guard against a bug sending +30 dB into a Gain or Biquad.
ENGINE_GAIN_MIN_DB = -150.0
ENGINE_GAIN_MAX_DB = 150.0
DEFAULT_GAIN_MAX_DB = 12.0

# GetConfigJson returns the whole config; leave room for big PEQ sets, refuse absurd frames.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class CdspError(Exception):
    """The engine answered, but with an error result (e.g. ``ConfigValidationError``).

    The link is still healthy: the command was understood and refused.
    """

    def __init__(self, command: str, result: str, message: str | None = None) -> None:
        self.command = command
        self.result = result
        self.message = message
        text = f"{command}: {result}"
        if message:
            text += f": {message}"
        super().__init__(text)


class CdspUnavailable(Exception):
    """No usable connection to the engine (down, restarting, or the link was just reset)."""


class CdspNotApplied(CdspError):
    """The engine accepted a config write, but the read-back never showed it active within the
    confirm window (``result == "NotApplied"``). Another writer (CamillaGUI) may have changed
    the same value meanwhile, or the engine is not applying configs."""


class CdspNotRunning(CdspError):
    """``set_config_yaml`` uploaded a config that changes the audio devices, and the engine did
    not run on them (``result == "NotRunning"``; the message carries the engine state and its
    stop reason). The new config IS the engine's active one: do not persist it as good."""


class CdspNoConfig(CdspError):
    """The engine has no active config (``result == "NoActiveConfig"``): it was started in
    wait mode (``-w``) and needs a ``set_config_yaml`` before anything else works."""


class CdspTimeout(CdspUnavailable):
    """No reply within the timeout. The link was reset, so whether the command took effect is
    unknown; ``on_connect`` re-applies the controller's state once the engine answers again."""


class Fader(TypedDict):
    volume: float
    mute: bool


Hook = Callable[["CamillaClient"], Awaitable[None]]


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _finite(x: Any, what: str) -> float:
    """Coerce to float and refuse NaN/inf: json would emit `NaN`, which the engine rejects, and a
    NaN reaching a gain computation is exactly the transient we must never produce."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError(f"{what} must be a number, got {x!r}")
    f = float(x)
    if not math.isfinite(f):
        raise ValueError(f"{what} must be finite, got {x!r}")
    return f


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _bounded(x: Any, what: str, lo: float, hi: float) -> int | float:
    """Clamp a number to [lo, hi] *without changing its type*: the engine's integer fields
    (``target_level``, ``chunksize``: usize) reject ``256.0``, so an int stays an int (bounds
    rounded inwards); a float must be finite."""
    if isinstance(x, int) and not isinstance(x, bool):
        return math.ceil(lo) if x < lo else math.floor(hi) if x > hi else x
    return _clamp(_finite(x, what), lo, hi)


def _is_gain_pointer(pointer: str) -> bool:
    return pointer.rsplit("/", 1)[-1] == "gain"


def _guard_gains(obj: Any, lo: float, hi: float, where: str) -> Any:
    """A copy of ``obj`` with every numeric ``gain`` (filter parameters, mixer sources) clamped
    to [lo, hi]. Other values pass through untouched (json still refuses NaN/inf)."""
    if isinstance(obj, dict):
        return {k: (_bounded(v, f"{where}/{k}", lo, hi) if k == "gain" and _is_number(v)
                    else _guard_gains(v, lo, hi, f"{where}/{k}"))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_guard_gains(v, lo, hi, f"{where}/{i}") for i, v in enumerate(obj)]
    return obj


def _gains_out_of_range(obj: Any, lo: float, hi: float, where: str = "") -> list[str]:
    """Paths of every ``gain`` outside [lo, hi] (or not finite) in a parsed config."""
    bad: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "gain" and _is_number(v):
                if not (math.isfinite(v) and lo <= v <= hi):
                    bad.append(f"{where}/{k}={v!r}")
            else:
                bad += _gains_out_of_range(v, lo, hi, f"{where}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad += _gains_out_of_range(v, lo, hi, f"{where}/{i}")
    return bad


def next_backoff(delay: float, backoff_max: float) -> float:
    """Doubling backoff, capped: a restarting engine is retried quickly, a dead one politely."""
    return min(delay * 2.0, backoff_max)


class CamillaClient:
    """Asyncio client for one CamillaDSP v5 websocket server.

    Lifecycle: ``await connect()`` starts a background task that keeps a connection alive
    (reconnecting with capped backoff) and fires ``on_connect(client)`` after every successful
    (re)connection, ``on_disconnect(client)`` after every loss. ``await close()`` stops it.

    Hooks are ``async def hook(client) -> None`` run as their own tasks (so they may call
    commands); exceptions are logged, never propagated. ``on_connect`` is cancelled if the link
    drops before it finishes, and runs again on the next connection: it must be idempotent
    (re-apply the whole state, as after a camilladsp restart that came up muted).
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        on_connect: Hook | None = None,
        on_disconnect: Hook | None = None,
        timeout: float = COMMAND_TIMEOUT_S,
        confirm_timeout: float = CONFIRM_TIMEOUT_S,
        backoff_min: float = 0.1,
        backoff_max: float = 2.0,
        ping_interval: float | None = 5.0,
        fader_min_db: float = ENGINE_FADER_MIN_DB,
        fader_max_db: float = DEFAULT_FADER_MAX_DB,
        gain_min_db: float = ENGINE_GAIN_MIN_DB,
        gain_max_db: float = DEFAULT_GAIN_MAX_DB,
        settle_s: float = RUNNING_SETTLE_S,
    ) -> None:
        if not ENGINE_FADER_MIN_DB <= fader_min_db < fader_max_db <= ENGINE_FADER_MAX_DB:
            raise ValueError("fader limits must satisfy -150 <= min < max <= +50 dB")
        if not ENGINE_GAIN_MIN_DB <= gain_min_db < gain_max_db <= ENGINE_GAIN_MAX_DB:
            raise ValueError("gain limits must satisfy -150 <= min < max <= +150 dB")
        self.url = url
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.timeout = timeout
        self.confirm_timeout = confirm_timeout
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        # Keepalive pings catch a *frozen* engine on an idle link (a dead process is seen at once).
        self.ping_interval = ping_interval
        self.fader_min_db = fader_min_db
        self.fader_max_db = fader_max_db
        self.gain_min_db = gain_min_db
        self.gain_max_db = gain_max_db
        self.settle_s = settle_s  # see RUNNING_SETTLE_S
        self.connect_count = 0  # successful connections so far (1 = first, 2+ = reconnects)

        self._ws: ClientConnection | None = None
        self._lock = asyncio.Lock()
        self._up = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._closing = False
        self._hook_task: asyncio.Task[None] | None = None
        self._side_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ lifecycle

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def connect(self, timeout: float | None = 5.0) -> bool:
        """Start the (re)connect loop and wait up to ``timeout`` s (None = forever) for a link.

        Never raises because the engine is absent: at boot camilladsp may not be up yet, and
        the controller must still draw ENGINE DOWN. Returns whether a link is up.
        """
        if self._runner is None or self._runner.done():
            self._closing = False
            self._runner = asyncio.create_task(self._run(), name="cdsp-link")
        return await self.wait_connected(timeout)

    async def wait_connected(self, timeout: float | None = None) -> bool:
        if self.connected:
            return True
        try:
            await asyncio.wait_for(self._up.wait(), timeout)
        except TimeoutError:
            return False
        return self.connected

    async def close(self) -> None:
        """Stop reconnecting and close the link. Idempotent."""
        self._closing = True
        ws = self._ws
        runner, self._runner = self._runner, None
        tasks = [t for t in (runner, self._hook_task, *self._side_tasks) if t and not t.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._drop(ws, "client closed")
        if ws is not None:
            try:
                await asyncio.wait_for(ws.wait_closed(), 1.0)
            except (TimeoutError, Exception):
                pass

    async def __aenter__(self) -> "CamillaClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _run(self) -> None:
        delay = self.backoff_min
        reported_down = False
        while not self._closing:
            try:
                ws = await ws_connect(
                    self.url,
                    open_timeout=self.timeout,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_interval,
                    close_timeout=1.0,
                    max_size=MAX_MESSAGE_BYTES,
                    compression=None,  # loopback: deflate only costs CPU on the Pi
                    proxy=None,  # websockets 15 honours *_PROXY env vars by default
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # refused, handshake timeout, bad URL...: keep trying
                if not reported_down:
                    log.warning("camilladsp unreachable at %s (%s); retrying", self.url, e)
                    reported_down = True
                else:
                    log.debug("reconnect to %s failed: %s", self.url, e)
                await asyncio.sleep(delay)
                delay = next_backoff(delay, self.backoff_max)
                continue

            delay = self.backoff_min
            reported_down = False
            self._ws = ws
            self.connect_count += 1
            self._up.set()
            log.info("connected to camilladsp at %s (connection #%d)", self.url, self.connect_count)
            if self.on_connect is not None:
                self._hook_task = self._spawn(self.on_connect, "on_connect")
            try:
                await ws.wait_closed()
            finally:
                self._drop(ws, "closed")
            if self._closing:
                break
            log.warning("lost connection to camilladsp at %s", self.url)
            # A re-apply running against a dead link is pointless; the next on_connect redoes it.
            if self._hook_task is not None and not self._hook_task.done():
                self._hook_task.cancel()
            if self.on_disconnect is not None:
                self._spawn(self.on_disconnect, "on_disconnect")
            await asyncio.sleep(delay)

    def _spawn(self, hook: Hook, label: str) -> asyncio.Task[None]:
        async def run() -> None:
            try:
                await hook(self)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s hook failed", label)

        task = asyncio.create_task(run(), name=f"cdsp-{label}")
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)
        return task

    def _drop(self, ws: ClientConnection | None, why: str) -> None:
        """Forget ``ws`` (if it is still current) and abort its transport without a closing
        handshake: the background loop sees it close and reconnects."""
        if ws is None:
            return
        if self._ws is ws:
            self._ws = None
            self._up.clear()
            log.debug("dropping camilladsp link: %s", why)
        transport = getattr(ws, "transport", None)
        if transport is not None:
            transport.abort()

    # ------------------------------------------------------------------ core

    async def command(self, name: str, timeout: float | None = None, **fields: Any) -> Any:
        """Send one v5 command and return its ``value`` (None for commands without one).

        Raises CdspError on an error result, CdspTimeout (a CdspUnavailable) when no reply
        comes within ``timeout`` (default ``self.timeout``), CdspUnavailable when there is no
        link, ValueError for unserialisable fields (NaN included). Do not wrap this in your own
        ``asyncio.wait_for``: cancelling a request in flight forces a reconnect.
        Config writes belong in ``set_value`` / ``patch_config`` / ``set_config_yaml``: a raw
        ``SetConfigValue`` sent here is not confirmed and can be lost (see ``_write_confirmed``).
        """
        async with self._lock:
            return await self._exchange(name, timeout, fields)

    async def _exchange(self, name: str, timeout: float | None, fields: dict[str, Any]) -> Any:
        """One request/reply. The caller holds ``self._lock``."""
        # allow_nan=False: refuse NaN/inf before anything reaches the engine.
        payload = json.dumps({"command": name, **fields}, allow_nan=False)
        limit = self.timeout if timeout is None else timeout
        ws = self._ws
        if ws is None:
            raise CdspUnavailable(f"{name}: not connected to camilladsp at {self.url}")
        try:
            async with asyncio.timeout(limit):
                await ws.send(payload)
                reply = await self._read_reply(ws, name)
        except TimeoutError:
            self._drop(ws, f"{name} timed out")
            raise CdspTimeout(f"{name}: no reply within {limit:g} s") from None
        except ConnectionClosed as e:
            self._drop(ws, f"{name}: connection closed")
            raise CdspUnavailable(f"{name}: connection to camilladsp lost") from e
        except asyncio.CancelledError:
            # The reply may still be on its way and would desync the next command.
            self._drop(ws, f"{name} cancelled in flight")
            raise
        result = reply.get("result")
        if result != "Ok":
            raise CdspError(name, str(result), reply.get("message"))
        return reply.get("value")

    async def _write_confirmed(
        self,
        name: str,
        fields: dict[str, Any],
        read: tuple[str, dict[str, Any]],
        applied: Callable[[Any], bool],
        timeout: float | None = None,
        confirm_s: float | None = None,
    ) -> None:
        """Send a config write, then hold the lock until a read-back shows it is active.

        Why: CamillaDSP 5.0.0-beta.1 replies Ok to SetConfigValue/PatchConfig/SetConfig
        *before* the active config is updated, and computes each write on the active config.
        Two writes to different parameters in quick succession therefore lose the first one
        (proven on the box: 5-15 % of raw 3-write bursts end with a write persistently lost,
        timing-dependent; 0 of 300 with this confirmation). Holding the lock until our
        write is visible makes every write start from a fresh base, and keeps our own poller
        from reading (and "adopting") the stale value in the window.
        """
        async with self._lock:
            await self._exchange(name, timeout, fields)
            await self._confirm(name, read, applied, confirm_s)

    async def _confirm(
        self,
        name: str,
        read: tuple[str, dict[str, Any]],
        applied: Callable[[Any], bool],
        confirm_s: float | None = None,
    ) -> None:
        """Poll ``read`` until ``applied(value)``. The caller holds ``self._lock``. Usually the
        first read already matches (~1 ms); the stale window seen on the box is up to ~60 ms."""
        window = self.confirm_timeout if confirm_s is None else confirm_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + window
        read_name, read_fields = read
        while True:
            if applied(await self._exchange(read_name, None, read_fields)):
                return
            if loop.time() > deadline:
                raise CdspNotApplied(
                    name, "NotApplied",
                    f"accepted, but the active config still differs after {window:g} s")
            await asyncio.sleep(CONFIRM_POLL_S)

    async def _read_reply(self, ws: ClientConnection, name: str) -> dict[str, Any]:
        while True:
            raw = await ws.recv()
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode("utf-8", "replace")
            try:
                reply = json.loads(raw)
            except ValueError:
                log.warning("ignoring malformed frame from camilladsp: %.80r", raw)
                continue
            if not isinstance(reply, dict):
                log.warning("ignoring non-object frame from camilladsp: %.80r", raw)
                continue
            tag = reply.get("reply")
            if tag == name:
                return reply
            if tag == "Invalid":  # the engine could not parse our request
                raise CdspError(name, "Invalid", reply.get("error"))
            # Pushed events (StateEvent, SignalLevelsEvent...) belong to subscriptions we never
            # open; anything else is not ours either. Skip it, keep waiting for our reply.
            log.debug("ignoring unsolicited %r while waiting for %s", tag, name)

    # ------------------------------------------------------------------ config

    async def get_value(self, pointer: str) -> Any:
        """Read one value of the active config by JSON Pointer, e.g. ``/filters/sub_delay/parameters/delay``."""
        return await self.command("GetConfigValue", value=_pointer(pointer))

    async def set_value(
        self, pointer: str, value: Any, clamp: tuple[float, float] | None = None
    ) -> Any:
        """Write one config value by JSON Pointer (applied at once, no pipeline rebuild for a
        parameter) and return once it is active (see ``_write_confirmed``). Returns the value
        actually sent.

        Numbers must be finite and keep their type (an int field such as ``target_level``
        rejects ``256.0``). ``clamp=(lo, hi)`` bounds a number here (the controller normally
        clamps to state.RANGES first). Whatever the caller passes, a ``.../gain`` pointer, and
        every ``gain`` inside a dict/list value, is clamped to [gain_min_db, gain_max_db]."""
        pointer = _pointer(pointer)
        if _is_number(value):
            if isinstance(value, float):
                value = _finite(value, pointer)
            if clamp is not None:
                if not clamp[0] <= clamp[1]:
                    raise ValueError(f"clamp bounds inverted: {clamp!r}")
                value = _bounded(value, pointer, *clamp)
            if _is_gain_pointer(pointer):  # last, so the safety ceiling always wins
                value = _bounded(value, pointer, self.gain_min_db, self.gain_max_db)
        elif isinstance(value, (dict, list)):
            value = _guard_gains(value, self.gain_min_db, self.gain_max_db, pointer)
        await self._write_confirmed(
            "SetConfigValue", {"pointer": pointer, "value": value},
            ("GetConfigValue", {"value": pointer}), lambda got: _matches(got, value))
        return value

    async def patch_config(self, patch: dict[str, Any]) -> None:
        """Merge a partial config (e.g. ``{"filters": {"x": {"parameters": {...}}}}``) into the
        active one, and return once it is active. Every numeric ``gain`` in the patch (filter
        parameters, mixer sources) is clamped to [gain_min_db, gain_max_db] first. The engine
        validates the result and refuses it whole if invalid."""
        if not isinstance(patch, dict):
            raise ValueError("patch must be a dict")
        patch = _guard_gains(patch, self.gain_min_db, self.gain_max_db, "")
        await self._write_confirmed(
            "PatchConfig", {"value": patch},
            ("GetConfigJson", {}), lambda got: _matches(_json_value(got), patch))

    async def set_config_yaml(self, yaml_text: str, timeout: float = SETUP_TIMEOUT_S) -> None:
        """Upload a complete config and return once it is the engine's active config, and, if
        it changes ``devices`` (or the engine was not running before), once the engine has
        run on the new devices for ``settle_s`` in a row. Setup actions only: a pipeline change
        may click.

        Success means "active, and running on its devices". The check is needed because the
        engine reports the new config, and even ``Running``, *before* it opens a new device;
        when the open fails it then exits (started from a file, the production unit) or goes
        ``Inactive`` (wait mode). Never persist a config as good unless this returned.

        Raises ``ValueError`` (nothing sent) when a ``gain`` is outside the gain limits: a whole
        config is refused, not rewritten. ``CdspError`` when the engine refuses the config
        (``ConfigReadError``...), ``CdspNotRunning`` when the engine did not run on the new
        devices within ``timeout``, ``CdspUnavailable`` when it exited meanwhile.
        """
        # The local parse feeds the gain guard and the confirmation; the engine stays the judge
        # of validity (bad YAML comes back as CdspError ConfigReadError with its message).
        try:
            wanted = yaml.safe_load(yaml_text)
        except yaml.YAMLError:
            wanted = None
        if not isinstance(wanted, dict):
            wanted = None
        if wanted is not None:
            bad = _gains_out_of_range(wanted, self.gain_min_db, self.gain_max_db)
            if bad:
                raise ValueError(f"config refused, gain outside [{self.gain_min_db:g}, "
                                 f"{self.gain_max_db:g}] dB: {', '.join(bad[:5])}")

        loop = asyncio.get_running_loop()
        async with self._lock:  # one hold: nobody writes between the snapshot and the upload
            before = _json_value(await self._exchange("GetConfigJson", None, {}))
            state_before = await self._exchange("GetState", None, {})
            t0 = loop.time()
            await self._exchange("SetConfig", timeout, {"value": yaml_text})
            if wanted is not None and _matches(before, wanted):
                active = before  # same content as active: a stale read-back cannot hurt
            else:
                active = await self._confirm_config(before, wanted, timeout)
        # Both sides are engine read-backs (every optional field filled), so != is exact.
        # active None = the engine dropped the upload: the running check reports why.
        devices_changed = (not isinstance(before, dict) or not isinstance(active, dict)
                           or before.get("devices") != active.get("devices"))
        if devices_changed or state_before not in RUNNING_STATES:
            # Outside the lock: the config is active, so a mute or a fader move may go through
            # while we watch the engine open its devices.
            left = timeout - (loop.time() - t0)
            await self._await_running(left if left > 2 * self.settle_s else 2 * self.settle_s)

    async def _confirm_config(self, before: Any, wanted: dict[str, Any] | None,
                              timeout: float) -> Any:
        """After SetConfig: poll GetConfigJson until the upload is the active config, and return
        that read-back. The caller holds ``self._lock``.

        Returns None when the engine has no config any more. In wait mode (-w) a device that
        fails to open makes the engine drop the upload (GetConfigJson reads null ~5 ms after
        SetConfig, seen on the box). With a config before, a null means exactly that. Without
        one (first upload), a null may also be the stale pre-upload read, so it counts only
        once GetState has read Inactive for ``settle_s``.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        inactive_since: float | None = None
        while True:
            active = _json_value(await self._exchange("GetConfigJson", None, {}))
            if active is not None:
                # Reads back as uploaded, or at least moved off the old config (covers a
                # harmless normalisation difference that _matches does not know about).
                if (wanted is not None and _matches(active, wanted)) or active != before:
                    return active
            elif before is not None:
                return None  # taken, then dropped
            else:
                now = loop.time()
                if await self._exchange("GetState", None, {}) != "Inactive":
                    inactive_since = None
                elif inactive_since is None:
                    inactive_since = now
                elif now - inactive_since >= self.settle_s:
                    return None
            if loop.time() > deadline:
                raise CdspNotApplied(
                    "SetConfig", "NotApplied",
                    f"accepted, but the active config still differs after {timeout:g} s")
            await asyncio.sleep(CONFIRM_POLL_S)

    async def _await_running(self, timeout: float) -> None:
        """Return once GetState has read Running/Paused for ``settle_s`` in a row. Raise
        CdspNotRunning when it reads Inactive for ``settle_s`` in a row (a single Inactive may
        be the stale pre-upload state) or the timeout passes first; CdspUnavailable propagates
        when the engine exits."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        state, since = None, loop.time()
        while True:
            now_state = await self.get_state()
            now = loop.time()
            if now_state != state:
                state, since = now_state, now
            if state in RUNNING_STATES and now - since >= self.settle_s:
                return
            if (state == "Inactive" and now - since >= self.settle_s) or now > deadline:
                reason = await self.get_stop_reason()
                raise CdspNotRunning("SetConfig", "NotRunning",
                                     f"engine {state} on the new devices, stop reason {reason!r}")
            await asyncio.sleep(RUNNING_POLL_S)

    async def get_config_json(self) -> dict[str, Any]:
        """The active config, every optional field filled in (the engine sends a JSON string).

        Raises CdspNoConfig when the engine has none (wait mode ``-w``, or after a failed
        device open there: GetConfigJson answers Ok with the string ``"null"``, seen on the
        box); the controller should then upload its config."""
        value = _json_value(await self.command("GetConfigJson"))
        if value is None:
            raise CdspNoConfig("GetConfigJson", "NoActiveConfig",
                               "the engine has no active config; upload one with set_config_yaml")
        return value

    # ------------------------------------------------------------------ faders

    def _fader_index(self, idx: int) -> int:
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < FADER_COUNT:
            raise ValueError(f"fader index must be 0..{FADER_COUNT - 1} (0 = Main), got {idx!r}")
        return idx

    def clamp_fader_db(self, db: float) -> float:
        return _clamp(_finite(db, "fader volume"), self.fader_min_db, self.fader_max_db)

    async def get_faders(self) -> list[Fader]:
        """Main then Aux1..Aux4: ``[{"volume": dB, "mute": bool}, ...]``."""
        value = await self.command("GetFaders")
        return [Fader(volume=float(f["volume"]), mute=bool(f["mute"])) for f in value]

    async def set_fader(self, idx: int, db: float) -> float:
        """Set a fader (the engine ramps it). Returns the clamped dB actually sent."""
        idx = self._fader_index(idx)
        db = self.clamp_fader_db(db)
        await self.command("SetFaderVolume", fader=idx, value=db)
        return db

    async def adjust_fader(
        self, idx: int, delta: float, min: float | None = None, max: float | None = None
    ) -> float:
        """Nudge a fader by ``delta`` dB in the engine (AdjustFaderVolume: no read-modify-write
        race with CamillaGUI on the value itself). Returns the new volume.

        Bounds (``min``/``max``, themselves bounded to the client's fader limits; default the
        client limits):
        * ``max`` is a ceiling: a fader above it is pulled down to it, whatever the sign of
          ``delta`` (the safe direction: a phone-set +6 dB comes back to the knob's 0 dB).
        * ``min`` is a floor for a fader moving down *through* it. It never raises a fader
          that already sits below it: the engine would clamp a -150 dB Aux up to ``min`` on a
          +0.5 dB detent (+144 dB, seen on the box). So when the floor is above the engine's
          -150 dB, the fader is read first and the floor sent is lowered to where the fader
          is (minus a negative delta, so turning down keeps working down there).

        Guarantee: the result never exceeds ``max(current + delta, level read)``. The read and
        the adjust are one lock hold (~1 ms); only another client (CamillaGUI) moving the same
        fader inside that window can make the floor lift it, and then never above the level
        it had when read.
        """
        idx = self._fader_index(idx)
        delta = _finite(delta, "fader delta")
        lo = self.fader_min_db if min is None else self.clamp_fader_db(min)
        hi = self.fader_max_db if max is None else self.clamp_fader_db(max)
        if lo > hi:
            raise ValueError(f"adjust_fader bounds inverted: min {lo} > max {hi}")
        async with self._lock:
            floor = lo
            if lo > ENGINE_FADER_MIN_DB:  # at the engine floor nothing can sit below it
                got = await self._exchange("GetFaderVolume", None, {"fader": idx})
                current = float(got[1])  # [fader_index, volume_dB]
                if current < lo:
                    floor = current + delta if delta < 0 else current
                    # the engine does not clamp an explicit min to its own -150 dB floor
                    floor = ENGINE_FADER_MIN_DB if floor < ENGINE_FADER_MIN_DB else floor
            value = await self._exchange(
                "AdjustFaderVolume", None,
                {"fader": idx, "value": delta, "min": floor, "max": hi})
        return float(value[1])  # [fader_index, new_volume_dB]

    async def set_fader_mute(self, idx: int, mute: bool) -> None:
        idx = self._fader_index(idx)
        await self.command("SetFaderMute", fader=idx, value=bool(mute))

    # ------------------------------------------------------------------ status

    async def get_version(self) -> str:
        return str(await self.command("GetVersion"))

    async def get_state(self) -> str:
        """``Running`` | ``Paused`` | ``Inactive`` | ``Starting`` | ``Stalled``."""
        return str(await self.command("GetState"))

    async def get_stop_reason(self) -> str | dict[str, Any]:
        """``None`` (still running), ``Done``, ``CaptureError``, ... As sent by the engine:
        reasons that carry data (format changes) may come as a one-key object."""
        return await self.command("GetStopReason")

    async def get_processing_load(self) -> float:
        """Pipeline load in percent."""
        return float(await self.command("GetProcessingLoad"))

    async def get_signal_levels(self) -> dict[str, list[float]]:
        """Last chunk's ``playback_rms``, ``playback_peak``, ``capture_rms``, ``capture_peak``
        (dB per channel; -200 = silence or muted)."""
        value = await self.command("GetSignalLevels")
        return {k: [float(x) for x in v] for k, v in value.items()}

    async def get_clipped_samples(self) -> int:
        """Clipped samples since start or the last reset (a new config does not reset it)."""
        return int(await self.command("GetClippedSamples"))

    async def reset_clipped_samples(self) -> None:
        await self.command("ResetClippedSamples")


def _pointer(pointer: str) -> str:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError(f"JSON Pointer must start with '/', got {pointer!r}")
    return pointer


def _json_value(value: Any) -> Any:
    """GetConfigJson carries the config as a JSON *string*; accept an object too."""
    return json.loads(value) if isinstance(value, str) else value


def _matches(got: Any, want: Any) -> bool:
    """Does the engine's read-back ``got`` show what we wrote (``want``)?

    Subset semantics for objects (the engine fills every optional field), numbers compared as
    floats (we send 8834, it stores 8834.0), and ``None`` in ``want`` means "engine default",
    so it is not checked.
    """
    if want is None:
        return True
    if isinstance(want, bool):
        return got is want
    if isinstance(want, (int, float)):
        return (isinstance(got, (int, float)) and not isinstance(got, bool)
                and float(got) == float(want))
    if isinstance(want, dict):
        return isinstance(got, dict) and all(
            v is None or (k in got and _matches(got[k], v)) for k, v in want.items())
    if isinstance(want, list):
        return (isinstance(got, list) and len(got) == len(want)
                and all(_matches(g, w) for g, w in zip(got, want)))
    return got == want

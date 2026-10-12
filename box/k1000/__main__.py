"""`python3 -m k1000`: the K1000 control service (knobs, screen, state, persistence).

Wiring only; the behaviour lives in `control.Controller`. What this file guarantees:

- It starts whatever is there and keeps trying for the rest: no screen yet (the panel's fbtft
  probe can lose the boot race) -> retried every HW_RETRY_S; knobs that do not answer (loose
  ribbon, an I2C glitch at boot) -> retried every HW_RETRY_S, and the screen says KNOBS OFFLINE
  meanwhile (B2 mute does nothing then); no engine -> ENGINE DOWN until it appears. Nothing here
  may stop the controller from running, and the audio never waits for the UI.
- It never mutes on the way out. SIGTERM/SIGINT persist the state and leave the engine exactly as
  it is: audio must survive a k1000 restart (docs/03 §2 "Audio never depends on the UI").
- Logs go to stdout without timestamps (journald adds them).

Test/bench flags: `--headless` (no framebuffer), `--no-hw` (no MCP23017 knobs), `--stdin` (one
command per line on stdin, replies on stdout prefixed with '@'; see `COMMANDS`), and
`--test-devices` (SignalGenerator -> File /dev/null instead of the ALSA card: engine tests only,
never for a show).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .cdsp import CamillaClient
from .control import FADER_CEILING_DB, Controller, EngineSettings, Paths, recover_state
from .profiles import (SETTINGS_PATH, ProfileError, card_present, choose_profile, load_profile,
                       load_settings)
from .ui.input_map import Nudge, apply_focus

log = logging.getLogger("k1000")

RENDER_MAX_FPS = 15.0
RENDER_IDLE_S = 1.0      # re-render at least this often (hints/highlights wake it on expiry)
HW_RETRY_S = 2.0         # screen / knobs not there: try again this often
FORCE_WRITE_S = 5.0      # rewrite the panel even if the frame did not change (heals overdraw)
KNOBS_ALARM_S = 1.0      # knobs unhealthy this long -> KNOBS OFFLINE (a retried glitch is not)

COMMANDS = """\
nudge <field> <detents> [fine]   set <field> <json value> [confirm]   toggle <field> [confirm]
mute on|off|toggle   reset_live   page <MAIN|LIVE|SETUP|HWTEST>   next_page   hint <text>
ev enc <0-3> <detents> | ev sw <0-7> <press|release|short|long>   (as if from the knobs)
status   state   save   settle   help   quit"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m k1000", description="K1000 control service")
    p.add_argument("--settings", default=SETTINGS_PATH, help="k1000.yaml (default %(default)s)")
    p.add_argument("--state", help="state.json path (overrides k1000.yaml paths.state)")
    p.add_argument("--active-config", help="active.yml path (overrides paths.active_config)")
    p.add_argument("--url", help="engine websocket (overrides engine.url)")
    p.add_argument("--headless", action="store_true", help="no screen")
    p.add_argument("--no-hw", action="store_true", help="no knobs/buttons (MCP23017)")
    p.add_argument("--stdin", action="store_true", help="read commands from stdin (bench/tests)")
    p.add_argument("--test-devices", action="store_true",
                   help="SignalGenerator -> File /dev/null devices (engine tests, never a show)")
    p.add_argument("--test-level", type=float, default=-20.0, help="test sine level, dBFS")
    p.add_argument("--log-level", default=os.environ.get("K1000_LOG", "INFO"))
    p.add_argument("--version", action="version", version=f"k1000 {__version__}")
    return p


def setup_logging(level: str) -> None:
    logging.basicConfig(stream=sys.stdout, level=getattr(logging, str(level).upper(), logging.INFO),
                        format="%(levelname)s %(name)s: %(message)s", force=True)
    logging.getLogger("websockets").setLevel(logging.WARNING)


# =========================================================================== screen
def open_screen(quiet: bool = False) -> Any:
    """The ZynScreen framebuffer, or None when there is none (yet). `quiet`: a retry, log at
    debug level only.

    Only the framebuffer named fb_ili9486 is the panel; there is no /dev/fb0 fallback. Before the
    panel registers (~7 s into the boot), fb0 is either absent or, with an HDMI monitor plugged
    in, vc4's HDMI framebuffer: taking it would draw the UI there for the whole uptime (the
    screen is never looked for again) and leave the panel dark."""
    lvl = logging.DEBUG if quiet else logging.WARNING
    try:
        from .ui.display import Framebuffer, find_fb_device
        dev = find_fb_device()
        if dev is None:
            log.log(lvl, "no ZynScreen framebuffer (fb_ili9486) yet: retrying every %g s", HW_RETRY_S)
            return None
        if not Path(dev).exists():
            log.log(lvl, "no framebuffer (%s) yet: retrying every %g s", dev, HW_RETRY_S)
            return None
        fb = Framebuffer(dev)
        fb.unbind_console()     # root's ExecStartPre normally did it already
        fb.backlight(True)
        log.info("screen on %s", dev)
        return fb
    except Exception as e:   # noqa: BLE001 - no screen is never fatal
        log.log(lvl, "screen unavailable (%s): retrying every %g s", e, HW_RETRY_S)
        return None


async def screen_task(ctl: Controller, hw: dict[str, Any], retry_s: float = HW_RETRY_S) -> None:
    """The screen for the whole life of the process: a panel that registers late (or not at
    all) is retried, never given up on (a dark UI for a whole gig is not an option)."""
    tries = 0
    while hw.get("fb") is None:
        fb = open_screen(tries > 0)
        tries += 1
        if fb is None:
            await asyncio.sleep(retry_s)
            continue
        hw["fb"] = fb
        if tries > 1:
            log.warning("screen found after %d tries", tries)
    await render_loop(ctl, hw["fb"])


def _draw(fb: Any, page: str, state: Any, status: Any, focus: Any) -> None:
    from .ui import pages
    fb.show(pages.render(page, state, status, focus))


def _stop_frame(fb: Any) -> None:
    """What the panel shows while k1000 is down: honest, and says the audio is untouched."""
    try:
        from PIL import Image, ImageDraw
        from .ui import pages
        img = Image.new("RGB", pages.SIZE, pages.BG)
        d = ImageDraw.Draw(img)
        d.text((pages.W // 2, 110), "K1000 CONTROL", font=pages.font(40), fill=pages.AMBER, anchor="mm")
        d.text((pages.W // 2, 160), "restarting…", font=pages.font(30), fill=pages.FG, anchor="mm")
        d.text((pages.W // 2, 220), "audio unchanged · knobs inactive", font=pages.font(22, pages.FONT_REG),
               fill=pages.DIM, anchor="mm")
        fb.show(img)
    except Exception as e:   # noqa: BLE001
        log.debug("stop frame failed: %s", e)


def _next_wake(focus: Any, now: float) -> float:
    """Seconds until the next frame that would differ without any change: a hint, an edit
    highlight or a SETUP confirm expiring (+10 ms), capped at RENDER_IDLE_S."""
    out = RENDER_IDLE_S
    for attr in ("hint_until", "edit_until", "confirm_until"):
        t = getattr(focus, attr, 0.0)
        if isinstance(t, (int, float)) and t > now:
            out = min(out, t - now + 0.01)
    return max(0.0, out)


async def render_loop(ctl: Controller, fb: Any) -> None:
    """Re-render on change, at most RENDER_MAX_FPS (meters change ~10 times a second), and at
    least every RENDER_IDLE_S. PIL work runs in a thread so knob events stay responsive."""
    from .ui import pages
    loop = asyncio.get_running_loop()
    try:
        ms = await asyncio.to_thread(pages.warm_up)
        log.info("screen warm-up %.0f ms", ms)
    except Exception as e:   # noqa: BLE001
        log.warning("screen warm-up failed: %s", e)
    min_dt = 1.0 / RENDER_MAX_FPS
    last = 0.0
    failed = False
    wait = 0.0
    forced = loop.time()
    while True:
        # Woken by a change, by the next hint/highlight/confirm expiry (the only things the
        # pages draw from the clock), else every RENDER_IDLE_S. An unchanged frame is never
        # rendered at meter rate (that alone cost ~15 % of a core).
        await ctl.wait_changed(wait)
        dt = loop.time() - last
        if dt < min_dt:
            await asyncio.sleep(min_dt - dt)
        last = loop.time()
        if last - forced >= FORCE_WRITE_S and hasattr(fb, "invalidate"):
            # Framebuffer.show skips a frame identical to the last one written; anything that
            # drew over the panel meanwhile (fbcon bound because the panel registered after
            # the root unbind, a glitch) would stay until the UI changes.
            fb.invalidate()
            forced = last
        page, st, status, focus = ctl.snapshot()
        wait = _next_wake(focus, time.monotonic())
        try:
            await asyncio.to_thread(_draw, fb, page, st, status, focus)
            failed = False
        except Exception as e:   # noqa: BLE001 - the screen must never stop the controller
            if not failed:
                log.exception("render failed: %s", e)
            failed = True


# =========================================================================== knobs
def open_controls(settings: dict[str, Any], quiet: bool = False) -> Any:
    """The MCP23017 knobs, or None when they do not answer (logged once; `knobs_task` retries).
    A half-opened driver is closed again (its INT lines and bus released for the retry)."""
    controls = None
    try:
        from .hw.mcp23017 import ZynthianControls
        controls = ZynthianControls()
        controls.start()
        log.info("knobs on i2c-1 0x20 (irq=%s)", getattr(controls, "irq", "?"))
        return controls
    except Exception as e:   # noqa: BLE001 - ControlsError, ImportError, PermissionError...
        log.log(logging.DEBUG if quiet else logging.ERROR,
                "knobs unavailable (%s): retrying every %g s", e, HW_RETRY_S)
        if controls is not None:
            try:
                controls.close()
            except Exception:   # noqa: BLE001
                pass
        return None


async def watch_knobs(ctl: Controller, controls: Any, period: float = 0.25,
                      alarm_s: float = KNOBS_ALARM_S) -> None:
    """KNOBS OFFLINE on the screen while the driver reports I2C trouble for > alarm_s (it
    retries and recovers by itself; a glitch it heals within that time stays silent)."""
    loop = asyncio.get_running_loop()
    bad_since: float | None = None
    while True:
        healthy = bool(getattr(controls, "healthy", True)) and not getattr(controls, "closed", False)
        now = loop.time()
        if healthy:
            bad_since = None
            ctl.set_knobs_ok(True)
        else:
            bad_since = now if bad_since is None else bad_since
            if now - bad_since >= alarm_s:
                ctl.set_knobs_ok(False)
        await asyncio.sleep(period)


async def knobs_task(ctl: Controller, settings: dict[str, Any], hw: dict[str, Any],
                     retry_s: float = HW_RETRY_S,
                     opener: Callable[..., Any] = open_controls) -> None:
    """The knobs for the whole life of the process: opened (retried every `retry_s` while the
    MCP23017 does not answer, KNOBS OFFLINE on the screen meanwhile), events fed to the
    controller, health watched; if the event stream ever fails, closed and reopened."""
    tries = 0
    while True:
        controls = hw.get("controls")
        if controls is None:
            # In the loop thread, like the first open always was: start() binds the loop.
            controls = opener(settings, tries > 0)
            tries += 1
            if controls is None:
                ctl.set_knobs_ok(False)
                await asyncio.sleep(retry_s)
                continue
            hw["controls"] = controls
            if tries > 1:
                log.warning("knobs available after %d tries", tries)
        tries = 0
        ctl.set_knobs_ok(True)
        watch = asyncio.get_running_loop().create_task(watch_knobs(ctl, controls),
                                                       name="k1000-knobs-watch")
        try:
            await input_loop(ctl, controls)
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)
        # input_loop only returns when the event stream failed: start over
        ctl.set_knobs_ok(False)
        hw["controls"] = None
        try:
            controls.close()
        except Exception:   # noqa: BLE001
            pass
        await asyncio.sleep(retry_s)


async def input_loop(ctl: Controller, controls: Any) -> None:
    from .hw.mcp23017 import ControlsClosed, ControlsError
    while True:
        try:
            ev = await controls.next_event()
        except ControlsClosed:
            return
        except ControlsError as e:
            log.error("knobs failed (%s): reopening them", e)
            return
        if ev is None:
            continue
        try:
            ctl.handle_event(ev)
        except Exception:   # noqa: BLE001 - one bad event must not kill the knobs
            log.exception("event %r failed", ev)


# =========================================================================== stdin console
def _reply(kind: str, payload: Any = None) -> None:
    line = f"@{kind}" if payload is None else f"@{kind} {json.dumps(payload, default=str)}"
    print(line, flush=True)


async def run_command(ctl: Controller, line: str, stop: asyncio.Event) -> None:
    """One console command (see COMMANDS). Replies '@ok <cmd>' / '@err <why>' / '@status {...}'."""
    parts = line.strip().split()
    if not parts:
        return
    cmd, args = parts[0].lower(), parts[1:]
    try:
        if cmd == "nudge":
            v = ctl.nudge(args[0], float(args[1]), len(args) > 2 and args[2] == "fine")
            if v is not None:    # the edit highlight, as a knob turn shows it
                apply_focus(ctl.focus, Nudge(args[0], 0))
            _reply("ok" if v is not None else "err", {"cmd": cmd, "field": args[0], "value": v})
        elif cmd == "ev":
            # A knob event, exactly as the MCP23017 driver would deliver it (slot/item select,
            # held pushes = FINE, B1..B4 with their press kinds): bench tests of the real UI.
            from .hw.mcp23017 import EncoderEvent, SwitchEvent
            kind, idx = args[0].lower(), int(args[1])
            if kind == "enc":
                ev: Any = EncoderEvent(idx, int(args[2]), time.monotonic())
            elif kind == "sw" and args[2] in ("press", "release", "short", "long"):
                ev = SwitchEvent(idx, args[2], time.monotonic())
            else:
                raise ValueError(f"ev {kind} {args[2:]}: expected 'enc <i> <d>' or 'sw <i> <kind>'")
            ctl.handle_event(ev)
            _reply("ok", {"cmd": cmd, "page": ctl.page, "slot": ctl.focus.slot,
                          "item": ctl.focus.item, "held": sorted(ctl.focus.held)})
        elif cmd == "set":
            try:
                value = json.loads(args[1])
            except ValueError:
                value = args[1]
            v = ctl.set_value(args[0], value, confirmed=len(args) > 2 and args[2] == "confirm")
            _reply("ok", {"cmd": cmd, "field": args[0], "value": v})
        elif cmd == "toggle":
            ok = ctl.toggle(args[0], confirmed=len(args) > 1 and args[1] == "confirm")
            _reply("ok" if ok else "err", {"cmd": cmd, "field": args[0], "value": ctl.state.get(args[0])})
        elif cmd == "mute":
            arg = args[0] if args else "toggle"
            on = ctl.set_mute(True if arg == "on" else False if arg == "off" else "toggle")
            _reply("ok", {"cmd": cmd, "muted": on})
        elif cmd == "reset_live":
            ctl.reset_live()
            _reply("ok", {"cmd": cmd})
        elif cmd == "page":
            _reply("ok", {"cmd": cmd, "page": ctl.goto_page(args[0].upper())})
        elif cmd == "next_page":
            _reply("ok", {"cmd": cmd, "page": ctl.next_page()})
        elif cmd == "hint":
            ctl.show_hint(" ".join(args))
            _reply("ok", {"cmd": cmd})
        elif cmd == "status":
            st = ctl.snapshot()[2]          # what the screen would show (ready included)
            _reply("status", {**st.__dict__, "page": ctl.page, "engine_ready": ctl.engine_ready})
        elif cmd == "state":
            _reply("state", ctl.state.to_dict())
        elif cmd == "save":
            _reply("ok" if ctl.save_now() else "err", {"cmd": cmd})
        elif cmd == "settle":
            ok = await ctl.settle(timeout=float(args[0]) if args else 10.0)
            _reply("ok" if ok else "err", {"cmd": cmd})
        elif cmd == "help":
            _reply("help", COMMANDS)
        elif cmd in ("quit", "exit"):
            _reply("ok", {"cmd": cmd})
            stop.set()
        else:
            _reply("err", {"cmd": cmd, "why": "unknown command", "help": COMMANDS})
    except (IndexError, ValueError, KeyError, TypeError) as e:
        _reply("err", {"cmd": cmd, "why": f"{type(e).__name__}: {e}"})


async def stdin_loop(ctl: Controller, stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    try:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    except (OSError, ValueError) as e:
        log.warning("--stdin: cannot read stdin (%s)", e)
        return
    while True:
        raw = await reader.readline()
        if not raw:
            return      # EOF: keep running (systemd gives /dev/null)
        try:
            await run_command(ctl, raw.decode("utf-8", "replace"), stop)
        except Exception:   # noqa: BLE001
            log.exception("command %r failed", raw)


# =========================================================================== main
async def run(args: argparse.Namespace) -> int:
    settings = load_settings(args.settings)
    paths = Paths.from_settings(settings, state=args.state, active_config=args.active_config)
    # A torn state.json is rebuilt from active.yml (the tune the engine boots from) BEFORE
    # anything writes either file.
    state, recovered = recover_state(paths)
    choice = choose_profile(settings, state.profile)
    try:
        profile = await asyncio.to_thread(load_profile, choice.name, settings)
    except ProfileError as e:      # cannot happen for a chosen name; never crash-loop on it
        log.error("profile %s: %s; using dev", choice.name, e)
        profile = await asyncio.to_thread(load_profile, "dev", settings)
    eng = EngineSettings.from_settings(settings, url=args.url)
    log.info("k1000 %s: profile %s (%s), engine %s, state %s", __version__, profile.name,
             choice.source, eng.url, paths.state)

    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    probe: Callable[[str], bool | None] | None = card_present
    if args.test_devices:
        from .dspconfig import signal_generator_variant
        level = args.test_level

        def transform(cfg: dict[str, Any]) -> dict[str, Any]:
            return signal_generator_variant(cfg, level_db=level)

        probe = None    # virtual devices: always present
        log.warning("--test-devices: SignalGenerator -> /dev/null, the ALSA card is not used")

    client = CamillaClient(eng.url, fader_max_db=FADER_CEILING_DB)
    ctl = Controller(client, profile, state, paths, settings=settings, profile_choice=choice,
                     boot_gain_db=eng.boot_gain_db, boot_ramp_s=eng.boot_ramp_s,
                     transform=transform, device_probe=probe)
    if recovered:
        ctl.show_hint(recovered, 15.0)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    hw: dict[str, Any] = {"fb": None, "controls": None}
    await ctl.start()
    ui: list[asyncio.Task[Any]] = []          # screen + knobs: kept until the controller stopped
    if not args.headless:
        ui.append(loop.create_task(screen_task(ctl, hw), name="k1000-screen"))
    if not args.no_hw:
        ui.append(loop.create_task(knobs_task(ctl, settings, hw), name="k1000-knobs"))
    console = loop.create_task(stdin_loop(ctl, stop), name="k1000-stdin") if args.stdin else None

    await stop.wait()
    log.info("stopping: saving the state, leaving the audio as it is")
    await _cancel([console] if console is not None else [])
    # The screen keeps showing (and B2 keeps muting) while stop() lets a ramp/guard in flight
    # finish: a frozen 'MUTED' while the PA ramps up would say the opposite of the truth.
    await ctl.stop()
    await _cancel(ui)
    fb = hw.get("fb")
    if fb is not None:
        _stop_frame(fb)
        fb.close()
    await client.close()
    controls = hw.get("controls")
    if controls is not None:
        controls.close()
    log.info("stopped")
    return 0


async def _cancel(tasks: list[asyncio.Task[Any]]) -> None:
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except BaseException:   # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

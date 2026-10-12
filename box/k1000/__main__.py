"""`python3 -m k1000`: the K1000 control service (knobs, screen, state, persistence).

Wiring only; the behaviour lives in `control.Controller`. What this file guarantees:

- It starts whatever is there: no screen -> headless, no knobs -> no knobs (both logged), no
  engine -> ENGINE DOWN until it appears. Nothing here may stop the controller from running.
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
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .cdsp import CamillaClient
from .control import FADER_CEILING_DB, Controller, EngineSettings, Paths, recover_state
from .profiles import (SETTINGS_PATH, ProfileError, card_present, choose_profile, load_profile,
                       load_settings)

log = logging.getLogger("k1000")

RENDER_MAX_FPS = 15.0
RENDER_IDLE_S = 0.2      # re-render at least this often (hint / edit-highlight expiry)

COMMANDS = """\
nudge <field> <detents> [fine]   set <field> <json value> [confirm]   toggle <field> [confirm]
mute on|off|toggle   reset_live   page <MAIN|LIVE|SETUP|HWTEST>   next_page   hint <text>
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
def open_screen() -> Any:
    """The ZynScreen framebuffer, or None (headless) when there is none."""
    try:
        from .ui.display import Framebuffer, find_fb_device
        dev = find_fb_device() or "/dev/fb0"
        if not Path(dev).exists():
            log.warning("no framebuffer (%s): running headless", dev)
            return None
        fb = Framebuffer(dev)
        fb.unbind_console()     # root's ExecStartPre normally did it already
        fb.backlight(True)
        log.info("screen on %s", dev)
        return fb
    except Exception as e:   # noqa: BLE001 - no screen is never fatal
        log.warning("screen unavailable (%s): running headless", e)
        return None


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
    while True:
        await ctl.wait_changed(RENDER_IDLE_S)
        dt = loop.time() - last
        if dt < min_dt:
            await asyncio.sleep(min_dt - dt)
        last = loop.time()
        page, st, status, focus = ctl.snapshot()
        try:
            await asyncio.to_thread(_draw, fb, page, st, status, focus)
            failed = False
        except Exception as e:   # noqa: BLE001 - the screen must never stop the controller
            if not failed:
                log.exception("render failed: %s", e)
            failed = True


# =========================================================================== knobs
def open_controls(settings: dict[str, Any]) -> Any:
    """The MCP23017 knobs, or None when absent (logged; the box then runs without knobs)."""
    try:
        from .hw.mcp23017 import ZynthianControls
        controls = ZynthianControls()
        controls.start()
        log.info("knobs on i2c-1 0x20 (irq=%s)", getattr(controls, "irq", "?"))
        return controls
    except Exception as e:   # noqa: BLE001 - ControlsError, ImportError, PermissionError...
        log.warning("knobs unavailable (%s): running without knobs", e)
        return None


async def input_loop(ctl: Controller, controls: Any) -> None:
    from .hw.mcp23017 import ControlsClosed, ControlsError
    while True:
        try:
            ev = await controls.next_event()
        except ControlsClosed:
            return
        except ControlsError as e:
            log.error("knobs failed (%s): running without knobs", e)
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
            _reply("ok" if v is not None else "err", {"cmd": cmd, "field": args[0], "value": v})
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
            st = ctl.status.copy()
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

    fb = None if args.headless else open_screen()
    controls = None if args.no_hw else open_controls(settings)
    await ctl.start()
    tasks: list[asyncio.Task[Any]] = []
    if controls is not None:
        tasks.append(loop.create_task(input_loop(ctl, controls), name="k1000-input"))
    if fb is not None:
        tasks.append(loop.create_task(render_loop(ctl, fb), name="k1000-render"))
    if args.stdin:
        tasks.append(loop.create_task(stdin_loop(ctl, stop), name="k1000-stdin"))

    await stop.wait()
    log.info("stopping: saving the state, leaving the audio as it is")
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except BaseException:   # noqa: BLE001
            pass
    await ctl.stop()
    if fb is not None:
        _stop_frame(fb)
        fb.close()
    await client.close()
    if controls is not None:
        controls.close()
    log.info("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

# SPDX-License-Identifier: GPL-3.0-or-later
"""SETUP > Laptop access (docs/06-phone.md §11): the Friday tune's way in over the K1000 Wi-Fi.

At the rig the laptop reaches the box only over the K1000 Wi-Fi, whose fence keeps CamillaGUI
(5005, no authentication) and SSH (22) away from phones. Laptop access opens those two ports, for
the AP's own subnet only, for 2 hours at most.

Why it is split this way (least privilege):

- **k1000 asks, root decides.** The k1000 service runs as user k1000 with a capability bounding
  set of CAP_NET_BIND_SERVICE alone, so it cannot change a firewall, and it cannot use sudo
  either (sudo needs capabilities the bounding set removed). It does not try. It only creates a
  flag file in its own runtime directory (`/run/k1000/laptop-open`). A root path unit
  (`k1000-laptop.path`) sees the flag and starts `k1000-laptop.service`, which runs
  `/opt/k1000/net/k1000-laptop-access.sh`: it inserts ONE tagged rule in the fence's own table
  (tcp 22 and 5005, from 10.42.0.0/24 to the AP address, on the AP interface), keeps it while the
  flag exists, and removes it on any way out. k1000 cannot choose the ports, the subnet or the
  duration: the request carries nothing.
- **Never persistent.** The rule lives only in the running ruleset (the fence file never holds
  it), the flag lives in /run (tmpfs): a reboot starts closed. Root closes it by itself after
  2 hours (counted on the boot clock, immune to a clock set by NTP mid-show), when the flag goes
  (k1000 closed it, or k1000 stopped: systemd removes /run/k1000 with the service), and when the
  service stops for any reason (ExecStopPost). k1000 never needs to be alive to close it.
- **The box shows what root did.** Root writes `/run/k1000-laptop/status` (state, the deadline
  on the boot clock, why it closed). The countdown pill, the SETUP value and the PHONE page's
  password text follow that file, not k1000's own wish: a refused or failed opening says so.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

FLAG = Path("/run/k1000/laptop-open")
STATUS = Path("/run/k1000-laptop/status")
UPTIME = Path("/proc/uptime")
LAPTOP_S = 7200.0             # what root enforces (k1000-laptop-access.sh): shown, not decided here
ANSWER_S = 6.0                # no answer from root within this: the opening failed
POLL_S = 1.0
PORTS = (22, 5005)

CLOSED, OPENING, OPEN, CLOSING, UNAVAILABLE = "closed", "opening", "open", "closing", "unavailable"

REASONS = {
    "box": "closed",
    "time": "closed (2 h)",
    "k1000": "closed (k1000 stopped)",
    "stopped": "closed",
    "no-fence": "no Wi-Fi fence (AP not set up)",
    "no-ap": "no AP settings (setup-ap.sh)",
    "error": "failed (see log)",
}


def read_uptime(path: Path = UPTIME) -> float | None:
    """Seconds since boot (CLOCK_BOOTTIME, the clock root's deadline uses), or None."""
    try:
        return float(path.read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_status(path: Path = STATUS) -> dict[str, str]:
    """Root's key=value status file; {} when absent or unreadable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines()[:20]:
        k, sep, v = line.partition("=")
        if sep and k.strip().isidentifier() and len(v) <= 80:
            out[k.strip()] = v.strip()
    return out


def fmt_hmm(seconds: float) -> str:
    """7200 -> '2:00', 7140 -> '1:59', 30 -> '0:01': hours:minutes, minutes rounded UP (the pill
    never says 0:00 while the access is still open)."""
    m = max(0, math.ceil(max(0.0, seconds) / 60.0 - 1e-9))
    return f"{m // 60}:{m % 60:02d}"


class LaptopAccess:
    """The k1000 side: request/close the opening, and follow what root reports.

    `open()` / `close()` / `toggle()` never block and never raise; `poll()` (every POLL_S, from
    `run()`) reads root's status; `left_s()` / `pill()` / `state` feed the screen. `on_change`
    is called (in the loop) whenever the state or the minute shown changes, `on_close(reason)`
    when an opening ends."""

    def __init__(self, *, flag: Path | str = FLAG, status: Path | str = STATUS,
                 uptime: Callable[[], float | None] = read_uptime,
                 clock: Callable[[], float] = time.monotonic, answer_s: float = ANSWER_S) -> None:
        self.flag = Path(flag)
        self.status_file = Path(status)
        self._uptime = uptime
        self._clock = clock
        self.answer_s = answer_s
        self.state = CLOSED
        self.until: float | None = None          # root's deadline, boot clock
        self.reason = ""
        self.message = ""                        # the last thing worth a hint ("" = none)
        self._asked: float | None = None         # when open() asked (monotonic)
        self._asked_up: float | None = None      # ... and on the boot clock
        self._closing: float | None = None
        self._shown: tuple[str, str] = (CLOSED, "")
        self.on_change: Callable[[], Any] | None = None
        self.on_close: Callable[[str], Any] | None = None

    # ------------------------------------------------------------------ requests
    def open(self) -> bool:
        """Ask root to open (creates the flag). False (and `message`) when it cannot even ask."""
        if self.state in (OPEN, OPENING):
            return True
        try:
            fd = os.open(self.flag, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
            try:
                os.write(fd, b"open\n")
            finally:
                os.close(fd)
        except OSError as e:
            self.state = UNAVAILABLE
            self.message = "LAPTOP ACCESS: NOT AVAILABLE"
            log.error("laptop access: cannot request it (%s: %s): is k1000 running under systemd "
                      "(RuntimeDirectory=k1000)?", self.flag, e.strerror or e)
            self._notify()
            return False
        self._asked, self._asked_up = self._clock(), self._uptime()
        self.state, self.reason, self.message = OPENING, "", ""
        log.warning("laptop access requested (CamillaGUI + SSH from the K1000 Wi-Fi, 2 h at most)")
        self._notify()
        return True

    def close(self, why: str = "box") -> None:
        """Close it (removes the flag; root removes the rule within a second)."""
        had = self._remove_flag()
        if self.state in (OPEN, OPENING) or had:
            log.warning("laptop access: closing (%s)", why)
            self.state = CLOSING if self.state == OPEN else CLOSED
            self._closing = self._clock()
            self._asked = None
            self.message = ""
            self._notify()

    def toggle(self) -> bool:
        """SETUP > Laptop access: open when closed, close when open. Returns the wish."""
        if self.state in (OPEN, OPENING):
            self.close("box")
            return False
        return self.open()

    def shutdown(self) -> None:
        """k1000 is stopping: close (systemd removes /run/k1000 anyway; this is for runs
        outside systemd and for an orderly log)."""
        if self._remove_flag():
            log.info("laptop access: closed (k1000 stopping)")

    def _remove_flag(self) -> bool:
        try:
            self.flag.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as e:
            log.error("laptop access: cannot remove %s: %s", self.flag, e)
            return False

    # ------------------------------------------------------------------ what root says
    def poll(self) -> bool:
        """Read root's status; returns True when what the screen shows changed."""
        st = read_status(self.status_file)
        up = self._uptime()
        flag = self.flag.exists()
        until = _float(st.get("until"))
        at = _float(st.get("at"))
        expired = until is not None and up is not None and until <= up
        root_open = st.get("state") == "open" and until is not None and not expired
        prev = self.state
        if root_open and flag and self.state != CLOSING:
            if self.state != OPEN:
                log.warning("laptop access OPEN: tcp %s from %s, %.0f s left",
                            st.get("ports", "22,5005"), st.get("subnet", "the AP subnet"),
                            (until - up) if (until is not None and up is not None) else LAPTOP_S)
            self.state, self.until, self._asked, self.message = OPEN, until, None, ""
        elif self.state == OPENING:
            fresh = (st.get("state") == "closed" and at is not None and self._asked_up is not None
                     and at >= math.floor(self._asked_up))
            if fresh:
                self._failed(st.get("reason", "error"))
            elif not flag:
                self._failed("error")
            elif self._asked is not None and self._clock() - self._asked > self.answer_s:
                self._failed("no-answer")
        elif self.state == OPEN:
            if expired:
                self._ended("time")
            elif st.get("state") == "closed":
                self._ended(st.get("reason") or "stopped")
            else:
                self._ended("k1000" if not flag else "stopped")
        elif self.state == CLOSING:
            if not root_open:
                self._ended("box")
            elif self._closing is not None and self._clock() - self._closing > 5.0:
                log.error("laptop access: root has not closed it 5 s after the request "
                          "(journalctl -u k1000-laptop)")
                self._ended("box")
        shown = (self.state, self.pill() or "")
        changed = shown != self._shown or prev != self.state
        self._shown = shown
        if changed:
            self._notify()
        return changed

    def _failed(self, reason: str) -> None:
        self._remove_flag()
        self.state, self.until, self._asked, self.reason = CLOSED, None, None, reason
        if reason == "no-answer":
            self.message = "LAPTOP ACCESS: NO ANSWER (SEE LOG)"
            log.error("laptop access: no answer from k1000-laptop.service within %.0f s "
                      "(is k1000-laptop.path running? systemctl status k1000-laptop.path)",
                      self.answer_s)
        else:
            self.message = f"LAPTOP ACCESS {REASONS.get(reason, 'failed').upper()}"
            log.error("laptop access refused by root: %s (journalctl -t k1000-laptop)", reason)
        if self.on_close is not None:
            self._call(self.on_close, reason)

    def _ended(self, reason: str) -> None:
        was_open = self.state == OPEN
        self.state, self.until, self._asked, self._closing = CLOSED, None, None, None
        self.reason = reason
        self._remove_flag()
        self.message = "" if reason == "box" else f"LAPTOP ACCESS {REASONS.get(reason, 'closed').upper()}"
        if was_open or reason != "box":
            log.warning("laptop access closed (%s)", reason)
        if self.on_close is not None:
            self._call(self.on_close, reason)

    # ------------------------------------------------------------------ views
    @property
    def is_open(self) -> bool:
        return self.state == OPEN

    def left_s(self) -> float:
        """Seconds of access left (0.0 = not open)."""
        if self.state != OPEN or self.until is None:
            return 0.0
        up = self._uptime()
        return max(0.0, self.until - up) if up is not None else 0.0

    def pill(self) -> str | None:
        """'LAPTOP OPEN 1:59' while open (the SHOW pill), else None."""
        left = self.left_s()
        return f"LAPTOP OPEN {fmt_hmm(left)}" if left > 0 else None

    def take_message(self) -> str:
        m, self.message = self.message, ""
        return m

    async def run(self, period: float = POLL_S) -> None:
        """Follow root's status for the life of the process (cheap: one small file in /run)."""
        while True:
            try:
                self.poll()
            except Exception as e:   # noqa: BLE001 - the knobs never depend on this
                log.warning("laptop access poll: %s: %s", type(e).__name__, e)
            await asyncio.sleep(period)

    def _notify(self) -> None:
        if self.on_change is not None:
            self._call(self.on_change)

    @staticmethod
    def _call(fn: Callable[..., Any], *args: Any) -> None:
        try:
            fn(*args)
        except Exception as e:   # noqa: BLE001
            log.warning("laptop access hook %r failed: %s", fn, e)


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


__all__ = ["LaptopAccess", "fmt_hmm", "read_status", "read_uptime", "FLAG", "STATUS", "PORTS",
           "LAPTOP_S", "OPEN", "OPENING", "CLOSED", "CLOSING", "UNAVAILABLE"]

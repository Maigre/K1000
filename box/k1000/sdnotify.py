# SPDX-License-Identifier: GPL-3.0-or-later
"""systemd's readiness and watchdog messages (sd_notify(3)), with the standard library only.

k1000.service is `Type=notify` with `WatchdogSec=`: systemd waits for READY=1 before it calls the
service started, then expects WATCHDOG=1 at least once per WatchdogSec. A missed deadline means
the service hangs: systemd sends SIGABRT (the journal gets every thread's traceback, see
PYTHONFAULTHANDLER in the unit) and `Restart=always` starts a new k1000, which finds the engine
playing and adopts it (control.py, "The engine is the truth while it plays").

Why it looks like this:

- **The ping comes from the event loop, never from a thread.** `serve()` is awaited by the main
  task: it pings, then waits for the stop event with a timeout. A loop that is stuck (a blocking
  call, a busy loop, a lock taken in the loop thread) runs neither, so the pings stop. A side
  thread would keep pinging for a control surface that no longer answers.
- **Pinging often costs nothing and tolerates more.** systemd fires WatchdogSec after the LAST
  ping, whatever the interval, so the interval only sets how long the loop may legitimately be
  late: every quarter of WatchdogSec, a loop that is busy for up to three quarters of it is never
  restarted. sd_watchdog_enabled(3) asks for half at most.
- **Nothing at all without NOTIFY_SOCKET** (the tests, a run by hand, the bench console): every
  call is a no-op and `serve()` only waits for the stop. Likewise no ping without WATCHDOG_USEC,
  or when WATCHDOG_PID names another process (the variables were inherited, not meant for us).
- **Never blocks, never raises.** The socket is non-blocking: a full queue or a missing socket is
  logged once and the controller carries on. The watchdog must never be what stops the service.
- The address is a path, or an abstract name written with a leading `@` (systemd uses either).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Mapping

log = logging.getLogger(__name__)

PING_FRACTION = 0.25          # ping every quarter of WatchdogSec (see the module docstring)


class Notifier:
    """One service's link to systemd, read from the environment once."""

    def __init__(self, env: Mapping[str, str] | None = None, pid: int | None = None) -> None:
        env = os.environ if env is None else env
        pid = os.getpid() if pid is None else pid
        raw = env.get("NOTIFY_SOCKET", "")
        self.address: str | None = None
        if raw.startswith("/"):
            self.address = raw
        elif raw.startswith("@") and len(raw) > 1:
            self.address = "\0" + raw[1:]
        elif raw:
            log.warning("NOTIFY_SOCKET %r is not a path or an abstract name: no systemd "
                        "notifications", raw)
        self.watchdog_s: float | None = None
        usec, wpid = env.get("WATCHDOG_USEC", ""), env.get("WATCHDOG_PID", "")
        if self.address is not None and usec:
            try:
                timeout = int(usec) / 1e6
                mine = not wpid or int(wpid) == pid
            except ValueError:
                timeout, mine = 0.0, False
            if timeout > 0 and mine:
                self.watchdog_s = timeout
        self._sock: socket.socket | None = None
        self._failed = False

    @property
    def enabled(self) -> bool:
        return self.address is not None

    @property
    def ping_s(self) -> float | None:
        """How often `serve()` pings, or None when no watchdog is asked for."""
        return None if self.watchdog_s is None else self.watchdog_s * PING_FRACTION

    def send(self, *fields: str) -> bool:
        """One datagram of newline-separated `KEY=value` fields. False when it was not sent."""
        if self.address is None or not fields:
            return False
        try:
            if self._sock is None:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
                self._sock.setblocking(False)
            self._sock.sendto("\n".join(fields).encode("utf-8"), self.address)
        except OSError as e:
            if not self._failed:
                self._failed = True
                log.warning("systemd notification failed (%s): carrying on without it", e)
            return False
        if self._failed:
            self._failed = False
            log.info("systemd notifications work again")
        return True

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    async def serve(self, stop: asyncio.Event) -> None:
        """READY=1 now, then WATCHDOG=1 from this event loop until `stop` is set, then
        STOPPING=1. Awaited by the main task in place of `stop.wait()`."""
        if not self.enabled:
            await stop.wait()
            return
        self.send("READY=1")
        log.info("systemd: ready%s", "" if self.watchdog_s is None else
                 f", watchdog {self.watchdog_s:g} s (ping every {self.ping_s:g} s from the loop)")
        try:
            while not stop.is_set():
                if self.ping_s is None:
                    await stop.wait()
                    break
                self.send("WATCHDOG=1")
                try:
                    await asyncio.wait_for(stop.wait(), self.ping_s)
                except (TimeoutError, asyncio.TimeoutError):
                    pass
            self.send("STOPPING=1")
        finally:
            self.close()

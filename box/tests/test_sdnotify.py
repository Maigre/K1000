# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for k1000.sdnotify (k1000.service: Type=notify, WatchdogSec=): READY=1, the watchdog's
WATCHDOG=1 pings from the event loop (and none while the loop is stuck), STOPPING=1, nothing at
all without NOTIFY_SOCKET, and a failing socket never raising. The last test runs the real
`python3 -m k1000` with a fake systemd socket and no engine.

systemd is played by a datagram socket bound by the test; an abstract name (`@...`) keeps the
address short whatever the temp directory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from k1000 import sdnotify
from k1000.sdnotify import Notifier

BOX = Path(__file__).resolve().parents[1]


class FakeSystemd:
    """The service manager's end of NOTIFY_SOCKET: every datagram, with its arrival time."""

    def __init__(self, path: str | None = None) -> None:
        self.name = path or "@k1000-test-" + uuid.uuid4().hex[:12]
        addr = self.name if path else "\0" + self.name[1:]
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(addr)
        self.sock.settimeout(0.05)
        self.got: list[tuple[float, str]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self.got.append((time.monotonic(), data.decode()))

    def messages(self) -> list[str]:
        with self._lock:
            return [m for _t, m in self.got]

    def times(self, msg: str) -> list[float]:
        with self._lock:
            return [t for t, m in self.got if m == msg]

    def wait_for(self, msg: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if msg in self.messages():
                return True
            time.sleep(0.01)
        return False

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self.sock.close()


@pytest.fixture
def systemd() -> Any:
    s = FakeSystemd()
    yield s
    s.close()


def env_for(s: FakeSystemd, watchdog_s: float | None = None, pid: int | None = None) -> dict[str, str]:
    env = {"NOTIFY_SOCKET": s.name}
    if watchdog_s is not None:
        env["WATCHDOG_USEC"] = str(int(watchdog_s * 1e6))
    if pid is not None:
        env["WATCHDOG_PID"] = str(pid)
    return env


def run(coro: Any, limit: float = 10.0) -> Any:
    return asyncio.run(asyncio.wait_for(coro, limit))


# --------------------------------------------------------------------------- the environment
def test_without_notify_socket_nothing_is_sent_and_serve_just_waits() -> None:
    n = Notifier(env={})
    assert not n.enabled and n.watchdog_s is None and n.ping_s is None
    assert n.send("READY=1") is False

    async def body() -> float:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.1, stop.set)
        t0 = time.monotonic()
        await n.serve(stop)
        return time.monotonic() - t0

    assert 0.09 <= run(body()) < 2.0


def test_addresses_paths_abstract_names_and_refusals(caplog: pytest.LogCaptureFixture) -> None:
    assert Notifier(env={"NOTIFY_SOCKET": "/run/systemd/notify"}).address == "/run/systemd/notify"
    assert Notifier(env={"NOTIFY_SOCKET": "@sd-x"}).address == "\0sd-x"
    with caplog.at_level(logging.WARNING, logger="k1000.sdnotify"):
        for raw in ("vsock:2:1234", "relative/sock", "@"):
            assert not Notifier(env={"NOTIFY_SOCKET": raw}).enabled
    assert "vsock:2:1234" in caplog.text


def test_watchdog_settings_follow_systemd_rules() -> None:
    sock = {"NOTIFY_SOCKET": "@x"}
    n = Notifier(env={**sock, "WATCHDOG_USEC": "15000000"}, pid=42)
    assert n.watchdog_s == 15.0 and n.ping_s == 15.0 * sdnotify.PING_FRACTION == 3.75
    assert Notifier(env={**sock, "WATCHDOG_USEC": "15000000", "WATCHDOG_PID": "42"}, pid=42).watchdog_s == 15.0
    # inherited by a child: not ours to feed
    assert Notifier(env={**sock, "WATCHDOG_USEC": "15000000", "WATCHDOG_PID": "41"}, pid=42).watchdog_s is None
    for bad in ("0", "-5", "abc", ""):
        assert Notifier(env={**sock, "WATCHDOG_USEC": bad}, pid=42).watchdog_s is None
    # no socket, no watchdog, whatever WATCHDOG_USEC says
    assert Notifier(env={"WATCHDOG_USEC": "15000000"}, pid=42).watchdog_s is None
    # sd_watchdog_enabled(3): ping at least every half; a quarter leaves the loop 3/4 of slack
    assert 0 < sdnotify.PING_FRACTION <= 0.5


# --------------------------------------------------------------------------- serve()
def test_ready_then_pings_from_the_loop_then_stopping(systemd: FakeSystemd) -> None:
    n = Notifier(env=env_for(systemd, watchdog_s=0.2))

    async def body() -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.5, stop.set)
        await n.serve(stop)

    run(body())
    assert systemd.wait_for("STOPPING=1", 2.0)
    msgs = systemd.messages()
    assert msgs[0] == "READY=1" and msgs[-1] == "STOPPING=1", msgs
    pings = msgs[1:-1]
    assert pings and set(pings) == {"WATCHDOG=1"}
    assert len(pings) >= 5, msgs        # every 0.05 s for 0.5 s


def test_a_stuck_loop_stops_the_pings_and_a_free_one_resumes_them(systemd: FakeSystemd) -> None:
    """The point of the watchdog: a blocking call in the loop (here 0.6 s, four times the
    watchdog's 0.15 s) leaves systemd without a ping for that long, while threads run on."""
    n = Notifier(env=env_for(systemd, watchdog_s=0.15))
    beats: list[float] = []
    alive = threading.Event()

    def side_thread() -> None:      # what a thread-based ping would have done: kept beating
        while not alive.is_set():
            beats.append(time.monotonic())
            time.sleep(0.02)

    async def body() -> tuple[float, float]:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        marks: list[float] = []

        def block() -> None:
            marks.append(time.monotonic())
            time.sleep(0.6)             # the hang: nothing else runs in the loop meanwhile
            marks.append(time.monotonic())

        loop.call_later(0.3, block)
        loop.call_later(1.2, stop.set)
        await n.serve(stop)
        return marks[0], marks[1]

    th = threading.Thread(target=side_thread, daemon=True)
    th.start()
    t_block, t_free = run(body())
    alive.set()
    th.join(2)
    assert systemd.wait_for("STOPPING=1", 2.0)
    pings = systemd.times("WATCHDOG=1")
    during = [t for t in pings if t_block + 0.01 < t < t_free - 0.01]
    assert not during, (t_block, t_free, pings)
    assert [t for t in pings if t < t_block], pings              # it pinged before ...
    after = [t for t in pings if t >= t_free]
    assert len(after) >= 2, pings                                # ... and again once free
    gap = max(b - a for a, b in zip(pings, pings[1:]))
    assert gap >= 0.55 > n.watchdog_s, pings                     # systemd would have fired
    assert [t for t in beats if t_block < t < t_free]            # the thread never noticed


def test_ready_only_without_a_watchdog(systemd: FakeSystemd) -> None:
    n = Notifier(env=env_for(systemd))

    async def body() -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.3, stop.set)
        await n.serve(stop)

    run(body())
    assert systemd.wait_for("STOPPING=1", 2.0)
    assert systemd.messages() == ["READY=1", "STOPPING=1"]


def test_a_missing_socket_never_raises_and_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    n = Notifier(env={"NOTIFY_SOCKET": "@k1000-test-nobody-" + uuid.uuid4().hex[:12],
                      "WATCHDOG_USEC": "100000"})

    async def body() -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.3, stop.set)
        await n.serve(stop)

    with caplog.at_level(logging.WARNING, logger="k1000.sdnotify"):
        run(body())
    assert caplog.text.count("systemd notification failed") == 1, caplog.text
    assert n.send("WATCHDOG=1") is False


def test_a_socket_path_works_too() -> None:
    d = tempfile.mkdtemp(prefix="k1sd", dir="/tmp")
    path = os.path.join(d, "notify")
    s = FakeSystemd(path)
    try:
        assert Notifier(env={"NOTIFY_SOCKET": path}).send("READY=1")
        assert s.wait_for("READY=1", 2.0)
    finally:
        s.close()
        os.unlink(path)
        os.rmdir(d)


# --------------------------------------------------------------------------- the service
def unit_values(text: str, key: str) -> list[str]:
    return [line.split("=", 1)[1] for line in text.splitlines() if line.startswith(key + "=")]


def test_the_unit_asks_for_what_the_service_sends() -> None:
    text = (BOX / "systemd" / "k1000.service").read_text()
    assert unit_values(text, "Type") == ["notify"]
    assert unit_values(text, "Restart") == ["always"]          # a watchdog kill is restarted
    wd = unit_values(text, "WatchdogSec")
    assert wd == ["15"]
    # The main process sends (NotifyAccess=main, the default): python itself, not a shell.
    assert unit_values(text, "NotifyAccess") in ([], ["main"])
    assert unit_values(text, "ExecStart") == ["/usr/bin/python3 -m k1000"]
    assert any("PYTHONFAULTHANDLER=1" in v.split() for v in unit_values(text, "Environment"))
    assert unit_values(text, "TimeoutStartSec") == ["30"]
    # the comment's ping interval is the code's
    assert f"every {float(wd[0]) * sdnotify.PING_FRACTION:g} s from its event loop" in text


def test_the_service_says_ready_feeds_the_watchdog_and_says_stopping(tmp_path: Path,
                                                                      systemd: FakeSystemd) -> None:
    """`python3 -m k1000` as systemd runs it, minus the hardware and the engine (k1000 copes with
    an engine that is not there): READY=1 once up, WATCHDOG=1 while it runs, STOPPING=1 on quit."""
    with socket.socket() as s:              # a port nobody listens on: ENGINE DOWN, retried
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = tmp_path / "k1000.yaml"
    settings.write_text(yaml.safe_dump({
        "profile": "dev",
        "profiles": {"dev": {"probe": False}},
        "engine": {"url": f"ws://127.0.0.1:{port}"},
        "paths": {"state": str(tmp_path / "state.json"), "active_config": str(tmp_path / "active.yml")},
    }))
    env = dict(os.environ, PYTHONPATH=str(BOX), PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1",
               **env_for(systemd, watchdog_s=0.4))
    env.pop("WATCHDOG_PID", None)
    proc = subprocess.Popen([sys.executable, "-m", "k1000", "--settings", str(settings), "--headless",
                             "--no-hw", "--no-phone", "--stdin"], cwd=str(BOX), env=env, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert systemd.wait_for("READY=1", 20.0), systemd.messages()
        time.sleep(0.6)
        assert proc.poll() is None
        msgs = systemd.messages()
        assert msgs[0] == "READY=1" and msgs.count("WATCHDOG=1") >= 3, msgs
        assert proc.stdin is not None
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        out, _ = proc.communicate(timeout=15)
        assert proc.returncode == 0, out
        assert systemd.wait_for("STOPPING=1", 2.0), systemd.messages()
        assert "systemd: ready, watchdog 0.4 s" in out, out
        replies = [json.loads(l.split(" ", 1)[1]) for l in out.splitlines() if l.startswith("@ok ")]
        assert {"cmd": "quit"} in replies
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

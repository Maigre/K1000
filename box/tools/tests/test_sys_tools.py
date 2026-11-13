# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the system side of the box: the camilladsp.service start script, the k1000-console.service
panel guard, install.sh helpers, tools/k1000-soak.sh, tools/k1000-m4-bringup.sh, tools/k1000-lean.sh
and bootstrap.sh.

Self-contained (no k1000 import, no hardware, no live service touched): the unit's shell script is
extracted the way systemd unquotes it and run against a temp config, a fake /proc/asound and a
fake camilladsp; the tools run against fake binaries on PATH. The soak tests start a small fake
engine websocket on $K1000_SYS_TEST_PORT (default 12372).

    cd box && python3 -m pytest -q -p no:cacheprovider tools/tests
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

BOX = Path(__file__).resolve().parents[2]
UNIT = BOX / "systemd" / "camilladsp.service"
INSTALL = BOX / "install.sh"
SOAK = BOX / "tools" / "k1000-soak.sh"
BRINGUP = BOX / "tools" / "k1000-m4-bringup.sh"
CONSOLE_UNIT = BOX / "systemd" / "k1000-console.service"
LEAN = BOX / "tools" / "k1000-lean.sh"
BOOTSTRAP = BOX / "bootstrap.sh"
PORT = int(os.environ.get("K1000_SYS_TEST_PORT", "12372"))


# --------------------------------------------------------------------------- unit script
def exec_start(unit_text: str, key: str = "ExecStart=") -> str:
    """The shell script of `ExecStart=/bin/sh -c '...'` as systemd hands it to sh.

    systemd joins continuation lines (backslash -> space), removes the quotes of the item and
    turns `$$` into `$` and `%%` into `%` (checked against `systemctl show -p ExecStart` on the box).
    """
    lines = unit_text.splitlines()
    i = next(k for k, line in enumerate(lines) if line.startswith(key))
    val = lines[i][len(key):]
    while val.endswith("\\"):
        i += 1
        val = val[:-1] + " " + lines[i]
    cmd, sep, script = val.partition(" -c ")
    assert cmd == "/bin/sh" and sep, val[:40]
    assert script.startswith("'") and script.endswith("'")
    script = script[1:-1]
    assert "\\" not in script, "a backslash inside the script would be a systemd escape"
    assert "'" not in script, "a single quote would end systemd's quoting"
    assert not re.search(r"%(?!%)", script.replace("%%", "")), "a lone % is a systemd specifier"
    assert not re.search(r"(?<!\$)\$(?!\$)", script.replace("$$", "")), "a lone $ is a systemd variable"
    return script.replace("$$", "$").replace("%%", "%")


FAKE_CDSP = """#!/bin/sh
if [ "$1" = "-c" ]; then
  if grep -q VALID "$2" 2>/dev/null; then echo "Config is valid"; exit 0; fi
  echo "2026-10-12 17:38:10.305374 INFO [camilladsp] version"
  echo "Config is not valid"
  echo "devices: missing field chunksize"
  exit 101
fi
echo "$*" > "{mark}"
exit 0
"""


class StartScript:
    """The unit's start script, pointed at a temp dir: run it, watch whether it execs."""

    def __init__(self, tmp: Path, wmax: int = 30, sleep: str = "0.1") -> None:
        self.tmp = tmp
        self.cfg = tmp / "active.yml"
        self.logdir = tmp / "log"
        self.asound = tmp / "asound"
        self.mark = tmp / "exec.txt"
        self.logdir.mkdir()
        self.asound.mkdir()
        fake = tmp / "camilladsp"
        fake.write_text(FAKE_CDSP.replace("{mark}", str(self.mark)))
        fake.chmod(0o755)
        s = exec_start(UNIT.read_text())
        for a, b in (("cfg=/var/lib/k1000/active.yml", f"cfg={self.cfg}"),
                     ("logdir=/var/log/k1000", f"logdir={self.logdir}"),
                     ("asound=/proc/asound", f"asound={self.asound}"),
                     ("wmax=30", f"wmax={wmax}"),
                     ("sleep 1", f"sleep {sleep}")):
            assert s.count(a) == 1, a
            s = s.replace(a, b)
        assert s.count("/usr/local/bin/camilladsp") == 2
        self.script = s.replace("/usr/local/bin/camilladsp", str(fake))
        self.proc: subprocess.Popen | None = None

    def config(self, *devices: str, valid: bool = True) -> None:
        body = "# VALID\n" if valid else "# broken\n"
        body += "devices:\n  samplerate: 48000\n"
        for kind, dev in zip(("capture", "playback"), devices):
            body += f"  {kind}:\n    type: Alsa\n    channels: 4\n    device: {dev}\n"
        self.cfg.write_text(body)

    def start(self) -> None:
        self.proc = subprocess.Popen(["sh", "-c", self.script], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)

    def wait_exec(self, timeout: float) -> list[str] | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.mark.exists() and self.mark.read_text().strip():
                return self.mark.read_text().split()
            time.sleep(0.02)
        return None

    def finish(self) -> str:
        assert self.proc is not None
        try:
            _out, err = self.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _out, err = self.proc.communicate()
        return err

    def kill(self) -> str:
        assert self.proc is not None
        self.proc.kill()
        return self.proc.communicate()[1]


def test_unit_script_is_valid_sh():
    script = exec_start(UNIT.read_text())
    r = subprocess.run(["sh", "-n", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_unit_keeps_50_engine_logs():
    script = exec_start(UNIT.read_text())
    assert "--log_rotate_size 256000 --log_keep_nbr 50" in script


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
def test_unit_verifies_with_systemd():
    r = subprocess.run(["systemd-analyze", "verify", str(UNIT)], capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert "escape" not in out.lower(), out
    if Path("/usr/local/bin/camilladsp").exists():     # on the box: everything resolves
        assert r.returncode == 0, out


def test_engine_starts_at_once_when_the_card_is_there(tmp_path):
    st = StartScript(tmp_path)
    (st.asound / "card1").mkdir()
    (st.asound / "M4").symlink_to("card1")
    st.config("hw:M4", "hw:M4")
    t0 = time.monotonic()
    st.start()
    argv = st.wait_exec(3)
    assert argv is not None
    assert time.monotonic() - t0 < 1.5
    assert argv == ["-m", "-g-40", "-p", "1234", "-a", "127.0.0.1", "-o", f"{st.logdir}/camilladsp.log",
                    "--log_rotate_size", "256000", "--log_keep_nbr", "50", str(st.cfg)]
    assert "waiting" not in st.finish()


def test_engine_waits_for_a_missing_card_then_starts_within_a_second(tmp_path):
    st = StartScript(tmp_path)
    st.config("hw:M4", "hw:M4")
    st.start()
    assert st.wait_exec(1.0) is None                     # no relaunch churn while it is absent
    (st.asound / "card1").mkdir()
    (st.asound / "M4").symlink_to("card1")
    t0 = time.monotonic()
    assert st.wait_exec(2) is not None
    assert time.monotonic() - t0 < 0.6                   # 0.1 s poll here, 1 s in the unit
    err = st.finish()
    assert err.count("k1000: waiting for audio device(s) hw:M4 (") == 1, err   # once, deduplicated
    assert "hw:M4 hw:M4" not in err


def test_device_wait_is_capped_then_the_engine_tries_anyway(tmp_path):
    st = StartScript(tmp_path, wmax=3)
    st.config("hw:M4", "hw:M4")
    st.start()
    argv = st.wait_exec(3)
    assert argv is not None and argv[-1] == str(st.cfg)
    err = st.finish()
    assert "still not in" in err and "starting the engine anyway" in err


@pytest.mark.parametrize("device,entry", [
    ("hw:M4", "M4"),
    ("hw:CARD=M4,DEV=0", "M4"),
    ("plughw:M4", "M4"),
    ('"hw:M4"', "M4"),
    ("'hw:M4'", "M4"),
    ("hw:M4   # the gig card", "M4"),
    ("hw:1", "card1"),
    ("hw:sndrpihifiberry", "sndrpihifiberry"),
])
def test_device_names_map_to_the_proc_entry_k1000_reads(tmp_path, device, entry):
    st = StartScript(tmp_path)
    st.config(device, device)
    st.start()
    assert st.wait_exec(0.5) is None, "must wait while the entry is missing"
    (st.asound / entry).mkdir()
    assert st.wait_exec(2) is not None
    st.finish()


@pytest.mark.parametrize("device", ["default", "null", "plug:dmix"])
def test_non_hw_devices_are_not_waited_for(tmp_path, device):
    st = StartScript(tmp_path)
    st.config(device, device)
    st.start()
    assert st.wait_exec(1.5) is not None
    st.finish()


def test_file_devices_and_no_procfs_are_not_waited_for(tmp_path):
    st = StartScript(tmp_path)
    st.cfg.write_text("# VALID\ndevices:\n  capture:\n    type: SignalGenerator\n"
                      "  playback:\n    type: File\n    filename: /dev/null\n")
    st.start()
    assert st.wait_exec(1.5) is not None
    st.finish()
    (tmp_path / "b").mkdir()
    st2 = StartScript(tmp_path / "b")
    st2.config("hw:M4", "hw:M4")
    st2.asound.rmdir()                                   # no /proc/asound at all: no check
    st2.start()
    assert st2.wait_exec(1.5) is not None
    st2.finish()


def test_invalid_config_is_waited_for_with_a_stable_message(tmp_path):
    st = StartScript(tmp_path, sleep="0.01")
    st.config("default", valid=False)
    st.start()
    assert st.wait_exec(1.0) is None
    st.config("default")
    assert st.wait_exec(2) is not None
    err = st.finish()
    lines = [l for l in err.splitlines() if "waiting for a valid" in l]
    # ~100 polls in 1 s: printed once, then once per 30 polls - never once per poll
    assert 1 <= len(lines) <= 8, err
    assert lines[0].endswith("devices: missing field chunksize")


def test_unwritable_logdir_logs_to_the_journal(tmp_path):
    st = StartScript(tmp_path)
    st.logdir.rmdir()
    st.config("default")
    st.start()
    argv = st.wait_exec(2)
    assert argv is not None and "-o" not in argv
    assert "not a writable directory" in st.finish()


def test_switch_back_to_a_present_card_while_waiting(tmp_path):
    """k1000 rewrites active.yml (m4 -> dev) while the engine waits for the M4: picked up."""
    st = StartScript(tmp_path)
    (st.asound / "sndrpihifiberry").mkdir()
    st.config("hw:M4", "hw:M4")
    st.start()
    assert st.wait_exec(0.5) is None
    st.config("hw:sndrpihifiberry", "hw:sndrpihifiberry")
    assert st.wait_exec(1.5) is not None
    st.finish()


# --------------------------------------------------------------------------- k1000-console.service
class ConsoleGuard:
    """k1000-console.service's script against a fake /sys: graphics/fbN/name and
    vtconsole/vtconN/{name,bind}, as the kernel lays them out."""

    def __init__(self, tmp: Path, wmax: int = 40, watch: int = 10) -> None:
        self.gfx = tmp / "graphics"
        self.vtc = tmp / "vtconsole"
        self.gfx.mkdir()
        for vt, name, bind in (("vtcon0", "(S) dummy device", "1"), ("vtcon1", "(M) frame buffer device", "0")):
            (self.vtc / vt).mkdir(parents=True)
            (self.vtc / vt / "name").write_text(name + "\n")
            (self.vtc / vt / "bind").write_text(bind + "\n")
        s = exec_start(CONSOLE_UNIT.read_text())
        for a, b in (("gfx=/sys/class/graphics", f"gfx={self.gfx}"), ("vtc=/sys/class/vtconsole", f"vtc={self.vtc}"),
                     ("wmax=600", f"wmax={wmax}"), ("watch=30", f"watch={watch}")):
            assert s.count(a) == 1, a
            s = s.replace(a, b)
        assert s.count("sleep 0.1") == 2
        self.script = s.replace("sleep 0.1", "sleep 0.02")
        self.proc: subprocess.Popen | None = None

    def fb(self, n: int, name: str) -> None:
        (self.gfx / f"fb{n}").mkdir()
        (self.gfx / f"fb{n}" / "name").write_text(name + "\n")

    def bind(self, vt: str = "vtcon1") -> str:
        return (self.vtc / vt / "bind").read_text().strip()

    def takeover(self) -> None:                     # what fbcon does when it binds a framebuffer
        (self.vtc / "vtcon1" / "bind").write_text("1\n")

    def start(self) -> None:
        self.proc = subprocess.Popen(["sh", "-c", self.script], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)

    def finish(self, timeout: float = 10) -> tuple[int, str, str]:
        assert self.proc is not None
        out, err = self.proc.communicate(timeout=timeout)
        return self.proc.returncode, out, err


def test_console_unit_script_is_valid_sh():
    r = subprocess.run(["sh", "-n", "-c", exec_start(CONSOLE_UNIT.read_text())], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
def test_control_unit_verifies_with_systemd():
    """k1000.service (Type=notify, WatchdogSec=; the contract with k1000/sdnotify.py is
    tests/test_sdnotify.py)."""
    unit = BOX / "systemd" / "k1000.service"
    r = subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert "escape" not in out.lower() and "unknown" not in out.lower(), out
    if Path("/opt/k1000/k1000").exists():               # on the box: everything resolves
        assert r.returncode == 0, out


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
def test_console_unit_verifies_with_systemd():
    r = subprocess.run(["systemd-analyze", "verify", str(CONSOLE_UNIT)], capture_output=True, text=True)
    assert "escape" not in (r.stdout + r.stderr).lower(), r.stdout + r.stderr


def test_console_guard_is_wanted_never_waited_for():
    """k1000 must not wait for the panel: Wants= only, no ordering either way."""
    k = (BOX / "systemd" / "k1000.service").read_text()
    assert re.search(r"^Wants=.*\bk1000-console\.service\b", k, re.M)
    assert not re.search(r"^(After|Requires|BindsTo)=.*k1000-console", k, re.M)
    c = CONSOLE_UNIT.read_text()
    assert not re.search(r"^(Before|After)=", c, re.M)
    assert "WantedBy=multi-user.target" in c and "RemainAfterExit=yes" in c
    assert re.search(r"^UNITS=\(.*\bk1000-console\b.*\)", INSTALL.read_text(), re.M)


def test_console_guard_unbinds_a_panel_that_registers_after_k1000(tmp_path):
    """The ops-1 race: the guard starts first, the panel registers later and fbcon binds it."""
    g = ConsoleGuard(tmp_path)
    g.start()
    time.sleep(0.2)
    g.fb(0, "fb_ili9486")
    g.takeover()
    rc, out, err = g.finish()
    assert rc == 0, err
    assert g.bind() == "0" and g.bind("vtcon0") == "1"      # the dummy console is never touched
    assert "ZynScreen is fb0" in out and "console unbound 1 time(s)" in out


def test_console_guard_catches_a_bind_that_lands_after_the_device(tmp_path):
    """fbcon binds in the same kernel call that creates fbN: the device can be seen first."""
    g = ConsoleGuard(tmp_path, watch=15)
    g.fb(0, "fb_ili9486")
    g.start()
    time.sleep(0.12)
    g.takeover()
    rc, out, _ = g.finish()
    assert rc == 0 and g.bind() == "0" and "console unbound 1 time(s)" in out


def test_console_guard_finds_the_panel_by_name_behind_hdmi(tmp_path):
    g = ConsoleGuard(tmp_path)
    g.fb(0, "vc4drmfb")
    g.fb(1, "fb_ili9486")
    g.takeover()
    g.start()
    rc, out, _ = g.finish()
    assert rc == 0 and g.bind() == "0" and "ZynScreen is fb1" in out


def test_console_guard_without_a_panel_touches_nothing(tmp_path):
    g = ConsoleGuard(tmp_path, wmax=5)
    g.fb(0, "vc4drmfb")                              # HDMI only: its console stays
    g.takeover()
    g.start()
    rc, out, err = g.finish()
    assert rc == 0 and g.bind() == "1"
    assert "no ZynScreen framebuffer" in err and out == ""


# --------------------------------------------------------------------------- k1000-usbwatch.service
USBWATCH_UNIT = BOX / "systemd" / "k1000-usbwatch.service"


class UsbGuard:
    """k1000-usbwatch.service's script against a fake /proc/asound, /sys/class/sound and USB tree,
    laid out as on the box (M4 day): /proc/asound/M4 -> card1, /sys/class/sound/card1/device ->
    .../usb1/1-1/1-1.2/1-1.2:1.0, the hub's `authorized` at .../usb1/1-1. One loop tick = 20 ms."""

    def __init__(self, tmp: Path, grace: int = 5, back0: int = 10, backmax: int = 20,
                 fallback: bool = False) -> None:
        self.asnd, self.cls = tmp / "asound", tmp / "class"
        self.hub = tmp / "devices" / "usb1" / "1-1"
        self.fallback = tmp / "fallback"
        self.cfg = tmp / "active.yml"
        self.asnd.mkdir()
        self.cls.mkdir()
        self.hub.mkdir(parents=True)
        (self.hub / "authorized").write_text("1\n")
        if fallback:
            self.fallback.mkdir()
            (self.fallback / "authorized").write_text("1\n")
        self.cfg.write_text("devices:\n  capture:\n    device: hw:M4\n  playback:\n    device: hw:M4\n")
        s = exec_start(USBWATCH_UNIT.read_text())
        for a, b in (("asnd=/proc/asound", f"asnd={self.asnd}"), ("cls=/sys/class/sound", f"cls={self.cls}"),
                     ("cfg=/var/lib/k1000/active.yml", f"cfg={self.cfg}"),
                     ("fallback=/sys/bus/usb/devices/1-1", f"fallback={self.fallback}"),
                     ("grace=10", f"grace={grace}"), ("back0=30", f"back0={back0}"),
                     ("backmax=300", f"backmax={backmax}"), ("tick=1", "tick=0.02"), ("pause=1", "pause=0.01")):
            assert s.count(a) == 1, a
            s = s.replace(a, b)
        self.script = s
        self.lines: list[tuple[float, str]] = []
        self.proc: subprocess.Popen | None = None

    def card(self, usb: bool = True) -> None:
        """The card appears: asound/M4 -> card1, class/card1/device -> its USB interface (or the
        HiFiBerry's platform device)."""
        dev = self.hub / "1-1.2" / "1-1.2:1.0" if usb else self.asnd.parent / "devices" / "platform" / "soc:sound"
        dev.mkdir(parents=True, exist_ok=True)
        (self.cls / "card1").mkdir(exist_ok=True)
        if not (self.cls / "card1" / "device").is_symlink():
            (self.cls / "card1" / "device").symlink_to(dev)
        (self.asnd / "card1").mkdir(exist_ok=True)
        if not (self.asnd / "M4").is_symlink():
            (self.asnd / "M4").symlink_to("card1")

    def unplug(self) -> None:
        (self.asnd / "M4").unlink()

    def start(self) -> None:
        self.proc = subprocess.Popen(["sh", "-c", self.script], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        t0 = time.monotonic()

        def read() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            for line in self.proc.stdout:
                self.lines.append((time.monotonic() - t0, line.strip()))
        threading.Thread(target=read, daemon=True).start()

    def wait_for(self, text: str, n: int = 1, timeout: float = 5.0) -> list[float]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            hits = [t for t, line in self.lines if text in line]
            if len(hits) >= n:
                return hits
            time.sleep(0.01)
        raise AssertionError(f"{text!r} x{n} not seen in {self.lines}")

    def resets(self) -> list[str]:
        return [line for _t, line in self.lines if "resetting USB hub" in line]

    def stop(self) -> str:
        assert self.proc is not None
        self.proc.terminate()
        _out, err = self.proc.communicate(timeout=5)
        return err


def test_usbwatch_unit_script_is_valid_sh():
    r = subprocess.run(["sh", "-n", "-c", exec_start(USBWATCH_UNIT.read_text())], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
def test_usbwatch_unit_verifies_with_systemd():
    r = subprocess.run(["systemd-analyze", "verify", str(USBWATCH_UNIT)], capture_output=True, text=True)
    assert "escape" not in (r.stdout + r.stderr).lower(), r.stdout + r.stderr


def test_usbwatch_is_installed_enabled_and_runs_as_root():
    u = USBWATCH_UNIT.read_text()
    assert "WantedBy=multi-user.target" in u and "Restart=always" in u
    assert not re.search(r"^User=", u, re.M)               # sysfs `authorized` is root's
    assert re.search(r"^UNITS=\(.*\bk1000-usbwatch\b.*\)", INSTALL.read_text(), re.M)


def test_usbwatch_resets_the_hub_of_a_card_that_stays_missing(tmp_path):
    """The 11:46 case: the M4 was there, the kernel gave up on its port. After `grace` ticks the
    guard resets the hub it saw above the card, then backs off (10, 20, 20 ticks), and says when
    the card is back; a later loss starts over at `grace`."""
    g = UsbGuard(tmp_path)
    g.card()
    g.start()
    try:
        g.wait_for("watching the audio devices")
        time.sleep(0.15)
        assert g.resets() == []                              # present: nothing happens
        g.unplug()
        t_gone = time.monotonic()
        g.wait_for("resetting USB hub 1-1 (reset 3)", timeout=6)
        r = g.resets()
        assert [line.split("(reset ")[1] for line in r[:3]] == ["1)", "2)", "3)"]
        assert "hw:M4 missing for 5 s" in r[0] and "missing for 15 s" in r[1] and "missing for 35 s" in r[2]
        assert time.monotonic() - t_gone >= 35 * 0.02
        time.sleep(0.1)                                      # the line comes before the 0, pause, 1
        assert (g.hub / "authorized").read_text().strip() == "1"   # re-authorized after each reset
        g.card()
        g.wait_for("back after")
        assert any("3 USB reset(s)" in line or "4 USB reset(s)" in line for _t, line in g.lines)
        n = len(g.resets())
        g.unplug()
        g.wait_for("(reset 1)", n=2, timeout=4)
        assert "missing for 5 s" in g.resets()[n]
    finally:
        g.stop()


def test_usbwatch_leaves_a_card_on_no_usb_alone(tmp_path):
    """The HiFiBerry (dev profile) has no USB hub above it: missing, it is never acted on (and no
    fallback hub exists here)."""
    g = UsbGuard(tmp_path)
    g.card(usb=False)
    g.start()
    try:
        g.wait_for("watching the audio devices")
        time.sleep(0.1)
        g.unplug()
        time.sleep(30 * 0.02)
        assert g.resets() == []
    finally:
        g.stop()


def test_usbwatch_falls_back_to_the_pi_hub_for_a_card_never_seen(tmp_path):
    """Booted with the M4 plugged but never enumerated: no hub learnt, so the Pi 4's 1-1."""
    g = UsbGuard(tmp_path, fallback=True)
    g.start()
    try:
        g.wait_for("resetting USB hub fallback (reset 1)", timeout=4)
        time.sleep(0.1)
        assert (g.fallback / "authorized").read_text().strip() == "1"
    finally:
        g.stop()


def test_usbwatch_never_resets_while_the_card_is_present(tmp_path):
    g = UsbGuard(tmp_path, fallback=True)
    g.card()
    g.start()
    try:
        g.wait_for("watching the audio devices")
        time.sleep(25 * 0.02)
        assert g.resets() == []
    finally:
        g.stop()


# --------------------------------------------------------------------------- install.sh
def test_install_sh_syntax():
    assert subprocess.run(["bash", "-n", str(INSTALL)]).returncode == 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="no shellcheck")
def test_shellcheck_clean():
    r = subprocess.run(["shellcheck", str(INSTALL), str(SOAK), str(BRINGUP), str(LEAN), str(BOOTSTRAP)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def _revision_of(src: Path) -> str:
    text = INSTALL.read_text()
    m = re.search(r"^revision_of\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "revision_of() not found in install.sh"
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(src.parent))
    r = subprocess.run(["bash", "-c", m.group(0) + 'revision_of "$1"', "x", str(src)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_revision_of_a_rollback_copy_keeps_its_revision(tmp_path):
    src = tmp_path / "k1000-rollback"
    src.mkdir()
    (src / "REVISION").write_text("e3a3deb\n")
    assert _revision_of(src) == f"e3a3deb (reinstalled from {src})"
    (src / "REVISION").write_text("e3a3deb (reinstalled from /tmp/old)\n")
    assert _revision_of(src) == f"e3a3deb (reinstalled from {src})"      # never twice


def test_revision_of_unknown_and_git(tmp_path):
    src = tmp_path / "copy"
    src.mkdir()
    assert _revision_of(src) == f"unknown (from {src})"
    if shutil.which("git") is None:
        return
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "init", "-q", str(src)], check=True, env=env)
    (src / "f").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "f"], check=True, env=env)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "x"], check=True, env=env)
    sha = subprocess.run(["git", "-C", str(src), "rev-parse", "--short", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    (src / "REVISION").write_text("ignored\n")
    assert _revision_of(src).startswith(sha)


def test_install_keeps_the_journal_and_never_touches_the_network():
    text = INSTALL.read_text()
    m = re.search(r"cat > \"\$TMP/journald.conf\" <<'EOF'\n(.*?)\nEOF\n", text, re.S)
    assert m and "Storage=persistent" in m.group(1) and "SystemMaxUse=32M" in m.group(1)
    assert "/etc/systemd/journald.conf.d/50-k1000-journal.conf" in text
    disabled = re.search(r"for u in (bluetooth\.service[^;]*); do", text).group(1).split()
    assert not [u for u in disabled if re.search(r"NetworkManager|ssh|wpa|avahi|network", u)]
    # cloud-init is tools/k1000-lean.sh's call (after k1000-console.service), never install.sh's
    assert not re.search(r"^\s*touch\s+/etc/cloud/cloud-init\.disabled", text, re.M)


# --------------------------------------------------------------------------- processor settings
CPU_DIR = BOX / "cpu"


def _cpu_table() -> dict[str, str]:
    """install.sh's CPU_FILES: file in box/cpu/ -> where it is installed."""
    m = re.search(r"^CPU_FILES=\(\n(.*?)^\)\n", INSTALL.read_text(), re.S | re.M)
    assert m, "CPU_FILES not found in install.sh"
    rows = re.findall(r'^\s*"(\S+) (\S+)"\s*$', m.group(1), re.M)
    assert len(rows) == len(m.group(1).strip().splitlines())
    return dict(rows)


def _ini(path: Path, section: str) -> dict[str, str]:
    out: dict[str, str] = {}
    cur = None
    for line in path.read_text().splitlines():
        if line.startswith("["):
            cur = line.strip("[]")
        elif cur == section and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _cpus(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.replace(",", " ").split():
        a, _, b = part.partition("-")
        out |= set(range(int(a), int(b or a) + 1))
    return out


def test_cpu_files_and_table_agree():
    table = _cpu_table()
    assert sorted(table) == sorted(p.name for p in CPU_DIR.iterdir()), table
    for src, dst in table.items():
        if dst.endswith(".service.d/50-k1000-cpu.conf"):
            unit = dst.split("/")[-2][:-2]
            assert (BOX / "systemd" / unit).exists(), unit      # a drop-in for one of OUR units
            assert src == unit.removesuffix(".service") + ".conf"
        else:
            assert dst in ("/etc/udev/rules.d/61-k1000-cpu-governor.rules",
                           "/etc/systemd/system.conf.d/50-k1000-cpu.conf"), dst
    for p in CPU_DIR.iterdir():
        assert p.read_text().startswith("# SPDX-License-Identifier: GPL-3.0-or-later\n"), p


def test_cpu_governor_rule_comes_after_the_os_one():
    name = "61-k1000-cpu-governor.rules"
    assert name > "60-ondemand-governor.rules"      # raspberrypi-sys-mods' rule: ours runs last
    rules = [l for l in (CPU_DIR / name).read_text().splitlines() if l and not l.startswith("#")]
    assert rules == ['KERNEL=="cpu*", SUBSYSTEM=="cpu", ATTR{cpufreq/scaling_governor}="performance"']
    # `udevadm verify` (systemd 254+) checks keys and syntax; older udevadm: skipped
    if shutil.which("udevadm") and subprocess.run(["udevadm", "verify", "--help"],
                                                  capture_output=True).returncode == 0:
        r = subprocess.run(["udevadm", "verify", str(CPU_DIR / name)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_cpu_split_engine_alone_on_two_cores():
    system = _cpus(_ini(CPU_DIR / "system.conf", "Manager")["CPUAffinity"])
    engine = _ini(CPU_DIR / "camilladsp.conf", "Service")
    assert engine == {"CPUAffinity": "2 3"}           # only a directive: no Exec*, no start logic
    assert system == {0, 1} and _cpus(engine["CPUAffinity"]) == {2, 3}
    for unit in ("k1000", "camillagui"):
        assert _cpus(_ini(CPU_DIR / f"{unit}.conf", "Service")["CPUAffinity"]) == system, unit
    # the control service: the INTENDED order, below the system's own work and above CamillaGUI.
    # Whether nice ranks one service against another at all is a box check (manual 2 §6).
    nice = int(_ini(CPU_DIR / "k1000.conf", "Service")["Nice"])
    gui = int(_ini(BOX / "systemd" / "camillagui.service", "Service")["Nice"])
    assert 0 < nice < gui
    assert "Nice" not in _ini(CPU_DIR / "camilladsp.conf", "Service")


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
def test_cpu_dropins_verify_with_their_units(tmp_path):
    """Each unit with its drop-in, the way systemd finds them (unit.service.d/ next to it)."""
    for src, dst in _cpu_table().items():
        if ".service.d/" not in dst:
            continue
        unit = dst.split("/")[-2][:-2]
        (tmp_path / unit).write_text((BOX / "systemd" / unit).read_text())
        d = tmp_path / f"{unit}.d"
        d.mkdir()
        (d / "50-k1000-cpu.conf").write_text((CPU_DIR / src).read_text())
        r = subprocess.run(["systemd-analyze", "verify", str(tmp_path / unit)], capture_output=True,
                           text=True)
        bad = [l for l in (r.stdout + r.stderr).splitlines() if "50-k1000-cpu.conf" in l]
        assert not bad, bad


def _run_cpu_block(root: Path, do_cpu: str = "") -> tuple[str, dict[str, int]]:
    """install.sh step 6b against a fake /etc under `root`. `put` is a stand-in (the real one
    chowns to root): install when the content differs, report it, set PUT_CHANGED."""
    text = INSTALL.read_text()
    table = re.search(r"^CPU_FILES=\(\n.*?^\)\n", text, re.S | re.M).group(0)
    choice = re.search(r"^CPU_CHOICE=.*\ncpu_choice\(\) .*\n", text, re.M).group(0)
    start = text.index("# --- 6b. processor settings")
    block = text[start:text.index("if [ $RELOAD = 1 ]; then", start)]
    prog = ('set -euo pipefail\nCHANGES=0 RELOAD=0\nnote() { printf "%s\\n" "$*"; }\n'
            'changed() { CHANGES=$((CHANGES + 1)); note "$*"; }\n'
            'put() { PUT_CHANGED=0; if [ -f "$2" ] && cmp -s "$1" "$2"; then return 0; fi; '
            'mkdir -p "$(dirname "$2")"; cp "$1" "$2"; changed "installed $2"; PUT_CHANGED=1; }\n'
            f'SRC={BOX}\nTMP={root}/tmp\nmkdir -p "$TMP"\nDO_CPU={do_cpu}\n'
            + (table + choice + block).replace("/etc/", f"{root}/etc/")
            + 'echo "CHANGES=$CHANGES RELOAD=$RELOAD"\n')
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    tail = r.stdout.strip().splitlines()[-1]
    return r.stdout, {k: int(v) for k, v in (kv.split("=") for kv in tail.split())}


def test_install_cpu_settings_on_off_and_remembered(tmp_path):
    table = _cpu_table()
    installed = [tmp_path / dst.lstrip("/") for dst in table.values()]

    out, n = _run_cpu_block(tmp_path)                       # first install: on by default
    assert n == {"CHANGES": len(table), "RELOAD": 1}, out
    for src, p in zip(table, installed):
        assert p.read_text() == (CPU_DIR / src).read_text()
    assert "processor settings on: cores and nice for what starts from now on" in out
    assert _run_cpu_block(tmp_path)[1] == {"CHANGES": 0, "RELOAD": 0}     # idempotent

    out, n = _run_cpu_block(tmp_path, "off")                # --cpu-off: all removed, remembered
    assert not [p for p in installed if p.exists()], out
    assert n == {"CHANGES": len(table) + 1, "RELOAD": 1}, out         # + the choice file
    assert "processor settings off: reboot to apply" in out           # not "from the next boot"
    assert "shares cores 0-1 with everything else" in out
    choice = tmp_path / "etc/k1000/cpu-tuning"
    assert choice.read_text().splitlines()[-1] == "off"
    for unit in ("camilladsp", "k1000", "camillagui"):                 # empty drop-in dirs go too
        assert not (tmp_path / f"etc/systemd/system/{unit}.service.d").exists()
    assert (tmp_path / "etc/udev/rules.d").is_dir()                     # never a system directory
    assert _run_cpu_block(tmp_path)[1] == {"CHANGES": 0, "RELOAD": 0}  # a later plain run: still off
    assert _run_cpu_block(tmp_path, "off")[1] == {"CHANGES": 0, "RELOAD": 0}

    out, n = _run_cpu_block(tmp_path, "on")                 # --cpu-on: back, remembered
    assert all(p.exists() for p in installed) and n["RELOAD"] == 1, out
    assert choice.read_text().splitlines()[-1] == "on"
    assert _run_cpu_block(tmp_path)[1] == {"CHANGES": 0, "RELOAD": 0}

    # a choice file edited by hand: anything but "off" is on, comments ignored
    choice.write_text("# off\n  off  \n")
    assert _run_cpu_block(tmp_path)[1]["CHANGES"] == len(table)
    choice.write_text("# off\nmaybe\n")
    assert _run_cpu_block(tmp_path)[1]["CHANGES"] == len(table)


def _run_cpu_start_guard(root: Path, *, start: int, do_cpu: str = "", choice: str | None = None,
                         pid1: str, online: str = "0-3") -> subprocess.CompletedProcess:
    """install.sh's --start guard (before step 0) against a fake /etc, /proc/1/status and
    /sys/devices/system/cpu/online under `root`."""
    text = INSTALL.read_text()
    choice_def = re.search(r"^CPU_CHOICE=.*\ncpu_choice\(\) .*\n", text, re.M).group(0)
    start_at = text.index("# --- the processor settings switched off, and --start")
    block = text[start_at:text.index("# --- 0. ", start_at)]
    (root / "proc/1").mkdir(parents=True, exist_ok=True)
    (root / "proc/1/status").write_text(f"Name:\tsystemd\nCpus_allowed:\t3\nCpus_allowed_list:\t{pid1}\n")
    (root / "online").write_text(online + "\n")
    choice_file = root / "etc/k1000/cpu-tuning"
    choice_file.unlink(missing_ok=True)
    if choice is not None:
        choice_file.parent.mkdir(parents=True, exist_ok=True)
        choice_file.write_text(f"# K1000 processor settings\n{choice}\n")
    prog = ('set -euo pipefail\ndie() { printf "%s\\n" "$*" >&2; exit 1; }\n'
            f'DO_START={start} DO_CPU={do_cpu}\n'
            + (choice_def + block).replace("/etc/", f"{root}/etc/")
                                  .replace("/proc/1/status", f"{root}/proc/1/status")
                                  .replace("/sys/devices/system/cpu/online", f"{root}/online")
            + 'echo passed\n')
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True)


def test_install_refuses_start_until_the_reboot_that_cpu_off_needs(tmp_path):
    """systemd keeps cores 0-1 after --cpu-off until the next boot (a daemon-reload gives none
    back): --start would put the engine there with everything else, so it is refused, before any
    change, while PID 1 has fewer cores than the box."""
    def run(**kw):
        return _run_cpu_start_guard(tmp_path, **kw)

    r = run(start=1, do_cpu="off", pid1="0-1")                       # --cpu-off --start
    assert r.returncode == 1 and "passed" not in r.stdout, r.stdout
    assert "cores 0-1 of 0-3" in r.stderr and "run without --start, then reboot" in r.stderr
    r = run(start=1, choice="off", pid1="0-1")      # --cpu-off earlier, no reboot since, --start
    assert r.returncode == 1, r.stdout
    # allowed: rebooted since (PID 1 has every core), no --start, settings on (by flag or file)
    for kw in (dict(do_cpu="off", pid1="0-3"), dict(choice="off", pid1="0-3"),
               dict(do_cpu="on", choice="off", pid1="0-1"), dict(choice="on", pid1="0-1"),
               dict(pid1="0-1")):
        r = run(start=1, **kw)
        assert r.returncode == 0 and r.stdout == "passed\n", (kw, r.stderr)
    r = run(start=0, do_cpu="off", pid1="0-1")
    assert r.returncode == 0 and r.stdout == "passed\n", r.stderr


def test_install_cpu_flags_parse_and_are_documented():
    # --deps exits inside the option loop, before the root check: the flag in front of it was
    # parsed (an unknown one exits 2 first), and nothing is installed even when run as root.
    for flag in ("--cpu-off", "--cpu-on"):
        r = subprocess.run(["bash", str(INSTALL), flag, "--deps"], capture_output=True, text=True)
        assert r.returncode == 0 and "rsync" in r.stdout.split(), (flag, r.stderr)
    r = subprocess.run(["bash", str(INSTALL), "--cpu-of", "--deps"], capture_output=True, text=True)
    assert r.returncode == 2 and "unknown option '--cpu-of'" in r.stderr, r.stderr
    r = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--cpu-off" in r.stdout and "--cpu-on" in r.stdout


# --------------------------------------------------------------------------- fake bins
def _bin(dirpath: Path, name: str, body: str) -> None:
    p = dirpath / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


def test_headless_leftovers_block_is_idempotent(tmp_path):
    """install.sh --headless: disables only plainly enabled units, renames the tty1 auto-login
    once, and a second run changes nothing (run with a fake systemctl, never the real one)."""
    text = INSTALL.read_text()
    start = text.index("  # The desktop image's services a DSP box never uses")
    end = text.index('  note "headless changes take effect at the next reboot"')
    block = text[start:end]
    dropin = tmp_path / "getty@tty1.service.d" / "autologin.conf"
    dropin.parent.mkdir()
    dropin.write_text("[Service]\nExecStart=\n")
    block = block.replace("/etc/systemd/system/getty@tty1.service.d/autologin.conf", str(dropin))
    fb = tmp_path / "bin"
    fb.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    for u, st in (("bluetooth.service", "enabled"), ("cups.service", "enabled"), ("cups.socket", "enabled"),
                  ("cups.path", "enabled"), ("rpcbind.service", "enabled"), ("rpcbind.socket", "static")):
        (state / u).write_text(st + "\n")                    # nfs-blkmap.service: not installed
    _bin(fb, "systemctl", f'echo "$*" >> "{tmp_path}/calls"\n'
                          'u=$(echo "$*" | awk \'{print $NF}\')\n'
                          f'case $1 in\n  is-enabled) cat "{state}/$u" 2>/dev/null || {{ echo "not-found"; exit 1; }} ;;\n'
                          f'  disable) echo disabled > "{state}/$u" ;;\nesac\n')
    prog = ('set -euo pipefail\nCHANGES=0\nnote() { printf "%s\\n" "$*"; }\n'
            'changed() { CHANGES=$((CHANGES + 1)); note "$*"; }\n' + block + 'echo "CHANGES=$CHANGES"\n')
    env = dict(os.environ, PATH=f"{fb}:/usr/bin:/bin")
    r1 = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    assert r1.returncode == 0, r1.stderr
    assert "CHANGES=6" in r1.stdout, r1.stdout             # 5 units + the auto-login
    disabled = [c for c in (tmp_path / "calls").read_text().splitlines() if c.startswith("disable")]
    assert sorted(c.split()[-1] for c in disabled) == ["bluetooth.service", "cups.path", "cups.service",
                                                       "cups.socket", "rpcbind.service"]
    assert not dropin.exists() and dropin.with_name("autologin.conf.k1000-off").exists()
    r2 = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and "CHANGES=0" in r2.stdout, r2.stdout + r2.stderr


# --------------------------------------------------------------------------- k1000-soak.sh
class FakeEngine:
    def __init__(self, port: int, faders=None) -> None:
        from websockets.sync.server import serve
        self.values = {"GetState": "Running", "GetProcessingLoad": 1.5, "GetBufferLevel": 230,
                       "GetClippedSamples": 0, "GetCaptureRate": 48001,
                       "GetFaders": faders if faders is not None else
                       [{"volume": -20.0, "mute": False}] + [{"volume": 0.0, "mute": False}] * 4}

        def handler(ws):
            for msg in ws:
                name = json.loads(msg)["command"]
                ws.send(json.dumps({"reply": name, "result": "Ok", "value": self.values.get(name)}))

        self.server = serve(handler, "127.0.0.1", port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


@pytest.fixture
def soak_env(tmp_path):
    fb = tmp_path / "bin"
    fb.mkdir()
    _bin(fb, "systemctl", 'case "$*" in\n'
                          '  *k1000.service*) printf "NRestarts=2\\nActiveState=active\\n" ;;\n'
                          '  *) printf "NRestarts=7\\nActiveState=active\\n" ;;\nesac\n')
    _bin(fb, "vcgencmd", 'echo "throttled=0x0"\n')
    (tmp_path / "boot_id").write_text("3f2a9c1e-1111-2222-3333-444455556666\n")
    (tmp_path / "uptime").write_text("8123.45 30000.00\n")
    (tmp_path / "camilladsp_rCURRENT.log").write_text("ok\nPB: write underrun\nCapture: read overrun\n")
    (tmp_path / "camilladsp_r2026-10-12_17-06-36.log").write_text("Playback error: xrun\nfine\n")
    env = dict(os.environ, PATH=f"{fb}:{os.environ.get('PATH', '/usr/bin:/bin')}",
               K1000_WS=f"ws://127.0.0.1:{PORT}", K1000_SOAK_LOG=str(tmp_path / "soak.log"),
               K1000_CDSP_LOG=str(tmp_path / "camilladsp.log"),
               K1000_SOAK_BOOT_ID=str(tmp_path / "boot_id"), K1000_SOAK_UPTIME=str(tmp_path / "uptime"))
    return tmp_path, env


def _fields(line: str) -> dict:
    return dict(p.split("=", 1) for p in line.split()[1:])


def test_soak_once_records_reboot_and_k1000_fields(soak_env):
    tmp, env = soak_env
    eng = FakeEngine(PORT)
    try:
        r = subprocess.run(["sh", str(SOAK), "--once"], env=env, capture_output=True, text=True, timeout=20)
    finally:
        eng.close()
    assert r.returncode == 0, r.stderr
    line = (tmp / "soak.log").read_text().splitlines()[-1]
    f = _fields(line)
    assert f["state"] == "Running" and f["rate"] == "48001" and f["load"] == "1.50"
    assert f["xruns"] == "2" and f["xruns_all"] == "3"
    assert f["restarts"] == "7" and f["k1000"] == "active" and f["k1000_restarts"] == "2"
    assert f["main_db"] == "-20.0" and f["muted"] == "false"
    assert f["boot"] == "3f2a9c1e" and f["up"] == "8123" and f["throttled"] == "0x0"
    assert "xruns=2 xruns_all=3" in line          # the README's grep relies on this order


def test_soak_once_with_the_engine_down(soak_env):
    tmp, env = soak_env
    env["K1000_WS"] = "ws://127.0.0.1:9"              # nothing listens there
    r = subprocess.run(["sh", str(SOAK), "--once"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    f = _fields((tmp / "soak.log").read_text().splitlines()[-1])
    assert f["state"] == "DOWN" and f["main_db"] == "-" and f["muted"] == "-"
    assert f["boot"] == "3f2a9c1e"


def _soak_line(ts, **kw):
    base = dict(state="Running", load="1.20", buffer="230", clipped="0", xruns="0", xruns_all="0",
                rate="48000", restarts="0", temp_c="50.0", throttled="0x0", main_db="-20.0",
                muted="false", k1000="active", k1000_restarts="0", boot="aaaaaaaa", up="100")
    base.update(kw)
    return ts + " " + " ".join(f"{k}={v}" for k, v in base.items())


def _summary(tmp: Path, lines: list[str], now: str, engine_logs: dict[str, str] | None = None):
    """--summary on these soak lines; `engine_logs`: name -> text of the engine logs beside it
    (none by default: the box's real /var/log/k1000 is never read by a test)."""
    log = tmp / "s.log"
    log.write_text("\n".join(lines) + "\n")
    logs = tmp / "cdsp"
    logs.mkdir(exist_ok=True)
    for name, text in (engine_logs or {}).items():
        (logs / name).write_text(text)
    env = dict(os.environ, K1000_SOAK_NOW=now, K1000_CDSP_LOG=str(logs / "camilladsp.log"))
    return subprocess.run(["sh", str(SOAK), "--summary", str(log)], env=env, capture_output=True,
                          text=True, timeout=20)


def test_soak_summary_clean(tmp_path):
    lines = [_soak_line(f"2026-10-15T23:00:{s:02d}+02:00", up=str(100 + s)) for s in (0, 10, 20, 30)]
    r = _summary(tmp_path, lines, "2026-10-15T23:00:35+02:00")
    assert r.returncode == 0, r.stdout
    assert "4 samples" in r.stdout and "clean" in r.stdout


def test_soak_summary_flags_a_buffer_that_stays_high(tmp_path):
    """The engine keeps a backlog after a dropout: the buffer level settles higher and stays."""
    levels = (0, 536, 540, 531, 536, 538, 534, 1010, 1012, 1008)
    lines = [_soak_line(f"2026-10-15T23:{i // 6:02d}:{i % 6 * 10:02d}+02:00", buffer=str(v), up=str(100 + 10 * i))
             for i, v in enumerate(levels)]
    r = _summary(tmp_path, lines, "2026-10-15T23:01:35+02:00")
    assert r.returncode == 1, r.stdout
    assert "buffer: starts at 536 frames, 531 .. 1012" in r.stdout
    assert "the playback buffer rose to 1012 frames, 9.9 ms above its start (536)" in r.stdout
    assert "first at 2026-10-15T23:01:10+02:00" in r.stdout


def test_soak_summary_accepts_buffer_jitter_and_the_zero_before_the_first_average(tmp_path):
    levels = (0, 0, 536, 580, 500, 610, 470, 536)
    lines = [_soak_line(f"2026-10-15T23:{i // 6:02d}:{i % 6 * 10:02d}+02:00", buffer=str(v), up=str(100 + 10 * i))
             for i, v in enumerate(levels)]
    r = _summary(tmp_path, lines, "2026-10-15T23:01:15+02:00")
    assert r.returncode == 0, r.stdout
    assert "clean" in r.stdout and "no buffer rise" in r.stdout


def test_soak_summary_buffer_rise_threshold_is_settable(tmp_path):
    levels = (128, 130, 126, 128, 129, 127, 200, 204)
    lines = [_soak_line(f"2026-10-15T23:{i // 6:02d}:{i % 6 * 10:02d}+02:00", buffer=str(v), up=str(100 + 10 * i))
             for i, v in enumerate(levels)]
    log = tmp_path / "s.log"
    log.write_text("\n".join(lines) + "\n")
    (tmp_path / "cdsp").mkdir()
    env = dict(os.environ, K1000_SOAK_NOW="2026-10-15T23:01:15+02:00", K1000_SOAK_BUFFER_RISE="64",
               K1000_CDSP_LOG=str(tmp_path / "cdsp" / "camilladsp.log"))
    r = subprocess.run(["sh", str(SOAK), "--summary", str(log)], env=env, capture_output=True,
                       text=True, timeout=20)
    assert r.returncode == 1, r.stdout
    assert "the playback buffer rose to 204 frames, 1.6 ms above its start (128)" in r.stdout


def test_soak_summary_flags_a_reboot_a_gap_and_a_stale_log(tmp_path):
    lines = [_soak_line("2026-10-15T23:00:00+02:00", up="5000"),
             _soak_line("2026-10-15T23:00:10+02:00", up="5010"),
             _soak_line("2026-10-16T02:10:00+02:00", boot="bbbbbbbb", up="40"),
             _soak_line("2026-10-16T02:10:10+02:00", boot="bbbbbbbb", up="50")]
    r = _summary(tmp_path, lines, "2026-10-16T08:00:00+02:00")
    assert r.returncode == 1
    assert "REBOOTED" in r.stdout and "boot bbbbbbbb" in r.stdout
    assert "gap of 3h09m50s" in r.stdout
    assert "uptime went back" in r.stdout
    assert "not running any more" in r.stdout


def test_soak_summary_flags_xruns_restarts_mute_and_power(tmp_path):
    lines = [_soak_line("2026-10-15T23:00:00+02:00"),
             _soak_line("2026-10-15T23:00:10+02:00", xruns="1", xruns_all="3", restarts="2",
                        k1000_restarts="1", muted="true", throttled="0x50000", state="DOWN",
                        k1000="activating"),
             _soak_line("2026-10-15T23:00:20+02:00", xruns_all="1")]   # restarts back to 0 (reboot)
    r = _summary(tmp_path, lines, "2026-10-15T23:00:25+02:00")
    assert r.returncode == 1
    out = r.stdout
    for needle in ("xruns rose by 1", "xruns_all rose by 3", "xruns_all went DOWN",
                   "camilladsp.service restarted 2 times", "k1000.service restarted 1 times",
                   "Main muted in 1 samples", "throttled: 0x50000", "1 samples not Running",
                   "k1000.service not active in 1 samples"):
        assert needle in out, (needle, out)


# The acceptance's case (2026-10-13): one underrun in the previous engine run's rotated log,
# hours before the soak; every sample carries xruns_all=1.
OLD_RUN = ("2026-10-12 22:16:47.157237 INFO  [camilladsp] <src/bin.rs:463> CamillaDSP version 5.0.0-beta.1\n"
           "2026-10-12 22:37:52.631549 WARN  [camilladsp::alsa_backend::device] "
           "<src/alsa_backend/device.rs:126> PB: Prepare playback after buffer underrun\n"
           "2026-10-13 04:16:58.999831 INFO  [camilladsp::alsa_backend::device] "
           "<src/alsa_backend/device.rs:1046> Capture stopped\n")
LIVE_RUN = ("2026-10-13 04:59:15.339777 INFO  [camilladsp] <src/bin.rs:463> CamillaDSP version 5.0.0-beta.1\n"
            "2026-10-13 04:59:15.525352 INFO  [camilladsp::alsa_backend::device] "
            "<src/alsa_backend/device.rs:133> PB: Starting playback from Prepared state\n")


def test_soak_summary_ignores_a_dropout_from_before_the_soak(tmp_path):
    lines = [_soak_line(f"2026-10-13T05:00:{s:02d}+02:00", xruns_all="1", up=str(100 + s))
             for s in (0, 10, 20, 30)]
    r = _summary(tmp_path, lines, "2026-10-13T05:00:35+02:00",
                 {"camilladsp_r2026-10-12_22-16-47.log": OLD_RUN, "camilladsp_rCURRENT.log": LIVE_RUN})
    assert r.returncode == 0, r.stdout
    assert "clean" in r.stdout and "ATTENTION" not in r.stdout
    assert ("note: engine logs: 1 dropout line(s) from before the soak, not counted (latest at "
            "2026-10-12 22:37:52: PB: Prepare playback after buffer underrun)") in r.stdout
    assert "note: xruns_all=1 already at the first sample" in r.stdout


def test_soak_summary_flags_engine_dropouts_inside_the_soak_only(tmp_path):
    lines = [_soak_line("2026-10-13T05:00:00+02:00", xruns_all="1"),
             _soak_line("2026-10-13T05:00:10+02:00", xruns_all="1"),
             _soak_line("2026-10-13T05:00:20+02:00", xruns="1", xruns_all="2"),
             _soak_line("2026-10-13T05:00:30+02:00", xruns="1", xruns_all="3")]
    live = LIVE_RUN + ("2026-10-13 05:00:15.123456 WARN  [camilladsp::alsa_backend::device] "
                       "<src/alsa_backend/device.rs:126> PB: Prepare playback after buffer underrun\n"
                       "2026-10-13 07:12:00.000001 WARN  [camilladsp::alsa_backend::device] "
                       "<src/alsa_backend/device.rs:126> PB: Prepare playback after buffer underrun\n")
    r = _summary(tmp_path, lines, "2026-10-13T05:00:35+02:00",
                 {"camilladsp_r2026-10-12_22-16-47.log": OLD_RUN, "camilladsp_rCURRENT.log": live,
                  # an undated line takes its file name's time (05:00:25): inside, so it counts
                  "camilladsp_r2026-10-13_05-00-25.log": "Capture: read overrun\n"})
    assert r.returncode == 1, r.stdout
    out = r.stdout
    assert ("engine logs: 2 dropout line(s) during the soak, first at 2026-10-13 05:00:15: "
            "PB: Prepare playback after buffer underrun") in out
    assert "xruns rose by 1" in out and "xruns_all rose by 2" in out
    assert "note: engine logs: 1 dropout line(s) from before the soak, not counted" in out
    assert ("note: engine logs: 1 dropout line(s) after the soak's last sample, not counted "
            "(first at 2026-10-13 07:12:00") in out


def test_soak_summary_without_engine_logs_says_so(tmp_path):
    lines = [_soak_line(f"2026-10-15T23:00:{s:02d}+02:00", up=str(100 + s)) for s in (0, 10)]
    r = _summary(tmp_path, lines, "2026-10-15T23:00:15+02:00")
    assert r.returncode == 0, r.stdout
    assert "note: no engine log at" in r.stdout and "dropouts judged on the samples only" in r.stdout


def test_soak_summary_old_log_format_still_reads(tmp_path):
    """A log written by the previous version (no boot/up/k1000 fields) still summarises."""
    lines = ["2026-10-15T23:00:00+02:00 state=Running load=1.23 buffer=0 clipped=0 xruns=0 xruns_all=0 "
             "rate=48000 restarts=0 temp_c=52.1 throttled=0x0"] * 2
    r = _summary(tmp_path, lines, "2026-10-15T23:00:30+02:00")
    assert "2 samples" in r.stdout


# --------------------------------------------------------------------------- k1000-m4-bringup.sh
STREAM0 = """MOTU M4 at usb-0000:01:00.0-1.3, high speed : USB Audio

Playback:
  Status: Stop
  Interface 1
    Altset 1
    Format: {fmt}
    Channels: 4
    Endpoint: 0x01 (1 OUT) (ASYNC)
    Rates: 44100, 48000, 88200, 96000, 176400, 192000
    Data packet interval: 125 us
    Bits: 24

Capture:
  Status: Stop
  Interface 2
    Altset 1
    Format: {fmt}
    Channels: 4
    Endpoint: 0x82 (2 IN) (ASYNC)
    Rates: 44100, 48000, 88200, 96000, 176400, 192000
    Data packet interval: 125 us
    Bits: 24
"""

FAKE_APLAY = """case "$*" in
  *--dump-hw-params*)
    printf 'HW Params of device "hw:M4":\\n--------------------\\nFORMAT:  S32_LE\\nCHANNELS: 4\\nRATE: [44100 192000]\\n--------------------\\n'
    echo "$(basename "$0"): set_params:1387: Channels count non available"
    echo "dump $*" >> "{w}/calls"; exit 1 ;;
esac
n=$(ls "{w}" | grep -c '^tone_')
echo "tone $*" >> "{w}/calls"
cat > "{w}/tone_$n.raw"
[ -e "{w}/aplay_fails" ] && exit 1
exit 0
"""

FAKE_SYSTEMCTL = """echo "systemctl $*" >> "{w}/calls"
u=${{2%.service}}
case $1 in
  is-active) cat "{w}/state_$u" 2>/dev/null || echo active ;;
  stop) echo inactive > "{w}/state_$u"
        [ "$u" = camilladsp ] && echo closed > "{w}/asound/card1/pcm0p/sub0/status" ;;
  start) echo active > "{w}/state_$u" ;;
esac
exit 0
"""


class M4Box:
    """A fake /proc/asound with an M4, fake binaries, a settings file and a scripted terminal."""

    def __init__(self, tmp: Path, *, present=True, busy=False, card_id="M4", fmt="S32_LE",
                 settings: str | None = None) -> None:
        self.w = tmp
        a = tmp / "asound"
        for d in ("card0", "card1/pcm0p/sub0", "card1/pcm0c/sub0"):
            (a / d).mkdir(parents=True, exist_ok=True)
        (a / "card0" / "id").write_text("sndrpihifiberry\n")
        (a / "sndrpihifiberry").symlink_to("card0")
        cards = " 0 [sndrpihifiberry]: HifiberryDacpAd - snd_rpi_hifiberry_dacplusadcpro\n"
        if present:
            (a / card_id).symlink_to("card1")
            (a / "card1" / "id").write_text(card_id + "\n")
            (a / "card1" / "usbid").write_text("07fd:000b\n")
            (a / "card1" / "stream0").write_text(STREAM0.format(fmt=fmt))
            (a / "card1" / "pcm0p" / "sub0" / "status").write_text("state: RUNNING\n" if busy else "closed\n")
            (a / "card1" / "pcm0c" / "sub0" / "status").write_text("state: RUNNING\n" if busy else "closed\n")
            cards += f" 1 [{card_id:<15}]: USB-Audio - M4\n"
        else:
            shutil.rmtree(a / "card1")
        (a / "cards").write_text(cards)
        fb = tmp / "bin"
        fb.mkdir()
        _bin(fb, "aplay", FAKE_APLAY.replace("{w}", str(tmp)))
        _bin(fb, "arecord", FAKE_APLAY.replace("{w}", str(tmp)))
        _bin(fb, "systemctl", FAKE_SYSTEMCTL.format(w=tmp))
        _bin(fb, "amixer", 'echo "amixer $*" >> "%s/calls"; exit 1\n' % tmp)
        _bin(fb, "vcgencmd", 'echo "throttled=0x0"\n')
        _bin(fb, "lsusb", "exit 0\n")
        _bin(fb, "dmesg", 'echo "[ 3.1] usb 1-1.3: New USB device found, idVendor=07fd, idProduct=000b"\n')
        # The tool's python3 = the one running these tests: the box's has numpy (k1000-lean keeps
        # python3-numpy), a dev machine's /usr/bin/python3 may not, and --tone builds its tone with it.
        _bin(fb, "python3", f'exec "{sys.executable}" "$@"\n')
        self.settings = tmp / "k1000.yaml"
        self.settings.write_text(settings if settings is not None else (BOX / "etc" / "k1000.yaml").read_text())
        self.tty = tmp / "tty"
        self.tty.write_text("")
        self.env = dict(os.environ, PATH=f"{fb}:/usr/bin:/bin", K1000_SETTINGS=str(self.settings),
                        K1000_ASOUND=str(a), K1000_ACTIVE=str(tmp / "active.yml"),
                        K1000_BRINGUP_TTY=str(self.tty), K1000_BRINGUP_ALLOW_NONROOT="1")

    def run(self, *args: str, answers: str = "", timeout: float = 60) -> subprocess.CompletedProcess:
        self.tty.write_text(answers)
        r = subprocess.run(["bash", str(BRINGUP), *args], env=self.env, capture_output=True,
                           text=True, timeout=timeout)
        assert "Traceback" not in r.stderr, r.stderr
        return r

    def calls(self) -> list[str]:
        p = self.w / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def tones(self) -> list[Path]:
        return sorted(self.w.glob("tone_*.raw"), key=lambda p: int(p.stem.split("_")[1]))


def test_bringup_checks_a_free_m4(tmp_path):
    box = M4Box(tmp_path)
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    for needle in ("PASS  card M4 registered (card1), USB 07fd:000b", "playback_channels: null (probe) -> 4",
                   "CamillaDSP picks S32_LE", "48000 Hz offered", "input_map [2, 3]",
                   "out1 Top L, out2 Top R, out3 Sub L, out4 Sub R", "UNCONFIRMED",
                   "MONITOR knob", "INPUT MONITOR MIX", "MON 1 / MON 2 / 3-4", "RESULT: 0 FAIL"):
        assert needle in out, (needle, out)
    calls = box.calls()
    assert [c for c in calls if c.startswith("dump")] and not [c for c in calls if c.startswith(("tone", "systemctl"))]
    assert all("-f S16_LE -c 1" in c for c in calls if c.startswith("dump"))


def test_bringup_never_opens_a_busy_m4(tmp_path):
    box = M4Box(tmp_path, busy=True)
    r = box.run()
    assert r.returncode == 0, r.stdout
    assert "the engine holds it" in r.stdout
    assert not [c for c in box.calls() if c.startswith(("dump", "tone", "systemctl"))]


def test_bringup_without_the_card(tmp_path):
    box = M4Box(tmp_path, present=False)
    r = box.run()
    assert r.returncode == 1
    assert "FAIL  card M4 is not registered" in r.stdout
    assert "RESULT: 1 FAIL" in r.stdout and "Traceback" not in r.stderr, r.stderr
    assert not [c for c in box.calls() if c.startswith(("dump", "tone"))]
    r = box.run("--tone", "--i-have-turned-the-speakers-off", answers="STOP\n")
    assert r.returncode == 1 and "REFUSED: no M4 card" in r.stderr, r.stdout + r.stderr
    assert "Traceback" not in r.stderr
    assert not [c for c in box.calls() if c.startswith(("systemctl", "tone"))]


def test_bringup_finds_the_m4_under_another_name(tmp_path):
    box = M4Box(tmp_path, card_id="M4_1")
    r = box.run()
    assert r.returncode == 1
    assert "set capture_device and playback_device to hw:M4_1" in r.stdout


def test_bringup_flags_settings_the_m4_cannot_honour(tmp_path):
    settings = ("profile: m4\nprofiles:\n  m4:\n    capture_channels: 2\n    playback_format: S16_LE\n"
                "    input_map: [4, 5]\n    output_map: [TopL, TopR, SubL]\n"
                "controls:\n  buttons: [7, 6, 5, 4]\n")
    box = M4Box(tmp_path, settings=settings)
    r = box.run()
    assert r.returncode == 1
    out = r.stdout
    for needle in ("PASS  profile: m4", "capture_channels: 2 is not offered",
                   "playback_format: S16_LE is not offered", "input_map [4, 5]: the M4 has capture channels 0-3",
                   "needs 4 entries", "INFO  controls:"):
        assert needle in out, (needle, out)


@pytest.mark.parametrize("flag,expect", [
    ("", "WARN  controls: still the defaults"),
    ("  buttons_confirmed: true\n", "PASS  controls: the defaults, confirmed by the Knob test"),
    ('  buttons_confirmed: "true"\n', "WARN  controls: still the defaults"),   # a string: k1000 refuses it too
])
def test_bringup_reads_the_knob_test_confirmation(tmp_path, flag, expect):
    """M4 day: the Knob test proved the default map; `buttons_confirmed: true` (what k1000 reads,
    ControlMap.from_cfg) must end the WARN here too."""
    base = (BOX / "etc" / "k1000.yaml").read_text()
    assert "\ncontrols:\n" in base
    settings = base.replace("\ncontrols:\n", "\ncontrols:\n" + flag, 1)
    r = M4Box(tmp_path, settings=settings).run()
    assert expect in r.stdout, r.stdout


def test_tone_refuses_without_the_speakers_flag(tmp_path):
    box = M4Box(tmp_path)
    r = box.run("--tone", answers="STOP\n\n\n\n\n")
    assert r.returncode == 2 and "REFUSED" in r.stderr
    assert box.calls() == [] and box.tones() == []


def test_tone_on_a_busy_m4_needs_STOP(tmp_path):
    box = M4Box(tmp_path, busy=True)
    r = box.run("--tone", "--i-have-turned-the-speakers-off", answers="yes\n")
    assert r.returncode == 1 and "nothing was stopped" in r.stdout
    assert not [c for c in box.calls() if c.startswith(("systemctl", "tone"))]


def _peak_dbfs(raw: bytes, fmt: str, ch: int):
    import numpy as np
    if fmt == "S32_LE":
        a = np.frombuffer(raw, dtype="<i4").astype(float) / 2147483647.0
    elif fmt == "S16_LE":
        a = np.frombuffer(raw, dtype="<i2").astype(float) / 32767.0
    else:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        a = v.astype(float) / 8388607.0
    return a.reshape(-1, ch)


def _bursts(x) -> int:
    import numpy as np
    on = np.abs(x) > 1e-6
    # a burst = a run of activity; pauses inside a beep are only zero crossings (<= 2 samples)
    idx = np.flatnonzero(on)
    return 0 if idx.size == 0 else 1 + int(np.sum(np.diff(idx) > 100))


def test_tone_stops_plays_each_output_at_minus_40_and_restores(tmp_path):
    import numpy as np
    box = M4Box(tmp_path, busy=True)
    r = box.run("--tone", "--i-have-turned-the-speakers-off", answers="STOP\n\n\n\n\n")
    assert r.returncode == 0, r.stdout + r.stderr
    sysc = [c for c in box.calls() if c.startswith("systemctl")]
    assert sysc == ["systemctl is-active k1000.service", "systemctl stop k1000.service",
                    "systemctl is-active camilladsp.service", "systemctl stop camilladsp.service",
                    "systemctl start camilladsp.service", "systemctl start k1000.service"], sysc
    tones = [c for c in box.calls() if c.startswith("tone")]
    assert len(tones) == 4
    assert all("-D hw:M4 -t raw -f S32_LE -c 4 -r 48000" in c for c in tones), tones
    files = box.tones()
    assert len(files) == 4
    for k, f in enumerate(files):
        x = _peak_dbfs(f.read_bytes(), "S32_LE", 4)
        others = [c for c in range(4) if c != k]
        assert not np.any(x[:, others]), f"out{k + 1}: other outputs must carry digital zero"
        peak = 20 * np.log10(np.max(np.abs(x[:, k])))
        assert -40.1 <= peak <= -40.0 + 1e-6, peak
        assert x[0, k] == 0 and x[-1, k] == 0
        assert _bursts(x[:, k]) == 2 * (k + 1)          # out N: N beeps, twice
    assert "out 1 (Top L): 1 beep(s)" in r.stdout and "out 4 (Sub R): 4 beep(s)" in r.stdout
    assert "out1 = Top L. out2 = Top R. out3 = Sub L. out4 = Sub R." in r.stdout


def test_tone_restores_the_services_when_playing_fails(tmp_path):
    box = M4Box(tmp_path, busy=True)
    (tmp_path / "aplay_fails").write_text("")
    r = box.run("--tone", "--i-have-turned-the-speakers-off", answers="STOP\n\n\n\n\n")
    assert r.returncode == 1
    sysc = [c for c in box.calls() if c.startswith("systemctl")]
    assert sysc[-2:] == ["systemctl start camilladsp.service", "systemctl start k1000.service"]
    assert len([c for c in box.calls() if c.startswith("tone")]) == 1


def test_tone_quit_after_the_first_output_restores(tmp_path):
    box = M4Box(tmp_path, busy=True)
    r = box.run("--tone", "--i-have-turned-the-speakers-off", answers="STOP\nq\n")
    assert r.returncode == 0
    assert len(box.tones()) == 1
    assert [c for c in box.calls() if c.startswith("systemctl start")] == [
        "systemctl start camilladsp.service", "systemctl start k1000.service"]


def test_tone_on_a_free_m4_touches_no_service_and_s16_works(tmp_path):
    import numpy as np
    box = M4Box(tmp_path, fmt="S16_LE")
    r = box.run("--tone", "--i-have-turned-the-speakers-off", "--freq", "60", answers="r\n\n\n\n\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [c for c in box.calls() if c.startswith("systemctl")]
    files = box.tones()
    assert len(files) == 5                                # out1 repeated once
    x = _peak_dbfs(files[0].read_bytes(), "S16_LE", 4)
    assert -40.2 <= 20 * np.log10(np.max(np.abs(x[:, 0]))) <= -40.0     # truncated, never rounded up


def test_tone_s24_3le(tmp_path):
    import numpy as np
    box = M4Box(tmp_path, fmt="S24_3LE")
    r = box.run("--tone", "--i-have-turned-the-speakers-off", answers="\n\n\n\n")
    assert r.returncode == 0, r.stdout + r.stderr
    x = _peak_dbfs(box.tones()[2].read_bytes(), "S24_3LE", 4)
    assert not np.any(x[:, [0, 1, 3]])
    assert -40.1 <= 20 * np.log10(np.max(np.abs(x[:, 2]))) <= -40.0


def test_tone_refuses_without_root(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root")
    box = M4Box(tmp_path)
    box.env.pop("K1000_BRINGUP_ALLOW_NONROOT")
    r = box.run("--tone", "--i-have-turned-the-speakers-off", answers="STOP\n")
    assert r.returncode == 2 and "sudo" in r.stderr
    assert not [c for c in box.calls() if c.startswith(("systemctl", "tone"))]


def test_bad_freq_is_refused(tmp_path):
    box = M4Box(tmp_path)
    for args in (("--freq", "20000"), ("--freq",), ("--freq", "abc")):
        r = box.run("--tone", "--i-have-turned-the-speakers-off", *args)
        assert r.returncode == 2
    assert box.calls() == []


# --------------------------------------------------------------------------- k1000-lean.sh
def _lean_src(body: str, **env: str) -> subprocess.CompletedProcess:
    """Source k1000-lean.sh (no main) and run `body` in the same bash."""
    prog = f'set -euo pipefail\nset --\nsource "{LEAN}"\n{body}\n'
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=dict(os.environ, **env))


@pytest.mark.parametrize("pkg", ["openssh-server", "network-manager", "python3-numpy", "python3-pil",
                                 "libasound2t64", "alsa-utils", "linux-image-rpi-v8", "raspi-firmware",
                                 "initramfs-tools", "systemd-timesyncd", "dnsmasq-base", "wpasupplicant",
                                 "python3.13", "libpython3.13-stdlib", "fonts-dejavu-core", "python3-qrcode",
                                 "nftables", "firmware-brcm80211", "sudo", "cron"])
def test_lean_guard_refuses_a_plan_that_removes_what_must_survive(tmp_path, pkg):
    plan = tmp_path / "plan"
    plan.write_text(f"chromium\n{pkg}\nlightdm\n")
    r = _lean_src(f'guard_plan "{plan}"')
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REFUSED" in r.stderr and f"  {pkg}\n" in r.stderr and "chromium" not in r.stderr


def test_lean_guard_passes_a_desktop_plan(tmp_path):
    plan = tmp_path / "plan"
    plan.write_text("\n".join(["chromium", "lightdm", "labwc", "libgtk-3-0t64", "python3.13-tk", "libdbus-glib-1-2",
                               "pipewire", "wireplumber", "plymouth", "rpd-theme", "network-manager-gnome",
                               "python3-pygame", "rpi-connect", "cups-daemon", "libpython3.13-dev"]) + "\n")
    r = _lean_src(f'guard_plan "{plan}"')
    assert r.returncode == 0, r.stderr


class LeanBox:
    """k1000-lean.sh against fake dpkg/apt/systemctl/lsinitramfs and temp /etc, /boot and /boot/firmware.

    The card is healthy by default: the boot packages installed, initramfs8/_2712 identical to their
    /boot/initrd.img-* sources, i2c-dev in modules-load.d, and (`guard="boot"`) k1000-console.service
    started by this boot and logging that it found the panel. Runs in the foreground
    (K1000_LEAN_FOREGROUND=1) unless a test removes it."""

    CMDLINE = ("console=serial0,115200 console=tty1 root=PARTUUID=230baedc-02 rootfstype=ext4 fsck.repair=yes "
               "rootwait quiet splash plymouth.ignore-serial-consoles cfg80211.ieee80211_regdom=FR "
               "vt.global_cursor_default=0")
    ESSENTIAL = ["raspi-firmware", "linux-image-rpi-v8", "initramfs-tools", "raspberrypi-sys-mods", "systemd",
                 "udev", "kmod", "sudo", "openssh-server", "network-manager", "wpasupplicant", "python3"]
    INITRD = b"K1000-INITRAMFS\n" * 4096

    def __init__(self, tmp: Path, pkgs: dict[str, str], plan: list[str], units: dict[str, str],
                 guard: str = "boot") -> None:
        self.tmp = tmp
        self.w = tmp / "w"
        self.boot = tmp / "boot"
        self.initrd = tmp / "initrd"
        self.etc = tmp / "etc"
        self.state = tmp / "state"
        fb = tmp / "bin"
        for d in (self.w, self.boot, self.initrd, self.etc / "cloud", self.etc / "rpi", self.etc / "modules-load.d",
                  fb, self.w / "units", self.w / "show"):
            d.mkdir(parents=True, exist_ok=True)
        (self.boot / "cmdline.txt").write_text(self.CMDLINE)          # no final newline, like the box's
        (self.boot / "config.txt").write_text("[all]\ndtparam=spi=on\ndtoverlay=k1000-zynscreen,rotate=270\n")
        (self.boot / "kernel8.img").write_bytes(b"KERNEL" * 1000)
        for fl, dst in (("v8", "initramfs8"), ("2712", "initramfs_2712")):
            (self.initrd / f"initrd.img-6.18.50+rpt-rpi-{fl}").write_bytes(self.INITRD)
            (self.boot / dst).write_bytes(self.INITRD)
        (self.etc / "modules-load.d" / "k1000.conf").write_text("# K1000\ni2c-dev\n")
        (self.w / "proc_cmdline").write_text("reboot=w coherent_pool=1M 8250.nr_uarts=1 " + self.CMDLINE + "\n")
        allpkgs = {p: "manual" for p in self.ESSENTIAL}
        allpkgs.update(pkgs)
        (self.w / "pkgs").write_text("".join(f"{p} ii \n" for p in allpkgs))
        (self.w / "auto").write_text("".join(f"{p}\n" for p, how in allpkgs.items() if how == "auto"))
        (self.w / "plan").write_text("".join(f"Purg {p} [1.0]\n" for p in plan))
        for u, st in units.items():
            (self.w / "units" / u).write_text(st + "\n")
        if guard == "boot":
            self.guard_ran(9_712_345, "k1000-console: ZynScreen is fb0 (waited 0 s); console unbound 1 time(s)")
        w = self.w
        _bin(fb, "dpkg-query", f'cat "{w}/pkgs"\n')
        _bin(fb, "dpkg", f'case $1 in --audit) cat "{w}/audit" 2>/dev/null ;; --print-architecture) echo arm64 ;; esac\n')
        _bin(fb, "id", 'echo 0\n')
        # Like the real one: ~1 MB of listing with init near the top, so a `| grep -q` reader closes the
        # pipe early and this dies of SIGPIPE (F1). NOINIT in the image: a listing without init.
        _bin(fb, "lsinitramfs", 'if grep -q NOINIT "$1"; then seq 1 30000 | sed "s|^|usr/lib/m|"; exit; fi\n'
                                'echo .\necho init\nseq 1 30000 | sed "s|^|usr/lib/modules/6.18.50/kernel/m|"\n')
        _bin(fb, "apt-mark", f'case $1 in showauto) cat "{w}/auto" ;; manual) shift; echo "apt-mark manual $*" >> "{w}/calls" ;; esac\n')
        # a real apt-get "removes" the planned packages from the fake dpkg database
        _bin(fb, "apt-get", f'case " $* " in\n  *" -s "*) cat "{w}/plan" ;;\n'
                            f'  *) echo "REAL apt-get $*" >> "{w}/calls"\n'
                            f'     for p in $(awk \'{{print $2}}\' "{w}/plan"); do grep -v "^$p " "{w}/pkgs" > "{w}/p.new" || true; mv "{w}/p.new" "{w}/pkgs"; done\n'
                            f'     : > "{w}/plan" ;;\nesac\n')
        _bin(fb, "systemctl", f'u=$(echo "$*" | awk \'{{print $NF}}\')\n'
                              f'case $1 in\n'
                              f'  is-enabled) cat "{w}/units/$u" 2>/dev/null || {{ echo not-found; exit 1; }} ;;\n'
                              f'  is-active) [ -e "{w}/active-$u" ] || exit 3 ;;\n'
                              f'  show) cat "{w}/show/$u" 2>/dev/null ;;\n'
                              f'  disable) echo "systemctl $*" >> "{w}/calls"; echo disabled > "{w}/units/$u" ;;\n'
                              f'  list-dependencies) ;;\n'
                              f'esac\n')
        _bin(fb, "journalctl", f'cat "{w}/journal" 2>/dev/null\n')
        env = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
        self.env = dict(env, PATH=f"{fb}:/usr/bin:/bin", K1000_BOOT_DIR=str(self.boot),
                        K1000_LEAN_STATE=str(self.state), K1000_LEAN_ETC=str(self.etc),
                        K1000_LEAN_INITRD_DIR=str(self.initrd), K1000_LEAN_PROC_CMDLINE=str(w / "proc_cmdline"),
                        K1000_LEAN_FOREGROUND="1")

    def guard_ran(self, start_us: int, line: str, status: int = 0) -> None:
        """k1000-console.service as `systemctl show` and the journal see it."""
        (self.w / "show" / "k1000-console.service").write_text(
            f"ActiveState=active\nExecMainStatus={status}\nExecMainStartTimestampMonotonic={start_us}\n")
        (self.w / "journal").write_text(line + "\n")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(LEAN), *args], capture_output=True, text=True, env=self.env)

    def calls(self) -> list[str]:
        f = self.w / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def snapshot(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.tmp)): p.read_bytes() for p in sorted(self.tmp.rglob("*"))
                if p.is_file() and "w" not in p.relative_to(self.tmp).parts[:1]}


def test_lean_refused_plan_removes_nothing(tmp_path):
    box = LeanBox(tmp_path, {"rpd-theme": "manual", "chromium": "auto", "openssh-server": "auto"},
                  plan=["chromium", "openssh-server", "rpd-theme"], units={})
    r = box.run("--only", "desktop")
    assert r.returncode == 1
    assert "REFUSED" in r.stderr and "openssh-server" in r.stderr
    assert not [c for c in box.calls() if c.startswith("REAL apt-get")]
    assert "apt-mark manual openssh-server" in box.calls()        # the keep marks come first
    saved = list(box.state.glob("REFUSED-desktop-*.txt"))
    assert len(saved) == 1 and "openssh-server" in saved[0].read_text()
    assert sorted(p.name for p in box.boot.iterdir()) == ["cmdline.txt", "config.txt", "initramfs8",
                                                          "initramfs_2712", "kernel8.img"]


def test_lean_dry_run_changes_nothing(tmp_path):
    box = LeanBox(tmp_path, {"rpd-theme": "manual", "chromium": "auto", "python3-numpy": "auto",
                             "rpi-swap": "manual", "cloud-init": "manual"},
                  plan=["chromium", "rpd-theme"],
                  units={"NetworkManager-wait-online.service": "enabled", "apt-daily.timer": "enabled",
                         "man-db.timer": "static", "k1000-console.service": "enabled"})
    before = box.snapshot()
    r = box.run("--dry-run")
    assert r.returncode == 0, r.stderr
    assert box.snapshot() == before and box.calls() == []
    out = r.stdout
    for want in ("would: marked manual: python3-numpy", "would: desktop: purge 2 packages",
                 "would: cloud-init disabled", "would: disabled NetworkManager-wait-online.service",
                 "would: disabled apt-daily.timer", "would: swap: zram only",
                 "would: cmdline.txt: add logo.nologo", "would: cmdline.txt: drop splash",
                 "would: config.txt: disable_splash=1"):
        assert want in out, (want, out)
    assert "man-db.timer" not in out                       # static: not plainly enabled, left alone
    assert re.search(r"dry run: \d+ changes", out)


def test_lean_full_run_then_rerun_is_0_changes(tmp_path):
    box = LeanBox(tmp_path, {"rpd-theme": "manual", "chromium": "auto", "plymouth": "auto",
                             "python3-numpy": "auto", "rpi-swap": "manual"},
                  plan=["chromium", "plymouth", "rpd-theme"],
                  units={"NetworkManager-wait-online.service": "enabled", "e2scrub_reap.service": "enabled",
                         "apt-daily.timer": "enabled", "k1000-console.service": "enabled"})
    r = box.run()
    assert r.returncode == 0, r.stderr
    real = [c for c in box.calls() if c.startswith("REAL apt-get")]
    assert len(real) == 1 and "purge --autoremove rpd-theme chromium plymouth" in real[0], real
    assert "SuggestsImportant=false" in real[0]          # the autoremove plan was empty: not run
    plans = sorted(box.state.glob("desktop-*.txt"))
    assert plans and plans[0].read_text().split() == ["chromium", "plymouth", "rpd-theme"]
    assert (box.etc / "cloud" / "cloud-init.disabled").exists()
    swap = (box.etc / "rpi" / "swap.conf.d" / "50-k1000.conf").read_text()
    assert "[Main]\nMechanism=zram\n" in swap
    cmd = (box.boot / "cmdline.txt").read_text()
    assert "\n" not in cmd                                   # still ONE line, still no final newline
    words = cmd.split()
    assert "splash" not in words and "plymouth.ignore-serial-consoles" not in words
    assert words.count("quiet") == 1 and words[-1] == "logo.nologo"
    assert words[:6] == LeanBox.CMDLINE.split()[:6]          # root= and the rest untouched, in order
    assert "\ndisable_splash=1\n" in (box.boot / "config.txt").read_text()
    assert list(box.boot.glob("cmdline.txt.k1000-lean-boot-*")) and list(box.state.glob("boot-boot-*/cmdline.txt"))
    assert list(box.boot.glob("cmdline.txt.k1000-lean-desktop-*"))     # before the purge, kept apart
    assert re.search(r"k1000-lean: \d+ changes \(they take effect at the next boot", r.stdout)
    # the box now answers "installed" for nothing that was purged, and its units say disabled
    (box.w / "pkgs").write_text("".join(f"{p} ii \n" for p in LeanBox.ESSENTIAL + ["python3-numpy", "rpi-swap"]))
    (box.w / "auto").write_text("")
    (box.w / "plan").write_text("")
    (box.w / "calls").unlink()
    snap = box.snapshot()
    r2 = box.run()
    assert r2.returncode == 0, r2.stderr
    assert "k1000-lean: 0 changes" in r2.stdout, r2.stdout
    assert box.calls() == [] and box.snapshot() == snap


def test_lean_desktop_puts_quiet_back_after_the_splash_package_takes_it(tmp_path):
    """rpd-plym-splash's prerm strips ' quiet', ' splash', ' plymouth.ignore-serial-consoles'."""
    box = LeanBox(tmp_path, {"rpd-plym-splash": "auto", "rpd-theme": "manual"}, plan=["rpd-plym-splash", "rpd-theme"],
                  units={})
    fake = tmp_path / "bin" / "apt-get"
    fake.write_text(fake.read_text().replace(
        '     : > ', f'     sed -i "s/ quiet//; s/ splash//; s/ plymouth.ignore-serial-consoles//" "{box.boot}/cmdline.txt"\n     : > '))
    r = box.run("--only", "desktop")
    assert r.returncode == 0, r.stderr
    words = (box.boot / "cmdline.txt").read_text().split()
    assert words.count("quiet") == 1 and "splash" not in words
    assert "quiet put back" in r.stdout


def test_lean_keeps_cloud_init_until_the_console_guard_is_enabled(tmp_path):
    box = LeanBox(tmp_path, {"cloud-init": "manual"}, plan=[], units={"k1000-console.service": "disabled"})
    r = box.run("--only", "services")
    assert r.returncode == 0, r.stderr
    assert not (box.etc / "cloud" / "cloud-init.disabled").exists()
    assert "cloud-init left on: k1000-console.service is not enabled" in r.stdout
    assert "k1000-lean: 0 changes" in r.stdout


@pytest.mark.parametrize("case", ["never-ran", "restarted-later", "failed", "no-panel"])
def test_lean_keeps_cloud_init_until_the_guard_has_run_at_a_boot(tmp_path, case):
    """Enabled is not enough (BOOT-2): installed by the same run (bootstrap), it never ran at a boot;
    re-run by install.sh --start an hour in, it proves nothing about the boot race."""
    box = LeanBox(tmp_path, {"cloud-init": "manual"}, plan=[], units={"k1000-console.service": "enabled"},
                  guard="none")
    if case == "restarted-later":
        box.guard_ran(3_600_000_000, "k1000-console: ZynScreen is fb0 (waited 0 s); console unbound 0 time(s)")
    elif case == "failed":
        box.guard_ran(9_000_000, "k1000-console: ZynScreen is fb0", status=1)
    elif case == "no-panel":
        box.guard_ran(9_000_000, "k1000-console: no ZynScreen framebuffer (fb_ili9486) after 60 s: console left as it is")
    r = box.run("--only", "services")
    assert r.returncode == 0, r.stderr
    assert not (box.etc / "cloud" / "cloud-init.disabled").exists()
    assert "cloud-init left on: k1000-console.service " in r.stdout, r.stdout
    assert ("did not find the panel" in r.stdout) == (case == "no-panel")


def test_lean_turns_cloud_init_off_once_the_guard_ran_at_this_boot(tmp_path):
    box = LeanBox(tmp_path, {"cloud-init": "manual"}, plan=[], units={"k1000-console.service": "enabled"})
    r = box.run("--only", "services")
    assert r.returncode == 0, r.stderr
    assert (box.etc / "cloud" / "cloud-init.disabled").exists() and "cloud-init disabled" in r.stdout


def test_lean_cmdline_with_plymouth_installed_only_disables_it(tmp_path):
    box = LeanBox(tmp_path, {"plymouth": "auto"}, plan=[], units={})
    r = box.run("--only", "boot")
    assert r.returncode == 0, r.stderr
    words = (box.boot / "cmdline.txt").read_text().split()
    assert "plymouth.enable=0" in words and "splash" in words and "logo.nologo" in words


def test_lean_refuses_a_two_line_cmdline(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    (box.boot / "cmdline.txt").write_text(LeanBox.CMDLINE + "\nquiet\n")
    r = box.run("--only", "boot")
    assert r.returncode == 1 and "more than one line" in r.stderr
    assert (box.boot / "cmdline.txt").read_text() == LeanBox.CMDLINE + "\nquiet\n"


def test_lean_list_and_unknown_step(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    assert box.run("--list").stdout.split() == ["keep", "desktop", "services", "timers", "swap", "boot", "firmware"]
    r = box.run("--only", "network")
    assert r.returncode == 1 and "unknown step 'network'" in r.stderr


def test_lean_has_init_survives_pipefail_on_a_long_listing(tmp_path):
    """F1: `lsinitramfs IMG | grep -qx init` under pipefail failed on every healthy initramfs."""
    box = LeanBox(tmp_path, {}, plan=[], units={})
    r = _lean_src(f'has_init "{box.boot}/initramfs8" && echo HAS-INIT', PATH=box.env["PATH"])
    assert r.returncode == 0 and "HAS-INIT" in r.stdout, r.stderr
    old = subprocess.run(["bash", "-c", f'set -o pipefail; lsinitramfs "{box.boot}/initramfs8" | grep -qx init'],
                         env=dict(os.environ, PATH=box.env["PATH"]))
    assert old.returncode != 0               # the fake reproduces the bug the old check had


def test_lean_check_passes_a_healthy_card_and_changes_nothing(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    snap = box.snapshot()
    r = box.run("--check")
    assert r.returncode == 0, r.stderr
    assert "safe to power-cycle" in r.stdout and box.snapshot() == snap and box.calls() == []


@pytest.mark.parametrize("breakage, want", [
    ("audit", "dpkg --audit"),
    ("truncated", "initramfs8 differs from"),
    ("missing-2712", "initramfs_2712 is missing"),
    ("noinit", "initramfs8 has no /init"),
    ("root", "but this boot came up with root=PARTUUID=deadbeef-02"),
    ("two-lines", "more than one line"),
    ("no-rootwait", "has no rootwait"),
    ("no-kernel", "kernel8.img is missing"),
    ("no-i2c-dev", "i2c-dev is not loaded at boot"),
    ("lost-package", "not installed: openssh-server"),
])
def test_lean_check_refuses_a_card_that_could_fail_to_boot(tmp_path, breakage, want):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    cmdline = box.boot / "cmdline.txt"
    if breakage == "audit":
        (box.w / "audit").write_text("The following packages are only half configured:\n plymouth\n")
    elif breakage == "truncated":
        (box.boot / "initramfs8").write_bytes(LeanBox.INITRD[: len(LeanBox.INITRD) // 2])
    elif breakage == "missing-2712":
        (box.boot / "initramfs_2712").unlink()
    elif breakage == "noinit":
        for f in (box.boot / "initramfs8", box.initrd / "initrd.img-6.18.50+rpt-rpi-v8"):
            f.write_bytes(b"NOINIT\n" * 100)
    elif breakage == "root":
        (box.w / "proc_cmdline").write_text("coherent_pool=1M root=PARTUUID=deadbeef-02 rootwait\n")
    elif breakage == "two-lines":
        cmdline.write_text(LeanBox.CMDLINE + "\nquiet\n")
    elif breakage == "no-rootwait":
        cmdline.write_text(LeanBox.CMDLINE.replace(" rootwait", ""))
    elif breakage == "no-kernel":
        (box.boot / "kernel8.img").unlink()
    elif breakage == "no-i2c-dev":
        (box.etc / "modules-load.d" / "k1000.conf").write_text("# nothing\n")
    elif breakage == "lost-package":
        (box.w / "pkgs").write_text((box.w / "pkgs").read_text().replace("openssh-server ii \n", ""))
    r = box.run("--check")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "do NOT power-cycle" in r.stderr and want in r.stderr, r.stderr


def test_lean_check_reads_i2c_dev_from_etc_modules_too(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    (box.etc / "modules-load.d" / "k1000.conf").unlink()
    (box.etc / "modules").write_text("# /etc/modules\ni2c-dev\n")
    assert box.run("--check").returncode == 0


def test_lean_desktop_with_nothing_to_remove_skips_the_removal_check(tmp_path):
    """A no-op re-run (a converged card, bootstrap again) is not a removal: no false alarm, exit 0."""
    box = LeanBox(tmp_path, {}, plan=[], units={})
    (box.boot / "initramfs8").write_bytes(b"x")             # would fail the check, were it run
    r = box.run("--only", "desktop")
    assert r.returncode == 0, r.stderr
    assert "k1000-lean: 0 changes" in r.stdout and box.calls() == []


def test_lean_desktop_removal_that_breaks_the_initramfs_says_do_not_power_cycle(tmp_path):
    box = LeanBox(tmp_path, {"plymouth": "auto", "rpd-theme": "manual"}, plan=["plymouth", "rpd-theme"], units={})
    fake = tmp_path / "bin" / "apt-get"
    # an interrupted raspi-firmware hook: the rebuilt initramfs only half copied onto /boot/firmware
    fake.write_text(fake.read_text().replace(
        '     : > ', f'     printf "NEW-INITRAMFS" > "{box.initrd}/initrd.img-6.18.50+rpt-rpi-v8"\n'
                   f'     printf "NEW-" > "{box.boot}/initramfs8"\n     : > '))
    r = box.run("--only", "desktop")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "initramfs8 differs from" in r.stderr and "BEFORE any power cycle" in r.stderr


def test_lean_full_run_ends_with_the_power_cycle_check(tmp_path):
    box = LeanBox(tmp_path, {"rpd-theme": "manual", "rpi-swap": "manual"}, plan=["rpd-theme"],
                  units={"apt-daily.timer": "enabled"})
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert "safe to power-cycle" in r.stdout and re.search(r"\d+ changes \(they take effect", r.stdout)


def _fake_systemd_run(fb: Path, w: Path, run: bool = True) -> None:
    """Records its arguments; with `run`, runs the command at once (like the unit would), with the
    --setenv variables, INVOCATION_ID set and stdout/stderr appended to the StandardOutput= file."""
    _bin(fb, "systemd-run", f'echo "systemd-run $*" >> "{w}/calls"\n' + ('exit 0\n' if not run else
         'log=\nwhile [ $# -gt 0 ]; do\n  case $1 in\n'
         '    -p) case $2 in StandardOutput=append:*) log=${2#StandardOutput=append:} ;; esac; shift 2 ;;\n'
         '    --setenv=*) export "${1#--setenv=}"; shift ;;\n    --*) shift ;;\n    *) break ;;\n  esac\ndone\n'
         'INVOCATION_ID=fake "$@" >> "$log" 2>&1\nexit 0\n'))


def test_lean_real_run_detaches_into_a_systemd_unit_and_returns_its_status(tmp_path):
    """BOOT-1/F5: apt never runs in the SSH session's process tree."""
    box = LeanBox(tmp_path, {"rpd-theme": "manual", "rpi-swap": "manual"}, plan=["rpd-theme"], units={})
    del box.env["K1000_LEAN_FOREGROUND"]
    _fake_systemd_run(tmp_path / "bin", box.w)
    r = box.run("--only", "desktop")
    assert r.returncode == 0, r.stdout + r.stderr
    sr = [c for c in box.calls() if c.startswith("systemd-run ")]
    assert len(sr) == 1 and "--collect" in sr[0] and "--unit=k1000-lean-" in sr[0]
    assert f"StandardOutput=append:{box.state}/run-" in sr[0] and "--only desktop" in sr[0]
    assert f"--setenv=K1000_BOOT_DIR={box.boot}" in sr[0]
    assert "running as the systemd unit k1000-lean-" in r.stdout
    assert "desktop: purged 1 packages" in r.stdout                  # the unit's log, followed
    rcs = list(box.state.glob("run-*.rc"))
    assert len(rcs) == 1 and rcs[0].read_text().strip() == "0"
    assert len([c for c in box.calls() if c.startswith("REAL apt-get")]) == 1


def test_lean_detached_failure_is_the_exit_status(tmp_path):
    box = LeanBox(tmp_path, {"rpd-theme": "manual", "openssh-server": "manual"},
                  plan=["rpd-theme", "openssh-server"], units={})
    del box.env["K1000_LEAN_FOREGROUND"]
    _fake_systemd_run(tmp_path / "bin", box.w)
    r = box.run("--only", "desktop")
    assert r.returncode == 1 and "REFUSED" in r.stdout


def test_lean_detached_unit_gone_without_a_status_fails_loudly(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    del box.env["K1000_LEAN_FOREGROUND"]
    _fake_systemd_run(tmp_path / "bin", box.w, run=False)          # the unit dies at once
    r = box.run("--only", "boot")
    assert r.returncode == 1 and "ended without an exit status" in r.stderr and "--check" in r.stderr


def test_lean_dry_run_and_check_never_detach(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    del box.env["K1000_LEAN_FOREGROUND"]
    _fake_systemd_run(tmp_path / "bin", box.w)
    assert box.run("--dry-run").returncode == 0 and box.run("--check").returncode == 0
    assert not [c for c in box.calls() if c.startswith("systemd-run")]


# --------------------------------------------------------------------------- k1000-lean.sh --only firmware
# Raspberry Pi OS's own config.txt (the lines the firmware step reads), with the K1000 lines added.
PIOS_CONFIG = ("# For more options and information see\n# http://rptl.io/configtxt\n\n"
               "dtparam=audio=on\n\n# Automatically load overlays for detected cameras\ncamera_auto_detect=1\n\n"
               "# Automatically load overlays for detected DSI displays\ndisplay_auto_detect=1\n\n"
               "# Automatically load initramfs files, if found\nauto_initramfs=1\n\n"
               "dtoverlay=vc4-kms-v3d\nmax_framebuffers=2\ndisable_fw_kms_setup=1\narm_64bit=1\n"
               "disable_overscan=1\narm_boost=1\n\n[cm4]\notg_mode=1\n\n[cm5]\ndtoverlay=dwc2,dr_mode=host\n\n"
               "[all]\ndtparam=spi=on\ndtoverlay=k1000-zynscreen,rotate=270\n")
FIRMWARE = {"auto_initramfs": "0", "camera_auto_detect": "0", "display_auto_detect": "0"}


def _config_values(text: str, key: str) -> list[str]:
    return [ln.split("=", 1)[1] for ln in text.splitlines() if ln.strip().startswith(key + "=")]


def _eeprom(box: LeanBox, config: str = "[all]\nBOOT_UART=0\nBOOT_ORDER=0xf41\n") -> None:
    """A fake rpi-eeprom-config: prints `config` with no argument, records any other call."""
    _bin(box.tmp / "bin", "rpi-eeprom-config",
         f'if [ $# = 0 ]; then printf "%s" "{config}"; else echo "rpi-eeprom-config $*" >> "{box.w}/calls"; fi\n')


def test_lean_firmware_is_opt_in_a_plain_run_leaves_it(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    (box.boot / "config.txt").write_text(PIOS_CONFIG)
    for args in ((), ("--dry-run",)):
        r = box.run(*args)
        assert r.returncode == 0, r.stderr
        assert "not run (opt-in, each with its own power cycle): firmware (--only firmware)" in r.stdout
        assert "auto_initramfs" not in r.stdout and "EEPROM" not in r.stdout
    text = (box.boot / "config.txt").read_text()
    for key in FIRMWARE:
        assert _config_values(text, key) == ["1"], key


def test_lean_firmware_sets_the_three_lines_where_they_stand_then_reruns_to_0_changes(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    (box.boot / "config.txt").write_text(PIOS_CONFIG)
    _eeprom(box)
    r = box.run("--only", "firmware")
    assert r.returncode == 0, r.stdout + r.stderr
    text = (box.boot / "config.txt").read_text()
    for key, val in FIRMWARE.items():
        assert _config_values(text, key) == [val], key
        assert f"# K1000 (tools/k1000-lean.sh --only firmware) was: {key}=1\n{key}=0\n" in text
    assert "[all]\n# K1000" not in text                      # set where they stand: nothing appended
    backups = list(box.boot.glob("config.txt.k1000-lean-firmware-*"))
    assert len(backups) == 1 and backups[0].read_text() == PIOS_CONFIG
    assert list(box.state.glob("boot-firmware-*/config.txt"))
    assert (box.boot / "cmdline.txt").read_text() == LeanBox.CMDLINE            # cmdline.txt untouched
    assert (box.boot / "initramfs8").read_bytes() == LeanBox.INITRD             # the initramfs too
    out = r.stdout
    assert "config.txt: auto_initramfs=0 (undo: cp " in out and "config.txt: display_auto_detect=0" in out
    assert "no initramfs (auto_initramfs=0) and a root= the kernel finds alone" in out
    assert "safe to power-cycle" in out and re.search(r"3 changes \(they take effect", out)
    assert "copy config.txt.k1000-lean-firmware-" in out
    # the EEPROM: read, never written; the command is printed for a person
    assert "DISABLE_HDMI=1 is NOT set" in out and "sudo rpi-eeprom-config --apply /tmp/boot.conf" in out
    assert box.calls() == []
    snap = box.snapshot()
    r2 = box.run("--only", "firmware")
    assert r2.returncode == 0, r2.stderr
    assert "k1000-lean: 0 changes" in r2.stdout and box.snapshot() == snap and box.calls() == []


def test_lean_firmware_adds_the_lines_a_config_lacks_under_all(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    before = (box.boot / "config.txt").read_text()               # LeanBox's: none of the three lines
    r = box.run("--only", "firmware")
    assert r.returncode == 0, r.stdout + r.stderr
    text = (box.boot / "config.txt").read_text()
    assert text.startswith(before)
    assert text[len(before):] == ("\n[all]\n# K1000 (tools/k1000-lean.sh --only firmware): a faster firmware boot\n"
                                  "auto_initramfs=0\ncamera_auto_detect=0\ndisplay_auto_detect=0\n")
    assert "rpi-eeprom-config not found" in r.stdout


def test_lean_firmware_sets_only_what_is_left(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    (box.boot / "config.txt").write_text(PIOS_CONFIG.replace("camera_auto_detect=1", "camera_auto_detect=0")
                                         .replace("auto_initramfs=1", "auto_initramfs=0"))
    r = box.run("--only", "firmware")
    assert r.returncode == 0, r.stderr
    assert "display_auto_detect=0 (undo" in r.stdout and "config.txt: auto_initramfs" not in r.stdout
    assert re.search(r"\b1 changes \(they take effect", r.stdout)


@pytest.mark.parametrize("root, extra, why", [
    ("root=UUID=8a1e-4f2c", "", "has root=UUID=8a1e-4f2c, which only an initramfs resolves"),
    ("root=LABEL=rootfs", "", "has root=LABEL=rootfs"),
    ("root=/dev/mapper/k1000-root", "", "has root=/dev/mapper/k1000-root"),
    ("root=PARTUUID=230baedc-02", " boot=overlay", "boot=overlay"),
    ("root=PARTUUID=230baedc-02", " cryptdevice=/dev/mmcblk0p2:root", "cryptdevice="),
    ("root=PARTUUID=230baedc-02", "@named", "config.txt names an initramfs"),
])
def test_lean_firmware_refuses_a_card_that_needs_its_initramfs_and_changes_nothing(tmp_path, root, extra, why):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    config = PIOS_CONFIG
    if extra == "@named":
        config += "initramfs initramfs8 followkernel\n"
        extra = ""
    (box.boot / "config.txt").write_text(config)
    cmdline = LeanBox.CMDLINE.replace("root=PARTUUID=230baedc-02", root) + extra
    (box.boot / "cmdline.txt").write_text(cmdline)
    (box.w / "proc_cmdline").write_text(cmdline + "\n")
    snap = box.snapshot()
    for args in (("--only", "firmware"), ("--dry-run", "--only", "firmware")):
        r = box.run(*args)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "firmware: REFUSED, nothing changed" in r.stderr and why in r.stderr, r.stderr
        assert box.snapshot() == snap


@pytest.mark.parametrize("root", ["root=PARTUUID=230baedc-02", "root=/dev/mmcblk0p2", "root=/dev/sda2",
                                  "root=/dev/nvme0n1p2"])
def test_lean_firmware_takes_a_root_the_kernel_finds_alone(tmp_path, root):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    cmdline = LeanBox.CMDLINE.replace("root=PARTUUID=230baedc-02", root)
    (box.boot / "cmdline.txt").write_text(cmdline)
    (box.w / "proc_cmdline").write_text(cmdline + "\n")
    r = box.run("--only", "firmware")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _config_values((box.boot / "config.txt").read_text(), "auto_initramfs") == ["0"]


def test_lean_firmware_dry_run_changes_nothing_and_prints_the_eeprom_command(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    (box.boot / "config.txt").write_text(PIOS_CONFIG)
    _eeprom(box)
    snap = box.snapshot()
    r = box.run("--dry-run", "--only", "firmware")
    assert r.returncode == 0, r.stderr
    for key in FIRMWARE:
        assert f"would: config.txt: {key}=0" in r.stdout
    assert "dry run: 3 changes" in r.stdout and "rpi-eeprom-config --apply" in r.stdout
    assert box.snapshot() == snap and box.calls() == []


def test_lean_firmware_says_when_the_eeprom_already_has_disable_hdmi(tmp_path):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    _eeprom(box, "[all]\nBOOT_UART=0\nDISABLE_HDMI=1\nBOOT_ORDER=0xf41\n")
    r = box.run("--only", "firmware")
    assert r.returncode == 0, r.stderr
    assert "EEPROM: DISABLE_HDMI=1 already set" in r.stdout and "--apply" not in r.stdout
    assert box.calls() == []


def _eeprom_applies(conf: str) -> dict[str, str]:
    """What the bootloader takes from an EEPROM configuration: the lines before any section and
    under [all]; [none] and any other section skipped (the reading eeprom_hdmi makes)."""
    on, got = True, {}
    for ln in conf.splitlines():
        s = ln.strip()
        if s.startswith("["):
            on = s.replace(" ", "") == "[all]"
        elif on and "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            got[k.strip()] = v.strip()
    return got


def _run_printed_eeprom_edit(out: str, before: str, d: Path) -> str:
    """Run the edit line k1000-lean printed for a person, on `before`, with ~/ and /tmp/ moved
    into `d`; return the configuration it would --apply."""
    tail = out.split("DISABLE_HDMI=1 is NOT set", 1)[1].splitlines()[1:]
    lines = []
    for ln in tail:
        if not ln.startswith("    "):
            break
        lines.append(ln[4:])
    assert lines[0].endswith("rpi-eeprom-config > ~/eeprom-before.conf")
    assert lines[2].startswith("diff ~/eeprom-before.conf /tmp/boot.conf")
    assert lines[3] == "sudo rpi-eeprom-config --apply /tmp/boot.conf"
    assert lines[-1] == "# undo: sudo rpi-eeprom-config --apply ~/eeprom-before.conf"
    (d / "eeprom-before.conf").write_text(before)
    edit = lines[1].replace("~/", f"{d}/").replace("/tmp/boot.conf", str(d / "boot.conf"))
    r = subprocess.run(["bash", "-c", edit], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return (d / "boot.conf").read_text()


# The shapes a Pi 4 EEPROM configuration takes: stock (an [all] block, then [none]), the one the
# old printed `echo >>` left on the box on 2026-11-12 (DISABLE_HDMI=1 under [none]: never applied),
# a DISABLE_HDMI=0 under [all], and an old one with no section at all.
STOCK_EEPROM = "[all]\nBOOT_UART=0\nWAKE_ON_GPIO=1\nPOWER_OFF_ON_HALT=0\n\n[none]\nFREEZE_VERSION=0\n"


@pytest.mark.parametrize("before, stranded", [
    (STOCK_EEPROM, False),
    (STOCK_EEPROM + "DISABLE_HDMI=1\n", True),
    ("[all]\nBOOT_UART=0\nDISABLE_HDMI=0\n\n[none]\nFREEZE_VERSION=0\n", False),
    ("BOOT_UART=0\nBOOT_ORDER=0xf41\n", False),
])
def test_lean_firmware_eeprom_command_puts_disable_hdmi_where_it_applies(tmp_path, before, stranded):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    _eeprom(box, before)
    r = box.run("--only", "firmware")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DISABLE_HDMI=1 is NOT set" in r.stdout and "already set" not in r.stdout
    assert ("not in effect" in r.stdout) == stranded
    assert box.calls() == []                                    # printed, never run
    after = _run_printed_eeprom_edit(r.stdout, before, tmp_path)
    assert _eeprom_applies(after)["DISABLE_HDMI"] == "1"
    assert after.count("DISABLE_HDMI") == 1                     # no stray line left under [none]
    rest = {k: v for k, v in _eeprom_applies(after).items() if k != "DISABLE_HDMI"}
    assert rest == {k: v for k, v in _eeprom_applies(before).items() if k != "DISABLE_HDMI"}
    if "[all]" in before:                                       # the measured form: right under [all]
        assert after.startswith("[all]\nDISABLE_HDMI=1\n")
        assert after.replace("DISABLE_HDMI=1\n", "", 1) == re.sub(r"DISABLE_HDMI=.*\n", "", before)
    # the box reads it back as set: the next run has nothing to print
    _eeprom(box, after)
    r2 = box.run("--only", "firmware")
    assert "EEPROM: DISABLE_HDMI=1 already set" in r2.stdout and "--apply" not in r2.stdout


def test_lean_check_without_an_initramfs_does_not_ask_for_one(tmp_path):
    """auto_initramfs=0: the firmware loads none, so a missing or stale initramfs8 is no reason to stop."""
    box = LeanBox(tmp_path, {}, plan=[], units={})
    (box.boot / "config.txt").write_text(PIOS_CONFIG.replace("auto_initramfs=1", "auto_initramfs=0"))
    (box.boot / "initramfs8").unlink()
    (box.boot / "initramfs_2712").write_bytes(b"x")
    r = box.run("--check")
    assert r.returncode == 0, r.stderr
    assert "no initramfs (auto_initramfs=0)" in r.stdout
    # ... and with the load back on, the same card must not be power-cycled
    (box.boot / "config.txt").write_text(PIOS_CONFIG)
    r = box.run("--check")
    assert r.returncode == 1 and "initramfs8 is missing" in r.stderr


@pytest.mark.parametrize("root, extra, why", [
    ("root=UUID=8a1e-4f2c", "", "has root=UUID=8a1e-4f2c"),
    ("root=PARTUUID=230baedc-02", " boot=overlay", "boot=overlay"),
])
def test_lean_check_without_an_initramfs_refuses_a_root_only_it_finds(tmp_path, root, extra, why):
    box = LeanBox(tmp_path, {}, plan=[], units={})
    (box.boot / "config.txt").write_text(PIOS_CONFIG.replace("auto_initramfs=1", "auto_initramfs=0"))
    cmdline = LeanBox.CMDLINE.replace("root=PARTUUID=230baedc-02", root) + extra
    (box.boot / "cmdline.txt").write_text(cmdline)
    (box.w / "proc_cmdline").write_text(cmdline + "\n")
    r = box.run("--check")
    assert r.returncode == 1, r.stdout
    assert "do NOT power-cycle" in r.stderr and why in r.stderr and "auto_initramfs=0, so this card would not boot" in r.stderr


def test_lean_check_reads_a_named_initramfs_as_loaded(tmp_path):
    """`initramfs FILE followkernel` loads it whatever auto_initramfs says: still checked."""
    box = LeanBox(tmp_path, {}, plan=[], units={})
    (box.boot / "config.txt").write_text(PIOS_CONFIG.replace("auto_initramfs=1", "auto_initramfs=0")
                                         + "initramfs initramfs8 followkernel\n")
    (box.boot / "initramfs8").unlink()
    r = box.run("--check")
    assert r.returncode == 1 and "initramfs8 is missing" in r.stderr


# --------------------------------------------------------------------------- install.sh --deps, --boot I2C
def test_install_deps_lists_what_bootstrap_must_check():
    r = subprocess.run(["bash", str(INSTALL), "--deps"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    deps = r.stdout.split()
    for p in ("python3-qrcode", "python3-smbus2", "python3-websockets", "alsa-utils", "device-tree-compiler"):
        assert p in deps


def _i2c_block(tmp: Path, config: str, loaded: bool) -> tuple[str, dict, Path]:
    text = INSTALL.read_text()
    start = text.index("  # The knob board (MCP23017 at 0x20) is on /dev/i2c-1")
    end = text.index("  # No blinking console cursor on the ZynScreen")
    conf = tmp / "modules-load.d" / "k1000.conf"
    mod = tmp / "sys_module_i2c_dev"
    if loaded:
        mod.mkdir()
    block = (text[start:end].replace("/etc/modules-load.d/k1000.conf", str(conf))
             .replace("/sys/module/i2c_dev", str(mod)))
    boot = tmp / "boot"
    boot.mkdir(exist_ok=True)
    (boot / "config.txt").write_text(config)
    (tmp / "t").mkdir(exist_ok=True)
    fb = tmp / "bin"
    fb.mkdir(exist_ok=True)
    _bin(fb, "modprobe", f'echo "modprobe $*" >> "{tmp}/calls"\n')
    prog = ('set -euo pipefail\nCHANGES=0\nnote() { printf "%s\\n" "$*"; }\n'
            'changed() { CHANGES=$((CHANGES + 1)); note "$*"; }\n'
            'put() { PUT_CHANGED=0; if [ -f "$2" ] && cmp -s "$1" "$2"; then return 0; fi; '
            'mkdir -p "$(dirname "$2")"; cp "$1" "$2"; changed "installed $2"; PUT_CHANGED=1; }\n'
            f'BOOT_DIR="{boot}"\nTMP="{tmp}/t"\n' + block + 'echo "CHANGES=$CHANGES"\n')
    return prog, dict(os.environ, PATH=f"{fb}:/usr/bin:/bin:/usr/sbin:/sbin"), boot


@pytest.mark.parametrize("config, adds", [
    ("[all]\n# K1000\ndtparam=i2c_arm=on\ndtparam=spi=on\n", False),          # the first box (hand-added)
    ("dtparam=audio=on,i2c_arm=on\n[all]\n", False),                         # before any section, combined
    ("#dtparam=i2c_arm=on\n[all]\ndtparam=audio=on\n", True),                 # Lite's commented default
    ("[all]\n[cm4]\ndtparam=i2c_arm=on\n", True),                            # only for another board
])
def test_install_boot_turns_i2c_on_and_loads_i2c_dev_at_boot_once(tmp_path, config, adds):
    """F2: a fresh Lite card had no /dev/i2c-1 (the knobs dead), the first box only by accident."""
    prog, env, boot = _i2c_block(tmp_path, config, loaded=False)
    r1 = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    assert r1.returncode == 0, r1.stderr
    cfg = (boot / "config.txt").read_text()
    assert cfg.startswith(config) and (cfg.endswith("[all]\n# K1000: I2C for the knob board (MCP23017 at 0x20 on i2c-1)\n"
                                                  "dtparam=i2c_arm=on\n") == adds)
    assert (tmp_path / "modules-load.d" / "k1000.conf").read_text().splitlines()[-1] == "i2c-dev"
    assert (tmp_path / "calls").read_text() == "modprobe i2c-dev\n"
    assert f"CHANGES={3 if adds else 2}" in r1.stdout, r1.stdout
    (tmp_path / "sys_module_i2c_dev").mkdir()                       # now loaded
    r2 = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and "CHANGES=0" in r2.stdout, r2.stdout + r2.stderr
    assert (boot / "config.txt").read_text() == cfg


# --------------------------------------------------------------------------- bootstrap.sh
def _pinned(var: str, text: str | None = None) -> str:
    return re.search(rf"^{var}=(\S*)$", BOOTSTRAP.read_text() if text is None else text, re.M).group(1)


PIN_SHA256 = _pinned("CDSP_PIN_SHA256")
OLD_TGZ_SHA256 = _pinned("CDSP_OLD_TGZ_SHA256")
# A fake engine per kind: its sha256 (what the fake sha256sum reads in it) and its --version line.
ENGINES = {"pinned": (PIN_SHA256, "CamillaDSP 5.0.0-beta.2 (0930f9b)"),
           "old": (_pinned("CDSP_OLD_BIN_SHA256"), "CamillaDSP 5.0.0-beta.1 (48bcd28)"),
           "other": ("ab" * 32, "CamillaDSP 5.0.0-beta.2 (1234567)")}
ENGINE_IS = {None: "no engine", "pinned": "the pinned build (D-019)", "old": "the old engine, 5.0.0-beta.1",
             "other": "another build (CamillaDSP 5.0.0-beta.2 (1234567), sha256 abababababab...)"}
# sha256sum FILE: FILE.sha256 when there is one (a tarball), else the "# sha256" line of a fake engine,
# else the real sum.
FAKE_SHA256SUM = ('[ -r "$1" ] || { echo "sha256sum: $1: No such file or directory" >&2; exit 1; }\n'
                  'if [ -f "$1.sha256" ]; then s=$(cat "$1.sha256"); else s=$(sed -n "s/^# sha256 //p" "$1" | head -n 1); fi\n'
                  'if [ -z "$s" ]; then s=$(/usr/bin/sha256sum "$1" | cut -d" " -f1); fi\necho "$s  $1"\n')


def _fake_engine(path: Path, kind: str) -> Path:
    sha, version = ENGINES[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n# sha256 {sha}\necho '{version}'\n")
    path.chmod(0o755)
    return path


def test_bootstrap_never_upgrades_or_reboots():
    text = BOOTSTRAP.read_text()
    assert "upgrade" not in re.sub(r"#.*", "", text)        # never apt upgrade
    assert not re.search(r"^\s*(sudo\s+)?(reboot|systemctl\s+reboot|shutdown)", text, re.M)


def test_bootstrap_pins_the_engine_that_build_sh_makes():
    """D-019: the commit and the binary's sha256 are the same in bootstrap.sh and in the build recipe,
    so build.sh's "this is the binary measured on the box" and bootstrap's check agree. The old
    engine stays pinned by its release tarball and binary."""
    text = BOOTSTRAP.read_text()
    build = (BOX / "tools" / "engine-build" / "build.sh").read_text()
    assert re.fullmatch(r"[0-9a-f]{40}", _pinned("PIN", build)) and _pinned("CDSP_PIN") == _pinned("PIN", build)
    assert re.fullmatch(r"[0-9a-f]{64}", PIN_SHA256) and PIN_SHA256 == _pinned("PIN_SHA256", build)
    assert re.search(r"^CDSP_PIN_URL=", text, re.M)         # the one place for a download, once hosted
    assert _pinned("CDSP_OLD_URL").endswith("/releases/download/v5.0.0-beta.1/camilladsp-linux-aarch64.tar.gz")
    for var in ("CDSP_OLD_TGZ_SHA256", "CDSP_OLD_BIN_SHA256"):
        assert re.fullmatch(r"[0-9a-f]{64}", _pinned(var)), var


def _bootstrap_box(tmp: Path, with_ap: bool = False, installed: str = "*", engine: str | None = "pinned",
                   pin_url: str = "") -> tuple[Path, Path, dict]:
    """bootstrap.sh next to a fake install.sh/k1000-lean.sh; fake dpkg says `installed` (a case
    pattern) is installed. The engine is a fake of kind `engine` (None: none) at tmp/usr-local-bin;
    `install` copies, `curl` serves tmp/download. `pin_url` fills CDSP_PIN_URL in the copy.
    Returns (box dir, fake bin dir, env)."""
    box = tmp / "box"
    (box / "tools").mkdir(parents=True)
    (box / "bootstrap.sh").write_text(BOOTSTRAP.read_text().replace("\nCDSP_PIN_URL=\n", f"\nCDSP_PIN_URL={pin_url}\n"))
    helptext = "#   sudo ./install.sh --boot ...\n" + ("#   sudo ./install.sh --ap   access point\n" if with_ap else "")
    (box / "install.sh").write_text(f'#!/bin/sh\ncase $1 in --help) printf "{helptext}" ;; '
                                    '--deps) printf "python3-qrcode\\npython3-smbus2\\n" ;; *) echo RAN-INSTALL "$@" ;; esac\n')
    (box / "tools" / "k1000-lean.sh").write_text('#!/bin/sh\necho "lean $*"\necho "k1000-lean: dry run: 3 changes"\n')
    for f in (box / "install.sh", box / "tools" / "k1000-lean.sh"):
        f.chmod(0o755)
    fb = tmp / "bin"
    fb.mkdir()
    _bin(fb, "dpkg", f'case $1 in --print-architecture) echo arm64 ;; -s) case $2 in {installed}) exit 0 ;; esac; exit 1 ;; esac\n')
    cdsp = tmp / "usr-local-bin" / "camilladsp"
    cdsp.parent.mkdir(parents=True, exist_ok=True)
    if engine is not None:
        _fake_engine(cdsp, engine)
    _bin(fb, "sha256sum", FAKE_SHA256SUM)
    _bin(fb, "install", 'while [ $# -gt 2 ]; do shift; done\ncp "$1" "$2" && chmod 755 "$2"\n')
    _bin(fb, "curl", f'echo "curl $*" >> "{tmp}/curl-calls"\n'
                     'while [ $# -gt 0 ]; do case $1 in -o) out=$2; shift ;; esac; shift; done\n'
                     f'[ -f "{tmp}/download" ] || exit 6\ncp "{tmp}/download" "$out"\n')
    env = {k: v for k, v in os.environ.items()
           if k not in ("INVOCATION_ID", "CAMILLADSP_BINARY", "CAMILLADSP_TARBALL")}
    return box, fb, dict(env, PATH=f"{fb}:/usr/bin:/bin", K1000_BOOTSTRAP_CDSP=str(cdsp))


@pytest.mark.parametrize("with_ap", [False, True])
def test_bootstrap_dry_run_runs_install_then_lean_and_uses_ap_when_offered(tmp_path, with_ap):
    box, fb, env = _bootstrap_box(tmp_path, with_ap)
    r = subprocess.run(["bash", str(box / "bootstrap.sh"), "--dry-run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "RAN-INSTALL" not in out                          # a dry run never runs install.sh
    assert "no apt-get update" in out                        # nothing missing: no network needed
    flags = "--boot --headless --ap" if with_ap else "--boot --headless"
    assert f"would run: install.sh {flags} (" in out          # said, not counted (it has no dry run)
    assert "lean --dry-run" in out
    assert "engine: CamillaDSP 5.0.0-beta.2 (0930f9b), the pinned build (D-019): nothing to do" in out
    assert "k1000-bootstrap: dry run: 3 changes" in out, out   # lean's 3, the pinned engine none


def test_bootstrap_runs_apt_update_only_when_a_package_is_missing(tmp_path):
    """F4: a re-run on a finished card at a gig (no internet) must not wait on apt."""
    box, fb, env = _bootstrap_box(tmp_path, installed="i2c-tools|python3-smbus2")
    r = subprocess.run(["bash", str(box / "bootstrap.sh"), "--dry-run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "would run: apt-get update (missing: python3-qrcode)" in r.stdout
    assert "would: apt-get install" not in r.stdout          # i2c-tools is there; install.sh installs qrcode
    text = BOOTSTRAP.read_text()
    upd = text.index("apt-get update -q")
    assert text.rindex("if [ ${#missing[@]} = 0 ]; then", 0, upd) > 0  # the real run sits behind the same test


def test_bootstrap_real_run_detaches_into_a_systemd_unit(tmp_path):
    box, fb, env = _bootstrap_box(tmp_path, engine="old")
    _fake_engine(tmp_path / "out" / "camilladsp", "pinned")
    _bin(fb, "id", 'echo 0\n')
    logdir = tmp_path / "logs"
    _bin(fb, "systemd-run", f'echo "systemd-run $*" >> "{tmp_path}/calls"\n'
                            'for a in "$@"; do case $a in --setenv=K1000_BOOTSTRAP_RC=*) rc=${a#--setenv=K1000_BOOTSTRAP_RC=} ;; '
                            'StandardOutput=append:*) log=${a#StandardOutput=append:} ;; esac; done\n'
                            'echo "k1000-bootstrap: 0 changes: this card is already a K1000" >> "$log"\necho 0 > "$rc"\n')
    env = dict(env, K1000_BOOTSTRAP_LOGDIR=str(logdir), K1000_REVISION="abc1234", CAMILLADSP_BINARY="out/camilladsp")
    r = subprocess.run(["bash", str(box / "bootstrap.sh"), "--no-ap"], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    call = (tmp_path / "calls").read_text()
    assert "--collect" in call and "--unit=k1000-bootstrap-" in call and "--setenv=K1000_REVISION=abc1234" in call
    # the unit starts in /: the engine binary is handed on by its full path
    assert f"--setenv=CAMILLADSP_BINARY={tmp_path.resolve()}/out/camilladsp " in call
    assert call.rstrip().endswith(f"{box.resolve()}/bootstrap.sh --no-ap")
    assert "running as the systemd unit k1000-bootstrap-" in r.stdout and "already a K1000" in r.stdout
    assert "RAN-INSTALL" not in r.stdout                     # nothing ran in the SSH session itself


def test_bootstrap_without_an_engine_to_install_stops_before_it_detaches(tmp_path):
    box, fb, env = _bootstrap_box(tmp_path, engine="old")
    _bin(fb, "id", 'echo 0\n')
    _bin(fb, "systemd-run", f'echo "systemd-run $*" >> "{tmp_path}/calls"\n')
    r = subprocess.run(["bash", str(box / "bootstrap.sh")], capture_output=True, text=True,
                       env=dict(env, K1000_BOOTSTRAP_LOGDIR=str(tmp_path / "logs")))
    assert r.returncode == 1 and "no engine to install" in r.stderr, r.stdout + r.stderr
    assert not (tmp_path / "calls").exists() and not (tmp_path / "logs").exists()
    assert "running as the systemd unit" not in r.stdout


@pytest.mark.parametrize("binary", ["kept", "vanished"])
def test_bootstrap_detached_unit_acts_on_the_same_engine_and_always_leaves_its_status(tmp_path, binary):
    """The unit really runs here: a fake systemd-run starts the script in / with only the
    --setenv variables (and INVOCATION_ID), appending to the log the terminal follows. The unit
    gets the engine path the terminal decided on, and a stop in the engine decision (the binary
    gone between the terminal's check and the unit's) still leaves the exit status: the terminal
    shows the reason, not "ended without an exit status"."""
    box, fb, env = _bootstrap_box(tmp_path, engine="old")
    (box / "bootstrap.sh").chmod(0o755)                     # the unit runs it by its path
    build = _fake_engine(tmp_path / "out" / "camilladsp", "pinned")
    _bin(fb, "id", 'echo 0\n')
    vanish = f'rm -f "{build}"\n' if binary == "vanished" else ""
    run = fb / "systemd-run"
    run.write_text('#!/bin/bash\n'
                   f'echo "systemd-run $*" >> "{tmp_path}/calls"\n'
                   'envs=() log=\nwhile [ $# -gt 0 ]; do\n  case $1 in\n'
                   '    --setenv=*) envs+=("${1#--setenv=}") ;;\n'
                   '    -p) shift; case $1 in StandardOutput=append:*) log=${1#StandardOutput=append:} ;; esac ;;\n'
                   '    -*) ;;\n    *) break ;;\n  esac\n  shift\ndone\n'
                   # never let a unit loose on the real /usr/local/bin/camilladsp
                   'case " ${envs[*]} " in *" K1000_BOOTSTRAP_CDSP="*) ;; *) echo "no K1000_BOOTSTRAP_CDSP" >> "$log"; exit 0 ;; esac\n'
                   + vanish +
                   '(cd / && env -i PATH="$PATH" INVOCATION_ID=fake-unit "${envs[@]}" "$@" >> "$log" 2>&1) || true\n')
    run.chmod(0o755)
    env = dict(env, K1000_BOOTSTRAP_LOGDIR=str(tmp_path / "logs"), CAMILLADSP_BINARY="out/camilladsp")
    r = subprocess.run(["bash", str(box / "bootstrap.sh")], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert f"--setenv=K1000_BOOTSTRAP_CDSP={_cdsp(tmp_path)} " in (tmp_path / "calls").read_text()
    rcs = list((tmp_path / "logs").glob("run-*.rc"))
    assert len(rcs) == 1 and "without an exit status" not in r.stderr, r.stdout + r.stderr
    if binary == "kept":
        assert r.returncode == 0 and rcs[0].read_text() == "0\n", r.stdout + r.stderr
        assert _engine_left(tmp_path, "pinned")
        assert (f"installed {_cdsp(tmp_path)}: CamillaDSP 5.0.0-beta.2 (0930f9b), the pinned build (D-019, next5 0930f9b) "
                "(it replaces the old engine, 5.0.0-beta.1)\n") in r.stdout
    else:
        assert r.returncode == 1 and rcs[0].read_text() == "1\n", r.stdout + r.stderr
        assert f"ERROR: CAMILLADSP_BINARY={build}: no such file. Nothing was changed" in r.stdout
        assert _engine_left(tmp_path, "old") and "RAN-INSTALL" not in r.stdout


def _ssh_block(tmp: Path, journal: str, sshd_ok: bool = True, keys: int = 1, dry: int = 0) -> tuple[subprocess.CompletedProcess, Path]:
    text = BOOTSTRAP.read_text()
    start = text.index("# --- 4. SSH: keys only")
    end = text.index("# --- 5. the lean OS")
    dropin = tmp / "sshd_config.d" / "10-k1000-keyonly.conf"
    dropin.parent.mkdir(exist_ok=True)
    fb = tmp / "bin"
    fb.mkdir(exist_ok=True)
    (tmp / "t").mkdir(exist_ok=True)
    _bin(fb, "journalctl", f'echo "journalctl $*" >> "{tmp}/calls"\nprintf "%s" "{journal}"\n')
    _bin(fb, "sshd", f'echo "sshd $*" >> "{tmp}/calls"\n' + ("exit 0\n" if sshd_ok else "echo bad >&2; exit 255\n"))
    prog = (f'set -euo pipefail\nCHANGES=0 DRY={dry} KEYS_OK={keys} TMP="{tmp}/t" SSHD_KEYONLY="{dropin}"\n'
            'note() { printf "%s\\n" "$*"; }\nwould() { CHANGES=$((CHANGES + 1)); note "would: $*"; }\n'
            'die() { printf "ERROR: %s\\n" "$*" >&2; exit 1; }\n' + text[start:end] + 'echo "CHANGES=$CHANGES"\n')
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=dict(os.environ, PATH=f"{fb}:/usr/bin:/bin"))
    return r, dropin


ACCEPTED = "Accepted publickey for k1000 from 192.0.2.17 port 48310 ssh2: RSA SHA256:x\\n"


def test_bootstrap_ssh_keys_only_after_a_key_login(tmp_path):
    """F6: the first box's hand-made drop-in, reproduced only when nobody can be locked out."""
    r, dropin = _ssh_block(tmp_path, ACCEPTED)
    assert r.returncode == 0, r.stderr
    assert dropin.read_text().splitlines()[1:] == ["PasswordAuthentication no", "KbdInteractiveAuthentication no"]
    assert "sshd -t" in (tmp_path / "calls").read_text() and "CHANGES=1" in r.stdout
    assert "restart" not in BOOTSTRAP.read_text().split("# --- 4. SSH: keys only")[1].split("# --- 5.")[0].replace(
        "is not restarted", "")                              # applies at the next boot, no sshd restart
    r2, _ = _ssh_block(tmp_path, ACCEPTED)                  # there: never rewritten
    assert "CHANGES=0" in r2.stdout


@pytest.mark.parametrize("journal, keys", [("", 1), ("Accepted password for k1000 from 1.2.3.4 port 1 ssh2\\n", 1),
                                           (ACCEPTED, 0)])
def test_bootstrap_ssh_left_alone_without_proof_of_a_key_login(tmp_path, journal, keys):
    r, dropin = _ssh_block(tmp_path, journal, keys=keys)
    assert r.returncode == 0, r.stderr
    assert not dropin.exists() and "WARNING" in r.stdout and "CHANGES=0" in r.stdout


def test_bootstrap_ssh_drop_in_removed_again_when_sshd_refuses_it(tmp_path):
    r, dropin = _ssh_block(tmp_path, ACCEPTED, sshd_ok=False)
    assert r.returncode == 1 and "SSH is unchanged" in r.stderr
    assert not dropin.exists() and not list(dropin.parent.iterdir())


def test_bootstrap_ssh_dry_run_writes_nothing(tmp_path):
    r, dropin = _ssh_block(tmp_path, ACCEPTED, dry=1)
    assert r.returncode == 0 and "would: SSH: keys only" in r.stdout and not dropin.exists()


# --------------------------------------------------------------------------- bootstrap container test (F1-F6)
# Findings of the bootstrap run on a fresh Raspberry Pi OS Lite card in an emulated container.
INSTALL_DONE = "k1000-install: deployed abc1234: 0 changes\n"
CLOUD_INIT_ON = ("k1000-lean: cloud-init left on: k1000-console.service has not run at this boot. "
                 "Without the guard, a faster boot starts k1000 before the panel registers\n")


def _bootstrap_run(tmp: Path, *args: str, install_out: str = INSTALL_DONE, install_rc: int = 0,
                   lean_out: str = "k1000-lean: 0 changes\n", installed: str = "*", with_ap: bool = True,
                   engine: str | None = "pinned", pin_url: str = "",
                   **env_extra: str) -> tuple[subprocess.CompletedProcess, str]:
    """bootstrap.sh as root, in the foreground (K1000_BOOTSTRAP_FOREGROUND=1), on fakes only:
    install.sh and k1000-lean.sh print what they are given (lean also its environment), the engine
    is a fake of kind `engine` (default: the pinned build), k1000 has no SSH key (step 4 only
    warns), apt-get and systemd-run only record their calls. Returns (the run, the recorded calls)."""
    box, fb, env = _bootstrap_box(tmp, with_ap, installed, engine, pin_url)
    calls = tmp / "calls"
    calls.write_text("")
    (tmp / "install.out").write_text(install_out)
    (tmp / "lean.out").write_text(lean_out)
    helptext = "#   sudo ./install.sh --boot ...\n" + ("#   sudo ./install.sh --ap   access point\n" if with_ap else "")
    (box / "install.sh").write_text(
        f'#!/bin/sh\ncase $1 in --help) printf "{helptext}" ;; --deps) printf "python3-qrcode\\npython3-smbus2\\n" ;; '
        f'*) echo "install $*" >> "{calls}"; cat "{tmp}/install.out"; exit {install_rc} ;; esac\n')
    (box / "tools" / "k1000-lean.sh").write_text(
        '#!/bin/sh\necho "lean K1000_LEAN_FOREGROUND=${K1000_LEAN_FOREGROUND:-unset} '
        f'INVOCATION_ID=${{INVOCATION_ID:-unset}} $*" >> "{calls}"\ncat "{tmp}/lean.out"\n')
    _bin(fb, "id", '[ "$1" = -u ] && { echo 0; exit 0; }\nexit 1\n')
    _bin(fb, "apt-get", f'echo "DEBIAN_FRONTEND=${{DEBIAN_FRONTEND:-unset}} apt-get $*" >> "{calls}"\n')
    _bin(fb, "systemd-run", f'echo "systemd-run $*" >> "{calls}"\nexit 1\n')
    _bin(fb, "journalctl", "exit 0\n")
    for k in ("K1000_LEAN_FOREGROUND", "K1000_BOOTSTRAP_RC", "CAMILLADSP_TARBALL", "K1000_AP_ETC", "DEBIAN_FRONTEND"):
        env.pop(k, None)
    env = dict(env, K1000_BOOTSTRAP_FOREGROUND="1", K1000_AP_ETC=str(tmp / "apetc"), **env_extra)
    r = subprocess.run(["bash", str(box / "bootstrap.sh"), *args], capture_output=True, text=True, env=env)
    return r, calls.read_text()


def test_bootstrap_in_the_foreground_runs_lean_in_place(tmp_path):
    """F1: K1000_BOOTSTRAP_FOREGROUND=1 (no INVOCATION_ID) used to let k1000-lean.sh start its own
    k1000-lean-<time> unit, and die where systemd-run is missing: lean now runs in place."""
    r, calls = _bootstrap_run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "lean K1000_LEAN_FOREGROUND=1 INVOCATION_ID=unset \n" in calls
    assert "systemd-run" not in calls and "running as the systemd unit" not in r.stdout
    assert "install --boot --headless --ap" in calls                   # really ran in place
    r, calls = _bootstrap_run(tmp_path / "dry", "--dry-run")
    assert "lean K1000_LEAN_FOREGROUND=1 INVOCATION_ID=unset --dry-run" in calls
    assert "install " not in calls


def test_install_counts_the_changes_setup_ap_makes(tmp_path):
    """F2: install.sh --ap now adds setup-ap.sh's "k1000-ap: N changes" to its own total, also
    when the AP failed after changing things, and keeps setup-ap.sh's exit status."""
    text = INSTALL.read_text()
    block = text[text.index("AP_FAILED=0\nif [ $DO_AP = 1 ]; then"):text.index("# --- 9. optional: (re)start")]
    src = tmp_path / "src"
    (src / "net").mkdir(parents=True)
    (tmp_path / "t").mkdir()
    prog = (f'set -euo pipefail\nCHANGES=4 DO_AP=1 SRC="{src}" TMP="{tmp_path}/t"\nAP_UNITS_STALE=()\n'
            'note() { printf "k1000-install: %s\\n" "$*"; }\n' + block + 'echo "CHANGES=$CHANGES AP_FAILED=$AP_FAILED"\n')

    def run(setup_ap: str) -> str:
        (src / "net" / "setup-ap.sh").write_text(setup_ap)
        r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout

    out = run('echo "k1000-ap: installed k1000-ap-hostapd.service"\necho "k1000-ap: 3 changes"\n')
    assert out.endswith("CHANGES=7 AP_FAILED=0\n") and "k1000-ap: 3 changes" in out
    out = run('echo "k1000-ap: ERROR: the AP did not stay up" >&2\necho "k1000-ap: 2 changes"\nexit 4\n')
    assert out.endswith("CHANGES=6 AP_FAILED=1\n") and "did not stay up" in out
    out = run('echo "k1000-ap: ERROR: NetworkManager is not running" >&2\nexit 1\n')
    assert out.endswith("CHANGES=4 AP_FAILED=1\n") and "net/setup-ap.sh failed" in out
    out = run('echo "k1000-ap: 0 changes"\n')
    assert out.endswith("CHANGES=4 AP_FAILED=0\n")


def test_bootstrap_counts_install_total_once_setup_ap_included(tmp_path):
    """F2: bootstrap takes install.sh's own total line (which holds setup-ap.sh's changes), never a
    setup-ap.sh count line on top of it."""
    out = ("k1000-ap: installed k1000-ap-hostapd.service\nk1000-ap: 3 changes\n"
           "k1000-install: deployed abc1234: 3 changes\n")
    r, _ = _bootstrap_run(tmp_path, install_out=out)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "k1000-bootstrap: 3 changes. Now power-cycle the box" in r.stdout
    assert "already a K1000" not in r.stdout


def _revision_block(tmp: Path, revision: str | None, pending: str, k1000_revision: str | None = None):
    """install.sh step 3 (box/ -> /opt/k1000 + REVISION) with a fake rsync whose dry run lists
    `pending`, on a source copy without git and without a REVISION file (an rsync'd /opt/k1000-src)."""
    text = INSTALL.read_text()
    block = text[text.index('if [ "$SRC" != "$APP" ]; then'):text.index("# --- 4. settings, state and log")]
    rev_fn = re.search(r"^revision_of\(\) \{\n.*?^\}\n", text, re.S | re.M).group(0)
    src, app = tmp / "k1000-src", tmp / "opt-k1000"
    for d in (src, app, tmp / "t", tmp / "bin"):
        d.mkdir(parents=True, exist_ok=True)
    if revision is not None:
        (app / "REVISION").write_text(revision)
    (tmp / "pending").write_text(pending)
    _bin(tmp / "bin", "rsync", f'case " $* " in *" --itemize-changes "*) cat "{tmp}/pending" ;; esac\n')
    prog = ('set -euo pipefail\nCHANGES=0\nnote() { printf "%s\\n" "$*"; }\n'
            'changed() { CHANGES=$((CHANGES + 1)); note "$*"; }\n'
            'put() { PUT_CHANGED=0; if [ -f "$2" ] && cmp -s "$1" "$2"; then return 0; fi; '
            'cp "$1" "$2"; changed "installed $2"; PUT_CHANGED=1; }\n' + rev_fn +
            f'SRC="{src}" APP="{app}" PREV="{tmp}/prev" TMP="{tmp}/t"\n' + block + 'echo "CHANGES=$CHANGES"\n')
    env = {k: v for k, v in os.environ.items() if k != "K1000_REVISION"}
    env.update(PATH=f"{tmp / 'bin'}:/usr/bin:/bin", GIT_CEILING_DIRECTORIES=str(tmp))
    if k1000_revision is not None:
        env["K1000_REVISION"] = k1000_revision
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout, (app / "REVISION").read_text(), src


def test_install_keeps_revision_when_nothing_changed_and_nothing_names_it(tmp_path):
    """F3: a re-run from /opt/k1000-src without K1000_REVISION wrote "unknown (from ...)" over the
    REVISION a real deploy recorded (1 change, and bootstrap then asked for a power cycle)."""
    out, rev, _ = _revision_block(tmp_path, "abc1234\n", "")
    assert rev == "abc1234\n" and "CHANGES=0" in out, out


def test_install_revision_still_follows_a_real_change_or_a_given_revision(tmp_path):
    out, rev, _ = _revision_block(tmp_path / "a", "abc1234\n", "", k1000_revision="def5678")
    assert rev == "def5678\n" and "CHANGES=1" in out, out              # K1000_REVISION always wins
    out, rev, src = _revision_block(tmp_path / "b", "abc1234\n", ">f.st...... k1000/app.py\n")
    assert rev == f"unknown (from {src})\n" and "CHANGES=2" in out, out  # new code: the old one would lie
    out, rev, src = _revision_block(tmp_path / "c", None, "")
    assert rev == f"unknown (from {src})\n" and "CHANGES=1" in out, out  # nothing recorded yet


@pytest.mark.parametrize("install_rc, lean_out, rc, todo", [
    (0, "k1000-lean: 0 changes\n", 0, None),
    (4, CLOUD_INIT_ON + "k1000-lean: 0 changes\n", 4, "cloud-init off, the phone's access point"),
    (3, "k1000-lean: 0 changes\n", 3, "CamillaGUI"),
    (5, "k1000-lean: 0 changes\n", 3, "the phone's access point, CamillaGUI"),
], ids=["finished", "cloud-init-and-ap", "gui", "ap-and-gui"])
def test_bootstrap_says_already_a_k1000_only_when_nothing_is_left(tmp_path, install_rc, lean_out, rc, todo):
    """F4: "0 changes: this card is already a K1000" was printed in the same run as "cloud-init is
    still on" and "the phone's access point is NOT set up"."""
    r, _ = _bootstrap_run(tmp_path, install_rc=install_rc, lean_out=lean_out)
    assert r.returncode == rc, r.stdout + r.stderr
    if todo is None:
        assert "k1000-bootstrap: 0 changes: this card is already a K1000\n" in r.stdout
    else:
        assert f"k1000-bootstrap: 0 changes; still to do: {todo} (below)\n" in r.stdout
        assert "already a K1000" not in r.stdout
    assert ("cloud-init is still on" in r.stdout) == ("cloud-init" in (todo or ""))
    assert ("access point is NOT set up" in r.stdout) == ("access point" in (todo or ""))


def test_bootstrap_dry_run_on_a_finished_card_is_0_changes(tmp_path):
    """F5: install.sh has no dry run, and "would run install.sh" was counted on every dry run, so a
    finished card dry-ran to "1 changes". With --no-ap, an AP already marked down is not counted."""
    lean = "k1000-lean: dry run: 0 changes\n"
    r, calls = _bootstrap_run(tmp_path, "--dry-run", lean_out=lean)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "would run: install.sh --boot --headless --ap (" in r.stdout and "install " not in calls
    assert "k1000-bootstrap: dry run: 0 changes (install.sh's not included: it has no dry run)" in r.stdout
    r, _ = _bootstrap_run(tmp_path / "noap", "--dry-run", "--no-ap", lean_out=lean)
    assert "would: mark the phone's access point down" in r.stdout and "dry run: 1 changes" in r.stdout
    (tmp_path / "noap2").mkdir()
    (tmp_path / "noap2" / "apetc").mkdir()
    (tmp_path / "noap2" / "apetc" / "ap.info").write_text("ssid=K1000\nband=a\nstate=down\n")
    r, _ = _bootstrap_run(tmp_path / "noap2", "--dry-run", "--no-ap", lean_out=lean)
    assert "already marked down" in r.stdout and "dry run: 0 changes" in r.stdout, r.stdout


# --------------------------------------------------------------------------- bootstrap: the engine (D-019)
DRY_LEAN = "k1000-lean: dry run: 0 changes\n"
OLD_WARNING = ("WARNING: --old-engine, CamillaDSP 5.0.0-beta.1: it does NOT meet D-018's 10 ms in to out: "
               "never under 12.5 ms, and 13 to 15 ms more after one dropout (docs/17)")


def _cdsp(tmp: Path) -> Path:
    return tmp / "usr-local-bin" / "camilladsp"


def _engine_left(tmp: Path, kind: str | None) -> bool:
    """The fake engine of kind `kind` is still the one in place (None: still none)."""
    return _cdsp(tmp).read_text().count(ENGINES[kind][0]) == 1 if kind else not _cdsp(tmp).exists()


def _only_warnings(out: str) -> bool:
    """Before step 1 a run prints only its checks' warnings (not trixie, no SSH key)."""
    return all(line.startswith("k1000-bootstrap: WARNING: ") for line in out.splitlines())


def _old_tarball(tmp: Path, sha: str = OLD_TGZ_SHA256) -> Path:
    """The 5.0.0-beta.1 release tarball, faked: a fake old engine named camilladsp in it, and the
    tarball's sha256 (for the fake sha256sum) beside it."""
    import tarfile
    eng = _fake_engine(tmp / "rel" / "camilladsp", "old")
    tgz = tmp / "rel" / "camilladsp-linux-aarch64.tar.gz"
    with tarfile.open(tgz, "w:gz") as t:
        t.add(eng, arcname="camilladsp")
    eng.unlink()
    Path(f"{tgz}.sha256").write_text(sha + "\n")
    return tgz


@pytest.mark.parametrize("dry", [False, True], ids=["run", "dry"])
def test_bootstrap_keeps_the_pinned_engine_and_never_reads_a_given_binary(tmp_path, dry):
    """The second bring-up run, after a power cycle: the CAMILLADSP_BINARY of the first may be gone."""
    bindir = _cdsp(tmp_path).parent
    args = ("--dry-run",) if dry else ()
    r, calls = _bootstrap_run(tmp_path, *args, lean_out=DRY_LEAN if dry else "k1000-lean: 0 changes\n",
                              CAMILLADSP_BINARY=str(tmp_path / "gone" / "camilladsp"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "engine: CamillaDSP 5.0.0-beta.2 (0930f9b), the pinned build (D-019): nothing to do\n" in r.stdout
    assert ("dry run: 0 changes" if dry else "0 changes: this card is already a K1000") in r.stdout
    assert _cdsp(tmp_path).read_text().count(PIN_SHA256) == 1 and sorted(p.name for p in bindir.iterdir()) == ["camilladsp"]
    assert "WARNING: --old-engine" not in r.stdout and not (tmp_path / "curl-calls").exists()


@pytest.mark.parametrize("dry", [False, True], ids=["run", "dry"])
@pytest.mark.parametrize("engine", [None, "old", "other"])
def test_bootstrap_without_the_pinned_engine_or_a_binary_stops_before_any_change(tmp_path, engine, dry):
    """A fresh card, the old engine, another build: no silent install of the old engine, nothing
    changed (not even apt, though i2c-tools is missing), and the way to the pinned build."""
    args = ("--no-ap", "--dry-run") if dry else ("--no-ap",)
    r, calls = _bootstrap_run(tmp_path, *args, engine=engine, installed="python3-qrcode|python3-smbus2")
    assert r.returncode == 1, r.stdout + r.stderr
    err = r.stderr
    assert (f"k1000-bootstrap: ERROR: no engine to install: this box has {ENGINE_IS[engine]} at {_cdsp(tmp_path)}, "
            "and no CAMILLADSP_BINARY was given. Nothing was changed.\n") in err
    assert "box/tools/engine-build/build.sh" in err and "scp out/camilladsp k1000:/tmp/camilladsp" in err
    assert f"sudo CAMILLADSP_BINARY=/tmp/camilladsp {(tmp_path / 'box').resolve()}/bootstrap.sh {' '.join(args)}  " in err
    assert "add --old-engine: it does NOT meet D-018's 10 ms in to out" in err
    assert calls == "" and _only_warnings(r.stdout)           # no apt, no install.sh, no lean: nothing ran
    assert _engine_left(tmp_path, engine) and not (tmp_path / "curl-calls").exists()


@pytest.mark.parametrize("engine", [None, "old", "other"])
def test_bootstrap_installs_the_pinned_engine_from_a_local_binary(tmp_path, engine):
    """Offline: the binary build.sh made. The dry run says it, the run does it, a re-run keeps it."""
    build = _fake_engine(tmp_path / "out" / "camilladsp", "pinned")
    r, _ = _bootstrap_run(tmp_path, "--dry-run", lean_out=DRY_LEAN, engine=engine, CAMILLADSP_BINARY=str(build))
    assert r.returncode == 0, r.stdout + r.stderr
    replaces = f"; it replaces {ENGINE_IS[engine]}" if engine else ""
    assert (f"would: install CamillaDSP, the pinned build (D-019, next5 0930f9b), into {_cdsp(tmp_path)} "
            f"from {build} (sha256 checked{replaces})\n") in r.stdout
    assert "dry run: 1 changes" in r.stdout
    assert _engine_left(tmp_path, engine)

    r, _ = _bootstrap_run(tmp_path / "real", engine=engine, CAMILLADSP_BINARY=str(build))
    assert r.returncode == 0, r.stdout + r.stderr
    cdsp = _cdsp(tmp_path / "real")
    assert cdsp.read_text() == build.read_text() and os.access(cdsp, os.X_OK)
    replaced = f" (it replaces {ENGINE_IS[engine]})" if engine else ""
    assert (f"installed {cdsp}: CamillaDSP 5.0.0-beta.2 (0930f9b), the pinned build (D-019, next5 0930f9b)"
            f"{replaced}\n") in r.stdout
    assert "k1000-bootstrap: 1 changes. Now power-cycle the box" in r.stdout
    assert not list(cdsp.parent.glob("*.k1000-new")) and not (tmp_path / "real" / "curl-calls").exists()
    assert "--old-engine" not in r.stdout


@pytest.mark.parametrize("dry", [False, True], ids=["run", "dry"])
@pytest.mark.parametrize("given", ["other", "missing"])
def test_bootstrap_refuses_a_binary_that_is_not_the_pinned_build(tmp_path, given, dry):
    path = tmp_path / "out" / "camilladsp"
    if given == "other":
        _fake_engine(path, "other")
    args = ("--dry-run",) if dry else ()
    r, calls = _bootstrap_run(tmp_path, *args, engine="old", CAMILLADSP_BINARY=str(path))
    assert r.returncode == 1 and calls == "" and _only_warnings(r.stdout), r.stdout + r.stderr
    if given == "other":
        assert (f"ERROR: CAMILLADSP_BINARY={path} is not the pinned build (sha256 {'ab' * 32}, expected "
                f"{PIN_SHA256}). Nothing was changed.") in r.stderr
        assert "this is the binary measured on the box" in r.stderr
    else:
        assert f"ERROR: CAMILLADSP_BINARY={path}: no such file. Nothing was changed" in r.stderr
    assert _engine_left(tmp_path, "old")


def test_bootstrap_puts_the_old_engine_back_only_with_old_engine(tmp_path):
    """The fallback: its tarball alone never installs it; --old-engine does, and says what it costs."""
    tgz = _old_tarball(tmp_path)
    # the pinned build stays, whatever tarball is given
    r, _ = _bootstrap_run(tmp_path / "a", CAMILLADSP_TARBALL=str(tgz))
    assert r.returncode == 0 and "the pinned build (D-019): nothing to do" in r.stdout, r.stdout + r.stderr
    assert _cdsp(tmp_path / "a").read_text().count(PIN_SHA256) == 1
    # without the pinned build, the tarball alone is a stop that names --old-engine
    r, calls = _bootstrap_run(tmp_path / "b", engine="other", CAMILLADSP_TARBALL=str(tgz))
    assert r.returncode == 1 and calls == "", r.stdout + r.stderr
    assert "CAMILLADSP_TARBALL is the old engine's tarball: it is used with --old-engine only." in r.stderr
    # --old-engine, dry: said, with the warning, nothing changed
    r, _ = _bootstrap_run(tmp_path / "c", "--dry-run", "--old-engine", lean_out=DRY_LEAN, CAMILLADSP_TARBALL=str(tgz))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (f"would: install CamillaDSP, the OLD engine, 5.0.0-beta.1 (--old-engine), into {_cdsp(tmp_path / 'c')} "
            f"from {tgz} (sha256 checked; it replaces the pinned build (D-019))\n") in r.stdout
    assert r.stdout.count(OLD_WARNING) == 2 and r.stdout.rstrip().endswith(OLD_WARNING)
    assert _cdsp(tmp_path / "c").read_text().count(PIN_SHA256) == 1
    # --old-engine, offline from the tarball: installed, and said twice, the last line included
    r, _ = _bootstrap_run(tmp_path / "d", "--old-engine", CAMILLADSP_TARBALL=str(tgz))
    assert r.returncode == 0, r.stdout + r.stderr
    cdsp = _cdsp(tmp_path / "d")
    assert cdsp.read_text().count(ENGINES["old"][0]) == 1
    assert (f"installed {cdsp}: CamillaDSP 5.0.0-beta.1 (48bcd28), the OLD engine, 5.0.0-beta.1 (--old-engine) "
            "(it replaces the pinned build (D-019))\n") in r.stdout
    assert r.stdout.count(OLD_WARNING) == 2 and r.stdout.rstrip().endswith(OLD_WARNING)
    assert not (tmp_path / "d" / "curl-calls").exists()
    # --old-engine on the old engine: kept, still warned
    r, _ = _bootstrap_run(tmp_path / "e", "--old-engine", engine="old")
    assert r.returncode == 0 and "0 changes: this card is already a K1000" in r.stdout, r.stdout + r.stderr
    assert "engine: CamillaDSP 5.0.0-beta.1 (48bcd28), the old engine, 5.0.0-beta.1: nothing to do\n" in r.stdout
    assert r.stdout.count(OLD_WARNING) == 2
    # --old-engine with internet: from the release, checked after the download
    r, _ = _bootstrap_run(tmp_path / "f", "--dry-run", "--old-engine", lean_out=DRY_LEAN, engine=None)
    assert f"from {_pinned('CDSP_OLD_URL')} (sha256 checked after the download)\n" in r.stdout, r.stdout


@pytest.mark.parametrize("case, want", [
    ("both", "--old-engine and CAMILLADSP_BINARY name two different engines: give one. Nothing was changed"),
    ("bad-tarball", "is not the 5.0.0-beta.1 release tarball (missing, or another sha256). Nothing was changed"),
])
def test_bootstrap_old_engine_refusals_change_nothing(tmp_path, case, want):
    tgz = _old_tarball(tmp_path, sha=OLD_TGZ_SHA256 if case == "both" else "cd" * 32)
    extra = {"CAMILLADSP_BINARY": str(_fake_engine(tmp_path / "out" / "camilladsp", "pinned"))} if case == "both" else {}
    r, calls = _bootstrap_run(tmp_path, "--old-engine", engine="other", CAMILLADSP_TARBALL=str(tgz), **extra)
    assert r.returncode == 1 and want in r.stderr and calls == "", r.stdout + r.stderr
    assert _engine_left(tmp_path, "other")


def test_bootstrap_downloads_the_pinned_build_once_its_url_is_filled_in(tmp_path):
    """CDSP_PIN_URL is empty until the hosting is decided; filled in, the download is checked like a
    local binary, and a wrong download installs nothing."""
    url = "https://downloads.example.org/k1000/camilladsp"
    r, _ = _bootstrap_run(tmp_path / "dry", "--dry-run", lean_out=DRY_LEAN, engine=None, pin_url=url)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (f"would: install CamillaDSP, the pinned build (D-019, next5 0930f9b), into {_cdsp(tmp_path / 'dry')} "
            f"from {url} (sha256 checked after the download)\n") in r.stdout
    assert not (tmp_path / "dry" / "curl-calls").exists()
    for sub, kind, rc in (("ok", "pinned", 0), ("bad", "other", 1)):
        tmp = tmp_path / sub
        _fake_engine(tmp / "download", kind)
        r, calls = _bootstrap_run(tmp, engine=None, pin_url=url)
        assert r.returncode == rc, r.stdout + r.stderr
        assert url in (tmp / "curl-calls").read_text()
        if rc == 0:
            assert _cdsp(tmp).read_text().count(PIN_SHA256) == 1 and f"installed {_cdsp(tmp)}: " in r.stdout
        else:
            assert f"CamillaDSP binary hash mismatch ({url}): nothing installed" in r.stderr
            assert not _cdsp(tmp).exists() and "install " not in calls


def test_bootstrap_installs_its_tools_without_debconf_prompts(tmp_path):
    """F6: apt-get install without DEBIAN_FRONTEND=noninteractive logged debconf fallbacks, and on a
    terminal (foreground mode) debconf could prompt."""
    r, calls = _bootstrap_run(tmp_path, installed="python3-qrcode|python3-smbus2")    # i2c-tools missing
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends i2c-tools" in calls


def test_install_dependencies_without_debconf_prompts(tmp_path):
    """F6: install.sh step 2 (APT_DEPS) with a fake dpkg (nothing installed) and a fake apt-get."""
    text = INSTALL.read_text()
    block = text[text.index("missing=()\nfor p in \"${APT_DEPS[@]}\""):text.index("if [ -x /usr/local/bin/camilladsp ]")]
    fb = tmp_path / "bin"
    fb.mkdir()
    _bin(fb, "dpkg", "exit 1\n")
    _bin(fb, "apt-get", f'echo "DEBIAN_FRONTEND=${{DEBIAN_FRONTEND:-unset}} apt-get $*" >> "{tmp_path}/calls"\n')
    prog = ('set -euo pipefail\nCHANGES=0\nnote() { printf "%s\\n" "$*"; }\n'
            'changed() { CHANGES=$((CHANGES + 1)); note "$*"; }\nAPT_DEPS=(python3-qrcode hostapd)\n' + block)
    env = {k: v for k, v in os.environ.items() if k != "DEBIAN_FRONTEND"}
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=dict(env, PATH=f"{fb}:/usr/bin:/bin"))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "calls").read_text() == \
        "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-qrcode hostapd\n"


def test_every_apt_install_is_noninteractive():
    """F6, for every script: each apt-get call that installs or removes carries the prefix (a dry
    `apt-get -s` and apt-get named inside a message do not count)."""
    found = 0
    for script in (BOOTSTRAP, INSTALL, LEAN, BOX / "net" / "setup-ap.sh"):
        for n, line in enumerate(script.read_text().splitlines(), 1):
            code = re.sub(r'"[^"]*"', '""', line)
            code = re.sub(r"(^|\s)#.*$", "", code)
            for m in re.finditer(r"(\S*\s)?apt-get\s+(install|-y|purge|remove|autoremove)\b", code):
                found += 1
                assert (m.group(1) or "").strip() == "DEBIAN_FRONTEND=noninteractive", f"{script.name}:{n}: {line}"
    assert found >= 4                                     # bootstrap 1, install.sh 2, lean 1

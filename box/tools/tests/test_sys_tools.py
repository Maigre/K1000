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
    assert box.run("--list").stdout.split() == ["keep", "desktop", "services", "timers", "swap", "boot"]
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
def test_bootstrap_pins_camilladsp_by_tarball_and_binary_hash():
    text = BOOTSTRAP.read_text()
    assert "releases/download/v5.0.0-beta.1/camilladsp-linux-aarch64.tar.gz" in text
    for var in ("CDSP_TGZ_SHA256", "CDSP_BIN_SHA256"):
        assert re.search(rf"^{var}=[0-9a-f]{{64}}$", text, re.M), var
    assert "upgrade" not in re.sub(r"#.*", "", text)        # never apt upgrade
    assert not re.search(r"^\s*(sudo\s+)?(reboot|systemctl\s+reboot|shutdown)", text, re.M)


def _bootstrap_box(tmp: Path, with_ap: bool = False, installed: str = "*") -> tuple[Path, Path, dict]:
    """bootstrap.sh next to a fake install.sh/k1000-lean.sh; fake dpkg says `installed` (a case
    pattern) is installed. Returns (box dir, fake bin dir, env)."""
    box = tmp / "box"
    (box / "tools").mkdir(parents=True)
    shutil.copy(BOOTSTRAP, box / "bootstrap.sh")
    helptext = "#   sudo ./install.sh --boot ...\n" + ("#   sudo ./install.sh --ap   access point\n" if with_ap else "")
    (box / "install.sh").write_text(f'#!/bin/sh\ncase $1 in --help) printf "{helptext}" ;; '
                                    '--deps) printf "python3-qrcode\\npython3-smbus2\\n" ;; *) echo RAN-INSTALL "$@" ;; esac\n')
    (box / "tools" / "k1000-lean.sh").write_text('#!/bin/sh\necho "lean $*"\necho "k1000-lean: dry run: 3 changes"\n')
    for f in (box / "install.sh", box / "tools" / "k1000-lean.sh"):
        f.chmod(0o755)
    fb = tmp / "bin"
    fb.mkdir()
    _bin(fb, "dpkg", f'case $1 in --print-architecture) echo arm64 ;; -s) case $2 in {installed}) exit 0 ;; esac; exit 1 ;; esac\n')
    env = {k: v for k, v in os.environ.items() if k != "INVOCATION_ID"}
    return box, fb, dict(env, PATH=f"{fb}:/usr/bin:/bin")


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
    m = re.search(r"k1000-bootstrap: dry run: (\d+) changes", out)
    pinned = Path("/usr/local/bin/camilladsp").exists() and "pinned build, nothing to do" in out
    assert m and int(m.group(1)) == (0 if pinned else 1) + 3, out


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
    box, fb, env = _bootstrap_box(tmp_path)
    _bin(fb, "id", 'echo 0\n')
    logdir = tmp_path / "logs"
    _bin(fb, "systemd-run", f'echo "systemd-run $*" >> "{tmp_path}/calls"\n'
                            'for a in "$@"; do case $a in --setenv=K1000_BOOTSTRAP_RC=*) rc=${a#--setenv=K1000_BOOTSTRAP_RC=} ;; '
                            'StandardOutput=append:*) log=${a#StandardOutput=append:} ;; esac; done\n'
                            'echo "k1000-bootstrap: 0 changes: this card is already a K1000" >> "$log"\necho 0 > "$rc"\n')
    env = dict(env, K1000_BOOTSTRAP_LOGDIR=str(logdir), K1000_REVISION="abc1234")
    r = subprocess.run(["bash", str(box / "bootstrap.sh")], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    call = (tmp_path / "calls").read_text()
    assert "--collect" in call and "--unit=k1000-bootstrap-" in call and "--setenv=K1000_REVISION=abc1234" in call
    assert call.rstrip().endswith(f"{box.resolve()}/bootstrap.sh")
    assert "running as the systemd unit k1000-bootstrap-" in r.stdout and "already a K1000" in r.stdout
    assert "RAN-INSTALL" not in r.stdout                     # nothing ran in the SSH session itself


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
CDSP_PIN = re.search(r"^CDSP_BIN_SHA256=([0-9a-f]{64})$", BOOTSTRAP.read_text(), re.M).group(1)
INSTALL_DONE = "k1000-install: deployed abc1234: 0 changes\n"
CLOUD_INIT_ON = ("k1000-lean: cloud-init left on: k1000-console.service has not run at this boot. "
                 "Without the guard, a faster boot starts k1000 before the panel registers\n")


def _bootstrap_run(tmp: Path, *args: str, install_out: str = INSTALL_DONE, install_rc: int = 0,
                   lean_out: str = "k1000-lean: 0 changes\n", installed: str = "*", with_ap: bool = True,
                   **env_extra: str) -> tuple[subprocess.CompletedProcess, str]:
    """bootstrap.sh as root, in the foreground (K1000_BOOTSTRAP_FOREGROUND=1), on fakes only:
    install.sh and k1000-lean.sh print what they are given (lean also its environment), CamillaDSP
    is the pinned build, k1000 has no SSH key (step 4 only warns), apt-get and systemd-run only
    record their calls. Returns (the run, the recorded calls)."""
    box, fb, env = _bootstrap_box(tmp, with_ap, installed)
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
    _bin(fb, "sha256sum", f'echo "{CDSP_PIN}  $1"\n')
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

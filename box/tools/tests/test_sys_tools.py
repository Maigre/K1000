"""Tests for the system side of the box: the camilladsp.service start script, install.sh helpers,
tools/k1000-soak.sh and tools/k1000-m4-bringup.sh.

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


# --------------------------------------------------------------------------- install.sh
def test_install_sh_syntax():
    assert subprocess.run(["bash", "-n", str(INSTALL)]).returncode == 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="no shellcheck")
def test_shellcheck_clean():
    r = subprocess.run(["shellcheck", str(INSTALL), str(SOAK), str(BRINGUP)], capture_output=True,
                       text=True)
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
    # cloud-init stays enabled on purpose (it would move k1000's start onto the fbcon bind, see install.sh)
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


def _summary(tmp: Path, lines: list[str], now: str):
    log = tmp / "s.log"
    log.write_text("\n".join(lines) + "\n")
    env = dict(os.environ, K1000_SOAK_NOW=now)
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
    for needle in ("xruns reached 1", "xruns_all reached 3", "xruns_all went DOWN",
                   "camilladsp.service restarted 2 times", "k1000.service restarted 1 times",
                   "Main muted in 1 samples", "throttled: 0x50000", "1 samples not Running",
                   "k1000.service not active in 1 samples"):
        assert needle in out, (needle, out)


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

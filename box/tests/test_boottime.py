# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/k1000-boottime.sh: the boot table, on logs shaped like the box's.

Fixtures (tests/fixtures/boottime): `vclog --msg` lines in vclog.c's "%06i.%03i: <msg>" shape (ms.us
since power-on, out of order like the ring buffer), and `journalctl -b -o short-monotonic` lines with
the k1000 service's log format ("LEVEL logger: message", journald adds the rest)."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

BOX = Path(__file__).resolve().parents[1]
TIMER = BOX / "tools" / "k1000-boottime.sh"
FIX = Path(__file__).resolve().parent / "fixtures" / "boottime"


def run(tmp: Path, vclog: str | None = None, journal: str | None = None, *args: str):
    v = tmp / "vclog.txt"
    j = tmp / "journal.txt"
    v.write_text((FIX / "vclog.txt").read_text() if vclog is None else vclog)
    j.write_text((FIX / "journal.txt").read_text() if journal is None else journal)
    env = dict(os.environ, K1000_BOOTTIME_VCLOG=str(v), K1000_BOOTTIME_JOURNAL=str(j))
    return subprocess.run(["bash", str(TIMER), *args], capture_output=True, text=True, env=env)


def table(out: str) -> dict[str, str]:
    rows = {}
    for line in out.splitlines()[1:]:
        m = re.match(r"^  (.+?)\s{2,}(\S+)(.*)$", line)
        if m:
            rows[m.group(1).strip()] = m.group(2)
    return rows


def test_counts_from_power_on(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "seconds after power-on" in r.stdout.splitlines()[0]
    t = table(r.stdout)
    assert t["Linux start"] == "10.40"          # the last "Starting ARM", ms -> s
    assert t["userspace start"] == "13.61"      # 10.398 + 3.2145
    assert t["k1000 started"] == "15.80"
    assert t["engine Running"] == "17.41"       # the FIRST one
    assert t["ramp start"] == "17.60"
    assert t["Main at level"] == "20.70"
    assert t["first screen frame"] == "21.10"


def test_monotonic_offset_is_taken_out(tmp_path):
    """A journal whose monotonic clock does not start at 0 at the kernel's first line."""
    shifted = "".join(
        re.sub(r"^\[\s*([0-9.]+)\]", lambda m: f"[{float(m.group(1)) + 10.0:12.6f}]", line) + "\n"
        for line in (FIX / "journal.txt").read_text().splitlines())
    a = table(run(tmp_path).stdout)
    b = table(run(tmp_path, journal=shifted).stdout)
    assert a == b


def test_older_k1000_falls_back_and_says_so(tmp_path):
    old = "".join(line + "\n" for line in (FIX / "journal.txt").read_text().splitlines()
                  if "boot ramp" not in line and "first frame" not in line and "engine Running" not in line)
    r = run(tmp_path, journal=old)
    assert r.returncode == 0, r.stderr
    t = table(r.stdout)
    assert t["ramp start"] == "16.51" and "older k1000" in r.stdout
    assert t["first screen frame"] == "20.01"
    assert t["engine Running"] == "-" and t["Main at level"] == "-"


def test_without_the_firmware_log_counts_from_linux_start(tmp_path):
    r = run(tmp_path, vclog="")
    assert r.returncode == 0, r.stderr
    assert "after LINUX START" in r.stdout
    t = table(r.stdout)
    assert "Linux start" not in t and t["userspace start"] == "3.21"


def test_no_journal_is_an_error(tmp_path):
    r = run(tmp_path, journal="")
    assert r.returncode == 1 and "no journal lines" in r.stderr


def test_save_keeps_the_raw_logs_and_changes_nothing_else(tmp_path):
    keep = tmp_path / "kept"
    r = run(tmp_path, None, None, "--save", str(keep))
    assert r.returncode == 0, r.stderr
    assert (keep / "vclog.txt").read_text() == (FIX / "vclog.txt").read_text()
    assert (keep / "journal.txt").read_text() == (FIX / "journal.txt").read_text()


def test_the_markers_it_reads_are_the_ones_k1000_logs():
    """The timer greps log lines: they must exist, worded the same, in the service."""
    control = (BOX / "k1000" / "control.py").read_text()
    main = (BOX / "k1000" / "__main__.py").read_text()
    assert 'log.info("engine Running")' in control
    assert 'log.info("boot ramp: start,' in control and 'log.info("boot ramp: done,' in control
    assert "pushing the state, ramping in" in control
    assert 'log.info("screen: first frame")' in main and 'log.info("screen on %s", dev)' in main
    assert 'log.info("k1000 %s: profile %s' in main

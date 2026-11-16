# SPDX-License-Identifier: GPL-3.0-or-later
"""The boot splash: k1000/ui/splash.py (the render), install.sh step 5c (rendered at each deploy),
k1000's screen-up marker, and that the three agree with k1000-console.service, which paints it
(its shell behaviour: tools/tests/test_sys_tools.py, the console guard tests)."""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from k1000 import __main__ as main_mod
from k1000.ui import display, pages, splash

BOX = Path(__file__).resolve().parents[1]
INSTALL = BOX / "install.sh"
CONSOLE_UNIT = BOX / "systemd" / "k1000-console.service"


def unit_var(name: str) -> str:
    m = re.search(rf"\b{name}=([^;\s]+);", CONSOLE_UNIT.read_text())
    assert m, name
    return m.group(1)


# ------------------------------------------------------------------------------ render
@pytest.mark.parametrize("revision, label", [
    ("9184861", "build 9184861"),
    ("v0.2-5-g9184861-dirty\n", "build v0.2-5-g9184861-dirty"),
    ("e3a3deb (reinstalled from /tmp/k1000-rollback)", "build e3a3deb"),
    ("unknown (from /opt/k1000-src)", "build unknown"),
    ("", "build unknown"),
])
def test_build_label_is_the_first_word_of_revision(revision, label):
    assert splash.build_label(revision) == label


def test_default_splash_is_the_name_and_the_build():
    img, warning = splash.render("9184861")
    assert warning is None and img.size == pages.SIZE and img.mode == "RGB"
    a = np.asarray(img)
    amber = np.all(a == pages.AMBER, axis=2)
    ys, xs = np.nonzero(amber)
    assert len(xs) > 2000                                       # the K1000 name, large
    assert abs((xs.min() + xs.max()) / 2 - pages.W / 2) < 6     # centred
    assert ys.max() < pages.H - 70                              # above the build line
    assert np.asarray(splash.render("abc1234")[0]).tobytes() != a.tobytes()   # the build shows


def test_default_splash_is_the_same_bytes_for_the_same_build():
    """install.sh re-renders at every run: the same build must give 0 changes."""
    assert display.to_rgb565(splash.render("9184861")[0]) == display.to_rgb565(splash.render("9184861")[0])


def test_a_long_revision_still_fits():
    for rev in ("v0.2-133-g9184861-dirty", "x" * 120):
        img, _ = splash.render(rev)
        a = np.asarray(img)
        bottom = a[pages.H - 70:]
        cols = np.nonzero(np.any(bottom != pages.BG, axis=(0, 2)))[0]
        assert len(cols) and cols.min() >= 18 and cols.max() <= pages.W - 18, rev


def test_his_picture_is_fitted_whole_on_black(tmp_path):
    p = tmp_path / "splash.png"
    Image.new("RGB", (200, 200), (255, 0, 0)).save(p)          # square: bars left and right
    img, warning = splash.render("9184861", p)
    assert warning is None and img.size == pages.SIZE
    a = np.asarray(img)
    assert tuple(a[pages.H // 2, pages.W // 2]) == (255, 0, 0)
    assert tuple(a[pages.H // 2, 5]) == pages.BG and tuple(a[pages.H // 2, pages.W - 5]) == pages.BG
    red = np.nonzero(np.all(a == (255, 0, 0), axis=2))
    assert red[0].min() == 0 and red[0].max() == pages.H - 1   # scaled up to the full height


def test_an_unreadable_picture_gives_the_default_and_says_why(tmp_path):
    p = tmp_path / "splash.png"
    p.write_text("not a picture")
    img, warning = splash.render("9184861", p)
    assert warning and str(p) in warning and "default splash" in warning
    assert img.tobytes() == splash.render("9184861")[0].tobytes()


def test_main_writes_one_raw_panel_frame(tmp_path, capsys):
    out, png = tmp_path / "splash.rgb565", tmp_path / "look.png"
    assert splash.main(["--revision", "9184861", "--out", str(out), "--png", str(png)]) == 0
    raw = out.read_bytes()
    assert len(raw) == splash.FRAME_BYTES == pages.W * 2 * pages.H     # rows of 960 bytes, no padding
    look = Image.open(png)
    assert look.size == pages.SIZE
    assert raw == display.to_rgb565(look)
    assert capsys.readouterr().err == ""


def test_main_reads_revision_next_to_the_package(tmp_path, monkeypatch):
    rev = tmp_path / "REVISION"
    rev.write_text("abc1234\n")
    monkeypatch.setattr(splash, "REVISION_FILE", rev)
    a, b = tmp_path / "a", tmp_path / "b"
    splash.main(["--out", str(a)])
    splash.main(["--revision", "abc1234", "--out", str(b)])
    assert a.read_bytes() == b.read_bytes()
    assert splash.REVISION_FILE.name == "REVISION" and splash.read_revision(tmp_path / "none") == ""


def test_main_needs_somewhere_to_write():
    with pytest.raises(SystemExit) as e:
        splash.main(["--revision", "x"])
    assert e.value.code == 2


def test_main_warns_on_a_bad_picture_and_still_renders(tmp_path, capsys):
    bad = tmp_path / "splash.png"
    bad.write_bytes(b"\x89PNG broken")
    out = tmp_path / "s.rgb565"
    assert splash.main(["--revision", "x", "--image", str(bad), "--out", str(out)]) == 0
    assert len(out.read_bytes()) == splash.FRAME_BYTES
    assert "the default splash instead" in capsys.readouterr().err


# ------------------------------------------------------------------------------ the three agree
def test_the_unit_paints_what_install_renders_on_the_panel_it_was_rendered_for():
    unit = CONSOLE_UNIT.read_text()
    assert unit_var("splash") == "/opt/k1000/splash.rgb565"
    assert re.search(r"^SPLASH=\$APP/splash\.rgb565$", INSTALL.read_text(), re.M)
    assert "--exclude=/splash.rgb565" in INSTALL.read_text()      # step 3's mirror leaves it alone
    assert '!= 480,320 ]' in unit and '!= 16 ]' in unit and '!= 960 ]' in unit
    assert pages.SIZE == (480, 320) and splash.FRAME_BYTES == 960 * 320


def test_the_unit_stops_on_the_marker_k1000_touches():
    assert unit_var("drawn") == str(main_mod.SCREEN_UP)
    k = (BOX / "systemd" / "k1000.service").read_text()
    assert re.search(r"^RuntimeDirectory=k1000$", k, re.M)        # /run/k1000 exists while k1000 runs
    assert main_mod.SCREEN_UP.parent == Path("/run/k1000")


def test_render_loop_touches_the_marker_before_its_first_frame(tmp_path, monkeypatch):
    marker = tmp_path / "screen-up"
    monkeypatch.setattr(main_mod, "SCREEN_UP", marker)
    monkeypatch.setattr(pages, "warm_up", lambda: 0.0)
    seen: list[bool] = []
    monkeypatch.setattr(main_mod, "_draw", lambda fb, *a: seen.append(marker.exists()))

    class Ctl:
        last_input = None
        phone = None

        async def wait_changed(self, timeout: float) -> None:
            await asyncio.sleep(0.01)

        def snapshot(self):
            return ("SHOW", None, None, None)

    async def body() -> None:
        task = asyncio.get_running_loop().create_task(main_mod.render_loop(Ctl(), object()))
        try:
            for _ in range(300):
                await asyncio.sleep(0.01)
                if len(seen) >= 2:
                    break
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(body())
    assert seen[:2] == [True, True]


def test_the_marker_is_never_fatal(tmp_path):
    main_mod._mark_screen_up(tmp_path / "missing-dir" / "screen-up")      # no /run/k1000: no error
    main_mod._mark_screen_up(tmp_path / "screen-up")
    assert (tmp_path / "screen-up").exists()


# ------------------------------------------------------------------------------ install.sh step 5c
class _Step:
    """install.sh step 5c with APP in tmp (the real package, linked), the real renderer behind a
    `python3` on PATH, and a put() that copies (install.sh's own needs root)."""

    def __init__(self, tmp: Path, revision: str | None = "9184861\n", python: str | None = None) -> None:
        text = INSTALL.read_text()
        step = text[text.index("# --- 5c. the boot splash"):text.index("# --- 6. systemd")]
        self.app, self.image = tmp / "opt-k1000", tmp / "etc-splash.png"
        assert step.count("SPLASH_IMAGE=/etc/k1000/splash.png") == 1
        step = step.replace("SPLASH_IMAGE=/etc/k1000/splash.png", f"SPLASH_IMAGE={self.image}")
        self.app.mkdir()
        (self.app / "k1000").symlink_to(BOX / "k1000")
        if revision is not None:
            (self.app / "REVISION").write_text(revision)
        fb, t = tmp / "bin", tmp / "t"
        fb.mkdir()
        t.mkdir()
        py = fb / "python3"
        py.write_text("#!/bin/sh\n" + (python or f'exec "{sys.executable}" "$@"\n'))
        py.chmod(0o755)
        self.prog = ('set -euo pipefail\nCHANGES=0\n'
                     f'APP="{self.app}"\nTMP="{t}"\n'
                     'note() { printf "%s\\n" "$*"; }\n'
                     'changed() { CHANGES=$((CHANGES + 1)); note "$*"; }\n'
                     'put() { PUT_CHANGED=0; if [ -f "$2" ] && cmp -s "$1" "$2"; then return 0; fi; '
                     'cp "$1" "$2"; changed "installed $2"; PUT_CHANGED=1; }\n'
                     + step + 'echo "CHANGES=$CHANGES"\n')
        self.env = dict(os.environ, PATH=f"{fb}:/usr/bin:/bin")

    def run(self) -> tuple[str, int]:
        r = subprocess.run(["bash", "-c", self.prog], capture_output=True, text=True, env=self.env)
        assert r.returncode == 0, r.stdout + r.stderr
        m = re.search(r"^CHANGES=(\d+)$", r.stdout, re.M)
        assert m, r.stdout
        return r.stdout, int(m.group(1))

    @property
    def splash(self) -> Path:
        return self.app / "splash.rgb565"


def test_install_renders_the_build_it_deploys_once(tmp_path):
    s = _Step(tmp_path)
    out, n = s.run()
    assert n == 1 and "installed" in out, out
    assert s.splash.read_bytes() == display.to_rgb565(splash.render("9184861")[0])
    out, n = s.run()
    assert n == 0, out                                          # same build: nothing to change
    (s.app / "REVISION").write_text("abc1234 (reinstalled from /tmp/r)\n")
    out, n = s.run()
    assert n == 1 and s.splash.read_bytes() == display.to_rgb565(splash.render("abc1234")[0])
    assert "PYTHONPATH=$APP PYTHONDONTWRITEBYTECODE=1 python3 -m k1000.ui.splash" in s.prog  # no cache in /opt/k1000


def test_install_uses_his_picture_when_there_is_one(tmp_path):
    s = _Step(tmp_path)
    Image.new("RGB", (480, 320), (0, 0, 255)).save(s.image)
    out, n = s.run()
    assert n == 1, out
    assert s.splash.read_bytes() == display.to_rgb565(Image.new("RGB", (480, 320), (0, 0, 255)))


def test_install_warns_on_a_broken_picture_and_renders_the_default(tmp_path):
    s = _Step(tmp_path)
    s.image.write_text("not a picture")
    out, n = s.run()
    assert n == 1 and "WARNING: k1000.ui.splash:" in out and "the default splash instead" in out, out
    assert s.splash.read_bytes() == display.to_rgb565(splash.render("9184861")[0])


def test_install_without_revision_says_build_unknown(tmp_path):
    s = _Step(tmp_path, revision=None)
    s.run()
    assert s.splash.read_bytes() == display.to_rgb565(splash.render("")[0])


def test_a_failed_render_warns_and_removes_the_old_splash(tmp_path):
    s = _Step(tmp_path, python='echo "ModuleNotFoundError: No module named \'PIL\'" >&2; exit 1\n')
    out, n = s.run()
    assert n == 0 and "WARNING: boot splash not rendered" in out and "No module named" in out, out
    s.splash.write_bytes(b"the previous build's splash")
    out, n = s.run()
    assert n == 1 and not s.splash.exists() and "removed" in out, out

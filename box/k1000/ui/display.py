"""Framebuffer output for the ZynScreen (ILI9486 via fbtft, RGB565, 480x320).

Why a raw framebuffer and not a toolkit: the box has no display server on this panel, the
kernel driver (`fb_ili9486`) already does SPI transfers on its own (deferred I/O, ~30 fps), and
writing one RGB565 frame is a single `pwrite`. Nothing here may take the audio down: every
hardware call is wrapped, failures are logged once and reported as `False`, never raised into
the control loop.

Three facts proven on the box (2026-10-12):
- `/dev/fb0` is 480x320, 16 bpp, stride 960, standard RGB565 little-endian (the panel's BGR
  wiring is handled by the overlay's `bgr` flag).
- The Linux console (fbcon) is bound to it and would draw over us -> `unbind_console()`.
- The backlight is lit when GPIO22 is physically HIGH. fbtft maps `bl_power` onto that pin
  through the overlay's active-low/high flag, and an old overlay inverts it -> `backlight()`
  reads the pin (read-only /dev/gpiomem) and the current `bl_power` first, learns the polarity
  from that pair, and writes only if the pin is not already where it should be (no dark blink
  at service start, and nothing to write when the boot default is already right).

`bl_power` and the vtconsole `bind` are root-only (0644); the service runs as user k1000. So
the root ExecStartPre should run `python3 -m k1000.ui.display unbind backlight-on`; in the
service itself `backlight(True)` then just confirms the pin, and only logs an error when the
pin is wrong AND it cannot fix it.
"""
from __future__ import annotations

import glob
import logging
import mmap
import os
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

FB_BLANK_UNBLANK = 0      # bl_power value meaning "on" in the backlight class ABI
FB_BLANK_POWERDOWN = 4    # bl_power value meaning "off"
_GPLEV0 = 0x34            # BCM2835/2711 GPIO level register offset in /dev/gpiomem


def to_rgb565(img: Image.Image) -> bytes:
    """PIL image -> RGB565 little-endian bytes (row-major, no padding).

    Truncation (not rounding) matches what the panel shows and keeps it one vectorised pass:
    ~3 ms for 480x320 on a Pi 4.
    """
    if img.mode != "RGB":
        img = img.convert("RGB")
    a = np.asarray(img, dtype=np.uint16)
    v = ((a[:, :, 0] & 0xF8) << 8) | ((a[:, :, 1] & 0xFC) << 3) | (a[:, :, 2] >> 3)
    return v.astype("<u2").tobytes()


def find_fb_device(driver: str = "fb_ili9486", sysfs: str = "/sys") -> str | None:
    """Return `/dev/fbN` whose driver name is `driver`, or None.

    Why: fb numbering follows probe order. With an HDMI monitor attached at boot the vc4 DRM
    fbdev can take fb0 and push the panel to fb1, so the service should find the panel by name.
    """
    for name_file in sorted(glob.glob(f"{sysfs}/class/graphics/fb*/name")):
        try:
            if Path(name_file).read_text().strip() == driver:
                return "/dev/" + Path(name_file).parent.name
        except OSError:
            continue
    return None


def _write_sysfs(path: str | Path, value: str) -> None:
    with open(path, "w") as f:
        f.write(value)


def read_gpio_level(pin: int, gpiomem: str = "/dev/gpiomem") -> bool | None:
    """Physical level of a BCM GPIO via a read-only map of /dev/gpiomem, or None if unknown.

    Works for kernel-owned pins (fbtft holds GPIO22, so libgpiod cannot request it). Pi 4 /
    BCM2711 register layout only; any error just means "can't verify".
    """
    try:
        fd = os.open(gpiomem, os.O_RDONLY | os.O_SYNC)
    except OSError:
        return None
    try:
        with mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ) as m:
            (reg,) = struct.unpack_from("<I", m, _GPLEV0 + 4 * (pin // 32))
        return bool((reg >> (pin % 32)) & 1)
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


class Framebuffer:
    """One RGB565 framebuffer. `show()` writes a whole frame in one syscall, skipping repeats.

    `sysfs`, `gpiomem` and `level_reader` are injectable so the unit tests run without hardware.
    """

    def __init__(
        self,
        path: str = "/dev/fb0",
        size: tuple[int, int] = (480, 320),
        *,
        sysfs: str = "/sys",
        backlight_gpio: int | None = 22,
        backlight_lit_high: bool = True,
        gpiomem: str = "/dev/gpiomem",
        level_reader=None,
    ) -> None:
        self.path = path
        self.size = size
        self.sysfs = sysfs
        self.backlight_gpio = backlight_gpio
        self.backlight_lit_high = backlight_lit_high
        self._level = level_reader or (lambda pin: read_gpio_level(pin, gpiomem))
        self._fd: int | None = None
        self._last: bytes | None = None
        self._stride = size[0] * 2
        self._bl_inverted = False  # learnt: bl_power semantics lie (old overlay flag)
        self._error_logged = False
        self.frames_written = 0
        self.frames_skipped = 0

    # -- frame output ---------------------------------------------------------------------
    def _open(self) -> int:
        if self._fd is None:
            self._fd = os.open(self.path, os.O_WRONLY)
            self._stride = self._read_stride()
            self._last = None
        return self._fd

    def _read_stride(self) -> int:
        """Line length in bytes from sysfs (fbN only); falls back to width*2."""
        name = Path(self.path).name
        try:
            return int(Path(f"{self.sysfs}/class/graphics/{name}/stride").read_text())
        except (OSError, ValueError):
            return self.size[0] * 2

    def frame_bytes(self, img: Image.Image) -> bytes:
        """The exact bytes `show()` would write (resized if needed, padded to the stride)."""
        if img.size != self.size:
            img = img.resize(self.size)
        data = to_rgb565(img)
        row = self.size[0] * 2
        if self._stride > row:  # pad each row; never seen on the ZynScreen (stride 960)
            a = np.frombuffer(data, np.uint8).reshape(self.size[1], row)
            pad = np.zeros((self.size[1], self._stride - row), np.uint8)
            data = np.hstack([a, pad]).tobytes()
        return data

    def show(self, img: Image.Image) -> bool:
        """Write `img` to the panel. True if written, False if skipped (identical) or failed."""
        try:
            fd = self._open()
            data = self.frame_bytes(img)
            if data == self._last:
                self.frames_skipped += 1
                return False
            view = memoryview(data)
            off = 0
            while off < len(view):  # fb writes are normally complete; loop just in case
                off += os.pwrite(fd, view[off:], off)
            self._last = data
            self.frames_written += 1
            self._error_logged = False
            return True
        except OSError as e:
            if not self._error_logged:
                log.error("framebuffer %s write failed: %s", self.path, e)
                self._error_logged = True
            self.close()  # reopen on the next frame
            return False

    def invalidate(self) -> None:
        """Forget the last frame so the next `show()` always writes (e.g. after a console blink)."""
        self._last = None

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    # -- console --------------------------------------------------------------------------
    def unbind_console(self) -> bool:
        """Detach fbcon so its cursor/text stop drawing over the UI (needs root).

        Finds the vtconsole whose name says 'frame buffer' instead of assuming vtcon1.
        """
        ok = False
        for vt in sorted(glob.glob(f"{self.sysfs}/class/vtconsole/vtcon*")):
            try:
                if "frame buffer" not in Path(vt, "name").read_text():
                    continue
                if Path(vt, "bind").read_text().strip() != "0":
                    _write_sysfs(Path(vt, "bind"), "0")
                ok = True
            except OSError as e:
                log.error("cannot unbind console %s: %s", vt, e)
        self.invalidate()
        return ok

    # -- backlight ------------------------------------------------------------------------
    def _bl_power_path(self) -> Path | None:
        paths = sorted(glob.glob(f"{self.sysfs}/class/backlight/*/bl_power"))
        for p in paths:  # prefer the panel's own backlight device
            if "ili9486" in p:
                return Path(p)
        return Path(paths[0]) if paths else None

    def backlight(self, on: bool) -> bool:
        """Switch the backlight; True when the pin is verified (or unverifiable but written).

        1. Read GPIO22 and the current `bl_power`: that pair tells the driver's polarity (the
           Zynthian 2020 overlay inverts it) without toggling anything. If the pin is already
           right, stop: no write, no blink, no root needed.
        2. Otherwise write `bl_power`, re-read the pin, and flip once more if it still lies.
        A write refused for lack of root is logged with the fix, and reported as False.
        """
        path = self._bl_power_path()
        if path is None:
            log.warning("no backlight device under %s/class/backlight", self.sysfs)
            return False
        want_high = on == self.backlight_lit_high
        gpio = self.backlight_gpio
        level = self._level(gpio) if gpio is not None else None
        cur = _read_int(path)
        if level is not None and cur is not None:
            lit_now = level == self.backlight_lit_high
            self._bl_inverted = lit_now != (cur == FB_BLANK_UNBLANK)
            if level == want_high:
                return True
        try:
            _write_sysfs(path, str(self._bl_value(on)))
            if gpio is None:
                return True
            level = self._level(gpio)
            if level is None or level == want_high:
                return True
            self._bl_inverted = not self._bl_inverted
            log.warning("backlight polarity inverted by the driver; using bl_power=%d for %s",
                        self._bl_value(on), "on" if on else "off")
            _write_sysfs(path, str(self._bl_value(on)))
            return self._level(gpio) in (None, want_high)
        except PermissionError as e:
            log.error("backlight %s but cannot write %s (%s): run 'python3 -m k1000.ui.display "
                      "backlight-%s' as root (k1000.service ExecStartPre=+)",
                      "should be on" if on else "should be off", path, e, "on" if on else "off")
            return False
        except OSError as e:
            log.error("backlight write failed (%s): %s", path, e)
            return False

    def _bl_value(self, on: bool) -> int:
        on_eff = on != self._bl_inverted
        return FB_BLANK_UNBLANK if on_eff else FB_BLANK_POWERDOWN


def _read_int(path: str | Path) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    """Root helper for the service's ExecStartPre (and for a quick look over SSH).

        python3 -m k1000.ui.display unbind backlight-on     # what ExecStartPre=+ should run
        python3 -m k1000.ui.display status                  # fb device, bl_power, GPIO22, vtcon

    Commands run in order; exit 1 if any failed (the unit should ignore it with a leading '-':
    a dark screen must never stop the control service from starting).
    """
    logging.basicConfig(level=logging.INFO, format="k1000.ui.display: %(message)s")
    args = list(sys.argv[1:] if argv is None else argv) or ["status"]
    dev = find_fb_device() or "/dev/fb0"
    fb = Framebuffer(dev)
    ok = True
    for cmd in args:
        if cmd == "unbind":
            ok &= fb.unbind_console()
        elif cmd in ("backlight-on", "backlight-off"):
            ok &= fb.backlight(cmd == "backlight-on")
        elif cmd == "status":
            bl = fb._bl_power_path()
            binds = {}
            for v in sorted(glob.glob("/sys/class/vtconsole/vtcon*")):
                try:
                    binds[Path(v).name] = (Path(v, "bind").read_text().strip(),
                                           Path(v, "name").read_text().strip())
                except OSError as e:
                    binds[Path(v).name] = ("?", str(e))
            pin = fb.backlight_gpio if fb.backlight_gpio is not None else 22
            print(f"fb={dev} bl_power={_read_int(bl) if bl else None} ({bl}) "
                  f"gpio{pin}={read_gpio_level(pin)} vtcon={binds}")
        else:
            print(f"unknown command {cmd!r} (unbind | backlight-on | backlight-off | status)",
                  file=sys.stderr)
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

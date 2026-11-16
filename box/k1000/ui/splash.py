# SPDX-License-Identifier: GPL-3.0-or-later
"""The boot splash: what the ZynScreen shows from the moment its driver registers until k1000's
first frame. By default the K1000 name and the build; `/etc/k1000/splash.png` (any picture PIL
reads) replaces it.

Why it is rendered ahead, as the panel's raw bytes, and not drawn at boot:
- Nothing on the boot path may wait for Python and PIL: their imports alone cost most of a second
  on the Pi 4, on the two cores k1000 and everything but the engine start on. install.sh runs this
  once per deploy (step 5c) and writes `/opt/k1000/splash.rgb565`.
- At boot `k1000-console.service` (root, plain sh) copies that file onto the panel's framebuffer
  the moment the panel registers, and again after each console unbind (fbcon draws on the panel
  until then), and stops as soon as k1000 is about to draw its first frame (it touches
  `__main__.SCREEN_UP` first). One `cat`: no process to start, nothing to decode.
- The bytes are exactly one 480x320 RGB565 frame, rows of 960 bytes with no padding: what
  `display.Framebuffer` writes. The unit paints it only on a panel of that geometry.

    python3 -m k1000.ui.splash --out splash.rgb565                 # the build from ../REVISION
    python3 -m k1000.ui.splash --revision abc1234 --png look.png   # see it on a laptop
    python3 -m k1000.ui.splash --image /etc/k1000/splash.png --out splash.rgb565
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from . import pages
from .display import to_rgb565

# REVISION sits next to the package on the box (/opt/k1000/REVISION, install.sh step 3).
REVISION_FILE = Path(__file__).resolve().parents[2] / "REVISION"
FRAME_BYTES = pages.W * pages.H * 2


def build_label(revision: str) -> str:
    """`build <first word of REVISION>`: "9184861-dirty (reinstalled from /tmp/x)" -> "build
    9184861-dirty"; nothing usable -> "build unknown"."""
    words = (revision or "").split()
    return "build " + (words[0] if words else "unknown")


def read_revision(path: Path | None = None) -> str:
    try:
        return (path or REVISION_FILE).read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def default_image(revision: str) -> Image.Image:
    """The K1000 name, large, and the build under it."""
    img = Image.new("RGB", pages.SIZE, pages.BG)
    d = ImageDraw.Draw(img)
    d.text((pages.W // 2, pages.H // 2 - 24), "K1000", font=pages.font(120), fill=pages.AMBER,
           anchor="mm")
    label, size, max_w = build_label(revision), 26, pages.W - 40

    def width(text: str) -> float:
        return d.textlength(text, font=pages.font(size, pages.FONT_REG))

    while size > 14 and width(label) > max_w:
        size -= 2
    while len(label) > 8 and width(label) > max_w:     # still too long: cut the end, say so
        label = label[:-2].rstrip("…") + "…"
    d.text((pages.W // 2, pages.H - 48), label, font=pages.font(size, pages.FONT_REG),
           fill=pages.DIM, anchor="mm")
    return img


def picture(path: Path) -> Image.Image:
    """His picture, fitted whole into the panel (aspect kept, black bars), never cropped."""
    with Image.open(path) as src:
        src = ImageOps.exif_transpose(src).convert("RGB")
        fitted = ImageOps.contain(src, pages.SIZE)
    img = Image.new("RGB", pages.SIZE, pages.BG)
    img.paste(fitted, ((pages.W - fitted.width) // 2, (pages.H - fitted.height) // 2))
    return img


def render(revision: str, image: Path | None = None) -> tuple[Image.Image, str | None]:
    """(the splash, a warning or None). A picture that cannot be read gives the default splash
    and says why: a deploy never fails over the splash."""
    if image is not None:
        try:
            return picture(image), None
        except Exception as e:   # noqa: BLE001 - any unreadable file: the default instead
            return default_image(revision), f"{image}: {e}; the default splash instead"
    return default_image(revision), None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m k1000.ui.splash",
                                description="Render the K1000 boot splash")
    p.add_argument("--revision", help=f"the build to show (default: the first line of {REVISION_FILE})")
    p.add_argument("--image", type=Path, help="a picture instead of the K1000 name and build")
    p.add_argument("--out", type=Path, help="write the panel's raw RGB565 frame here")
    p.add_argument("--png", type=Path, help="write a PNG here (to look at it)")
    a = p.parse_args(argv)
    if a.out is None and a.png is None:
        p.error("nothing to write: give --out and/or --png")
    revision = a.revision if a.revision is not None else read_revision()
    img, warning = render(revision, a.image)
    if warning:
        print(f"k1000.ui.splash: {warning}", file=sys.stderr)
    if a.out is not None:
        raw = to_rgb565(img)
        assert len(raw) == FRAME_BYTES, len(raw)
        a.out.write_bytes(raw)
    if a.png is not None:
        img.save(a.png)
    return 0


if __name__ == "__main__":
    sys.exit(main())

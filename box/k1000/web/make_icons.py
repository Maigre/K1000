# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the PWA icons (static/icons/*.png): `python3 -m k1000.web.make_icons [outdir]`.

Run once on a dev machine and commit the PNGs (they are small); the box never runs this.
- icon-*.png (purpose "any"): a rounded black tile, an amber knob, "K1000" in white.
- maskable-*.png (purpose "maskable"): full-bleed black, the same drawing inside the central
  80 % safe zone, so Android's circle/squircle masks never cut the knob or the name.
Drawn at 1024 px and downscaled (LANCZOS) for clean edges at 192 and 512.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

AMBER = (255, 184, 0, 255)
WHITE = (255, 255, 255, 255)
KNOB = (28, 28, 28, 255)
BLACK = (0, 0, 0, 255)
BIG = 1024
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _font(px: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(FONT, px)
    except OSError:
        return ImageFont.load_default(px)


def _drawing(size: int, scale: float) -> Image.Image:
    """The knob + name on a transparent square, scaled about the centre by `scale`."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = size / 2

    def p(x: float, y: float) -> tuple[float, float]:   # unit coords (0..1) -> pixels, scaled
        return (c + (x - 0.5) * size * scale, c + (y - 0.5) * size * scale)

    def r(v: float) -> float:
        return v * size * scale

    kx, ky, kr = 0.5, 0.40, 0.25
    # scale ticks (a pot's 270 degree travel)
    for i in range(11):
        a = math.radians(135 + i * 27)
        x0, y0 = p(kx + math.cos(a) * (kr + 0.05), ky + math.sin(a) * (kr + 0.05))
        x1, y1 = p(kx + math.cos(a) * (kr + 0.10), ky + math.sin(a) * (kr + 0.10))
        d.line((x0, y0, x1, y1), fill=AMBER if i < 8 else (110, 80, 0, 255), width=max(2, int(r(0.022))))
    cx, cy = p(kx, ky)
    d.ellipse((cx - r(kr), cy - r(kr), cx + r(kr), cy + r(kr)), fill=AMBER)
    d.ellipse((cx - r(kr - 0.045), cy - r(kr - 0.045), cx + r(kr - 0.045), cy + r(kr - 0.045)), fill=KNOB)
    a = math.radians(135 + 7 * 27)       # the indicator, at the 8th tick
    ex, ey = p(kx + math.cos(a) * (kr - 0.07), ky + math.sin(a) * (kr - 0.07))
    d.line((cx, cy, ex, ey), fill=WHITE, width=max(3, int(r(0.045))))
    d.ellipse((ex - r(0.024), ey - r(0.024), ex + r(0.024), ey + r(0.024)), fill=WHITE)
    tx, ty = p(0.5, 0.83)
    d.text((tx, ty), "K1000", font=_font(int(r(0.19))), fill=WHITE, anchor="mm")
    return img


def icon_any(px: int) -> Image.Image:
    base = Image.new("RGBA", (BIG, BIG), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    d.rounded_rectangle((0, 0, BIG - 1, BIG - 1), radius=int(BIG * 0.2), fill=BLACK,
                        outline=(60, 60, 60, 255), width=int(BIG * 0.012))
    base.alpha_composite(_drawing(BIG, 0.9))
    return base.resize((px, px), Image.LANCZOS)


def icon_maskable(px: int) -> Image.Image:
    base = Image.new("RGBA", (BIG, BIG), BLACK)
    base.alpha_composite(_drawing(BIG, 0.72))   # inside the 80 % safe circle
    return base.convert("RGB").resize((px, px), Image.LANCZOS)


def main(outdir: str | None = None) -> list[Path]:
    out = Path(outdir) if outdir else Path(__file__).resolve().parent / "static" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for px in (192, 512):
        for name, img in ((f"icon-{px}.png", icon_any(px)), (f"maskable-{px}.png", icon_maskable(px))):
            path = out / name
            img.save(path, optimize=True)
            written.append(path)
    return written


if __name__ == "__main__":
    for p in main(sys.argv[1] if len(sys.argv) > 1 else None):
        print(p, p.stat().st_size, "bytes")

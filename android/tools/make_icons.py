#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Launcher icon for the K1000 Android app, from the PWA's own maskable icon.

The PWA's maskable-512.png (box/k1000/web/static/icons/, drawn by box/k1000/web/make_icons.py)
keeps its drawing inside the central 80 % circle. An Android adaptive icon shows the central
72 dp of a 108 dp layer, so the maskable image is scaled to 90/108 of the layer: its 80 % safe
circle then fills the 72 dp viewport exactly, and the black background layer matches its own
black. Writes app/src/main/res/mipmap-<density>/ic_launcher_foreground.png, and
ic_launcher_monochrome.png (the drawing's brightness as alpha, for Android 13+ themed icons).

Usage: python3 tools/make_icons.py <path/to/maskable-512.png>   (needs Pillow)
"""
import sys
from pathlib import Path

from PIL import Image

DENSITIES = {"mdpi": 1.0, "hdpi": 1.5, "xhdpi": 2.0, "xxhdpi": 3.0, "xxxhdpi": 4.0}
LAYER_DP = 108
IMAGE_DP = 90


def main() -> int:
    src = Image.open(sys.argv[1]).convert("RGBA")
    res = Path(__file__).resolve().parent.parent / "app" / "src" / "main" / "res"
    for name, scale in DENSITIES.items():
        layer = round(LAYER_DP * scale)
        size = round(IMAGE_DP * scale)
        img = src.resize((size, size), Image.LANCZOS)
        out = Image.new("RGBA", (layer, layer), (0, 0, 0, 0))
        off = (layer - size) // 2
        out.paste(img, (off, off))
        d = res / f"mipmap-{name}"
        d.mkdir(parents=True, exist_ok=True)
        out.save(d / "ic_launcher_foreground.png", optimize=True)
        mono = Image.new("RGBA", (layer, layer), (0, 0, 0, 0))
        glyph = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        glyph.putalpha(img.convert("L"))
        mono.paste(glyph, (off, off))
        mono.save(d / "ic_launcher_monochrome.png", optimize=True)
        print(d, layer)
    return 0


if __name__ == "__main__":
    sys.exit(main())

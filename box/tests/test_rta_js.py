# SPDX-License-Identifier: GPL-3.0-or-later
"""The phone-mic math (k1000/web/static/rta.js) is JavaScript: its unit tests are node's
(tests/js/*.test.js). This runs them inside the pytest suite when node is installed (dev
machines); the box has no node, so there it is skipped."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS = sorted((Path(__file__).parent / "js").glob("*.test.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_rta_js_unit_tests() -> None:
    assert JS, "no JS tests found"
    out = subprocess.run(["node", "--test", *map(str, JS)], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout[-4000:] + out.stderr[-2000:]

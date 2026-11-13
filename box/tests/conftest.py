# SPDX-License-Identifier: GPL-3.0-or-later
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from k1000 import state as _state


@pytest.fixture(autouse=True)
def _engine_rate_per_test():
    """The engine's sample rate is one per process (state.set_engine_rate, D-022: the Controller
    sets it from its profile). Each test starts at the default and leaves it there, whatever
    profile it ran: no test passes or fails because of the rate a previous one left."""
    _state.set_engine_rate(_state.DEFAULT_SAMPLERATE)
    yield
    _state.set_engine_rate(_state.DEFAULT_SAMPLERATE)

# 8 — Development

For anyone changing the code. The design contract is [`docs/03-v0-architecture.md`](../03-v0-architecture.md);
this chapter is the map and the working habits.

## Repository layout

| Path | What |
|---|---|
| `README.md` | The landing page |
| `DECISIONS.md` | The decision log (D-001 …) |
| `ROADMAP.md` | Phases, gates, progress |
| `docs/requirements.md`, `docs/NN-*.md` | Requirements, reviews, plans, specs, numbered in creation order |
| `docs/manual/` | This manual |
| `docs/ui/` | Screens drawn by the code (`docs/ui/v1/`: the UI v1 design mockups, kept as design history) |
| `docs/04-operator-card.md`, `docs/13-quick-start.md` | The two printed pages for the gig case |
| `tools/make-print.sh` | Renders those two pages to PDF ([below](#printing-the-cards)) |
| `box/` | Everything that goes on the Pi. Mirrored to `/opt/k1000/` by `install.sh` |
| `box/bootstrap.sh`, `box/install.sh` | A fresh card → K1000; deploy and update ([chapter 2](02-installation.md)) |
| `box/README.md` | The ops reference on the box |
| `box/etc/k1000.yaml` | The settings template |
| `box/k1000/` | The Python package: the control service |
| `box/systemd/` | `camilladsp`, `k1000`, `camillagui`, `k1000-console` units, and `k1000-laptop.path` / `k1000-laptop.service` (SETUP › PHONE › Laptop access) |
| `box/net/` | The phone's access point: `setup-ap.sh`, its units, its DNS files, the subnet guard `k1000-ap-guard.sh`, and `k1000-laptop-access.sh` (the root side of Laptop access) |
| `box/overlays/k1000-zynscreen.dts` | The screen's device-tree overlay (GPL-2.0) |
| `box/camillagui/` | CamillaGUI's config for K1000 |
| `box/tests/` | Unit tests of the package |
| `box/tools/` | Ops tools (`k1000-lean.sh`, `k1000-soak.sh`, `k1000-m4-bringup.sh`, `k1000-knobtrace.py`) and their tests in `tools/tests/` |

### The package `box/k1000/`

| Module | Does |
|---|---|
| `__main__.py` | Wiring: settings, state recovery, the controller, the screen and knob tasks (retried for life), the phone server, the `--stdin` console |
| `control.py` | **The brain.** One state, one apply worker, the boot ramp, adoption of outside changes, the drift policy, persistence; the UI v1 actions (notch helper, reset and its undo, Lo-Cut, master-up guard, auto-home), alerts and pills |
| `state.py` | The persistent model, every range (`RANGES`), atomic writes |
| `profiles.py` | Device profiles (`dev`, `m4`), the settings file, ALSA probing |
| `dspconfig.py` | CamillaDSP v5 config from profile + state; click-safe change plans; drift detection |
| `cdsp.py` | The asyncio websocket client for CamillaDSP v5 |
| `rta.py` | The SHOW spectrum: `GetSpectrum` on its own second websocket, the ballistics and the PEAK detector |
| `laptop.py` | SETUP › PHONE › Laptop access, the k1000 side: asks root through a flag in `/run/k1000`, follows root's status |
| `tune.py` | The `/tune` page's filter-list parser (REW, EqualizerAPO, typed lines) and its apply/undo |
| `hw/mcp23017.py` | The knob and button driver (MCP23017 + gpiod interrupts, the quadrature decoder) |
| `hw/simchip.py` | A real-time simulated MCP23017, for the knob timing test and the knob trace's bench |
| `ui/pages.py`, `ui/display.py` | The screen pages (SHOW, ALIGN, SETUP, the Knob test) and the framebuffer (changed rows only) |
| `ui/input_map.py` | Knob and button meaning per page, as a table |
| `ui/qr.py` | The PHONE page, with its QR codes |
| `ui/eqgraph.py` | The EQ curve maths (the engine's own biquads) and the graph drawing |
| `ui/mock_v1.py` | The UI v1 design mockups (`docs/ui/v1/`); not used by the service |
| `web/server.py`, `web/static/`, `web/tune/` | The phone page (PWA), its websocket bridge, the first-time page, and the laptop's `/tune` page |
| `web/make_icons.py` | Draws the PWA icons in `web/static/icons/` (run on a dev machine; the box never runs it) |

## Architecture in one page

```
camilladsp.service   CamillaDSP 5.0.0-beta.1, boots MUTED at -40 dB from /var/lib/k1000/active.yml
        ▲ websocket 127.0.0.1:1234
k1000.service        python3 -m k1000: knobs, screen, phone page, state, persistence
        │ writes /var/lib/k1000/state.json + active.yml (atomic)
camillagui.service   CamillaGUI on :5005, wired network (or the K1000 Wi-Fi during Laptop access)
```

k1000 also opens a **second websocket** to the engine for the SHOW spectrum (`rta.py`), only while
SHOW is on screen and Spectrum is ON, so a spectrum poll never queues in front of a mute.

- **Audio never depends on the UI.** If k1000 dies, the engine keeps playing as it was. If the
  engine restarts, it comes up muted and k1000 ramps it in once audio flows.
- **A fixed-slot pipeline.** Every filter exists from boot; every show-time action is a parameter
  write or a ramped fader, never a pipeline rebuild. Mono L+R, the profile and the dev audition
  are the only rebuilds, and they are SETUP actions with a confirm (chosen with E2, set by a push).
- **One state, every surface.** Knobs, phone and CamillaGUI edit the same engine. K1000 adopts what
  it owns, keeps any cut or mute made outside it, and pulls raises back down. It never releases a
  mute it did not make (the Aux4 marker records what it holds).

Details: `docs/03-v0-architecture.md` §2–§3. Why it is built this way: D-002 (architecture), D-004
(CamillaDSP v5 beta), D-005/D-006 (Pi OS Lite), D-007 (phone), D-009 (UI v1), D-013 (buttons, SETUP) in
[`DECISIONS.md`](../../DECISIONS.md).

## Running the tests

### On a laptop

The unit tests need no hardware and no CamillaDSP. Python 3.13 or newer:

```sh
python3 -m venv ~/.venvs/k1000
~/.venvs/k1000/bin/pip install pyyaml websockets numpy pillow qrcode pytest
cd box
~/.venvs/k1000/bin/python -m pytest -q -p no:cacheprovider -m "not engine and not hw" tests tools/tests
```

A few tests skip themselves when a tool is missing (a QR decoder, `shellcheck`, a Pi-only timing
budget).

### On the box

```sh
cd /opt/k1000 && python3 -m pytest -q -p no:cacheprovider tests tools/tests     # unit + engine tests
cd /opt/k1000 && python3 -m pytest -q -p no:cacheprovider -m hw                 # hardware: stop k1000 first
```

| Marker | Needs | Default |
|---|---|---|
| (none) | nothing | runs |
| `engine` | the `camilladsp` binary | runs where it is installed, skipped elsewhere |
| `hw` | the real MCP23017 and its GPIO lines | **off** (`pytest.ini`: `-m "not hw"`); opt in with `-m hw`, with k1000 stopped |

**The engine tests never open the sound card.** They start private CamillaDSP instances on
`SignalGenerator` → `File` devices, on their own ports, so they can run on a box that is playing:

| Variable | Default port | Tests |
|---|---|---|
| `K1000_CDSP_TEST_PORT` | 12342 | `test_cdsp.py` |
| `K1000_DSPCONFIG_PORT` | 12341 | `test_dspconfig.py` |
| `K1000_CONTROL_TEST_PORT` | 12346 | `test_control.py`, `test_rta.py` |
| `K1000_SYS_TEST_PORT` | 12372 | `tools/tests/test_sys_tools.py` |

To test a working tree on the box without deploying it, run the suite from a scratch copy:

```sh
ssh k1000 'mkdir -p dev'
rsync -a --delete --exclude __pycache__ box/ k1000:dev/k1000-test/
ssh k1000 'cd dev/k1000-test && python3 -m pytest -q -p no:cacheprovider tests tools/tests'
```

## The bench console

`python3 -m k1000 --stdin` reads one command per line and answers on stdout, prefixed with `@`.
With `--test-devices` the engine plays a test sine into `/dev/null` instead of the sound card, so
it is safe anywhere. On the box, use a private port and private paths so it never meets the
running service:

```sh
mkdir -p ~/bench && cd ~/bench
cat > k1000.yaml <<EOF
profile: dev
engine: {url: "ws://127.0.0.1:12399"}
paths: {state: $HOME/bench/state.json, active_config: $HOME/bench/active.yml}
EOF
/usr/local/bin/camilladsp -w -p 12399 -a 127.0.0.1 > camilladsp.log 2>&1 &
PYTHONPATH=/opt/k1000 python3 -m k1000 --settings ~/bench/k1000.yaml \
    --headless --no-hw --no-phone --stdin --test-devices
```

(`-w`: the engine waits for a config; k1000 uploads one. Stop the engine with `kill %1` after
`quit`.)

| Command | Does |
|---|---|
| `status` | What the screen would show, as JSON (`@status {...}`) |
| `state` | The whole state (`@state {...}`) |
| `nudge <field> <detents> [fine]` | As a knob turn: `nudge master_db -2` → −1 dB |
| `set <field> <json value> [confirm]` | Set a value (`confirm` for setup actions) |
| `toggle <field> [confirm]` | Flip a switch, or the next choice |
| `mute on\|off\|toggle` | Main mute |
| `reset_live`, `undo_reset` | Live EQ flat (SETUP › LIVE EQ › Reset live EQ); put it back (SETUP › LIVE EQ › Undo reset) |
| `notch` | The notch helper, as B3 on SHOW |
| `page <SHOW\|ALIGN\|SETUP\|HWTEST\|PHONE>`, `next_page` | Change page (the v0 names `MAIN` and `LIVE` still mean ALIGN and SHOW) |
| `laptop open\|close\|status` | SETUP › PHONE › Laptop access |
| `hint <text>` | Show a hint |
| `phone on\|off\|toggle\|status` | Phone control (needs the phone server: drop `--no-phone`) |
| `ev enc <0-3> <detents>` | A physical encoder event, through the `controls:` map |
| `ev sw <0-7> <press\|release\|short\|long>` | A physical switch event (B4, the mute, is `sw 7` by default: `press` then `long` = hold B4) |
| `save`, `settle` | Save now; wait until the engine has everything |
| `help`, `quit` | — |

Field names are the `state.json` paths: `master_db`, `balance_db`, `live_peq.2.gain`,
`out_trim_db.1`, `live_lowshelf.freq` ([chapter 3](03-configuration.md#11-statejson-fields)),
plus `align_delay_ms`, ALIGN E2's signed delay (`sub_delay_ms − top_delay_ms`, D-011): `nudge
align_delay_ms -1`, `set align_delay_ms -1.25` (the tops 1.25 ms later).

Other flags: `--headless` (no screen), `--no-hw` (no knobs), `--no-phone`, `--url`, `--state`,
`--active-config`, `--test-level <dBFS>` (the test sine, default −20), `--log-level`.

**Screens without hardware:** `cd box && python3 -m k1000.ui.pages ../docs/ui` redraws every
SHOW, ALIGN, SETUP, alert and Knob test picture into `docs/ui/` (`python3 -m k1000.ui.pages
--bench 300` times SHOW's render instead), and `python3 -m k1000.ui.qr ../docs/ui` the PHONE pages.
The UI v1 design mockups: `python3 -m k1000.ui.mock_v1 ../docs/ui/v1`. The manual
([chapter 4](04-usage.md)) embeds the `docs/ui/` pictures by name: keep the names, or fix the
links in the same commit.

## The knob tools

**Monitor** (what each physical control sends):

```sh
sudo systemctl stop k1000            # it owns the chip; audio keeps playing, B4 does nothing meanwhile
cd /opt/k1000 && python3 -m k1000.hw.mcp23017 --monitor       # --raw: every pin change; --selftest: the interrupt lines
sudo systemctl start k1000
```

**Trace** (`box/tools/k1000-knobtrace.py`): records every contact change with known turns, to tell
the encoder type and the decoder's errors apart. Analysis and fix:
[`docs/08-knob-precision.md`](../08-knob-precision.md).

| Command | Does |
|---|---|
| `capture --guided -o <file>` | About 2 minutes of prompted turns, then the analysis and the `controls:` lines to use. Refuses to run while k1000 runs |
| `capture --quick` | E1 only, about 1 minute |
| `analyze <file>` | Re-reads a saved trace, and replays it through the installed decoder |
| `demo` | What a 4- and a 2-transition encoder look like (synthetic) |
| `bench` | The real knob worker on a simulated chip, beside SHOW's render load: its sampling delay and the clicks lost at fast turns; no hardware |

```sh
sudo systemctl stop k1000
python3 /opt/k1000/tools/k1000-knobtrace.py capture --guided -o ~/knobs-1.trace
sudo systemctl start k1000
```

## Printing the cards

The two pages for the gig case, the [operator card](../04-operator-card.md) and the
[quick start](../13-quick-start.md), are Markdown in `docs/`. `tools/make-print.sh` (at the
repository root, on the laptop) renders them to PDF:

```sh
tools/make-print.sh               # into ./print/ (gitignored)
tools/make-print.sh ~/k1000-print # or any folder
```

It uses `~/.local/share/md-to-pdf/md_to_pdf.sh` when it is there (pandoc + LuaLaTeX, 9 pt, 1 cm
margins, no contents page), else plain `pandoc` with the same page settings, else it says what to
install. Each page must stay **one A4 page**: the script checks the page count with `pdfinfo`
when it can and warns otherwise. If a page spills, shorten the wording, not the font.

## Coding rules

- **An SPDX line at the top of every source file**: `# SPDX-License-Identifier: GPL-3.0-or-later`
  (the overlay: `GPL-2.0`).
- **Nothing secret in the repository**, ever: no keys, no passwords, no certificates, no private
  network details, no client or venue names. The repository is public.
- English for code and docs.
- **The audio rules** (docs/03 §6): never change the pipeline at show time; never let a crash or a
  restart produce a loud transient (boot muted, ramp in, clamp every value to `RANGES`); never move
  a fader into an engine that is not Running; never undo the operator's mute, nor a cut or mute
  made outside K1000; writes that click (delay, polarity) go under a group mute.
- **Hardware access is wrapped**, so unit tests run without hardware. Hardware tests are opt-in
  (`-m hw`). A missing screen or knob board never stops the service: it is retried.
- **Engine tests use private ports** and virtual devices, never the sound card.
- Comments say **why**, not what.

## How decisions are recorded

- **`DECISIONS.md`**: one entry per decision (context, options, choice, why). States: `proposed`
  (anyone suggests) → `decided` (only the author decides) → `superseded by D-NNN`. A decided entry
  is never rewritten: a new one supersedes it.
- **Reviews and studies** go to `docs/NN-<topic>.md`, numbered in creation order. A doc a decision
  cites is not rewritten afterwards: a follow-up doc is added instead.
- **`ROADMAP.md`** keeps the phases, the gates and dated progress lines.
- This manual follows the code: when a behaviour changes, the chapter that describes it changes in
  the same commit.

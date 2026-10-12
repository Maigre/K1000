# SPDX-License-Identifier: GPL-3.0-or-later
"""Device profiles: which ALSA device, how many channels, which inputs/outputs carry what.

Everything up to the 4 logical outputs (0 Sub L, 1 Sub R, 2 Top L, 3 Top R) is identical across
profiles; a profile only decides the device block, the `input` mixer (program L/R) and the
`output` mixer (logical -> physical). See docs/03-v0-architecture.md §3 "Profiles".

Why probing: the MOTU M4's capture channel count/format are not known before it is plugged in,
and an ALSA `hw:` device refuses a wrong channel count. `probe_alsa` reads
`aplay/arecord --dump-hw-params`; when the device is busy (CamillaDSP holds it) the USB stream
description in /proc/asound/<card>/stream0 is read instead, which never opens the PCM.
Nothing here falls back silently to another device: the `m4` profile is pinned for gigs. A pin
that cannot be honoured as written (unknown name, unreadable file) is never replaced by `dev`
quietly: `choose_profile` returns an `error` for the screen and prefers the last running profile.

Why formats are not taken from the probe: the generated `devices` block must be the same for the
same hardware whether or not the probe ran (device absent at boot, busy, ...). Otherwise a mere
k1000 restart would see a device "difference", and fixing a device difference restarts the audio
devices (an audible drop). So `format` is written only when set explicitly (or proven, `dev`);
otherwise CamillaDSP auto-selects (S32_LE first). The probe validates and decides channel counts,
which a `hw:` device does not negotiate.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

log = logging.getLogger(__name__)

SETTINGS_PATH = "/etc/k1000/k1000.yaml"
PROC_ASOUND = "/proc/asound"

LOGICAL_NAMES: tuple[str, ...] = ("Sub L", "Sub R", "Top L", "Top R")
_LOGICAL_ALIASES = {
    "subl": 0, "sub_l": 0, "sub l": 0,
    "subr": 1, "sub_r": 1, "sub r": 1,
    "topl": 2, "top_l": 2, "top l": 2,
    "topr": 3, "top_r": 3, "top r": 3,
}

# ALSA format name -> CamillaDSP format name (backend_alsa.md "Sample formats").
ALSA_TO_CDSP: dict[str, str] = {
    "S16_LE": "S16_LE",
    "S24_3LE": "S24_3_LE",
    "S24_LE": "S24_4_LE",
    "S32_LE": "S32_LE",
    "FLOAT_LE": "F32_LE",
    "FLOAT64_LE": "F64_LE",
}
# CamillaDSP's own auto-select order: 32-bit int, 24-bit, 16-bit, floats last.
FORMAT_PREFERENCE: tuple[str, ...] = ("S32_LE", "S24_3_LE", "S24_4_LE", "S16_LE", "F32_LE", "F64_LE")
CDSP_FORMATS = frozenset(FORMAT_PREFERENCE)

# devices.target_level: playback buffer (frames) kept when a chunk arrives. 256 = 1 chunk, the
# contract value, proven on the I2S HiFiBerry. A USB interface may need more margin against
# underruns (more latency: 256 frames = 5.3 ms at 48 kHz): `profiles.<name>.target_level` in
# k1000.yaml, clamped to CamillaDSP's usable range ((2 + queuelimit 4) * chunksize 256 max). A
# change is a devices change: it takes effect at the next ENGINE start (k1000 rewrites
# active.yml when it starts), never at show time.
TARGET_LEVEL = 256
TARGET_LEVEL_RANGE = (64, 1536)


# Key `load_settings` adds when the file exists but cannot be used as written.
SETTINGS_ERROR = "_error"


class ProfileError(ValueError):
    """Unknown profile name or an override that cannot be used."""


@dataclass(frozen=True)
class ProfileChoice:
    """Which profile to run and why. `error` set = show it on the screen (the pin is in doubt)."""

    name: str
    source: str                     # "settings" | "state" | "default"
    error: str | None = None


@dataclass
class HwParams:
    """What one PCM direction accepts (subset relevant to CamillaDSP)."""

    channels_min: int
    channels_max: int
    formats: list[str]              # CamillaDSP names, device order
    alsa_formats: list[str]         # raw ALSA names
    rate_min: int | None = None
    rate_max: int | None = None

    def accepts_channels(self, n: int) -> bool:
        return self.channels_min <= n <= self.channels_max


@dataclass
class AlsaProbe:
    """Result of probing one device in both directions."""

    device: str
    present: bool | None            # None = could not tell (not a hw: name, no /proc entry)
    busy: bool = False
    playback: HwParams | None = None
    capture: HwParams | None = None
    source: str = ""                # "dump-hw-params" | "proc-stream" | ""
    error: str = ""


@dataclass
class Profile:
    """A device profile as `dspconfig.generate` consumes it.

    `input_map` = capture channels (0-based) feeding program L, R.
    `output_map[physical] = logical` (0 Sub L, 1 Sub R, 2 Top L, 3 Top R); unused when `audition`.
    `audition` = dev-style 4->2 output mixer driven by `State.dev_audition` (sum/tops/subs).
    `pinned` = no fallback: if the device is missing the screen must say so.
    """

    name: str
    capture_device: str
    playback_device: str
    capture_channels: int
    playback_channels: int
    capture_format: str | None
    playback_format: str | None
    input_map: tuple[int, int]
    output_map: tuple[int, ...]
    audition: bool = False
    pinned: bool = False
    probe: bool = False
    present: bool | None = None
    warnings: list[str] = field(default_factory=list)
    target_level: int = TARGET_LEVEL


_DEFAULTS: dict[str, dict[str, Any]] = {
    # Proven on the box 2026-10-12: HiFiBerry DAC+ADC Pro, 2 in / 2 out, S32_LE, 48 kHz.
    "dev": dict(
        capture_device="hw:sndrpihifiberry", playback_device="hw:sndrpihifiberry",
        capture_channels=2, playback_channels=2,
        capture_format="S32_LE", playback_format="S32_LE",
        input_map=(0, 1), output_map=(0, 1),
        audition=True, pinned=False, probe=False,
    ),
    # MOTU M4: counts/format are assumptions until probed (4-ch capture assumed).
    # Program from line inputs 3-4; out1/2 = tops, out3/4 = subs.
    "m4": dict(
        capture_device="hw:M4", playback_device="hw:M4",
        capture_channels=4, playback_channels=4,
        capture_format=None, playback_format=None,
        input_map=(2, 3), output_map=(2, 3, 0, 1),
        audition=False, pinned=True, probe=True,
    ),
}
PROFILE_NAMES: tuple[str, ...] = tuple(_DEFAULTS)


# --------------------------------------------------------------------------- settings file
_PROFILE_LINE = re.compile(
    r"""^profile[ \t]*:[ \t]*["']?[ \t]*([A-Za-z0-9_-]+)[ \t]*["']?[ \t]*(?:#.*)?$""", re.M)


def load_settings(path: str | Path = SETTINGS_PATH) -> dict[str, Any]:
    """Read /etc/k1000/k1000.yaml; never raises.

    Missing -> {} (built-in defaults; a bench box may have none). Present but unusable (YAML
    error, e.g. a tab typed while switching to m4; not a mapping; unreadable; empty) ->
    {SETTINGS_ERROR: why}, plus the top-level `profile:` line when it can still be read on its
    own, so a typo elsewhere in the file does not lose the gig pin.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.info("no settings at %s, using built-in defaults", p)
        return {}
    except Exception as e:  # noqa: BLE001 - OSError, UnicodeDecodeError...
        log.error("cannot read settings %s (%s)", p, e)
        return {SETTINGS_ERROR: f"cannot read {p}: {e}"}
    try:
        data = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001 - yaml.YAMLError, RecursionError...
        why = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        out: dict[str, Any] = {SETTINGS_ERROR: f"{p} does not parse ({why})"}
        m = _PROFILE_LINE.search(text)
        if m:
            out["profile"] = m.group(1)
        log.error("settings %s do not parse (%s); profile line: %s", p, e, out.get("profile"))
        return out
    if not isinstance(data, dict):
        log.error("settings %s is not a mapping (%s)", p, type(data).__name__)
        return {SETTINGS_ERROR: f"{p} is not a mapping"}
    return data


def normalize_profile_name(value: Any) -> str | None:
    """'M4', ' m4 ' -> 'm4'; None if it is not a known profile."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if v in _DEFAULTS else None


def choose_profile(settings: Any, state_profile: Any = None) -> ProfileChoice:
    """The settings file pins the profile (gig: `m4`); else the last running one; else `dev`.

    A `profile:` value that is present but unknown, or a settings file that exists but does not
    parse, never silently becomes `dev`: the choice carries `error` (log + screen) and falls back
    to the last running profile (State.profile) before the built-in `dev`.
    """
    settings = settings if isinstance(settings, dict) else {}
    err = settings.get(SETTINGS_ERROR)
    errors = [err] if isinstance(err, str) and err else []
    raw = settings.get("profile")
    name = normalize_profile_name(raw)
    if name is not None:
        choice = ProfileChoice(name, "settings", "; ".join(errors) or None)
    else:
        if raw is not None:
            errors.append(f"unknown profile {raw!r} in settings (known: {', '.join(PROFILE_NAMES)})")
        last = normalize_profile_name(state_profile)
        choice = ProfileChoice(last or "dev", "state" if last else "default", "; ".join(errors) or None)
    if choice.error:
        log.error("profile: %s -> running %s (from %s)", choice.error, choice.name, choice.source)
    return choice


def select_profile_name(settings: dict[str, Any], state_profile: str | None = None) -> str:
    """`choose_profile(...).name` (kept for callers that only need the name)."""
    return choose_profile(settings, state_profile).name


# --------------------------------------------------------------------------- parsing
_NUM = re.compile(r"\d+")


def _int_range(value: str) -> tuple[int, int] | None:
    nums = [int(n) for n in _NUM.findall(value)]
    if not nums:
        return None
    return min(nums), max(nums)


def parse_hw_params(text: str) -> HwParams | None:
    """Parse the `--dump-hw-params` table (FORMAT / CHANNELS / RATE lines). None if absent."""
    fmts: list[str] | None = None
    chans: tuple[int, int] | None = None
    rates: tuple[int, int] | None = None
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        if key == "FORMAT":
            fmts = value.split()
        elif key == "CHANNELS":
            chans = _int_range(value)
        elif key == "RATE":
            rates = _int_range(value)
    if fmts is None or chans is None:
        return None
    return HwParams(
        channels_min=chans[0], channels_max=chans[1],
        formats=[ALSA_TO_CDSP[f] for f in fmts if f in ALSA_TO_CDSP],
        alsa_formats=fmts,
        rate_min=rates[0] if rates else None, rate_max=rates[1] if rates else None,
    )


def parse_usb_stream(text: str) -> dict[str, HwParams]:
    """Parse /proc/asound/<card>/stream0 (snd-usb-audio) into {"playback"|"capture": HwParams}.

    Every altset's Format/Channels/Rates are merged per direction.
    """
    out: dict[str, dict[str, Any]] = {}
    section: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line in ("Playback:", "Capture:"):
            section = line[:-1].lower()
            out.setdefault(section, {"fmts": [], "chans": [], "rates": []})
            continue
        if section is None:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        acc = out[section]
        if key == "Format":
            for f in value.split():
                if f not in acc["fmts"]:
                    acc["fmts"].append(f)
        elif key == "Channels":
            acc["chans"] += [int(n) for n in _NUM.findall(value)]
        elif key == "Rates":
            acc["rates"] += [int(n) for n in _NUM.findall(value)]
    result: dict[str, HwParams] = {}
    for sec, acc in out.items():
        if not acc["chans"]:
            continue
        result[sec] = HwParams(
            channels_min=min(acc["chans"]), channels_max=max(acc["chans"]),
            formats=[ALSA_TO_CDSP[f] for f in acc["fmts"] if f in ALSA_TO_CDSP],
            alsa_formats=acc["fmts"],
            rate_min=min(acc["rates"]) if acc["rates"] else None,
            rate_max=max(acc["rates"]) if acc["rates"] else None,
        )
    return result


def best_format(formats: list[str]) -> str | None:
    for f in FORMAT_PREFERENCE:
        if f in formats:
            return f
    return None


# --------------------------------------------------------------------------- probing
_CARD = re.compile(r"^(?:plug)?hw:(?:CARD=)?([^,]+)")


def card_id(device: str) -> str | None:
    """'hw:M4' -> 'M4', 'hw:CARD=M4,DEV=0' -> 'M4', 'hw:1' -> 'card1'; None for non-hw names."""
    m = _CARD.match(device.strip())
    if not m:
        return None
    cid = m.group(1)
    return f"card{cid}" if cid.isdigit() else cid


def card_present(device: str, proc_root: str | Path = PROC_ASOUND) -> bool | None:
    """Is the card behind `device` registered? Reads /proc only, never opens the PCM."""
    cid = card_id(device)
    if cid is None:
        return None
    return (Path(proc_root) / cid).exists()


def _dump(tool: str, device: str, timeout: float) -> tuple[str, int]:
    # S16 mono + 1 s cap: if the device ever accepted these params it would play/record
    # 1 s of digital silence (/dev/zero as S16 is 0), never the full-scale DC that U8 zeros are.
    sink = "/dev/zero" if tool == "aplay" else "/dev/null"
    cmd = [tool, "-D", device, "--dump-hw-params", "-f", "S16_LE", "-c", "1", "-r", "48000",
           "-d", "1", sink]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"{tool} failed: {e}", -1
    return (p.stdout or "") + (p.stderr or ""), p.returncode


def probe_alsa(device: str, timeout: float = 5.0, proc_root: str | Path = PROC_ASOUND) -> AlsaProbe:
    """Probe what `device` accepts for playback and capture.

    Order: /proc presence check (no PCM open) -> `aplay/arecord --dump-hw-params` -> if busy,
    the USB stream file. Never raises; failures are in `.error`.
    """
    pr = AlsaProbe(device=device, present=card_present(device, proc_root))
    if pr.present is False:
        pr.error = f"{device}: card not present"
        return pr
    errors: list[str] = []
    for tool, attr in (("aplay", "playback"), ("arecord", "capture")):
        text, _rc = _dump(tool, device, timeout)
        hw = parse_hw_params(text)
        if hw is not None:
            setattr(pr, attr, hw)
            pr.source = "dump-hw-params"
            pr.present = True
            continue
        low = text.lower()
        if "busy" in low:
            pr.busy = True
            pr.present = True
        elif "no such" in low or "cannot get card index" in low:
            pr.present = False if pr.present is None else pr.present
        errors.append(text.strip().splitlines()[-1] if text.strip() else f"{tool}: no output")
    if (pr.playback is None or pr.capture is None) and pr.busy:
        cid = card_id(device)
        if cid:
            try:
                stream = (Path(proc_root) / cid / "stream0").read_text()
            except OSError:
                stream = ""
            parsed = parse_usb_stream(stream)
            if parsed:
                pr.playback = pr.playback or parsed.get("playback")
                pr.capture = pr.capture or parsed.get("capture")
                pr.source = "proc-stream"
    if errors and (pr.playback is None or pr.capture is None):
        pr.error = "; ".join(errors)
    return pr


# --------------------------------------------------------------------------- profile building
def _logical_index(v: Any) -> int:
    if isinstance(v, bool):
        raise ProfileError(f"bad output_map entry {v!r}")
    if isinstance(v, int):
        idx = v
    elif isinstance(v, str) and v.strip().lower() in _LOGICAL_ALIASES:
        idx = _LOGICAL_ALIASES[v.strip().lower()]
    else:
        raise ProfileError(f"bad output_map entry {v!r} (use 0-3 or SubL/SubR/TopL/TopR)")
    if not 0 <= idx <= 3:
        raise ProfileError(f"output_map entry {v!r} out of 0..3")
    return idx


def _pick_channels(name: str, kind: str, explicit: Any, current: int, hw: HwParams | None,
                   warnings: list[str]) -> int:
    n = explicit if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0 else None
    if hw is None:
        return n if n is not None else current
    if n is not None and hw.accepts_channels(n):
        return n
    if n is not None:
        warnings.append(f"{name}: {kind}_channels {n} not accepted by device "
                        f"({hw.channels_min}-{hw.channels_max}); using {hw.channels_max}")
    return hw.channels_max


def _pick_format(name: str, kind: str, explicit: Any, current: str | None, hw: HwParams | None,
                 warnings: list[str]) -> str | None:
    """Explicit format, else the built-in one (`dev`: proven S32_LE; `m4`: None = auto-select).

    Never the probed format (see the module docstring: the devices block must not depend on
    whether the probe ran). The probe only validates: a configured format the device does not
    offer is dropped to None (CamillaDSP then picks the best one it offers) with a warning.
    """
    f = explicit if isinstance(explicit, str) and explicit in CDSP_FORMATS else None
    if isinstance(explicit, str) and f is None:
        warnings.append(f"{name}: unknown {kind}_format {explicit!r} ignored")
    chosen = f if f is not None else current
    if hw is None:
        return chosen
    if not hw.formats:
        warnings.append(f"{name}: device offers no format CamillaDSP can use ({hw.alsa_formats})")
        return chosen
    if chosen is not None and chosen not in hw.formats:
        warnings.append(f"{name}: {kind}_format {chosen} not offered by device {hw.formats}; "
                        "letting CamillaDSP choose")
        return None
    return chosen


def _section(name: str, overrides: Any, warnings: list[str]) -> dict[str, Any]:
    """The `profiles.<name>` mapping from a settings dict (or a bare section); junk -> {} + warning.

    Why lenient: a hand-edited k1000.yaml must not crash-loop k1000 at boot; the built-in
    defaults are the proven/contract values.
    """
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        warnings.append(f"{name}: settings overrides are a {type(overrides).__name__}, not a mapping; ignored")
        return {}
    err = overrides.get(SETTINGS_ERROR)
    if isinstance(err, str) and err:
        warnings.append(f"settings: {err}")
    if "profiles" not in overrides:
        return {k: v for k, v in overrides.items() if k != SETTINGS_ERROR}
    profs = overrides.get("profiles")
    if profs is None:
        return {}
    if not isinstance(profs, dict):
        warnings.append("settings: `profiles` is not a mapping; using built-in profile defaults")
        return {}
    sec = profs.get(name)
    if sec is None:
        return {}
    if not isinstance(sec, dict):
        warnings.append(f"{name}: settings profiles.{name} is not a mapping ({sec!r}); "
                        "using built-in defaults")
        return {}
    return dict(sec)


def load_profile(name: str, overrides: dict[str, Any] | None = None, *,
                 probe: bool | None = None,
                 prober: Callable[[str], AlsaProbe] = probe_alsa) -> Profile:
    """Build a Profile from built-in defaults + k1000.yaml overrides (+ a device probe).

    `overrides` may be the whole settings dict (its `profiles.<name>` section is used) or that
    section directly. Keys: capture_device, playback_device, capture_channels, playback_channels,
    capture_format, playback_format, input_map [L, R], output_map [phys1..physN], probe,
    target_level (frames, 64..1536).
    `probe` None = profile default (m4 probes, dev does not: its facts are proven and the box's
    HiFiBerry is shared). Probed channel counts beat configured ones the device refuses.
    `name` is normalised ('M4' -> 'm4'); only an unknown name raises (ProfileError). Malformed
    overrides never raise: they become warnings and the built-in values are used. A settings
    dict from `load_settings` that carries SETTINGS_ERROR adds that error to `warnings`.
    """
    key = normalize_profile_name(name)
    if key is None:
        raise ProfileError(f"unknown profile {name!r} (known: {', '.join(PROFILE_NAMES)})")
    name = key
    warnings: list[str] = []
    ov = _section(name, overrides, warnings)
    d = dict(_DEFAULTS[name])
    for key in ("capture_device", "playback_device"):
        if isinstance(ov.get(key), str) and ov[key].strip():
            d[key] = ov[key].strip()
    do_probe = d["probe"] if probe is None else probe
    if probe is None and isinstance(ov.get("probe"), bool):
        do_probe = ov["probe"]

    present: bool | None = None
    pb_hw: HwParams | None = None
    cap_hw: HwParams | None = None
    if do_probe:
        pr = prober(d["playback_device"])
        present = pr.present
        pb_hw, cap_hw = pr.playback, pr.capture
        if d["capture_device"] != d["playback_device"]:
            pr_c = prober(d["capture_device"])
            cap_hw = pr_c.capture
            if pr.present is False or pr_c.present is False:
                present = False
            elif pr.present and pr_c.present:
                present = True
            else:
                present = None
        if present is False:
            warnings.append(f"{name}: device {d['playback_device']} not present")
        elif pr.error and (pb_hw is None or cap_hw is None):
            warnings.append(f"{name}: probe incomplete ({pr.error}); using configured/assumed values")

    d["capture_channels"] = _pick_channels(name, "capture", ov.get("capture_channels"),
                                           d["capture_channels"], cap_hw, warnings)
    d["playback_channels"] = _pick_channels(name, "playback", ov.get("playback_channels"),
                                            d["playback_channels"], pb_hw, warnings)
    d["capture_format"] = _pick_format(name, "capture", ov.get("capture_format"),
                                       d["capture_format"], cap_hw, warnings)
    d["playback_format"] = _pick_format(name, "playback", ov.get("playback_format"),
                                        d["playback_format"], pb_hw, warnings)
    if cap_hw and cap_hw.rate_max and not (cap_hw.rate_min or 0) <= 48000 <= cap_hw.rate_max:
        warnings.append(f"{name}: capture does not list 48000 Hz")
    if pb_hw and pb_hw.rate_max and not (pb_hw.rate_min or 0) <= 48000 <= pb_hw.rate_max:
        warnings.append(f"{name}: playback does not list 48000 Hz")

    # input map: program L/R from these capture channels
    imap = ov.get("input_map", d["input_map"])
    try:
        imap_t = (int(imap[0]), int(imap[1]))
    except (TypeError, ValueError, IndexError, KeyError, OverflowError):
        warnings.append(f"{name}: bad input_map {imap!r}, using {d['input_map']}")
        imap_t = tuple(d["input_map"])  # type: ignore[assignment]
    nc = d["capture_channels"]
    if not all(0 <= c < nc for c in imap_t):
        # The profile's own program inputs if they exist (m4: line 3-4), never quietly another
        # physical input first (m4 0-1 = the mic/instrument preamps: a measurement mic to the PA).
        default = tuple(d["input_map"])
        if all(0 <= c < nc for c in default):
            fallback = default
        else:
            fallback = (0, 1) if nc >= 2 else (0, 0)
        warnings.append(f"{name}: input_map {list(imap_t)} outside {nc} capture channels; "
                        f"using {list(fallback)}")
        imap_t = fallback  # type: ignore[assignment]

    # output map: physical i <- logical output_map[i]
    omap = ov.get("output_map", d["output_map"])
    try:
        omap_t = tuple(_logical_index(v) for v in omap)
    except (ProfileError, TypeError) as e:
        warnings.append(f"{name}: {e}; using default output_map")
        omap_t = tuple(d["output_map"])
    if len(omap_t) > d["playback_channels"]:
        warnings.append(f"{name}: output_map has {len(omap_t)} entries for "
                        f"{d['playback_channels']} playback channels; extra dropped")
        omap_t = omap_t[: d["playback_channels"]]
    if not d["audition"]:
        missing = [LOGICAL_NAMES[i] for i in range(4) if i not in omap_t]
        if missing:
            warnings.append(f"{name}: not routed to any output: {', '.join(missing)}")

    tl = ov.get("target_level", TARGET_LEVEL)
    if isinstance(tl, bool) or not isinstance(tl, int):
        warnings.append(f"{name}: target_level {tl!r} is not a whole number of frames; "
                        f"using {TARGET_LEVEL}")
        tl = TARGET_LEVEL
    elif not TARGET_LEVEL_RANGE[0] <= tl <= TARGET_LEVEL_RANGE[1]:
        clamped = min(TARGET_LEVEL_RANGE[1], max(TARGET_LEVEL_RANGE[0], tl))
        warnings.append(f"{name}: target_level {tl} outside {TARGET_LEVEL_RANGE[0]}.."
                        f"{TARGET_LEVEL_RANGE[1]}; using {clamped}")
        tl = clamped

    for w in warnings:
        log.warning("%s", w)
    return Profile(
        name=name,
        capture_device=d["capture_device"], playback_device=d["playback_device"],
        capture_channels=d["capture_channels"], playback_channels=d["playback_channels"],
        capture_format=d["capture_format"], playback_format=d["playback_format"],
        input_map=imap_t, output_map=omap_t,
        audition=d["audition"], pinned=d["pinned"], probe=do_probe,
        present=present, warnings=warnings, target_level=tl,
    )

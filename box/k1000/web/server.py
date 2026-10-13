# SPDX-License-Identifier: GPL-3.0-or-later
"""K1000 phone surface (D-007, docs/06-phone.md): the box's own PWA over HTTPS + a websocket,
and a captive portal on plain HTTP.

Why it is shaped like this:

- **Same controller as the knobs.** Phone messages become the Controller's public calls
  (`nudge`, `set_value`, `set_mute`, `reset_live`), so the phone gets the same clamps, the same
  click-safe engine moves and the same single state as the knobs. Never CamillaGUI, which POSTs
  whole configs and can undo knob work.
- **A strict allowlist, smaller than the knobs'.** The phone reaches the show controls only:
  master / balance / Lo-Cut (steps only, never an absolute jump) and the live EQ layer (absolute
  values, because dragging a notch is the point of the phone), mute, and a confirmed live-EQ
  reset. Setup-class fields (profile, mono subs, delays, polarity, system EQ, trims) are refused.
  Master may rise by a few dB per second at most, whatever the phone sends.
- **Venue snapshots** (docs/12) are whole-slot actions, never single fields: save the current
  values into a slot (an overwrite needs `confirm: true`), rename, preview what a recall would
  change, recall (`confirm: true`, and the slot's `at` so a slot saved again meanwhile is
  refused), undo the recall (`confirm: true`). The Controller applies a recall through its own
  parameter writes and never touches master or mute; names are sanitised there.
- **The phone never stalls the box.** Every client has its own sender that only ever sends the
  LATEST state (slow phones skip states, they do not queue them), a send timeout that drops a
  stuck phone, a message-rate bucket (a mute is never rate-limited), and every error is caught
  per client: nothing a phone sends can raise into the controller or another phone.
- **Physical access is the authorisation.** The servers bind to the AP address only (IP_FREEBIND,
  so they listen before the AP is up): the Wi-Fi password is shown on the box screen only. The
  websocket also checks Origin against the box's own names.
- **No new dependency.** python3-websockets serves the static files through its
  `process_request` hook and upgrades /ws. The captive portal is a tiny asyncio HTTP/1.x server:
  phones' background traffic (HEAD, HTTP/1.0, bodies) must be answered quietly, which the
  websocket handshake parser does not do.
- **The laptop's tune page rides on the same servers** (docs/06-phone.md §11): `/tune` and its
  websocket `/tune/ws` answer only while SETUP > Laptop access is open (`Controller.laptop`), a
  tune client is closed the moment it closes, and the page sends text that the box reads,
  clamps and applies through the Controller (`tune.TuneDesk`). Phones never see it.
- **Polling, not the screen's event.** `Controller.wait_changed()` clears a single event that the
  screen's render loop waits on; a second waiter would steal its wake-ups. The pump reads
  `Controller.version` (falling back to comparing snapshots) every `tick_s` instead, and only
  while a phone is connected.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import html
import http
import ipaddress
import json
import logging
import math
import socket
import ssl
import struct
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from .. import __version__
from ..state import N_LIVE_PEQ, RANGES, atomic_write
from ..tune import MAX_TEXT, TuneDesk
from ..ui.input_map import LIVE_SLOT_LABELS, LIVE_SLOT_SHORT, LIVE_SLOTS
from ..venues import N_SLOTS as VENUE_SLOTS
from ..venues import NAME_MAX as VENUE_NAME_MAX
from ..venues import NAME_PICKER as VENUE_NAME_CHARS
from ..venues import RAW_NAME_MAX as VENUE_RAW_NAME_MAX

log = logging.getLogger("k1000.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
TUNE_DIR = Path(__file__).resolve().parent / "tune"      # served under /tune, laptop access only
PROTOCOL_VERSION = 1

# ----------------------------------------------------------------------------- the allowlist
# Steps only (the UI's +/- buttons): (most negative, most positive) detents per message.
NUDGE_CAPS: dict[str, tuple[int, int]] = {
    "master_db": (-12, 2),          # -6 dB .. +1 dB per tap; the rise is also rate-limited
    "balance_db": (-4, 4),
    "lowcut_hz": (-12, 12),
}
for _band in LIVE_SLOTS:
    NUDGE_CAPS[f"{_band}.freq"] = (-24, 24)     # 2 octaves
    NUDGE_CAPS[f"{_band}.gain"] = (-12, 12)     # 6 dB
    NUDGE_CAPS[f"{_band}.q"] = (-12, 12)        # 2 octaves of Q
# Absolute values: the live EQ only (graph drags, 'notch here') and the Lo-Cut switch.
SET_NUMERIC: frozenset[str] = frozenset(f"{b}.{p}" for b in LIVE_SLOTS for p in ("freq", "gain", "q"))
SET_BOOL: frozenset[str] = frozenset({"lowcut_on"})
PHONE_RANGES: tuple[str, ...] = (
    "master_db", "balance_db", "lowcut_hz",
    "live_peq.freq", "live_peq.gain", "live_peq.q",
    "live_lowshelf.freq", "live_lowshelf.gain", "live_lowshelf.q",
    "live_highshelf.freq", "live_highshelf.gain", "live_highshelf.q",
)
VENUE_TYPES = ("venue_save", "venue_rename", "venue_diff", "venue_recall", "venue_undo")
MESSAGE_TYPES = ("ping", "nudge", "set", "band", "notch", "mute", "reset_live") + VENUE_TYPES
VENUE_UNDO_STEP_S = 5            # the undo countdown is sent in 5 s steps (the page counts down)
MAX_MESSAGE = 1024               # bytes: the largest legal phone message is ~120 (bigger: 1009)
TUNE_MAX_MESSAGE = 4 * MAX_TEXT + 1024   # bytes: the tune page's pasted text (UTF-8, JSON-escaped)
TUNE_TYPES = ("ping", "state", "preview", "apply", "undo")
TUNE_MAX_CLIENTS = 2
MAX_ABS = 1e9                    # any number a phone sends is refused beyond this
OUT_MAX = 32                     # replies queued per phone (pongs/errs dropped first when full)
METER_FLOOR_DB = -60.0
FLAT_DB = 0.05                   # |gain| below this = band inactive (pages.py uses the same)

QR_OVERRIDE_S = 60.0             # E1's choice of QR on the PHONE page lasts this long
CLOSE_BUSY = 4002
CLOSE_OFF = 4003
CLOSE_LAPTOP = 4004              # the tune page: SETUP > Laptop access closed
LEFT_EVERY_S = 30.0              # the tune page's "time left", refreshed this often
TUNE_CLOSED_PAGE = (
    b"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    b"<title>K1000 tune</title><body style='background:#000;color:#fff;font-family:sans-serif;"
    b"padding:24px;max-width:40em'><h1 style='color:#ffb800'>K1000: laptop access is closed</h1>"
    b"<p>On the box: <b>SETUP &rsaquo; Laptop access</b>, push twice. It opens CamillaGUI "
    b"(<code>http://10.42.0.1:5005/</code>), SSH and this page for 2 hours, from the K1000 Wi-Fi "
    b"only. Then reload this page.</p>")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
}
CA_CONTENT_TYPE = "application/x-x509-ca-cert"
BUILD_MARK = b"__K1000_BUILD__"
BUILD_STAMPED = frozenset({"/sw.js", "/app.js"})
PORTAL_ONLY = frozenset({"/portal.html"})
QR_CYCLE = ("wifi", "url", "setup")   # E1 on the PHONE page steps through these


# =========================================================================== settings
@dataclass(frozen=True)
class PhoneSettings:
    """k1000.yaml `phone:` (docs/06-phone.md). Invalid values fall back to these defaults.

    `enabled: false` = no phone servers at all (knobs only). `address` is the AP address: the bind
    address, the URL host and the first allowed host. `plain_app` serves the PWA over plain HTTP on
    `plain_port` ('fallback' = only when the TLS files are missing; it then works as a web page but
    cannot be installed). `http_port` is the captive portal + CA download (0 = off).
    """

    enabled: bool = True
    address: str = "10.42.0.1"
    bind: str = ""                                   # "" = address
    hosts: tuple[str, ...] = ()                      # () = (address, "k1000.home.arpa")
    https_port: int = 443
    http_port: int = 80
    plain_port: int = 8080
    plain_app: str = "fallback"                      # fallback | always | off
    tls_dir: Path = Path("/etc/k1000/tls")
    cert: str = "k1000.crt"
    key: str = "k1000.key"
    ca: str = "k1000-ca.crt"
    ssid: str = "K1000"
    psk_file: Path = Path("/etc/k1000/ap.psk")
    ap_info_file: Path = Path("/etc/k1000/ap.info")  # written by setup-ap.sh: ssid, band, label
    iface: str = "wlan0"
    band: str = ""                                   # PHONE page label; ap.info wins
    max_clients: int = 4
    rate_per_s: float = 30.0
    burst: int = 40
    master_up_db_per_s: float = 6.0
    notch_gain_db: float = -6.0
    notch_q: float = 8.0
    extra_origins: tuple[str, ...] = ()
    state_file: Path = Path("/var/lib/k1000/phone.json")
    lease_files: tuple[str, ...] = ("/var/lib/NetworkManager/dnsmasq-{iface}.leases",   # --backend nm (root)
                                    "/run/k1000-ap/dnsmasq.leases")    # hostapd backend (the default)
    arp_file: str = "/proc/net/arp"
    sys_net: str = "/sys/class/net"

    @property
    def bind_address(self) -> str:
        return self.bind or self.address

    @property
    def host_names(self) -> tuple[str, ...]:
        return self.hosts or (self.address, "k1000.home.arpa")

    @property
    def subnet(self) -> ipaddress.IPv4Network:
        try:
            return ipaddress.ip_network(f"{self.address}/24", strict=False)  # type: ignore[return-value]
        except ValueError:
            return ipaddress.ip_network("10.42.0.0/24")  # type: ignore[return-value]

    @classmethod
    def from_settings(cls, settings: Any, **overrides: Any) -> "PhoneSettings":
        """Parse k1000.yaml's `phone:` section; never raises. Keyword overrides win (tests)."""
        sec = settings.get("phone") if isinstance(settings, dict) else None
        sec = sec if isinstance(sec, dict) else {}
        d = cls()
        bad: list[str] = []

        def pick(key: str, ok: Callable[[Any], bool], conv: Callable[[Any], Any] = lambda v: v) -> Any:
            default = getattr(d, key)
            if key not in sec or sec[key] is None:
                return default
            v = sec[key]
            try:
                if ok(v):
                    return conv(v)
            except (TypeError, ValueError):
                pass
            bad.append(key)
            return default

        def is_int(lo: int, hi: int) -> Callable[[Any], bool]:
            return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi

        def is_num(lo: float, hi: float) -> Callable[[Any], bool]:
            return lambda v: (isinstance(v, (int, float)) and not isinstance(v, bool)
                              and math.isfinite(v) and lo <= v <= hi)

        def is_str(v: Any) -> bool:
            return isinstance(v, str) and v.strip() != "" and "\n" not in v

        def is_ip(v: Any) -> bool:
            return is_str(v) and bool(ipaddress.ip_address(v.strip()))

        def str_list(v: Any) -> bool:
            return isinstance(v, (list, tuple)) and all(is_str(x) for x in v)

        g_rng, q_rng = RANGES["live_peq.gain"], RANGES["live_peq.q"]
        out = cls(
            enabled=pick("enabled", lambda v: isinstance(v, bool)),
            address=pick("address", is_ip, lambda v: v.strip()),
            bind=pick("bind", is_ip, lambda v: v.strip()),
            hosts=pick("hosts", str_list, lambda v: tuple(x.strip() for x in v)),
            https_port=pick("https_port", is_int(0, 65535)),
            http_port=pick("http_port", is_int(0, 65535)),
            plain_port=pick("plain_port", is_int(0, 65535)),
            plain_app=pick("plain_app", lambda v: v in ("fallback", "always", "off")),
            tls_dir=pick("tls_dir", is_str, lambda v: Path(v.strip())),
            ssid=pick("ssid", lambda v: is_str(v) and len(v.encode()) <= 32),
            psk_file=pick("psk_file", is_str, lambda v: Path(v.strip())),
            ap_info_file=pick("ap_info_file", is_str, lambda v: Path(v.strip())),
            iface=pick("iface", lambda v: is_str(v) and len(v) <= 15 and "/" not in v),
            band=pick("band", lambda v: isinstance(v, str) and len(v) <= 40),
            max_clients=pick("max_clients", is_int(1, 16)),
            rate_per_s=pick("rate_per_s", is_num(1.0, 200.0), float),
            burst=pick("burst", is_int(1, 400)),
            master_up_db_per_s=pick("master_up_db_per_s", is_num(0.5, 40.0), float),
            notch_gain_db=pick("notch_gain_db", is_num(g_rng.min, 0.0), float),
            notch_q=pick("notch_q", is_num(q_rng.min, q_rng.max), float),
            extra_origins=pick("extra_origins", str_list, lambda v: tuple(x.strip() for x in v)),
            state_file=pick("state_file", is_str, lambda v: Path(v.strip())),
        )
        if bad:
            log.warning("k1000.yaml phone: invalid %s: using the defaults for them", ", ".join(bad))
        return replace(out, **overrides) if overrides else out


# =========================================================================== small helpers
def _f(v: Any, nd: int = 2) -> float | None:
    """A JSON-safe rounded float (NaN/inf/non-numbers -> None: JSON.parse rejects NaN)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return round(f, nd) if math.isfinite(f) else None


def _is_num(v: Any) -> bool:
    """A finite JSON number of a sane size. A 400-digit JSON integer is legal JSON and fits in
    MAX_MESSAGE, but math.isfinite() would raise OverflowError on it: bound ints first."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return -MAX_ABS <= v <= MAX_ABS
    return isinstance(v, float) and math.isfinite(v) and -MAX_ABS <= v <= MAX_ABS


def _is_time(v: Any) -> bool:
    """A Unix time as the box stamps venue slots (0 <= t < 1e11; beyond MAX_ABS on purpose)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    if isinstance(v, int):
        return 0 <= v < 10 ** 11
    return math.isfinite(v) and 0.0 <= v < 1e11


def _origin(scheme: str, host: str, port: int, default_port: int) -> str:
    h = f"[{host}]" if ":" in host else host
    return f"{scheme}://{h}" if port == default_port else f"{scheme}://{h}:{port}"


def _host_only(hostport: str) -> str:
    h = hostport.strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


class _Bucket:
    """Token bucket: `rate` tokens/s, at most `burst` saved."""

    def __init__(self, rate: float, burst: float, clock: Callable[[], float]) -> None:
        self.rate, self.burst, self._clock = float(rate), float(burst), clock
        self.tokens, self._t = float(burst), clock()

    def take(self, n: float = 1.0) -> bool:
        now = self._clock()
        self.tokens = min(self.burst, self.tokens + (now - self._t) * self.rate)
        self._t = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


# =========================================================================== presence (screen QR)
def read_arp(text: str, iface: str, subnet: ipaddress.IPv4Network,
             exclude: Iterable[str] = ()) -> set[str]:
    """IPv4 neighbours on `iface` in `subnet` with a complete entry (/proc/net/arp, world-readable).

    Why: with `setup-ap.sh --backend nm`, NetworkManager's dnsmasq lease file lives in
    /var/lib/NetworkManager (0700 root on Pi OS trixie, checked on the box), out of reach of the
    k1000 user (the default hostapd backend's /run/k1000-ap leases are readable and read too). A
    phone that joins the AP talks to the box at once (DHCP, DNS, its connectivity probe), so it
    shows up here within a second.
    """
    out: set[str] = set()
    skip = set(exclude)
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or parts[5] != iface:
            continue
        try:
            flags = int(parts[2], 16)
            ip = ipaddress.ip_address(parts[0])
        except ValueError:
            continue
        if not flags & 0x2 or parts[3] == "00:00:00:00:00:00":
            continue
        if ip in subnet and parts[0] not in skip:
            out.add(parts[0])
    return out


def read_leases(text: str, subnet: ipaddress.IPv4Network, now: float) -> set[str]:
    """Active leases in a dnsmasq lease file ('expiry mac ip host clientid'; expiry 0 = infinite)."""
    out: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            expiry = int(parts[0])
            ip = ipaddress.ip_address(parts[2])
        except ValueError:
            continue
        if (expiry == 0 or expiry > now) and ip in subnet:
            out.add(parts[2])
    return out


def iface_ipv4(iface: str) -> str | None:
    """The interface's IPv4 address (SIOCGIFADDR: no privileges, no subprocess), or None."""
    try:
        import fcntl
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", iface.encode()[:15]))
        return socket.inet_ntoa(res[20:24])
    except (OSError, ImportError, ValueError):
        return None


def ap_is_up(settings: PhoneSettings, addr_of: Callable[[str], str | None] = iface_ipv4,
             iface: str | None = None) -> bool | None:
    """True when the AP interface is up and holds the AP address (NM only puts it there when
    the k1000-ap connection is active); False when it is down; None when there is no such
    interface (no Wi-Fi at all). `iface` overrides settings.iface (ap.info names it)."""
    name = iface or settings.iface
    base = Path(settings.sys_net) / name
    try:
        oper = (base / "operstate").read_text().strip()
    except OSError:
        return None
    if oper not in ("up", "unknown", "dormant"):
        return False
    return addr_of(name) == settings.address


def subnet_clash(settings: PhoneSettings, ap_iface: str,
                 addr_of: Callable[[str], str | None] = iface_ipv4) -> str:
    """'eth0 10.42.0.23' when an interface other than the AP holds an address in the AP's subnet,
    else ''. A laptop on eth0 with NetworkManager's "Shared to other computers" is 10.42.0.1/24 too:
    setup-ap.sh, the hostapd unit and the NetworkManager guard then keep the AP down (SSH over eth0
    would be lost), and the PHONE page says why. Each interface's primary IPv4 address only."""
    try:
        names = sorted(p.name for p in Path(settings.sys_net).iterdir())
    except OSError:
        return ""
    subnet = settings.subnet
    for name in names:
        if name in (ap_iface, "lo"):
            continue
        addr = addr_of(name)
        try:
            if addr and ipaddress.ip_address(addr) in subnet:
                return f"{name} {addr}"
        except ValueError:
            continue
    return ""


def read_psk(path: Path) -> tuple[str | None, str]:
    """(password, "") or (None, why). /etc/k1000/ap.psk is root:k1000 0640 (setup-ap.sh)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"no {path} (run setup-ap.sh)"
    except PermissionError:
        return None, f"{path} not readable by k1000"
    except (OSError, UnicodeDecodeError) as e:
        return None, f"{path}: {e}"
    psk = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not 8 <= len(psk) <= 63 or not all(32 <= ord(ch) < 127 for ch in psk):
        return None, f"{path}: not a WPA2 password"
    return psk, ""


AP_INFO_KEYS = ("ssid", "band", "channel", "label", "address", "iface", "backend", "portal", "ssh",
                "state")


def read_ap_info(path: Path) -> dict[str, str]:
    """setup-ap.sh's /etc/k1000/ap.info (key=value: ssid, band, channel, label, iface, backend,
    portal, ssh, state up|down); {} if absent. The SSID must be one a QR can carry (1..32 bytes, printable);
    `iface` must look like an interface name."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() in AP_INFO_KEYS:
            v = v.strip()
            if v and len(v.encode()) <= 40 and v.isprintable():
                out[k.strip()] = v
    if "ssid" in out and len(out["ssid"].encode()) > 32:
        del out["ssid"]
    if "iface" in out and (len(out["iface"]) > 15 or not all(ch.isalnum() or ch in "-_." for ch in out["iface"])):
        del out["iface"]
    return out


# =========================================================================== screen info
@dataclass
class PhoneScreenInfo:
    """What the box's PHONE page shows (`ui/qr.render_phone_page`)."""

    feature: bool = True             # k1000.yaml phone.enabled
    on: bool = True                  # the SETUP runtime switch
    ap_up: bool | None = None
    ssid: str = "K1000"
    psk: str | None = None
    psk_error: str = ""
    url: str = "https://10.42.0.1/"
    setup_url: str = ""              # the portal page (CA download), "" when not served
    stations: int = 0                # phones on the AP (lease / neighbour)
    phones: int = 0                  # phones connected to the page right now
    tls: bool = True                 # HTTPS (installable) or the plain fallback
    qr: str = "wifi"                 # what the big QR shows: wifi | url | setup
    band: str = ""
    error: str = ""                  # e.g. port 443 refused
    ap_clash: str = ""               # "eth0 10.42.0.23": another interface in the AP's subnet
    ap_kept_down: bool = False       # setup-ap.sh --down (the no-go): ap.info state=down
    laptop_left: float = 0.0         # SETUP > Laptop access open: seconds left (the password as text)


# =========================================================================== the bridge
class PhoneBridge:
    """Phone messages -> Controller calls (validated, allowlisted); Controller snapshot -> JSON.

    Uses only the Controller's public surface: snapshot(), nudge(), set_value(), set_mute(),
    reset_live(), displayed_muted() (and `version` / `mute_view()` when present). Every method
    returns (ok, value, why) and never raises on bad input.

    **Mute generation.** Every state message carries `mute_seq`, a number that moves whenever the
    mute the box shows changes and whenever any surface asks for a mute. A phone sends the
    `seq` it saw when the finger went down, and an UNMUTE is applied only if nothing about the
    mute changed since: an emergency mute made meanwhile (B2, another phone) is never undone by
    someone who was looking at the old state. A MUTE is always applied. The Controller's own
    generation (`Controller.mute_view()`, WIRING.md) is preferred, because it also sees B2; the
    bridge keeps its own (displayed-mute changes + mutes applied here) when it is absent.

    **What a hold means** (`hold` in the state) follows B2: `Controller.b2_muted()` when the
    Controller has it. While K1000 itself holds Main muted on its way in (a boot ramp, a setup
    guard) and nobody muted, the box screen says "ramps in by itself · hold B2 to keep muted":
    the phone's hold then MUTES too (keeps it muted), never unmutes into an engine that is not
    processing yet.
    """

    def __init__(self, ctl: Any, *, notch_gain_db: float = -6.0, notch_q: float = 8.0,
                 master_up_db_per_s: float = 6.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.ctl = ctl
        self.notch_gain_db = notch_gain_db
        self.notch_q = notch_q
        up = max(0.5, float(master_up_db_per_s))
        # the whole box (all phones together) may raise master by this much per second
        self._master_up = _Bucket(up, max(1.0, up / 2.0), clock)
        self._mute_seen: tuple[bool, bool] | None = None
        self._mute_gen = 0

    # ------------------------------------------------------------------ mute generation
    def _displayed_muted(self) -> bool:
        dm = getattr(self.ctl, "displayed_muted", None)
        if callable(dm):
            return bool(dm())
        _page, st, status, _focus = self.ctl.snapshot()
        smuted = getattr(status, "muted", None)
        return smuted if isinstance(smuted, bool) else bool(getattr(st, "muted", False))

    def _hold_unmutes(self, displayed: bool) -> bool:
        """True when a mute hold means UNMUTE (B2's rule, Controller.b2_muted() when present)."""
        bm = getattr(self.ctl, "b2_muted", None)
        return bool(bm()) if callable(bm) else displayed

    def mute_view(self) -> tuple[bool, int]:
        """(a hold means UNMUTE, the mute generation). Cheap; call it as often as you like: a
        change of the displayed mute (or of what a hold means) between two calls moves the
        generation."""
        mv = getattr(self.ctl, "mute_view", None)
        if callable(mv):
            m, g = mv()
            return bool(m), int(g)
        disp = self._displayed_muted()
        key = (disp, self._hold_unmutes(disp))
        if key != self._mute_seen:
            self._mute_seen = key
            self._mute_gen += 1
        return key[1], self._mute_gen

    def _apply_mute(self, on: bool) -> bool:
        out = bool(self.ctl.set_mute(on))
        if not callable(getattr(self.ctl, "mute_view", None)):
            self._mute_gen += 1          # a request is a new generation, even a repeated mute
        return out

    # ------------------------------------------------------------------ out
    def hello(self, phone_on: bool, build: str = "") -> dict[str, Any]:
        ranges = {}
        for name in PHONE_RANGES:
            r = RANGES[name]
            ranges[name] = {"min": r.min, "max": r.max, "step": r.step, "log": r.log,
                            "fine": r.fine, "unit": r.unit}
        return {"t": "hello", "v": PROTOCOL_VERSION, "server": f"k1000 {__version__}",
                "build": build, "phone_on": phone_on, "ranges": ranges, "bands": list(LIVE_SLOTS),
                "labels": list(LIVE_SLOT_LABELS), "short": list(LIVE_SLOT_SHORT),
                "notch": {"gain": self.notch_gain_db, "q": self.notch_q},
                "nudge": {k: list(v) for k, v in NUDGE_CAPS.items()},
                "venues": {"slots": VENUE_SLOTS, "name_max": VENUE_NAME_MAX,
                           "name_chars": VENUE_NAME_CHARS}}

    @staticmethod
    def state_message(snap: tuple[Any, Any, Any, Any], *, phone_on: bool, phones: int,
                      mute: tuple[bool, int] | None = None) -> dict[str, Any]:
        """`mute` = mute_view() taken with this snapshot (the page needs it with the state):
        `hold` says what the MUTE button does now, `mute_seq` is the generation it sends back."""
        _page, st, status, _focus = snap
        smuted = getattr(status, "muted", None)
        muted = smuted if isinstance(smuted, bool) else bool(getattr(st, "muted", False))
        hold_unmutes, mute_seq = (muted, 0) if mute is None else (bool(mute[0]), int(mute[1]))
        bands = []
        for path in LIVE_SLOTS:
            try:
                b = st.get(path) if hasattr(st, "get") else None
            except KeyError:
                b = None
            bands.append({"f": _f(getattr(b, "freq", None), 1), "g": _f(getattr(b, "gain", None), 2),
                          "q": _f(getattr(b, "q", None), 3)})
        drift = getattr(status, "drift", None) or []
        engine = getattr(status, "engine", "Unknown")
        present = getattr(status, "device_present", None)
        venues: list[dict[str, Any] | None] = []
        for v in list(getattr(status, "venues", None) or [])[:VENUE_SLOTS]:
            try:
                venues.append({"n": str(v[0]), "at": _f(v[1], 3)} if v else None)
            except Exception:   # noqa: BLE001 - an odd status entry: shown empty
                venues.append(None)
        venues += [None] * (VENUE_SLOTS - len(venues))
        vslot = getattr(status, "venue_slot", -1)
        vslot = vslot if isinstance(vslot, int) and not isinstance(vslot, bool) else -1
        undo = _f(getattr(status, "venue_undo_s", 0.0), 3) or 0.0
        undo_q = int(math.ceil(undo / VENUE_UNDO_STEP_S) * VENUE_UNDO_STEP_S) if undo > 0 else 0
        return {
            "t": "state",
            "master_db": _f(getattr(st, "master_db", None)),
            "muted": bool(muted),
            "hold": "unmute" if hold_unmutes else "mute",
            "mute_seq": mute_seq,
            "mute_known": isinstance(smuted, bool),
            "balance_db": _f(getattr(st, "balance_db", None)),
            "lowcut_on": bool(getattr(st, "lowcut_on", True)),
            "lowcut_hz": _f(getattr(st, "lowcut_hz", None), 1),
            "bands": bands,
            "engine": engine if engine is None else str(engine),
            "linked": bool(getattr(status, "connected", False)),
            "device_present": present if isinstance(present, bool) else None,
            "device": str(getattr(status, "device", "") or ""),
            "profile": str(getattr(status, "profile", "") or getattr(st, "profile", "") or ""),
            "message": str(getattr(status, "message", "") or ""),
            "drift": [str(x) for x in list(drift)[:6]],
            "ramping": bool(getattr(status, "ramping", False)),
            "load": _f(getattr(status, "load", None), 1),
            "phone_on": bool(phone_on),
            "phones": int(phones),
            "venues": venues,
            "venue": ({"s": vslot, "n": str(getattr(status, "venue_name", "") or "")}
                      if 0 <= vslot < VENUE_SLOTS else None),
            "venue_undo_s": undo_q,
        }

    @staticmethod
    def meters_message(status: Any) -> dict[str, Any]:
        levels = list(getattr(status, "levels", None) or [])
        clips = list(getattr(status, "clip", None) or [])
        out_l, out_c = [], []
        for i in range(4):
            v = _f(levels[i] if i < len(levels) else None, 3)
            v = METER_FLOOR_DB if v is None else max(METER_FLOOR_DB, min(0.0, v))
            out_l.append(round(v * 2.0) / 2.0)   # 0.5 dB: a silent box sends nothing at all
            try:
                out_c.append(bool(clips[i]) if i < len(clips) else False)
            except Exception:   # noqa: BLE001 - e.g. a numpy array as a truth value
                out_c.append(False)
        return {"t": "m", "l": out_l, "c": out_c}

    # ------------------------------------------------------------------ in
    def apply(self, msg: dict[str, Any]) -> tuple[bool, Any, str]:
        """One phone message (already JSON-decoded, rate-checked). Returns (ok, value, why)."""
        t = msg.get("t")
        if t == "nudge":
            return self._nudge(msg.get("f"), msg.get("d"), msg.get("fine") is True)
        if t == "set":
            return self._set(msg.get("f"), msg.get("v"))
        if t == "band":
            return self._band(msg)
        if t == "notch":
            return self._notch(msg.get("hz"), msg.get("s"))
        if t == "mute":
            return self._mute(msg.get("on"), msg.get("seq"))
        if t == "reset_live":
            if msg.get("confirm") is not True:
                return False, None, "reset needs confirm: true"
            self.ctl.reset_live()
            return True, None, ""
        if t in VENUE_TYPES:
            return self._venue(t, msg)
        return False, None, "unknown message"

    # ------------------------------------------------------------------ venue snapshots
    def _venue(self, t: str, msg: dict[str, Any]) -> tuple[bool, Any, str]:
        """venue_save / venue_rename / venue_diff / venue_recall / venue_undo (docs/12).
        Validated here (slot index, name type and length, confirm, `at`), sanitised and applied
        by the Controller (save_venue, rename_venue, venue_changes, recall_venue, undo_recall)."""
        ctl = self.ctl
        if not callable(getattr(ctl, "recall_venue", None)):
            return False, None, "venues not available on this box"
        if t == "venue_undo":
            if msg.get("confirm") is not True:
                return False, None, "undo needs confirm: true"
            ok, text = ctl.undo_recall(source="phone")
            return (True, text, "") if ok else (False, None, text)
        s = msg.get("s")
        if isinstance(s, bool) or not isinstance(s, int) or not 0 <= s < VENUE_SLOTS:
            return False, None, f"venue: s must be 0..{VENUE_SLOTS - 1}"
        slot = ctl.venues.get(s)
        if t == "venue_diff":
            if slot is None:
                return False, None, f"slot {s + 1} is empty"
            changes = ctl.venue_changes(s) or []
            return True, {"s": s, "n": slot.name, "at": _f(slot.saved_at, 3), "changes": changes,
                          "muted": bool(ctl.displayed_muted())}, ""
        if t == "venue_recall":
            if msg.get("confirm") is not True:
                return False, None, "recall needs confirm: true"
            at = msg.get("at")
            if at is not None and not _is_time(at):
                return False, None, "venue: at must be the slot's saved time"
            ok, text = ctl.recall_venue(s, source="phone",
                                        expect_saved_at=float(at) if at is not None else None)
            return (True, text, "") if ok else (False, None, text)
        name = msg.get("name")
        if name is not None:
            if not isinstance(name, str):
                return False, None, "venue: name must be text"
            if len(name) > VENUE_RAW_NAME_MAX:
                return False, None, f"venue: name longer than {VENUE_RAW_NAME_MAX} characters"
        if t == "venue_rename":
            if name is None:
                return False, None, "rename needs a name"
            ok, text = ctl.rename_venue(s, name, source="phone")
            return (True, text, "") if ok else (False, None, text)
        # venue_save
        if slot is not None and msg.get("confirm") is not True:
            return False, None, f"slot {s + 1} holds {slot.name}: overwrite needs confirm: true"
        ok, text = ctl.save_venue(s, name, source="phone")
        return (True, text, "") if ok else (False, None, text)

    def _nudge(self, f: Any, d: Any, fine: bool) -> tuple[bool, Any, str]:
        if not isinstance(f, str) or f not in NUDGE_CAPS:
            return False, None, f"not a phone control: {str(f)[:40]}"
        if isinstance(d, bool) or not isinstance(d, int):
            return False, None, "detents must be an integer"
        lo, hi = NUDGE_CAPS[f]
        if not lo <= d <= hi or d == 0:
            return False, None, f"{f}: {d} detents out of {lo}..{hi}"
        if f == "master_db" and d > 0:
            rng = RANGES["master_db"]
            step = rng.fine if (fine and rng.fine) else rng.step
            if not self._master_up.take(d * step):
                return False, None, "master: rising too fast"
        v = self.ctl.nudge(f, d, fine, source="phone")
        if v is None:
            return False, None, f"{f}: refused"
        return True, v, ""

    def _mute(self, on: Any, seq: Any) -> tuple[bool, Any, str]:
        """MUTE: always. UNMUTE: only with the `seq` of the state the phone was showing when the
        hold started, and only if the mute has not changed since (see the class docstring).
        A refusal returns the mute as it is now."""
        if not isinstance(on, bool):
            return False, None, "mute needs on: true/false"
        unmutes, gen = self.mute_view()
        if on:
            return True, self._apply_mute(True), ""
        shown = self._displayed_muted()
        if isinstance(seq, bool) or not isinstance(seq, int):
            return False, shown, "unmute refused: this page is out of date (reload it)"
        if seq != gen:
            return False, shown, "MUTE CHANGED meanwhile: unmute refused, look and hold again"
        if not unmutes:
            if shown:   # K1000 itself holds Main muted (boot ramp / setup guard): B2's rule
                return False, True, "K1000 brings the sound in by itself: nothing to unmute"
            return True, False, ""
        return True, self._apply_mute(False), ""

    def _set(self, f: Any, v: Any) -> tuple[bool, Any, str]:
        if not isinstance(f, str):
            return False, None, "not a phone setting"
        if f in SET_BOOL:
            if not isinstance(v, bool):
                return False, None, f"{f} needs true/false"
        elif f in SET_NUMERIC:
            if not _is_num(v):
                return False, None, f"{f} needs a number"
            v = float(v)
        else:
            return False, None, f"not a phone setting: {str(f)[:40]}"
        out = self.ctl.set_value(f, v, source="phone")
        if out is None:
            return False, None, f"{f}: refused"
        return True, out, ""

    def _band(self, msg: dict[str, Any]) -> tuple[bool, Any, str]:
        s = msg.get("s")
        if isinstance(s, bool) or not isinstance(s, int) or not 0 <= s < len(LIVE_SLOTS):
            return False, None, "band: s must be 0..5"
        vals = {k: msg.get(k) for k in ("f", "q", "g") if k in msg}
        if not vals:
            return False, None, "band: nothing to set"
        if not all(_is_num(v) for v in vals.values()):
            return False, None, "band: values must be numbers"
        out: dict[str, Any] = {}
        # frequency and Q first, gain last: a band moves while still at its old gain
        for key, param in (("f", "freq"), ("q", "q"), ("g", "gain")):
            if key in vals:
                r = self.ctl.set_value(f"{LIVE_SLOTS[s]}.{param}", float(vals[key]))
                if r is None:
                    return False, out or None, f"band {param}: refused"
                out[key] = r
        return True, out, ""

    def _notch(self, hz: Any, s: Any = None) -> tuple[bool, Any, str]:
        """'Notch here': a flat PEQ slot -> hz, notch Q, notch cut (in that order).

        `s` = the slot the phone predicted (it starts dragging that dot at once, under the same
        finger): used only if it is still flat, else refused, so a drag that follows can never
        move a band someone else just set. Without `s`: the first flat slot."""
        if not _is_num(hz):
            return False, None, "notch needs hz"
        st = self.ctl.snapshot()[1]

        def flat(i: int) -> bool:
            return abs(float(getattr(st.live_peq[i], "gain", 0.0))) < FLAT_DB

        if s is not None:
            if isinstance(s, bool) or not isinstance(s, int) or not 0 <= s < N_LIVE_PEQ:
                return False, None, f"notch: s must be 0..{N_LIVE_PEQ - 1}"
            if not flat(s):
                return False, None, f"notch: {LIVE_SLOT_SHORT[s]} is already in use"
            i = s
        else:
            free = [i for i in range(N_LIVE_PEQ) if flat(i)]
            if not free:
                return False, None, "all 4 notches in use"
            i = free[0]
        base = f"live_peq.{i}"
        f = self.ctl.set_value(f"{base}.freq", RANGES["live_peq.freq"].clamp(hz))
        q = self.ctl.set_value(f"{base}.q", self.notch_q)
        g = self.ctl.set_value(f"{base}.gain", self.notch_gain_db)
        if f is None or q is None or g is None:
            return False, None, "notch: refused"
        return True, {"s": i, "f": f, "q": q, "g": g}, ""


# =========================================================================== static files
@dataclass(frozen=True)
class _Asset:
    body: bytes
    ctype: str
    etag: str


def load_static(root: Path) -> tuple[dict[str, _Asset], str]:
    """Every file under `root` into memory, keyed by URL path; sw.js and app.js get the build id.

    Serving from memory: no disk I/O per request and no path traversal possible (only known
    paths exist). The build id (a hash of every other file, as written) changes sw.js whenever
    anything changes, which is what makes a phone's service worker fetch the new app shell; the
    page compares its own stamp with the box's `hello.build` and reloads itself once when the
    box was updated (a cached shell would otherwise run one launch behind the box).
    """
    files: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if not p.is_file() or any(part.startswith(".") for part in rel.split("/")):
            continue
        if p.suffix not in CONTENT_TYPES:
            continue
        files["/" + rel] = p.read_bytes()
    h = hashlib.sha256()
    for k in sorted(files):
        if k != "/sw.js":
            h.update(k.encode() + b"\0" + files[k] + b"\0")
    build = h.hexdigest()[:12]
    assets: dict[str, _Asset] = {}
    for k, body in files.items():
        if k in BUILD_STAMPED:
            body = body.replace(BUILD_MARK, build.encode())
        etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        assets[k] = _Asset(body, CONTENT_TYPES[Path(k).suffix], etag)
    return assets, build


def _csp(connect: str) -> str:
    return ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"connect-src {connect}; worker-src 'self'; manifest-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")


# =========================================================================== the server
@dataclass(eq=False)
class _Client:
    ws: Any
    peer: str
    bucket: _Bucket
    kick: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sent_state: int = -1
    sent_meter: int = -1
    errors: _Bucket | None = None
    # Replies wait here for the sender task: the receive loop never awaits a send, so a phone
    # whose downlink is stalled still has its next frames (a MUTE) processed at once.
    out: collections.deque[str] = field(default_factory=collections.deque)
    tracebacks: int = 0              # per-phone: the first failure is logged with a traceback,
    log_bucket: _Bucket | None = None    # later ones as one line, throttled


class _QuietHandshakes(logging.Filter):
    """websockets logs every malformed request at ERROR with a traceback. Every DNS name on the AP
    resolves to the box, so phones' background apps send plenty of those: debug only."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR and str(record.msg).startswith("opening handshake failed"):
            record.levelno, record.levelname, record.exc_info, record.exc_text = (
                logging.DEBUG, "DEBUG", None, None)
            return logging.getLogger("k1000.web").isEnabledFor(logging.DEBUG)
        return True


_WS_LOG = logging.getLogger("k1000.web.ws")
_WS_LOG.setLevel(logging.WARNING)
_WS_LOG.addFilter(_QuietHandshakes())


def _listen_socket(host: str, port: int) -> socket.socket:
    """A listening socket that may bind an address the box does not hold yet (IP_FREEBIND):
    k1000 can start before the AP is up (k1000-ap-hostapd, or NetworkManager with --backend nm),
    and keeps listening across AP restarts."""
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with contextlib.suppress(OSError):
            if fam == socket.AF_INET:
                s.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_FREEBIND", 15), 1)
            else:
                s.setsockopt(socket.IPPROTO_IPV6, 78, 1)        # IPV6_FREEBIND
        s.bind((host, port))
        s.listen(64)
        s.setblocking(False)
    except BaseException:
        s.close()
        raise
    return s


class PhoneServer:
    """The phone surface: `await start()` / `await stop()`; `set_enabled()` is the SETUP switch;
    `screen_info()` feeds the box's PHONE page; `select_qr()` flips its big QR.

    `start()` never raises: a missing certificate, a refused port or a broken static folder is
    logged and shown on the PHONE page, and the rest keeps running (the knobs never depend on it).
    """

    def __init__(self, ctl: Any, settings: PhoneSettings | None = None, *,
                 static_dir: Path | str | None = None, clock: Callable[[], float] = time.monotonic,
                 tick_s: float = 0.05, send_timeout_s: float = 2.0, presence_s: float = 2.0,
                 tune_dir: Path | str | None = None, tune_undo_file: Path | str | None = None) -> None:
        self.ctl = ctl
        self.cfg = settings or PhoneSettings()
        self.static_dir = Path(static_dir) if static_dir else STATIC_DIR
        self.tune_dir = Path(tune_dir) if tune_dir else TUNE_DIR
        self._clock = clock
        self.tick_s = tick_s
        self.send_timeout_s = send_timeout_s
        self.presence_s = presence_s
        self.bridge = PhoneBridge(ctl, notch_gain_db=self.cfg.notch_gain_db, notch_q=self.cfg.notch_q,
                                  master_up_db_per_s=self.cfg.master_up_db_per_s, clock=clock)
        self._on = self._load_on()
        self._clients: set[_Client] = set()
        self._ws_servers: list[Any] = []
        self._portal_servers: list[asyncio.base_events.Server] = []
        self._tasks: list[asyncio.Task[Any]] = []
        self._static: dict[str, _Asset] = {}
        self.build = ""
        self._tls: ssl.SSLContext | None = None
        self.tls_error = ""
        self._ca_pem: bytes | None = None
        self._ca_der: bytes | None = None
        self.ports: dict[str, int] = {}
        self.errors: list[str] = []
        self._state_json = ""
        self._meter_json = ""
        self._state_seq = 0
        self._meter_seq = 0
        self._dirty = True
        self._last_version: Any = None
        self._last_mute: tuple[bool, int] | None = None
        self._qr_force: str | None = None   # E1 on the PHONE page: "wifi" | "url" for a while
        self._qr_until = 0.0
        self._ap_up: bool | None = None
        self._addr_of: Callable[[str], str | None] = iface_ipv4   # tests swap it
        self._clash = ""
        self._ready_seen: bool | None = None
        self._stations: set[str] = set()
        self._psk: tuple[str | None, str] = (None, "not read yet")
        self._ap_info: dict[str, str] = {}
        self._portal_sem = asyncio.Semaphore(32)
        self._tune_static: dict[str, _Asset] = {}
        self._tune_clients: set[Any] = set()
        self._tune: TuneDesk | None = None
        self.tune_undo_file = tune_undo_file
        self._started = False
        self.stats: dict[str, int] = {"connects": 0, "refused": 0, "rate_limited": 0, "errors": 0,
                                      "dropped": 0, "dropped_replies": 0}
        self.rx: dict[str, int] = {}

    # ------------------------------------------------------------------ public
    @property
    def enabled(self) -> bool:
        """The runtime switch (SETUP > Phone). Off: the page loads but controls nothing."""
        return self._on

    @property
    def phones(self) -> int:
        return len(self._clients)

    @property
    def remote_mute_ready(self) -> bool:
        """A phone can mute right now: phone servers in k1000.yaml, SETUP › Phone on, the AP up
        (no subnet clash) and the page served. The knobs-offline alert names the phone mute only
        then (Controller.phone_changed)."""
        return bool(self.cfg.enabled and self._on and self._ap_up is True and not self._clash
                    and ("https" in self.ports or "plain" in self.ports))

    def _notify_ready(self) -> None:
        """Tell the controller when remote_mute_ready flips (it rewrites the knobs alert)."""
        ready = self.remote_mute_ready
        if ready == self._ready_seen:
            return
        self._ready_seen = ready
        hook = getattr(self.ctl, "phone_changed", None)
        if callable(hook):
            try:
                hook()
            except Exception as e:  # noqa: BLE001 - the phone never breaks the controller
                log.debug("phone_changed: %s", e)

    def set_enabled(self, on: bool) -> bool:
        on = bool(on)
        if on == self._on:
            return on
        self._on = on
        log.warning("phone control %s", "ON" if on else "OFF (knobs only)")
        self._dirty = True
        self._save_on(on)
        if not on:
            for c in list(self._clients):
                self._spawn(self._close(c, CLOSE_OFF, "phone off"), "k1000-web-close")
        self._notify_ready()
        return on

    def toggle(self) -> bool:
        return self.set_enabled(not self._on)

    def select_qr(self, delta: int = 1) -> str:
        """E1 on the PHONE page: step through the codes (Wi-Fi -> URL -> first-time setup page,
        the last one only when the portal port is served) for QR_OVERRIDE_S, then back to the
        automatic choice (Wi-Fi until a phone is on the AP, then the URL). Returns it."""
        cycle = [q for q in QR_CYCLE if q != "setup" or self.setup_url]
        shown = self.screen_info().qr
        i = cycle.index(shown) if shown in cycle else 0
        self._qr_force = cycle[(i + (1 if delta >= 0 else -1)) % len(cycle)]
        self._qr_until = self._clock() + QR_OVERRIDE_S
        return self._qr_force

    @property
    def url(self) -> str:
        """The page's address as served right now (HTTPS, else the plain fallback)."""
        if "https" in self.ports:
            return _origin("https", self.cfg.address, self.ports["https"], 443) + "/"
        if "plain" in self.ports:
            return _origin("http", self.cfg.address, self.ports["plain"], 80) + "/"
        return _origin("https", self.cfg.address, self.cfg.https_port or 443, 443) + "/"

    @property
    def setup_url(self) -> str:
        """The captive-portal page (Open K1000 + the one-time CA download), "" if not served."""
        if "http" not in self.ports:
            return ""
        return _origin("http", self.cfg.address, self.ports["http"], 80) + "/"

    def screen_info(self) -> PhoneScreenInfo:
        """Cached presence + settings for the PHONE page (cheap: called by the render path)."""
        psk, why = self._psk
        setup_url = self.setup_url
        qr = "url" if self._stations or self._clients else "wifi"
        if (self._qr_force is not None and self._clock() < self._qr_until
                and (self._qr_force != "setup" or setup_url)):
            qr = self._qr_force
        return PhoneScreenInfo(
            feature=self.cfg.enabled, on=self._on, ap_up=self._ap_up,
            ssid=self._ap_info.get("ssid") or self.cfg.ssid, psk=psk, psk_error=why, url=self.url,
            setup_url=setup_url,
            stations=len(self._stations), phones=len(self._clients), tls="https" in self.ports,
            qr=qr, band=self._ap_info.get("label") or self.cfg.band,
            error=self.errors[-1] if self.errors else "",
            ap_clash=self._clash, ap_kept_down=self._ap_info.get("state") == "down",
            laptop_left=self._laptop_left() if self.laptop_open() else 0.0)

    def origins(self) -> set[str]:
        """Origins allowed to open /ws: the box's own names on the ports it serves the page on."""
        out = set(self.cfg.extra_origins)
        for h in self.cfg.host_names:
            if "https" in self.ports:
                out.add(_origin("https", h, self.ports["https"], 443))
            if "plain" in self.ports:
                out.add(_origin("http", h, self.ports["plain"], 80))
        return out

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.refresh_presence()
        if not self.cfg.enabled:
            log.info("phone disabled in k1000.yaml (phone.enabled: false): knobs only")
            self._notify_ready()
            return
        try:
            self._static, self.build = await asyncio.to_thread(load_static, self.static_dir)
        except Exception as e:  # noqa: BLE001 - a broken install must not stop the service
            self._error(f"phone page files unreadable ({self.static_dir}: {e})")
            self._static = {}
        try:
            self._tune_static, _ = await asyncio.to_thread(load_static, self.tune_dir)
        except Exception as e:  # noqa: BLE001 - the tune page is never worth the phone page
            log.warning("tune page files unreadable (%s: %s): no /tune", self.tune_dir, e)
            self._tune_static = {}
        self._tls = self._load_tls()
        self._load_ca()
        host = self.cfg.bind_address
        if self.cfg.https_port and self._tls is not None:
            await self._start_app("https", host, self.cfg.https_port, self._tls)
        elif self.cfg.https_port:
            log.warning("phone: no HTTPS (%s): the page cannot be installed as an app", self.tls_error)
        if self._plain_wanted() and self.cfg.plain_port:
            await self._start_app("plain", host, self.cfg.plain_port, None)
        if self.cfg.http_port:
            await self._start_portal(host, self.cfg.http_port)
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._pump(), name="k1000-web-pump"))
        self._tasks.append(loop.create_task(self._presence_loop(), name="k1000-web-presence"))
        log.info("phone: %s (%s), control %s, servers %s", self.url,
                 "HTTPS" if "https" in self.ports else "no HTTPS", "ON" if self._on else "OFF",
                 ", ".join(f"{k}:{v}" for k, v in self.ports.items()) or "none")
        self._notify_ready()

    async def stop(self, timeout: float = 1.0) -> None:
        """Close the servers and tell the phones (code 1001), cancel the tasks; never raises.

        Bounded by `timeout` in total: k1000.service gives the whole process 5 s to stop, and the
        controller still has to persist the state after this. A phone that does not answer its
        close frame is dropped by websockets itself (close_timeout) in the background."""
        tasks, self._tasks = list(self._tasks), []
        for t in tasks:
            t.cancel()
        waits: list[Any] = []
        for srv in self._ws_servers:
            with contextlib.suppress(Exception):
                srv.close()
                waits.append(srv.wait_closed())
        for psrv in self._portal_servers:
            with contextlib.suppress(Exception):
                psrv.close()
                if hasattr(psrv, "close_clients"):
                    psrv.close_clients()
                waits.append(psrv.wait_closed())
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, *waits, return_exceptions=True), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            log.info("phone: servers still closing after %.1f s (dropped in the background)", timeout)
        except Exception as e:  # noqa: BLE001
            log.debug("phone stop: %s", e)
        self._ws_servers.clear()
        self._portal_servers.clear()
        self._clients.clear()
        self._tune_clients.clear()
        self.ports.clear()
        self._started = False
        self._notify_ready()

    # ------------------------------------------------------------------ on/off persistence
    def _load_on(self) -> bool:
        try:
            data = json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
            v = data.get("on") if isinstance(data, dict) else None
            return v if isinstance(v, bool) else True
        except FileNotFoundError:
            return True
        except Exception as e:  # noqa: BLE001 - a corrupt switch file must not stop the service
            log.warning("phone switch %s unreadable (%s): phone ON", self.cfg.state_file, e)
            return True

    def _save_on(self, on: bool) -> None:
        def write() -> None:
            try:
                atomic_write(self.cfg.state_file, json.dumps({"on": on}) + "\n")
            except Exception as e:  # noqa: BLE001
                log.warning("cannot save the phone switch to %s: %s", self.cfg.state_file, e)
        try:
            asyncio.get_running_loop().run_in_executor(None, write)
        except RuntimeError:
            write()

    # ------------------------------------------------------------------ TLS / CA
    def _load_tls(self) -> ssl.SSLContext | None:
        cert, key = self.cfg.tls_dir / self.cfg.cert, self.cfg.tls_dir / self.cfg.key
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cert, key)
            with contextlib.suppress(NotImplementedError):
                ctx.set_alpn_protocols(["http/1.1"])
        except FileNotFoundError:
            self.tls_error = f"no certificate in {self.cfg.tls_dir}"
            return None
        except PermissionError:
            self.tls_error = f"{key} not readable by k1000 (deploy root:k1000 0640)"
            self._error(self.tls_error)
            return None
        except (OSError, ssl.SSLError, ValueError) as e:
            self.tls_error = f"bad certificate/key in {self.cfg.tls_dir}: {e}"
            self._error(self.tls_error)
            return None
        self._warn_expiry(cert)
        return ctx

    def _warn_expiry(self, cert: Path) -> None:
        try:   # CPython's own decoder (no extra dependency); best effort
            info = ssl._ssl._test_decode_cert(str(cert))  # type: ignore[attr-defined]
            left = (ssl.cert_time_to_seconds(info["notAfter"]) - time.time()) / 86400.0
        except Exception:   # noqa: BLE001
            return
        if left < 0:
            self._error(f"phone certificate EXPIRED {-left:.0f} days ago: re-sign it")
        elif left < 60:
            log.warning("phone certificate expires in %.0f days: re-sign it", left)

    def _load_ca(self) -> None:
        p = self.cfg.tls_dir / self.cfg.ca
        try:
            data = p.read_bytes()
        except OSError:
            self._ca_pem = self._ca_der = None
            return
        try:
            if b"-----BEGIN CERTIFICATE-----" in data:
                self._ca_pem = data
                self._ca_der = ssl.PEM_cert_to_DER_cert(data.decode("ascii"))
            else:
                self._ca_der = data
                self._ca_pem = ssl.DER_cert_to_PEM_cert(data).encode("ascii")
        except (ValueError, UnicodeDecodeError) as e:
            log.warning("phone CA %s unusable: %s", p, e)
            self._ca_pem = self._ca_der = None

    def _plain_wanted(self) -> bool:
        """Plain page: always, or as the fallback when HTTPS is not being served (no certificate,
        or port 443 refused): a page that cannot be installed beats no page."""
        return self.cfg.plain_app == "always" or (self.cfg.plain_app == "fallback"
                                                   and "https" not in self.ports)

    def _error(self, text: str) -> None:
        log.error("phone: %s", text)
        self.errors.append(text)
        del self.errors[:-5]

    # ------------------------------------------------------------------ servers
    async def _start_app(self, name: str, host: str, port: int, ctx: ssl.SSLContext | None) -> None:
        try:
            sock = _listen_socket(host, port)
        except OSError as e:
            self._error(f"cannot listen on {host}:{port} ({e.strerror or e})")
            return
        try:
            srv = await serve(
                self._ws_handler, sock=sock, ssl=ctx,
                process_request=self._make_process_request("https" if ctx else "http"),
                server_header=f"K1000/{__version__}", compression=None, max_size=TUNE_MAX_MESSAGE,
                max_queue=8, ping_interval=5.0, ping_timeout=10.0, open_timeout=10.0,
                close_timeout=2.0, logger=_WS_LOG)
        except Exception as e:  # noqa: BLE001
            sock.close()
            self._error(f"cannot serve on {host}:{port} ({e})")
            return
        self._ws_servers.append(srv)
        self.ports[name] = sock.getsockname()[1]

    async def _start_portal(self, host: str, port: int) -> None:
        try:
            sock = _listen_socket(host, port)
            srv = await asyncio.start_server(self._portal_conn, sock=sock, limit=8192)
        except OSError as e:
            self._error(f"captive portal: cannot listen on {host}:{port} ({e.strerror or e})")
            return
        self._portal_servers.append(srv)
        self.ports["http"] = sock.getsockname()[1]

    def _spawn(self, coro: Any, name: str) -> None:
        try:
            t = asyncio.get_running_loop().create_task(coro, name=name)
        except RuntimeError:
            coro.close()
            return
        self._tasks.append(t)
        t.add_done_callback(lambda tt: tt in self._tasks and self._tasks.remove(tt))

    # ------------------------------------------------------------------ HTTP on the app server
    def _make_process_request(self, scheme: str) -> Callable[[ServerConnection, Request], Response | None]:
        def process_request(conn: ServerConnection, req: Request) -> Response | None:
            path = urllib.parse.unquote(req.path.split("?", 1)[0].split("#", 1)[0]) or "/"
            if path in ("/ws", "/tune/ws"):
                origin = req.headers.get("Origin")
                if origin not in self.origins():
                    self.stats["refused"] += 1
                    log.warning("phone: websocket from origin %r refused", origin)
                    return self._response(403, b"forbidden origin\n", "text/plain; charset=utf-8")
                if path == "/tune/ws" and not self.laptop_open():
                    return self._response(403, b"laptop access is closed (SETUP > Laptop access)\n",
                                          "text/plain; charset=utf-8")
                return None
            if path == "/tune" or path.startswith("/tune/"):
                return self._tune_response(path, req.headers, scheme)
            return self._app_response(path, req.headers, scheme)
        return process_request

    def _app_response(self, path: str, headers: Headers, scheme: str) -> Response:
        if path == "/ping":    # the portal's 'is the K1000 certificate trusted?' check
            return self._response(204, b"", None, [("Access-Control-Allow-Origin", "*"),
                                                   ("Cache-Control", "no-store")])
        if path in ("/k1000-ca.crt", "/k1000-ca.der"):
            return self._ca_response(path)
        if path == "/":
            path = "/index.html"
        asset = self._static.get(path)
        if asset is None or path in PORTAL_ONLY:
            return self._response(404, b"not found\n", "text/plain; charset=utf-8")
        extra = [("ETag", asset.etag), ("Cache-Control", "no-cache")]
        if path.endswith(".html"):
            host = _host_only(headers.get("Host", "") or "")
            hp = headers.get("Host", "") if host in self.cfg.host_names else ""
            ws = "wss" if scheme == "https" else "ws"
            connect = f"'self' {ws}://{hp}" if hp and all(c not in hp for c in " ;'\"") else "'self'"
            extra.append(("Content-Security-Policy", _csp(connect)))
        if path == "/sw.js":
            extra.append(("Service-Worker-Allowed", "/"))
        if headers.get("If-None-Match") == asset.etag:
            return self._response(304, b"", None, extra)
        return self._response(200, asset.body, asset.ctype, extra)

    def _tune_response(self, path: str, headers: Headers, scheme: str) -> Response:
        """/tune (the laptop's tune page) and its files: only while SETUP > Laptop access is
        open, never cached (a closed access must show the closed page at the next load)."""
        if not self.laptop_open():
            return self._response(403, TUNE_CLOSED_PAGE, "text/html; charset=utf-8",
                                  [("Cache-Control", "no-store")])
        rel = "/index.html" if path in ("/tune", "/tune/") else path[len("/tune"):]
        asset = self._tune_static.get(rel)
        if asset is None:
            return self._response(404, b"not found\n", "text/plain; charset=utf-8")
        extra = [("Cache-Control", "no-store")]
        if rel.endswith(".html"):
            host = _host_only(headers.get("Host", "") or "")
            hp = headers.get("Host", "") if host in self.cfg.host_names else ""
            ws = "wss" if scheme == "https" else "ws"
            connect = f"'self' {ws}://{hp}" if hp and all(c not in hp for c in " ;'\"") else "'self'"
            extra.append(("Content-Security-Policy", _csp(connect)))
        return self._response(200, asset.body, asset.ctype, extra)

    def _ca_response(self, path: str) -> Response:
        body = self._ca_der if path.endswith(".der") else self._ca_pem
        if body is None:
            return self._response(404, b"the K1000 CA certificate is not on this box\n",
                                  "text/plain; charset=utf-8")
        return self._response(200, body, CA_CONTENT_TYPE, [
            ("Content-Disposition", f'attachment; filename="{path[1:]}"'),
            ("Cache-Control", "no-store")])

    @staticmethod
    def _response(status: int, body: bytes, ctype: str | None,
                  extra: Iterable[tuple[str, str]] = ()) -> Response:
        h = Headers([("Date", time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())),
                     ("Connection", "close"), ("Content-Length", str(len(body))),
                     ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer")])
        if ctype:
            h["Content-Type"] = ctype
        for k, v in extra:
            h[k] = v
        return Response(status, http.HTTPStatus(status).phrase, h, body)

    # ------------------------------------------------------------------ the captive portal
    async def _portal_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._portal_sem.locked():          # a burst of background traffic: shed it
            writer.close()
            return
        async with self._portal_sem:
            try:
                async with asyncio.timeout(5.0):
                    head = await reader.readuntil(b"\r\n\r\n")
                writer.write(self.portal_response(head))
                async with asyncio.timeout(5.0):
                    await writer.drain()
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError,
                    ConnectionError, OSError):
                pass
            except Exception:   # noqa: BLE001 - a portal bug must never reach the service
                log.exception("captive portal request failed")
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), 1.0)

    def portal_response(self, head: bytes) -> bytes:
        """One raw HTTP/1.x request head -> the raw response (tolerant: GET/HEAD, 1.0/1.1)."""
        try:
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ")
        except ValueError:
            return _raw(400, b"bad request\n", "text/plain; charset=utf-8")
        if not version.startswith("HTTP/1."):
            return _raw(400, b"bad request\n", "text/plain; charset=utf-8")
        if method not in ("GET", "HEAD"):
            return _raw(405, b"method not allowed\n", "text/plain; charset=utf-8",
                        [("Allow", "GET, HEAD")])
        hdrs: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        if target.startswith(("http://", "https://")):
            u = urllib.parse.urlsplit(target)
            host, path = (u.hostname or ""), (u.path or "/")
        else:
            host, path = _host_only(hdrs.get("host", "")), target
        path = urllib.parse.unquote(path.split("?", 1)[0].split("#", 1)[0]) or "/"
        head_only = method == "HEAD"
        here = _origin("http", self.cfg.address, self.ports.get("http", self.cfg.http_port) or 80, 80)
        if host not in self.cfg.host_names:
            # Every name resolves to the box on the AP: a phone's connectivity probe lands here.
            # A redirect (not 204) is what makes Android raise "Sign in to network".
            return _raw(302, b"K1000\n", "text/plain; charset=utf-8", [("Location", here + "/")],
                        head_only)
        if path in ("/k1000-ca.crt", "/k1000-ca.der"):
            body = self._ca_der if path.endswith(".der") else self._ca_pem
            if body is None:
                return _raw(404, b"the K1000 CA certificate is not on this box\n",
                            "text/plain; charset=utf-8", (), head_only)
            return _raw(200, body, CA_CONTENT_TYPE,
                        [("Content-Disposition", f'attachment; filename="{path[1:]}"')], head_only)
        if path in ("/", "/index.html", "/portal.html"):
            return _raw(200, self._portal_page(), "text/html; charset=utf-8",
                        [("Content-Security-Policy", _csp(self._ping_origin() or "'none'"))], head_only)
        asset = self._static.get(path)
        if asset is not None and (path.startswith("/portal") or path.startswith("/icons/")):
            return _raw(200, asset.body, asset.ctype, [("Cache-Control", "no-cache")], head_only)
        return _raw(302, b"K1000\n", "text/plain; charset=utf-8", [("Location", here + "/")], head_only)

    def _ping_origin(self) -> str:
        if self._tls is None or "https" not in self.ports:
            return ""
        return _origin("https", self.cfg.address, self.ports["https"], 443)

    def _portal_page(self) -> bytes:
        asset = self._static.get("/portal.html")
        if asset is None:
            text = (f"<!doctype html><title>K1000</title><p>K1000: open "
                    f"<a href=\"{html.escape(self.url)}\">{html.escape(self.url)}</a>")
            return text.encode()
        ping = self._ping_origin()
        vals = {
            "APP_URL": self.url,
            "PING_URL": (ping + "/ping") if ping else "",
            "CA": "1" if self._ca_pem else "",
            "TLS": "1" if "https" in self.ports else "",
            "SSID": self.cfg.ssid,
            "ON": "1" if self._on else "",
        }
        text = asset.body.decode("utf-8")
        for k, v in vals.items():
            text = text.replace("{{" + k + "}}", html.escape(v, quote=True))
        return text.encode("utf-8")

    # ------------------------------------------------------------------ websocket
    async def _ws_handler(self, ws: ServerConnection) -> None:
        peer = str((ws.remote_address or ("?",))[0])
        req = getattr(ws, "request", None)
        path = urllib.parse.unquote(str(req.path).split("?", 1)[0].split("#", 1)[0]) if req else ""
        if path == "/tune/ws":                 # process_request checked Origin and the access
            await self._tune_handler(ws, peer)
            return
        if not self._on:
            await self._refuse(ws, CLOSE_OFF, "phone off")
            return
        if len(self._clients) >= self.cfg.max_clients:
            log.warning("phone %s refused: already %d phones", peer, len(self._clients))
            await self._refuse(ws, CLOSE_BUSY, "too many phones")
            return
        c = _Client(ws, peer, _Bucket(self.cfg.rate_per_s, self.cfg.burst, self._clock),
                    errors=_Bucket(5.0, 5.0, self._clock), log_bucket=_Bucket(0.2, 3.0, self._clock))
        self._clients.add(c)
        self.stats["connects"] += 1
        self._dirty = True
        log.info("phone %s connected (%d)", peer, len(self._clients))
        c.out.append(json.dumps(self.bridge.hello(self._on, self.build)))   # always the first frame
        sender = asyncio.get_running_loop().create_task(self._sender(c), name="k1000-web-send")
        try:
            self._refresh(force=True)
            c.kick.set()
            async for raw in ws:
                if len(raw.encode() if isinstance(raw, str) else raw) > MAX_MESSAGE:
                    # the server takes the tune page's bigger frames: a phone's stays at 1 KB
                    self.stats["errors"] += 1
                    await ws.close(1009, "message too big")
                    break
                reply = self._on_message(c, raw)
                if reply is not None:
                    self._queue(c, reply)
        except ConnectionClosed:
            pass
        except Exception as e:   # noqa: BLE001 - one phone's failure stays with that phone
            self.stats["errors"] += 1
            self._log_failure(c, "connection failed", e)
        finally:
            self._clients.discard(c)
            self._dirty = True
            sender.cancel()
            with contextlib.suppress(BaseException):
                await sender
            log.info("phone %s disconnected (%d left)", peer, len(self._clients))

    # ------------------------------------------------------------------ the tune page (laptop)
    def laptop_open(self) -> bool:
        """SETUP > Laptop access is open right now (as root reports it to the controller)."""
        lap = getattr(self.ctl, "laptop", None)
        try:
            return lap is not None and lap.state == "open" and lap.left_s() > 0
        except Exception:   # noqa: BLE001 - when in doubt, closed
            return False

    def _laptop_left(self) -> float:
        lap = getattr(self.ctl, "laptop", None)
        try:
            return round(float(lap.left_s()), 1) if lap is not None else 0.0
        except Exception:   # noqa: BLE001
            return 0.0

    @property
    def tune(self) -> TuneDesk:
        if self._tune is None:
            self._tune = TuneDesk(self.ctl, self.tune_undo_file)
        return self._tune

    async def _tune_handler(self, ws: ServerConnection, peer: str) -> None:
        """One laptop on /tune/ws: preview / apply / undo, while laptop access stays open."""
        if not self.laptop_open():
            await self._refuse(ws, CLOSE_LAPTOP, "laptop access closed")
            return
        if len(self._tune_clients) >= TUNE_MAX_CLIENTS:
            await self._refuse(ws, CLOSE_BUSY, "tune page already open elsewhere")
            return
        self._tune_clients.add(ws)
        bucket = _Bucket(5.0, 10.0, self._clock)
        log.warning("tune page: %s connected (laptop access, %.0f s left)", peer, self._laptop_left())
        sent_left = self._clock()

        async def send(obj: dict[str, Any]) -> None:
            await asyncio.wait_for(ws.send(json.dumps(obj)), self.send_timeout_s * 2.5)

        try:
            await send({"t": "hello", "v": PROTOCOL_VERSION, "build": self.build,
                        "left_s": self._laptop_left(), **self.tune.state()})
            async for raw in ws:
                if not self.laptop_open():
                    break
                reply = await self._tune_message(peer, raw, bucket)
                if reply is not None:
                    await send(reply)
                if self._clock() - sent_left > LEFT_EVERY_S:
                    sent_left = self._clock()
                    await send({"t": "left", "s": self._laptop_left()})
        except (ConnectionClosed, TimeoutError, asyncio.TimeoutError):
            pass
        except Exception as e:   # noqa: BLE001 - a tune page failure stays with that page
            self.stats["errors"] += 1
            log.error("tune page %s failed: %s: %s", peer, type(e).__name__, e)
        finally:
            self._tune_clients.discard(ws)
            if not self.laptop_open():
                await self._refuse(ws, CLOSE_LAPTOP, "laptop access closed")
            log.info("tune page: %s disconnected", peer)

    async def _tune_message(self, peer: str, raw: Any, bucket: _Bucket) -> dict[str, Any] | None:
        if not isinstance(raw, str):
            return {"t": "err", "why": "text frames only"}
        try:
            msg = json.loads(raw)
        except (ValueError, RecursionError):
            return {"t": "err", "why": "not JSON"}
        if not isinstance(msg, dict) or msg.get("t") not in TUNE_TYPES:
            return {"t": "err", "why": "unknown message"}
        t = msg["t"]
        mid = msg.get("id") if isinstance(msg.get("id"), int) and not isinstance(msg.get("id"), bool) else None
        if t == "ping":
            return {"t": "pong", "id": mid}
        if not bucket.take():
            return {"t": "err", "id": mid, "why": "too fast"}
        if t == "state":
            return {"t": "state", "id": mid, "left_s": self._laptop_left(), **self.tune.state()}
        if t == "preview":
            return {"t": "preview", "id": mid, **self.tune.preview(msg.get("text"))}
        if t == "apply":
            out = await self.tune.apply(msg.get("text"))
            log.warning("tune page %s: apply -> %s", peer,
                        f"{len(out.get('filters') or [])} filter(s), saved {out.get('saved')}"
                        if out.get("ok") else f"refused ({out.get('why')})")
            return {"t": "applied", "id": mid, **out}
        out = await self.tune.undo()
        log.warning("tune page %s: undo -> %s", peer, "done" if out.get("ok") else out.get("why"))
        return {"t": "undone", "id": mid, **out}

    async def close_tune_pages(self) -> None:
        """Laptop access closed: every tune page is told (4004) and dropped."""
        for ws in list(self._tune_clients):
            self._tune_clients.discard(ws)
            await self._refuse(ws, CLOSE_LAPTOP, "laptop access closed")

    async def _refuse(self, ws: ServerConnection, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.send(json.dumps({"t": "bye", "code": code, "why": reason,
                                                       "phone_on": self._on})), 1.0)
            await asyncio.wait_for(ws.close(code, reason), 2.0)

    async def _close(self, c: _Client, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(c.ws.send(json.dumps({"t": "bye", "code": code, "why": reason,
                                                         "phone_on": self._on})), 1.0)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(c.ws.close(code, reason), 2.0)

    def _queue(self, c: _Client, reply: dict[str, Any]) -> None:
        """Hand a reply to the phone's sender (never blocks). Full queue (a stalled downlink):
        pongs and errors go first, then the oldest reply."""
        if len(c.out) >= OUT_MAX:
            for i, text in enumerate(c.out):
                if text.startswith(('{"t": "pong"', '{"t": "err"')):
                    del c.out[i]
                    break
            else:
                c.out.popleft()
            self.stats["dropped_replies"] += 1
        c.out.append(json.dumps(reply))
        c.kick.set()

    def _log_failure(self, c: _Client, what: str, e: BaseException) -> None:
        """A phone's failure in the log, without letting one phone flood the journal: the first
        one with its traceback, the next few as one line, then nothing (stats still count)."""
        if c.tracebacks == 0:
            c.tracebacks = 1
            log.error("phone %s: %s", c.peer, what, exc_info=(type(e), e, e.__traceback__))
        elif c.log_bucket is None or c.log_bucket.take():
            log.warning("phone %s: %s again (%s: %s)", c.peer, what, type(e).__name__, str(e)[:120])

    def _on_message(self, c: _Client, raw: Any) -> dict[str, Any] | None:
        """One raw message -> the reply (or None). Never raises: whatever a phone sends, a bug
        it triggers stays with that message."""
        try:
            return self._handle(c, raw)
        except Exception as e:  # noqa: BLE001
            self.stats["errors"] += 1
            self._log_failure(c, "message failed", e)
            return self._err(c, None, f"internal error ({type(e).__name__})")

    def _handle(self, c: _Client, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, str) or len(raw) > MAX_MESSAGE:
            return self._err(c, None, "bad message")
        try:
            msg = json.loads(raw)
        except (ValueError, RecursionError):
            return self._err(c, None, "not JSON")
        if not isinstance(msg, dict):
            return self._err(c, None, "not an object")
        t = msg.get("t")
        mid = msg.get("id")
        mid = mid if (isinstance(mid, int) and not isinstance(mid, bool)) or (
            isinstance(mid, str) and len(mid) <= 32) else None
        if t not in MESSAGE_TYPES:
            return self._err(c, mid, "unknown message")
        self.rx[t] = self.rx.get(t, 0) + 1
        if t == "ping":
            ts = msg.get("ts")
            return {"t": "pong", "ts": ts if _is_num(ts) else None}
        if not self._on:
            return self._err(c, mid, "phone off")
        urgent = t == "mute" and msg.get("on") is True     # a MUTE is never rate-limited
        if not urgent and not c.bucket.take():
            self.stats["rate_limited"] += 1
            return self._err(c, mid, "too fast")
        try:
            ok, value, why = self.bridge.apply(msg)
        except Exception as e:  # noqa: BLE001 - a controller error stays with this message
            self.stats["errors"] += 1
            self._log_failure(c, f"{t} failed", e)
            return self._err(c, mid, f"internal error ({type(e).__name__})")
        if ok and t in ("mute",) + VENUE_TYPES:
            self._dirty = True               # every phone sees it at once (mute generation, slots)
        f = msg.get("f")
        if (t in ("mute", "reset_live", "notch") or (t in VENUE_TYPES and t != "venue_diff")
                or (t == "set" and isinstance(f, str) and f in SET_BOOL)):
            log.info("phone %s: %s %s -> %s", c.peer, t,
                     json.dumps({k: v for k, v in msg.items() if k not in ("t", "id")})[:80],
                     value if ok else f"refused ({why})")
        if not ok:
            if t == "mute":           # a refused unmute is never throttled away: say it, with the mute
                return {"t": "err", "id": mid, "why": why, "v": value}
            return self._err(c, mid, why)
        return {"t": "ack", "id": mid, "v": value} if mid is not None else None

    def _err(self, c: _Client, mid: Any, why: str) -> dict[str, Any] | None:
        if c.errors is not None and not c.errors.take():
            return None     # a broken phone looping on errors gets silence, not a flood
        return {"t": "err", "id": mid, "why": why}

    async def _send(self, c: _Client, text: str) -> None:
        async with c.lock:
            try:
                await asyncio.wait_for(c.ws.send(text), self.send_timeout_s)
            except (TimeoutError, asyncio.TimeoutError):
                self.stats["dropped"] += 1
                log.warning("phone %s too slow: dropped", c.peer)
                with contextlib.suppress(Exception):
                    c.ws.transport.abort()
                raise ConnectionClosed(None, None) from None

    async def _sender(self, c: _Client) -> None:
        """Replies in order, then only ever the LATEST state/meters: a slow phone skips states,
        never queues them. The only task that writes to this phone (besides the final 'bye')."""
        try:
            while True:
                await c.kick.wait()
                c.kick.clear()
                while c.out:
                    await self._send(c, c.out.popleft())
                if c.sent_state != self._state_seq and self._state_json:
                    seq, text = self._state_seq, self._state_json
                    await self._send(c, text)
                    c.sent_state = seq
                if c.sent_meter != self._meter_seq and self._meter_json:
                    seq, text = self._meter_seq, self._meter_json
                    await self._send(c, text)
                    c.sent_meter = seq
        except (ConnectionClosed, asyncio.CancelledError):
            return
        except Exception:   # noqa: BLE001
            log.exception("phone %s: sender failed", c.peer)
            with contextlib.suppress(Exception):
                c.ws.transport.abort()

    # ------------------------------------------------------------------ the pump
    def _refresh(self, force: bool = False) -> bool:
        """Snapshot -> state/meter JSON; bump what changed and wake every phone's sender.

        The mute is read on every tick, whatever `version` says: some engine-side mute changes
        (a setup guard, a reconnect) do not bump it, and the mute generation must not lag."""
        v = getattr(self.ctl, "version", None)
        mute = self.bridge.mute_view()
        if (not force and not self._dirty and isinstance(v, int) and v == self._last_version
                and mute == self._last_mute):
            return False
        self._last_version = v if isinstance(v, int) else None
        self._last_mute = mute
        self._dirty = False
        snap = self.ctl.snapshot()
        state = json.dumps(self.bridge.state_message(snap, phone_on=self._on, phones=len(self._clients),
                                                     mute=mute))
        meters = json.dumps(self.bridge.meters_message(snap[2]))
        changed = False
        if state != self._state_json:
            self._state_json, self._state_seq, changed = state, self._state_seq + 1, True
        if meters != self._meter_json:
            self._meter_json, self._meter_seq, changed = meters, self._meter_seq + 1, True
        if changed:
            for c in self._clients:
                c.kick.set()
        return changed

    async def _pump(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            if self._tune_clients and not self.laptop_open():
                try:
                    await self.close_tune_pages()
                except Exception as e:  # noqa: BLE001
                    log.debug("closing tune pages: %s", e)
            if not self._clients:
                continue
            try:
                self._refresh()
            except Exception as e:  # noqa: BLE001 - the pump must survive anything
                self.stats["errors"] += 1
                log.warning("phone pump: %s: %s", type(e).__name__, e)

    # ------------------------------------------------------------------ presence
    def refresh_presence(self) -> None:
        """AP up?, phones on the AP, the Wi-Fi password: cached for the PHONE page."""
        cfg = self.cfg
        self._ap_info = read_ap_info(cfg.ap_info_file)
        iface = self._ap_info.get("iface") or cfg.iface      # setup-ap.sh --iface (a USB dongle)
        self._ap_up = ap_is_up(cfg, self._addr_of, iface=iface)
        clash = subnet_clash(cfg, iface, self._addr_of)
        if clash != self._clash:
            if clash:
                log.warning("phone: %s is in the AP's subnet %s: the AP stays down while it lasts "
                            "(a laptop on eth0 sharing its connection?)", clash, cfg.subnet)
            else:
                log.info("phone: the AP's subnet is free again")
        self._clash = clash
        stations: set[str] = set()
        for pattern in cfg.lease_files:
            try:
                text = Path(pattern.format(iface=iface)).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            stations |= read_leases(text, cfg.subnet, time.time())
        try:
            text = Path(cfg.arp_file).read_text(encoding="utf-8", errors="replace")
            stations |= read_arp(text, iface, cfg.subnet, exclude=(cfg.address,))
        except OSError:
            pass
        stations.discard(cfg.address)
        if stations != self._stations:
            log.info("phone: %d device(s) on the K1000 Wi-Fi", len(stations))
        self._stations = stations
        self._psk = read_psk(cfg.psk_file)
        self._notify_ready()

    async def _presence_loop(self) -> None:
        while True:
            await asyncio.sleep(self.presence_s)
            try:
                self.refresh_presence()
            except Exception as e:  # noqa: BLE001
                log.debug("presence: %s", e)


def _raw(status: int, body: bytes, ctype: str, extra: Iterable[tuple[str, str]] = (),
         head_only: bool = False) -> bytes:
    """A raw HTTP/1.1 response for the portal (always Connection: close, never cached)."""
    lines = [f"HTTP/1.1 {status} {http.HTTPStatus(status).phrase}",
             f"Content-Type: {ctype}", f"Content-Length: {len(body)}", "Connection: close",
             "Cache-Control: no-store", "X-Content-Type-Options: nosniff",
             "Referrer-Policy: no-referrer"]
    names = {k.lower() for k, _ in extra}
    if "cache-control" in names:
        lines.remove("Cache-Control: no-store")
    lines += [f"{k}: {v}" for k, v in extra]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (b"" if head_only else body)


__all__ = ["PhoneBridge", "PhoneScreenInfo", "PhoneServer", "PhoneSettings", "ap_is_up",
           "iface_ipv4", "load_static", "read_ap_info", "read_arp", "read_leases", "read_psk",
           "NUDGE_CAPS", "SET_NUMERIC", "SET_BOOL"]

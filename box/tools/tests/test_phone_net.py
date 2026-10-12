# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the phone's network side (D-007, docs/06-phone.md): net/setup-ap.sh and the phone
parts of install.sh (apt deps, the TLS certificate step, --ap, the AP units) and bootstrap.sh.

Self-contained, like test_sys_tools.py: setup-ap.sh runs in a sandbox against fake nmcli,
systemctl, nft, dpkg, ip... (its K1000_AP_* test hooks point every path into tmp); nothing touches
a real network, unit or firewall. A real `dnsmasq --test` checks the generated DNS files when
dnsmasq is installed (it is on the box: dnsmasq-base). install.sh blocks are cut out of the
script and run with fakes, the way test_sys_tools.py does it.

    cd box && python3 -m pytest -q -p no:cacheprovider tools/tests/test_phone_net.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BOX = Path(__file__).resolve().parents[2]
NET = BOX / "net"
SETUP_AP = NET / "setup-ap.sh"
INSTALL = BOX / "install.sh"
BOOTSTRAP = BOX / "bootstrap.sh"


def _bin(d: Path, name: str, body: str, shebang: str = "#!/bin/sh") -> None:
    p = d / name
    p.write_text(shebang + "\n" + body)
    p.chmod(0o755)


# --------------------------------------------------------------------------- the sandbox
FAKE_NMCLI = r'''
# fake nmcli: connections as sorted key=value files in $FAKE/state/con/<name>.nmconnection
import os, sys
st = os.environ["FAKE"] + "/state"; cd = st + "/con"; os.makedirs(cd, exist_ok=True)
with open(st + "/nmcli.log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
a = sys.argv[1:]
fields = get = None
while a and a[0] in ("-f", "-g", "-t", "--wait"):
    if a[0] == "-f": fields = a[1]; a = a[2:]
    elif a[0] == "-g": get = a[1]; a = a[2:]
    elif a[0] == "--wait": a = a[2:]
    else: a = a[1:]
active_f = st + "/active"
active = set(open(active_f).read().split()) if os.path.exists(active_f) else set()
def save_active(): open(active_f, "w").write("\n".join(sorted(active)))
def path(n): return f"{cd}/{n}.nmconnection"
def load(n):
    d = {}
    for line in open(path(n)):
        k, _, v = line.rstrip("\n").partition("="); d[k] = v
    return d
def write(n, d): open(path(n), "w").write("".join(f"{k}={d[k]}\n" for k in sorted(d)))
names = sorted(f[:-13] for f in os.listdir(cd) if f.endswith(".nmconnection"))
if a[:2] == ["radio", "wifi"]:
    f = st + "/radio"
    if len(a) == 3: open(f, "w").write("enabled" if a[2] == "on" else "disabled"); sys.exit(0)
    print(open(f).read() if os.path.exists(f) else "disabled"); sys.exit(0)
if a[:2] == ["general", "reload"]: sys.exit(0)
if a[:2] == ["device", "set"]: open(st + "/managed-" + a[2], "w").write(a[4]); sys.exit(0)
if a == ["device"]:
    def rd(n, d=""):
        return open(st + "/" + n).read().strip() if os.path.exists(st + "/" + n) else d
    print("eth0:connected:Wired connection 1")
    if "k1000-ap" in active: print("wlan0:connected:k1000-ap")
    elif rd("wlan0-client"): print("wlan0:connected:" + rd("wlan0-client"))
    elif rd("managed-wlan0") == "no": print("wlan0:unmanaged:")
    else: print("wlan0:disconnected:")
    print("lo:connected (externally):lo")
    sys.exit(0)
if a[0] == "connection": a = a[1:]
cmd = a[0]
if cmd == "show":
    if len(a) >= 2 and a[1] != "--active":
        print(load(a[1]).get(get, "")); sys.exit(0)
    for n in (names if "--active" not in a else [n for n in names if n in active]):
        print(n if fields == "NAME" else f"{n}:{path(n)}")
    sys.exit(0)
if cmd in ("modify", "add"):
    if cmd == "add":
        i = a.index("con-name"); n = a[i + 1]; rest = a[i + 2:]
        d = {"connection.id": n, "connection.type": a[a.index("type") + 1]}
    else:
        n = a[1]; rest = a[2:]; d = load(n)
    for k, v in zip(rest[::2], rest[1::2]): d[k] = v
    write(n, d); sys.exit(0)
if cmd == "up":
    active.add(a[1]); save_active()
    open(st + "/starts", "a").write("nmcli-up " + a[1] + "\n"); sys.exit(0)
if cmd == "down": active.discard(a[1]); save_active(); sys.exit(0)
if cmd == "delete": os.remove(path(a[1])); active.discard(a[1]); save_active(); sys.exit(0)
sys.exit(f"fake nmcli: unhandled {a}")
'''

FAKE_SYSTEMCTL = r'''
# fake systemctl: units as $FAKE/state/units/<unit>.{active,enabled}; every call logged
u=$FAKE/state/units; mkdir -p "$u"; echo "$*" >> "$FAKE/state/systemctl.log"
q=0; args=""
for x in "$@"; do case $x in --quiet) q=1 ;; *) args="$args $x" ;; esac; done
set -- $args
cmd=$1; shift
case $cmd in
  is-active) [ "$1" = NetworkManager ] && exit 0
             if [ -e "$u/$1.active" ]; then [ $q = 1 ] || echo active; exit 0; fi
             [ $q = 1 ] || echo inactive; exit 3 ;;
  is-enabled) if [ -e "$u/$1.enabled" ]; then [ $q = 1 ] || echo enabled; exit 0; fi
              [ $q = 1 ] || echo disabled; exit 1 ;;
  enable) for x in "$@"; do touch "$u/$x.enabled"; done ;;
  disable) for x in "$@"; do rm -f "$u/$x.enabled"; done ;;
  start|restart|reload)
    for x in "$@"; do
      if [ "$cmd" = reload ] && [ ! -e "$u/$x.active" ]; then exit 1; fi
      touch "$u/$x.active"; echo "$cmd $x" >> "$FAKE/state/starts"
      if [ "$x" = k1000-ap-fence.service ]; then nft -f "$FAKE/etc/ap-fence.nft"; fi
      if [ "$x" = k1000-ap-hostapd.service ] && [ -e "$FAKE/state/hostapd-dies" ]; then rm -f "$u/$x.active"; fi
    done ;;
  show) for x in "$@"; do last=$x; done; cat "$u/$last.nrestarts" 2>/dev/null || echo 0 ;;
  stop) for x in "$@"; do rm -f "$u/$x.active"; echo "stop $x" >> "$FAKE/state/starts"
          if [ "$x" = k1000-ap-fence.service ]; then nft delete table inet k1000_ap; fi; done ;;
  daemon-reload) echo daemon-reload >> "$FAKE/state/starts" ;;
esac
exit 0
'''

FAKE_NFT = r'''
s=$FAKE/state; echo "nft $*" >> "$s/nft.log"
case "$1 $2" in
  "-c -f") cp "$3" "$s/nft-checked"; grep -q 'table inet k1000_ap {' "$3" ;;
  "-f "*) cp "$2" "$s/nft-loaded" ;;
  "list table") [ -e "$s/nft-loaded" ] ;;
  "delete table") rm -f "$s/nft-loaded" ;;
  *) exit 0 ;;
esac
'''

# install(1) without root: -o/-g are recorded in <dst>.owner (read back by the fake stat)
FAKE_INSTALL = r'''
import os, shutil, sys
a = sys.argv[1:]; mode = None; owner = group = None; d = mkparents = False; files = []
while a:
    x = a.pop(0)
    if x == "-m": mode = int(a.pop(0), 8)
    elif x == "-o": owner = a.pop(0)
    elif x == "-g": group = a.pop(0)
    elif x == "-d": d = True
    elif x == "-D": mkparents = True
    else: files.append(x)
def own(p):
    if owner or group:
        open(p + ".owner", "w").write(f"{owner or 'root'}:{group or 'root'}\n")
if d:
    for p in files:
        os.makedirs(p, exist_ok=True)
        if mode is not None: os.chmod(p, mode)
        own(p)
else:
    src, dst = files
    if mkparents: os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o755 if mode is None else mode)
    own(dst)
'''


class ApBox:
    """setup-ap.sh in a sandbox: fake tools on PATH, every K1000_AP_* path under tmp."""

    def __init__(self, tmp: Path, hostapd: bool = True) -> None:
        self.tmp = tmp
        self.fake = tmp / "fake"
        f = self.fake
        for d in ("etc", "nm", "state", "units", "nmconf", "sys/wlan0/wireless", "sys/wlan1/wireless",
                  "sys/eth0", "rfkill/rfkill0", "rfkill/rfkill1"):
            (f / d).mkdir(parents=True)
        (f / "rfkill/rfkill0/type").write_text("bluetooth\n")
        (f / "rfkill/rfkill0/soft").write_text("1\n")
        (f / "rfkill/rfkill1/type").write_text("wlan\n")
        (f / "rfkill/rfkill1/soft").write_text("1\n")
        (f / "state/radio").write_text("disabled")
        b = tmp / "bin"
        b.mkdir()
        py = "#!" + sys.executable
        _bin(b, "nmcli", FAKE_NMCLI, py)
        _bin(b, "install", FAKE_INSTALL, py)
        _bin(b, "systemctl", FAKE_SYSTEMCTL, "#!/bin/bash")
        _bin(b, "nft", FAKE_NFT, "#!/bin/bash")
        _bin(b, "dpkg", '[ "$2" = hostapd ] && { [ -e "$FAKE/state/hostapd-installed" ]; exit $?; }\nexit 0\n')
        _bin(b, "id", '[ "$1" = -u ] && echo 0 || /usr/bin/id "$@"\n')
        _bin(b, "getent", '[ "$1 $2" = "group k1000" ] && echo "k1000:x:1000:" && exit 0\nexit 2\n')
        _bin(b, "chown", 'echo "$1" > "$2.owner"\n')
        _bin(b, "stat", 'f=$3; o=$(cat "$f.owner" 2>/dev/null || echo "user:user"); '
                        'echo "$o $(/usr/bin/stat -c %a "$f")"\n')
        _bin(b, "apt-mark", 'f=$FAKE/state/manual; touch "$f"\n'
                            'case $1 in showmanual) cat "$f" ;; manual) echo "$2" >> "$f" ;; esac\n')
        _bin(b, "iw", 'echo "country FR: DFS-ETSI"\n')
        _bin(b, "raspi-config", 'echo FR\n')
        _bin(b, "sleep", 'echo "sleep $*" >> "$FAKE/state/starts"\n')
        # the AP address shows on the interface while the AP is up (NM connection or hostapd unit)
        up = ('up=0; grep -q k1000-ap "$FAKE/state/active" 2>/dev/null && up=1\n'
              '[ -e "$FAKE/state/units/k1000-ap-hostapd.service.active" ] && up=1\n')
        _bin(b, "ip", up + 'i=$(sed -n "s/^K1000_AP_IFACE=//p" "$FAKE/etc/ap.env" 2>/dev/null); i=${i:-wlan0}\n'
                           'case " $* " in *" to "*) cat "$FAKE/state/clash" 2>/dev/null ;; esac\n'
                           '[ $up = 1 ] && echo "3: $i    inet 10.42.0.1/24 brd 10.42.0.255 scope global"\n'
                           'exit 0\n')
        _bin(b, "python3", "cat > /dev/null\n" + up + "[ $up = 1 ] && echo 10.42.0.1\nexit 0\n")
        hb = tmp / "hbin"
        hb.mkdir()
        _bin(hb, "hostapd", "exit 0\n")
        _bin(hb, "hostapd_cli", '[ -e "$FAKE/state/units/k1000-ap-hostapd.service.active" ] '
                                '&& echo state=ENABLED || echo state=DISABLED\n')
        self.bin, self.hbin = b, hb
        self.hostapd = False
        if hostapd:
            self.install_hostapd()

    def install_hostapd(self) -> None:
        (self.fake / "state/hostapd-installed").touch()
        self.hostapd = True

    def run(self, *args: str, rc: int = 0) -> str:
        (self.fake / "state/starts").write_text("")
        path = f"{self.bin}:" + (f"{self.hbin}:" if self.hostapd else "") + "/usr/bin:/bin:/usr/sbin:/sbin"
        f = self.fake
        env = dict(os.environ, PATH=path, FAKE=str(f), K1000_AP_ETC=str(f / "etc"),
                   K1000_AP_DNS_DST=str(f / "nm/k1000.conf"), K1000_AP_SYS_NET=str(f / "sys"),
                   K1000_AP_UNIT_DIR=str(f / "units"), K1000_AP_NM_CONF_DIR=str(f / "nmconf"),
                   K1000_AP_RFKILL=str(f / "rfkill"), K1000_AP_SETTLE_S="0",
                   K1000_AP_DISPATCH_DIR=str(f / "dispatch"), K1000_AP_GUARD_MARK=str(f / "state/guard-mark"))
        for k in ("K1000_AP_IFACE", "K1000_AP_SSID", "K1000_AP_COUNTRY"):
            env.pop(k, None)
        r = subprocess.run(["bash", str(SETUP_AP), *args], capture_output=True, text=True, env=env)
        out = r.stdout + r.stderr
        assert r.returncode == rc, f"setup-ap.sh {' '.join(args)} -> {r.returncode}\n{out}"
        m = re.findall(r"^k1000-ap: (\d+) changes$", r.stdout, re.M)
        self.changes = int(m[-1]) if m else None
        return out

    def info(self, key: str) -> str:
        text = (self.fake / "etc/ap.info").read_text()
        m = re.search(rf"^{key}=(.*)$", text, re.M)
        return m.group(1) if m else ""

    def con(self, key: str) -> str:
        p = self.fake / "state/con/k1000-ap.nmconnection"
        m = re.search(rf"^{re.escape(key)}=(.*)$", p.read_text(), re.M)
        return m.group(1) if m else ""

    def unit(self, name: str, state: str) -> bool:
        return (self.fake / f"state/units/{name}.service.{state}").exists()

    def starts(self) -> str:
        return (self.fake / "state/starts").read_text()

    def hostapd_conf(self) -> str:
        return (self.fake / "etc/hostapd.conf").read_text()


def _dnsmasq_test(conf: Path) -> None:
    dnsmasq = shutil.which("dnsmasq", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if dnsmasq:
        r = subprocess.run([dnsmasq, "--test", f"--conf-file={conf}"], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------------------------- setup-ap.sh defaults
def test_setup_ap_first_run_takes_the_decided_defaults(tmp_path):
    """D-007 as refined (2026-10-12): hostapd with PMF unset and wlan0 unmanaged by NetworkManager,
    5 GHz ch 36, country FR, captive portal OFF, SSH fenced off; fence loaded before the AP."""
    box = ApBox(tmp_path)
    box.run()
    assert box.changes and box.changes > 10
    assert [box.info(k) for k in ("backend", "portal", "ssh", "iface", "band", "channel")] == \
        ["hostapd", "off", "off", "wlan0", "a", "36"]
    conf = box.hostapd_conf()
    for line in ("interface=wlan0", "driver=nl80211", "ssid=K1000", "country_code=FR", "hw_mode=a",
                 "channel=36", "ieee80211n=1", "ieee80211ac=1", "wpa=2", "wpa_key_mgmt=WPA-PSK",
                 "rsn_pairwise=CCMP"):
        assert re.search(rf"^{re.escape(line)}$", conf, re.M), line
    assert not re.search(r"^\s*ieee80211w", conf, re.M)                # PMF unset = not advertised
    psk = (box.fake / "etc/ap.psk").read_text().strip()
    assert re.fullmatch(r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}", psk)
    assert f"wpa_passphrase={psk}\n" in conf
    assert oct((box.fake / "etc/hostapd.conf").stat().st_mode & 0o777) == "0o600"
    assert (box.fake / "etc/ap.psk.owner").read_text().strip() == "root:k1000"
    # NetworkManager leaves wlan0 alone, and no NM AP connection is made
    assert "unmanaged-devices=interface-name:wlan0" in (box.fake / "nmconf/k1000-ap-unmanaged.conf").read_text()
    assert (box.fake / "state/managed-wlan0").read_text() == "no"
    assert not (box.fake / "state/con/k1000-ap.nmconnection").exists()
    # portal off: only the box's name resolves; dnsmasq's own config includes that file
    assert (box.fake / "nm/k1000.conf").read_bytes() == (NET / "dnsmasq-k1000-noportal.conf").read_bytes()
    apdns = (box.fake / "etc/ap-dnsmasq.conf").read_text()
    assert f"conf-file={box.fake / 'nm/k1000.conf'}" in apdns and "listen-address=10.42.0.1" in apdns
    _dnsmasq_test(box.fake / "etc/ap-dnsmasq.conf")
    # the fence: SSH off, loaded before hostapd starts; units enabled; the handover start twice
    fence = (box.fake / "etc/ap-fence.nft").read_text()
    assert "tcp dport { 53, 80, 443, 8080 } accept" in fence and " 22," not in fence
    starts = box.starts().splitlines()
    first_fence = next(i for i, s in enumerate(starts) if "k1000-ap-fence" in s)
    first_ap = next(i for i, s in enumerate(starts) if "k1000-ap-hostapd" in s)
    assert first_fence < first_ap
    assert sum(1 for s in starts if s == "restart k1000-ap-hostapd.service") == 2
    for u in ("k1000-ap-fence", "k1000-ap-hostapd", "k1000-ap-dnsmasq"):
        assert box.unit(u, "enabled") and box.unit(u, "active"), u
        assert (box.fake / f"units/{u}.service").read_bytes() == (NET / f"{u}.service").read_bytes()
    assert (box.fake / "rfkill/rfkill1/soft").read_text().strip() == "0"         # wlan unblocked
    assert (box.fake / "rfkill/rfkill0/soft").read_text().strip() == "1"         # bluetooth untouched
    assert "hostapd" in (box.fake / "state/manual").read_text().split()         # never autoremoved
    box.run()
    assert box.changes == 0 and "restart" not in box.starts()


def test_setup_ap_down_is_kept_by_plain_runs_and_only_up_lifts_it(tmp_path):
    """The Friday no-go: the AP off the air, off across reboots (units disabled) AND across later
    plain runs (install.sh --ap, bootstrap.sh), fence kept; `--up` brings it back."""
    box = ApBox(tmp_path)
    box.run()
    out = box.run("--down")
    for u in ("k1000-ap-hostapd", "k1000-ap-dnsmasq"):
        assert not box.unit(u, "enabled") and not box.unit(u, "active"), u
    assert box.unit("k1000-ap-fence", "active") and (box.fake / "state/nft-loaded").exists()
    assert box.info("state") == "down" and "--up" in out
    assert box.info("band") == "a" and box.info("backend") == "hostapd"        # the rest is kept
    box.run("--down")
    assert box.changes == 0
    out = box.run()                                                       # e.g. install.sh --ap later
    assert "kept DOWN" in out and "--up" in out and box.info("state") == "down"
    for u in ("k1000-ap-hostapd", "k1000-ap-dnsmasq"):
        assert not box.unit(u, "enabled") and not box.unit(u, "active"), u
    assert "restart k1000-ap-hostapd" not in box.starts()
    box.run("--band", "bg")                                               # a change keeps it down too
    assert box.info("band") == "bg" and not box.unit("k1000-ap-hostapd", "active")
    box.run("--up")
    assert box.unit("k1000-ap-hostapd", "enabled") and box.unit("k1000-ap-hostapd", "active")
    assert box.info("state") == "up" and box.info("backend") == "hostapd" and box.info("band") == "bg"
    box.run()
    assert box.changes == 0


def test_setup_ap_down_on_a_fresh_card_marks_it_down_for_later_runs(tmp_path):
    """bootstrap.sh --no-ap (a spare card after a no-go): nothing set up yet, the mark alone."""
    box = ApBox(tmp_path)
    box.run("--down")
    assert (box.fake / "etc/ap.info").read_text() == "state=down\n"
    box.run()
    assert not box.unit("k1000-ap-hostapd", "active") and box.info("state") == "down"
    assert box.info("band") == "a"                                        # configured, not on the air


@pytest.mark.parametrize("backend", ["hostapd", "nm"])
def test_setup_ap_refuses_while_another_interface_is_in_its_subnet(tmp_path, backend):
    """A laptop on eth0 with NetworkManager's 'Shared to other computers' (10.42.0.1/24, the box
    gets 10.42.0.x): the AP would take the laptop's address and SSH over eth0 would be lost."""
    box = ApBox(tmp_path)
    (box.fake / "state/clash").write_text("2: eth0    inet 10.42.0.23/24 brd 10.42.0.255 scope global dynamic eth0\n")
    out = box.run("--backend", backend, rc=4)
    assert "eth0 holds 10.42.0.23/24" in out and "Shared to other computers" in out
    assert "nothing changed" in out and not (box.fake / "etc/ap.info").exists()
    assert box.starts() == "" and not (box.fake / "etc/ap-fence.nft").exists()
    box.run("--no-up")                                                    # configuring only: a warning
    assert box.info("state") == "up" and not box.unit("k1000-ap-hostapd", "active")
    assert "WARNING: eth0 holds 10.42.0.23/24" in box.run("--no-up")
    out = box.run("--status")
    assert "SUBNET CLASH: eth0 holds 10.42.0.23/24" in out
    (box.fake / "state/clash").unlink()                                   # the laptop unplugged
    box.run("--backend", backend)
    assert "no other interface in 10.42.0.0/24" in box.run("--status")


def test_fence_drops_only_the_ap_address_from_other_interfaces_and_caps_connections(tmp_path):
    """The eth0 rule covers 10.42.0.1 alone (an eth0 network on 10.42.0.0/24 keeps working); the
    page's ports take at most 16 connections per phone and 64 in all (WEB-1)."""
    box = ApBox(tmp_path)
    box.run()
    fence = (box.fake / "etc/ap-fence.nft").read_text()
    assert 'iifname != { "wlan0", "lo" } ip daddr 10.42.0.1 counter drop' in fence
    assert "ip daddr 10.42.0.0/24" not in fence
    lines = [ln.strip() for ln in fence.splitlines()]
    per_phone = 'iifname "wlan0" tcp dport { 80, 443, 8080 } ct state new add @phone_conns { ip saddr ct count over 16 } counter reject with tcp reset'
    in_all = 'iifname "wlan0" tcp dport { 80, 443, 8080 } ct state new ct count over 64 counter reject with tcp reset'
    accept = 'iifname "wlan0" tcp dport { 53, 80, 443, 8080 } accept'
    assert lines.index(per_phone) < lines.index(in_all) < lines.index(accept)
    assert "set phone_conns {" in fence and "flags dynamic" in fence
    nft = shutil.which("nft", path="/usr/sbin:/sbin:/usr/bin:/bin")
    if nft and os.geteuid() == 0:
        r = subprocess.run([nft, "-c", "-f", str(box.fake / "etc/ap-fence.nft")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_setup_ap_fails_when_hostapd_dies_after_its_start(tmp_path):
    """systemctl restart returns as soon as hostapd is forked (Type=simple): a refused channel or
    band only shows a moment later. Exit 4 (install.sh --ap and bootstrap see it), with the hint."""
    box = ApBox(tmp_path)
    (box.fake / "state/hostapd-dies").touch()
    out = box.run(rc=4)
    assert "did not stay up" in out and "--band bg" in out
    assert "sleep 3" in box.starts()                                      # it settled first
    (box.fake / "state/hostapd-dies").unlink()
    (box.fake / "state/units/k1000-ap-hostapd.service.nrestarts").write_text("0\n")
    box.run()
    assert box.unit("k1000-ap-hostapd", "active")


def test_setup_ap_sees_a_hostapd_restarted_meanwhile(tmp_path):
    box = ApBox(tmp_path)
    box.run()
    # the fake counts no restarts by itself: a script stands in for systemd restarting hostapd
    real_sleep = box.bin / "sleep"
    real_sleep.write_text('#!/bin/sh\necho "sleep $*" >> "$FAKE/state/starts"\n'
                          'echo 1 > "$FAKE/state/units/k1000-ap-hostapd.service.nrestarts"\n')
    out = box.run("--channel", "40", rc=4)
    assert "died and was restarted" in out


def test_setup_ap_refuses_a_wifi_client_link(tmp_path):
    """A card set up with Wi-Fi in Raspberry Pi Imager, reached over that Wi-Fi: making wlan0 the AP
    would cut the link (and the SSH session over it). Refused before anything changes."""
    box = ApBox(tmp_path)
    (box.fake / "state/wlan0-client").write_text("HomeWifi")
    out = box.run(rc=4)
    assert "'HomeWifi' as a client" in out and "Nothing changed" in out
    assert not (box.fake / "state/managed-wlan0").exists() and not (box.fake / "etc/ap.info").exists()
    (box.fake / "state/wlan0-client").unlink()
    box.run()
    assert box.unit("k1000-ap-hostapd", "active")


def test_setup_ap_password_is_never_readable_by_others(tmp_path):
    """The new password file is created 0600 and gets root:k1000 0640 before it is moved in."""
    box = ApBox(tmp_path)
    _bin(box.bin, "mv", 'for a in "$@"; do last2=$prev; prev=$a; done\n'
                        'case $last2 in *ap.psk.tmp) /usr/bin/stat -c %a "$last2" >> "$FAKE/state/psk-modes";; esac\n'
                        'exec /bin/mv "$@"\n')
    box.run()
    assert (box.fake / "state/psk-modes").read_text().split() == ["640"]
    assert (box.fake / "etc/ap.psk.tmp.owner").read_text().strip() == "root:k1000"


def test_setup_ap_binds_hostapd_to_its_interface_and_installs_the_guard(tmp_path):
    box = ApBox(tmp_path)
    box.run()
    dropin = box.fake / "units/k1000-ap-hostapd.service.d/k1000-iface.conf"
    assert "BindsTo=sys-subsystem-net-devices-wlan0.device" in dropin.read_text()
    assert "After=sys-subsystem-net-devices-wlan0.device" in dropin.read_text()
    guard = box.fake / "dispatch/90-k1000-ap-guard"
    assert guard.read_bytes() == (NET / "k1000-ap-guard.sh").read_bytes()
    assert oct(guard.stat().st_mode & 0o777) == "0o755"
    assert "K1000_AP_BACKEND=hostapd" in (box.fake / "etc/ap.env").read_text()
    box.run("--iface", "wlan1")
    assert "BindsTo=sys-subsystem-net-devices-wlan1.device" in dropin.read_text()
    assert "restart k1000-ap-hostapd.service" in box.starts()
    box.run("--remove")
    assert not dropin.exists() and not guard.exists() and not (box.fake / "etc/ap.env").exists()


def test_setup_ap_choices_stick_and_portal_on_is_one_option(tmp_path):
    box = ApBox(tmp_path)
    box.run()
    box.run("--portal", "on")
    assert box.info("portal") == "on"
    assert (box.fake / "nm/k1000.conf").read_bytes() == (NET / "dnsmasq-k1000.conf").read_bytes()
    assert "restart k1000-ap-hostapd.service" in box.starts()            # dnsmasq re-reads it
    _dnsmasq_test(box.fake / "etc/ap-dnsmasq.conf")
    box.run("--band", "bg", "--ssh", "on")
    conf = box.hostapd_conf()
    assert "hw_mode=g\n" in conf and "channel=6\n" in conf and "ieee80211ac" not in conf
    assert "tcp dport { 22, 53, 80, 443, 8080 }" in (box.fake / "etc/ap-fence.nft").read_text()
    box.run()                                                             # a plain re-run keeps them
    assert box.changes == 0
    assert [box.info(k) for k in ("portal", "band", "channel", "ssh")] == ["on", "bg", "6", "on"]


def test_setup_ap_nm_backend_stays_available_with_pmf_disabled(tmp_path):
    box = ApBox(tmp_path)
    box.run()
    box.run("--backend", "nm")
    assert box.con("802-11-wireless-security.pmf") == "1"                 # NM enum: 1 = disable
    assert box.con("802-11-wireless.mode") == "ap" and box.con("ipv4.method") == "shared"
    assert not box.unit("k1000-ap-hostapd", "enabled") and not box.unit("k1000-ap-hostapd", "active")
    assert not (box.fake / "nmconf/k1000-ap-unmanaged.conf").exists()
    assert (box.fake / "state/managed-wlan0").read_text() == "yes"
    box.run("--backend", "hostapd")
    assert box.con("connection.autoconnect") == "no"                      # kept, down, never autoconnecting
    assert box.unit("k1000-ap-hostapd", "active") and box.info("backend") == "hostapd"


def test_setup_ap_without_hostapd_refuses_and_says_how(tmp_path):
    box = ApBox(tmp_path, hostapd=False)
    out = box.run(rc=1)
    assert "apt install hostapd" in out and "--backend nm" in out
    assert not (box.fake / "etc/ap.info").exists()
    box.install_hostapd()
    box.run()
    assert box.info("backend") == "hostapd"


# --------------------------------------------------------------------------- the units and the guard
def _unit_lines(name: str, key: str) -> list[str]:
    return [ln.split("=", 1)[1] for ln in (NET / name).read_text().splitlines() if ln.startswith(key + "=")]


def test_hostapd_unit_is_fenced_bound_and_limited():
    """Requires= the fence (stopping it stops the AP) + an ExecStartPre on its table (a skipped
    fence is not a failed unit); a finite start limit for both AP units."""
    assert _unit_lines("k1000-ap-hostapd.service", "Requires") == ["k1000-ap-fence.service"]
    assert "k1000-ap-fence.service" in _unit_lines("k1000-ap-hostapd.service", "After")[0]
    pre = _unit_lines("k1000-ap-hostapd.service", "ExecStartPre")
    assert "nft list table inet k1000_ap" in pre[0] and "ip -4 -o addr show to" in pre[1]
    for u in ("k1000-ap-hostapd.service", "k1000-ap-dnsmasq.service"):
        assert _unit_lines(u, "StartLimitIntervalSec") == ["120"] and _unit_lines(u, "StartLimitBurst") == ["8"], u


def _run_pre(tmp: Path, line: str, env: dict) -> subprocess.CompletedProcess:
    """One ExecStartPre=/bin/sh -c '...' line run the way systemd would ($$ -> $), with fakes."""
    m = re.fullmatch(r"/bin/sh -c '(.*)'", line)
    assert m, line
    script = m.group(1).replace("$$", "$").replace("/usr/sbin/", "").replace(
        "/run/k1000-ap-guard.stopped", str(tmp / "mark"))
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, env=env)


def test_hostapd_unit_refuses_unfenced_and_in_an_occupied_subnet(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    _bin(b, "nft", '[ -e "$FAKE/fence" ]\n')
    _bin(b, "ip", 'cat "$FAKE/addrs"\n')
    env = dict(os.environ, PATH=f"{b}:/usr/bin:/bin", FAKE=str(tmp_path),
               K1000_AP_IFACE="wlan0", K1000_AP_ADDR="10.42.0.1/24")
    fence, subnet = _unit_lines("k1000-ap-hostapd.service", "ExecStartPre")[:2]
    r = _run_pre(tmp_path, fence, env)
    assert r.returncode == 1 and "fence" in r.stderr and "REFUSED" in r.stderr
    (tmp_path / "fence").touch()
    assert _run_pre(tmp_path, fence, env).returncode == 0
    (tmp_path / "addrs").write_text("3: wlan0    inet 10.42.0.1/24 brd 10.42.0.255 scope global wlan0\n")
    assert _run_pre(tmp_path, subnet, env).returncode == 0 and not (tmp_path / "mark").exists()
    (tmp_path / "addrs").write_text("2: eth0    inet 10.42.0.23/24 brd 10.42.0.255 scope global dynamic eth0\n"
                                    "3: wlan0    inet 10.42.0.1/24 brd 10.42.0.255 scope global wlan0\n")
    r = _run_pre(tmp_path, subnet, env)
    assert r.returncode == 1 and "eth0 holds 10.42.0.23/24" in r.stderr
    assert (tmp_path / "mark").exists()                                   # the guard brings it back later


class GuardBox:
    """net/k1000-ap-guard.sh (the NetworkManager dispatcher script) against fakes."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.ap = ApBox(tmp)
        f = self.ap.fake
        (f / "etc/ap.env").write_text("K1000_AP_IFACE=wlan0\nK1000_AP_ADDR=10.42.0.1/24\nK1000_AP_BACKEND=hostapd\n")
        (f / "etc/ap.info").write_text("ssid=K1000\nstate=up\n")
        (f / "state/units").mkdir(exist_ok=True)
        _bin(self.ap.bin, "logger", 'shift 2; echo "$*" >> "$FAKE/state/journal"\n')

    def run(self, iface: str = "eth0", action: str = "up") -> str:
        f = self.ap.fake
        (f / "state/starts").write_text("")
        env = dict(os.environ, PATH=f"{self.ap.bin}:/usr/bin:/bin", FAKE=str(f),
                   K1000_AP_ENV=str(f / "etc/ap.env"), K1000_AP_INFO=str(f / "etc/ap.info"),
                   K1000_AP_GUARD_MARK=str(f / "state/guard-mark"))
        r = subprocess.run(["sh", str(NET / "k1000-ap-guard.sh"), iface, action], capture_output=True,
                           text=True, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    def hostapd(self, active: bool, enabled: bool = True) -> None:
        u = self.ap.fake / "state/units"
        for state, on in (("active", active), ("enabled", enabled)):
            p = u / f"k1000-ap-hostapd.service.{state}"
            p.touch() if on else p.unlink(missing_ok=True)


def test_guard_stops_the_ap_on_a_subnet_clash_and_brings_it_back(tmp_path):
    g = GuardBox(tmp_path)
    f = g.ap.fake
    g.hostapd(active=True)
    assert g.run() == "" and g.ap.starts() == ""                         # no clash: nothing
    (f / "state/clash").write_text("2: eth0    inet 10.42.0.23/24 brd 10.42.0.255 scope global dynamic eth0\n")
    out = g.run("eth0", "dhcp4-change")
    assert "AP stopped" in out and "eth0 holds 10.42.0.23/24" in out
    assert "stop k1000-ap-hostapd.service" in g.ap.starts() and (f / "state/guard-mark").exists()
    assert "AP stopped" in (f / "state/journal").read_text()
    assert g.run("eth0", "dhcp4-change") == ""                            # already stopped: quiet
    (f / "state/clash").unlink()                                          # the cable unplugged
    out = g.run("eth0", "down")
    assert "start k1000-ap-hostapd.service" in g.ap.starts() and "free again" in out
    assert not (f / "state/guard-mark").exists()
    assert g.run("eth0", "up") == ""                                      # nothing left to restore


def test_guard_never_brings_back_an_ap_taken_down(tmp_path):
    g = GuardBox(tmp_path)
    f = g.ap.fake
    g.hostapd(active=True)
    (f / "state/clash").write_text("2: eth0    inet 10.42.0.23/24 brd 10.42.0.255 scope global eth0\n")
    g.run()
    (f / "etc/ap.info").write_text("ssid=K1000\nstate=down\n")           # setup-ap.sh --down meanwhile
    (f / "state/clash").unlink()
    g.run("eth0", "down")
    assert "start" not in g.ap.starts().replace("stop", "")
    g.hostapd(active=False, enabled=False)                                # disabled: never started
    (f / "state/guard-mark").touch()
    (f / "etc/ap.info").write_text("ssid=K1000\nstate=up\n")
    g.run("eth0", "down")
    assert g.ap.starts() == ""


def test_guard_ignores_other_events_and_boxes_without_an_ap(tmp_path):
    g = GuardBox(tmp_path)
    f = g.ap.fake
    g.hostapd(active=True)
    (f / "state/clash").write_text("2: eth0    inet 10.42.0.23/24 brd 10.42.0.255 scope global eth0\n")
    assert g.run("eth0", "pre-up") == "" and g.ap.starts() == ""
    (f / "etc/ap.env").unlink()
    assert g.run("eth0", "up") == "" and g.ap.starts() == ""


def test_guard_takes_an_unfenced_nm_ap_down(tmp_path):
    g = GuardBox(tmp_path)
    f = g.ap.fake
    (f / "state/active").write_text("k1000-ap")
    (f / "state/con").mkdir(parents=True, exist_ok=True)
    (f / "state/con/k1000-ap.nmconnection").write_text("connection.id=k1000-ap\n")
    out = g.run("wlan0", "up")                                            # no fence table loaded
    assert "fence" in out and "k1000-ap" not in (f / "state/active").read_text()
    (f / "state/nft-loaded").touch()
    (f / "state/active").write_text("k1000-ap")
    assert g.run("wlan0", "up") == "" and "k1000-ap" in (f / "state/active").read_text()


def test_guard_syntax_and_shellcheck():
    guard = NET / "k1000-ap-guard.sh"
    assert os.access(guard, os.X_OK)
    assert subprocess.run(["sh", "-n", str(guard)]).returncode == 0
    if shutil.which("shellcheck") is None:
        pytest.skip("no shellcheck")
    r = subprocess.run(["shellcheck", str(guard)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("bad", ["--band x", "--portal maybe", "--ssh yes", "--backend iwd",
                                 "--channel x", "--iface a/b", "--bogus", "--band a --channel 11",
                                 "--band bg --channel 36", "--channel 52", "--band bg --channel 14",
                                 "--up --down", "--up --no-up"])
def test_setup_ap_rejects_bad_options(tmp_path, bad):
    ApBox(tmp_path).run(*bad.split(), rc=2)


def test_setup_ap_syntax_and_shellcheck():
    assert subprocess.run(["bash", "-n", str(SETUP_AP)]).returncode == 0
    if shutil.which("shellcheck") is None:
        pytest.skip("no shellcheck")
    r = subprocess.run(["shellcheck", str(SETUP_AP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_setup_ap_help_states_the_defaults():
    r = subprocess.run(["bash", str(SETUP_AP), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    head = r.stdout
    assert "first time: hostapd" in head and "captive portal off" in head and "channel 36" in head
    assert "--backend nm" in head and "--portal on" in head and "--down" in head


# --------------------------------------------------------------------------- install.sh
def test_install_deps_carry_the_phone_packages_and_no_gpl2_smbus():
    r = subprocess.run(["bash", str(INSTALL), "--deps"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    deps = r.stdout.split()
    assert len(deps) == len(set(deps)), deps
    for p in ("hostapd", "python3-qrcode", "python3-smbus2"):
        assert p in deps
    assert "python3-smbus" not in deps                                    # GPL-2.0-only (D-008)


def test_install_help_offers_ap_so_bootstrap_uses_it():
    r = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    assert re.search(r"(^|\s)--ap(\s|$)", r.stdout) and "K1000_TLS_DIR" in r.stdout


def _tls_block(tmp: Path) -> tuple[str, dict, Path]:
    """install.sh's step 0 (the checks, before anything changes) + step 4b (the copy), with fakes."""
    text = INSTALL.read_text()
    check = text[text.index("# --- 0. the phone certificate"):text.index("# --- 1. user and groups")]
    start = text.index("# --- 4b. the phone page's certificate")
    end = text.index("# --- 5. CamillaGUI")
    tls = tmp / "etc-k1000-tls"
    block = check + text[start:end].replace("TLS=/etc/k1000/tls", f"TLS={tls}")
    fb = tmp / "bin"
    fb.mkdir()
    _bin(fb, "install", FAKE_INSTALL, "#!" + sys.executable)
    _bin(fb, "stat", 'f=$3; [ -e "$f" ] || exit 1; o=$(cat "$f.owner" 2>/dev/null || echo "user:user"); '
                     'echo "$o $(/usr/bin/stat -c %a "$f")"\n')
    _bin(fb, "systemctl", "exit 3\n")
    prog = ('set -euo pipefail\nCHANGES=0\nDO_START=0\nnote() { printf "%s\\n" "$*"; }\n'
            'changed() { CHANGES=$((CHANGES + 1)); note "$*"; }\n'
            'die() { printf "ERROR: %s\\n" "$*" >&2; exit 1; }\n' + block + 'echo "CHANGES=$CHANGES"\n')
    return prog, dict(os.environ, PATH=f"{fb}:/usr/bin:/bin:/usr/sbin:/sbin"), tls


def _cert(d: Path, name: str) -> None:
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-nodes", "-days", "2", "-subj", f"/CN={name}", "-keyout", str(d / f"{name}.key"),
                    "-out", str(d / f"{name}.crt")], check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_install_tls_puts_leaf_and_ca_cert_root_k1000_0640_and_never_the_ca_key(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _cert(src, "k1000")
    _cert(src, "ca")
    shutil.copy(src / "ca.crt", src / "k1000-ca.crt")
    prog, env, tls = _tls_block(tmp_path)
    env["K1000_TLS_DIR"] = str(src)
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(p.name for p in tls.iterdir() if not p.name.endswith(".owner")) == \
        ["k1000-ca.crt", "k1000.crt", "k1000.key"]                          # never ca.key
    assert "ca.key is the CA's private key" in r.stdout
    for f in ("k1000.crt", "k1000.key", "k1000-ca.crt"):
        assert (tls / f).read_bytes() == (src / f).read_bytes()
        assert oct((tls / f).stat().st_mode & 0o777) == "0o640"
        assert (tls / f"{f}.owner").read_text().strip() == "root:k1000"
    assert oct(tls.stat().st_mode & 0o777) == "0o750" and (tls.parent / f"{tls.name}.owner").exists()
    assert "CHANGES=4" in r.stdout
    r2 = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and "CHANGES=0" in r2.stdout, r2.stdout + r2.stderr


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_install_tls_refuses_a_key_that_is_not_the_certificates(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _cert(src, "k1000")
    _cert(src, "other")
    shutil.copy(src / "other.key", src / "k1000.key")
    shutil.copy(src / "other.crt", src / "k1000-ca.crt")
    prog, env, tls = _tls_block(tmp_path)
    env["K1000_TLS_DIR"] = str(src)
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "does not match" in r.stderr
    assert not tls.exists()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
@pytest.mark.parametrize("broken", ["missing", "garbage"])
def test_install_tls_is_checked_before_anything_changes(tmp_path, broken):
    """A failed scp (a file missing) or two unreadable files: die before step 3 syncs new code."""
    text = INSTALL.read_text()
    assert text.index("# --- 0. the phone certificate") < text.index("# --- 1. user and groups") \
        < text.index("# --- 3. the application")
    src = tmp_path / "src"
    src.mkdir()
    if broken == "missing":
        _cert(src, "k1000")
    else:                         # both unreadable: their (empty) public keys must not "match"
        for f in ("k1000.crt", "k1000.key"):
            (src / f).write_text("not a PEM\n")
    (src / "k1000-ca.crt").write_text("x\n")
    if broken == "missing":
        (src / "k1000.key").unlink()
    prog, env, tls = _tls_block(tmp_path)
    env["K1000_TLS_DIR"] = str(src)
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "nothing installed" in r.stderr, r.stdout + r.stderr
    assert not tls.exists()


def test_install_without_tls_dir_says_the_page_is_plain_http(tmp_path):
    prog, env, tls = _tls_block(tmp_path)
    env.pop("K1000_TLS_DIR", None)
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "CHANGES=0" in r.stdout
    assert "plain HTTP (http://10.42.0.1:8080/" in r.stdout


def test_install_leaves_the_ap_units_to_setup_ap(tmp_path):
    """W3: setup-ap.sh alone installs, updates and APPLIES the AP units (--ap); a plain install only
    reports a changed one (copying it would leave setup-ap.sh nothing to apply)."""
    text = INSTALL.read_text()
    assert 'AP_UNITS=(k1000-ap-fence k1000-ap-hostapd k1000-ap-dnsmasq)' in text
    for u in ("k1000-ap-fence", "k1000-ap-hostapd", "k1000-ap-dnsmasq"):
        assert (NET / f"{u}.service").is_file()
    block = text[text.index('for u in "${AP_UNITS[@]}"; do'):text.index("# Reboot the box if PID 1")]
    code = re.sub(r"#.*", "", block)
    assert '[ -e "/etc/systemd/system/$u.service" ] || continue' in block
    for word in ("put ", "install ", "cp ", "enable", "start"):
        assert word not in code, word
    ap = text[text.index("# --- 8b."):text.index("# --- 9.")]
    assert 'bash "$SRC/net/setup-ap.sh" ||' in ap                          # no arguments: its defaults
    # run the report against a fake /etc/systemd/system
    etc = tmp_path / "system"
    etc.mkdir()
    shutil.copy(NET / "k1000-ap-fence.service", etc)
    (etc / "k1000-ap-hostapd.service").write_text("old\n")
    prog = ('set -euo pipefail\nSRC=%s\nAP_UNITS=(k1000-ap-fence k1000-ap-hostapd k1000-ap-dnsmasq)\n'
            'DO_AP=0\nnote() { printf "%%s\\n" "$*"; }\n' % BOX
            + block.replace("/etc/systemd/system", str(etc))
            + ap[ap.index("AP_FAILED=0"):])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "units changed (k1000-ap-hostapd): not applied" in r.stdout and "--ap" in r.stdout
    assert (etc / "k1000-ap-hostapd.service").read_text() == "old\n"
    tail = text[text.index("# Exit 3: CamillaGUI failed"):]
    assert "exit 4" in tail and "exit 5" in tail and "exit 3" in tail


def test_bootstrap_goes_on_when_only_the_access_point_failed():
    text = BOOTSTRAP.read_text()
    case = text[text.index('case $rc in'):text.index('esac', text.index('case $rc in'))]
    assert re.search(r"^\s*4\) AP_FAILED=1;", case, re.M)
    assert re.search(r"^\s*5\) GUI_FAILED=1 AP_FAILED=1;", case, re.M)
    assert text.rstrip().endswith("if [ $AP_FAILED = 1 ]; then exit 4; fi")


def test_bootstrap_no_ap_leaves_the_access_point_marked_down():
    """OPS-1: a spare card after a phone no-go. --no-ap: install.sh without --ap, then the AP marked
    down (setup-ap.sh --down), which every later --ap run keeps."""
    text = BOOTSTRAP.read_text()
    assert re.search(r"^\s*--no-ap\) NO_AP=1 ;;", text, re.M)
    assert 'if [ $NO_AP = 0 ] && grep -Eq' in text and 'flags+=(--ap)' in text
    assert '"$SRC/net/setup-ap.sh" --down' in text
    r = subprocess.run(["bash", str(BOOTSTRAP), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--no-ap" in r.stdout


def test_k1000_service_has_room_for_the_phone_sockets():
    unit = (BOX / "systemd/k1000.service").read_text()
    assert re.search(r"^LimitNOFILE=4096$", unit, re.M)

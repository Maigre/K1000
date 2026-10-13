# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for SETUP > Laptop access, root's side (docs/06-phone.md §11): net/k1000-laptop-access.sh
against a fake nft that keeps a ruleset like the box's (the AP fence's input chain, handles,
comments, as `nft -a list chain` prints them on nft 1.1.3), and the units and install.sh pieces
that run it. Nothing touches a real firewall, unit or network.

The real nft was checked on the box separately, in a throwaway network namespace (never the live
fence): the inserted rule, its listing and its deletion by handle (docs/06-phone.md §10).

    cd box && python3 -m pytest -q -p no:cacheprovider tools/tests/test_laptop_access.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

BOX = Path(__file__).resolve().parents[2]
HELPER = BOX / "net" / "k1000-laptop-access.sh"
PATH_UNIT = BOX / "systemd" / "k1000-laptop.path"
SERVICE = BOX / "systemd" / "k1000-laptop.service"
K1000_UNIT = BOX / "systemd" / "k1000.service"
INSTALL = BOX / "install.sh"
sys.path.insert(0, str(BOX))

# The live box's fence input chain (`sudo nft -a list chain inet k1000_ap input`, 2026-10-13).
FENCE = [
    (4, 'iifname "wlan0" ct state established,related accept'),
    (6, 'iifname "wlan0" udp dport { 53, 67 } accept'),
    (8, 'iifname "wlan0" tcp dport { 80, 443, 8080 } ct state new add @phone_conns { ip saddr ct count '
        'over 16 } counter packets 0 bytes 0 reject with tcp reset'),
    (10, 'iifname "wlan0" tcp dport { 80, 443, 8080 } ct state new ct count over 64 counter packets 0 '
         'bytes 0 reject with tcp reset'),
    (12, 'iifname "wlan0" tcp dport { 53, 80, 443, 8080 } accept'),
    (13, 'iifname "wlan0" icmp type echo-request accept'),
    (14, 'iifname "wlan0" counter packets 24 bytes 4836 drop'),
    (16, 'iifname != { "lo", "wlan0" } ip daddr 10.42.0.1 counter packets 0 bytes 0 drop'),
]
RULE = ('iifname "wlan0" ip saddr 10.42.0.0/24 ip daddr 10.42.0.1 tcp dport { 22, 5005 } '
        'ct state new accept comment "k1000-laptop-access"')

FAKE_NFT = r'''
import json, os, sys
st = os.environ["FAKE"] + "/nft.json"
s = json.load(open(st))
a = sys.argv[1:]
open(os.environ["FAKE"] + "/nft.log", "a").write(json.dumps(a) + "\n")
def save():
    json.dump(s, open(st + ".tmp", "w")); os.replace(st + ".tmp", st)   # never a torn read
handles = "-a" in a
a = [x for x in a if x != "-a"]
if not s["table"]:
    sys.stderr.write("Error: No such file or directory\n"); sys.exit(1)
if a == ["list", "table", "inet", "k1000_ap"]:
    print("table inet k1000_ap {"); print("}"); sys.exit(0)
if a == ["list", "chain", "inet", "k1000_ap", "input"]:
    print("table inet k1000_ap {")
    print("\tchain input {" + (" # handle 1" if handles else ""))
    print("\t\ttype filter hook input priority filter - 10; policy accept;")
    for h, r in s["rules"]:
        print("\t\t" + r + (f" # handle {h}" if handles else ""))
    print("\t}"); print("}"); sys.exit(0)
if a[:5] == ["insert", "rule", "inet", "k1000_ap", "input"]:
    if s.get("fail_insert"):
        sys.stderr.write("Error: Could not process rule\n"); sys.exit(1)
    s["next"] += 1
    s["rules"].insert(0, [s["next"], " ".join(a[5:])]); save(); sys.exit(0)
if a[:5] == ["delete", "rule", "inet", "k1000_ap", "input"] and a[5] == "handle":
    n = len(s["rules"]); s["rules"] = [r for r in s["rules"] if str(r[0]) != a[6]]
    save(); sys.exit(0 if len(s["rules"]) < n else 1)
sys.stderr.write("fake nft: unhandled %r\n" % a); sys.exit(2)
'''


class Root:
    """The helper in a sandbox: a fake nft first on PATH, every path under tmp."""

    def __init__(self, tmp: Path, *, env_text: str | None = "K1000_AP_IFACE=wlan0\nK1000_AP_ADDR=10.42.0.1/24\n"
                 "K1000_AP_BACKEND=hostapd\n", table: bool = True) -> None:
        self.tmp = tmp
        self.fake = tmp / "fake"
        self.fake.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "nft").write_text("#!" + sys.executable + "\n" + FAKE_NFT)
        for p in self.bin.iterdir():
            p.chmod(0o755)
        self.run_dir = tmp / "run-k1000"
        self.run_dir.mkdir()
        self.flag = self.run_dir / "laptop-open"
        self.state_dir = tmp / "run-k1000-laptop"
        self.ap_env = tmp / "ap.env"
        if env_text is not None:
            self.ap_env.write_text(env_text)
        self.uptime = tmp / "uptime"
        self.set_uptime(5000.37)
        self.save({"table": table, "rules": [list(r) for r in FENCE], "next": 16})

    def set_uptime(self, s: float) -> None:
        self.uptime.write_text(f"{s:.2f} 12345.00\n")

    def nft(self) -> dict:
        return json.loads((self.fake / "nft.json").read_text())

    def save(self, s: dict) -> None:
        tmp = self.fake / "nft.json.test"
        tmp.write_text(json.dumps(s))
        os.replace(tmp, self.fake / "nft.json")         # the helper's nft may be reading it

    def ours(self) -> list[str]:
        return [r for _h, r in self.nft()["rules"] if "k1000-laptop-access" in r]

    def env(self, **kw: str) -> dict:
        e = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", FAKE=str(self.fake),
                 K1000_LAPTOP_FLAG=str(self.flag), K1000_LAPTOP_STATE_DIR=str(self.state_dir),
                 K1000_AP_ENV=str(self.ap_env), K1000_UPTIME=str(self.uptime),
                 K1000_LAPTOP_TICK_S="0.02", K1000_LAPTOP_CHECK_TICKS="3")
        e.update(kw)
        return e

    def sh(self, cmd: str, rc: int | None = 0, **kw: str) -> str:
        r = subprocess.run(["sh", str(HELPER), cmd], capture_output=True, text=True, env=self.env(**kw),
                           timeout=30)
        if rc is not None:
            assert r.returncode == rc, f"{cmd} -> {r.returncode}\n{r.stdout}{r.stderr}"
        return r.stdout + r.stderr

    def start(self, **kw: str) -> subprocess.Popen:
        return subprocess.Popen(["sh", str(HELPER), "run"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=self.env(**kw))

    def status(self) -> dict:
        p = self.state_dir / "status"
        if not p.exists():
            return {}
        return dict(line.split("=", 1) for line in p.read_text().splitlines() if "=" in line)

    def wait(self, pred, what: str, timeout: float = 5.0) -> None:
        t = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > t:
                raise AssertionError(f"{what} not met")
            time.sleep(0.01)



def _open(root: Root, **kw: str) -> subprocess.Popen:
    root.flag.write_text("open\n")
    p = root.start(**kw)
    root.wait(lambda: root.status().get("state") == "open", "opened")
    return p


# ======================================================================= opening and closing
def test_opens_one_rule_for_the_ap_subnet_and_closes_when_the_box_asks(tmp_path):
    root = Root(tmp_path)
    p = _open(root)
    assert root.ours() == [RULE]
    rules = root.nft()["rules"]
    assert rules[0][1] == RULE and [r for _h, r in rules[1:]] == [r for _h, r in FENCE]   # at the head
    st = root.status()
    assert st == {"state": "open", "until": str(5000 + 7200), "at": "5000", "ports": "22,5005",
                  "subnet": "10.42.0.0/24", "iface": "wlan0"}
    assert oct((root.state_dir / "status").stat().st_mode & 0o777) == "0o644"
    root.flag.unlink()                                         # SETUP > Laptop access, one push
    assert p.wait(5) == 0
    assert "laptop access OPEN for 120 min: tcp 22 (SSH) and 5005 (CamillaGUI) from 10.42.0.0/24" \
        in p.stdout.read()
    out = root.sh("close")                                     # the unit's ExecStopPost
    assert root.ours() == [] and len(root.nft()["rules"]) == len(FENCE)
    assert root.status()["state"] == "closed" and root.status()["reason"] == "box"
    assert not root.flag.exists() and not (root.state_dir / "reason").exists()
    assert "laptop access CLOSED (box)" in out


def test_closes_by_itself_after_two_hours_on_the_boot_clock(tmp_path):
    root = Root(tmp_path)
    p = _open(root)
    root.set_uptime(5000.37 + 7199)
    time.sleep(0.15)
    assert p.poll() is None and root.ours() == [RULE]
    root.set_uptime(5000.37 + 7200)
    assert p.wait(5) == 0
    root.sh("close")
    assert root.ours() == [] and root.status()["reason"] == "time" and not root.flag.exists()


def test_never_longer_than_two_hours_whatever_the_environment(tmp_path):
    root = Root(tmp_path)
    p = _open(root, K1000_LAPTOP_S="999999")
    assert root.status()["until"] == str(5000 + 7200)
    root.flag.unlink()
    p.wait(5)
    root.sh("close")
    p = _open(root, K1000_LAPTOP_S="12;rm -rf /")
    assert root.status()["until"] == str(5000 + 7200)
    root.flag.unlink()
    p.wait(5)
    root.sh("close")


def test_k1000_stopping_closes_it(tmp_path):
    root = Root(tmp_path)
    p = _open(root)
    shutil.rmtree(root.run_dir)                                # systemd removes /run/k1000
    assert p.wait(5) == 0
    root.sh("close")
    assert root.ours() == [] and root.status()["reason"] == "k1000"


def test_a_stop_of_the_unit_closes_it_too(tmp_path):
    """systemd stops the unit (k1000 stopped: PartOf=; a kill; RuntimeMaxSec): SIGTERM, then
    ExecStopPost = close, whatever the loop was doing."""
    root = Root(tmp_path)
    p = _open(root)
    p.terminate()
    p.wait(5)
    root.sh("close")
    assert root.ours() == [] and root.status()["reason"] == "stopped" and not root.flag.exists()


def test_a_reloaded_fence_gets_the_opening_back_and_a_vanished_fence_closes(tmp_path):
    root = Root(tmp_path)
    p = _open(root)
    s = root.nft()
    s["rules"] = [list(r) for r in FENCE]                      # setup-ap.sh re-run: nft -f
    root.save(s)
    root.wait(lambda: root.ours() == [RULE], "put back")
    s = root.nft()
    s["table"] = False                                         # the fence unit stopped
    root.save(s)
    assert p.wait(5) == 0
    out = p.stdout.read()
    assert "the fence was reloaded: opening put back" in out and "the AP fence went away" in out
    s["table"] = True
    s["rules"] = [list(r) for r in FENCE]
    root.save(s)
    root.sh("close")
    assert root.status()["reason"] == "no-fence" and not root.flag.exists()


# ======================================================================= refusals
def test_no_fence_no_opening(tmp_path):
    root = Root(tmp_path, table=False)
    root.flag.write_text("open\n")
    out = root.sh("run", rc=1)
    assert "REFUSED: the AP fence (nftables table inet k1000_ap) is not loaded" in out
    root.sh("close")
    assert root.status()["state"] == "closed" and root.status()["reason"] == "no-fence"
    assert not root.flag.exists()                              # the path unit never re-fires


@pytest.mark.parametrize("env_text", [
    None,
    "K1000_AP_IFACE=wlan0\n",
    "K1000_AP_IFACE=wlan0\nK1000_AP_ADDR=10.42.0.1/16\n",
    "K1000_AP_IFACE=wlan0\nK1000_AP_ADDR=10.42.0.1; drop\n",
    'K1000_AP_IFACE=wlan0" accept #\nK1000_AP_ADDR=10.42.0.1/24\n',
    "K1000_AP_IFACE=wlan0 lo\nK1000_AP_ADDR=10.42.0.1/24\n",
    "K1000_AP_IFACE=averyveryverylongname\nK1000_AP_ADDR=10.42.0.1/24\n",
    "K1000_AP_IFACE=wlan0\nK1000_AP_ADDR=10.42.0.x/24\n",
])
def test_unusable_ap_settings_are_refused_before_any_rule(tmp_path, env_text):
    root = Root(tmp_path, env_text=env_text)
    root.flag.write_text("open\n")
    out = root.sh("run", rc=1)
    assert "REFUSED: no usable AP settings" in out
    assert root.ours() == [] and not any("insert" in line for line in
                                         (root.fake / "nft.log").read_text().splitlines())
    root.sh("close")
    assert root.status()["reason"] == "no-ap"


def test_nft_refusing_the_rule_is_an_error(tmp_path):
    root = Root(tmp_path)
    s = root.nft()
    s["fail_insert"] = True
    root.save(s)
    root.flag.write_text("open\n")
    assert "ERROR: nft refused the opening rule" in root.sh("run", rc=1)
    root.sh("close")
    assert root.status()["reason"] == "error" and not root.flag.exists()


def test_without_a_request_nothing_is_opened(tmp_path):
    root = Root(tmp_path)
    root.sh("run")
    assert root.ours() == [] and not (root.fake / "nft.log").exists()


def test_close_is_idempotent_and_removes_every_copy(tmp_path):
    root = Root(tmp_path)
    s = root.nft()
    s["rules"] = [[30, RULE], [31, RULE]] + s["rules"]
    root.save(s)
    root.sh("close")
    assert root.ours() == [] and len(root.nft()["rules"]) == len(FENCE)
    root.sh("close")
    assert root.status()["state"] == "closed"


def test_status_for_a_human(tmp_path):
    root = Root(tmp_path)
    assert "laptop access: closed" in root.sh("status")
    p = _open(root)
    out = root.sh("status")
    assert "laptop access: OPEN" in out and RULE in out and "state=open" in out
    root.flag.unlink()
    p.wait(5)
    root.sh("close")
    assert root.sh("x", rc=2).startswith("usage:")


def test_helper_syntax_mode_and_shellcheck():
    assert os.access(HELPER, os.X_OK)
    assert subprocess.run(["sh", "-n", str(HELPER)]).returncode == 0
    if shutil.which("dash"):
        assert subprocess.run(["dash", "-n", str(HELPER)]).returncode == 0
    if shutil.which("shellcheck") is None:
        pytest.skip("no shellcheck")
    r = subprocess.run(["shellcheck", str(HELPER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


# ======================================================================= units and install.sh
def _ini(path: Path) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    sec = ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            sec = line.strip("[]")
            out.setdefault(sec, [])
            continue
        k, _, v = line.partition("=")
        out.setdefault(sec, []).append((k, v))
    return out


def _get(ini: dict, sec: str, key: str) -> list[str]:
    return [v for k, v in ini.get(sec, []) if k == key]


def test_units_agree_with_the_k1000_side_and_never_start_at_boot():
    from k1000 import laptop
    path, svc, k = _ini(PATH_UNIT), _ini(SERVICE), _ini(K1000_UNIT)
    assert _get(path, "Path", "PathExists") == [str(laptop.FLAG)]
    assert _get(path, "Path", "Unit") == ["k1000-laptop.service"]
    assert _get(svc, "Unit", "ConditionPathExists") == [str(laptop.FLAG)]
    assert "Install" not in path and "Install" not in svc            # nothing enables them
    assert _get(svc, "Unit", "Requisite") == ["k1000.service"]          # never pulls k1000 in
    assert _get(svc, "Unit", "PartOf") == ["k1000.service"]
    assert not _get(svc, "Unit", "Requires") and not _get(svc, "Unit", "BindsTo")
    assert _get(svc, "Service", "ExecStart") == ["/bin/sh /opt/k1000/net/k1000-laptop-access.sh run"]
    assert _get(svc, "Service", "ExecStopPost") == ["/bin/sh /opt/k1000/net/k1000-laptop-access.sh close"]
    assert _get(svc, "Service", "RuntimeDirectory") == [laptop.STATUS.parent.name]
    assert _get(svc, "Service", "RuntimeDirectoryPreserve") == ["yes"]
    assert _get(svc, "Service", "CapabilityBoundingSet") == ["CAP_NET_ADMIN CAP_DAC_OVERRIDE"]
    assert _get(svc, "Service", "NoNewPrivileges") == ["yes"]
    assert _get(svc, "Service", "ProtectSystem") == ["strict"]
    assert int(_get(svc, "Service", "RuntimeMaxSec")[0]) <= 7200 + 120
    assert not _get(svc, "Service", "Restart")
    # k1000 itself: its runtime directory holds the flag, it wants the watcher, and it still has
    # no way to root (the bounding set is CAP_NET_BIND_SERVICE alone: no sudo, no nft)
    assert _get(k, "Service", "RuntimeDirectory") == [laptop.FLAG.parent.name]
    assert "k1000-laptop.path" in _get(k, "Unit", "Wants")[0].split()
    assert _get(k, "Service", "CapabilityBoundingSet") == ["CAP_NET_BIND_SERVICE"]
    assert _get(k, "Service", "User") == ["k1000"]


def test_the_helper_opens_exactly_the_documented_ports():
    from k1000 import laptop
    text = HELPER.read_text()
    assert 'PORTS="22, 5005"' in text and tuple(laptop.PORTS) == (22, 5005)
    assert '[ "$DURATION" -le 7200 ] || DURATION=7200' in text
    assert re.search(r"^TAG=k1000-laptop-access$", text, re.M)


def test_install_puts_the_laptop_units_and_never_enables_or_starts_them():
    text = INSTALL.read_text()
    assert "LAPTOP_UNITS=(k1000-laptop.path k1000-laptop.service)" in text
    block = text[text.index('for u in "${LAPTOP_UNITS[@]}"; do'):]
    block = block[:block.index("done") + 4]
    assert 'put "$SRC/systemd/$u" "/etc/systemd/system/$u" 644' in block
    code = re.sub(r"#.*", "", text)
    assert not re.search(r"systemctl (enable|start|restart)[^\n]*laptop", code)
    assert not re.search(r"systemctl (enable|start)[^\n]*LAPTOP_UNITS", code)
    for u in ("k1000-laptop.path", "k1000-laptop.service"):
        assert (BOX / "systemd" / u).is_file()
    # the helper ships with the mirror (/opt/k1000/net), root-owned: install.sh's rsync of box/
    assert HELPER.parent.name == "net"

# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/k1000-cpu.py, the two bench switches against the USB stall (D-023 point 3) and
their check, and for their units in cpu/. The tool runs against a fake /proc and /sys shaped like
the box's (Raspberry Pi 4, four cores, the xHCI's interrupt an MSI the kernel refuses to move) and
a fake kernel that records every change: no real thread, interrupt or file of this machine is
changed. One test runs the read-only check on this machine's own /proc.

What only the box can show (the kernel's real refusals, the effect on the delay steps) is in
docs/manual/02-installation.md §6, "The bench switches".

    cd box && python3 -m pytest -q -p no:cacheprovider tools/tests/test_cpu_switches.py
"""
from __future__ import annotations

import errno
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BOX = Path(__file__).resolve().parents[2]
TOOL = BOX / "tools" / "k1000-cpu.py"
CPU_DIR = BOX / "cpu"


def _load():
    spec = importlib.util.spec_from_file_location("k1000_cpu", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["k1000_cpu"] = mod          # dataclasses resolve the module by name
    spec.loader.exec_module(mod)
    return mod


kc = _load()
PF_KTHREAD = 0x00200000


class FakeKernel:
    """Applies the tool's changes to the fake tree, like the kernel would, and records them.
    `refuse`: paths whose write fails with EIO (a per-CPU or chained interrupt)."""

    def __init__(self, fake: "FakePi") -> None:
        self.fake = fake
        self.refuse: set[Path] = set()
        self.calls: list[tuple] = []

    def write(self, path: Path, text: str) -> None:
        self.calls.append(("write", str(path.relative_to(self.fake.root)), text))
        if path in self.refuse:
            raise OSError(errno.EIO, "Input/output error")
        if not path.exists():
            raise OSError(errno.ENOENT, "No such file or directory")
        path.write_text(text)

    def set_affinity(self, tid: int, cpus) -> None:
        self.calls.append(("affinity", tid, frozenset(cpus)))
        self.fake.set_task(tid, cpus=kc.fmt_cpus(cpus))

    def set_sched(self, tid: int, policy: int, prio: int) -> None:
        self.calls.append(("sched", tid, policy, prio))
        self.fake.set_task(tid, policy=policy, prio=prio)


class NoChanges(FakeKernel):
    def write(self, path, text):
        raise AssertionError(f"check wrote {path}")

    def set_affinity(self, tid, cpus):
        raise AssertionError(f"check moved {tid}")

    def set_sched(self, tid, policy, prio):
        raise AssertionError(f"check rescheduled {tid}")


class FakePi:
    """A Raspberry Pi 4 running K1000 with the processor settings on, as files under `root`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.tasks: dict[int, dict] = {}
        self.kernel = FakeKernel(self)
        w = self.w
        w("sys/devices/system/cpu/online", "0-3\n")
        for c in range(4):
            w(f"sys/devices/system/cpu/cpu{c}/cpufreq/scaling_governor", "performance\n")
        w("sys/devices/virtual/workqueue/cpumask", "f\n")
        w("proc/irq/default_smp_affinity", "f\n")
        w("proc/sys/kernel/sched_rt_runtime_us", "950000\n")
        w("proc/sys/kernel/sched_rt_period_us", "1000000\n")
        # interrupts as on the box: per-CPU ones refuse a move, the xHCI's MSI too, the PCIe MSI's
        # own parent has no handler directory (a chained demultiplexer)
        self.irq(11, ["arch_timer"], refuse=True)
        self.irq(23, [])                                   # brcmstb PCIe MSI demultiplexer
        self.irq(37, ["xhci_hcd"], refuse=True)
        self.irq(40, ["mmc0", "mmc1"])                     # the SD card and the Wi-Fi's SDIO
        self.irq(41, ["eth0"])
        self.irq(42, ["eth0"])
        self.irq(45, ["fe215040.serial"], cpus="1")        # already off core 0
        # threads: kernel, then user space (processor settings on: 0-1, the engine 2-3)
        self.task(2, comm="kthreadd", kernel=True, cpus="0-3")
        self.task(14, comm="ksoftirqd/0", kernel=True, cpus="0")
        self.task(22, comm="ksoftirqd/1", kernel=True, cpus="1")
        self.task(12, comm="migration/0", kernel=True, cpus="0", policy=1, prio=99)
        self.task(95, comm="irq/40-mmc0", kernel=True, cpus="0-3", policy=1, prio=50)
        self.task(96, comm="irq/40-mmc1", kernel=True, cpus="0-3", policy=1, prio=50)
        self.task(1, comm="systemd", cpus="0-1")
        self.task(300, comm="systemd-journal", cpus="0-1", unit="systemd-journald.service")
        self.task(700, comm="camilladsp", cpus="2-3", unit="camilladsp.service")
        self.task(700, tid=705, comm="AlsaPlayback", cpus="2-3", unit="camilladsp.service", policy=1, prio=10)
        self.task(800, comm="python3", cpus="0-1", unit="k1000.service")
        self.task(800, tid=801, comm="python3", cpus="0-1", unit="k1000.service")
        self.task(900, comm="camillagui", cpus="0-1", unit="camillagui.service")
        self.task(950, comm="sh", cpus="0-1", unit="k1000-usbwatch.service")
        self.task(1200, comm="sshd", cpus="0-1", unit="ssh.service")
        self.interrupts({"11": [500, 400, 400, 400], "37": [16000, 0, 0, 0], "40": [30, 0, 0, 0]})

    def w(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def irq(self, n: int, names: list[str], cpus: str = "0-3", refuse: bool = False) -> None:
        self.w(f"proc/irq/{n}/smp_affinity_list", cpus + "\n")
        for name in names:
            (self.root / f"proc/irq/{n}/{name}").mkdir()
        if refuse:
            self.kernel.refuse.add(self.root / f"proc/irq/{n}/smp_affinity_list")

    def task(self, pid: int, tid: int | None = None, comm: str = "x", cpus: str = "0-3",
             kernel: bool = False, policy: int = 0, prio: int = 0, unit: str = "") -> None:
        tid = tid or pid
        self.tasks[tid] = dict(pid=pid, comm=comm, cpus=cpus, flags=PF_KTHREAD if kernel else 0x400100,
                               policy=policy, prio=prio)
        self.w(f"proc/{pid}/cgroup", f"0::/system.slice/{unit}\n" if unit else "0::/\n")
        self._write_task(tid)

    def _write_task(self, tid: int) -> None:
        t = self.tasks[tid]
        d = f"proc/{t['pid']}/task/{tid}"
        self.w(f"{d}/status", f"Name:\t{t['comm']}\nUmask:\t0022\nState:\tS (sleeping)\nTgid:\t{t['pid']}\n"
                              f"PPid:\t1\nCpus_allowed:\tf\nCpus_allowed_list:\t{t['cpus']}\n")
        rest = ["S"] + ["0"] * 49                          # stat fields 3 to 52
        rest[6], rest[37], rest[38] = str(t["flags"]), str(t["prio"]), str(t["policy"])
        self.w(f"{d}/stat", f"{tid} ({t['comm']}) " + " ".join(rest) + "\n")

    def set_task(self, tid: int, **kw) -> None:
        if tid not in self.tasks:
            raise ProcessLookupError(errno.ESRCH, "No such process")
        self.tasks[tid].update(kw)
        self._write_task(tid)

    def interrupts(self, counts: dict[str, list[int]]) -> None:
        names = {"11": "GICv2  30 Level     arch_timer", "37": "BRCM STB PCIe MSI 524288 Edge      xhci_hcd",
                 "40": "GICv2 158 Level     mmc0, mmc1"}
        lines = ["           CPU0       CPU1       CPU2       CPU3"]
        lines += [f"{k:>4}: " + " ".join(f"{c:10d}" for c in v) + "   " + names[k] for k, v in counts.items()]
        lines.append("IPI0:        10         10         10         10       Rescheduling interrupts")
        lines.append(" Err:          0")
        self.w("proc/interrupts", "\n".join(lines) + "\n")

    def box(self, kernel: FakeKernel | None = None) -> "kc.Box":
        return kc.Box(self.root, kernel or self.kernel)

    def cpus(self, tid: int) -> str:
        return self.tasks[tid]["cpus"]

    def irq_cpus(self, n: int) -> str:
        return (self.root / f"proc/irq/{n}/smp_affinity_list").read_text().strip()

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text().strip()

    def choose(self, **choices: str) -> None:
        files = {"cpu": "cpu-tuning", "usb_cpu0": "usb-cpu0", "usb_rt": "usb-rt"}
        for k, v in choices.items():
            self.w(f"etc/k1000/{files[k]}", f"# K1000 {files[k]}: on or off. Set by install.sh.\n{v}\n")


@pytest.fixture
def pi(tmp_path):
    return FakePi(tmp_path / "root")


def _check(pi: FakePi, sample: float = 0) -> tuple[str, int]:
    lines, rc = kc.check(pi.box(NoChanges(pi)), sample=sample, sleep=lambda s: None)
    return "\n".join(lines), rc


# --------------------------------------------------------------------------- the cpu lists
def test_cpu_lists_and_masks_read_and_print_like_the_kernel():
    assert kc.parse_cpus("0-1,3") == {0, 1, 3} and kc.parse_cpus("") == set() and kc.parse_cpus(None) == set()
    assert kc.fmt_cpus({0, 1, 3}) == "0-1,3" and kc.fmt_cpus({1}) == "1" and kc.fmt_cpus(set()) == "-"
    assert kc.parse_mask("f\n") == {0, 1, 2, 3} and kc.parse_mask("00000002") == {1}
    assert kc.parse_mask("1,00000001") == {0, 32}
    assert kc.fmt_mask({1}) == "2" and kc.fmt_mask({1, 2, 3}) == "e" and kc.fmt_mask({0, 1, 2, 3}) == "f"


def _ini(path: Path, section: str) -> list[tuple[str, str]]:
    out, cur = [], None
    for line in path.read_text().splitlines():
        if line.startswith("["):
            cur = line.strip("[]")
        elif cur == section and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out.append((k, v))
    return out


def test_the_tools_cores_are_the_files_cores():
    """The tool's constants and the files install.sh puts in place say the same cores."""
    base = dict(_ini(CPU_DIR / "system.conf", "Manager"))["CPUAffinity"]
    assert kc.parse_cpus(base) == kc.BASE_CORES
    assert kc.parse_cpus(dict(_ini(CPU_DIR / "camilladsp.conf", "Service"))["CPUAffinity"]) == kc.ENGINE_CORES
    for name, section in (("usb-cpu0-system.conf", "Manager"), ("usb-cpu0-k1000.conf", "Service"),
                          ("usb-cpu0-camillagui.conf", "Service")):
        # an empty assignment first (CPUAffinity= adds up: it resets the 0 1 of 50-k1000-cpu.conf)
        assert _ini(CPU_DIR / name, section) == [("CPUAffinity", ""), ("CPUAffinity", kc.fmt_cpus(kc.USB_CPU0_CORES))]
    assert kc.USB_CPU not in kc.USB_CPU0_CORES and kc.USB_CPU0_CORES < kc.BASE_CORES
    assert not kc.ENGINE_CORES & kc.BASE_CORES
    assert kc.IRQ_THREAD_PRIO < kc.USB_RT_PRIO < 99


# --------------------------------------------------------------------------- usb-cpu0
def test_usb_cpu0_moves_what_can_move_off_core_0_and_keeps_the_usb_host(pi, tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    log, rc = kc.apply_usb_cpu0(pi.box(), str(run))
    text = "\n".join(log)
    assert rc == 0, text
    assert pi.irq_cpus(40) == "1" and pi.irq_cpus(41) == "1" and pi.irq_cpus(42) == "1"   # SD/Wi-Fi, eth0
    assert pi.irq_cpus(37) == "0-3"                     # the xHCI's: never written at all
    assert not [c for c in pi.kernel.calls if c[0] == "write" and "irq/37/" in c[1]]
    assert not [c for c in pi.kernel.calls if c[0] == "write" and "irq/23/" in c[1]]   # the MSI's parent
    assert not [c for c in pi.kernel.calls if c[0] == "write" and "irq/45/" in c[1]]   # already off 0
    assert pi.irq_cpus(11) == "0-3" and "arch_timer (Input/output error)" in text          # refused
    assert pi.read("proc/irq/default_smp_affinity") == "2"                                 # new ones: core 1
    assert pi.read("sys/devices/virtual/workqueue/cpumask") == "e"                         # 1-3
    for tid in (1, 300, 800, 801, 900, 950, 1200):                                         # user space: 1
        assert pi.cpus(tid) == "1", tid
    assert pi.cpus(700) == pi.cpus(705) == "2-3"                                           # the engine
    for tid in (2, 14, 12, 95):                                                            # kernel threads
        assert pi.tasks[tid]["cpus"] in ("0", "0-3"), tid
    assert "7 user-space threads moved to core 1" in text
    rec = json.loads((run / "report").read_text())
    assert rec["kept"] == [[37, ["xhci_hcd"]]]
    assert [r[:2] for r in rec["refused"]] == [[11, ["arch_timer"]]]
    assert [r[0] for r in rec["moved"]] == [40, 41, 42] and rec["threads"] == 7
    assert rec["elsewhere"] == [45]


def test_usb_cpu0_never_moves_the_engine_even_when_it_is_on_core_0(pi):
    """Processor settings just turned on: the engine keeps 0-3 until its next start. The switch
    leaves it to them and says so."""
    pi.set_task(700, cpus="0-3")
    pi.set_task(705, cpus="0-3")
    log, rc = kc.apply_usb_cpu0(pi.box())
    assert rc == 0 and pi.cpus(700) == pi.cpus(705) == "0-3"
    assert "WARNING: the engine is allowed on core 0 (2 threads)" in "\n".join(log)


def test_usb_cpu0_undo_gives_back_what_a_boot_without_it_gives(pi, tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    kc.apply_usb_cpu0(pi.box(), str(run))
    pi.task(1300, comm="later", cpus="1", unit="k1000.service")       # started while it was on
    pi.irq(50, ["spi0"], cpus="1")                                     # set up while it was on
    log, rc = kc.undo_usb_cpu0(pi.box(), str(run))
    assert rc == 0, log
    assert [pi.irq_cpus(n) for n in (40, 41, 42, 45, 50)] == ["0-3", "0-3", "0-3", "1", "0-3"]
    assert pi.read("proc/irq/default_smp_affinity") == "f"
    assert pi.read("sys/devices/virtual/workqueue/cpumask") == "f"
    for tid in (1, 300, 800, 801, 900, 950, 1200, 1300):
        assert pi.cpus(tid) == "0-1", tid                              # the processor settings' cores
    assert pi.cpus(700) == "2-3"
    assert not (run / "report").exists()
    # the xHCI's interrupt was never moved: undo writes it neither
    assert not [c for c in pi.kernel.calls if c[0] == "write" and "irq/37/" in c[1]]


def test_usb_cpu0_twice_changes_nothing_more(pi, tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    kc.apply_usb_cpu0(pi.box(), str(run))
    n = len(pi.kernel.calls)
    log, rc = kc.apply_usb_cpu0(pi.box(), str(run))
    again = pi.kernel.calls[n:]
    # only the refused per-CPU interrupt and the two masks are written again; no thread moves
    assert rc == 0 and not [c for c in again if c[0] == "affinity"], again
    assert {c[1] for c in again if c[0] == "write"} == {"proc/irq/11/smp_affinity_list",
                                                        "proc/irq/default_smp_affinity",
                                                        "sys/devices/virtual/workqueue/cpumask"}
    # and its undo still knows which interrupt was off core 0 before the first run
    assert json.loads((run / "report").read_text())["elsewhere"] == [45]
    kc.undo_usb_cpu0(pi.box(), str(run))
    assert [pi.irq_cpus(n) for n in (40, 41, 42, 45)] == ["0-3", "0-3", "0-3", "1"]


def test_usb_cpu0_fails_when_the_new_interrupt_mask_cannot_be_written(pi):
    pi.kernel.refuse.add(pi.root / "proc/irq/default_smp_affinity")
    log, rc = kc.apply_usb_cpu0(pi.box())
    assert rc == 1 and "ERROR: /proc/irq/default_smp_affinity" in "\n".join(log)


def test_usb_cpu0_without_a_workqueue_mask_still_applies(pi):
    (pi.root / "sys/devices/virtual/workqueue/cpumask").unlink()
    log, rc = kc.apply_usb_cpu0(pi.box())
    assert rc == 0 and pi.irq_cpus(40) == "1" and "workqueues" not in "\n".join(log)


# --------------------------------------------------------------------------- usb-rt
def test_usb_rt_puts_ksoftirqd_0_alone_at_fifo_51_and_undo_puts_it_back(pi):
    log, rc = kc.apply_usb_rt(pi.box())
    text = "\n".join(log)
    assert rc == 0, text
    assert (pi.tasks[14]["policy"], pi.tasks[14]["prio"]) == (1, 51)
    assert (pi.tasks[22]["policy"], pi.tasks[22]["prio"]) == (0, 0)            # ksoftirqd/1 untouched
    assert (pi.tasks[95]["policy"], pi.tasks[95]["prio"]) == (1, 50)           # the SD/Wi-Fi thread
    assert [c for c in pi.kernel.calls if c[0] == "sched"] == [("sched", 14, 1, 51)]
    assert "ksoftirqd/0(14): SCHED_FIFO 51 (was SCHED_OTHER)" in text
    assert "no xHCI interrupt thread" in text and "950000 of every 1000000" in text
    log, rc = kc.undo_usb_rt(pi.box())
    assert rc == 0 and (pi.tasks[14]["policy"], pi.tasks[14]["prio"]) == (0, 0), log


def test_usb_rt_raises_the_xhci_thread_too_when_the_kernel_runs_one(pi):
    """threadirqs on the kernel command line: the xHCI's handler runs in irq/37-xhci_hcd (FIFO 50)."""
    pi.task(97, comm="irq/37-xhci_hcd", kernel=True, cpus="0", policy=1, prio=50)
    pi.task(98, comm="irq/137-xhci_hc", kernel=True, cpus="0", policy=1, prio=50)   # comm cut at 15
    pi.task(99, comm="irq/40-mmc0", kernel=True, cpus="0", policy=1, prio=50)
    log, rc = kc.apply_usb_rt(pi.box())
    assert rc == 0, log
    assert [(c[1], c[3]) for c in pi.kernel.calls if c[0] == "sched"] == [(14, 51), (97, 51), (98, 51)]
    out, crc = _check_with(pi, usb_rt="on")
    assert "usb-rt, real-time priority for the deferred USB work: chosen on (/etc/k1000/usb-rt), in force" in out
    kc.undo_usb_rt(pi.box())
    assert [(pi.tasks[t]["policy"], pi.tasks[t]["prio"]) for t in (14, 97, 98, 99)] == [(0, 0), (1, 50), (1, 50), (1, 50)]


@pytest.mark.parametrize("runtime", ["-1", "1000000", "", None])
def test_usb_rt_refused_with_nothing_changed_without_the_real_time_throttle(pi, runtime):
    path = pi.root / "proc/sys/kernel/sched_rt_runtime_us"
    if runtime is None:
        path.unlink()
    else:
        path.write_text(runtime + "\n")
    log, rc = kc.apply_usb_rt(pi.box())
    assert rc == 1 and "refused, nothing changed: the real-time throttle is off" in log[0]
    assert pi.kernel.calls == []


def test_usb_rt_without_ksoftirqd_0_is_an_error_and_changes_nothing(pi):
    pi.tasks[14]["comm"] = "ksoftirqd/9"
    pi._write_task(14)
    log, rc = kc.apply_usb_rt(pi.box())
    assert rc == 1 and "no ksoftirqd/0" in log[0] and pi.kernel.calls == []


def test_a_user_thread_named_like_ksoftirqd_is_never_touched(pi):
    pi.task(2000, comm="ksoftirqd/0", cpus="0-1")                    # not a kernel thread
    kc.apply_usb_rt(pi.box())
    assert [c[1] for c in pi.kernel.calls if c[0] == "sched"] == [14]


# --------------------------------------------------------------------------- check
def _check_with(pi: FakePi, **choices: str) -> tuple[str, int]:
    pi.choose(**choices)
    return _check(pi)


def test_check_with_both_off_by_default(pi):
    out, rc = _check(pi)                                # no choice files: settings on, switches off
    assert rc == 0, out
    assert "processor settings: chosen on (/etc/k1000/cpu-tuning)" in out
    assert "governor: performance on cores 0-3" in out and "systemd (PID 1): cores 0-1" in out
    assert "units: camilladsp 2-3 · camillagui 0-1 · k1000 0-1 · k1000-usbwatch 0-1" in out
    assert "usb-cpu0, core 0 left to the USB handling: chosen off (/etc/k1000/usb-cpu0), not in force" in out
    assert "usb-rt, real-time priority for the deferred USB work: chosen off (/etc/k1000/usb-rt), not in force" in out
    assert "[ ] ksoftirqd/0 at SCHED_FIFO 51 (now SCHED_OTHER)" in out
    assert "real-time threads allowed on core 0: migration/0 SCHED_FIFO 99 · irq/40-mmc0 SCHED_FIFO 50" in out
    assert "NOT AS CHOSEN" not in out


def test_check_says_when_a_switch_chosen_on_is_not_in_force(pi):
    out, rc = _check_with(pi, usb_cpu0="on", usb_rt="on")
    assert rc == 1
    assert "chosen on (/etc/k1000/usb-cpu0), not in force: NOT AS CHOSEN" in out
    assert "chosen on (/etc/k1000/usb-rt), not in force: NOT AS CHOSEN" in out


def test_check_in_force_after_both_apply(pi, tmp_path):
    run = pi.root / "run/k1000-usb-cpu0"
    run.mkdir(parents=True)
    kc.apply_usb_cpu0(pi.box(), str(run))
    kc.apply_usb_rt(pi.box())
    out, rc = _check_with(pi, usb_cpu0="on", usb_rt="on")
    assert rc == 0, out
    assert "usb-cpu0, core 0 left to the USB handling: chosen on (/etc/k1000/usb-cpu0), in force" in out
    for line in ("[x] applied by k1000-usb-cpu0.service at ", "[x] systemd (PID 1) on core 1",
                 "[x] K1000 units on core 1", "[x] no user-space thread allowed on core 0",
                 "[x] new interrupts on core 1 (default_smp_affinity 2)",
                 "[x] kernel workqueues off core 0 (cores 1-3)",
                 "[x] the interrupts it moved are off core 0 (3)",
                 "stays on core 0 (the kernel cannot move it): 11 arch_timer · 37 xhci_hcd (USB host)",
                 "[x] ksoftirqd/0 at SCHED_FIFO 51 (now SCHED_FIFO 51)",
                 "usb-rt, real-time priority for the deferred USB work: chosen on (/etc/k1000/usb-rt), in force"):
        assert line in out, line
    # the processor settings off: the switches count as off whatever their files say
    out, rc = _check_with(pi, cpu="off")
    assert rc == 1 and "usb-cpu0, core 0 left to the USB handling: chosen off" in out


def test_check_names_what_breaks_a_switch(pi):
    """A bench load pinned to core 0 afterwards, an interrupt moved back by hand: PARTLY."""
    run = pi.root / "run/k1000-usb-cpu0"
    run.mkdir(parents=True)
    kc.apply_usb_cpu0(pi.box(), str(run))
    pi.task(3000, comm="stress", cpus="0")
    (pi.root / "proc/irq/41/smp_affinity_list").write_text("0-3\n")
    out, rc = _check_with(pi, usb_cpu0="on")
    assert rc == 1
    assert "usb-cpu0, core 0 left to the USB handling: chosen on (/etc/k1000/usb-cpu0), PARTLY in force: NOT AS CHOSEN" in out
    assert "[ ] no user-space thread allowed on core 0 (1: stress(3000))" in out
    assert "[ ] the interrupts it moved are off core 0 (3) (back on it: 41 eth0)" in out


def test_check_an_unmovable_interrupt_set_up_after_the_switch_is_not_a_failure(pi):
    """k1000 restarted: the knob board's GPIO lines are set up again, on a chip that cannot move
    them. The switch is still in force; the check names them."""
    run = pi.root / "run/k1000-usb-cpu0"
    run.mkdir(parents=True)
    kc.apply_usb_cpu0(pi.box(), str(run))
    pi.irq(60, ["k1000-knobs"], cpus="0-3", refuse=True)
    out, rc = _check_with(pi, usb_cpu0="on")
    assert rc == 0 and "chosen on (/etc/k1000/usb-cpu0), in force" in out, out
    assert "also on core 0 (set up since; the kernel may refuse to move it): 60 k1000-knobs" in out


def test_check_reads_the_choice_files_like_install_sh(pi):
    for text, on in (("on", True), ("  on  ", True), ("# off\non\n", True), ("maybe", False), ("", False)):
        pi.w("etc/k1000/usb-rt", text)
        out, _ = _check(pi)
        assert ("deferred USB work: chosen on" in out) is on, text
    for text, on in (("off", False), ("# x\n off \n", False), ("on", True), ("anything", True)):
        pi.w("etc/k1000/cpu-tuning", text)
        out, _ = _check(pi)
        assert (f"processor settings: chosen {'on' if on else 'off'}" in out), text


def test_check_counts_what_core_0_takes_over_the_sample(pi):
    second = {"11": [750, 650, 650, 650], "37": [32000, 0, 0, 0], "40": [30, 0, 0, 0]}
    lines, _ = kc.check(pi.box(NoChanges(pi)), sample=2, sleep=lambda s: pi.interrupts(second))
    last = lines[-1]
    assert last == "interrupts taken by core 0 in 2 s: 37 xhci_hcd 16000 · 11 arch_timer 250", last


def test_check_on_this_machines_own_proc_is_read_only_and_parses():
    """The real /proc of the machine running the tests (another kernel, other names): it must
    parse without a traceback. Nothing is written: check has no write path."""
    r = subprocess.run([sys.executable, str(TOOL), "check", "--sample", "0.2"], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode in (0, 1), r.stderr
    assert "Traceback" not in r.stderr
    for head in ("processor settings: chosen", "usb-cpu0, core 0 left", "usb-rt, real-time priority",
                 "interrupts taken by core 0 in 0.2 s"):
        assert head in r.stdout, r.stdout


def test_the_command_line(pi, tmp_path, monkeypatch, capsys):
    run = tmp_path / "rt"
    run.mkdir()
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(run))
    assert kc.main(["usb-cpu0", "apply"], box=pi.box()) == 0
    assert (run / "report").exists()
    assert "k1000-cpu usb-cpu0 apply: interrupts moved to core 1: 40 mmc0,mmc1 · 41 eth0 · 42 eth0" in capsys.readouterr().out
    assert kc.main(["usb-rt", "apply"], box=pi.box()) == 0
    assert kc.main(["usb-cpu0", "undo"], box=pi.box()) == 0 and not (run / "report").exists()
    with pytest.raises(SystemExit):
        kc.main(["usb-rt", "off"], box=pi.box())
    r = subprocess.run([sys.executable, str(TOOL), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "usb-cpu0 apply" in r.stdout and "check" in r.stdout


# --------------------------------------------------------------------------- the units
def _unit(name: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    sec = ""
    for line in (CPU_DIR / name).read_text().splitlines():
        if line.startswith("["):
            sec = line.strip("[]")
        elif "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out.setdefault(f"{sec}.{k}", []).append(v)
    return out


@pytest.mark.parametrize("switch", ["usb-cpu0", "usb-rt"])
def test_the_units_run_the_tool_and_undo_on_any_stop(switch):
    u = _unit(f"k1000-{switch}.service")
    tool = "/usr/bin/python3 /opt/k1000/tools/k1000-cpu.py"
    assert u["Service.Type"] == ["oneshot"] and u["Service.RemainAfterExit"] == ["yes"]
    assert u["Service.ExecStart"] == [f"{tool} {switch} apply"]
    # ExecStopPost, not ExecStop: it also runs after a failed start, so a half-applied switch is undone
    assert u["Service.ExecStopPost"] == [f"{tool} {switch} undo"] and "Service.ExecStop" not in u
    assert u["Install.WantedBy"] == ["multi-user.target"]          # at every boot, once enabled
    assert u["Service.CapabilityBoundingSet"] == ["CAP_SYS_NICE"]
    # these would forbid what the unit does: /proc/irq read-only, or the FIFO refused
    assert "Service.ProtectKernelTunables" not in u and "Service.RestrictRealtime" not in u
    assert "Service.ProcSubset" not in u and "Service.ProtectProc" not in u
    assert not [k for k in u if k.startswith("Unit.Before") or k.startswith("Unit.Requires")]   # audio never waits


def test_the_usb_cpu0_report_is_where_check_reads_it():
    u = _unit("k1000-usb-cpu0.service")
    assert f"/run/{u['Service.RuntimeDirectory'][0]}/report" == kc.REPORT
    assert u["Service.RuntimeDirectoryMode"] == ["0755"] and u["Service.UMask"] == ["0022"]   # check: no root


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
@pytest.mark.parametrize("switch", ["usb-cpu0", "usb-rt"])
def test_the_units_verify_with_systemd(tmp_path, switch):
    name = f"k1000-{switch}.service"
    (tmp_path / name).write_text((CPU_DIR / name).read_text())
    r = subprocess.run(["systemd-analyze", "verify", str(tmp_path / name)], capture_output=True, text=True)
    bad = [line for line in (r.stdout + r.stderr).splitlines() if name in line and "k1000-cpu.py" not in line]
    assert not bad, bad


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
@pytest.mark.parametrize("unit", ["k1000.service", "camillagui.service"])
def test_the_usb_cpu0_dropins_verify_after_the_processor_settings_ones(tmp_path, unit):
    """Each unit with both drop-ins, the way systemd finds them (51- read after 50-)."""
    (tmp_path / unit).write_text((BOX / "systemd" / unit).read_text())
    d = tmp_path / f"{unit}.d"
    d.mkdir()
    name = unit.removesuffix(".service")
    (d / "50-k1000-cpu.conf").write_text((CPU_DIR / f"{name}.conf").read_text())
    (d / "51-k1000-usb-cpu0.conf").write_text((CPU_DIR / f"usb-cpu0-{name}.conf").read_text())
    assert sorted(p.name for p in d.iterdir()) == ["50-k1000-cpu.conf", "51-k1000-usb-cpu0.conf"]
    r = subprocess.run(["systemd-analyze", "verify", str(tmp_path / unit)], capture_output=True, text=True)
    bad = [line for line in (r.stdout + r.stderr).splitlines() if "k1000-cpu.conf" in line or "usb-cpu0" in line]
    assert not bad, bad

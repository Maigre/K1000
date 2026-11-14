#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""k1000-cpu: the two bench switches against the USB stall (D-023 point 3), and their check.

Why: during the 96 kHz night of 13 to 14 Nov the delay in to out grew by up to 3 ms, twice, with
nothing in any log. Read in the kernel's source (D-023, docs/17 §10): the M4's playback follows
its capture stream, and when the handling of USB completions stalls for about 3 ms the playback
queue rises to its maximum and stays there. That handling runs on core 0, where the xHCI's
interrupt is taken and cannot be moved. Two system settings should keep it from stalling. Each is
a switch of install.sh, off by default, so that the bench can turn it on and off and measure it
(docs/manual/02-installation.md §6, "The bench switches"). Their effect is NOT measured yet.

Commands (apply and undo run as root, from their units; check needs no root and changes nothing):

  usb-cpu0 apply  k1000-usb-cpu0.service, "core 0 left to the USB handling": every interrupt the
                  kernel lets move goes to core 1, and new ones too; the kernel's unbound
                  workqueues leave core 0; every user-space thread allowed on core 0 goes to
                  core 1, except the engine's. Never the xHCI's interrupt. Writes what it did to
                  $RUNTIME_DIRECTORY/report (/run/k1000-usb-cpu0/report).
  usb-cpu0 undo   Interrupts and workqueues back on every core; each thread apply moved back on
                  the cores it had (the report names them), the threads systemd started meanwhile
                  on cores 0 and 1 (the processor settings' cores). A login session's commands
                  started meanwhile keep their cores (a reader the bench pinned to core 1 too).
  usb-rt apply    k1000-usb-rt.service, "real-time priority for the deferred USB work":
                  ksoftirqd/0, and the xHCI's interrupt thread if the kernel runs one (threadirqs),
                  at SCHED_FIFO 51. Refused, with nothing changed, while sched_rt_runtime_us lifts
                  the real-time limit (-1). On the box's kernel that knob caps no real-time thread
                  anyway (no RT group scheduling): docs/manual/02-installation.md §6 says what does.
  usb-rt undo     ksoftirqd/0 back to SCHED_OTHER, an xHCI thread back to FIFO 50.
  check           For the processor settings and each switch: what is chosen (/etc/k1000/) and
                  whether it is in force on the running system, part by part. Exit 0 when each
                  switch is as chosen, 1 when one is not. --sample S (default 2): also count the
                  interrupts core 0 takes in S seconds (0: skip).

On the box:

  /opt/k1000/tools/k1000-cpu.py check
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

USB_CPU = 0                             # the xHCI's interrupt is taken there and cannot move
USB_CPU0_CORES = frozenset({1})         # usb-cpu0: everything but the engine (cpu/usb-cpu0-*.conf)
BASE_CORES = frozenset({0, 1})          # the processor settings: the same (cpu/system.conf)
ENGINE_CORES = frozenset({2, 3})        # the engine (cpu/camilladsp.conf)
ENGINE_UNIT = "camilladsp.service"
USB_IRQ = re.compile(r"xhci")           # the USB host's interrupt, by its handler's name
XHCI_THREAD = re.compile(r"^irq/\d+-(s-)?xhci")   # its thread, with threadirqs (comm is 15 chars)
USB_RT_PRIO = 51                        # above the interrupt threads, far below migration/0 (99)
IRQ_THREAD_PRIO = 50                    # the kernel's priority for interrupt threads
SCHED_OTHER, SCHED_FIFO = 0, 1          # Linux's numbers (os.SCHED_* on Linux)
PF_KTHREAD = 0x00200000                 # /proc/<pid>/stat flags: a kernel thread
POLICY = {0: "SCHED_OTHER", 1: "SCHED_FIFO", 2: "SCHED_RR", 3: "SCHED_BATCH", 5: "SCHED_IDLE",
          6: "SCHED_DEADLINE"}

CPU_CHOICE = "/etc/k1000/cpu-tuning"    # install.sh --cpu-on/--cpu-off: absent or not "off" = on
USB_CPU0_CHOICE = "/etc/k1000/usb-cpu0"  # --usb-cpu0-on/-off: "on" = on, anything else = off
USB_RT_CHOICE = "/etc/k1000/usb-rt"      # --usb-rt-on/-off: the same
REPORT = "/run/k1000-usb-cpu0/report"    # k1000-usb-cpu0.service's RuntimeDirectory + "report"
WQ_MASK = "/sys/devices/virtual/workqueue/cpumask"
DEFAULT_IRQ_MASK = "/proc/irq/default_smp_affinity"


# --------------------------------------------------------------------------- cpu lists and masks
def parse_cpus(text: str | None) -> frozenset[int]:
    """'0-1,3' -> {0, 1, 3}."""
    out: set[int] = set()
    for part in (text or "").replace(",", " ").split():
        a, _, b = part.partition("-")
        out |= set(range(int(a), int(b or a) + 1))
    return frozenset(out)


def fmt_cpus(cpus) -> str:
    """{0, 1, 3} -> '0-1,3' (the kernel's own list format); empty -> '-'."""
    s = sorted(cpus)
    parts = []
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[j] + 1:
            j += 1
        parts.append(str(s[i]) if i == j else f"{s[i]}-{s[j]}")
        i = j + 1
    return ",".join(parts) or "-"


def parse_mask(text: str | None) -> frozenset[int]:
    """A hex CPU mask as /proc and /sys print it ('f', '00000002', 'ff,ffffffff')."""
    value = int((text or "0").strip().replace(",", "") or "0", 16)
    return frozenset(i for i in range(value.bit_length()) if value >> i & 1)


def fmt_mask(cpus) -> str:
    return format(sum(1 << c for c in cpus), "x")


# --------------------------------------------------------------------------- the system
class Kernel:
    """The three kinds of change, on the real kernel. Tests pass a fake with the same methods."""

    def write(self, path: Path, text: str) -> None:
        with open(path, "w") as f:      # one write(2): the kernel's answer raises here
            f.write(text)

    def set_affinity(self, tid: int, cpus) -> None:
        os.sched_setaffinity(tid, cpus)

    def set_sched(self, tid: int, policy: int, prio: int) -> None:
        os.sched_setscheduler(tid, policy, os.sched_param(prio))


class Box:
    """The running system, as files under `root` ("/" on the box, a fake tree in the tests)."""

    def __init__(self, root: str | Path = "/", kernel: Kernel | None = None) -> None:
        self.root = Path(root)
        self.kernel = kernel or Kernel()

    def p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def read(self, path: str | Path) -> str | None:
        try:
            return (path if isinstance(path, Path) else self.p(path)).read_text()
        except OSError:
            return None

    def online(self) -> frozenset[int]:
        return parse_cpus(self.read("/sys/devices/system/cpu/online")) or frozenset({0})


@dataclass(frozen=True)
class Task:
    pid: int
    tid: int
    comm: str
    flags: int         # the kernel's task flags (stat field 9)
    cpus: frozenset
    policy: int
    prio: int
    unit: str          # the systemd unit its cgroup names ("" for none)
    start: int = 0     # when it started (stat field 22): a thread id reused later is another thread

    @property
    def kernel(self) -> bool:
        return bool(self.flags & PF_KTHREAD)

    def label(self) -> str:
        return f"{self.comm}({self.tid})"


def _unit_of(text: str | None) -> str:
    for line in (text or "").splitlines():
        for part in reversed(line.split(":", 2)[-1].split("/")):
            if part.endswith(".service"):
                return part
    return ""


def tasks(box: Box) -> list[Task]:
    """Every thread of every process. One that ends while it is read is left out."""
    out = []
    try:
        pids = [d for d in box.p("/proc").iterdir() if d.name.isdigit()]
    except OSError:
        return out
    for pdir in pids:
        unit = _unit_of(box.read(pdir / "cgroup"))
        try:
            tids = [t for t in (pdir / "task").iterdir() if t.name.isdigit()]
        except OSError:
            continue
        for t in tids:
            status, stat = box.read(t / "status"), box.read(t / "stat")
            if status is None or stat is None or ")" not in stat:
                continue
            f = {}
            for line in status.splitlines():
                k, sep, v = line.partition(":")
                if sep:
                    f[k] = v.strip()
            rest = stat[stat.rindex(")") + 2:].split()     # fields 3 on (the name may hold spaces)
            try:
                out.append(Task(int(pdir.name), int(t.name), f.get("Name", "?"), int(rest[6]),
                                parse_cpus(f.get("Cpus_allowed_list")), int(rest[38]), int(rest[37]), unit,
                                int(rest[19])))
            except (ValueError, IndexError):
                continue
    return sorted(out, key=lambda t: (t.pid, t.tid))


@dataclass(frozen=True)
class Irq:
    n: int
    names: tuple[str, ...]     # its handlers (one directory each in /proc/irq/<n>/); none if chained
    cpus: frozenset            # smp_affinity_list: where it may be taken

    def label(self) -> str:
        return f"{self.n} {','.join(self.names) or '(no handler)'}"


def irqs(box: Box) -> list[Irq]:
    out = []
    try:
        dirs = [d for d in box.p("/proc/irq").iterdir() if d.name.isdigit()]
    except OSError:
        return out
    for d in dirs:
        try:
            names = tuple(sorted(x.name for x in d.iterdir() if x.is_dir()))
        except OSError:
            continue
        out.append(Irq(int(d.name), names, parse_cpus(box.read(d / "smp_affinity_list"))))
    return sorted(out, key=lambda i: i.n)


def _usb_irq(irq: Irq) -> bool:
    return any(USB_IRQ.search(n) for n in irq.names)


def _short(items: list[str], n: int = 12) -> str:
    return " · ".join(items[:n]) + (f" · and {len(items) - n} more" if len(items) > n else "")


# --------------------------------------------------------------------------- usb-cpu0
def apply_usb_cpu0(box: Box, report_dir: str | None = None) -> tuple[list[str], int]:
    """Core 0 left to the USB handling. Returns (log lines, exit status)."""
    log, rc = [], 0
    target = USB_CPU0_CORES & box.online()
    if not target:
        return [f"core {fmt_cpus(USB_CPU0_CORES)} is not online: nothing changed"], 1
    record: dict = {"applied": time.strftime("%Y-%m-%dT%H:%M:%S"), "moved": [], "kept": [],
                    "refused": [], "elsewhere": [], "thread_cores": []}
    for irq in irqs(box):
        if not irq.names:            # no handler, or a demultiplexer's (the PCIe MSI's): left alone
            continue
        if _usb_irq(irq):
            record["kept"].append([irq.n, list(irq.names)])
            continue
        if USB_CPU not in irq.cpus:  # off core 0 already: undo leaves it where it is
            record["elsewhere"].append(irq.n)
            continue
        try:
            box.kernel.write(box.p(f"/proc/irq/{irq.n}/smp_affinity_list"), fmt_cpus(target) + "\n")
            record["moved"].append([irq.n, list(irq.names)])
        except OSError as e:
            record["refused"].append([irq.n, list(irq.names), e.strerror or str(e)])
    def lab(r: list) -> str:
        return f"{r[0]} {','.join(r[1])}"
    log.append(f"interrupts moved to core {fmt_cpus(target)}: {_short([lab(r) for r in record['moved']]) or 'none'}")
    if record["kept"]:
        log.append(f"kept on core {USB_CPU}: {_short([lab(r) for r in record['kept']])} (the USB host)")
    if record["refused"]:
        log.append(f"the kernel refused to move, stays on core {USB_CPU}: "
                   + _short([f"{lab(r)} ({r[2]})" for r in record["refused"]]))
    try:
        box.kernel.write(box.p(DEFAULT_IRQ_MASK), fmt_mask(target) + "\n")
        log.append(f"interrupts set up from now on: core {fmt_cpus(target)}")
    except OSError as e:
        log.append(f"ERROR: {DEFAULT_IRQ_MASK}: {e.strerror or e}")
        rc = 1
    wq = box.p(WQ_MASK)
    if wq.exists():
        cores = box.online() - {USB_CPU}
        try:
            box.kernel.write(wq, fmt_mask(cores) + "\n")
            log.append(f"kernel workqueues (unbound): cores {fmt_cpus(cores)}")
        except OSError as e:
            log.append(f"ERROR: {WQ_MASK}: {e.strerror or e}")
            rc = 1
    moved, failed, engine0 = 0, [], []
    for t in tasks(box):
        if t.kernel or USB_CPU not in t.cpus:
            continue
        if t.unit == ENGINE_UNIT:     # its cores are the processor settings' (2-3): never moved here
            engine0.append(t)
            continue
        try:
            box.kernel.set_affinity(t.tid, target)
            moved += 1
            record["thread_cores"].append([t.tid, t.start, fmt_cpus(t.cpus)])   # for undo
        except ProcessLookupError:
            pass
        except OSError as e:
            failed.append(f"{t.label()} ({e.strerror or e})")
    log.append(f"{moved} user-space threads moved to core {fmt_cpus(target)}; the engine's left alone")
    if failed:
        log.append("could not move: " + _short(failed))
    if engine0:
        log.append(f"WARNING: the engine is allowed on core {USB_CPU} ({len(engine0)} threads): the processor "
                   "settings reach it at its next start (between shows: sudo systemctl restart camilladsp)")
    record["threads"] = moved
    if report_dir:
        try:      # applied again without an undo between: what was off core 0, and each moved
            # thread's cores, are the first run's view (this run found those threads on core 1)
            first = json.loads(Path(report_dir, "report").read_text())
            record["elsewhere"] = first["elsewhere"]
            seen = {(r[0], r[1]) for r in first.get("thread_cores", [])}
            record["thread_cores"] = first.get("thread_cores", []) + [
                r for r in record["thread_cores"] if (r[0], r[1]) not in seen]
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            pass
        try:
            Path(report_dir, "report").write_text(json.dumps(record, indent=1) + "\n")
        except OSError as e:
            log.append(f"WARNING: cannot write {report_dir}/report: {e.strerror or e}")
    return log, rc


def undo_usb_cpu0(box: Box, report_dir: str | None = None) -> tuple[list[str], int]:
    """Back to what a boot without the switch gives. Every interrupt on core 1 alone goes back on
    every core (the ones apply moved, and the ones set up since), except those apply found off core
    0 already. Each thread apply moved, still on core 1 alone, gets back the cores it had; another
    thread on core 1 alone gets the processor settings' cores when systemd started it (PID 1, a
    service: it inherited core 1 from the switch), and is left alone otherwise (a login session's
    commands, such as a reader the bench pinned to core 1). Both lists come from apply's report;
    without one, only the second rule applies."""
    log, rc = [], 0
    on = box.online()
    target = USB_CPU0_CORES & on
    elsewhere: set[int] = set()
    had: dict[tuple[int, int], frozenset] = {}
    if report_dir:
        try:
            report = json.loads(Path(report_dir, "report").read_text())
            elsewhere = set(report.get("elsewhere", []))
            had = {(int(r[0]), int(r[1])): parse_cpus(r[2]) for r in report.get("thread_cores", [])}
        except (OSError, ValueError, AttributeError, TypeError, IndexError):
            pass
    back = []
    for irq in irqs(box):
        if not irq.names or _usb_irq(irq) or irq.cpus != target or irq.n in elsewhere:
            continue
        try:
            box.kernel.write(box.p(f"/proc/irq/{irq.n}/smp_affinity_list"), fmt_cpus(on) + "\n")
            back.append(irq.label())
        except OSError as e:
            log.append(f"WARNING: interrupt {irq.label()}: {e.strerror or e}")
    log.append(f"interrupts back on cores {fmt_cpus(on)}: {_short(back) or 'none'}")
    for path in (DEFAULT_IRQ_MASK, WQ_MASK):
        if path == WQ_MASK and not box.p(path).exists():
            continue
        try:
            box.kernel.write(box.p(path), fmt_mask(on) + "\n")
        except OSError as e:
            log.append(f"ERROR: {path}: {e.strerror or e}")
            rc = 1
    log.append(f"new interrupts and kernel workqueues: cores {fmt_cpus(on)}")
    base = BASE_CORES & on
    back_had, back_base, left = 0, 0, 0
    for t in tasks(box):
        if t.kernel or t.unit == ENGINE_UNIT or t.cpus != target:
            continue
        cores = had.get((t.tid, t.start))
        if cores is None and not (t.tid == 1 or t.unit.endswith(".service")):
            left += 1
            continue
        try:
            box.kernel.set_affinity(t.tid, base if cores is None else (cores & on) or base)
        except OSError:
            continue
        if cores is None:
            back_base += 1
        else:
            back_had += 1
    log.append(f"{back_had} user-space threads back on the cores they had, {back_base} started by systemd "
               f"meanwhile on cores {fmt_cpus(base)}"
               + (f"; {left} of login sessions left on core {fmt_cpus(target)}" if left else ""))
    if report_dir:
        Path(report_dir, "report").unlink(missing_ok=True)
    return log, rc


# --------------------------------------------------------------------------- usb-rt
def _rt_limit(box: Box) -> tuple[int | None, int | None]:
    """sched_rt_runtime_us and sched_rt_period_us. Since Linux 6.12 they cap real-time threads only
    on a kernel with RT group scheduling (kernel/sched/rt.c, update_curr_rt), which Raspberry Pi's
    bcm2711_defconfig does not set; normal tasks keep their share through the fair server instead
    (50 ms of each second of each core, kernel/sched/deadline.c). -1 still means: limits lifted."""
    def num(path):
        try:
            return int((box.read(path) or "").strip())
        except ValueError:
            return None
    return num("/proc/sys/kernel/sched_rt_runtime_us"), num("/proc/sys/kernel/sched_rt_period_us")


def _usb_threads(ts: list[Task]) -> tuple[list[Task], list[Task]]:
    soft = [t for t in ts if t.kernel and t.comm == f"ksoftirqd/{USB_CPU}"]
    xhci = [t for t in ts if t.kernel and XHCI_THREAD.match(t.comm)]
    return soft, xhci


def apply_usb_rt(box: Box) -> tuple[list[str], int]:
    runtime, period = _rt_limit(box)
    if runtime is None or period is None or not 0 <= runtime < period:
        return [f"refused, nothing changed: the real-time limit is lifted or unreadable "
                f"(sched_rt_runtime_us {runtime}, sched_rt_period_us {period}): K1000 raises no "
                f"thread on a system set up that way"], 1
    soft, xhci = _usb_threads(tasks(box))
    if not soft:
        return [f"ERROR: no ksoftirqd/{USB_CPU} thread found: nothing changed"], 1
    log, rc = [], 0
    for t in soft + xhci:
        try:
            box.kernel.set_sched(t.tid, SCHED_FIFO, USB_RT_PRIO)
            log.append(f"{t.label()}: SCHED_FIFO {USB_RT_PRIO} (was {POLICY.get(t.policy, t.policy)}"
                       + (f" {t.prio})" if t.prio else ")"))
        except OSError as e:
            log.append(f"ERROR: {t.label()}: {e.strerror or e}")
            rc = 1
    if not xhci:
        log.append("no xHCI interrupt thread: the kernel takes that interrupt directly (no threadirqs)")
    log.append(f"sched_rt_runtime_us: {runtime} of every {period} µs (a cap on real-time threads only "
               f"if the kernel has RT group scheduling)")
    return log, rc


def undo_usb_rt(box: Box) -> tuple[list[str], int]:
    soft, xhci = _usb_threads(tasks(box))
    log, rc = [], 0
    for t, policy, prio in [(t, SCHED_OTHER, 0) for t in soft] + [(t, SCHED_FIFO, IRQ_THREAD_PRIO) for t in xhci]:
        try:
            box.kernel.set_sched(t.tid, policy, prio)
            log.append(f"{t.label()}: {POLICY[policy]}" + (f" {prio}" if prio else "") + " (the kernel's default)")
        except OSError as e:
            log.append(f"ERROR: {t.label()}: {e.strerror or e}")
            rc = 1
    return log, rc


# --------------------------------------------------------------------------- check
def _choice(box: Box, path: str) -> str:
    """What install.sh reads: the lines that are not comments, all blanks removed."""
    text = box.read(path) or ""
    return "".join("".join(line.split()) for line in text.splitlines() if not line.lstrip().startswith("#"))


def read_interrupts(text: str | None) -> dict[str, tuple[list[int], str]]:
    """/proc/interrupts: label -> (counts per CPU, what it is)."""
    lines = (text or "").splitlines()
    if not lines:
        return {}
    ncpu = len(lines[0].split())
    out = {}
    for line in lines[1:]:
        label, sep, rest = line.partition(":")
        if not sep:
            continue
        words = rest.split()
        counts = []
        for w in words[:ncpu]:
            if not w.isdigit():
                break
            counts.append(int(w))
        what = " ".join(words[len(counts):])
        m = re.search(r"\b(?:Level|Edge)\s+(.*)$", what)
        out[label.strip()] = (counts, m.group(1) if m else what)
    return out


def _state(conds: list[tuple[str, bool]]) -> str:
    n = sum(ok for _, ok in conds)
    return "in force" if n == len(conds) else "not in force" if n == 0 else "PARTLY in force"


def check(box: Box, sample: float = 2.0, sleep=time.sleep) -> tuple[list[str], int]:
    out: list[str] = []
    on = box.online()
    ts = tasks(box)
    user = [t for t in ts if not t.kernel]
    base_on = _choice(box, CPU_CHOICE) != "off"
    chosen = {"usb-cpu0": base_on and _choice(box, USB_CPU0_CHOICE) == "on",
              "usb-rt": base_on and _choice(box, USB_RT_CHOICE) == "on"}

    # the processor settings (what both switches build on)
    govs = {}
    for c in sorted(on):
        govs.setdefault((box.read(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_governor") or "?").strip(),
                        set()).add(c)
    pid1 = next((t for t in user if t.tid == 1), None)
    units: dict[str, set] = {}
    for t in user:
        if t.unit.startswith(("k1000", "camilla")):
            units.setdefault(t.unit, set()).update(t.cpus)
    out.append(f"processor settings: chosen {'on' if base_on else 'off'} ({CPU_CHOICE})")
    out.append("  governor: " + " · ".join(f"{g} on cores {fmt_cpus(c)}" for g, c in govs.items()))
    out.append(f"  systemd (PID 1): cores {fmt_cpus(pid1.cpus) if pid1 else '?'}")
    out.append("  units: " + (" · ".join(f"{u.removesuffix('.service')} {fmt_cpus(c)}"
                                          for u, c in sorted(units.items(), key=lambda x: x[0].removesuffix(".service")))
                          or "none running"))

    rc = 0
    # usb-cpu0
    target = USB_CPU0_CORES & on
    report = None
    try:
        report = json.loads(box.read(REPORT) or "null")
    except ValueError:
        pass
    excused = {r[0] for r in (report or {}).get("kept", []) + (report or {}).get("refused", [])}
    moved = {r[0] for r in (report or {}).get("moved", [])}
    on0 = [i for i in irqs(box) if i.names and USB_CPU in i.cpus]
    back0 = [i for i in on0 if i.n in moved]               # moved by the unit, on core 0 again
    # on core 0 and in no report: set up after the unit ran, where the kernel cannot move it (the
    # default mask moves the others), or the switch is off
    other0 = [i for i in on0 if i.n not in excused | moved and not _usb_irq(i)]
    k1000_units = {u: c for u, c in units.items() if u != ENGINE_UNIT}
    threads0 = [t for t in user if USB_CPU in t.cpus]
    conds = [
        (f"applied by k1000-usb-cpu0.service" + (f" at {report.get('applied')}" if report else
                                                 f" (no {REPORT})"), report is not None),
        (f"systemd (PID 1) on core {fmt_cpus(target)}", bool(pid1) and pid1.cpus == target),
        (f"K1000 units on core {fmt_cpus(target)}", bool(k1000_units) and all(c == target for c in k1000_units.values())),
        (f"no user-space thread allowed on core {USB_CPU}" + (f" ({len(threads0)}: {_short([t.label() for t in threads0], 6)})"
                                                              if threads0 else ""), not threads0),
        (f"new interrupts on core {fmt_cpus(target)} (default_smp_affinity {(box.read(DEFAULT_IRQ_MASK) or '?').strip()})",
         parse_mask(box.read(DEFAULT_IRQ_MASK)) == target),
    ]
    if box.p(WQ_MASK).exists():
        wq = parse_mask(box.read(WQ_MASK))
        conds.append((f"kernel workqueues off core {USB_CPU} (cores {fmt_cpus(wq)})", bool(wq) and USB_CPU not in wq))
    conds.append((f"the interrupts it moved are off core {USB_CPU}" + (f" ({len(moved)})" if report else "")
                   + (f" (back on it: {_short([i.label() for i in back0], 8)})" if back0 else ""),
                   report is not None and not back0))
    st = _state(conds)
    want = chosen["usb-cpu0"]
    ok = st == ("in force" if want else "not in force")
    rc |= 0 if ok else 1
    out.append(f"usb-cpu0, core {USB_CPU} left to the USB handling: chosen {'on' if want else 'off'} "
               f"({USB_CPU0_CHOICE}), {st}" + ("" if ok else ": NOT AS CHOSEN"))
    out += [f"  [{'x' if c else ' '}] {label}" for label, c in conds]
    stays = [i.label() + (" (USB host)" if _usb_irq(i) else "") for i in on0 if i.n in excused or _usb_irq(i)]
    if stays:
        out.append(f"  stays on core {USB_CPU} (the kernel cannot move it): " + _short(stays))
    if other0:
        out.append(f"  {'also ' if stays else ''}on core {USB_CPU}" + (" (set up since; the kernel may refuse to move it)"
                                                                     if report else "") + ": "
                   + _short([i.label() for i in other0], 8))
    ithreads = [f"{t.comm} {fmt_cpus(t.cpus)}" for t in ts if t.kernel and t.comm.startswith("irq/")]
    if ithreads:
        out.append("  interrupt threads, cores: " + _short(ithreads))

    # usb-rt
    soft, xhci = _usb_threads(ts)
    sched = lambda t: POLICY.get(t.policy, str(t.policy)) + (f" {t.prio}" if t.prio else "")   # noqa: E731
    conds = [(f"ksoftirqd/{USB_CPU} at SCHED_FIFO {USB_RT_PRIO} (now {', '.join(sched(t) for t in soft) or 'not found'})",
              bool(soft) and all((t.policy, t.prio) == (SCHED_FIFO, USB_RT_PRIO) for t in soft))]
    if xhci:
        conds.append((f"xHCI interrupt threads at SCHED_FIFO {USB_RT_PRIO} (now {', '.join(t.comm + ' ' + sched(t) for t in xhci)})",
                      all((t.policy, t.prio) == (SCHED_FIFO, USB_RT_PRIO) for t in xhci)))
    st = _state(conds)
    want = chosen["usb-rt"]
    ok = st == ("in force" if want else "not in force")
    rc |= 0 if ok else 1
    out.append(f"usb-rt, real-time priority for the deferred USB work: chosen {'on' if want else 'off'} "
               f"({USB_RT_CHOICE}), {st}" + ("" if ok else ": NOT AS CHOSEN"))
    out += [f"  [{'x' if c else ' '}] {label}" for label, c in conds]
    if not xhci:
        out.append("  no xHCI interrupt thread (no threadirqs on the kernel command line)")
    rt0 = sorted((t for t in ts if t.policy in (1, 2) and USB_CPU in t.cpus), key=lambda t: -t.prio)
    out.append(f"  real-time threads allowed on core {USB_CPU}: " + (_short([f"{t.comm} {sched(t)}" for t in rt0]) or "none"))
    runtime, period = _rt_limit(box)
    out.append(f"  sched_rt_runtime_us: {runtime} of every {period} µs (a cap on real-time threads only "
               f"if the kernel has RT group scheduling)")

    # what core 0 takes
    if sample > 0:
        before = read_interrupts(box.read("/proc/interrupts"))
        sleep(sample)
        after = read_interrupts(box.read("/proc/interrupts"))
        seen = []
        for label, (counts, what) in after.items():
            prev = before.get(label, ([], ""))[0]
            if counts and prev and counts[0] > prev[0]:
                seen.append((counts[0] - prev[0], f"{label} {what}"))
        seen.sort(key=lambda x: -x[0])
        out.append(f"interrupts taken by core {USB_CPU} in {sample:g} s: "
                   + (_short([f"{w} {n}" for n, w in seen], 14) or "none"))
    return out, rc


# --------------------------------------------------------------------------- command line
def main(argv: list[str] | None = None, box: Box | None = None) -> int:
    ap = argparse.ArgumentParser(prog="k1000-cpu.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 2)[2])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("usb-cpu0", "usb-rt"):
        s = sub.add_parser(name)
        s.add_argument("action", choices=("apply", "undo"))
    c = sub.add_parser("check")
    c.add_argument("--sample", type=float, default=2.0, metavar="S")
    a = ap.parse_args(argv)
    box = box or Box()
    if a.cmd == "check":
        lines, rc = check(box, a.sample)
        print("\n".join(lines))
        return rc
    fn = {("usb-cpu0", "apply"): apply_usb_cpu0, ("usb-cpu0", "undo"): undo_usb_cpu0,
          ("usb-rt", "apply"): apply_usb_rt, ("usb-rt", "undo"): undo_usb_rt}[(a.cmd, a.action)]
    if a.cmd == "usb-cpu0":
        lines, rc = fn(box, os.environ.get("RUNTIME_DIRECTORY"))
    else:
        lines, rc = fn(box)
    for line in lines:
        print(f"k1000-cpu {a.cmd} {a.action}: {line}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

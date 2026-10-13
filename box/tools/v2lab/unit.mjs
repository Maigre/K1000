// SPDX-License-Identifier: GPL-3.0-or-later
/* Unit checks of the v2 page's pure modules (fields.js, util.js, rta.js's decoder is DOM-free
   too but imports the store; it is covered by e2e.mjs). Node only, no browser:
     node box/tools/v2lab/unit.mjs [fixtures.json]
   Prints one JSON object {ok, checks:[{name, ok, detail}]}; exit 1 on a failure. */

import { Bucket, Guard, NudgeQueue, generic, speedMult, stepLabel, stepValue } from "../../k1000/web/static/v2/js/fields.js";
import { delaySub, fmtDb, fmtHz, fmtSigned, mmss } from "../../k1000/web/static/v2/js/util.js";
import { loadFixtures } from "./fakebox.mjs";

const fx = loadFixtures(process.argv[2]);
const F = fx.hello.fields, SL = fx.hello.slide;
const checks = [];
const check = (name, ok, detail = "") => checks.push({ name, ok: !!ok, detail: ok ? "" : String(detail) });
const near = (a, b, e = 1e-6) => Math.abs(a - b) <= e;

// the box's arithmetic (state.py Range.nudge, nudge_align_delay)
check("generic paths", generic("live_peq.2.gain") === "live_peq.gain" && generic("out_trim_db.1") === "out_trim_db" && generic("master_db") === "master_db");
check("master +1 = 0.5 dB, clamped at 0", stepValue(F.master_db, -14, 1, false, "master_db") === -13.5 && stepValue(F.master_db, -0.5, 2, false, "master_db") === 0);
check("freq is log: +12 detents = one octave", near(stepValue(F["live_peq.freq"], 1000, 12, false, "live_peq.0.freq"), 2000, 0.01));
check("freq fine: +48 fine detents = one octave", near(stepValue(F["live_peq.freq"], 1000, 48, true, "live_peq.0.freq"), 2000, 0.01));
check("lowcut snaps on its grid (112.25 up -> 114, down -> 112)", stepValue(F.lowcut_hz, 112.25, 1, false, "lowcut_hz") === 114 && stepValue(F.lowcut_hz, 112.25, -1, false, "lowcut_hz") === 112);
check("lowcut fine = 1 Hz", stepValue(F.lowcut_hz, 112, 1, true, "lowcut_hz") === 113);
check("delay stops at 0 (ALIGNED) before crossing", stepValue(F.align_delay_ms, 0.05, -1, false, "align_delay_ms") === 0 && stepValue(F.align_delay_ms, 0, -1, false, "align_delay_ms") === -0.1);
check("delay fine = one sample", near(stepValue(F.align_delay_ms, 0, 1, true, "align_delay_ms"), 1000 / 48000, 1e-6));
check("gain clamps at the box's max", stepValue(F["live_peq.gain"], 5.5, 4, false, "live_peq.0.gain") === F["live_peq.gain"].max);

// the slide's speed mapping (docs/14 §3.1.2)
check("slow = fine", speedMult(50, SL, 4) === 0);
check("100..168 px/s = one normal detent", speedMult(120, SL, 4) === 1 && speedMult(168, SL, 4) === 1);
check("fast = the cap", speedMult(600, SL, 4) === 4 && speedMult(600, SL, 1) === 1);
check("in between = the box's accel_multiplier", speedMult(364, SL, 4) === 1 + Math.floor(3 * (364 - 168) / (560 - 168)));

// stepper labels from hello.fields[k].stepper
const lab = (k, d, o) => stepLabel(F[k], d, o).join(" ").trim();
check("master keys", F.master_db.stepper.map((d) => lab("master_db", d)).join("|") === "−6|−1|−½|+½|+1", F.master_db.stepper.map((d) => lab("master_db", d)));
check("freq keys", F["live_peq.freq"].stepper.map((d) => lab("live_peq.freq", d)).join("|") === "−⅓ oct|−1/12|+1/12|+⅓ oct");
check("delay keys", F.align_delay_ms.stepper.map((d) => lab("align_delay_ms", d)).join("|") === "−1 ms|−0.1|+0.1|+1 ms");
check("lowcut keys", F.lowcut_hz.stepper.map((d) => lab("lowcut_hz", d)).join("|") === "−10|−2|+2|+10");
check("Q keys: wider / narrower; a shelf: softer / steeper", lab("live_peq.q", -2, { q: true }) === "−2 wider" && lab("live_lowshelf.q", 1, { q: true, shelf: true }) === "+ steeper");
check("Q keys: a key that halves / doubles Q reads ×½ / ×2 (a box stepper of ±6 ⅙ oct, DESIGN §5.4)",
  lab("live_peq.q", -6, { q: true }) === "×½ wider" && lab("live_peq.q", 6, { q: true }) === "×2 narrower" && lab("live_highshelf.q", -6, { q: true, shelf: true }) === "×½ softer",
  [lab("live_peq.q", -6, { q: true }), lab("live_peq.q", 6, { q: true })]);
check("every stepper key is inside the caps", Object.entries(F).every(([, e]) => e.stepper.every((d) => d >= e.caps[0] && d <= e.caps[1])));

// the PA-up mirror: 6 dB/s, burst 3; the guard 6 dB in 2 s
let t = 0;
const clk = () => t;
const bk = new Bucket(F.master_db.up_limit.db_per_s, F.master_db.up_limit.burst_db, clk);
let n = 0;
for (let i = 0; i < 20; i++) if (bk.take(0.5)) n++;
t = 500;
let n2 = 0;
for (let i = 0; i < 20; i++) if (bk.take(0.5)) n2++;
check("bucket: burst 3 dB, then 6 dB/s", n === 6 && n2 === 6, [n, n2]);
const gd = new Guard(6, 2, clk);
t = 0;
let up = 0;
for (let i = 0; i < 20; i++) if (gd.room(0.5)) { gd.spend(0.5); up += 0.5; }
t = 2001;
check("guard: 6 dB within 2 s, then room again", up === 6 && gd.room(0.5), up);

// batches: one direction, one fine flag, sealed at a sticky stop, split at the caps
const q = new NudgeQueue();
q.add("master_db", -1, false); q.add("master_db", -1, false); q.add("master_db", 1, false);
const a = q.take("master_db", [-12, 2]), b = q.take("master_db", [-12, 2]);
check("a reversal is a new batch", a.d === -2 && b.d === 1 && !q.has("master_db"), JSON.stringify([a, b]));
q.add("x", 30, false);
const c1 = q.take("x", [-24, 24]), c2 = q.take("x", [-24, 24]);
check("split at the caps", c1.d === 24 && c2.d === 6);
q.add("y", 1, false); q.seal("y"); q.add("y", 1, false);
check("a sealed batch is not merged", q.take("y").d === 1 && q.take("y").d === 1);
q.add("z", 1, true); q.add("z", 1, false);
check("fine and normal never share a message", q.take("z").fine === true && q.take("z").fine === false);

// formatting
check("fmtDb", fmtDb(-6) === "−6.0" && fmtDb(0.04) === "0.0" && fmtDb(1.5) === "+1.5");
check("fmtHz", fmtHz(2440) === "2.44k" && fmtHz(12500) === "12.5k" && fmtHz(112) === "112");
check("fmtSigned", fmtSigned(0.5) === "+½" && fmtSigned(-3) === "−3" && fmtSigned(1.5) === "+1½");
check("delay words", delaySub(1.25, true) === "subs later" && delaySub(-1, false).startsWith("TOPS LATER") && delaySub(0, false) === "ALIGNED");
check("mmss", mmss(299.2) === "5:00" && mmss(7140) === "1:59:00");

const ok = checks.every((c) => c.ok);
console.log(JSON.stringify({ ok, checks }));
process.exit(ok ? 0 : 1);

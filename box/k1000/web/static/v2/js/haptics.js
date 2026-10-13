// SPDX-License-Identifier: GPL-3.0-or-later
/* Haptics (DESIGN §5.5, BRIDGE.md "Haptics"). In the APK: the phone's own effects through the
   bridge. In Chrome: navigator.vibrate. THIS PHONE › Haptics (off, light, full) only makes the
   page send less; the box's answers (ack / no answer) play at every level.

   kind      APK         Chrome           when
   tick      tick        8 ms, full only  a detent sent
   click     click       20               a landmark or sticky stop, band IN/OUT, FLAT
   reject    reject      15-40-15         range end, guard, refusal
   arm       long_press  30               a hold arms
   ok        confirm     40               the box acked a grab, a choice, an action
   unmuted   confirm     150              the box acked an unmute
   muted     confirm2    80-60-80         the box acked a mute
   elsewhere confirm2    60-60-60-60-60   a mute made elsewhere (B4, another phone)
   fail      reject3     3 x 100          no answer, link lost */

import { UI } from "./store.js";
import { can, nativeSend } from "./native.js";
import { now } from "./util.js";

const MAP = {
  tick: ["tick", 8], click: ["click", 20], reject: ["reject", [15, 40, 15]], arm: ["long_press", 30],
  ok: ["confirm", 40], unmuted: ["confirm", 150], muted: ["confirm2", [80, 60, 80]],
  elsewhere: ["confirm2", [60, 60, 60, 60, 60]], fail: ["reject3", [100, 100, 100, 100, 100]],
};
const ANSWERS = new Set(["ok", "unmuted", "muted", "elsewhere", "fail"]);
export const hapLog = [];
let lastTick = 0;

export function hap(kind) {
  const m = MAP[kind];
  if (!m) return;
  const level = UI.prefs.haptics;
  if (!ANSWERS.has(kind)) {
    if (level === "off") return;
    if (level === "light" && kind === "tick") return;
  }
  if (kind === "tick") {
    const t = now();
    if (t - lastTick < 40) return;
    lastTick = t;
  }
  hapLog.push(kind);
  if (hapLog.length > 50) hapLog.shift();
  if (can("haptic")) { nativeSend({ t: "haptic", kind: m[0] }); return; }
  if (kind === "tick" && level !== "full") return;
  try { if (navigator.vibrate) navigator.vibrate(m[1]); } catch (e) { /* no vibrator yet */ }
}

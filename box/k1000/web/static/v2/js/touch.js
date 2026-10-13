// SPDX-License-Identifier: GPL-3.0-or-later
/* Fingers on the glass, for every gesture: a second finger stops what the first one was doing
   (sending stops; what was already sent stays, DESIGN §5.3). */

export const ptrs = new Set();
const stoppers = [];
export function onSecondFinger(fn) { stoppers.push(fn); }
window.addEventListener("pointerdown", (e) => {
  if (e.isPrimary) ptrs.clear();
  ptrs.add(e.pointerId);
  if (ptrs.size > 1) for (const f of stoppers) f();
}, true);
const gone = (e) => { ptrs.delete(e.pointerId); };
window.addEventListener("pointerup", gone, true);
window.addEventListener("pointercancel", gone, true);

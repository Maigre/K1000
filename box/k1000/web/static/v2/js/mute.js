// SPDX-License-Identifier: GPL-3.0-or-later
/* MUTE: v1's protocol, unchanged on the wire (docs/14 §5; v1 app.js holdUnmutes / sendMute /
   muteDone), on a 115×76 key (DESIGN §5.2):
   - the intent is fixed at touch-down: want = !holdUnmutes(), seq = state.mute_seq;
   - hold 0.6 s to mute, 1.2 s to unmute; an unmute hold is cancelled the moment the box's mute
     changes (holdUnmutes() turns false, or mute_seq moves): "look, then hold again";
   - 12 px of travel or leaving the key cancels silently: a swipe never mutes;
   - {t:"mute", on, id:"mute-<n>"} (+ seq when unmuting), never rate-limited by the page;
     confirmed only by the box's ack OF THAT PRESS (each press has its own id: a late ack of an
     earlier, timed-out press never confirms this one); no ack in 1.5 s: said, with the real
     fallback from the box;
   - MUTE takes over from any other hold (a chip, ▼ PEAK, FLAT, a 1 s SETUP hold): that one is
     cancelled silently, and a second finger never stops MUTE's own hold (hold.js `preempt`);
   - no hold starts without a link or while a mute is pending; a hold that outlives the link
     sends nothing and says so. On a stalled link (store.linkSlow) a MUTE still goes out (late
     is the safe direction), an unmute does not. The box never mutes because a phone left. */

import { K, fallback, holdUnmutes, linkSlow, linked, render } from "./store.js";
import { bindHold, earlyFlash } from "./hold.js";
import { on, request } from "./link.js";
import { hap } from "./haptics.js";
import { muteWho, toast } from "./view.js";
import { $, setText } from "./util.js";

export const MUTE_MS = 600, UNMUTE_MS = 1200, ACK_MS = 1500;
let pend = null;                          // {want}: sent, the box's answer not seen yet
let pressN = 0;
export const mutePending = () => !!pend;

function sendMute(c) {
  // the link is checked again when the hold completes: a hold that outlived it sends nothing
  if (!linked()) { toast("LINK LOST · nothing sent · " + fallback(), "red", 3000); hap("fail"); render(); return; }
  if (!c.want && (!holdUnmutes() || K.state.mute_seq !== c.seq)) {
    toast("The mute changed meanwhile: look, then hold again", "red", 2600); hap("reject"); return;
  }
  if (!c.want && linkSlow()) {
    toast("LINK SLOW · unmute not sent · look, then hold again", "red", 3000); hap("fail"); render(); return;
  }
  const msg = { t: "mute", on: c.want, id: "mute-" + (++pressN) };
  if (!c.want) msg.seq = c.seq;
  pend = { want: c.want };
  const id = request(msg, (ok, r) => done(ok, r), ACK_MS);
  if (id == null) {
    pend = null;
    toast("Not connected: " + fallback(), "red", 3000); hap("fail");
  }
  render();
}
function done(ok, r) {
  const p = pend;
  pend = null;
  if (ok) {
    hap(r.v ? "muted" : "unmuted");
  } else if (r.code === "TIMEOUT") {
    toast("No answer from K1000: " + fallback(), "red", 3000); hap("fail");
  } else if (r.code === "LINK") {
    toast("Link lost: " + fallback(), "red", 3000); hap("fail");
  } else {
    toast(String(r.why || "refused"), "red", 3000); hap("fail");
  }
  if (!p) return;
  render();
}

export function muteMode() {
  if (!linked()) return "nolink";
  if (pend) return "pend";
  if (holdUnmutes()) return "muted";
  if (K.state.muted) return "keep";            // K1000 itself holds Main muted (boot ramp, setup guard)
  return "live";
}
export function renderMute() {
  const el = $("#mute");
  if (el._early != null) return;               // "hold" is showing
  const m = muteMode();
  el.className = "mute" + (m === "muted" ? " muted" : m === "keep" ? " keep" : m === "nolink" ? " nolink" : m === "pend" ? " pend" + (K.state.muted ? " muted" : "") : "");
  setText(el.querySelector("span"), m === "pend" ? (pend.want ? "MUTING…" : "UNMUTING…") : m === "muted" ? "MUTED" : m === "keep" ? "KEEP MUTED" : m === "nolink" ? "NO LINK" : "MUTE");
  el.setAttribute("aria-label", m === "muted" ? "Muted. Hold 1.2 s to unmute" : m === "nolink" ? "No link: this key cannot mute" : "Hold 0.6 s to mute");
}

export function initMute() {
  const el = $("#mute");
  bindHold(el, {
    preempt: true,
    begin: () => {
      const m = muteMode();
      if (m === "nolink") { toast("NO LINK · this key cannot mute · " + fallback(), "red", 2600); hap("fail"); return null; }
      if (m === "pend") return null;
      const want = !holdUnmutes(), seq = K.state.mute_seq;
      if (want) return { ms: MUTE_MS, want, seq };
      return { ms: UNMUTE_MS, want, seq, why: "The mute changed meanwhile: look, then hold again",
        valid: () => holdUnmutes() && K.state.mute_seq === seq };     // a link loss: dropHolds() says it
    },
    fire: (c) => sendMute(c),
    early: () => earlyFlash(el, "hold", 650, render),
    lost: () => { toast("LINK LOST · nothing sent · " + fallback(), "red", 3000); hap("fail"); },
  });
}

// A mute made elsewhere (B4, another phone, the console) is announced (DESIGN §9.9)
let lastMuted = null;
on("state", (S) => {
  const m = S.muted === true && holdUnmutes();
  if (lastMuted === false && m && !pend) {
    const me = K.hello ? "phone " + K.hello.you : "";
    const src = String(S.mute_src || "");
    if (src && src !== me) {
      toast("MUTED " + muteWho(), "red", 2600);
      hap("elsewhere");
    }
  }
  lastMuted = m;
});
on("lost", () => { lastMuted = null; });

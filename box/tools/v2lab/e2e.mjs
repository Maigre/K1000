// SPDX-License-Identifier: GPL-3.0-or-later
/* The v2 page in headless Chromium against the fake box (fakebox.mjs): fit, gestures, the mute,
   two fingers, holds whose key leaves the page, choose-then-set, the ALIGN lock, the notch
   helper, a stalled link, link loss, odd hello / state shapes, the box-update reload, the
   spectrum subscription, the APK bridge, the phone mic, MEASURE and RESPONSE (full Chromium with
   a fake mic playing a WAV made here: wav.mjs), and no console error anywhere.
     K1000_PLAYWRIGHT=/path/to/node_modules/playwright node box/tools/v2lab/e2e.mjs [fixtures.json] [--only=name,...] [--shots=dir]
   Prints one JSON object {ok, checks:[{name, ok, detail}]} on stdout; exit 1 on a failure. */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { FakeBox, center, clone, finger, hands, loadFixtures, loadPlaywright, showState, sleep, startStatic } from "./fakebox.mjs";
import { makeSignal, writeWav } from "./wav.mjs";
import { stepValue } from "../../k1000/web/static/v2/js/fields.js";

const args = process.argv.slice(2);
const fx = loadFixtures(args.find((a) => !a.startsWith("--")));
const only = (args.find((a) => a.startsWith("--only=")) || "").slice(7).split(",").filter(Boolean);
const shots = (args.find((a) => a.startsWith("--shots=")) || "").slice(8);
if (shots) fs.mkdirSync(shots, { recursive: true });
const { chromium } = loadPlaywright();

const checks = [];
function check(name, ok, detail = "") {
  checks.push({ name, ok: !!ok, detail: ok ? "" : typeof detail === "string" ? detail : JSON.stringify(detail) });
  if (process.env.K1000_V2_VERBOSE) process.stderr.write((ok ? "PASS " : "FAIL ") + name + (ok ? "" : " — " + JSON.stringify(detail)) + "\n");
}
const consoleErrors = [];
const UA = "Mozilla/5.0 (Linux; Android 13; Pixel 4a) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36";

const web = await startStatic();
const browser = await chromium.launch();

// opts.browser: another browser (the fake-mic ones); opts.permPrompt: the Permissions API says
// "prompt" for the mic (as a phone that never allowed it, or the APK's WebView); opts.pgmWav: the
// fake box's program for RESPONSE; opts.pgm: null = an older box (no hello.pgm). Every
// getUserMedia call and its stream are recorded (window.__gum, window.__streams), and every
// network request (t.reqs).
async function open(opts = {}) {
  const vp = opts.vp || { width: 393, height: 851 };
  const ctx = await (opts.browser || browser).newContext({ viewport: vp, deviceScaleFactor: 2.75, isMobile: true, hasTouch: true, userAgent: UA, serviceWorkers: "block" });
  if (opts.prefs) await ctx.addInitScript((p) => { try { localStorage.setItem("k1000v2.prefs", JSON.stringify(p)); } catch (e) { /* fine */ } }, opts.prefs);
  if (opts.permPrompt) {
    await ctx.addInitScript(() => {
      const q = navigator.permissions.query.bind(navigator.permissions);
      navigator.permissions.query = (d) => (d && d.name === "microphone" ? Promise.resolve({ state: "prompt", onchange: null }) : q(d));
    });
  }
  await ctx.addInitScript(() => {
    window.__gum = []; window.__streams = [];
    const md = navigator.mediaDevices;
    if (md && md.getUserMedia) {
      const orig = md.getUserMedia.bind(md);
      md.getUserMedia = (c) => {
        window.__gum.push({ at: performance.now(), c: JSON.stringify(c) });
        return orig(c).then((st) => { window.__streams.push(st); return st; });
      };
    }
  });
  const f2 = Object.assign({}, fx, { state: opts.state || showState(fx) });
  if (opts.hello) f2.hello = opts.hello(clone(fx.hello));
  const box = new FakeBox(f2, { stepValue, pgmWav: opts.pgmWav, pgm: opts.pgm });
  await box.attach(ctx);
  const page = await ctx.newPage();
  const reqs = [];
  page.on("request", (r) => reqs.push(r.method() + " " + r.url()));
  // opts.clock: the page's clock is Playwright's, paused: a slide's speed is then exact (headless
  // Chromium delivers touch moves in uneven bursts), and t.tick(ms) moves time
  if (opts.clock) await page.clock.install();
  const tag = opts.tag || "page";
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(tag + ": " + m.text()); });
  page.on("pageerror", (e) => consoleErrors.push(tag + ": pageerror " + e.message));
  await page.goto(web.url + "/v2/");
  await page.waitForFunction(() => window.K1000v2 && window.K1000v2.linked(), null, { timeout: 5000 });
  await sleep(150);
  if (opts.clock) await page.clock.pauseAt(Date.now() + 1000);
  const f = await finger(page);
  if (opts.hands) Object.assign(f, { two: await hands(page) });
  const tick = opts.clock ? async (ms) => { await page.clock.runFor(Math.max(1, Math.round(ms))); await sleep(5); } : (ms) => sleep(ms);
  return { ctx, page, box, f, tick, reqs, clock: !!opts.clock, close: async () => { box.stop(); await ctx.close(); } };
}
const ui = (page) => page.evaluate(() => K1000v2.ui());
const toasts = (page) => page.evaluate(() => K1000v2.toasts.slice());
const haps = (page) => page.evaluate(() => K1000v2.hapLog.slice());
const sum = (arr) => arr.reduce((a, m) => a + m.d, 0);
async function shot(page, name) { if (shots) await page.screenshot({ path: path.join(shots, name + ".png") }); }
async function run(name, fn) {
  if (only.length && !only.some((o) => name.startsWith(o))) return;
  try { await fn(); } catch (e) { check(name + " (ran)", false, String(e && e.stack || e)); }
}

// ================================================================== fit
const MEASURE = () => {
  const vw = innerWidth, vh = innerHeight, de = document.documentElement;
  const app = document.getElementById("app");
  const rect = (el) => el.getBoundingClientRect();
  const shown = (el) => {
    const r = rect(el);
    if (r.width < 1 || r.height < 1) return false;
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.display === "none" || cs.visibility === "hidden" || e.hidden) return false;
    }
    return true;
  };
  const onTop = (el) => {
    const r = rect(el), x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x > vw || y > vh) return false;
    const t = document.elementFromPoint(x, y);
    return !!t && (t === el || el.contains(t) || t.contains(el));
  };
  const name = (el) => (el.id ? "#" + el.id : el.className ? el.tagName.toLowerCase() + "." + String(el.className).split(" ")[0] : el.tagName.toLowerCase()) + ":" + (el.textContent || "").trim().slice(0, 14);
  const rows = ["#strip", "#graph", "#slot", "#tiles", "#alignRow", "#foot"].map((s) => [s, rect(document.querySelector(s))]);
  const outside = rows.filter(([, r]) => r.top < -0.5 || r.bottom > vh + 0.5).map(([s, r]) => s + " " + Math.round(r.top) + ".." + Math.round(r.bottom));
  const small = [];
  for (const el of document.querySelectorAll("button, [role=button], a[href], .tile")) {
    // the one named exception: the alert line is a full-width 28 px bar (DESIGN §5.1) that only
    // repeats the 44 px STATUS key of the status strip above it
    if (el.id === "alertLine" || !shown(el) || !onTop(el)) continue;
    const r = rect(el);
    if (r.height < 43.5 || r.width < 43.5) small.push(name(el) + " " + Math.round(r.width) + "x" + Math.round(r.height));
  }
  const clipped = [], ellipsis = [];
  const walker = document.createTreeWalker(app, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    if (!el || !shown(el)) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    const tr = range.getBoundingClientRect();
    if (tr.width < 0.5) continue;
    // clipped = cut sideways by a box that hides its overflow (1 px), or cut by more than 30 % of
    // its height (a glyph box is taller than its line box); content below a scroller's fold is
    // not clipped, it scrolls
    let scroller = false;
    for (let a = el; a && a !== document.body; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.overflowX === "visible" && cs.overflowY === "visible") continue;
      const ar = rect(a);
      scroller = scroller || cs.overflowY === "auto" || cs.overflowY === "scroll";
      const side = tr.left < ar.left - 1 || tr.right > ar.right + 1;
      const cutY = Math.max(0, ar.top - tr.top) + Math.max(0, tr.bottom - ar.bottom);
      if (side || (!scroller && cutY > 0.3 * tr.height)) {
        // the box's alerts may end in an ellipsis on the alert line (STATUS has them whole); the
        // page's own (data-own) are written to fit, so a cut one fails
        const al = el.closest(".alert");
        (al && !al.dataset.own ? ellipsis : clipped).push(name(el) + " in " + name(a) + (side ? " (side " + Math.round(tr.width) + ">" + Math.round(ar.width) + ")" : " (cut " + Math.round(cutY) + ")"));
        break;
      }
    }
  }
  const g = rect(document.getElementById("graph"));
  return { scrollH: de.scrollHeight - vh, scrollW: de.scrollWidth - vw, outside, small, clipped, ellipsis, graphH: Math.round(g.height),
    footBottom: Math.round(rect(document.getElementById("foot")).bottom), vh };
};
async function fitCheck(page, label) {
  const m = await page.evaluate(MEASURE);
  check(`fit ${label}: no overflow`, m.scrollH <= 0 && m.scrollW <= 0 && !m.outside.length && Math.abs(m.footBottom - m.vh) <= 1, m);
  check(`fit ${label}: targets >= 44 px`, !m.small.length, m.small);
  check(`fit ${label}: no clipped text`, !m.clipped.length, m.clipped);
  return m;
}
async function tapSel(t, page, sel) { const c = await center(page, sel); await t.f.tap(c.x, c.y); await sleep(120); }

await run("fit", async () => {
  for (const vp of [{ width: 393, height: 851 }, { width: 393, height: 740 }, { width: 393, height: 600 }, { width: 360, height: 640 }, { width: 393, height: 544 }]) {
    const L = vp.width + "x" + vp.height;
    const t = await open({ vp, tag: "fit " + L });
    const { page, box } = t;
    const m = await fitCheck(page, L + " LIVE");
    check(`fit ${L}: the graph keeps room`, m.graphH >= 150, m.graphH);
    await shot(page, "live-" + L);
    await tapSel(t, page, "#tiles .tile[data-f=gain]");
    check(`fit ${L}: GAIN steps open`, (await ui(page)).field === "gain");
    await fitCheck(page, L + " GAIN steps");
    await shot(page, "steps-gain-" + L);
    await tapSel(t, page, "#foot .tile");
    await fitCheck(page, L + " MASTER steps");
    await tapSel(t, page, "#stepper .sx");
    box.push({ alerts: [["critical", "KNOBS OFFLINE · USE PHONE MUTE / BYPASS"], ["warning", "DRIFT · GUI"]], muted: true, hold: "unmute", mute_src: "B4",
      pills: ["LAPTOP OPEN 1:59", "LO-CUT OFF", "OUT P2", "POL INV SUB+TOP", "MONO SUB"], lowcut_on: false });
    await sleep(250);
    await fitCheck(page, L + " muted + alerts + pills");
    await shot(page, "muted-" + L);
    box.push({ alerts: [], muted: false, hold: "mute", pills: ["OUT P2"], lowcut_on: true, bands: box.state.bands.map((b, i) => Object.assign(b, { on: i !== 1 })) });
    await sleep(200);
    await fitCheck(page, L + " band OUT");
    await tapSel(t, page, "#setupBtn");
    for (const tab of ["TOPS", "SUBS", "INPUT", "LIVE EQ", "PHONE", "BOX", "THIS PHONE"]) {
      await page.locator("#shTabs button", { hasText: tab.split(" ")[0] }).filter({ hasText: tab.split(" ")[1] || "" }).first().click();
      await sleep(120);
      await fitCheck(page, L + " SETUP " + tab);
      if (L === "393x740") await shot(page, "setup-" + tab.replace(" ", "_") + "-" + L);
    }
    await tapSel(t, page, "#shX");
    await tapSel(t, page, "#stBtn");
    await fitCheck(page, L + " STATUS");
    await tapSel(t, page, "#shX");
    box.accept = false; box.close();
    await sleep(300);
    await fitCheck(page, L + " NO LINK");
    await shot(page, "nolink-" + L);
    await t.close();
  }
});

// landscape (the APK locks portrait; a browser may not): the notice ends above the footer, so
// MASTER, the meters and MUTE stay live and uncovered (DESIGN §9.1)
await run("fit", async () => {
  const t = await open({ vp: { width: 851, height: 393 }, tag: "fit landscape" });
  const r = await t.page.evaluate(() => {
    const top = (sel) => { const el = document.querySelector(sel), b = el.getBoundingClientRect();
      const e = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2); return !!e && (e === el || el.contains(e)); };
    return { rotate: getComputedStyle(document.getElementById("rotate")).display, mute: top("#mute"), master: top("#foot .tile"), meters: top("#meters"),
      scroll: document.documentElement.scrollHeight - innerHeight, muteH: document.getElementById("mute").getBoundingClientRect().height };
  });
  check("fit 851x393 landscape: the notice shows, MUTE, MASTER and the meters stay uncovered", r.rotate === "flex" && r.mute && r.master && r.meters && r.scroll <= 0 && r.muteH >= 44, r);
  await shot(t.page, "landscape-851x393");
  await t.close();
});

// ================================================================== taps and the stepper
await run("tap", async () => {
  const t = await open({ tag: "tap" });
  const { page, box, f } = t;
  const g = await center(page, "#tiles .tile[data-f=gain]");
  await f.tap(g.x, g.y);
  await sleep(100);
  const u = await ui(page);
  const keys = await page.$$eval("#stepper button", (bs) => bs.map((b) => b.textContent.trim()));
  check("tap: a tap on GAIN opens its steps in the band row", u.field === "gain" && !(await page.isVisible("#bands")), { u, keys });
  check("tap: GAIN steps read −3 −½ FLAT +½ +3", keys.slice(1).join("|") === "−3|−½|FLAT|+½|+3", keys);
  check("tap: opening the steps sends nothing", !box.ofType("nudge").length);
  box.clearSent();
  const k = await center(page, "#stepper button:nth-child(3)");
  await f.tap(k.x, k.y); await sleep(200);
  const n = box.ofType("nudge");
  check("tap: a step key sends one nudge, in detents, never absolute", n.length === 1 && n[0].f === "live_peq.1.gain" && n[0].d === -1 && n[0].fine === false && !("v" in n[0]), n);
  check("tap: the tile shows the box's value", (await page.textContent("#tiles .tile[data-f=gain] .tv span")) === "−6.5");
  await f.tap(g.x, g.y); await sleep(100);
  check("tap: a second tap on the tile closes the steps", (await ui(page)).field === null && (await page.isVisible("#bands")));
  // a 12 px diagonal is no tap; 14 px vertical first is nothing at all
  await f.down(g.x, g.y); await sleep(20); await f.move(g.x + 9, g.y + 9); await sleep(20); await f.up(); await sleep(100);
  check("tap: a 12 px diagonal opens nothing", (await ui(page)).field === null);
  box.clearSent();
  await f.down(g.x, g.y); await f.slide(0, 30, 200); await f.slide(60, 0, 300); await f.up(); await sleep(150);
  check("tap: vertical first, then sideways: nothing at all", !box.ofType("nudge").length && (await ui(page)).field === null, box.ofType("nudge"));
  // FLAT: a 0.6 s hold, 0 dB and back IN in one message
  await f.tap(g.x, g.y); await sleep(100);
  box.clearSent();
  const fl = await center(page, "#stepper .flat");
  await f.tap(fl.x, fl.y, 100); await sleep(150);
  check("tap: FLAT tapped does nothing", !box.ofType("band").length);
  await f.hold(fl.x, fl.y, 750); await sleep(150);
  const b = box.ofType("band");
  check("tap: FLAT held sends band {s, g: 0, on: true}", b.length === 1 && b[0].s === 1 && b[0].g === 0 && b[0].on === true && !("f" in b[0]) && !("q" in b[0]), b);
  // the edge zone never slides
  const fr = await center(page, "#tiles .tile[data-f=freq]");
  box.clearSent();
  await f.down(20, fr.y); await f.slide(80, 0, 400); await f.up(); await sleep(150);
  check("tap: a press in the 24 px edge zone never slides", !box.ofType("nudge").length, box.ofType("nudge"));
  await t.close();
});

// ================================================================== slides
// A slide of dx px (after the 10 px lock-in, which does not count) over ms, in even steps.
async function slideOn(t, sel, dx, ms, opts = {}) {
  const { page, f } = t;
  const c = await center(page, sel);
  const x0 = opts.x0 != null ? opts.x0 : c.x - Math.sign(dx) * Math.min(60, c.r.width / 2 - 8);
  const n = opts.steps || Math.max(4, Math.round(ms / 16)), x1 = x0 + Math.sign(dx) * 10;
  await f.down(x0, c.y);
  await t.tick(30);
  await f.move(x1, c.y);
  await t.tick(40);
  for (let i = 1; i <= n; i++) { await t.tick(ms / n); await f.move(x1 + dx * i / n, c.y); }
  await t.tick(30);
  await f.up();
  await t.tick(250);
  await sleep(60);
  await t.tick(60);
}
await run("slide", async () => {
  let t = await open({ tag: "slide", clock: true });
  const { box } = t;
  const FREQ = "#tiles .tile[data-f=freq]";
  await slideOn(t, FREQ, 70, 1170);
  let n = box.ofType("nudge");
  check("slide: slow (60 px/s) = fine nudges, 14 px a detent", n.length > 0 && n.every((m) => m.f === "live_peq.1.freq" && m.fine === true) && sum(n) === 5, n.map((m) => [m.d, m.fine]));
  box.clearSent();
  await slideOn(t, FREQ, -70, 350);
  n = box.ofType("nudge");
  check("slide: ~200 px/s = normal nudges, 14 px a detent", n.length > 0 && n.every((m) => m.fine === false) && sum(n) === -5, n.map((m) => [m.d, m.fine]));
  const sends = (await t.page.evaluate(() => K1000v2.stats.log.slice())).filter((x) => x[0] === "nudge" && x[1] === "live_peq.1.freq").slice(-n.length);
  // (the last one may be the release's flush: "the pending batch is flushed, then nothing")
  check("slide: at most one nudge per field per 50 ms while sliding (page clock)", sends.length === n.length && sends.slice(0, -1).every((x, i) => i === 0 || x[2] - sends[i - 1][2] >= 49), sends);
  box.clearSent();
  await slideOn(t, FREQ, 140, 200);
  n = box.ofType("nudge");
  check("slide: fast (700 px/s) = accelerated, up to the field's cap", n.every((m) => m.fine === false) && sum(n) > 10 && sum(n) <= 40, n.map((m) => m.d));
  const before = await t.page.textContent(FREQ + " .tv span");
  check("slide: the value moved on the tile", before !== "2.44k", before);
  // master: up = 20 px a detent, never accelerated; down may accelerate
  box.clearSent();
  await slideOn(t, "#foot .tile", 190, 1300, { steps: 50, x0: 30 });
  n = box.ofType("nudge").filter((m) => m.f === "master_db");
  check("slide: master up = 0.5 dB per 20 px, never accelerated", n.length > 0 && n.every((m) => m.d > 0 && m.d <= 2 && m.fine === false) && sum(n) === 9, n.map((m) => m.d));
  box.clearSent();
  await slideOn(t, "#foot .tile", -100, 110, { steps: 8, x0: 125 });
  n = box.ofType("nudge").filter((m) => m.f === "master_db");
  check("slide: master down fast is accelerated (×4 cap)", sum(n) < -13, n.map((m) => m.d));
  await t.close();
  // master up beyond the guard: dropped, said, never queued
  t = await open({ tag: "slide-guard", clock: true });
  await slideOn(t, "#foot .tile", 300, 300, { steps: 20, x0: 30 });
  await t.tick(600);
  n = t.box.ofType("nudge").filter((m) => m.f === "master_db");
  const tt = await toasts(t.page), hh = await haps(t.page);
  check("slide: a fast master rise is cut at the guard (6 dB in 2 s)", sum(n) <= 12 && sum(n) >= 6 && n.every((m) => m.d <= 2), n.map((m) => m.d));
  check("slide: the excess is said (SLOW DOWN · master up is limited) with a reject, not queued", tt.includes("SLOW DOWN · master up is limited") && hh.includes("reject"), { tt, hh });
  const lifted = Date.now();
  await t.tick(1000); await sleep(100);
  const late = t.box.ofType("nudge").filter((m) => m.at > lifted);
  check("slide: nothing is sent after the finger lifts", !late.length, late);
  await t.close();
  // THIS PHONE › Slide sensitivity ×1.5: 14/1.5 px a detent (never on master up)
  t = await open({ tag: "slide-sens", prefs: { sens: 1.5 }, clock: true });
  await slideOn(t, FREQ, 70, 1200, { steps: 40 });
  n = t.box.ofType("nudge");
  check("slide: sensitivity ×1.5 = 9.3 px a detent", sum(n) === 7, n.map((m) => m.d));
  t.box.clearSent();
  await slideOn(t, "#foot .tile", 190, 1300, { steps: 50, x0: 30 });
  n = t.box.ofType("nudge").filter((m) => m.f === "master_db");
  check("slide: master up stays 20 px a detent at ×1.5", sum(n) === 9, n.map((m) => m.d));
  await t.close();
});

// ================================================================== band chips
await run("chips", async () => {
  const t = await open({ tag: "chips" });
  const { page, box, f } = t;
  const chip = (s) => `#bands .chip[data-s="${s}"]`;
  const p3 = await center(page, chip(2));
  await f.tap(p3.x, p3.y, 80); await sleep(150);
  check("chips: a tap selects", (await ui(page)).sel === 2 && (await page.getAttribute(chip(2), "class")).includes("sel"));
  check("chips: a tap sends nothing", !box.ofType("band").length);
  await f.hold(p3.x, p3.y, 750); await sleep(200);
  let b = box.ofType("band");
  check("chips: a 0.6 s hold sends band {s, on: false} alone", b.length === 1 && b[0].s === 2 && b[0].on === false && Object.keys(b[0]).filter((k) => !["t", "s", "on", "id", "at"].includes(k)).length === 0, b);
  check("chips: the chip shows OUT once the box says so", (await page.getAttribute(chip(2), "class")).includes("out"));
  check("chips: the toast says P3 OUT", (await toasts(page)).includes("P3 OUT"));
  box.clearSent();
  const p4 = await center(page, chip(3));
  await f.down(p4.x, p4.y); await f.slide(0, -13, 100, 5); await sleep(700); await f.up(); await sleep(150);
  check("chips: a hold that drifts 12 px does nothing (no IN/OUT, no select)", !box.ofType("band").length && (await ui(page)).sel === 2, { sent: box.sent, sel: (await ui(page)).sel });
  await f.down(p4.x, p4.y); await f.slide(30, 0, 120, 6); await sleep(600); await f.up(); await sleep(150);
  check("chips: a slide across the row does nothing", !box.ofType("band").length && (await ui(page)).sel === 2);
  await f.hold(p3.x, p3.y, 750); await sleep(200);
  b = box.ofType("band");
  check("chips: the next hold brings it back IN", b.length === 1 && b[0].on === true, b);
  await t.close();
});

// ================================================================== three taps = FLAT (Thomas, 14/10)
await run("flat3", async () => {
  const t = await open({ tag: "flat3" });
  const { page, box, f } = t;
  const chip = (s) => `#bands .chip[data-s="${s}"]`;
  const setP3 = async (g, on = true) => { box.push({ bands: box.state.bands.map((b, i) => (i === 2 ? Object.assign(b, { g, on }) : b)) }); await sleep(200); };
  const tap3 = async (c, gap = 150) => { for (let i = 0; i < 3; i++) { await f.tap(c.x, c.y, 50); await sleep(gap); } await sleep(250); };
  const undoShown = () => page.isVisible("#undo");
  const p3 = await center(page, chip(2)), p4 = await center(page, chip(3));
  await setP3(-4);
  await f.tap(p4.x, p4.y, 50); await sleep(700);                 // P4 selected
  await tap3(p3);                                                // tap 1 selects P3: it counts for nothing
  check("flat3: the tap that selects a chip starts no count (select + 2 taps: nothing)", !box.ofType("band").length && (await ui(page)).sel === 2, box.sent);
  await sleep(700);
  await tap3(p3);
  let b = box.ofType("band");
  check("flat3: three taps on the selected chip send band {s, g: 0, on: true}", b.length === 1 && b[0].s === 2 && b[0].g === 0 && b[0].on === true && !("f" in b[0]) && !("q" in b[0]), b);
  check("flat3: the toast says P3 FLAT and UNDO shows", (await toasts(page)).includes("P3 FLAT") && (await undoShown()) && (await page.textContent("#undo")) === "UNDO P3 FLAT");
  check("flat3: FLAT plays a click", (await haps(page)).some((x) => String(x).includes("click")), await haps(page));
  box.clearSent();
  await tapSel(t, page, "#undo"); await sleep(250);
  b = box.ofType("band");
  check("flat3: UNDO puts the cut back: band {s, g: -4, on: true}", b.length === 1 && b[0].s === 2 && b[0].g === -4 && b[0].on === true, b);
  check("flat3: UNDO goes once used", !(await undoShown()));
  box.clearSent(); await sleep(700);
  for (let i = 0; i < 3; i++) { await f.tap(p3.x, p3.y, 50); await sleep(600); }
  check("flat3: three slow taps (600 ms apart) do nothing", !box.ofType("band").length, box.sent);
  await sleep(700);
  await setP3(3);
  await tap3(p3);
  b = box.ofType("band");
  check("flat3: a boost is flattened too", b.length === 1 && b[0].g === 0 && b[0].on === true, b);
  check("flat3: but no UNDO for a boost that was IN (no rise from a tap)", !(await undoShown()) && (await toasts(page)).some((x) => /P3 FLAT · boost gone/.test(x)));
  box.clearSent(); await sleep(700);
  await tap3(p3);
  check("flat3: on a flat band: nothing sent, ALREADY FLAT", !box.ofType("band").length && (await toasts(page)).includes("P3 ALREADY FLAT"), box.sent);
  await sleep(700);
  await setP3(5, false);
  box.clearSent();
  await tap3(p3);
  check("flat3: an OUT band comes back IN flat, and UNDO is offered (OUT is silent)", box.ofType("band").length === 1 && (await undoShown()) && (await toasts(page)).includes("P3 FLAT · IN"));
  box.clearSent();
  await tapSel(t, page, "#undo"); await sleep(250);
  b = box.ofType("band");
  check("flat3: UNDO puts it back OUT with its gain: band {g: 5, on: false}", b.length === 1 && b[0].g === 5 && b[0].on === false, b);
  await sleep(700);
  await setP3(-6);
  box.clearSent();
  await tap3(p3);
  check("flat3: UNDO shows again", await undoShown());
  await setP3(-2);                                               // the box moved P3 meanwhile (B2 + E4)
  check("flat3: UNDO goes when the band moves meanwhile", !(await undoShown()));
  await sleep(700);
  await setP3(-6);
  await tap3(p3);
  check("flat3: (and once more for the timeout)", await undoShown());
  await sleep(5200);
  check("flat3: UNDO goes after 5 s", !(await undoShown()));
  await t.close();
});

// ================================================================== mute
await run("mute", async () => {
  const t = await open({ tag: "mute" });
  const { page, box, f } = t;
  const m = await center(page, "#mute");
  await f.hold(m.x, m.y, 300); await sleep(150);
  check("mute: an early release sends nothing", !box.ofType("mute").length);
  await f.down(m.x - 40, m.y); await f.slide(45, 0, 300, 10); await sleep(500); await f.up(); await sleep(150);
  check("mute: a swipe across MUTE never mutes", !box.ofType("mute").length);
  await f.down(m.x, m.y + 20); await f.slide(0, -40, 250, 10); await sleep(500); await f.up(); await sleep(150);
  check("mute: a thumb going up over MUTE never mutes", !box.ofType("mute").length);
  await f.hold(m.x, m.y, 700); await sleep(200);
  let mu = box.ofType("mute");
  check("mute: a 0.6 s hold sends {mute, on: true, id: mute-<n>}, no seq", mu.length === 1 && mu[0].on === true && /^mute-\d+$/.test(mu[0].id) && !("seq" in mu[0]), mu);
  check("mute: the key reads MUTED after the box's ack, the frame is red", (await page.textContent("#mute span")) === "MUTED" && (await page.isVisible("#frame")));
  check("mute: the ack plays the mute confirm", (await haps(page)).includes("muted"));
  const seq = box.state.mute_seq;
  box.clearSent();
  await f.hold(m.x, m.y, 800); await sleep(150);
  check("mute: an unmute needs 1.2 s", !box.ofType("mute").length);
  await f.hold(m.x, m.y, 1350); await sleep(200);
  mu = box.ofType("mute");
  check("mute: a 1.2 s hold unmutes with the mute_seq it saw", mu.length === 1 && mu[0].on === false && mu[0].seq === seq, { mu, seq });
  // the mute changes during an unmute hold: cancelled, said
  await f.hold(m.x, m.y, 700); await sleep(200);
  box.clearSent();
  await f.down(m.x, m.y); await sleep(400);
  box.push({ mute_seq: box.state.mute_seq + 1, mute_src: "B4" });
  await sleep(1000); await f.up(); await sleep(150);
  check("mute: an unmute hold is cancelled when the mute changes meanwhile", !box.ofType("mute").length && (await toasts(page)).some((x) => /changed meanwhile/i.test(x)));
  // no answer within 1.5 s: said, with the box's fallback
  box.push({ muted: false, hold: "mute" });
  await sleep(100);
  box.dropMute = true;
  await f.hold(m.x, m.y, 700); await sleep(1800);
  check("mute: no ack in 1.5 s = 'No answer from K1000: use B4 on the box', REJECT x3",
    (await toasts(page)).some((x) => x === "No answer from K1000: use B4 on the box") && (await haps(page)).includes("fail"));
  box.dropMute = false;
  // a mute made elsewhere is announced
  box.push({ muted: false, hold: "mute" }); await sleep(100);
  box.push({ muted: true, hold: "unmute", mute_src: "B4", mute_seq: box.state.mute_seq + 1 }); await sleep(200);
  check("mute: a mute from B4 is announced", (await toasts(page)).some((x) => /MUTED by B4/.test(x)) && (await page.textContent("#sMute")).includes("B4"));
  // K1000 holds mute: KEEP MUTED, and a hold MUTES (keeps muted)
  box.push({ muted: true, hold: "mute", mute_src: "", mute_band: { kind: "ramp", text: "RAMPS IN BY ITSELF" } }); await sleep(200);
  check("mute: while K1000 holds mute the key reads KEEP MUTED", (await page.textContent("#mute span")) === "KEEP MUTED");
  box.clearSent();
  await f.hold(m.x, m.y, 700); await sleep(200);
  mu = box.ofType("mute");
  check("mute: a hold then keeps it muted (on: true)", mu.length === 1 && mu[0].on === true, mu);
  // never rate-limited by the page: three mutes in a row all go out
  box.push({ muted: false, hold: "mute" }); await sleep(100); box.clearSent();
  for (let i = 0; i < 3; i++) { await f.hold(m.x, m.y, 650); await sleep(120); box.push({ muted: false, hold: "mute" }); await sleep(80); }
  check("mute: never rate-limited by the page", box.ofType("mute").length === 3, box.ofType("mute").length);
  await t.close();
});

// ================================================================== SETUP: choose, then set
await run("setup", async () => {
  const t = await open({ tag: "setup" });
  const { page, box, f } = t;
  await tapSel(t, page, "#setupBtn");
  check("setup: the sheet opens on the box's first group", (await ui(page)).sheet === "setup" && (await ui(page)).tab === fx.hello.setup[0].group);
  const tabs = await page.$$eval("#shTabs button", (bs) => bs.map((b) => b.textContent));
  check("setup: the tabs are the box's groups plus THIS PHONE", tabs.join("|") === fx.hello.setup.map((g) => g.group).concat(["THIS PHONE"]).map((x) => x.replace(/ /g, "")).join("|"), tabs);
  const row = '#shBody [data-row="top_inverted"]';
  const inv = await center(page, row + " .seg button:nth-child(2)");
  await f.tap(inv.x, inv.y); await sleep(150);
  check("setup: a choice sends nothing; the row turns red and SET appears below", !box.ofType("choose").length &&
    (await page.getAttribute(row, "class")).includes("pick") && (await page.isVisible(row + " .setk")));
  const setk = await center(page, row + " .setk");
  check("setup: SET is on its own line, never where the chosen key is", Math.abs(setk.y - inv.y) >= 40, { setk: setk.y, inv: inv.y });
  await f.tap(inv.x, inv.y); await sleep(100);
  check("setup: the chosen key itself never sets", !box.ofType("choose").length && (await toasts(page)).some((x) => /SET KEY BELOW/.test(x)));
  // a double-tap: the choice, then SET within 400 ms
  await page.locator(row + " .seg button:nth-child(1)").click();
  await sleep(100);
  await f.tap(inv.x, inv.y, 40);
  await sleep(60);
  const s2 = await center(page, row + " .setk");
  await f.tap(s2.x, s2.y, 40); await sleep(200);
  check("setup: SET within 400 ms of the choice is refused (a double-tap never commits)", !box.ofType("choose").length);
  await sleep(300);
  await f.tap(s2.x, s2.y); await sleep(250);
  const c = box.ofType("choose");
  check("setup: SET sends choose {f, v, seen}", c.length === 1 && c[0].f === "top_inverted" && c[0].v === true && c[0].seen === false, c);
  // another row cancels a pending choice
  box.clearSent();
  await sleep(500);
  await f.tap(inv.x, inv.y); await sleep(100);
  const lowRow = '#shBody [data-row="lowcut_on"]';
  await page.locator(lowRow + " .seg button:nth-child(1)").click();       // another row cancels the pick
  await sleep(100);
  check("setup: touching another row cancels the pending choice", (await ui(page)).pick === null || (await ui(page)).pick.f === "lowcut_on");
  // a rebuild row: SET is a 1 s hold, MAY CLICK
  await page.locator("#shTabs button", { hasText: "SUBS" }).click(); await sleep(150);
  const mono = '#shBody [data-row="mono_subs"]';
  const want = box.state.setup.mono_subs ? 1 : 2;
  await page.locator(mono + ` .seg button:nth-child(${want})`).click(); await sleep(500);
  const mk = await center(page, mono + " .setk");
  check("setup: a rebuild row's SET says HOLD 1 s and may click", /HOLD 1 s/.test(await page.textContent(mono + " .setk")) && /may click/i.test(await page.textContent(mono + " .setrow")));
  box.clearSent();
  await f.tap(mk.x, mk.y, 80); await sleep(150);
  check("setup: a tap on a rebuild SET does nothing", !box.ofType("choose").length);
  await f.hold(mk.x, mk.y, 1150); await sleep(250);
  const c2 = box.ofType("choose");
  check("setup: a 1 s hold sets it", c2.length === 1 && c2[0].f === "mono_subs" && c2[0].v === (want === 2), c2);
  // CHANGED MEANWHILE: the box refuses a stale seen (a choice within 450 ms of a SET is ignored)
  box.clearSent();
  await sleep(500);
  const pol = '#shBody [data-row="sub_inverted"]';
  await page.locator(pol + " .seg button:nth-child(2)").click(); await sleep(450);
  box.state.setup.sub_inverted = true;           // changed on the box meanwhile, not pushed yet
  const pk = await center(page, pol + " .setk");
  await f.tap(pk.x, pk.y); await sleep(250);
  check("setup: a stale choice is refused and said (CHANGED MEANWHILE)", (await toasts(page)).some((x) => /CHANGED MEANWHILE/.test(x)));
  box.state.setup.sub_inverted = false; box.broadcast();
  // ALL FLAT: a 1 s hold, then UNDO
  await page.locator("#shTabs button", { hasText: "LIVE" }).click(); await sleep(150);
  box.clearSent();
  const af = await center(page, "#shBody .act.warn");
  await f.tap(af.x, af.y, 80); await sleep(150);
  check("setup: ALL FLAT… tapped does nothing", !box.ofType("act").length);
  await f.hold(af.x, af.y, 1150); await sleep(250);
  const a = box.ofType("act");
  check("setup: ALL FLAT… held 1 s sends act reset_live", a.length === 1 && a[0].a === "reset_live", a);
  check("setup: Undo all flat counts down", /UNDO · [45]:\d\d/.test(await page.textContent("#shBody .srow:nth-child(2) .act")), await page.textContent("#shBody .srow:nth-child(2) .act"));
  // THIS PHONE: the UI v1 link, no mic request
  await page.locator("#shTabs button.me").click(); await sleep(150);
  check("setup: THIS PHONE has the UI v1 link to /", (await page.getAttribute("#shBody a.linkbtn", "href")) === "/");
  await shot(page, "this-phone");
  // a sheet scrolls under a vertical swipe (LIVE never does)
  await page.setViewportSize({ width: 393, height: 600 }); await sleep(200);
  const sb = await center(page, "#shBody");
  await f.down(sb.x, sb.y + 60); await f.slide(0, -160, 300, 12); await f.up(); await sleep(400);
  check("setup: a vertical swipe scrolls the sheet", (await page.evaluate(() => document.getElementById("shBody").scrollTop)) > 40);
  check("setup: and changed nothing", !box.ofType("nudge").length && !box.ofType("choose").length);
  await t.close();
});

// ================================================================== D-016: de-harsh, loudness, AUX IN
await run("d016", async () => {
  const t = await open({ tag: "d016", vp: { width: 393, height: 740 } });
  const { page, box, f } = t;
  await tapSel(t, page, "#setupBtn");
  const text = (sel) => page.textContent(sel);
  // TOPS: De-harsh OFF/ON with its FREQ tile beside it, the threshold its own row
  const dh = '#shBody [data-row="deharsh_on"]';
  check("d016: TOPS has De-harsh off / ON paired with its FREQ tile", (await page.$$eval(dh + " .seg button", (bs) => bs.map((b) => b.textContent))).join("|") === "OFF|ON" &&
    (await page.$$eval(dh + " .tile .tl b", (bs) => bs.map((b) => b.textContent))).join("|") === "FREQ", await page.$$eval(dh + " .seg button", (bs) => bs.map((b) => b.textContent)));
  check("d016: the de-harsh row says what it does", /hard attacks/.test(await text(dh + " .rn")), await text(dh + " .rn"));
  check("d016: the FREQ tile reads 2.70k", /2\.70k/.test(await text(dh + " .tile .tv")), await text(dh + " .tile .tv"));
  // ON is a rebuild: chosen, then a 1 s hold on SET (MAY CLICK)
  box.clearSent();
  await page.locator(dh + " .seg button:nth-child(2)").click(); await sleep(500);
  check("d016: De-harsh ON's SET says HOLD 1 s and may click", /HOLD 1 s/.test(await text(dh + " .setk")) && /may click/i.test(await text(dh + " .setrow")));
  const dk = await center(page, dh + " .setk");
  await f.hold(dk.x, dk.y, 1150); await sleep(250);
  const c = box.ofType("choose");
  check("d016: a 1 s hold sends choose deharsh_on true", c.length === 1 && c[0].f === "deharsh_on" && c[0].v === true && c[0].seen === false, c);
  // the threshold: its own tile, a tap opens its steps, a step is a nudge
  const th = '#shBody [data-row="deharsh_thresh_db"]';
  box.clearSent();
  await page.locator(th + " .tile").scrollIntoViewIfNeeded(); await sleep(150);
  await tapSel(t, page, th + " .tile");
  const st = await page.$$eval("#shBody .istep:not([hidden]) button", (bs) => bs.map((b) => b.textContent));
  check("d016: THRESHOLD steps open under its row", st.length >= 4, st);
  await page.locator("#shBody .istep:not([hidden]) button").last().click(); await sleep(300);
  const n = box.ofType("nudge");
  check("d016: a THRESHOLD step sends nudge deharsh_thresh_db", n.length >= 1 && n.every((m) => m.f === "deharsh_thresh_db" && m.d > 0), n);
  await fitCheck(page, "d016 TOPS");
  await shot(page, "d016-tops");
  // SUBS: Loudness with BOOST, then REFERENCE
  await page.locator("#shTabs button", { hasText: "SUBS" }).click(); await sleep(150);
  const ld = '#shBody [data-row="loud_on"]';
  check("d016: SUBS has Loudness paired with its BOOST tile", (await page.$$eval(ld + " .tile .tl b", (bs) => bs.map((b) => b.textContent))).join("|") === "BOOST" &&
    /reference/.test(await text(ld + " .rn")), await text(ld + " .rn"));
  check("d016: the REFERENCE tile reads -10.0", /−?-?10\.0/.test(await text('#shBody [data-row="loud_ref_db"] .tile .tv')), await text('#shBody [data-row="loud_ref_db"] .tile .tv'));
  await fitCheck(page, "d016 SUBS");
  await shot(page, "d016-subs");
  // INPUT on a box with no spare input (the fixture is dev's): both AUX rows read-only, said
  await page.locator("#shTabs button", { hasText: "INPUT" }).click(); await sleep(150);
  const ax = '#shBody [data-row="aux_mode"]', al = '#shBody [data-row="aux_gain_db"]';
  const why = fx.state.readonly.aux_mode;
  const isRo = (sel) => page.$eval(sel, (e) => e.classList.contains("ro"));
  check("d016: the fixture (dev) holds AUX read-only", /NO SPARE INPUT/.test(why || ""), fx.state.readonly);
  check("d016: AUX IN reads NO SPARE INPUT · READ-ONLY with the box's reason", (await isRo(ax)) &&
    (await text(ax + " .rv")) === "NO SPARE INPUT · READ-ONLY" && (await text(ax + " .rn")) === why, await text(ax));
  // a MIC stored from an m4 run routes nothing here: the row says NO SPARE INPUT, as the box does
  box.push({ setup: Object.assign({}, box.state.setup, { aux_mode: "mic" }) }); await sleep(250);
  check("d016: a stored MIC on a box with no spare input still reads NO SPARE INPUT", (await text(ax + " .rv")) === "NO SPARE INPUT · READ-ONLY", await text(ax + " .rv"));
  box.push({ setup: Object.assign({}, box.state.setup, { aux_mode: "off" }) }); await sleep(250);
  const meter = { label: await page.textContent("#shBody .srow:first-child .rl"), note: await page.textContent("#shBody .srow:first-child .rn") };
  check("d016: the INPUT meter says it is the program's, not the AUX", meter.label === "Program input" && /not the AUX IN/.test(meter.note), meter);
  check("d016: the AUX level row is dimmed with the reason", (await isRo(al)) && (await text(al + " .rn")) === why, await text(al));
  box.clearSent();
  await page.locator(ax + " .seg button:nth-child(2)").click(); await sleep(200);
  check("d016: a choice on a read-only AUX sends nothing and says why", !box.ofType("choose").length && (await toasts(page)).some((x) => x === why));
  await page.locator(al + " .tile").scrollIntoViewIfNeeded(); await sleep(150);
  await tapSel(t, page, al + " .tile");
  const said0 = (await toasts(page)).filter((x) => x === why).length;
  const tv0 = await text(al + " .tile .tv");
  const plus = page.locator("#shBody .istep:not([hidden]) button").last();
  await plus.click(); await plus.click(); await sleep(150);
  const back = await toasts(page);
  // refused on the page with the box's reason, BEFORE its PA-up mirror: nothing sent, nothing
  // spent (D-016 review: the page took the shared "pa" tokens the box never spent)
  check("d016: a step on the read-only AUX level sends nothing and says why", !box.ofType("nudge").length &&
    back.filter((x) => x === why).length >= said0 + 2, back.slice(-4));
  check("d016: the read-only AUX level does not move on the page", (await text(al + " .tile .tv")) === tv0, [tv0, await text(al + " .tile .tv")]);
  await fitCheck(page, "d016 INPUT read-only");
  await shot(page, "d016-input-dev");
  // STATUS says the same as the box (no AUX routed here), and a master rise right after the
  // refused AUX steps goes out whole: the page's "pa" mirror was not spent by them
  await tapSel(t, page, "#shX");
  await tapSel(t, page, "#stBtn"); await sleep(150);
  const kvAux = await page.$$eval("#shBody .kv", (rs) => (rs.find((r) => /De-harsh/.test(r.firstChild.textContent)) || { lastChild: { textContent: "" } }).lastChild.textContent);
  check("d016: STATUS reads NO SPARE INPUT for the AUX", /NO SPARE INPUT$/.test(kvAux), kvAux);
  await tapSel(t, page, "#shX");
  await tapSel(t, page, "#setupBtn");
  await page.locator("#shTabs button", { hasText: "INPUT" }).click(); await sleep(150);
  await page.locator(al + " .tile").scrollIntoViewIfNeeded(); await sleep(100);
  await tapSel(t, page, al + " .tile");
  await plus.click(); await plus.click();
  await tapSel(t, page, "#shX");
  await tapSel(t, page, "#foot .tile");
  box.clearSent();
  const before = (await toasts(page)).length;
  await page.locator("#stepper button").last().click(); await sleep(200);
  const mn = box.ofType("nudge").filter((m) => m.f === "master_db");
  const tl = (await toasts(page)).slice(before);
  check("d016: a master rise right after refused AUX steps goes out whole (no SLOW DOWN)", mn.length === 1 && mn[0].d > 0 &&
    !tl.some((x) => /SLOW DOWN/.test(x)), { mn, tl });
  await tapSel(t, page, "#stepper .sx");
  await tapSel(t, page, "#setupBtn");
  await page.locator("#shTabs button", { hasText: "INPUT" }).click(); await sleep(150);
  // the same rows on a box with IN1/IN2 spare (m4): editable, MIC is a rebuild
  box.push({ readonly: {} }); await sleep(250);
  check("d016: on the m4 the AUX rows edit", !(await isRo(ax)) && !(await isRo(al)) &&
    /MIC: IN1/.test(await text(ax + " .rn")), await text(ax + " .rn"));
  check("d016: AUX IN reads off / MIC / LINE", (await page.$$eval(ax + " .seg button", (bs) => bs.map((b) => b.textContent))).join("|") === "OFF|MIC|LINE");
  box.clearSent();
  await sleep(500);
  await page.locator(ax + " .seg button:nth-child(2)").click(); await sleep(500);
  const ak = await center(page, ax + " .setk");
  await f.hold(ak.x, ak.y, 1150); await sleep(250);
  const c2 = box.ofType("choose");
  check("d016: AUX MIC is chosen then held (a rebuild)", c2.length === 1 && c2[0].f === "aux_mode" && c2[0].v === "mic" && c2[0].seen === "off", c2);
  await fitCheck(page, "d016 INPUT m4");
  await shot(page, "d016-input-m4");
  await tapSel(t, page, "#shX");
  // the pills: AUX orange (warn) beside POL INV, LOUDNESS and DE-HARSH neutral
  box.push({ pills: ["AUX MIC", "MONO SUB", "LOUDNESS", "DE-HARSH"] }); await sleep(250);
  const pills = await page.$$eval("#pills .pill", (ps) => ps.map((p) => [p.className, p.textContent]));
  check("d016: the AUX pill is a warning, LOUDNESS / DE-HARSH neutral", pills.some(([c, x]) => x === "AUX MIC" && /warn/.test(c)) &&
    pills.some(([c, x]) => x === "LOUDNESS" && !/warn/.test(c)), pills);
  await fitCheck(page, "d016 pills");
  await t.close();
});

// ================================================================== ALIGN: locked unless the box unlocked this connection
await run("align", async () => {
  const t = await open({ tag: "align" });
  const { page, box, f } = t;
  const D = "#alignRow .tile[data-f=delay]";
  const d = await center(page, D);
  await f.tap(d.x, d.y); await sleep(150);
  check("align: a tap on a locked DELAY says LOCKED, opens nothing", (await ui(page)).field === null && (await toasts(page)).includes("LOCKED · HOLD 0.8 s TO EDIT"));
  await slideOn(t, D, 60, 400);
  check("align: a slide on a locked tile sends nothing", !box.ofType("nudge").length && !box.ofType("unlock").length);
  await f.hold(d.x, d.y, 950); await sleep(250);
  const u = box.ofType("unlock");
  check("align: a 0.8 s hold sends unlock {what: align}", u.length === 1 && u[0].what === "align", u);
  check("align: the tile shows the window the box granted", (await page.getAttribute(D, "class")).includes("unlocked") && /0:[23]\d/.test(await page.textContent(D + " .tl span")));
  check("align: LO-CUT unlocks with it", (await page.getAttribute("#alignRow .tile[data-f=lowcut]", "class")).includes("unlocked"));
  await slideOn(t, D, 42, 300, { steps: 12 });
  const n = box.ofType("nudge");
  check("align: unlocked, a slide nudges align_delay_ms", n.length > 0 && n.every((m) => m.f === "align_delay_ms"), n);
  await f.tap(d.x, d.y); await sleep(150);
  check("align: DELAY's steps have LOCK in the label cell", (await page.textContent("#stepper .sx")).includes("LOCK"));
  const lk = await center(page, "#stepper .sx");
  await f.tap(lk.x, lk.y); await sleep(200);
  check("align: LOCK sends lock and relocks at once", box.ofType("lock").length === 1 && (await page.getAttribute(D, "class")).includes("locked"));
  // the box relocks by itself (idle): the page follows
  await f.hold(d.x, d.y, 950); await sleep(250);
  box.ws.send(JSON.stringify({ t: "locked", what: "align", why: "idle" }));
  await sleep(200);
  check("align: the box's `locked` relocks the page", (await page.getAttribute(D, "class")).includes("locked") && !(await ui(page)).unlocked);
  // err LOCKED from the box (e.g. its window ran out first): relocked and said
  box.unlocked = false;
  await page.evaluate(() => 0);
  await f.hold(d.x, d.y, 950); await sleep(250);
  box.unlocked = false;           // the box's window is gone, the page still shows it open
  box.clearSent();
  await slideOn(t, D, 30, 300, { steps: 10 });
  check("align: err LOCKED relocks the page and says so", !(await ui(page)).unlocked && (await toasts(page)).some((x) => /LOCKED/.test(x)));
  await t.close();
});

// ================================================================== the graph: dots, the notch helper
await run("graph", async () => {
  const t = await open({ tag: "graph" });
  const { page, box, f } = t;
  const g = await center(page, "#cv");
  // long-press on empty graph (low on the graph, between dots): grab {hz, s}, silent
  const ex = g.r.x + g.r.width * 0.42, ey = g.r.y + g.r.height * 0.85;
  await f.hold(ex, ey, 750); await sleep(300);
  const gr = box.ofType("grab");
  check("graph: a long-press on empty graph sends grab {hz, s}", gr.length === 1 && typeof gr[0].hz === "number" && gr[0].s === 1, gr);
  check("graph: then the GAIN steps open on the band the box picked", box.grabs.length === 1 && box.grabs[0].new && (await ui(page)).field === "gain" && (await ui(page)).sel === box.grabs[0].s,
    { grabs: box.grabs, ui: await ui(page) });
  check("graph: the grab is silent (no band, no nudge sent)", !box.ofType("band").length && !box.ofType("nudge").length);
  // ▼ PEAK held: the helper at the box's PEAK (no hz)
  box.clearSent();
  await page.waitForSelector("#peakTag:not([hidden])", { timeout: 2000 });
  const pk = await center(page, "#peakTag");
  await f.tap(pk.x, pk.y, 80); await sleep(150);
  check("graph: ▼ PEAK tapped does nothing", !box.ofType("grab").length);
  await f.hold(pk.x, pk.y, 700); await sleep(300);
  const g2 = box.ofType("grab");
  check("graph: ▼ PEAK held sends grab with no hz (the box's PEAK, exactly B3)", g2.length === 1 && !("hz" in g2[0]), g2);
  // a dot drag: band {s, f, g}, coalesced
  await tapSel(t, page, "#stepper .sx");
  box.clearSent();
  const dot = await page.evaluate(() => {
    const b = K1000v2.state().bands[4], cv = document.getElementById("cv").getBoundingClientRect();
    const xOf = (f) => 24 + Math.log10(f / 20) / 3 * (cv.width - 28);
    const yOf = (db) => 44 + (7 - db) / 28 * (cv.height - 16 - 44);
    return { x: cv.left + xOf(b.f), y: cv.top + yOf(b.g) };
  });
  await f.down(dot.x, dot.y); await f.slide(60, 40, 500, 20); await f.up(); await sleep(250);
  const bd = box.ofType("band");
  // (timed on the page's clock; the last one is the release's: the last value, sent at once)
  const bs = (await page.evaluate(() => K1000v2.stats.log.slice())).filter((x) => x[0] === "band" && x[1] === 4).slice(-bd.length);
  check("graph: a dot drag sends band {s, f, g} (LS here)", bd.length >= 2 && bd.every((m) => m.s === 4 && typeof m.f === "number" && typeof m.g === "number" && !("on" in m)), bd);
  check("graph: at most one band message per 50 ms while dragging", bs.length === bd.length && bs.slice(0, -1).every((x, i) => i === 0 || x[2] - bs[i - 1][2] >= 49), bs);
  check("graph: the dragged band is selected", (await ui(page)).sel === 4);
  await shot(page, "graph-after");
  await t.close();
});

// ================================================================== the spectrum subscription
await run("rta", async () => {
  const t = await open({ tag: "rta" });
  const { page, box } = t;
  const subs = () => box.ofType("sub").map((m) => m.rta);
  check("rta: subscribed while LIVE shows the box spectrum", subs()[0] === true && (await page.evaluate(() => K1000v2.rta().frames)) > 0);
  await tapSel(t, page, "#setupBtn");
  await sleep(100);
  check("rta: a sheet over the graph unsubscribes", subs().slice(-1)[0] === false);
  await tapSel(t, page, "#shX"); await sleep(100);
  check("rta: back on LIVE subscribes again", subs().slice(-1)[0] === true);
  await page.evaluate(() => { Object.defineProperty(document, "hidden", { get: () => true, configurable: true }); document.dispatchEvent(new Event("visibilitychange")); });
  await sleep(100);
  check("rta: hidden = sub {rta: false}", subs().slice(-1)[0] === false);
  await page.evaluate(() => { Object.defineProperty(document, "hidden", { get: () => false, configurable: true }); document.dispatchEvent(new Event("visibilitychange")); });
  await sleep(100);
  check("rta: visible again = sub {rta: true}", subs().slice(-1)[0] === true);
  const n0 = subs().length;
  box.rtaSub = false;
  box.ws.send(JSON.stringify({ t: "unsub", what: "rta", why: "idle" }));
  await sleep(150);
  check("rta: the box's unsub while visible = subscribe again", subs().length === n0 + 1 && subs().slice(-1)[0] === true);
  await tapSel(t, page, "#srcBtn");
  await page.locator("#popShow button[data-v=off]").click(); await sleep(150);
  check("rta: PGM ▾ › OFF unsubscribes, the tag says RTA OFF", subs().slice(-1)[0] === false && (await page.textContent("#srcBtn")).startsWith("RTA OFF"));
  await t.close();
});

// ================================================================== link loss
await run("link", async () => {
  let t = await open({ tag: "link" });
  let { page, box, f } = t;
  box.close(1001);
  await sleep(120);
  check("link: a close dims the controls at once", (await page.getAttribute("body", "class")).includes("nolink") && (await page.textContent("#mute span")) === "NO LINK");
  check("link: the alert line names the real fallback", /NO LINK · to mute: use B4 on the box/.test(await page.textContent("#alertLine")));
  await page.waitForFunction(() => K1000v2.linked(), null, { timeout: 4000 });
  check("link: it reconnects by itself", box.connects >= 2);
  // a MUTE hold in progress when the link drops: dropped, said, never replayed
  box.clearSent();
  const m = await center(page, "#mute");
  await f.down(m.x, m.y); await sleep(250);
  box.close(1001);
  await sleep(500); await f.up();
  await page.waitForFunction(() => K1000v2.linked(), null, { timeout: 4000 });
  await sleep(300);
  check("link: a mute hold cut by the link sends nothing, now or after", !box.ofType("mute").length);
  check("link: and says LINK LOST · nothing sent", (await toasts(page)).some((x) => /LINK LOST · nothing sent/.test(x)) && (await haps(page)).includes("fail"));
  // a slide cut by the link: what was not sent is never sent
  box.clearSent();
  const g = await center(page, "#tiles .tile[data-f=gain]");
  await f.down(g.x - 50, g.y); await f.move(g.x - 40, g.y); await f.slide(30, 0, 200, 8);
  box.close(1001);
  const cut = Date.now();
  await f.slide(80, 0, 300, 10); await f.up();
  await page.waitForFunction(() => K1000v2.linked(), null, { timeout: 4000 });
  await sleep(400);
  const after = box.ofType("nudge").filter((x) => x.at > cut);
  check("link: detents of a slide cut by the link are never replayed", !after.length, after);
  // a dead link (nothing heard for 6.5 s, no close): dead, then back
  box.silent = true;
  const t0 = Date.now();
  await page.waitForFunction(() => !K1000v2.linked(), null, { timeout: 10000 });
  const dt = Date.now() - t0;
  check("link: nothing heard for 6.5 s = link lost", dt >= 5500 && dt <= 9000, dt);
  box.silent = false;
  await page.waitForFunction(() => K1000v2.linked(), null, { timeout: 6000 });
  check("link: back after the box answers again", true);
  // back-off while the box refuses
  box.accept = false;
  const lostBefore = (await toasts(page)).filter((x) => /LINK LOST/.test(x)).length;
  box.close(1001);
  const c0 = box.connects;
  const seen = [], shown = [];
  for (let i = 0; i < 14; i++) {
    await sleep(300);
    seen.push(await page.textContent("#alertLine"));
    shown.push(await page.evaluate(() => (document.getElementById("toast").hidden ? "" : document.getElementById("toast").textContent)));
  }
  const tries = box.connects - c0;
  const lostSaid = (await toasts(page)).filter((x) => /LINK LOST/.test(x)).length - lostBefore;
  check("link: LINK LOST is said once, not again at each failed retry", lostSaid === 1 && !shown.slice(-6).some((x) => /LINK LOST/.test(x)), { lostSaid, shown });
  check("link: NO LINK and the fallback stay up through every retry", seen.every((x) => /NO LINK · to mute/.test(x)), seen);
  check("link: back-off 0.5 s doubling to 3 s (3 to 5 tries in 4 s)", tries >= 3 && tries <= 5, tries);
  box.accept = true;
  await page.waitForFunction(() => K1000v2.linked(), null, { timeout: 5000 });
  // phone control off at the box
  box.ws.send(JSON.stringify({ t: "bye", code: 4003, why: "phone off", phone_on: false }));
  box.accept = false; box.close(4003);
  await sleep(200);
  check("link: 4003 = PHONE CONTROL OFF · use B4 on the box", /PHONE CONTROL OFF · use B4 on the box/.test(await page.textContent("#alertLine")));
  await t.close();
});

// ================================================================== the box was updated: reload when idle
await run("reload", async () => {
  const t = await open({ tag: "reload" });
  const { page, box, f } = t;
  let loads = 0;
  page.on("load", () => { loads++; });
  const s = await center(page, "#graph");
  await f.down(s.x, s.y + 60);              // a finger down (a long-press there is harmless)
  box.pushHello({ build: "feedfeedfeed" });
  await sleep(2600);
  check("reload: never while a finger is down", loads === 0);
  await f.up();
  await sleep(1200);
  check("reload: not before 2 s without a finger", loads === 0);
  await sleep(2200);
  check("reload: once idle 2 s, the page reloads once", loads === 1, loads);
  await page.waitForFunction(() => window.K1000v2 && K1000v2.linked(), null, { timeout: 5000 }).catch(() => {});
  await sleep(2600);
  check("reload: and only once for that build", loads === 1, loads);
  await t.close();
});

// ================================================================== two fingers (DESIGN §5.3, §9.1)
// MUTE is never blocked by another hold, and a second finger stops a slide, a drag or a hold
const muteXY = (page) => center(page, "#mute");
await run("twofinger", async () => {
  const t = await open({ tag: "twofinger", hands: true });
  const { page, box, f } = t;
  const two = f.two;
  const chip = await center(page, '#bands .chip[data-s="2"]'), mu = await muteXY(page);
  // a thumb rests on a chip (its hold would switch P3 OUT), the other holds MUTE
  box.clearSent();
  await two.set({ 1: [chip.x, chip.y] }); await sleep(120);
  await two.set({ 1: [chip.x, chip.y], 2: [mu.x, mu.y] }); await sleep(900);
  await two.off(); await sleep(250);
  let m = box.ofType("mute");
  check("two fingers: a chip held, then MUTE held: MUTE mutes", m.length === 1 && m[0].on === true && box.state.muted === true, box.sent);
  check("two fingers: and the chip's hold was cancelled (no IN / OUT)", !box.ofType("band").length, box.ofType("band"));
  // ▼ PEAK held, then MUTE
  box.push({ muted: false, hold: "mute" }); await sleep(150);
  await page.waitForSelector("#peakTag:not([hidden])", { timeout: 3000 });
  const pk = await center(page, "#peakTag");
  box.clearSent();
  await two.set({ 1: [pk.x, pk.y] }); await sleep(100);
  await two.set({ 1: [pk.x, pk.y], 2: [mu.x, mu.y] }); await sleep(900);
  await two.off(); await sleep(250);
  m = box.ofType("mute");
  check("two fingers: ▼ PEAK held, then MUTE held: MUTE mutes, no grab", m.length === 1 && m[0].on === true && !box.ofType("grab").length, box.sent);
  // MUTE first, a chip second: MUTE's hold goes on, the chip does nothing
  box.push({ muted: false, hold: "mute" }); await sleep(150);
  box.clearSent();
  await two.set({ 2: [mu.x, mu.y] }); await sleep(150);
  await two.set({ 2: [mu.x, mu.y], 3: [chip.x, chip.y] }); await sleep(800);
  await two.off(); await sleep(250);
  check("two fingers: MUTE held, then a chip: MUTE still mutes, the chip does nothing", box.ofType("mute").length === 1 && !box.ofType("band").length, box.sent);
  // the second finger lifts first: MUTE's hold is not disturbed either way
  box.push({ muted: false, hold: "mute" }); await sleep(150);
  // a chip hold, a second finger on a tile: the hold is cancelled, the tile does nothing
  box.clearSent();
  const bal = await center(page, "#alignRow .tile[data-f=bal]");
  await two.set({ 1: [chip.x, chip.y] }); await sleep(250);
  await two.set({ 1: [chip.x, chip.y], 2: [bal.x, bal.y] }); await sleep(100);
  await two.set({ 1: [chip.x, chip.y], 2: [bal.x + 60, bal.y] }); await sleep(600);
  await two.off(); await sleep(250);
  check("two fingers: a second finger cancels a chip hold, and never slides", !box.ofType("band").length && !box.ofType("nudge").length, box.sent);
  // a slide, then a second finger: sending stops
  box.clearSent();
  const g = await center(page, "#tiles .tile[data-f=gain]");
  await two.set({ 1: [g.x - 50, g.y] }); await sleep(30);
  for (let i = 1; i <= 8; i++) { await two.set({ 1: [g.x - 50 + i * 6, g.y] }); await sleep(40); }
  await sleep(120);
  const before = box.ofType("nudge").length;
  await two.set({ 1: [g.x - 2, g.y], 2: [bal.x, bal.y] }); await sleep(30);
  for (let i = 1; i <= 10; i++) { await two.set({ 1: [g.x - 2 + i * 6, g.y], 2: [bal.x, bal.y] }); await sleep(40); }
  await two.off(); await sleep(250);
  check("two fingers: a second finger stops a slide (what was sent stays, nothing more)", before > 0 && box.ofType("nudge").length === before, { before, after: box.ofType("nudge").length });
  // a dot drag, then a second finger: no band message after it, not even the last value
  box.clearSent();
  const dot = await page.evaluate(() => {
    const b = K1000v2.state().bands[1], cv = document.getElementById("cv").getBoundingClientRect();
    const xOf = (fq) => 24 + Math.log10(fq / 20) / 3 * (cv.width - 28);
    const yOf = (db) => 44 + (7 - db) / 28 * (cv.height - 16 - 44);
    return { x: cv.left + xOf(b.f), y: cv.top + yOf(b.g) };
  });
  await two.set({ 1: [dot.x, dot.y] }); await sleep(30);
  for (let i = 1; i <= 10; i++) { await two.set({ 1: [dot.x + i * 4, dot.y + i * 2] }); await sleep(30); }
  await sleep(120);
  const b0 = box.ofType("band").length;
  await two.set({ 1: [dot.x + 40, dot.y + 20], 2: [bal.x, bal.y] }); await sleep(30);
  for (let i = 11; i <= 30; i++) { await two.set({ 1: [dot.x + i * 4, dot.y + i * 2], 2: [bal.x, bal.y] }); await sleep(30); }
  await two.off(); await sleep(250);
  check("two fingers: a second finger stops a dot drag (no band message after it)", b0 > 0 && box.ofType("band").length === b0, { b0, total: box.ofType("band").length });
  check("two fingers: the drag readout is gone, the spectrum is back", await page.evaluate(() => document.getElementById("readout").hidden));
  await t.close();
});

// ================================================================== a hold never fires after its key left
await run("holdgone", async () => {
  const t = await open({ tag: "holdgone" });
  const { page, box, f } = t;
  // FLAT pressed 7.6 s after the steps opened: the idle close waits for the finger
  await tapSel(t, page, "#tiles .tile[data-f=gain]");
  const fl = await center(page, "#stepper .flat");
  box.clearSent();
  await sleep(7450);
  await f.down(fl.x, fl.y); await sleep(520);          // lifted past the 8 s idle, before FLAT's 0.6 s
  const openUnder = (await ui(page)).field === "gain";
  await f.up(); await sleep(700);
  check("hold gone: the steps never close under a finger (idle waits)", openUnder && (await ui(page)).field === "gain");
  check("hold gone: FLAT let go early sent nothing", !box.ofType("band").length, box.ofType("band"));
  await sleep(8200);
  check("hold gone: the idle close comes 8 s after the finger lifted", (await ui(page)).field === null);
  // FLAT held while Back (Esc) closes the steps: nothing is sent, now or when the finger lifts
  await tapSel(t, page, "#tiles .tile[data-f=gain]");
  const fl2 = await center(page, "#stepper .flat");
  box.clearSent();
  await f.down(fl2.x, fl2.y); await sleep(250);
  await page.keyboard.press("Escape"); await sleep(600);
  await f.up(); await sleep(300);
  check("hold gone: FLAT held while its steps close sends nothing", (await ui(page)).field === null && !box.ofType("band").length, box.ofType("band"));
  // a 1 s SETUP hold while the sheet closes (Back): no act
  await tapSel(t, page, "#setupBtn");
  await page.locator("#shTabs button", { hasText: "LIVE" }).click(); await sleep(150);
  const af = await center(page, "#shBody .act.warn");
  box.clearSent();
  await f.down(af.x, af.y); await sleep(400);
  await page.keyboard.press("Escape"); await sleep(900);
  await f.up(); await sleep(300);
  check("hold gone: ALL FLAT… held while the sheet closes sends nothing", (await ui(page)).sheet === null && !box.ofType("act").length, box.ofType("act"));
  // and while the tab changes under it (a second finger is refused: Esc stands in)
  await tapSel(t, page, "#setupBtn");
  await page.locator("#shTabs button", { hasText: "LIVE" }).click(); await sleep(150);
  const af2 = await center(page, "#shBody .act.warn");
  box.clearSent();
  await f.down(af2.x, af2.y); await sleep(300);
  await page.evaluate(() => document.querySelector('#shTabs button[data-k="BOX"]').click()); await sleep(1000);
  await f.up(); await sleep(300);
  check("hold gone: ALL FLAT… held while the tab changes sends nothing", !box.ofType("act").length, box.ofType("act"));
  await tapSel(t, page, "#shX");
  await t.close();
});

// ================================================================== a stalled link (open, silent): edits wait, MUTE goes
await run("stall", async () => {
  const t = await open({ tag: "stall" });
  const { page, box, f } = t;
  const bal = await center(page, "#tiles .tile[data-f=gain]");     // (the middle tile: no edge zone)
  box.silent = true;
  const t0 = Date.now();
  await sleep(1500);
  check("stall: under 2.5 s of silence the link is not slow yet", !(await page.evaluate(() => K1000v2.slow())));
  await sleep(1200);
  check("stall: after 2.5 s of silence: LINK SLOW on the alert line and the strip", (await page.evaluate(() => K1000v2.slow())) &&
    /LINK SLOW/.test(await page.textContent("#alertLine")) && (await page.textContent("#sLink")).includes("SLOW"));
  await f.down(bal.x - 50, bal.y); await f.slide(70, 0, 300, 12); await f.up(); await sleep(100);
  check("stall: a slide on a stalled link sends nothing and says LINK SLOW", !box.ofType("nudge").length && (await toasts(page)).includes("LINK SLOW · nothing sent"),
    { n: box.ofType("nudge"), toasts: (await toasts(page)).slice(-4) });
  await tapSel(t, page, "#tiles .tile[data-f=gain]");
  const k = await center(page, "#stepper button:nth-child(2)");
  await f.tap(k.x, k.y); await sleep(150);
  check("stall: a step key sends nothing either", !box.ofType("nudge").length);
  await tapSel(t, page, "#stepper .sx");
  const mu = await muteXY(page);
  await f.hold(mu.x, mu.y, 750); await sleep(150);
  check("stall: MUTE still goes out", box.ofType("mute").length === 1 && box.ofType("mute")[0].on === true, box.ofType("mute"));
  check("stall: all within the 6.5 s before the link is dead", Date.now() - t0 < 6000, Date.now() - t0);
  box.silent = false;
  await page.waitForFunction(() => !K1000v2.slow(), null, { timeout: 3000 }).catch(() => {});
  check("stall: heard again within a ping: LINK SLOW clears", !(await page.evaluate(() => K1000v2.slow())) && (await page.evaluate(() => K1000v2.linked())));
  await sleep(1700);           // (the unanswered mute's 1.5 s is said)
  box.clearSent();
  await f.down(bal.x - 50, bal.y); await f.slide(70, 0, 500, 20); await f.up(); await sleep(200);
  check("stall: then a slide sends again", box.ofType("nudge").length > 0);
  await t.close();
});

// ================================================================== MUTE: each press its own ack
await run("muteid", async () => {
  const t = await open({ tag: "muteid" });
  const { page, box, f } = t;
  const mu = await muteXY(page);
  box.dropMute = true;
  await f.hold(mu.x, mu.y, 700); await sleep(1700);
  const first = box.ofType("mute")[0];
  check("mute id: no answer in 1.5 s is said", (await toasts(page)).includes("No answer from K1000: use B4 on the box"));
  await f.hold(mu.x, mu.y, 700); await sleep(150);
  const second = box.ofType("mute")[1];
  check("mute id: every press has its own id", first && second && first.id !== second.id && /^mute-\d+$/.test(second.id), box.ofType("mute"));
  box.ws.send(JSON.stringify({ t: "ack", id: first.id, v: true }));     // the first press's late ack
  await sleep(150);
  check("mute id: the late ack of the earlier press never confirms this one", (await page.textContent("#mute span")) === "MUTING…");
  box.ws.send(JSON.stringify({ t: "ack", id: second.id, v: true }));
  await sleep(150);
  check("mute id: its own ack does", (await page.textContent("#mute span")) !== "MUTING…" && (await haps(page)).includes("muted"));
  box.dropMute = false;
  await t.close();
});

// ================================================================== ALIGN: the idle relock is said
await run("relock", async () => {
  const t = await open({ tag: "relock" });
  const { page, box, f } = t;
  const D = "#alignRow .tile[data-f=delay]";
  const d = await center(page, D);
  box.alignS = 2;
  await f.hold(d.x, d.y, 950); await sleep(150);
  await tapSel(t, page, D);
  check("relock: unlocked, DELAY's steps open", (await ui(page)).unlocked && (await ui(page)).field === "delay");
  await page.waitForFunction(() => !K1000v2.ui().unlocked, null, { timeout: 4000 });
  await sleep(200);
  box.ws.send(JSON.stringify({ t: "locked", what: "align", why: "idle" }));     // the box's, a tick after the page's expiry
  await sleep(200);
  const said = (await toasts(page)).filter((x) => x === "DELAY + LO-CUT LOCKED AGAIN").length;
  check("relock: the box's idle `locked` after the page's own expiry is said once", said === 1, (await toasts(page)).slice(-4));
  check("relock: and DELAY's steps close", (await ui(page)).field === null);
  // no `locked` from the box: the page relocks by itself a second after the window, said
  await f.hold(d.x, d.y, 950); await sleep(150);
  await sleep(3400);
  const said2 = (await toasts(page)).filter((x) => x === "DELAY + LO-CUT LOCKED AGAIN").length;
  check("relock: without the box's `locked` the page relocks by itself and says it", said2 === 2 && (await page.getAttribute(D, "class")).includes("locked"), (await toasts(page)).slice(-4));
  await t.close();
});

// ================================================================== the notch helper's answers
await run("grab", async () => {
  const st = showState(fx);
  st.bands[2] = { f: 3170, g: -4, q: 8.651, on: true };          // P3 cuts the ring already
  const t = await open({ tag: "grab", state: st });
  const { page, box, f } = t;
  await page.waitForSelector("#peakTag:not([hidden])", { timeout: 3000 });
  const pk = await center(page, "#peakTag");
  box.clearSent();
  await f.hold(pk.x, pk.y, 700); await sleep(300);
  check("grab: a bell already on the PEAK: selected, nothing written, said", box.grabs.length === 1 && !box.grabs[0].new && box.grabs[0].s === 2 &&
    (await ui(page)).sel === 2 && (await toasts(page)).some((x) => /P3 already there/.test(x)), { grabs: box.grabs, toasts: (await toasts(page)).slice(-3) });
  await tapSel(t, page, "#stepper .sx");
  box.push({ bands: box.state.bands.map((b, i) => (i < 4 ? Object.assign(b, { g: b.g || -3 }) : b)) }); await sleep(150);
  box.peakHz = 900;
  await sleep(300);
  const pk2 = await center(page, "#peakTag");
  await f.hold(pk2.x, pk2.y, 700); await sleep(300);
  check("grab: no free bell = NO FREE BAND · FLAT one first", (await toasts(page)).includes("NO FREE BAND · FLAT one first"), (await toasts(page)).slice(-3));
  await t.close();
});

// ================================================================== the page's own alerts fit the alert line
await run("alerts", async () => {
  for (const vp of [{ width: 360, height: 640 }, { width: 393, height: 851 }]) {
    const L = vp.width + "x" + vp.height;
    const t = await open({ vp, tag: "alerts " + L });
    const { page, box } = t;
    const cut = () => page.evaluate(() => {
      const al = document.getElementById("alertLine"), sp = al.firstChild;
      return { text: sp.textContent, own: al.dataset.own, hidden: al.hidden, cut: sp.scrollWidth > sp.clientWidth + 0.5 };
    });
    const states = [];
    box.push({ muted: true, hold: "mute", mute_band: { kind: "ramp", text: "RAMPS IN BY ITSELF" } }); await sleep(200);
    states.push(await cut());
    box.push({ mute_band: { kind: "ready", text: "READY" } }); await sleep(200);
    states.push(await cut());
    box.push({ muted: false, hold: "mute", mute_band: { kind: "", text: "" }, in_clip: true }); await sleep(200);
    states.push(await cut());
    box.push({ in_clip: false }); await sleep(100);
    box.silent = true; await sleep(2800);
    states.push(await cut());
    box.silent = false; await sleep(1200);
    box.push({ mute_fallback: "turn the source down · bypass" }); await sleep(150);
    box.accept = false; box.close(1001); await sleep(300);
    states.push(await cut());
    box.accept = true; await page.waitForFunction(() => K1000v2.linked(), null, { timeout: 5000 });
    box.push({ mute_fallback: "use B4 on the box" }); await sleep(150);
    box.ws.send(JSON.stringify({ t: "bye", code: 4003, why: "phone off", phone_on: false }));
    box.accept = false; box.close(4003); await sleep(300);
    states.push(await cut());
    box.accept = true;
    await page.waitForFunction(() => K1000v2.linked(), null, { timeout: 8000 });
    box.accept = false; box.close(4002); await sleep(300);
    states.push(await cut());
    const bad = states.filter((x) => x.hidden || x.cut || x.own !== "1");
    check(`alerts ${L}: the page's own alerts are whole on the alert line (instruction first)`, !bad.length && states.length === 7, states);
    check(`alerts ${L}: K1000's boot hold says the action first`, /^HOLD = KEEP MUTED · K1000 RAMPS IN BY ITSELF$/.test(states[0].text), states[0]);
    check(`alerts ${L}: headless NO LINK names its fallback whole`, states[4].text === "NO LINK · to mute: turn the source down · bypass", states[4]);
    check(`alerts ${L}: phone control off names B4`, states[5].text === "PHONE CONTROL OFF · use B4 on the box", states[5]);
    await t.close();
  }
});

// ================================================================== odd hello / state shapes never break MUTE
await run("shapes", async () => {
  const variants = [
    ["no setup", (h) => { delete h.setup; return h; }],
    ["setup not a list", (h) => { h.setup = {}; return h; }],
    ["bands not a list", (h) => { h.bands = "x"; return h; }],
    ["no short", (h) => { delete h.short; return h; }],
    ["slide without px_per_detent", (h) => { delete h.slide.px_per_detent; return h; }],
  ];
  for (const [name, fn] of variants) {
    const errs0 = consoleErrors.length;
    const t = await open({ tag: "shapes " + name, hello: fn });
    const { page, box, f } = t;
    box.push({ muted: true, hold: "unmute", mute_src: "B4" }); await sleep(200);
    const key = await page.textContent("#mute span");
    await tapSel(t, page, "#setupBtn"); await sleep(200);
    await page.evaluate(() => document.querySelector("#shTabs button.me") && document.querySelector("#shTabs button.me").click()); await sleep(150);
    await tapSel(t, page, "#stBtn"); await sleep(200);
    const status = await page.evaluate(() => K1000v2.ui().sheet);
    await tapSel(t, page, "#shX");
    box.clearSent();
    const ft = await center(page, "#foot .tile");
    await f.down(ft.x + 20, ft.y); await f.slide(-40, 0, 400, 16); await f.up(); await sleep(200);
    const dsum = box.ofType("nudge").reduce((a, m) => a + m.d, 0);
    const errs = consoleErrors.slice(errs0);
    consoleErrors.length = errs0;            // (said here, not again below)
    check(`shapes: ${name}: MUTE reads MUTED, SETUP and STATUS open, no error`, key === "MUTED" && status === "status" && !errs.length, { key, status, errs: errs.slice(0, 3) });
    // 30 px past the lock-in = 2 detents at 14 px (more if the burst reads fast: ×4 at most)
    check(`shapes: ${name}: a 40 px master slide down keeps the 14 px pitch`, dsum <= -2 && dsum >= -8, dsum);
    await t.close();
  }
  // a state whose setup is not an object
  const errs0 = consoleErrors.length;
  const st = showState(fx); st.setup = 5;
  const t = await open({ tag: "shapes state.setup", state: st });
  await tapSel(t, t.page, "#setupBtn"); await sleep(200);
  await tapSel(t, t.page, "#shX");
  const errs = consoleErrors.slice(errs0);
  consoleErrors.length = errs0;
  check("shapes: state.setup not an object: no error", !errs.length, errs.slice(0, 3));
  await t.close();
});

// ================================================================== sheets: labels, copy, the popover, SET in view
await run("sheets", async () => {
  const t = await open({ tag: "sheets", vp: { width: 393, height: 600 } });
  const { page, box, f } = t;
  await tapSel(t, page, "#srcBtn");
  check("sheets: the PGM ▾ popover is open", (await ui(page)).popOpen);
  await tapSel(t, page, "#setupBtn");
  check("sheets: opening a sheet closes the popover", !(await ui(page)).popOpen && (await page.isHidden("#pop")));
  const tileLabels = () => page.$$eval("#shBody .tile .tl b", (bs) => bs.map((b) => b.textContent));
  check("sheets: TOPS trims read TOP L / TOP R (then D-016's de-harsh FREQ, THRESHOLD)", (await tileLabels()).join("|") === "TOP L|TOP R|FREQ|THRESHOLD", await tileLabels());
  await page.locator("#shTabs button", { hasText: "SUBS" }).click(); await sleep(150);
  check("sheets: SUBS trims read SUB L / SUB R, the low-pass tile SUB LP (then D-016's loudness BOOST, REFERENCE)", (await tileLabels()).join("|") === "SUB L|SUB R|SUB LP|BOOST|REFERENCE", await tileLabels());
  // the low-pass choice at 600 px: its SET line scrolls into view
  const lp = '#shBody [data-row="sub_lowpass_on"]';
  const want = box.state.setup.sub_lowpass_on ? 1 : 2;
  await page.evaluate(() => { document.getElementById("shBody").scrollTop = 0; });
  await page.locator(lp + ` .seg button:nth-child(${want})`).click(); await sleep(200);
  const vis = await page.evaluate((sel) => {
    const k = document.querySelector(sel + " .setk").getBoundingClientRect(), b = document.getElementById("shBody").getBoundingClientRect();
    return { k: [Math.round(k.top), Math.round(k.bottom)], b: [Math.round(b.top), Math.round(b.bottom)], app: document.getElementById("app").scrollTop };
  }, lp);
  check("sheets: a pending choice's SET key is scrolled into view (the page itself never moves)", vis.k[0] >= vis.b[0] && vis.k[1] <= vis.b[1] && vis.app === 0, vis);
  await page.keyboard.press("Escape"); await sleep(100);
  await page.locator("#shTabs button", { hasText: "PHONE" }).first().click(); await sleep(150);
  const phone = await page.textContent("#shBody");
  check("sheets: PHONE says Phone control: ON", /Phone control\s*ON/.test(phone), phone.slice(0, 160));
  box.push({ laptop: { state: "closing", s: 0 } }); await sleep(250);
  check("sheets: laptop access closing reads CLOSING…", (await page.textContent("#shBody .act.go")).includes("CLOSING…"));
  box.push({ laptop: { state: "unavailable", s: 0 } }); await sleep(250);
  check("sheets: laptop access unavailable reads UNAVAILABLE", (await page.textContent("#shBody .act.go")).includes("UNAVAILABLE"));
  await page.locator("#shTabs button.me").click(); await sleep(150);
  check("sheets: THIS PHONE has the gestures", (await page.textContent("#shBody")).includes("Gestures") && (await page.$$eval("#shBody dl.gl dt", (d) => d.length)) >= 7);
  await tapSel(t, page, "#shX");
  // STATUS: the mute in the strip's words, mono subs and the sub low-pass
  const mu = await muteXY(page);
  await f.hold(mu.x, mu.y, 700); await sleep(250);
  await tapSel(t, page, "#stBtn");
  const st = await page.textContent("#shBody");
  check("sheets: STATUS Mute says by this phone, as the strip", /MUTED · by this phone/.test(st), st.slice(0, 300));
  check("sheets: STATUS has Mono subs · sub LP", /Mono subs · sub LP\s*ON · /.test(st));
  check("sheets: STATUS Spectrum says not running, not a raw 'off'", !/· PGM · off/.test(st));
  await tapSel(t, page, "#shX");
  await t.close();
});

// ================================================================== the APK bridge: who may hand over a port
await run("native", async () => {
  const t = await open({ tag: "native-origin" });
  const { page } = t;
  const r = await page.evaluate(() => {
    const out = {};
    for (const origin of ["null", "https://evil.example", ""]) {
      const ch = new MessageChannel();
      const got = [];
      ch.port1.onmessage = (e) => got.push(JSON.parse(e.data).t);
      window.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ t: "native", v: 1, app: "0.2.0", build: "x", ui: "v2", caps: ["haptic"] }), origin, ports: [ch.port2] }));
      out[origin || "(empty)"] = got;
    }
    return new Promise((res) => setTimeout(() => res({ out, refused: K1000v2.nativeRefused(), native: K1000v2.native() }), 300));
  });
  check("native: a handshake from an opaque (null) origin is refused", !r.out.null.length && r.refused.includes("null"), r);
  check("native: and from another origin", !r.out["https://evil.example"].length, r);
  check("native: the WebView's (origin \"\") is accepted", r.out["(empty)"].includes("ready") && r.native && r.native.app === "0.2.0", r);
  await t.close();
});

// ================================================================== landscape: no spectrum under the notice
await run("rta", async () => {
  const t = await open({ tag: "rta-landscape", vp: { width: 851, height: 393 } });
  const { page, box } = t;
  await sleep(300);
  const subs = () => box.ofType("sub").map((m) => m.rta);
  check("rta: in landscape (the notice over the graph) the page does not subscribe", !subs().includes(true) && !(await page.evaluate(() => K1000v2.rta().subscribed)), subs());
  await page.setViewportSize({ width: 393, height: 851 }); await sleep(400);
  check("rta: back in portrait it subscribes", subs().slice(-1)[0] === true, subs());
  await page.setViewportSize({ width: 851, height: 393 }); await sleep(400);
  check("rta: turned again it unsubscribes", subs().slice(-1)[0] === false, subs());
  await t.close();
});

// ================================================================== the APK bridge (simulated handshake)
await run("native", async () => {
  const t = await open({ tag: "native" });
  const { page, f } = t;
  await page.evaluate(() => {
    window.__app = [];
    const ch = new MessageChannel();
    window.__port = ch.port1;
    ch.port1.onmessage = (e) => window.__app.push(JSON.parse(e.data));
    window.postMessage(JSON.stringify({ t: "native", v: 1, app: "0.2.2", code: 5, build: "bench", ui: "v2",
      caps: ["haptic", "fullscreen", "exclusion", "keep_awake", "diag", "back", "set_ui", "insets"] }), location.origin, [ch.port2]);
  });
  await sleep(400);
  const his = t.box.ofType("hi");
  check("native: a handshake after the hello says hi again with the app's versionCode (app 0.2.2: code 5)",
    his.length === 2 && his[0].app === null && his[0].bridge === null && his[1].app === 5 && his[1].bridge === 1 && his[1].caps.includes("haptic"), his);
  const app = () => page.evaluate(() => window.__app.slice());
  let a = await app();
  check("native: the page answers ready {v: 1, caps: [back]}", a.some((m) => m.t === "ready" && m.v === 1 && Array.isArray(m.caps) && m.caps.includes("back")), a);
  check("native: keep_awake after ready", a.some((m) => m.t === "keep_awake" && m.on === true));
  const ex = a.filter((m) => m.t === "exclusion").pop();
  check("native: exclusion rects for the slide rows (full width)", ex && ex.rects.length >= 4 && ex.rects.every((r) => r.length === 4 && r[0] === 0 && r[2] >= 360), ex);
  await slideOn(t, "#tiles .tile[data-f=freq]", 42, 600, { steps: 20 });
  a = await app();
  check("native: haptics go through the bridge (tick per detent)", a.some((m) => m.t === "haptic" && m.kind === "tick"));
  await tapSel(t, page, "#tiles .tile[data-f=gain]");
  await page.evaluate(() => window.__port.postMessage(JSON.stringify({ t: "back", id: 7 })));
  await sleep(150);
  a = await app();
  check("native: Back closes the steps and answers handled", a.some((m) => m.t === "back_handled" && m.id === 7 && m.handled === true) && (await ui(page)).field === null);
  await page.evaluate(() => window.__port.postMessage(JSON.stringify({ t: "back", id: 8 })));
  await sleep(150);
  a = await app();
  check("native: Back at the top level answers handled: false", a.some((m) => m.t === "back_handled" && m.id === 8 && m.handled === false));
  await tapSel(t, page, "#setupBtn");
  await page.locator("#shTabs button.me").click(); await sleep(150);
  await page.evaluate(() => window.__port.postMessage(JSON.stringify({ t: "diag", app: "0.2.2", build: "bench", model: "Pixel 4a", release: "13", sdk: 33,
    webview_version: "141.0", unprocessed_supported: true, has_vibrator: true, system_haptics: true, exclusion_rects: [[0, 1, 2, 3]], density: 2.75 })));
  await sleep(300);
  check("native: the app's diagnostics show in THIS PHONE", (await page.textContent("#shBody")).includes("Pixel 4a · Android 13"));
  check("native: THIS PHONE shows the app's version and its UI v1 key", (await page.textContent("#shBody")).includes("app 0.2.2") && (await page.locator("#shBody button.act.go", { hasText: "UI v1" }).isVisible()));
  await page.locator("#shBody button.act.go", { hasText: "UI v1" }).click(); await sleep(150);
  a = await app();
  check("native: UI v1 = set_ui {ui: v1}", a.some((m) => m.t === "set_ui" && m.ui === "v1"));
  // a second handshake (the app re-posts): the newest port wins, ready again
  await page.evaluate(() => {
    const ch = new MessageChannel(); window.__app2 = [];
    ch.port1.onmessage = (e) => window.__app2.push(JSON.parse(e.data));
    window.postMessage(JSON.stringify({ t: "native", v: 1, app: "0.2.0", build: "bench", ui: "v2", caps: ["haptic"] }), location.origin, [ch.port2]);
  });
  await sleep(200);
  check("native: a re-posted handshake gets ready on the new port", (await page.evaluate(() => window.__app2.slice())).some((m) => m.t === "ready"));
  await t.close();
});

// ================================================================== the v2 service worker (ETag + 304 like the box)
await run("sw", async () => {
  const ctx = await browser.newContext({ viewport: { width: 393, height: 851 }, isMobile: true, hasTouch: true, userAgent: UA });
  const box = new FakeBox(Object.assign({}, fx, { state: showState(fx) }), { stepValue });
  await box.attach(ctx);
  const page = await ctx.newPage();
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push("sw: " + m.text()); });
  page.on("pageerror", (e) => consoleErrors.push("sw: pageerror " + e.message));
  await page.goto(web.url + "/v2/");
  await page.waitForFunction(() => window.K1000v2 && K1000v2.linked(), null, { timeout: 5000 });
  const scope = await page.evaluate(async () => {
    const r = await Promise.race([navigator.serviceWorker.ready, new Promise((res) => setTimeout(() => res(null), 6000))]);
    return r ? r.scope : "not ready: " + K1000v2.sw();
  });
  check("sw: the worker installs with scope /v2/ (the shell cached at install)", typeof scope === "string" && scope.endsWith("/v2/"), scope);
  await page.reload();
  await page.waitForFunction(() => window.K1000v2 && K1000v2.linked(), null, { timeout: 5000 });
  check("sw: a reload is controlled by it and links", await page.evaluate(() => !!navigator.serviceWorker.controller));
  // off the K1000 Wi-Fi: the cached shell opens and names the fallback
  await ctx.setOffline(true);
  box.accept = false; box.close(1001);
  await page.reload().catch(() => {});
  await page.waitForFunction(() => window.K1000v2, null, { timeout: 8000 }).catch(() => {});
  await sleep(1500);
  const off = await page.evaluate(() => ({ app: !!window.K1000v2, link: window.K1000v2 && K1000v2.link(), alert: document.getElementById("alertLine").textContent,
    mute: document.querySelector("#mute span").textContent }));
  check("sw: offline, the cached page opens and says NO LINK with the fallback", off.app && off.mute === "NO LINK" && /use B4 on the box/.test(off.alert), off);
  await ctx.setOffline(false);
  box.stop();
  await ctx.close();
});

// ================================================================== MEASURE fits, the popover fits (no mic needed)
await run("fitm", async () => {
  for (const vp of [{ width: 393, height: 851 }, { width: 393, height: 740 }, { width: 393, height: 600 }, { width: 360, height: 640 }, { width: 393, height: 544 }]) {
    const L = vp.width + "x" + vp.height;
    const t = await open({ vp, tag: "fitm " + L });
    const { page } = t;
    // ≋ sits at the strip's right edge, ▼ PEAK left of it: a PEAK coming or going never moves ≋
    // under a finger (it used to jump ~100 px, and ▼ took its place)
    const strip = () => page.evaluate(() => {
      const r = (id) => { const b = document.getElementById(id).getBoundingClientRect(); return [Math.round(b.left), Math.round(b.right)]; };
      return { meas: r("measBtn"), tag: document.getElementById("peakTag").hidden ? null : r("peakTag") };
    });
    await page.waitForSelector("#peakTag:not([hidden])", { timeout: 3000 });
    const withPk = await strip();
    t.box.peakHz = null;
    await page.waitForSelector("#peakTag[hidden]", { state: "attached", timeout: 3000 });
    const noPk = await strip();
    t.box.peakHz = 3170;
    await page.waitForSelector("#peakTag:not([hidden])", { timeout: 3000 });
    check(`fitm ${L}: ≋ stays put when the PEAK comes or goes (▼ PEAK left of it)`,
      withPk.tag && withPk.meas.join() === noPk.meas.join() && withPk.tag[1] <= withPk.meas[0] && withPk.meas[1] >= vp.width - 20, { withPk, noPk });
    await tapSel(t, page, "#srcBtn");
    await fitCheck(page, L + " PGM ▾ open");
    const muteOnTop = () => page.evaluate(() => { const el = document.getElementById("mute"), b = el.getBoundingClientRect();
      const e = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2); return !!e && (e === el || el.contains(e)) && b.height >= 44; });
    check(`fitm ${L}: MUTE uncovered with the popover open`, await muteOnTop());
    if (L === "393x740") await shot(page, "popover-" + L);
    await tapSel(t, page, "#popX");
    await tapSel(t, page, "#measBtn");
    check(`fitm ${L}: ≋ opens MEASURE (the MEASURE row in the ALIGN row's place)`, (await ui(page)).measure === true && (await page.isVisible("#measRow")) && (await page.isHidden("#alignRow")));
    await fitCheck(page, L + " MEASURE RTA");
    await shot(page, "measure-rta-" + L);
    await page.locator("#measRow button[data-view=resp]").click(); await sleep(200);
    await fitCheck(page, L + " MEASURE RESPONSE");
    check(`fitm ${L}: MUTE uncovered in MEASURE`, await muteOnTop());
    await tapSel(t, page, "#srcBtn");
    await fitCheck(page, L + " MEASURE + PGM ▾");
    await tapSel(t, page, "#popX");
    await tapSel(t, page, "#measBtn");
    check(`fitm ${L}: ≋ again leaves MEASURE`, (await ui(page)).measure === false && (await page.isVisible("#alignRow")));
    await t.close();
  }
});

// ================================================================== MEASURE without a mic, an older box
await run("measure", async () => {
  const t0 = await open({ tag: "measure-nopgm", pgm: null });
  await tapSel(t0, t0.page, "#measBtn");
  await t0.page.locator("#measRow button[data-view=resp]").click(); await sleep(250);
  check("measure: hello.pgm null (this box cannot serve it): RESPONSE says so, nothing subscribed",
    (await toasts(t0.page)).includes("RESPONSE: this box has no spectrum (k1000.yaml)") && !t0.box.ofType("sub").some((m) => "pgm" in m));
  await t0.close();
  const t = await open({ tag: "measure-old", pgm: "absent" });
  const { page, box } = t;
  await tapSel(t, page, "#measBtn");
  await page.locator("#measRow button[data-view=resp]").click(); await sleep(250);
  check("measure: an older box (no hello.pgm): RESPONSE says it needs the box update, nothing subscribed",
    (await toasts(page)).includes("RESPONSE needs the box update") && !box.ofType("sub").some((m) => "pgm" in m) && (await ui(page)).prefs.mview !== "resp");
  check("measure: its RESPONSE key is marked unavailable", (await page.getAttribute("#measRow button[data-view=resp]", "class")).includes("na"));
  // the BAL steps open, then ≋: they close with their tile
  await tapSel(t, page, "#measBtn");
  await tapSel(t, page, "#alignRow .tile[data-f=bal]");
  check("measure: the BAL steps open on LIVE", (await ui(page)).field === "bal");
  await tapSel(t, page, "#measBtn");
  check("measure: ≋ closes steps whose tile it hides (BAL)", (await ui(page)).field === null && (await ui(page)).measure === true);
  // Back: the steps, then MEASURE (then nothing: handled false)
  await tapSel(t, page, "#tiles .tile[data-f=gain]");
  await page.keyboard.press("Escape"); await sleep(100);
  check("measure: Back closes the steps first, MEASURE stays", (await ui(page)).field === null && (await ui(page)).measure === true);
  await page.keyboard.press("Escape"); await sleep(100);
  check("measure: Back then leaves MEASURE", (await ui(page)).measure === false && (await page.isVisible("#alignRow")));
  await t.close();
});

// ================================================================== the phone mic: Chromium's fake capture plays a WAV made here
const LABTMP = fs.mkdtempSync(path.join(os.tmpdir(), "k1000-v2lab-"));
const WAV = {
  pink: writeWav(path.join(LABTMP, "pink.wav"), makeSignal("pink")),
  tone: writeWav(path.join(LABTMP, "tone-1k.wav"), makeSignal("tone", { hz: 1000, above: 15 })),
  tilt: writeWav(path.join(LABTMP, "pink-tilt-2db-oct.wav"), makeSignal("tilt", { dbPerOct: 2 })),
  // the box's PEAK rule finds this one on the engine's numbers (4 dB would do); the analyser's
  // mean power per band, the first build's feed, needed 10 dB
  tone8k: writeWav(path.join(LABTMP, "tone-8k.wav"), makeSignal("tone", { hz: 8000, above: 7 })),
  // pink noise 6x over full scale: every block clips (a phone next to the PA)
  clip: writeWav(path.join(LABTMP, "pink-clipped.wav"), makeSignal("pink", { peak: 3 })),
};
const micBrowser = (wav, ui = true) => chromium.launch({ channel: "chromium",
  args: [...(ui ? ["--use-fake-ui-for-media-stream"] : []), "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${wav}`] });
const mic = (page) => page.evaluate(() => K1000v2.mic());
const meas = (page) => page.evaluate(() => K1000v2.measure());
const gum = (page) => page.evaluate(() => window.__gum.length);
const tracksEnded = (page) => page.evaluate(() => window.__streams.every((s) => s.getTracks().every((tr) => tr.readyState === "ended")));
const liveTracks = (page) => page.evaluate(() => window.__streams.reduce((n, s) => n + s.getTracks().filter((tr) => tr.readyState === "live").length, 0));
async function until(page, fn, arg, ms = 6000) {
  try { await page.waitForFunction(fn, arg, { timeout: ms, polling: 100 }); return true; } catch (e) { return false; }
}
const setHidden = (page, hidden) => page.evaluate((hd) => { Object.defineProperty(document, "hidden", { get: () => hd, configurable: true }); document.dispatchEvent(new Event("visibilitychange")); }, hidden);
const pgmSubs = (box) => box.ofType("sub").filter((m) => "pgm" in m).map((m) => m.pgm);
const lineSpread = (ln, lo, hi) => {
  const v = ln.f.map((f, i) => [f, ln.db[i]]).filter(([f, d]) => f >= lo && f <= hi && d != null).map((x) => x[1]).sort((a, b) => a - b);
  const med = v[v.length >> 1];
  return { med, lo: v[0] - med, hi: v[v.length - 1] - med, n: v.length };
};
async function popPick(t, page, v) { await tapSel(t, page, "#srcBtn"); await page.locator(`#popShow button[data-v=${v}]`).click(); await sleep(150); await tapSel(t, page, "#popX"); }

// A: never asked before (the Permissions API says "prompt"): no getUserMedia from LIVE, the popover
// or MEASURE; SETUP › THIS PHONE › ALLOW asks (the fake UI accepts); then the line, the tracks'
// life, RESPONSE against the fake box's program (the same pink noise, the engine's estimator)
await run("mic", async () => {
  const bA = await micBrowser(WAV.pink);
  const t = await open({ browser: bA, tag: "mic", permPrompt: true, pgmWav: WAV.pink, prefs: { rta: "mic", mspeed: "slow", msmooth: 3 } });
  const { page, box, f } = t;
  try {
    await sleep(400);
    check("mic: PHONE MIC chosen but never allowed: the graph says ALLOW THE MIC: SETUP › THIS PHONE, the tag MIC ?",
      (await page.textContent("#gNote")) === "ALLOW THE MIC: SETUP › THIS PHONE" && (await page.textContent("#srcBtn")).startsWith("MIC ?"), [await page.textContent("#gNote"), await page.textContent("#srcBtn")]);
    await popPick(t, page, "both");
    await popPick(t, page, "mic");
    check("mic: picking PHONE MIC / BOTH in PGM ▾ says where to allow it", (await toasts(page)).includes("ALLOW THE MIC: SETUP › THIS PHONE"));
    await tapSel(t, page, "#measBtn");
    await tapSel(t, page, "#mMic");
    await page.locator("#measRow button[data-view=resp]").click(); await sleep(300);
    check("mic: MEASURE says it too (RESPONSE: MIC OFF, allow it in SETUP)", /ALLOW THE MIC: SETUP › THIS PHONE/.test(await page.textContent("#gNote")), await page.textContent("#gNote"));
    check("mic: no getUserMedia from LIVE, the popover or MEASURE before the grant", (await gum(page)) === 0 && (await mic(page)).gum.length === 0);
    // MUTE in MEASURE: hold 0.6 s mutes, 1.2 s unmutes, as everywhere
    const mu = await center(page, "#mute");
    await f.hold(mu.x, mu.y, 700); await sleep(200);
    const m1 = box.ofType("mute");
    await f.hold(mu.x, mu.y, 1350); await sleep(200);
    const m2 = box.ofType("mute");
    check("mic: MUTE works in MEASURE (mute, then unmute)", m1.length === 1 && m1[0].on === true && m2.length === 2 && m2[1].on === false, m2);
    await page.locator("#measRow button[data-view=rta]").click(); await sleep(100);
    await tapSel(t, page, "#measBtn");
    // the one place that asks
    await tapSel(t, page, "#setupBtn");
    await page.locator("#shTabs button.me").click(); await sleep(150);
    const row = '#shBody [data-row="phone_mic"]';
    check("mic: SETUP › THIS PHONE › Phone mic reads NOT ALLOWED YET with an ALLOW THE MIC key", (await page.textContent(row + " .rv")) === "NOT ALLOWED YET" && (await page.isVisible(row + " button.act")));
    await page.locator(row + " button.act").click();
    await until(page, () => K1000v2.mic().session === true && K1000v2.mic().state === "off");
    await sleep(300);
    const m = await mic(page);
    check("mic: ALLOW asks once, from SETUP › THIS PHONE", (await gum(page)) === 1 && m.gum.length === 1 && m.gum[0].from === "setup" && m.gum[0].sheet === "setup" && m.gum[0].tab === "THIS PHONE", m.gum);
    check("mic: it asked echoCancellation, noiseSuppression and autoGainControl OFF",
      /"echoCancellation":false/.test(await page.evaluate(() => window.__gum[0].c)) && /"noiseSuppression":false/.test(await page.evaluate(() => window.__gum[0].c)) && /"autoGainControl":false/.test(await page.evaluate(() => window.__gum[0].c)));
    check("mic: SETUP shows what the browser kept (read back, all three OFF)", /read back: echo cancel OFF · noise suppr\. OFF · auto gain OFF/.test(await page.textContent(row)), await page.textContent(row));
    check("mic: the row is now OFF / ON, ON", (await page.textContent(row + " .rv")) === "ON" && (await page.isHidden(row + " button.act")) && (await page.getAttribute(row + " .seg button:nth-child(2)", "aria-pressed")) === "true");
    check("mic: the tracks are stopped while SETUP covers LIVE", (await tracksEnded(page)) && m.state === "off");
    await tapSel(t, page, "#shX");
    const on1 = await until(page, () => K1000v2.mic().state === "on" && K1000v2.mic().frames > 5);
    const g2 = (await mic(page)).gum;
    check("mic: back on LIVE (PHONE MIC chosen) it starts by itself, no prompt", on1 && g2.length === 2 && g2[1].from === "auto" && (await liveTracks(page)) === 1, g2);
    await shot(page, "mic-live");
    // the line: pink noise reads flat (band power), SLOW, 1/3 oct
    await sleep(5000);
    let ln = (await mic(page)).line;
    const sp = lineSpread(ln, 200, 8000);
    check("mic: pink noise -> the mic line flat within ±1.5 dB of its median, 200 Hz-8 kHz (SLOW, 1/3 oct)", ln && sp.lo >= -1.5 && sp.hi <= 1.5, sp);
    const y = await page.evaluate(() => { const ln = K1000v2.mic().line; return ln.top; });
    check("mic: the line is placed on the EQ grid (its loudest band at −3 dB), uncalibrated", typeof y === "number");
    // the tracks' life: a sheet, the page hidden, the source off; back without a prompt
    await tapSel(t, page, "#stBtn");
    const offSheet = await until(page, () => K1000v2.mic().state === "off");
    check("mic: a sheet over LIVE stops the tracks (track.stop)", offSheet && (await tracksEnded(page)));
    await tapSel(t, page, "#shX");
    check("mic: closing it restarts them, no prompt", (await until(page, () => K1000v2.mic().state === "on")) && (await liveTracks(page)) === 1);
    await setHidden(page, true);
    const offHidden = await until(page, () => K1000v2.mic().state === "off");
    check("mic: the page hidden stops the tracks", offHidden && (await tracksEnded(page)));
    await setHidden(page, false);
    check("mic: visible again: restarted, no prompt", (await until(page, () => K1000v2.mic().state === "on")) && (await liveTracks(page)) === 1);
    await popPick(t, page, "box");
    check("mic: PGM ▾ › BOX (the mic not shown) stops the tracks", (await until(page, () => K1000v2.mic().state === "off")) && (await tracksEnded(page)));
    await popPick(t, page, "mic");
    check("mic: PGM ▾ › PHONE MIC again: restarted", (await until(page, () => K1000v2.mic().state === "on")) && (await liveTracks(page)) === 1);
    check("mic: every getUserMedia after the grant was an automatic one (no new prompt possible)", (await mic(page)).gum.slice(1).every((x) => x.from === "auto"));
    // MUTE with the popover open
    await tapSel(t, page, "#srcBtn");
    box.clearSent();
    await f.hold(mu.x, mu.y, 700); await sleep(200);
    check("mic: MUTE works with PGM ▾ open (the popover covers the graph only)", box.ofType("mute").length === 1 && (await ui(page)).popOpen, box.ofType("mute"));
    box.push({ muted: false, hold: "mute", mute_src: "" }); await sleep(150);
    await tapSel(t, page, "#popX");

    // ---------------------------------------------------------------- RESPONSE
    await popPick(t, page, "both");
    check("resp: PGM ▾ › BOTH: the box's bars and the mic line (subscribed, the mic on)", (await until(page, () => K1000v2.rta().subscribed && K1000v2.mic().state === "on", null, 3000)));
    await tapSel(t, page, "#measBtn");
    box.clearSent();
    await page.locator("#measRow button[data-view=resp]").click();
    check("resp: RESPONSE shown = sub {pgm: true}", (await until(page, () => K1000v2.measure().frames > 0, null, 3000)) && pgmSubs(box)[0] === true, box.ofType("sub"));
    check("resp: RESPONSE stops the box spectrum (one picture: sub {rta: false})", box.ofType("sub").filter((x) => "rta" in x).map((x) => x.rta).slice(-1)[0] === false && !(await page.evaluate(() => K1000v2.rta().subscribed)));
    await shot(page, "resp-start");
    const full = await until(page, () => K1000v2.measure().cover >= 20, null, 30000);
    let r = await meas(page);
    const rs = r.line ? lineSpread(r.line, 200, 5000) : null;
    check("resp: the average fills to 20 s of windows", full && r.added >= 40, [r.cover, r.added, r.dropped, r.reason]);
    check("resp: the same pink noise on both sides (matched estimators) -> RESPONSE flat within ±1 dB, 200 Hz-5 kHz", rs && rs.lo >= -1 && rs.hi <= 1 && Math.abs(rs.med) <= 0.5, rs);
    check("resp: nothing drawn below 100 Hz or above 12 kHz (the phone's range, DESIGN §7), up to 12 kHz",
      r.line && r.line.f.every((x, i) => (x >= 100 && x <= 12000) || r.line.db[i] === null) && r.line.f.some((x, i) => x > 10000 && x <= 12000 && r.line.db[i] !== null) && r.maxHz === 12000,
      r.line && r.line.f.map((x, i) => [Math.round(x), r.line.db[i]]).filter(([x]) => x > 11000 && x < 13500));
    check("resp: the graph says the coverage", /RESPONSE · 20 s of 20 · uncalibrated mic − program/.test(await page.textContent("#gNote")), await page.textContent("#gNote"));
    await shot(page, "resp-pink");
    // muted: paused (nothing added), said; unmuted: on again
    let a0 = r.added;
    box.push({ muted: true, hold: "unmute", mute_src: "B4", mute_seq: box.state.mute_seq + 1 });
    await sleep(1600);
    r = await meas(page);
    check("resp: muted pauses it (no window added), the graph says PAUSED · MUTED", r.added === a0 && r.reason === "MUTED" && /PAUSED · MUTED · 20 s of 20/.test(await page.textContent("#gNote")), [r.added, a0, r.reason, await page.textContent("#gNote")]);
    box.push({ muted: false, hold: "mute", mute_src: "" });
    check("resp: unmuted, it adds windows again", await until(page, (n) => K1000v2.measure().added >= n + 2, a0, 3000));
    // no program, no spectrum
    box.pgmProg = false; await sleep(1300);
    r = await meas(page); a0 = r.added; await sleep(1100);
    check("resp: the box's prog false pauses it: PAUSED · NO PROGRAM · play music", (await meas(page)).added === a0 && (await meas(page)).reason === "NO PROGRAM" && /PAUSED · NO PROGRAM · play music/.test(await page.textContent("#gNote")));
    box.pgmProg = null; await sleep(1300);
    check("resp: prog null (the box cannot tell) pauses it too: BOX: PROGRAM NOT KNOWN", (await meas(page)).reason === "BOX: PROGRAM NOT KNOWN", (await meas(page)).reason);
    box.pgmProg = true; box.pgmSt = "unavailable"; await sleep(1300);
    r = await meas(page); a0 = r.added; await sleep(3000);
    check("resp: a box without a spectrum (said once, no frames after): PAUSED · BOX: NO SPECTRUM, held", (await meas(page)).added === a0 && (await meas(page)).reason === "BOX: NO SPECTRUM" &&
      /PAUSED · BOX: NO SPECTRUM/.test(await page.textContent("#gNote")), (await meas(page)).reason);
    box.pgmSt = "waiting"; await sleep(800);
    check("resp: waiting: PAUSED · BOX: SPECTRUM STARTING", (await meas(page)).reason === "BOX: SPECTRUM STARTING", (await meas(page)).reason);
    box.pgmSt = "live";
    await until(page, (n) => K1000v2.measure().added >= n + 2, a0, 3000);
    // the master does not restart it; a band, BAL, a SETUP change do
    r = await meas(page);
    box.push({ master_db: box.state.master_db - 2 }); await sleep(400);
    const rm = await meas(page);
    check("resp: the master does not restart it (divided out of each mic tick)", rm.restarts === r.restarts && rm.cover >= 19);
    check("resp: the mic ticks of the 0.8 s after a master move are not used (the engine's volume ramp)", rm.settled > r.settled, [r.settled, rm.settled]);
    await sleep(600);
    box.push({ bands: box.state.bands.map((b, i) => (i === 2 ? Object.assign({}, b, { g: -3 }) : b)) }); await sleep(300);
    const rb = await meas(page);
    check("resp: a band change restarts it (RESTARTED · the live EQ changed)", rb.restarts === r.restarts + 1 && rb.cover < 2 && /RESTARTED · the live EQ changed/.test(await page.textContent("#gNote")), [rb.restarts, rb.cover, await page.textContent("#gNote")]);
    const fa = await until(page, () => K1000v2.measure().firstAddMs != null, null, 4000);
    check("resp: after a restart the mic's first 0.4 s are not used (the change must reach the mic): its first window is kept 0.4 s or more after",
      fa && (await meas(page)).firstAddMs >= 400, (await meas(page)).firstAddMs);
    for (const [what, patch] of [["BAL", { balance_db: 0.5 }], ["the Lo-Cut", { lowcut_hz: 120 }], ["polarity", { setup: Object.assign({}, box.state.setup, { top_inverted: true }) }],
      ["a band OUT", { bands: box.state.bands.map((b, i) => (i === 0 ? Object.assign({}, b, { on: false }) : b)) }],
      ["the system EQ (/tune, state.sys_eq)", { sys_eq: "0badc0de" }]]) {
      await sleep(1200);
      const n0 = (await meas(page)).restarts;
      box.push(patch); await sleep(300);
      check("resp: " + what + " restarts it", (await meas(page)).restarts === n0 + 1);
    }
    check("resp: ...and says so: RESTARTED · the system EQ changed", /RESTARTED · the system EQ changed/.test(await page.textContent("#gNote")), await page.textContent("#gNote"));
    // the box ramping Main in (boot, engine back): paused, said
    await sleep(800);
    box.push({ ramping: true }); await sleep(400);
    r = await meas(page); a0 = r.added; await sleep(1200);
    // (the note may still say the last RESTARTED for its 2.6 s first)
    const rampSaid = await until(page, () => /PAUSED · BOX RAMPING IN/.test(document.getElementById("gNote").textContent), null, 3000);
    check("resp: the box ramping in pauses it: PAUSED · BOX RAMPING IN", rampSaid && (await meas(page)).added === a0 && (await meas(page)).reason === "BOX RAMPING IN",
      [(await meas(page)).added, a0, (await meas(page)).reason, await page.textContent("#gNote")]);
    box.push({ ramping: false });
    // RESET, AVG 10 s
    await sleep(1500);
    await tapSel(t, page, "#mReset");
    check("resp: RESET empties it", (await meas(page)).cover < 1 && /AVERAGE RESET/.test(await page.textContent("#gNote")));
    await tapSel(t, page, "#mAvg");
    check("resp: AVG 10 s", (await meas(page)).span === 10 && (await page.textContent("#mAvg span")) === "10 s");
    // the subscription's life: RTA view, a sheet, hidden, the box's lapse, leaving MEASURE
    box.clearSent();
    await page.locator("#measRow button[data-view=rta]").click(); await sleep(200);
    check("resp: the RTA view = sub {pgm: false}", pgmSubs(box).slice(-1)[0] === false);
    await page.locator("#measRow button[data-view=resp]").click(); await sleep(200);
    await tapSel(t, page, "#setupBtn"); await sleep(100);
    check("resp: a sheet = sub {pgm: false}", pgmSubs(box).slice(-1)[0] === false, pgmSubs(box));
    await tapSel(t, page, "#shX"); await sleep(150);
    await setHidden(page, true); await sleep(150);
    await setHidden(page, false); await sleep(200);
    check("resp: hidden = false, visible = true", pgmSubs(box).slice(-3).join() === "true,false,true", pgmSubs(box));
    const n1 = pgmSubs(box).length;
    box.setPgm(false);
    box.ws.send(JSON.stringify({ t: "unsub", what: "pgm", why: "idle" }));
    await sleep(250);
    check("resp: the box's unsub {what: pgm} while shown = subscribe again (and the rta one is untouched)", pgmSubs(box).length === n1 + 1 && pgmSubs(box).slice(-1)[0] === true && box.pgmSub);
    await tapSel(t, page, "#measBtn"); await sleep(150);
    check("resp: leaving MEASURE = sub {pgm: false}", pgmSubs(box).slice(-1)[0] === false && !box.pgmSub);
    // audio never leaves the phone
    const sent = box.sent.map((m) => { const x = Object.assign({}, m); delete x.at; return x; });
    const types = new Set(sent.map((m) => m.t));
    const subsOk = sent.filter((m) => m.t === "sub").every((m) => Object.keys(m).sort().join() === "id,pgm,t" || Object.keys(m).sort().join() === "id,rta,t");
    const big = sent.filter((m) => JSON.stringify(m).length > 200);
    check("mic: audio never leaves the phone: only ping and sub {rta | pgm} went out here, every frame small",
      [...types].every((x) => ["ping", "sub", "mute"].includes(x)) && subsOk && !big.length, { types: [...types], big: big.slice(0, 2) });
    const other = t.reqs.filter((u) => !u.startsWith("GET " + web.url + "/v2/") && !u.startsWith("GET " + web.url + "/icons/"));
    check("mic: no request other than the page's own files (GET /v2/…)", !other.length, other.slice(0, 6));
    // a link lost for long (10 s) stops the tracks
    await popPick(t, page, "mic");
    await until(page, () => K1000v2.mic().state === "on");
    box.accept = false; box.close();
    await sleep(4000);
    const still = (await mic(page)).state;
    const offLost = await until(page, () => K1000v2.mic().state === "off", null, 9000);
    check("mic: a short link loss keeps the line; one of 10 s stops the tracks", still === "on" && offLost && (await tracksEnded(page)), still);
    box.accept = true;
    check("mic: the link back: restarted, no prompt", (await until(page, () => K1000v2.linked() && K1000v2.mic().state === "on", null, 8000)) && (await mic(page)).gum.slice(1).every((x) => x.from === "auto"));
  } finally {
    await t.close();
    await bA.close();
  }
});

// B: a 1 kHz tone in pink noise, the permission granted by the browser: the mic's own PEAK, and the
// notch helper on it through the same gestures and the same `grab` message as the box's PEAK
await run("micpeak", async () => {
  const bB = await micBrowser(WAV.tone);
  const t = await open({ browser: bB, tag: "micpeak", prefs: { rta: "mic", msmooth: 6 } });
  const { page, box, f } = t;
  try {
    check("micpeak: granted by the browser (Permissions API): the line starts on LIVE with no prompt possible",
      (await until(page, () => K1000v2.mic().state === "on")) && (await mic(page)).perm === "granted");
    await sleep(2500);
    check("micpeak: no PEAK while the program is absent (the meters' input at −60 dBFS)", (await mic(page)).peak === null && (await mic(page)).prog === false);
    box.meters({ i: [-18, -19] });
    const found = await until(page, () => !!K1000v2.mic().peak, null, 6000);
    const pk = (await mic(page)).peak;
    check("micpeak: a 1 kHz tone -> the mic PEAK at ~1 kHz (within 1/24 oct), after its persistence", found && Math.abs(Math.log2(pk / 1000)) <= 1 / 24, pk);
    check("micpeak: the ▼ tag reads ▼ MIC 1.00k (the bin refined)", await until(page, () => /▼ MIC (99[5-9]|1\.0[01]k)$/.test(document.querySelector("#peakTag").textContent) && !document.getElementById("peakTag").hidden, null, 2000),
      await page.textContent("#peakTag"));
    await shot(page, "mic-peak");
    box.clearSent();
    const tg = await center(page, "#peakTag");
    await f.tap(tg.x, tg.y, 80); await sleep(150);
    check("micpeak: ▼ MIC tapped does nothing", !box.ofType("grab").length);
    await f.hold(tg.x, tg.y, 700); await sleep(300);
    let gr = box.ofType("grab");
    check("micpeak: ▼ MIC held = grab {hz: the mic PEAK} (the box's helper, silent)", gr.length === 1 && Math.abs(Math.log2(gr[0].hz / 1000)) <= 1 / 24 && !box.ofType("band").length && !box.ofType("nudge").length, gr);
    check("micpeak: the toast says MIC PEAK, the GAIN steps open", (await toasts(page)).some((x) => x.startsWith("MIC PEAK · ")) && (await ui(page)).field === "gain");
    await tapSel(t, page, "#stepper .sx");
    box.clearSent();
    const mk = await page.evaluate(() => {
      const cv = document.getElementById("cv").getBoundingClientRect(), hz = K1000v2.mic().peak;
      return { x: cv.left + 24 + Math.log10(hz / 20) / 3 * (cv.width - 28) + 10, y: cv.top + cv.height * 0.8 };
    });
    await f.hold(mk.x, mk.y, 750); await sleep(300);
    gr = box.ofType("grab");
    check("micpeak: a long-press on the PEAK marker (10 px off) = the same grab {hz}", gr.length === 1 && Math.abs(Math.log2(gr[0].hz / 1000)) <= 1 / 24, gr);
    await tapSel(t, page, "#stepper .sx");
    box.push({ muted: true, hold: "unmute", mute_src: "B4", mute_seq: box.state.mute_seq + 1 });
    check("micpeak: muted (the program gate shut): the PEAK goes within its release", await until(page, () => K1000v2.mic().peak === null, null, 2000));
    box.push({ muted: false, hold: "mute", mute_src: "" });
    // the PEAK pressed is the one at TOUCH-DOWN. The music drops just before a long-press on the
    // marker: the mic PEAK goes mid-hold (0.5 s after the gate shuts). Still grab {hz ≈ 1 kHz},
    // never the box's grab {} (the box's own PEAK, 3.17k here, not even shown in PHONE MIC)
    for (const lead of [150, 250]) {
      box.meters({ i: [-18, -19] });
      await until(page, () => !!K1000v2.mic().peak, null, 8000);
      await sleep(500);
      box.clearSent();
      const at = await page.evaluate(() => {
        const cv = document.getElementById("cv").getBoundingClientRect(), hz = K1000v2.mic().peak;
        return { x: cv.left + 24 + Math.log10(hz / 20) / 3 * (cv.width - 28) + 6, y: cv.top + cv.height * 0.8 };
      });
      box.meters({ i: [-70, -70] });
      await sleep(lead);
      await f.hold(at.x, at.y, 750); await sleep(300);
      const g = box.ofType("grab"), gone = (await mic(page)).peak === null;
      check(`micpeak: the music drops ${lead} ms before a long-press on the mic PEAK marker (the PEAK goes mid-hold): still grab {hz ≈ 1 kHz}, never grab {}`,
        gone && g.length === 1 && typeof g[0].hz === "number" && Math.abs(Math.log2(g[0].hz / 1000)) <= 1 / 24, { g, gone });
      if ((await ui(page)).field) await tapSel(t, page, "#stepper .sx");
    }
    // the ▼ tag too: in BOTH, the mic line stops mid-hold (its track ended) and the tag turns into
    // the box's ▼ PEAK under the finger: the hold still grabs the mic PEAK it began on
    await popPick(t, page, "both");
    box.meters({ i: [-18, -19] });
    const onMic = await until(page, () => /▼ MIC/.test(document.querySelector("#peakTag").textContent) && !document.getElementById("peakTag").hidden, null, 8000);
    box.clearSent();
    const tg2 = await center(page, "#peakTag");
    await page.evaluate(() => setTimeout(() => { const st = window.__streams[window.__streams.length - 1]; st.getAudioTracks()[0].dispatchEvent(new Event("ended")); }, 250));
    await f.hold(tg2.x, tg2.y, 700); await sleep(300);
    const g2 = box.ofType("grab"), tagNow = await page.textContent("#peakTag");
    check("micpeak: BOTH, the mic stops mid-hold on ▼ MIC (the tag turns into the box's ▼ PEAK under the finger): still grab {hz ≈ 1 kHz}",
      onMic && /▼ PEAK 3\.17k/.test(tagNow) && g2.length === 1 && typeof g2[0].hz === "number" && Math.abs(Math.log2(g2[0].hz / 1000)) <= 1 / 24, { onMic, g2, tagNow });
    if ((await ui(page)).field) await tapSel(t, page, "#stepper .sx");
    await popPick(t, page, "box");
    check("micpeak: back on BOX the tag is the box's PEAK again", await until(page, () => /▼ PEAK 3\.17k/.test(document.querySelector("#peakTag").textContent), null, 3000));
    box.clearSent();
    const tb = await center(page, "#peakTag");
    await f.hold(tb.x, tb.y, 700); await sleep(300);
    gr = box.ofType("grab");
    check("micpeak: and its hold is the box's grab {} (no hz), unchanged", gr.length === 1 && !("hz" in gr[0]), gr);
  } finally {
    await t.close();
    await bB.close();
  }
});

// D: the fake mic hears the pink noise through a known tilt (+2 dB/oct), the box's program is the
// plain pink: RESPONSE shows that tilt
await run("resptilt", async () => {
  const bD = await micBrowser(WAV.tilt);
  const t = await open({ browser: bD, tag: "resptilt", pgmWav: WAV.pink, prefs: { rta: "box", avg: 20, mview: "resp" } });
  const { page } = t;
  try {
    await tapSel(t, page, "#measBtn");
    const full = await until(page, () => K1000v2.measure().cover >= 20, null, 30000);
    const r = await meas(page);
    const pts = r.line ? r.line.f.map((x, i) => [Math.log2(x / 1000), r.line.db[i]]).filter(([o, d]) => d !== null && o >= Math.log2(0.2) && o <= Math.log2(5)) : [];
    const n = pts.length, sx = pts.reduce((a, p) => a + p[0], 0), sy = pts.reduce((a, p) => a + p[1], 0);
    const sxx = pts.reduce((a, p) => a + p[0] * p[0], 0), sxy = pts.reduce((a, p) => a + p[0] * p[1], 0);
    const slope = (n * sxy - sx * sy) / (n * sxx - sx * sx), icpt = (sy - slope * sx) / n;
    const resid = Math.max(...pts.map(([o, d]) => Math.abs(d - (icpt + slope * o))));
    check("resptilt: a +2 dB/oct tilt on the mic shows as 2 ± 0.3 dB/oct (200 Hz-5 kHz), within ±1 dB of that line", full && Math.abs(slope - 2) <= 0.3 && resid <= 1,
      { slope: slope.toFixed(2), resid: resid.toFixed(2), n });
    await shot(page, "resp-tilt");
  } finally {
    await t.close();
    await bD.close();
  }
});

// E: an 8 kHz tone the box's rule sees on the engine's numbers: the mic PEAK runs on them too
await run("micpeakhf", async () => {
  const bE = await micBrowser(WAV.tone8k);
  const t = await open({ browser: bE, tag: "micpeakhf", prefs: { rta: "mic", msmooth: 6 } });
  const { page, box } = t;
  try {
    const on = await until(page, () => K1000v2.mic().state === "on");
    box.meters({ i: [-18, -19] });
    const found = await until(page, () => !!K1000v2.mic().peak, null, 8000);
    const pk = (await mic(page)).peak;
    check("micpeakhf: an 8 kHz tone 7 dB over its 1/24 oct band -> the mic PEAK at 8 kHz (the box's rule on the engine's estimator, as the box; the analyser's mean power needed 10 dB)",
      on && found && Math.abs(Math.log2(pk / 8000)) <= 1 / 24, pk);
  } finally {
    await t.close();
    await bE.close();
  }
});

// F: the phone clipping (next to the PA): RESPONSE keeps nothing and says so; LIVE says it too
await run("respclip", async () => {
  const bF = await micBrowser(WAV.clip);
  const t = await open({ browser: bF, tag: "respclip", pgmWav: WAV.pink, prefs: { rta: "box", avg: 20, mview: "resp" } });
  const { page } = t;
  try {
    await tapSel(t, page, "#measBtn");
    await until(page, () => K1000v2.measure().frames > 4 && K1000v2.mic().state === "on", null, 8000);
    await sleep(1500);
    const r = await meas(page), note = await page.textContent("#gNote");
    check("respclip: a clipping mic pauses RESPONSE (no window kept): PAUSED · MIC CLIPPING · too loud for the phone: step back from the speakers",
      r.frames > 4 && r.added === 0 && r.reason === "MIC CLIPPING" && /^PAUSED · MIC CLIPPING · too loud for the phone: step back from the speakers · 0 s of 20$/.test(note), [r.frames, r.added, r.reason, note]);
    await tapSel(t, page, "#measBtn");
    await popPick(t, page, "mic");
    check("respclip: LIVE with the mic line says MIC CLIPPING too",
      await until(page, () => /^MIC CLIPPING · too loud for the phone/.test(document.getElementById("gNote").textContent) && !document.getElementById("gNote").hidden, null, 5000), await page.textContent("#gNote"));
  } finally {
    await t.close();
    await bF.close();
  }
});

// C: the mic refused (no fake UI: Chromium dismisses the dialog): BLOCKED, said, never asked again outside SETUP
await run("micdeny", async () => {
  const bC = await micBrowser(WAV.pink, false);
  const t = await open({ browser: bC, tag: "micdeny", prefs: { rta: "mic" } });
  const { page } = t;
  try {
    await tapSel(t, page, "#setupBtn");
    await page.locator("#shTabs button.me").click(); await sleep(150);
    const row = '#shBody [data-row="phone_mic"]';
    await page.locator(row + " button.act").click();
    await until(page, () => K1000v2.mic().perm === "denied");
    await sleep(200);
    const txt = await page.textContent(row);
    check("micdeny: refused: the row reads BLOCKED and says where to allow it, the key TRY AGAIN",
      (await page.textContent(row + " .rv")) === "BLOCKED" && /MIC BLOCKED: /.test(txt) && (await page.textContent(row + " button.act")) === "TRY AGAIN", txt);
    await tapSel(t, page, "#shX");
    await sleep(400);
    check("micdeny: LIVE says MIC BLOCKED: SETUP › THIS PHONE", /MIC BLOCKED: SETUP › THIS PHONE/.test(await page.textContent("#gNote")), await page.textContent("#gNote"));
    await popPick(t, page, "both");
    await tapSel(t, page, "#measBtn"); await tapSel(t, page, "#mMic"); await sleep(300);
    check("micdeny: nothing asks again outside SETUP (one getUserMedia in all)", (await gum(page)) === 1 && (await toasts(page)).includes("MIC BLOCKED: SETUP › THIS PHONE"));
  } finally {
    await t.close();
    await bC.close();
  }
});

// G: the APK's WebView (android/BRIDGE.md, "The microphone", app 0.2.2 on two emulators): Android's
// own mic dialog HIDES the page (visibilitychange) while it is up, and getUserMedia resolves 0.3 to
// 0.5 s after the page is visible again. The pending request must survive that: no error, no
// "denied", the grant kept; LIVE then starts the mic by itself, no second dialog. Simulated here:
// the page hidden at the request, visible 0.5 s later, the answer 0.4 s after that.
await run("micdialog", async () => {
  const bG = await micBrowser(WAV.pink);
  const t = await open({ browser: bG, tag: "micdialog", permPrompt: true, prefs: { rta: "mic" } });
  const { page } = t;
  try {
    await page.evaluate(() => {
      const md = navigator.mediaDevices, orig = md.getUserMedia.bind(md);
      const vis = (hd) => { Object.defineProperty(document, "hidden", { get: () => hd, configurable: true }); document.dispatchEvent(new Event("visibilitychange")); };
      window.__dialog = [];
      let first = true;
      md.getUserMedia = (c) => {
        if (!first) return orig(c);
        first = false;
        vis(true); window.__dialog.push("hidden");                   // Android's dialog covers the WebView
        return new Promise((res, rej) => setTimeout(() => {
          vis(false); window.__dialog.push("visible");               // "While using the app"
          setTimeout(() => { window.__dialog.push("answered"); orig(c).then(res, rej); }, 400);
        }, 500));
      };
    });
    await tapSel(t, page, "#setupBtn");
    await page.locator("#shTabs button.me").click(); await sleep(150);
    const row = '#shBody [data-row="phone_mic"]';
    await page.locator(row + " button.act").click();
    const got = await until(page, () => window.__dialog.length === 3 && K1000v2.mic().session === true, null, 4000);
    await sleep(300);
    const m = await mic(page);
    check("micdialog: hidden while Android's dialog is up, the request survives: granted, no error, not denied",
      got && m.session === true && m.err === "" && m.perm !== "denied" && m.state === "off" && (await page.textContent(row + " .rv")) === "ON",
      { dialog: await page.evaluate(() => window.__dialog), perm: m.perm, session: m.session, err: m.err, state: m.state, row: await page.textContent(row + " .rv") });
    await tapSel(t, page, "#shX");
    const on = await until(page, () => K1000v2.mic().state === "on" && K1000v2.mic().frames > 3);
    const g = (await mic(page)).gum;
    check("micdialog: back on LIVE the mic starts by itself (one more getUserMedia, automatic: no second dialog)",
      on && g.length === 2 && g[0].from === "setup" && g[1].from === "auto" && (await liveTracks(page)) === 1, g);
  } finally {
    await t.close();
    await bG.close();
  }
});
try { fs.rmSync(LABTMP, { recursive: true, force: true }); } catch (e) { /* fine */ }

// ================================================================== no console errors, anywhere
check("no console errors (CSP included)", !consoleErrors.length, consoleErrors.slice(0, 12));

await browser.close();
await web.close();
const ok = checks.every((c) => c.ok);
console.log(JSON.stringify({ ok, n: checks.length, failed: checks.filter((c) => !c.ok).length, checks }));
process.exit(ok ? 0 : 1);

// SPDX-License-Identifier: GPL-3.0-or-later
/* The v2 page against the REAL box server (k1000.web.server.PhoneServer with the real
   Controller on S1's in-memory engine), started by test_web_v2_page.py:
     node box/tools/v2lab/real.mjs http://127.0.0.1:<port>/v2/ [<pink.wav>]
   The real CSP, the real build stamp, the real /v2/ws and its acks: hello and state render, a
   stepper nudge and a band IN/OUT make the round trip, the service worker installs at /v2/, the
   box spectrum's frames arrive.
   With a WAV (the noise the test's fake engine plays as the program): RESPONSE end to end. Full
   Chromium whose fake mic plays that WAV, ≋ MEASURE › RESPONSE: the box's real `pgm` windows
   (its spectrum task, its accumulator, its encoding) are received and kept by the page, the
   coverage grows window by window, and the line reads flat (the same noise on both sides).
   Prints {ok, checks, master, band, resp} as JSON; exit 1 on a failure. */

import { center, finger, loadPlaywright, sleep } from "./fakebox.mjs";

const url = process.argv[2];
const wav = process.argv[3] || "";
const { chromium } = loadPlaywright();
const checks = [];
const check = (name, ok, detail = "") => checks.push({ name, ok: !!ok, detail: ok ? "" : JSON.stringify(detail) });
const errors = [];

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 393, height: 851 }, deviceScaleFactor: 2.75, isMobile: true, hasTouch: true });
const page = await ctx.newPage();
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push("pageerror " + e.message));
let out = {};
try {
  await page.goto(url);
  await page.waitForFunction(() => window.K1000v2 && K1000v2.linked(), null, { timeout: 8000 });
  const h = await page.evaluate(() => ({ build: K1000v2.build, hello: K1000v2.hello(), state: K1000v2.state() }));
  check("real: hello v2 from the box", h.hello.v === 2 && Array.isArray(h.hello.setup) && !!h.hello.fields.master_db);
  check("real: the page's build stamp is the box's v2 build (no reload)", h.build === h.hello.build && /^[0-9a-f]{12}$/.test(h.build), h);
  const fmt = (v) => (Math.abs(v) < 0.05 ? "0.0" : (v > 0 ? "+" : "−") + Math.abs(v).toFixed(1));
  const m0 = h.state.master_db;
  check("real: the state renders (MASTER)", (await page.textContent("#foot .tile .tv span")) === fmt(m0), { m0, shown: await page.textContent("#foot .tile .tv span") });
  check("real: the band row follows hello.order", (await page.$$eval("#bands .chip span", (s) => s.map((x) => x.textContent))).join(" ") === h.hello.order.map((s) => h.hello.short[s]).join(" "));
  const f = await finger(page);
  const mt = await center(page, "#foot .tile");
  await f.tap(mt.x, mt.y); await sleep(200);
  const key = page.locator("#stepper button", { hasText: "−½" }).first();
  const kb = await key.boundingBox();
  await f.tap(kb.x + kb.width / 2, kb.y + kb.height / 2);
  await page.waitForFunction((v) => K1000v2.state().master_db === v, m0 - 0.5, { timeout: 4000 });
  await sleep(600);
  out.master = await page.evaluate(() => K1000v2.state().master_db);
  check("real: a stepper nudge makes the round trip (state and tile)", out.master === m0 - 0.5 && (await page.textContent("#foot .tile .tv span")) === fmt(m0 - 0.5), out);
  // a band IN/OUT: close the steps (the label cell), then hold the P3 chip 0.6 s (protocol band 2)
  const sx = await center(page, "#stepper .sx");
  await f.tap(sx.x, sx.y); await sleep(200);
  const chip = await center(page, '#bands .chip[data-s="2"]');
  await f.hold(chip.x, chip.y, 750);
  await page.waitForFunction(() => K1000v2.state().bands[2].on === false, null, { timeout: 4000 });
  out.band = await page.evaluate(() => K1000v2.state().bands[2]);
  check("real: a chip hold switches the band OUT on the box", out.band.on === false && (await page.getAttribute('#bands .chip[data-s="2"]', "class")).includes("out"));
  check("real: the spectrum subscription was asked for", (await page.evaluate(() => K1000v2.stats.t.sub || 0)) >= 1);
  if (wav) {
    let live = true;
    try { await page.waitForFunction(() => K1000v2.rta().frames > 2 && K1000v2.rta().st === "live", null, { timeout: 6000 }); } catch (e) { live = false; }
    check("real: the box spectrum's frames arrive, live (the spectrum task on the fake engine)", live, await page.evaluate(() => K1000v2.rta()));
  }
  // the v2 service worker: scope /v2/, and the page still loads through it
  const reg = await page.evaluate(async () => {
    if (!("serviceWorker" in navigator)) return "no sw";
    const r = await Promise.race([navigator.serviceWorker.ready, new Promise((res) => setTimeout(() => res(null), 5000))]);
    return r ? r.scope : "not ready: " + K1000v2.sw();
  });
  check("real: the service worker is registered with scope /v2/", typeof reg === "string" && reg.endsWith("/v2/"), reg);
  await page.reload();
  await page.waitForFunction(() => window.K1000v2 && K1000v2.linked(), null, { timeout: 8000 });
  const ctl = await page.evaluate(() => !!navigator.serviceWorker && !!navigator.serviceWorker.controller);
  check("real: reloaded under the worker, linked again", ctl);
} catch (e) {
  check("real: ran", false, String(e && e.stack || e));
}
await browser.close();

// ------------------------------------------------------------------ RESPONSE, end to end
const lineSpread = (ln, lo, hi) => {
  const v = ln.f.map((f, i) => [f, ln.db[i]]).filter(([f, d]) => f >= lo && f <= hi && d != null).map((x) => x[1]).sort((a, b) => a - b);
  const med = v[v.length >> 1];
  return { med, lo: v[0] - med, hi: v[v.length - 1] - med, n: v.length };
};
if (wav) {
  const mb = await chromium.launch({ channel: "chromium",
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${wav}`] });
  try {
    const mctx = await mb.newContext({ viewport: { width: 393, height: 851 }, deviceScaleFactor: 2.75, isMobile: true, hasTouch: true });
    // a phone that allowed the mic before (Chromium's fake UI grants it), RESPONSE chosen, AVG 10 s
    await mctx.addInitScript(() => { try { localStorage.setItem("k1000v2.prefs", JSON.stringify({ rta: "box", avg: 10, mview: "resp" })); } catch (e) { /* fine */ } });
    const mp = await mctx.newPage();
    mp.on("console", (m) => { if (m.type() === "error") errors.push("mic: " + m.text()); });
    mp.on("pageerror", (e) => errors.push("mic: pageerror " + e.message));
    await mp.goto(url);
    await mp.waitForFunction(() => window.K1000v2 && K1000v2.linked(), null, { timeout: 8000 });
    const hp = await mp.evaluate(() => K1000v2.hello().pgm);
    check("real/resp: hello.pgm is the box's (v 1, 264 bins 10 Hz-20 kHz, 48 kHz, FFT 8192, peak, 0.5 s)",
      JSON.stringify(hp) === JSON.stringify({ v: 1, n: 264, f0: 10.0, f1: 20000.0, rate: 48000, fft: 8192, est: "peak", win_s: 0.5 }), hp);
    const mf = await finger(mp);
    const mb2 = await center(mp, "#measBtn");
    await mf.tap(mb2.x, mb2.y); await sleep(200);
    let ok = true;
    try { await mp.waitForFunction(() => K1000v2.measure().subscribed && K1000v2.mic().state === "on", null, { timeout: 6000 }); } catch (e) { ok = false; }
    check("real/resp: ≋ MEASURE › RESPONSE: sub {pgm: true} and the phone mic runs", ok, [await mp.evaluate(() => K1000v2.measure()), await mp.evaluate(() => K1000v2.mic())]);
    ok = true;
    try { await mp.waitForFunction(() => K1000v2.measure().added >= 2, null, { timeout: 8000 }); } catch (e) { ok = false; }
    let r = await mp.evaluate(() => K1000v2.measure());
    check("real/resp: the box's pgm windows arrive and the page keeps them (live, prog true: nothing pauses it)", ok && r.frames >= 2 && r.reason === "" && r.lastReason === "",
      { frames: r.frames, added: r.added, dropped: r.dropped, reason: r.reason, lastReason: r.lastReason });
    const c0 = r.cover;
    await sleep(3000);
    r = await mp.evaluate(() => K1000v2.measure());
    check("real/resp: the coverage grows, about 1 s a second (2 windows of 0.5 s)", r.cover - c0 >= 2 && r.cover - c0 <= 3.6, { c0, c1: r.cover, added: r.added });
    ok = true;
    try { await mp.waitForFunction(() => K1000v2.measure().cover >= 9, null, { timeout: 20000 }); } catch (e) { ok = false; }
    r = await mp.evaluate(() => K1000v2.measure());
    const rs = r.line ? lineSpread(r.line, 200, 5000) : null;
    out.resp = { frames: r.frames, added: r.added, dropped: r.dropped, cover: Math.round(r.cover * 10) / 10, spread: rs };
    check("real/resp: the average fills to 9 s of 10", ok && r.added >= 18, out.resp);
    check("real/resp: the same pink noise as program and on the mic -> RESPONSE flat within ±1.5 dB, 200 Hz-5 kHz (the box's estimator = the page's)",
      rs && rs.lo >= -1.5 && rs.hi <= 1.5 && Math.abs(rs.med) <= 0.5, rs);
    const note = await mp.textContent("#gNote");
    check("real/resp: the graph says the coverage (RESPONSE · n s of 10 · uncalibrated mic − program)", /^RESPONSE · (9|10) s of 10 · uncalibrated mic − program$/.test(note), note);
    // leaving MEASURE: sub {pgm: false}
    await mf.tap(mb2.x, mb2.y); await sleep(400);
    check("real/resp: leaving MEASURE unsubscribes (sub {pgm: false})", !(await mp.evaluate(() => K1000v2.measure().subscribed)) && (await mp.evaluate(() => K1000v2.mic().state)) === "off");
  } catch (e) {
    check("real/resp: ran", false, String(e && e.stack || e));
  }
  await mb.close();
}
check("real: no console errors under the box's real CSP", !errors.length, errors);
const ok = checks.every((c) => c.ok);
console.log(JSON.stringify(Object.assign({ ok, checks }, out)));
process.exit(ok ? 0 : 1);

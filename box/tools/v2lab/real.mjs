// SPDX-License-Identifier: GPL-3.0-or-later
/* The v2 page against the REAL box server (k1000.web.server.PhoneServer with the real
   Controller on S1's in-memory engine), started by test_web_v2_page.py:
     node box/tools/v2lab/real.mjs http://127.0.0.1:<port>/v2/
   The real CSP, the real build stamp, the real /v2/ws and its acks: hello and state render, a
   stepper nudge and a band IN/OUT make the round trip, the service worker installs at /v2/.
   Prints {ok, checks, master, band} as JSON; exit 1 on a failure. */

import { center, finger, loadPlaywright, sleep } from "./fakebox.mjs";

const url = process.argv[2];
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
check("real: no console errors under the box's real CSP", !errors.length, errors);
await browser.close();
const ok = checks.every((c) => c.ok);
console.log(JSON.stringify(Object.assign({ ok, checks }, out)));
process.exit(ok ? 0 : 1);

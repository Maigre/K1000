// SPDX-License-Identifier: GPL-3.0-or-later
/* K1000 first-time page: the two ways in, each one button. The K1000 app (Android, when the box
   holds its APK: no certificate, it joins the Wi-Fi by itself) and the web app (any phone: the v2
   page, then "add to home screen"; it needs the K1000 certificate once). The page checks whether
   this phone already trusts the certificate (an HTTPS fetch to the box only succeeds when it
   does) and shows the certificate steps only if not, for Android or for an iPhone.

   Android's "Sign in to network" window is a WebView that trusts SYSTEM certificates only: there
   the check would always fail, even with the K1000 CA installed. So in that window no verdict is
   given; the page explains how to get to Chrome instead (which trusts user CAs).

   The app card is in the page only when the box has an APK to offer (the box puts a "no app on
   this box" card there otherwise). It is hidden where it cannot work: on an iPhone, in that
   sign-in window (Chrome installs it), and on a box without HTTPS (the app only talks HTTPS). */
"use strict";
(() => {
  const d = document.body.dataset;
  const $ = (id) => document.getElementById(id);
  const check = $("check");
  const text = $("check-text");
  const ca = $("ca");
  const open = $("open");
  const app = $("app");
  const noapp = $("noapp");
  const ua = navigator.userAgent || "";
  const ios = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  document.body.classList.add(ios ? "ios" : "android");     // portal.css shows that phone's steps
  if (ios) {                                                 // an APK is Android's
    if (app) app.hidden = true;
    if (noapp) noapp.hidden = true;
  }
  const appShown = !!app && !app.hidden;
  if (!d.on) $("off").hidden = false;
  if (!d.ca) {
    $("ca-link").replaceWith(document.createTextNode("(the certificate is not on this box)"));
  }
  if (!d.tls || !d.ping) {
    check.className = "check warn";
    text.textContent = "This box has no HTTPS certificate: the web app works, but cannot be installed as an app.";
    ca.hidden = true;
    if (app) app.hidden = true;
    open.classList.add("primary");
    return;
  }
  // "; wv)" = an Android WebView (the captive-portal sign-in window is one), not Chrome
  if (/; wv\)/.test(ua)) {
    check.hidden = true;
    open.hidden = true;
    if (app) app.hidden = true;
    if (noapp) noapp.hidden = true;
    $("web").hidden = true;
    $("signin").hidden = false;
    $("here").textContent = location.origin + "/";
    return;
  }
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 3500);
  fetch(d.ping, { mode: "cors", cache: "no-store", signal: ctrl.signal })
    .then((r) => {
      clearTimeout(t);
      if (!r.ok) throw new Error(String(r.status));
      check.className = "check ok";
      text.textContent = "This phone trusts the K1000 certificate ✓";
      ca.hidden = true;
      open.classList.add("primary");
    })
    .catch(() => {
      clearTimeout(t);
      check.className = "check warn";
      text.textContent = "Needs the K1000 certificate once: the steps are below.";
      if (!appShown) {
        const link = $("ca-link");
        if (link) link.classList.add("primary");
      }
    });
})();

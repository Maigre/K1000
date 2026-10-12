// SPDX-License-Identifier: GPL-3.0-or-later
/* K1000 captive portal: says whether this phone already trusts the K1000 certificate (an HTTPS
   fetch to the box only succeeds when it does), and shows the one-time setup if not.

   Android's "Sign in to network" window is a WebView that trusts SYSTEM certificates only: there
   the check would always fail, even with the K1000 CA installed. So in that window no verdict is
   given; the page explains how to get to Chrome instead (which trusts user CAs). */
"use strict";
(() => {
  const d = document.body.dataset;
  const check = document.getElementById("check");
  const text = document.getElementById("check-text");
  const ca = document.getElementById("ca");
  const open = document.getElementById("open");
  if (!d.on) document.getElementById("off").hidden = false;
  if (!d.ca) {
    document.getElementById("ca-link").replaceWith(document.createTextNode("(the certificate is not on this box)"));
  }
  if (!d.tls || !d.ping) {
    check.className = "card warn";
    text.textContent = "This box has no HTTPS certificate: the page works, but cannot be installed as an app.";
    ca.hidden = true;
    return;
  }
  // "; wv)" = an Android WebView (the captive-portal sign-in window is one), not Chrome
  if (/; wv\)/.test(navigator.userAgent)) {
    check.hidden = true;
    open.hidden = true;
    document.getElementById("signin").hidden = false;
    document.getElementById("here").textContent = location.origin + "/";
    return;
  }
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 3500);
  fetch(d.ping, { mode: "cors", cache: "no-store", signal: ctrl.signal })
    .then((r) => {
      clearTimeout(t);
      if (!r.ok) throw new Error(String(r.status));
      check.className = "card ok";
      text.textContent = "This phone trusts the K1000 certificate ✓";
      ca.hidden = true;
    })
    .catch(() => {
      clearTimeout(t);
      check.className = "card warn";
      text.textContent = "This phone does not trust the K1000 certificate yet: install it once (below).";
      open.classList.remove("primary");
    });
})();

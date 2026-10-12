# 06 — The phone: K1000's own Wi-Fi, QR codes, captive portal, and an installable page (2026-10-12)

Decision: **D-007** (Thomas, 2026-10-12: "make the phone interface a PWA so I have a quick way in").
The knobs stay the primary surface. The phone is second, and it is **gated**: go/no-go at the
Friday 16/10 rig test (checklist in §9). If it is not proven there, it is switched off for Sunday,
**the access point included** (§9: `setup-ap.sh --down` + `phone: enabled: false`), and the knobs
carry the show.

**Defaults** (2026-10-12, refining D-007; each one is a single `setup-ap.sh` option to change):

- **AP backend: hostapd, PMF off** (`ieee80211w` left unset), `wlan0` unmanaged by NetworkManager.
  Why: the hub's verified knowledge drawer `rpi-internal-wifi-ap-hostapd` ("RPi internal Wi-Fi as
  an AP — hostapd, not wpa_supplicant, and PMF off (BCM4345/43455)"): NetworkManager's
  wpa_supplicant AP on the Pi's chip drops Apple/older-Android clients ~6 s after the handshake.
  `--backend nm` stays available.
- **Captive portal off** (the AP's DNS answers `k1000.home.arpa` only): Android then offers *Stay
  connected* and keeps auto-joining K1000 after a dropout (§5). The PHONE page's third QR,
  **FIRST TIME** (`http://10.42.0.1/`), covers the one-time certificate install.
- **5 GHz channel 36, country FR**; 2.4 GHz (`--band bg`, channel 6) is the documented fallback.
  Thomas stands up to ~10 m from the box.
- **The phone may unmute**, with the stale-unmute protection (`mute_seq`, §2). **SSH and
  CamillaGUI are unreachable from the AP** (the fence, §1).
- **A Friday no-go means the AP fully off** (§9), not just the page, and it stays off: later
  `install.sh --ap` / `bootstrap.sh` runs keep it down until `setup-ap.sh --up`.
- **The AP never shares its subnet** (§1): while another interface holds a 10.42.0.x address (a
  laptop on a direct `eth0` cable with NetworkManager's *Shared to other computers*), the AP stays
  down, so SSH over `eth0` keeps working.
- Thomas's phone: a Pixel 4a on Android 13 (probably). A Chrome home-screen shortcut is fine (§3).

## 1. The shape

```
 Android phone ── Wi-Fi "K1000" (WPA2, PMF off, password on the box screen only)
      │            wlan0 · hostapd (NetworkManager leaves wlan0 alone) · 10.42.0.1/24
      │            5 GHz ch 36, country FR (or 2.4 GHz ch 6)
      │            DHCP + DNS for the phones (dnsmasq: only k1000.home.arpa resolves; portal off)
      │            the FENCE: from the AP only DHCP, DNS, :80, :443, :8080 reach the box
      │            (16 connections per phone, 64 in all); 10.42.0.1 unreachable from eth0
      ▼
 k1000.service (python3 -m k1000, the same process as the knobs)
   ├─ https://10.42.0.1/      the K1000 page (installable PWA) + wss://10.42.0.1/ws
   ├─ http://10.42.0.1/       first-time page: "Open K1000" + the certificate download
   │                          (and the captive portal with --portal on)
   └─ Controller  ◄── the knobs drive the same one: same clamps, same click-safe moves, one state
```

- **The box's own access point**, so there is no house network to depend on. `eth0` is not
  touched. Set up by `box/net/setup-ap.sh` (idempotent; every choice sticks until changed:
  `--band bg`, `--portal on`, `--backend nm`, `--iface wlan1`, `--ssh on`). On the box:
  `sudo /opt/k1000/net/setup-ap.sh`, or `install.sh --ap`.
- **The fence** (`k1000-ap-fence.service`, its own nftables table, loaded at boot before any
  network): a phone on K1000 reaches only DHCP, DNS, the first-time page (80), the phone page
  (443) and its plain fallback (8080), with at most 16 open connections per phone and 64 in all
  to those ports (the k1000 process's sockets are the knobs' too; it also has `LimitNOFILE=4096`).
  Never CamillaGUI (:5005, no authentication: it could undo knob work and lift the volume limit),
  rpcbind, or SSH (unless `--ssh on`). Nothing on the AP is routed anywhere, and the AP address
  10.42.0.1 is unreachable from `eth0` (only that address: an `eth0` network that happens to be
  10.42.0.0/24 too keeps working). The hostapd unit **requires** the fence: it refuses to start
  without its table, and stopping the fence takes the AP down.
- **The subnet guard.** NetworkManager's *Shared to other computers* (a laptop on a direct `eth0`
  cable, the only `eth0` path at a gig without a house network) is 10.42.0.1/24, the AP's own
  subnet, and hands the box a 10.42.0.x address. With the AP up, the laptop's address would be
  local to the box (on `wlan0`) and SSH over `eth0` would be lost. So the AP never runs while
  another interface holds an address in 10.42.0.0/24: `setup-ap.sh` refuses (exit 4, nothing
  changed), the hostapd unit refuses at every start, and the NetworkManager dispatcher script
  `/etc/NetworkManager/dispatcher.d/90-k1000-ap-guard` stops the AP when such an address appears
  later (the cable plugged in after boot) and starts it again once it is gone
  (`journalctl -t k1000-ap-guard`). The box screen says so (*Wi-Fi AP is OFF* · `eth0 10.42.0.x`
  · *is in the AP subnet*). **On the laptop, share on another subnet** (e.g. `ipv4.addresses
  10.43.0.1/24` on its wired connection) or use the house network.
- **Never on a Wi-Fi client link**: when `wlan0` is connected to another Wi-Fi as a client (a card
  set up with Wi-Fi in Raspberry Pi Imager), `setup-ap.sh` refuses (exit 4) instead of cutting
  that link. Leave Wi-Fi empty in Imager.
- **QR codes on the box screen** (SETUP › Phone): first the Wi-Fi code (Android 10+ joins in one
  tap; the password is also printed, easy to type). Once a phone is on the AP the screen shows the
  page's URL. E1 turn steps through the codes: Wi-Fi → URL → **first time** (http://10.42.0.1/,
  the certificate page).
- **The page is K1000's own**, served by the k1000 process (python3-websockets already on the box;
  the new packages are python3-qrcode for the screen codes and hostapd for the AP). It drives the **same Controller
  as the knobs**. It is not CamillaGUI, which POSTs whole configs and can undo knob work.
- **HTTPS, so Android can install it.** A certificate for `10.42.0.1` / `k1000.home.arpa`, signed
  by a small local CA that Thomas installs once on his phone. The CA is **name-constrained**: it
  can only vouch for 10.42.0.0/24 and `k1000.home.arpa`, never for another site. Its private key
  never leaves mgr-ran; the box only holds the leaf certificate and key (`/etc/k1000/tls`).
- **Installed, it opens instantly.** A service worker keeps the page itself on the phone. With no
  link to the box the page still opens and says *Not connected to K1000 — join the K1000 Wi-Fi*.
  After a box update the first launch shows the old page for a second, then reloads itself once.

Screens: `docs/ui/phone_wifi.png` (step 1), `phone_url.png` (step 2), `phone_setup.png` (first
time), `phone_connected.png` (muted box, one phone connected), `phone_off.png`, `phone_ap_down.png`,
`phone_no_tls.png`.

## 2. What the phone does (v0)

| Page | Controls |
|---|---|
| **MAIN** | Master: −6 / −1 / −½ / +½ / +1 dB steps (never an absolute jump). Balance subs↔tops ±½/±1. Lo-Cut on/off + frequency (±1/12 oct, ±⅓ oct) |
| **LIVE EQ** | The 6 live bands (PEQ 1–4, low shelf, high shelf) on a 20 Hz–20 kHz log graph with the summed curve. **Drag a dot** = frequency + gain. **Long-press empty space = "notch here"**: the first flat PEQ band goes there at −6 dB, Q 8, and **stays under the finger**: keep it down and slide to tune it. Per band: FREQ ±1/12, ±⅓ oct · GAIN ±½, ±3 dB · Q ½× (wide) / − / + / 2× (narrow) · hold = band flat. **Reset live EQ** with a confirm |
| Always | **Hold to mute** (0.6 s; unmute needs 1.2 s), the 4 output meters (~10 Hz, clip boxes), engine state, NO DEVICE / ENGINE DOWN / the box's messages, external changes (drift) |

Not on the phone in v0 (setup-class, refused by the box): profile, mono subs, delays, polarity,
system EQ, trims, input trim, sub low-pass.

**The mute, exactly:**

- What a hold does is decided **when the finger goes down**, and the button says it: *HOLD TO
  MUTE*, *MUTED · HOLD TO UNMUTE*, or *HOLD TO KEEP MUTED* (K1000 itself holds Main muted on its
  way in, a boot ramp or a setup guard: like B2, the phone then never unmutes).
- A hold that starts as a MUTE always mutes, whatever happens meanwhile (B2, another phone).
- An UNMUTE carries the box's *mute generation* the page was showing. If anyone muted or unmuted
  during the hold, the hold is cancelled on the phone (*The mute changed meanwhile*) and the box
  refuses it anyway: nobody undoes someone else's emergency mute. The same rule holds for B2 on
  the box (`ui/input_map.py` `_b2`, `Controller.set_mute(seen=)`): B2 acts on what the screen
  showed when it was pressed.
- The phone buzzes when **the box confirms** (*MUTING…* until then); no answer in 1.5 s says
  *No answer from K1000: use B2 on the box*.
- B2 on the box remains the master mute, always.

**Safety, enforced by the box, not only the page:**

- A strict allowlist; values pass through the same `State` clamps as the knobs (live boosts ≤ +6 dB).
- Master moves in steps only: at most −6/+1 dB per message, and **at most +6 dB/s upwards** for
  all phones together. Down is never limited.
- A **mute is never rate-limited**; everything else is limited per phone (30 msg/s, burst 40).
- Drags are coalesced on the phone (one message per 50 ms, the last value always sent) and the box
  only ever sends each phone its **latest** state: a slow phone skips states, never stalls the box.
  The box never waits on a phone before reading its next message (a mute behind a stalled
  downlink is applied at once). A stuck phone is dropped after 2 s. At most **4 phones** at once.
- Nothing a phone sends can raise into the controller or another phone (per-phone error isolation,
  1 KB message limit, JSON only, numbers bounded, Origin checked against the box's own names), and
  a misbehaving phone cannot flood the journal (one traceback per phone, then throttled lines).
- "Notch here" names the slot the page is dragging; the box uses it only while it is still flat.
- The servers listen on 10.42.0.1 only, and **the fence** makes that address unreachable from any
  interface but the AP (Linux would otherwise accept it on `eth0` too). Physical access (the
  password on the box screen) is the authorisation.
- **SETUP › Phone** switches phone control off at run time (and it stays off across restarts:
  `/var/lib/k1000/phone.json`). The page then opens but controls nothing and says so.
  `phone: enabled: false` in `k1000.yaml` removes the phone servers entirely. Neither takes the
  Wi-Fi off the air: that is `sudo /opt/k1000/net/setup-ap.sh --down` (the no-go, §9).

## 3. Thomas's one-time phone setup (at home, before Friday)

1. **Box:** the certificate, the AP and its fence are installed: `box/README.md`, "The phone
   (D-007), once per card" (copies `k1000.crt`, `k1000.key` and `k1000-ca.crt`, never `ca.key`,
   to a temporary folder on the box, runs `install.sh --ap` with `K1000_TLS_DIR` and
   `K1000_REVISION`, removes the folder), then restart the k1000 service between shows so it
   loads the certificate. On the box: SETUP › **Phone**.
2. **Put the CA on the phone first (simplest):** the CA certificate is public. Copy
   `secrets/k1000-ca/k1000-ca.crt` (or `.der`) to the phone over USB, Seafile or a mail to
   yourself, into *Downloads*. (Or later from the box's first-time page, step 4.)
3. **Join the Wi-Fi from Settings:** Settings › Network & internet › Internet › the **⊞ QR icon**
   next to *Add network*, scan the **first QR** on the box screen → *Join*. (Joining from
   Settings matters: Android then asks the "no internet" question below. The password is printed
   under the SSID if the camera will not scan.)
   - Portal **off** (the default, §5): Android says *K1000 has no internet access — Stay
     connected?* → **Yes**, tick **Don't ask again**. K1000 then auto-joins like any network.
   - Portal **on** (`--portal on`): Android shows *Sign in to network* instead (§5).
4. **Install the K1000 certificate (once):**
   - The phone needs a **screen lock** (PIN/pattern): Android refuses certificates without one.
   - If it is not on the phone yet: on the box, E1 turn to the **FIRST TIME** QR (or open
     `http://10.42.0.1/` in **Chrome**), tap *Download the K1000 certificate*; Chrome may say it
     *can't be downloaded securely*: tap **Keep**.
   - Do **not** tap the downloaded file (Android 11+ answers *Can't install CA certificates*).
     Instead: Settings › Security & privacy › More security settings › Encryption & credentials ›
     **Install a certificate › CA certificate** › *Install anyway* › pick `k1000-ca.crt`.
     (Samsung: Settings › Security and privacy › Other security settings › Install from device
     storage › CA certificate.) It must be a **CA certificate**, not "VPN & app user" or "Wi-Fi".
   - Android then shows *Network may be monitored*: expected, harmless here (the CA can only
     vouch for the K1000 box).
5. **Open and install the page:** in Chrome, `http://10.42.0.1/` now says *This phone trusts the
   K1000 certificate ✓* → **Open K1000** (or the second QR, or type `https://10.42.0.1`). Then the
   **Install** button in the page's top bar, or ⋮ › *Add to Home screen* › *Install*.
   What appears depends on Chrome reaching Google's WebAPK server (it cannot from K1000, which
   has no internet): usually a **home-screen shortcut with a small Chrome badge**, not an app in
   the app drawer. That is fine (decided for Thomas's Pixel 4a): it opens full screen,
   offline-first, like the app would.

At the gig: **airplane mode, then Wi-Fi on** → the phone joins K1000 → tap the K1000 icon.

## 4. The first-time page and the captive portal

`http://10.42.0.1/` is always served (k1000, port 80): **Open K1000** + the one-time certificate
steps, and a check whether this phone already trusts the K1000 certificate (an HTTPS request to the
box only succeeds when it does). It also serves the CA at `http://10.42.0.1/k1000-ca.crt`
(`application/x-x509-ca-cert`).

Android's *Sign in to network* window (portal on) is a WebView that trusts **system** certificates
only: there the trust check would always fail, so the page says so instead of giving a verdict,
and asks for ⋮ › **Use this network as is**, then Chrome (which trusts the K1000 CA). That window
has no "Open in browser".

## 5. Captive portal: off (the default) or on

`setup-ap.sh --portal on|off` (sticks). Both serve the same page; only the AP's DNS differs.

| | Portal **on** (`--portal on`, the D-007 bonus) | Portal **off** (the default) |
|---|---|---|
| DNS | every name → 10.42.0.1 | only `k1000.home.arpa`; everything else "no such domain", at once |
| On joining | *Sign in to network* (the first-time page in the WebView) | *No internet — Stay connected?* (joined from Settings) |
| Afterwards | Android sees a portal that never validates: no "Don't ask again" possible; after each **automatic** join it pauses auto-joining K1000 for up to 10 min (Android 13 code). After a dropout mid-show the phone does not come back by itself: Wi-Fi tile off/on, or tap K1000 in Settings | With *Don't ask again*, K1000 is kept and auto-joined like any network, including after a dropout |
| First-time certificate | via the sign-in window → Chrome | the FIRST TIME QR on the box, or the CA put on the phone beforehand |

The portal was the D-007 "bonus"; it costs the main path its automatic reconnection, so it is
**off by default** (2026-10-12). The one thing it gave, the certificate page on joining, is the
PHONE page's third QR (**FIRST TIME**, `http://10.42.0.1/`). Back to the portal, one command:
`sudo /opt/k1000/net/setup-ap.sh --portal on` (and `--portal off` again).

## 6. Range, band, plan B

- The box's metal case is a known problem for the onboard Wi-Fi: `docs/01-approaches-review.md`
  already concluded "metal case kills onboard Wi-Fi → USB dongle with antenna", and the AP has
  never been on air (no range data). Thomas stands **up to ~10 m** from the box. **Measure it
  Friday** (checklist step 6), on both bands: `sudo /opt/k1000/net/setup-ap.sh --band a` /
  `--band bg`, walking 5 / 10 / 20 / 30 m from the box with the screen window facing the room;
  note where the page shows NO LINK. 5 GHz ch 36 is the default; 2.4 GHz usually carries further
  through a case and bodies (the fallback), and 5 GHz ch 36 is **indoor-only** in the EU.
- **Plan B, range:** a USB Wi-Fi dongle with an external antenna:
  `sudo /opt/k1000/net/setup-ap.sh --iface wlan1` (the box screen, the fence and the page follow
  `ap.info`). Hub knowledge: `realtek-usb-wifi-dongles`.
- **Why hostapd** (the default backend): the hub's knowledge drawer `rpi-internal-wifi-ap-hostapd`
  saw NetworkManager's AP mode (wpa_supplicant) on this Broadcom chip complete the WPA2 handshake,
  then drop every Apple / older-Android client before DHCP, ~6 s later; hostapd with PMF **not
  advertised at all** is what worked. install.sh installs `hostapd` (a venue has no internet).
  The first hostapd start after NetworkManager had the chip leaves its firmware stale (every
  association refused): setup-ap.sh starts hostapd twice on that handover, and the unit flushes
  NetworkManager's leftover address first. After a start, setup-ap.sh waits 3 s and requires
  hostapd still running, not restarted meanwhile and `ENABLED` (`hostapd_cli status`); a channel
  or band the chip refuses is exit 4 with the `--band bg` hint, never a silent success. Channels:
  5 GHz 36/40/44/48 (no DFS), 2.4 GHz 1–13. The unit gives up after 8 starts in 2 min and is
  bound to its interface (it waits for a dongle, and stops when it is unplugged). `--backend nm` (NetworkManager's AP, `pmf` disabled)
  stays available for a comparison; back with `--backend hostapd`.
- **Phones that join but drop** (associate, no IP, gone after ~6 s) on the hostapd backend: check
  `sudo /opt/k1000/net/setup-ap.sh --status` (backend hostapd, `ieee80211w` absent from
  `/etc/k1000/hostapd.conf`), then try `--band bg`, then the dongle.

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| *K1000 has no internet access — Stay connected?* | Portal off: **Yes** + **Don't ask again**. It only appears when K1000 was joined from Settings |
| *Sign in to network* | Portal on: the first-time page in Android's WebView. ⋮ › **Use this network as is**, then Chrome / the K1000 icon. (It cannot check the certificate: that is normal) |
| Page says *Not connected to K1000* although the phone was on K1000 | The phone left K1000 (dropout) and did not rejoin: Wi-Fi tile off/on, or tap K1000 in Settings. With mobile data on, Android may route the page to mobile data: **airplane mode + Wi-Fi** |
| No *Sign in to network* (portal on) | **Private DNS** set to a provider hostname (strict mode) bypasses the box's DNS: set it to *Automatic* or *Off*. The page still works by IP (`https://10.42.0.1/`) |
| *Your connection is not private* | The CA is not installed, or was installed as a user/VPN/Wi-Fi certificate: redo §3 step 4 as **CA certificate**. Check Settings › … › *Trusted credentials* › *User*: "K1000 Local CA" |
| Phone shows K1000, *Obtaining IP address…*, then drops | §6: `setup-ap.sh --status` (backend hostapd, PMF unset), then `--band bg`, then the dongle |
| Page says *Not connected to K1000 — join the K1000 Wi-Fi* | Not on K1000 (check the Wi-Fi), or k1000 is restarting (it reconnects by itself within ~3 s) |
| Page says *K1000 was updated: reloading* | The box was updated; the page reloads itself once. If it then says *close and reopen*, swipe K1000 away and open it again |
| Page says *Phone control is OFF on the box* | SETUP › Phone › push E1 |
| *The mute changed meanwhile* | Someone (B2, another phone) muted/unmuted during your hold: look, then hold again |
| *K1000 already has the maximum number of phones* | 4 phones max: close the page on another phone |
| Phone does not list "K1000" | Phone without 5 GHz, or channel 36 blocked by its region: `sudo /opt/k1000/net/setup-ap.sh --band bg` |
| **Outdoor gig** | 5 GHz channel 36 is **indoor-only** in the EU: `sudo /opt/k1000/net/setup-ap.sh --band bg` (2.4 GHz ch 6). The choice sticks until `--band a` |
| Box screen: *Wi-Fi AP is OFF* · *kept down (no-go)* | After `--down`: expected. Back on: `sudo /opt/k1000/net/setup-ap.sh --up` |
| Box screen: *Wi-Fi AP is OFF* · `eth0 10.42.0.x` *is in the AP subnet* | A laptop on the `eth0` cable shares its connection on NetworkManager's 10.42.0.1/24 (§1, the subnet guard). Share on another subnet (10.43.0.1/24) or unplug it: the AP comes back by itself. SSH over `eth0` works meanwhile |
| Box screen: *Wi-Fi AP is OFF* (no reason) | `sudo /opt/k1000/net/setup-ap.sh --status`, then `sudo /opt/k1000/net/setup-ap.sh`; `journalctl -u k1000-ap-hostapd -n 60` (`-u NetworkManager` with `--backend nm`) |
| `setup-ap.sh` exits 4 | It says why: another interface in 10.42.0.0/24, `wlan0` a Wi-Fi client, or hostapd did not stay up (then `--band bg`) |
| Box screen: password unreadable | `/etc/k1000/ap.psk` must be `root:k1000 0640` (setup-ap.sh fixes it) |
| New Wi-Fi password wanted | `sudo /opt/k1000/net/setup-ap.sh --new-password` (every phone scans the QR again) |
| SSH to the box over K1000 | Fenced off by default: `sudo /opt/k1000/net/setup-ap.sh --ssh on` (or use `eth0`, not with a laptop sharing on 10.42.0.x: §1) |
| Certificate expiry | The leaf is valid until **2029-01-14**; the box logs a warning 60 days before. Re-sign it on mgr-ran (`secrets/k1000-ca/README.txt`) |

## 8. Notes and limits

- The fence also drops any forwarding to or from the AP, including what NetworkManager's *shared*
  mode adds with `--backend nm` (IP forwarding/NAT from the AP to `eth0`: a phone on K1000 could
  otherwise reach the bench LAN). It lives in its own nftables
  table; enabling `nftables.service` with a `flush ruleset` config would wipe it (and NM's) until
  the next boot or `sudo systemctl reload k1000-ap-fence`.
- Without `/etc/k1000/tls`, the page falls back to plain HTTP on `http://10.42.0.1:8080/`: it works,
  but cannot be installed (Android installs PWAs only from secure origins) and the page says so.
- The screen's "phone on the Wi-Fi" detection reads the kernel's neighbour table (`/proc/net/arp`)
  and, with the default hostapd backend, dnsmasq's lease file (`/run/k1000-ap/`, readable).
  NetworkManager's lease file (`--backend nm`) is root-only on Pi OS trixie (checked on the box).
- The screen's QR codes: the panel takes the full height under the status bar, 8 px modules for
  the Wi-Fi code (~1.2 mm, ~36 mm across): about 2.5 camera pixels per module at 50 cm.
- The page keeps the phone screen awake while it is open (Wake Lock), and stores only the selected
  tab and band locally.
- An iPhone works as a web page (Safari); installing the CA there is a different path (profile +
  *Certificate Trust Settings*). Not a v0 target.

Wire protocol (JSON over `/ws`): box → phone `hello` (ranges, bands, build), `state` (on change;
`muted`, `hold`, `mute_seq`), `m` (meters), `ack`/`err`, `bye` (code 4002 too many phones, 4003
phone off), `pong`; phone → box `nudge {f, d, fine}`, `set {f, v}` (live EQ values, `lowcut_on`),
`band {s, f, g, q}`, `notch {hz, s}`, `mute {on, seq}` (seq: unmute only), `reset_live
{confirm: true}`, `ping`.

## 9. Friday rig-test checklist (go/no-go for Sunday)

1. The phone joins **K1000** from the QR, gets an IP (Settings › K1000 › IP address) and **stays
   connected for more than 30 s**; the box screen switches to the URL QR. (Fails → §6 plan B.)
2. Rejoin: Wi-Fi off/on, then walk out of range and back: the phone rejoins K1000 **by itself**
   within 30 s. (With portal on it may not: note it; §5.)
3. In airplane mode + Wi-Fi, the **K1000 icon** opens in < 2 s and shows LINK + RUNNING. Note what
   *Install* produced (app or Chrome-badged shortcut).
4. Master −1 dB on the phone → the box screen follows; a knob turn → the phone follows at once.
5. **Hold to mute** on the phone mutes the PA; B2 (long) on the box unmutes. Then both at once:
   phone hold + B2 together → the PA ends **muted**.
6. Range, both bands (§6): 5 / 10 / 20 / 30 m, screen window facing the room; note the band and
   the distance where NO LINK appears. **10 m must hold** (Thomas's working distance). Pick the
   band for Sunday (5 GHz ch 36 by default, `--band bg` if 2.4 GHz holds better).
7. Ring a monitor/feedback test: **long-press "notch here"** at the ringing frequency and slide
   it onto the ring without lifting; the box's LIVE page shows the band.
8. Scan the Wi-Fi QR from 50 cm: note whether it worked first time.
9. Turn the phone's Wi-Fi off mid-show: nothing changes in the sound.
10. SETUP › Phone off: the phone says OFF and controls nothing; on again: it reconnects.
11. `sudo systemctl restart k1000`: audio unchanged, the page reconnects by itself.
12. From the phone, `http://10.42.0.1:5005/` (CamillaGUI) does **not** load (the fence).

All pass → phone **on** for Sunday. Any fail → phone **fully off** for Sunday, knobs only:

```sh
sudo /opt/k1000/net/setup-ap.sh --down    # the Wi-Fi off the air, and it stays off across reboots
sudo nano /etc/k1000/k1000.yaml           # phone: enabled: false (no phone servers at all)
sudo systemctl restart k1000              # between shows: audio keeps playing
```

SETUP › Phone alone is **not** a no-go: it leaves the K1000 Wi-Fi on the air. The `--down` sticks:
a later `install.sh --ap` or `bootstrap.sh` keeps the AP down (it only keeps its files current),
and a spare card is made with `bootstrap.sh --no-ap`. Back on later:
`sudo /opt/k1000/net/setup-ap.sh --up` (it keeps its other choices) and `enabled: true`.
With the phone off, the box's KNOBS OFFLINE alert says `SOURCE DOWN / BYPASS` instead of
`USE PHONE MUTE / BYPASS`: it names the phone mute only while there is one to use.

## 10. Test record (2026-10-12, before integration)

- Unit tests `box/tests/test_web.py` + `test_qr.py` + `test_phone_wiring.py`: the bridge against the real Controller over an in-memory engine (mute generation and
  races, a stalled downlink, hostile numbers), the servers in-process (static files, Origin,
  limits, phone off, isolation, TLS with a throwaway certificate, portal), presence helpers, QR
  codes **decoded** from the rendered 480×320 page; B2 + phone races on the wired controller.
- End to end, headless Chromium at 412×915, 393×851 (Pixel 4a) and 360×780: installable on both
  the localhost and the HTTPS origin, service worker shell works offline and **reloads itself once
  after a box update**, every control reaches the Controller, knob moves show on the phone in
  ~30 ms, drags coalesced, mute races (B2 during a phone hold, two phones, an unmute during a
  change, an old page) all end MUTED, notch + slide in one gesture, step buttons ≥ 48 px at
  393 px and ≥ 44 px at 360 px, reconnect after a k1000 restart ~1 s, the portal in Chrome and in
  the sign-in WebView.
- The fence, checked with the real nft 1.1.3 (the box's) in a throwaway container: from the AP
  only 53/67/80/443/8080 (+22 with `--ssh on`), no forwarding either way, 10.42.0.1 unreachable
  from another interface, the box's own connections unaffected.
- Both DNS files against a real dnsmasq: portal on = every A query → 10.42.0.1; portal off =
  only `k1000.home.arpa` (AAAA "no data"), everything else NXDOMAIN at once; **nothing forwarded**.
- `setup-ap.sh` against a mocked nmcli/systemctl/nft in a sandbox: every option, both backends,
  idempotent re-runs (0 changes), `--down`/`--remove` twice. Since the wiring round, with the new
  defaults, in the repo: `box/tools/tests/test_phone_net.py` (first run = hostapd, `ieee80211w`
  absent, portal off, 5 GHz ch 36, FR, wlan0 unmanaged, fence before the AP; `--down` = units
  disabled and stopped; `--portal on`, `--band bg`, `--ssh on`, `--backend nm` with `pmf 1`;
  install.sh's certificate step, `--ap`, `--deps`; bootstrap's exit codes).
- Wiring round (2026-10-12, main 43f66b3 + the phone files): `pytest -m "not engine and not hw"`
  on mgr-ran 690 passed, 1 skipped (a Pi-only timing budget); on the box, in a scratch copy with
  the engine tests on a private port, 716 passed, 4 skipped (no QR decoder, no shellcheck there).
  The ZynScreen overlay with its SPDX line compiles to the same `.dtbo` as the installed one.
- Fix round (2026-10-12, after the net-safety and wiring reviews): the subnet guard (setup-ap.sh,
  the hostapd unit's `ExecStartPre`, the dispatcher script), the fence narrowed to 10.42.0.1 on
  `eth0` with per-phone/total connection caps, `Requires=` the fence + its table checked, the
  drop-in binding hostapd to its interface, finite start limits, the post-start check, `--down`
  kept by later runs (`--up`), the Wi-Fi-client refusal, the password file never world-readable,
  the KNOBS OFFLINE alert following the phone. Checked on the box (nft 1.1.3, systemd): the fence
  with its `ct count` caps loads in the kernel; both `ExecStartPre` refusals and the pass case
  ran in a throwaway unit (`eth0 holds 192.0.2.54/24, inside the AP subnet ...` with a test
  subnet); the guard script runs under `dash`; `systemd-analyze verify` passes the three units.
  `pytest -m "not engine and not hw"` on mgr-ran: 720 passed, 1 skipped (the Pi-only budget); on
  the box, scratch copy, engine tests on private ports: 745 passed, 5 skipped (no QR decoder, no
  shellcheck there). shellcheck clean on all seven shell scripts.
- **Not yet proven on hardware:** the AP itself on the Pi's wlan0 (5 GHz AP mode on brcmfmac), a
  real Android phone (QR join, CA install, *Sign in to network*, the install prompt), range, the
  hostapd backend. That is what the Friday checklist is for.

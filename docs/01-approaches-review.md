# 01 — Approaches review (2026-10-12)

First pass over the ways to give the Meyer mobile rig a head processor, **not limited to the first
idea** (Zynthian V4 + USB interface + CamillaDSP). Facts were checked on the web on 2026-10-12;
prices are Thomann.fr incl. VAT unless noted, and move. Requirements and open questions:
[`requirements.md`](requirements.md).

---

## 0. TL;DR

1. **The boxes need less crossover than expected.** Meyer designed UPA-1P + USW-1P to take the
   **same full-range signal**: the USW-1P carries its own low-pass, and coplanar tops and subs are in
   phase across the 60–120 Hz overlap. K1000's audio job is mostly **level, polarity, delay, a gentle
   top Lo-Cut and EQ**. That's trivial DSP for anything on the list below.
2. **So the product is the interface, not the DSP.** What's hard to buy is *fast live correction*:
   notch/shelve/rebalance in seconds, from knobs **and** a phone at once, on the box's own Wi‑Fi,
   with the system tune kept safe underneath. Off-the-shelf units do the audio well and the live UX
   poorly: dbx apps rate 1.8–3.1★, the PA2 needs an external router, and the DCX2496 is edited
   through a two-line front panel.
3. **Lead (proposed): build K1000 on CamillaDSP on a Raspberry Pi** (approach D). Prototype on the
   **Zynthian V4 you already own** with a 4-out class-compliant USB interface (Scarlett 4i4 Gen 4 or
   MOTU M4, ~€285). Keep the control layer independent of the surface, so the Zynthian shell can later
   give way to a Pi 5 box + Stream Deck+ (or stay) without rewriting.
4. **Honest hedge.** If the *function* matters more than owning the tool, a used **dbx DriveRack PA2
   (~€330)** or an **A&H CQ-20B (€888)** does 80% of this tomorrow. Build K1000 for the UI, the
   integration and the fun, not to save money.
5. **Lower-risk hybrid worth keeping in mind.** Proven DSP hardware carries the audio (instant boot,
   12 V) and K1000 is only the brain and UI. Candidates: a t.racks DSP 4x4 Mini Pro (open
   USB protocol), or a miniDSP 4x10 HD via `minidsp-rs`. This is the fallback if Pi audio
   reliability disappoints.

---

## 1. What the Meyer boxes actually need

| | UPA-1P | USW-1P |
|---|---|---|
| Drivers | 12″ + 3″ horn, 100°×40° | 2×15″, 62 kg |
| Operating range | 60 Hz–18 kHz | 32–200 Hz |
| Input | XLR + loop, 10 kΩ balanced, **pin 2 +** | same |
| Internal processing | crossover, correction, limiters | **built-in low-pass** + limiters |
| Polarity switch / attenuator | only on an *optional* input module | same |
| Level | **limiting begins ≈ 0 dBV** continuous; full SPL needs +20 dBV | same |

From Meyer's UPA-P manual and the LD-2 line driver:

- The UPA-P "is not a full range speaker" and is optimised for the USW-1P. Same full-range signal to
  both, **same polarity when close and coplanar**; **beyond ~1.5 m separation, polarity reversal
  and/or delay may be needed**.
- Ground-stacked subs gain ~6 dB → Meyer suggested a **separate sub feed for level** (the VX-1
  "virtual crossover") rather than a steep crossover.
- The LD-2 line driver offered a **Mid-Hi Lo-Cut at 160 Hz, 12 dB/oct**, a **sub polarity switch** and
  an 80 Hz sub split. That's the historical "pre-filter to spare the tops": it keeps LF excursion out
  of the UPA's 12″, so the tops limit later.

**Consequences for K1000**

- Per-output **gain, polarity, delay** are the core. The polarity switch most boxes lack is a must
  here, and delay matters whenever the subs aren't stacked under the tops (common on a roaming rig).
- A **switchable top Lo-Cut** (LD-2 style: 12 dB/oct ~100–160 Hz, maybe a steeper option).
- **No second low-pass on the subs** by default: it would stack with the internal one and shift the
  phase at the overlap. Keep it available as an option, defaulting to off.
- **Level**: any modern interface drives these boxes into limiting (2 Vrms ≈ +6 dBV is already 6 dB
  past onset). The needs are *balanced* outputs for long runs, sensible output trims, optionally a
  K1000 limiter that keeps the Meyer limiters idle, and above all **no thump**: self-powered boxes
  pass every power-on transient straight to the drivers.
- To confirm: the subs are USW-1P (same era, and the sub the UPA-P manual names). A USW-112P/210P
  would have no internal crossover and would change this section.

---

## 2. The approaches

### A — Buy a loudspeaker processor

| Unit | I/O, form | Control | Strong | Weak | Price |
|---|---|---|---|---|---|
| **dbx DriveRack PA2** | 2 in + RTA-mic in (phantom) / 6 XLR out, 1U | front panel + iOS/Android/Mac/Win app over **Ethernet (needs your own router)** | AutoEQ with mic, AFS (12 auto-notches), GEQ 31 + 8-band out PEQ, crossover, limiter, RTA | output delay **10 ms max** (input 100 ms), app 3.1★ with dropout complaints, full-width 1U | €459 new, ~€330 used |
| dbx Venu360 | 3/6, 1U | iPad app 1.8★ | 1000 ms out delay, more EQ | worse app, €1 098 | — |
| Behringer DCX2496 LE | 2/6, 1U | front panel only (the original's RS-232 is gone on the LE) | serious crossover/EQ DSP | live editing on a tiny display | €199 |
| t.racks DSP 408 | 4/8 XLR, 1U | front encoder + Windows software | good I/O | no phone control | €458 |
| Meyer Galileo GALAXY 408 | 4/8, 1U | Compass + **Compass Go (iPad)** | the reference: Meyer's own presets, U-Shaping | ~$10k+ new, ~$3.6k for a used Galileo 616; no RTA | — |

**Verdict.** The PA2 is the closest ready-made answer, and its feature list is the bar to beat:
AutoEQ, auto-notch, RTA mic input, snapshots. Its weak points are exactly K1000's reason to exist:
the network link, the app quality and the live ergonomics. Compass Go is the best **layout
reference** for the phone UI.

### B — A digital mixer as the head

Gains: mic inputs, mature apps (incl. third-party **Mixing Station**), RTA and feedback tools.

| Mixer | Per-output processing | Notes | Price |
|---|---|---|---|
| **A&H CQ-20B** (stagebox) / CQ-18T | PEQ or GEQ, **delay**, LF/HF bands as **12 dB/oct HP/LP** (= Meyer's Lo-Cut slope), **Feedback Assistant** (16 filters/out) | built-in dual-band Wi‑Fi | €888 / €959 |
| **Soundcraft Ui24R** | GEQ 31, AFS2, comp, **delay up to 500 ms**; HPF/LPF on aux (slopes unclear) | browser UI on any device, 10 outs | €849 |
| Mackie DL16S | per bus: PEQ, HPF/LPF, GEQ, comp/limiter, **delay**, RTA | discontinued; look for a used one | ~€730 |
| Behringer XR18 / MR18 | PEQ or GEQ per bus, **no output delay** | ruled out for spread-out layouts | €377 |

**Verdict.** The CQ-20B and Ui24R are credible heads, and the 12 dB/oct limit matters less with
Meyer boxes than it would elsewhere. Cost: a mixer's complexity and footprint for what is a
2-input job, and a UI that is a mixing UI rather than a system-correction one. Strong option if
you'd also *mix* from it (MC mic, a band's stereo + vocals…).

### C — Proven DSP hardware + K1000 as the brain/UI (hybrid)

The audio runs on a dedicated DSP that boots instantly and doesn't care if the UI crashes; a Pi or
ESP32 drives it and provides knobs, phone UI and measurement.

| Backend | I/O | Control path | Caveats | Price |
|---|---|---|---|---|
| **t.racks DSP 4x4 Mini Pro** (XLR) / Mini (jack) | 4×4 balanced, **12 V DC** | USB-HID, **fully reverse-engineered**: `openDSP-4x4` (AGPL, WebHID + Android OTG, on F-Droid since 06/2026) and a Python lib | 7-band PEQ/out, 680 ms delay, limiter, 10 crossover slopes; young community projects; noise mentioned in reviews; **exactly 4 outs** | Mini €118; Mini Pro ~€140 (UK) |
| **miniDSP 4x10 HD** | 8 balanced outs (terminal blocks) | `minidsp-rs` HTTP API: presets, volume/mute, levels, **live PEQ biquads**; delay/crossover via presets only | 2x4 HD and Flex Eight are unbalanced; Flex Balanced is TRS | — |

**Verdict.** The safest audio path, and battery-friendly. But measurement and a spectrum view
still need an audio input on the brain side, and you'd build the same UI as in D on top of a
narrower, less flexible engine. Keep it as the **fallback backend**, and design K1000's control
layer so the engine can be swapped (§5).

### D — Linux SBC + CamillaDSP (the build) — *proposed lead*

**Engine: CamillaDSP.**
- Status: stable **v4.1.3** (2026-04); **v5.0.0-beta.1 (2026-10-08)**, very active.
- What it has: biquads and LR/Butterworth combos, FIR, subsample delay, gain/polarity, a full
  routing mixer, Aux1–4 faders with ramps, compressor/limiter, and a websocket API with pycamilladsp.
- **v5 adds what K1000 wants**: a **spectrum analyzer over the websocket** (per channel; an RTA on
  the mic input for free), a **lookahead limiter**, faster biquads, and a fix for real-time priority
  (it was silently ineffective on v4 ALSA builds).
- **CPU**: a non-issue. Pi 4 runs ~2% for 2 channels × 10 biquads; 6 outputs of IIR is trivial and FIR
  fits.
- **Live changes**: filter parameter changes apply **in place without dropouts**. Pipeline or mixer
  changes rebuild the pipeline (risk of a click). Design constraint: **fixed-slot pipeline**, where
  every live action is a parameter write or a ramped fader (§5).
- Compared with the alternatives (PipeWire filter-chain, JACK + LSP plugins, BruteFIR, Pure Data),
  CamillaDSP is the only one that is a single, validated, API-driven appliance engine with a GUI.

**Hardware shapes:**

| Shape | Compute + surface | Audio I/O | For | Against |
|---|---|---|---|---|
| **D1 Zynthian V4 shell** | Pi 4; 3.5″ 480×320 SPI resistive touch; **4 push-encoders + 4 buttons on an MCP23017 (I²C 0x20)**; metal case | USB interface (its own HiFiBerry board is 2-in/2-out and must not be mixed with the USB card: two clocks would drift the sub/top alignment) | **owned and idle**; knobs + screen already wired and cased; ZynthianOS image keeps the display working (plain Pi OS left it dark for one user) | small resistive screen (fine for pages and a coarse RTA, poor for EQ curves); metal case kills onboard Wi‑Fi → USB dongle with antenna; Pi 4 USB power budget 1.2 A; synth chassis is bigger than a pure DSP needs |
| **D2 Pi 5 headless + USB surface** | Pi 5 in a small box; **Stream Deck+** (€195: 4 push-encoders + LCD strip + 8 LCD keys, mature Python lib) or **X-Touch Mini** (€52, 8 LED-ring encoders, class-compliant MIDI) | USB interface | best knob/display ergonomics per euro; surfaces are replaceable spares | more boxes and cables; Stream Deck+ is plastic |
| **D3 Pi 5 + HATs, no USB interface** | Pi 5 + any surface | **HiFiBerry Studio DAC8x** (€90, **8 balanced outs**, DB25, ~+14.7 dBu) + Studio ADC (€65, 2 balanced in) | most compact 8-out solution, single clock | DAC8x + ADC stacking/compatibility unverified; DB25 breakout needed; no mic preamp/phantom for measurement |
| D-phone | any of the above, **no physical surface** | — | cheapest, simplest | no blind "panic" controls; Wi‑Fi becomes a single point of failure for control |

**USB interface shortlist** (class-compliant, ≥ +16 dBu out, Linux-proven):

| Interface | I/O | Notes | Price |
|---|---|---|---|
| **Focusrite Scarlett 4i4 Gen 4** | 2 combo (mic w/ phantom) + 2 line (+22 dBu) / 4 TRS | full routing/mixer control from Linux (`scarlett2`, kernel ≥ 6.8); PSU option; switch off MSD mode first | ~€285 |
| **MOTU M4** | 2 combo + 2 line / 4 TRS | bus-powered, LCD meters, great DAC; its **front knob scales outs 1-2 only** → wire subs/tops so a knock can't unbalance them | €289 |
| **MOTU UltraLite mk5** | 2 combo + 6 line / **10 TRS**, +21 dBu | the pick for ≥ 6 outs; PSU; DC-coupled outs (thump care) | €740 |

Both 4-out picks allow **stereo program on the line inputs + a measurement mic on a combo input
with phantom** at the same time.

**Verdict.** This is the approach that delivers the differentiator: a two-surface live-correction
UI, its own AP, a spectrum-assisted notch, the measurement workflow and presets, all in code that
you own and can evolve. Risks are known and manageable: boot time (~10–20 s), power-loss
robustness (read-only root), USB-audio quirks (soak test), and thumps (mute-at-boot, power order,
later a relay). **D1 to prototype** (zero cost, today); the final shape decided after a field trial.

### E — Embedded DSP DIY (Teensy / ADAU1452)

- **Teensy 4.1 + 8×8 TDM codec board**: the CS42448 is end-of-life; the successor is a
  TLV320AIC3104 board at $22.
- **ADAU1452 boards** at ~$130: SigmaStudio is Windows-only, and you'd hand-roll the safeload writes.
- **freeDSP Aurora** has been dormant since 2022.
- **For**: sub-second boot, no filesystem.
- **Against**: weeks of analog work for balanced I/O, your own coefficient pipeline, and a weaker
  UI platform.
- **Verdict**: only if instant-on becomes a hard requirement. Even then, approach C gets instant-on
  for less.

### F — Laptop / iPad host (dismissed)

A laptop running plugins, or an iPad with AUM and a class-compliant interface. Not compact, not
road-robust, and the host is also your phone or computer. Not pursued.

---

## 3. Comparison

Scores are relative within this table (●●● best).

| | Live-fix UX | Audio robustness | Outs | Measure / RTA | Build effort | Cost | Size / power |
|---|---|---|---|---|---|---|---|
| A PA2 (used) | ● | ●●● | 6 | ●● (AutoEQ, RTA) | ●●● none | ~€330 | 1U full, mains |
| B CQ-20B / Ui24R | ●● | ●●● | 6–10 | ●● | ●●● none | €850–900 | stagebox, mains |
| C t.racks + K1000 UI | ●●● | ●●● | 4 | ●● (needs input on brain) | ●● | ~€120 + brain | tiny, **12 V** |
| **D1 Zynthian + USB** | ●●● | ●● | 4 (10 w/ mk5) | ●●● (v5 spectrum) | ●● | **~€285** | Zynthian case |
| D2 Pi 5 + Stream Deck+ | ●●● | ●● | 4–10 | ●●● | ●● | ~€560 | 2–3 small boxes |
| D3 Pi 5 + DAC8x | ●●● | ●● | **8** | ● (no phantom) | ●● (+ HAT risk) | ~€250 | smallest 8-out |
| E Teensy/ADAU | ●● | ●●● | 8 | ● | ● heavy | ~€150 + analog | small |

---

## 4. What K1000 should do, whichever route

These carry over to any build (D or C), and most are the bar a bought unit is judged against:

1. **Two EQ layers**: *System* (set at tune time, measured or by ear, saved per preset) and *Live*
   (on top: N notch/shelf slots at 0 dB by default; **"reset live"** in one move). A live tweak never
   destroys the tune.
2. **Macro controls first**: master, **sub↔top balance** (one knob), mute, mono-sub, top Lo-Cut,
   each reachable blind.
3. **Fixed-slot pipeline** (CamillaDSP): everything declared up front (Lo-Cut, PEQ slots, delay,
   polarity gains, Aux faders per output group). Show-time actions are only parameter writes or
   ramped faders, never pipeline rebuilds.
4. **Two surfaces, one model**: the encoders and the phone edit the same state, and either alone can
   run a show. Phone UI served by the box itself over **its own AP** (5 GHz, external antenna; mind
   phones that drop a network without internet).
5. **Notch workflow in seconds**: pick a free slot, set frequency (encoder, log steps, push for fine),
   gain, then Q. On the phone, **tap a peak on the spectrum** to place it. Later, **suggest-notch**:
   persistence/growth detection on the spectrum, one tap to accept (the dbx AFS / CQ Feedback
   Assistant idea, but supervised).
6. **Measurement, staged**: (a) noise out + RTA on the mic input (CamillaDSP v5 spectrum); (b)
   transfer function later, via a dsnoop/PipeWire tap + numpy; (c) laptop REW (it exports CamillaDSP
   YAML) or Open Sound Meter for deep tunes.
7. **Safe audio**: start **muted and ramp in**; documented power order (speakers on last, off first);
   later a mute relay on the outputs; watchdog + supervisor restarting the engine on device loss;
   **read-only root** with presets on a separate partition; long soak test logging xruns before the
   first gig.
8. **Presets** per place type (street, courtyard, indoor, …) and A/B compare.

---

## 5. Architecture sketch (for D, open to C)

```
             ┌──────────────── K1000 brain (Pi) ───────────────┐
 encoders ──►│ surface drivers ─┐                               │
 (Zynthian / │                  ├─► control model ──► engine ───┼─► CamillaDSP (websocket)
 StreamDeck) │ phone web UI ────┘   (state, presets,  adapter   │    └─ or t.racks / miniDSP
 phone ─────►│ (served on own AP)    layers, macros)            │       (C fallback)
             │ spectrum / RTA ◄──────────── engine levels/FFT ──┤
             └──────────────────────────────────────────────────┘
```

A small Python (or Rust) service owns the state and talks to the engine. Surfaces are thin.
That's what makes the Zynthian → Pi 5 / Stream Deck move, or a backend swap, cheap.

---

## 6. Recommendation (to discuss)

- **Lead: D, prototyped as D1** (Zynthian V4 + Scarlett 4i4 Gen 4 or MOTU M4 + CamillaDSP).
  First milestone is a bench proof:
  - audio through the fixed-slot pipeline
  - phone control (CamillaGUI compact view to start)
  - 4 encoders mapped to master / balance / notch freq / notch gain
  - boot time, thump and an 8-hour soak measured
- **Decide the final hardware shape after one real outing** (D1 kept vs D2 vs D3) on the evidence:
  was the 3.5″ screen enough, were the knobs used, did the Wi‑Fi hold.
- **Interface choice hinges on the output count**: 4 → Scarlett 4i4 Gen 4 (headroom, Linux
  routing control) or M4 (bus-powered, meters); 6+ → UltraLite mk5, or the D3 HAT route.
- **Fallbacks, ranked**: C (t.racks/miniDSP backend under the same UI) if Pi audio isn't trustworthy;
  A/B (PA2 used, CQ-20B) if the build stalls and the rig needs a head now.

## 7. What would change this

- *Building isn't the goal* → A (PA2 used) or B (CQ-20B) and stop here.
- *Mics/mixing wanted on the box* → B gains a lot.
- *Must run on battery / instant-on* → C (12 V, no boot).
- *6+ independent outs* → UltraLite mk5 or D3.
- *The subs aren't USW-1P* → revisit §1 (low-pass on the subs becomes K1000's job).

The open questions are in [`requirements.md`](requirements.md) §5.

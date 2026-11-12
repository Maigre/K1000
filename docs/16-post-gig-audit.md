# 16 — Post-gig audit and latency proposal

2026-11-12. Written from the repository, the project records and the engine's source code. The box
was not powered during this review, so nothing here was measured again. Each claim says whether it
was **measured** (on the bench, 16 Oct), **read** in source code, or **calculated**.

## 1. Summary

**The report.** The box played its first gig on 18 Oct. Beatmatching against the speakers was
impossible, even standing in front of one. The delay felt like 50 to 200 ms. The bench had measured
20.3 ms two days before.

**The most likely cause.** The engine (CamillaDSP 5.0.0-beta.1) does not come back to its normal
delay after a dropout. Every dropout leaves the sound later than before, and it stays later until
the engine restarts. The delay can pile up to about 105 ms (calculated). Our settings switch off the
only mechanism that would bring it back. The engine's author described and fixed this defect ten
days after the gig. We saw its first step ourselves on 16 Oct and filed it as a detail.

This is not proven for the gig night yet. The engine's log of that night should still be on the
card, and it will say how many dropouts happened. Reading it is the first thing to do when the box
is on the desk.

**The second cause is ours.** Even at its best the box adds 20 ms. The buffer was set large on the
reasoning that a DJ-only show makes latency free. That reasoning was wrong: a DJ who cues in
headphones and listens to the speakers is the user who suffers most from it. No latency target was
ever set, and nobody listened for it at the rig test.

**The proposal, in three layers.**

1. Stop the growth: an engine build with the upstream fix, a latency readout on the box, and a test
   that fails when the delay grows.
2. Lower the base from 20 ms to under 10 ms: smaller chunk and buffer, in measured steps, each
   held by a long run.
3. At the rig: a DJ monitor fed straight from the DJ mixer hears no processing delay at all,
   whatever the box does. That one is a rig choice, not development.

**The rest of the audit** (section 5): the audio design, the safety behaviour and the test suite
are sound. The weak points are the gates that were skipped under time pressure, an engine still in
beta, documents that no longer match the box, and a few private details in a public repository.

**What it takes from Thomas** (section 7): three decisions, and two short sittings at the desk.
The first sitting is about ten minutes of hands, then the work runs without him.

## 2. What happened to the latency

### 2.1 What is known

| Fact | Source |
|---|---|
| 20.28 ms from LINE IN to OUT on every output: engine 16.5 ms, M4 converters and USB about 3.8 ms | measured 16 Oct, just after an engine start (`docs/05-m4-day.md` §13) |
| After a USB drop that day, the playback buffer sat near 1000 frames instead of 536: +10 ms until the next engine start | measured 16 Oct (same table) |
| The engine runs chunk 256, `target_level` 512, `enable_rate_adjust: false` | read, `box/k1000/dspconfig.py`, `box/etc/k1000.yaml` |
| The box at the gig ran `025b8f8`: same buffer settings, plus the de-harsh compressor (no look-ahead, adds no delay) | read |
| At the gig the delay felt like 50 to 200 ms | Thomas, by ear |

### 2.2 The mechanism

Capture and playback are the same device, the M4, on one clock. So the number of samples inside the
box is constant as long as nothing stops: what comes in goes out at the same rate. That number is
the latency.

A dropout breaks this. When the engine gets no data to the M4 in time (the processor was busy, the
USB link hiccupped, the power sagged), playback stops for a moment. The input does not stop: it
keeps filling the capture buffer. When playback restarts, the engine waits one `target_level` of
time, as it does at a start, and then everything that piled up during the gap arrives on top.
The box now holds more samples than before, by about the length of the gap. Nothing ever removes
them. The next gap adds its own.

Read in the engine source at the version on the box (`v5.0.0-beta.1`, default ALSA backend):

- `src/alsa_backend/device.rs`, `play_buffer`: on an underrun it prepares the device, sleeps
  `target_level` frames of time, then writes. It does not look at what is queued behind.
- `README.md`, `target_level`: the level is only an initial condition. Keeping it there is the job
  of rate adjust, which is for devices on two different clocks. K1000 sets
  `enable_rate_adjust: false`, which is the usual choice for one device, so nothing keeps it.
- Where the backlog sits, and how much each stage can hold with our settings:

| Stage | Holds at most | Time |
|---|---|---|
| Playback buffer (a power of two ≥ 3 chunks = 1024 frames; 536 in normal running) | 1024 frames | 21.3 ms |
| Queue from processing to playback (`queuelimit` 4 chunks) | 1024 frames | 21.3 ms |
| Queue from capture to processing (4 chunks) | 1024 frames | 21.3 ms |
| Capture buffer (1024 frames) | 1024 frames | 21.3 ms |
| One chunk in the hands of each of the three threads | 768 frames | 16.0 ms |
| **Everything full** | **4864 frames** | **101 ms**, about 105 ms with the M4 |

The 1024-frame device buffers follow from the engine's sizing rule
(`src/alsa_backend/buffermanager.rs`) and the queue sizes from `src/engine_pipeline.rs`. They were
not read on the box; its log will confirm them.

The stages fill from the playback end backwards. The first to fill is the playback buffer, from
536 to 1024 frames: that is exactly the "settles near 1000 frames, +10 ms" seen on 16 Oct. Past
that the backlog moves into the queues, where the engine's buffer-level reading only partly sees
it. When everything is full the capture buffer overruns, drops about 20 ms of sound, and the box
stays near the top.

So the delay is between 20 ms and about 105 ms, depends on how many gaps the night had, and only
goes up. That fits a report of "50 to 200 ms" by ear.

### 2.3 The engine's author found the same thing

The upstream project fixed this after our gig. From its change log (branch `next5`, not yet in a
release):

> After a gap in the data, for example while CamillaDSP got no CPU time, playback underran and was
> restarted at the target level. The data that had been held up in the capture device during the
> gap then arrived on top of that. The level ended up thousands of frames above the target, and
> rate adjust took 30 s or more to bring it back. Right after a restart, chunks are now dropped
> while the level is more than a chunk or two above the target.

Commits `dccb8d2` (17 Oct, in `v5.0.0-beta.2`), `75ef252` (28 Oct) and `4feea56` (29 Oct). With rate
adjust on, the old engine took 30 s or more to recover. With rate adjust off, as on K1000, it never
did.

Two other fixes since `beta.1` touch things K1000 met:

- "A capture overrun no longer stops processing when capture and playback share a USB device":
  the M4 is such a device (implicit feedback). On `beta.1` this can end the engine, which systemd
  then restarts muted.
- "Don't panic on unused capture channels in the spectrum buffer": the crash loop of 16 Oct, which
  K1000 works around in its input mixer.

### 2.4 What is not known yet

- **How many dropouts the gig had.** The engine writes a warning for each one. Its log is kept on
  the card across restarts (50 files). This is the evidence that confirms or kills section 2.2.
- **Why it dropped out.** Power, USB, processor load, heat: section 5.1 lists what in the box can
  starve the engine. The system log of that night may say.
- **The alignment delay in use.** ALIGN can delay the subs or the tops by up to 50 ms. A delay on
  the tops adds straight to what a DJ hears. The saved settings on the box answer this in one read.
- **A delay hidden behind ALIGN.** The box stores a sub delay and a top delay, and ALIGN shows only
  their difference (`box/k1000/state.py`, `align_delay_ms`). Turning the ALIGN knob keeps one of
  the two at zero. An edit made in CamillaGUI or the tune page is adopted side by side
  (`box/k1000/dspconfig.py`), so both can hold a value: 45 and 40 reads "5 ms" on the screen and
  makes the whole PA 40 ms late. Whether the box holds such a pair is the same one read.
- **Whether 20 ms alone is already too much for Thomas.** If the log shows no dropout at all, then
  the base latency is the whole problem, and layer 2 below is the whole fix. The plan covers both.

### 2.5 Why the bench did not catch it

- Latency was measured once, a few minutes after a fresh engine start. That is the one moment when
  it is at its lowest.
- The long-run tool (`box/tools/k1000-soak.sh`) counts dropouts and logs the buffer level, but its
  summary never judges the level. A night that ends 60 ms late reads "clean" if no dropout happened
  during the sampled window, and a rising buffer is not a failure.
- The loop tool (`box/tools/bench/m4loop.py`) searches for a delay between 3 and 80 ms. It cannot
  report the top of the range above.
- The overnight run on the M4 at the show setting, and the USB pull drills, were planned, and no
  record says they ran before the gig. The drills are the test that produces this fault on purpose.
- The requirement said "latency irrelevant live (≤ ~10 ms) (?)" (`docs/requirements.md`). The
  question mark was never resolved, and 20 ms was recorded as a pass against no number.
- The rig test protocol had no listening check with a DJ source.

## 3. Proposal for the latency

### 3.1 The requirement to write down

> In to out, **10 ms or less at any moment of a show**, measured, never growing. The box shows its
> current delay and says so when it is above the limit.

Ten milliseconds is the same as standing 3.4 m further from the speaker. Sound already takes 3 ms
per metre to reach the DJ, so the box's share should be small against it.

### 3.2 Layer 1: stop the growth

| Option | What it is | For | Against |
|---|---|---|---|
| **A. Engine with the upstream fix** (recommended) | Build the engine from the upstream `next5` branch at a pinned commit, run K1000's engine tests on it, install it on the desk box | Fixes the cause where it is. Brings the two other fixes of section 2.3. Lets K1000 drop its spectrum workaround later | No release has it yet, so it is a build from source, of code newer and less run than `beta.1`. Config checks got stricter: K1000's generated config must pass them |
| B. Rate adjust on the current engine | `enable_rate_adjust: true` with the `Slip` resampler: a setting, no new binary | Works today | Made for two clocks, not for this. Recovery takes tens of seconds. Each correction is a tiny splice in the sound. To be measured before trusting it |
| C. K1000 watches and restarts | K1000 reads the buffer level; when it stays high it restarts the engine in a quiet moment | Independent of upstream | A restart is a gap of a second or more with the ramp. It only sees part of the backlog. It treats the symptom |

Recommendation: A, tested on the desk against the fault produced on purpose. B is the fallback if
the build misbehaves. C is not worth building unless both fail.

Whatever the option, three things go in with it:

1. **A latency readout.** K1000 already talks to the engine; it reads the buffer level once a
   second, shows the delay in ms on the STATUS screen and the phone, and flags it in red above the
   limit. It also counts dropouts since the engine started.
2. **A test that produces the fault.** Freeze the engine for 50 ms on the desk, measure the delay
   before and after with the loop. On `beta.1` it must grow and stay (this proves section 2.2 on
   our box). On the fix it must come back.
3. **The long run judges latency.** The soak summary fails when the buffer level ends above where
   it started, and the loop tool's search range goes up to 150 ms.

### 3.3 Layer 2: lower the base

The engine's share is about one chunk plus the target level. The M4's 3.8 ms is a floor at 48 kHz.

| Chunk | Target level | Engine | In to out | Status |
|---|---|---|---|---|
| 256 | 512 | 16.5 ms | 20.3 ms | measured, today |
| 256 | 256 | about 11 ms | about 15 ms | calculated |
| 128 | 256 | about 8.5 ms | about 12 ms | calculated |
| 128 | 128 | about 6 ms | about 10 ms | calculated |
| 64 | 128 | about 4.5 ms | about 8 ms | calculated |
| 64 | 64 | about 3 ms | about 7 ms | calculated |

Each step down leaves less margin against a dropout, and with the fix a dropout is a short gap
instead of a permanent delay. The method: one step at a time on the desk, the loop measurement, the
produced-fault test, then a night of music with the phone connected and the spectrum on. The lowest
setting that passes a clean night is the show setting. The next one up is the documented fallback.

The chunk size is a constant in the code (`CHUNKSIZE`, and timings derived from it such as
`STEP_WAIT_S`). It becomes a per-profile setting like `target_level`.

What makes a low setting hold is the box around the engine. Section 5.1 lists the levers.

Not proposed now: 96 kHz would halve the frame times and lower the M4's share, but it was decided
against on 16 Oct for other reasons and doubles the processor load. It stays a later option if
10 ms cannot be held at 48 kHz.

### 3.4 Layer 3: the rig

A DJ monitor fed from the DJ mixer's booth output, not through K1000, has no processing delay. It
is how club booths solve the same problem with far longer delays than ours. It costs one speaker
and a cable, and it makes the DJ's timing independent of the box. Whether the mobile rig carries
one is Thomas's choice. It does not replace layers 1 and 2: the mains should still be tight.

## 4. Order of work

| Step | Who | Needs the box |
|---|---|---|
| 1. Read the gig night off the box: engine logs, system log, saved settings. Copy them aside before anything restarts | machine | on the desk, powered, on the network |
| 2. Reproduce: loop measurement at start, produce a gap, measure again | machine | loop cables in place |
| 3. Readout, fault test, soak judgement, wider loop range: code and tests on a branch | machine | no |
| 4. Build the fixed engine, run the engine tests, install on the desk box, repeat step 2 | machine | yes |
| 5. Step the chunk and target down, measure each, run a night at the candidate setting | machine | yes, overnight |
| 6. Listen: beatmatch on the box at the old and the new setting | Thomas | desk, a DJ source, one speaker |
| 7. Confirm at the rig, with the alignment delay as tuned | Thomas | rig |

Steps 1 to 5 need one plug-in and no further hands. Section 7 packs them into sittings.

## 5. The rest of the audit

Two reviewers read the box software and the project material in parallel. Their main citations
were checked against the code. Findings are ranked within each part.

### 5.1 The box at run time

**Solid, keep as it is.**

- The engine's life cycle: it boots muted, exits when the device is lost, and only K1000 ramps it
  in, and only while it runs.
- Saving settings: temporary file, sync, rename, directory sync. A power cut does not corrupt them.
- K1000 never raises a level or unmutes on its own when it reconciles with the engine.
- The Wi-Fi fence, the time-limited laptop access, the install with a rollback copy and pinned
  checksums for the engine.

**What the control service does to the engine.** Nothing in it reloads, restarts or starves the
engine in steady running: knob and phone edits are single-value writes, never a whole-config
upload. What it does cost:

| Load | Rate |
|---|---|
| Meters | 2 commands every 0.1 s, always, even with nobody looking |
| Status poll | every second, including the whole config, regenerated and compared in Python |
| Spectrum | about 30 calls a second whenever SHOW is displayed |
| Screen drawing | 10 frames a second, about 20 % of a core |
| Big knob moves | up to about 80 writes a second for a moment |

The engine's audio threads have real-time priority, so Python cannot push them aside. The exposure
is lower down: no CPU governor is set anywhere (the processor clocks down between bursts), nothing
pins the engine and the service to different cores, and the Wi-Fi access point, the SD card and USB
share the same kernel. These are the levers for holding a small buffer.

**Findings.**

| # | Finding | Effect live | Fix |
|---|---|---|---|
| 1 | The hidden common delay behind ALIGN (section 2.4) | The whole PA up to 50 ms late with nothing on screen | Remove the common part when settings are loaded or adopted |
| 2 | The service never reads the buffer level, the capture rate or dropouts. Only the hand-started soak tool does | The gig left no record of its latency | The readout of section 3.2 |
| 3 | A hung control service is never restarted: the unit has no watchdog. The engine restarts muted and only K1000 ramps it in | A hang followed by any engine restart is silence until someone power-cycles | systemd watchdog, fed from the event loop |
| 4 | Any phone on the Wi-Fi has full control, with no pairing. It can also open laptop access (SSH, CamillaGUI) and change the rows that rebuild the engine config, such as AUX mic into the PA | One guest with the password can change the sound or open the box | Take laptop access and the rebuild rows off the phone's list |
| 5 | The automatic repair of a drifted config has no limit: a whole-config upload, Main muted, every 30 s while the drift lasts | A repeating mute | Stop after 3 tries and alert |
| 6 | MUTE waits for the same lock as config writes: up to 0.5 s behind a write, up to 10 s behind an upload | A late mute | A connection of its own for mute |
| 7 | Power-off is "unplug", on a read-write card with a persistent journal | Card wear and a small corruption risk | Read-only or overlay root, settings on their own partition |
| 8 | A spare card does not carry the show: the tune, the certificate and the Wi-Fi password have no backup path, and a fresh install needs the internet and a beta download | The spare is not a spare | A backup and restore tool; keep the downloads with the project |
| 9 | CamillaGUI listens on every interface without a password. The access point units give up after 8 failures. The engine runs as the login user, who has sudo | Smaller exposures | Bind to localhost, restart always, drop privileges |
| 10 | `control.py` and `web/server.py` are about 3,300 lines each. Two phone pages and two protocols run side by side | Harder to change safely | Retire the first phone page, split by responsibility |

### 5.2 Tests

About 1,300 tests run in under two minutes and prove the logic well. What they do not prove:

- Nothing runs on real audio hardware. The engine tests use a generated signal into a null device.
  There is no test for latency, buffer level, dropouts under load or a pulled USB cable.
- The fakes are modelled on `beta.1`. An engine update can break K1000 without failing a test.
- No whole-box test: boot order, a hung service plus an engine restart, a power cut on the card.
- The 175 system tests in `box/tools/tests` are not part of a plain test run, and nothing runs the
  suite on a push.

Proposal: the desk box with its loop cable becomes the hardware test bench. The latency, fault and
USB-drop tests are scripts that run on it, and an engine update has to pass them. A CI job runs the
logic suite on every push.

### 5.3 Decisions, roadmap, documents

| # | Finding | Fix |
|---|---|---|
| 1 | The buffer size of the M4 sits under a `decided` header as "decided by Claude (Thomas: I don't know)", against the ledger's own rule. Its premise, that a DJ-only show makes latency free, is the one the gig disproved | A new decision carrying the requirement of section 3.1 |
| 2 | Gates were skipped or left unrecorded: the freeze of Wed 14 Oct was followed by box commits on 17 Oct, and nothing records that the M4 soak and the USB drills ran | One pass or fail line per gate, and no show on a skipped gate |
| 3 | The repository has no trace of the gig. The README and the manual say "not yet played a show", the roadmap status is dated 13 Oct, and no tag names the build that played | A short debrief, a tag on `025b8f8`, a rewritten status |
| 4 | The ledger is behind the code: D-016 is `proposed` and deployed, the Android app and the second phone page were built before the gig with no entry, D-003 still describes a second-hand unit, and D-002 promised a hardware review after the first outing | Settle D-016, add the missing entries, hold the hardware review with latency as a criterion |
| 5 | D-004 chose a beta engine and froze it for the gig. It has no successor | Decision 2 of section 6 |
| 6 | The same facts live in many documents: the knob map in 13 files, a single decision touching 14 to 22 of them, three different boot times, 29 references to a design document that is not in the repository. Numbered documents were rewritten against the repository's own rule | One home per fact; the numbered documents stay as written |

### 5.4 Android

The app is a WebView shell (version 0.2.2, about 4,700 lines of Java) that adds the Wi-Fi join, the
certificate pinning, haptics and microphone access. Its helpers have 62 unit tests. Its three large
classes are covered only by a hand-run emulator check. The signing key exists in one copy. There is
no update path, and the box serves the APK over plain HTTP. `docs/09-android.md` still says "not yet
on a phone", although it drove the box at the rig on 17 Oct.

Proposal: copy the signing key to a second place now. Leave the app as it is until the latency is
closed.

### 5.5 The repository

- **Private details in a public repository.** The current files hold a private network address
  (the Android build file and its README), a laptop's host name, a private path and the M4's serial
  number. Older commits still hold another project's name and a first name, removed from the
  current files since. Cleaning the current files is an hour. Cleaning the history needs a forced
  push, which is Thomas's to decide and run.
- **Branches.** `night/snapshots` (venue presets, about 2,900 lines) is the only unmerged work that
  matters, and it is 30 commits behind. `night/android` and `night/phone-rta` look superseded.
- **Licence.** Consistent: GPL-3.0-or-later with headers in every source file.

### 5.6 Improvements beyond the latency, by priority

| When | What |
|---|---|
| With the latency work, because it touches it | The readout (5.1 #2), the hidden ALIGN delay (5.1 #1), CPU governor and core pinning, the watchdog on the control service (5.1 #3) |
| Before the next gig | The phone's list of allowed actions (5.1 #4), the repair limit (5.1 #5), mute on its own connection (5.1 #6), backup and a real spare card (5.1 #8), the debrief, tag and truthful status (5.3 #3), private details out of the current files (5.5), CI |
| Later | Read-only root (5.1 #7), retire the first phone page and split the two large modules (5.1 #10), the `night/snapshots` branch, the hardware review (5.3 #4), the Android app |

## 6. Decisions asked

1. **The latency requirement** of section 3.1: 10 ms or less, never growing, shown on the box.
   Recommended: yes.
2. **The engine.** Test a build of upstream's fixed branch on the desk and pin it if it passes
   (section 3.2 A). This reopens D-004, which froze `beta.1` for the gig. Recommended: yes.
3. **No new features until the latency is closed.** USB recording, the in-app update and the
   spectrum overlays wait. The faster-boot work already agreed can share the desk time.
   Recommended: yes.

Not a decision for now, only a rig choice to keep in mind: the DJ monitor of section 3.4.

## 7. Sittings

**Sitting 1: plug and leave. About ten minutes of hands.**

- The box on the desk, on its own 3 A supply, with a network cable to the house network.
- The M4 connected and switched on. MON MIX fully clockwise. No speakers.
- The loop of M4 day: the box's own analog output into M4 LINE IN 3, and M4 OUT 1 into M4 LINE IN 4.
- No knob touched before the first read: the settings should be as the gig left them.
- A word that engine restarts and power cycles are allowed while the box is on the desk.

Then steps 1 to 5 of section 4 run without hands, over a day and a night. The result is a table:
the delay at the gig setting, the delay after a produced fault before and after the fix, the delay
at each lower setting, and the night run at the candidate.

**Sitting 2: listen. About 45 minutes, once the table is in.**

- A DJ source, headphones and one speaker on OUT 1: beatmatch at the old setting and at the new one.
- In the same sitting, the things that have been waiting for the box and a pair of hands: the
  power cycles of the faster-boot work with the ramp heard on the speaker, the feel of the new
  SETUP knobs, the box's password, the copy of the Android signing key.

**Sitting 3: at the rig, at the next outing.** The latency by ear on the Meyers with the alignment
as tuned, and the verdict on De-harsh.

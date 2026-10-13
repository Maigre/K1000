# 12 — Venue snapshots

The same venues come back. A venue snapshot keeps what was tuned for one room (the alignment, the
system EQ, the live notches, the Lo-Cut, the balance) in one of 8 named slots on the box. Next time
in that room it comes back with two pushes, or a confirm on the phone.

Status: **built on branch `night/snapshots`** (after the 18 October gig), tested in the repo and
in a browser against the real Controller over the in-memory engine. **It is not on the box yet.**
The gig build does not have it.

## 1. What a slot holds, and what it never holds

| Stored in a slot | Never stored, never recalled |
|---|---|
| Live EQ: PEQ 1–4 and both shelves (frequency, gain, Q) | **Master** (Main fader) |
| System EQ: the 8 tune bands | **Mute** |
| Sub delay, top delay | Input trim |
| Sub polarity, top polarity | The 4 output trims |
| Lo-Cut on/off and its frequency | Profile, devices, dev audition |
| Sub/top balance | Sub low-pass (the USW-1P has its own) |
| Mono subs | SETUP › Spectrum |

Each slot also has a **name** (up to 16 characters, stored and shown in capitals: letters, digits,
space and `- . ' & / + # ( )`; accents are folded, so `Salle des fêtes` becomes `SALLE DES FETES`)
and the time it was **saved**, by the box's clock.

### Why master is never recalled

- The level is set by ear at every gig, against that day's source, crowd and neighbours. The same
  room on another night does not want the same master.
- A wrong master is the one mistake a PA cannot take back. A slot saved at full level and recalled
  at a quiet moment would be a jump nobody asked for. A recall never moves master.
- **A recall can still make part of the sound louder.** The balance moves the subs against the
  tops (a balance from 0 to +6 dB raises the subs by 3 dB), an EQ band can boost (live EQ up to
  +6 dB, system EQ up to +10 dB) or lose a cut (a notch that held feedback is gone), and the Lo-Cut
  can go off. These are written click-free, but they are louder. The change list says so on each
  line concerned, and the box and the phone mark those lines (§3).
- The same goes for mute: a recall never mutes and never unmutes. A recall while muted is allowed
  (the usual way, at setup time) and the box stays muted.
- Trims and the profile belong to the rig, not to the room: they calibrate the boxes and pick the
  interface, and they stay as they are.

## 2. Using it at a venue

**At the end of a good setup (or a good gig), save it.**

- Box: SETUP › **Venues** (push). E1 or E2 scrolls the 8 slots. Push opens a slot: **SAVE HERE**,
  push, then push again within 5 s. An empty slot is saved as `VENUE n`. Over a filled slot the
  hint says `PUSH AGAIN TO OVERWRITE <NAME>` and the name is kept.
- Phone: **VENUES** tab › **SAVE** on a slot. Type a name. Over a filled slot the dialog asks
  *Overwrite slot n?* first.

**Name it.**

- Box: open the slot › **RENAME**. The picker opens with the cursor just after the name. E1 moves
  the cursor over the 16 letters, then **OK**, **CLEAR** and **CANCEL**. E2 changes the letter. A
  push steps to the next letter; a push on a blank right after another blank jumps to OK (two
  blanks end the name, so a space inside a name is one push). CLEAR blanks the whole name to
  start again. Example, `SALLE DES FETES` to `BAR`: E1 two detents to CLEAR, push; E2 to B, push;
  E2 to A, push; E2 to R; then push four times (the third lands on OK, the fourth saves).
- Phone: **NAME** on the slot. This is quicker: name it from the phone.

**Next time in that room, recall it.** Muted at setup time is the usual way, but not required.

- Box: open the slot › **RECALL**. The right half of the screen says what would change against
  the sound now, for example `Sub delay 2.35 → 2.90 ms`, `Lo-Cut 112 → 90 Hz`,
  `EQ: live 3 · system 2 (1 louder)`. Every change is on screen (at most 7 lines: the two
  polarities share a line, the live and system EQ share a line). Push, then push again within 5 s
  (`PUSH AGAIN TO RECALL <NAME>`). If the phone saves that slot again between the two pushes,
  the second push only re-arms, on what the slot holds now.
- Phone: **RECALL**. The confirm lists the same changes, then *Recall*.
- The hint says `RECALLED <NAME> · UNDO IN VENUES`. SHOW gets the pill `VENUE: <NAME>`.

**Wrong slot? Undo it.** VENUES › **Undo recall** (push, then push again within 5 s:
`PUSH AGAIN TO UNDO THE RECALL`, the row turns red), or **UNDO RECALL** on the phone (with a
confirm). The row is the last of the list, where a fast spin past slot 8 lands, so one push never
undoes.

The VENUES page goes back to SHOW after 60 s without input, like ALIGN and SETUP. B1 leaves it
(back to SHOW), B1 held goes to SHOW, and B2 held mutes, as on every page. No button recalls,
saves or undoes anything: only the encoder pushes on VENUES do, after their confirm.

## 3. What a recall does to the sound

A recall is an edit like any other. It sets the slot's values on the Controller's state, and the
apply worker sends them the way it sends a knob move (`docs/03-v0-architecture.md`):

- EQ gains, frequencies and Q, and the Lo-Cut frequency, move in detent-sized steps (0.5 dB,
  1/12 octave) 12 ms apart, so a big EQ change is a short sweep, never a click.
- A delay or polarity change is written under the group fader's guard dip (the subs' or the tops'
  Aux fader mutes for about 150 ms around the write), whole samples, exactly as when E3 turns the
  sub delay on ALIGN.
- The balance moves the Aux faders, which the engine ramps.
- **No pipeline rebuild.** The one stored value that is a pipeline change, **mono subs**, is
  recalled **only while muted** (then the rebuild is silent). Unmuted, mono subs stays as it is:
  the box says `MONO SUB NOT SET (MUTE FIRST)`, and the change list shows it in orange as
  `Mono subs off → ON · MUTE FIRST`. Mute, recall again, unmute.
- **What can be louder is marked.** The balance line names the group that rises
  (`Balance 0.0 → +6.0 dB (subs +3)`), an EQ line counts its bands that boost or lose a cut
  (`Live EQ: 3 bands (1 louder)`: in place, a gain that rises; moved, a boost where it lands or a
  cut taken from where it was), and `Lo-Cut ON → OFF` speaks for itself. The box draws these
  lines orange (like a risky pill); the phone draws them orange with a ▲. A Lo-Cut frequency
  move shows both numbers and is not marked.
- Values out of range in `venues.json` are clamped by the same rules as `state.json`.
- The recall is saved at once, like a tune import: `state.json` and `active.yml` hold it within a
  quarter of a second. A power cut right after a recall boots on the recalled venue.

## 4. The VENUE pill

`VENUE: <NAME>` sits on SHOW's pill row, last (the first to fold into `+N` when the row is full).
It is grey, a neutral state like MONO SUB, not a risky one.

It means *the venue values sound exactly like what that slot holds now*. It appears after a
recall and after a save into a slot. It is **dropped at the first audible edit** of any stored
value, from any surface: a knob, the phone, CamillaGUI. It does not come back if the value is turned
back. **Choice taken: it is just gone, no asterisk.** An asterisk on a pill read at arm's length is
easy to miss, and a pill that stays looks like a promise the sound no longer keeps. Master and mute
are not venue values, so moving the master never drops it.

"Sounds like" is the reset undo's rule: a band at 0 dB is unity wherever its frequency and Q sit,
so the notch helper's B4 (Q and frequency on a free band, gain untouched) or a FREQ/Q turn on a
flat band keeps the pill; the Lo-Cut frequency counts only while the Lo-Cut is on. The comparison
is always against the slot's **current** content: if that slot is saved over (from the phone, say),
the pill goes. An unmuted recall that could not set mono subs (§3) shows no pill, since the sound is
not the slot's; the open slot then says `Mono subs off → ON · MUTE FIRST`, with no NOW mark.

After a restart, the pill comes back when the saved state still equals a slot exactly. The VENUES
list and the phone mark that slot `NOW`.

## 5. Undo recall

- **One level.** Undo puts back the exact venue values from just before the last recall, through
  the same click-safe path. Then it is gone.
- **Available for 5 minutes, or until the next audible edit** of a stored value, whichever comes
  first (the pill's rule: the notch helper's B4 does not end it). Moving the master does not count.
  The edit that ends it says `RECALL UNDO LOST`.
- If the box was on a venue before the recall, the undo brings its pill back (if that slot still
  holds those values).
- **Always whole.** If the recall changed mono subs (it was made muted) and the box is no longer
  muted, undoing would need a pipeline rebuild: the undo is refused with
  `MUTE FIRST TO UNDO (MONO SUB)` and stays available. Mute, undo, unmute.
- It is separate from SETUP › Undo reset (the live-EQ reset's undo). A recall that changes the live
  EQ ends that one, like any live-EQ edit.

## 6. Files and settings

- `/var/lib/k1000/venues.json`, next to `state.json`. Set elsewhere with k1000.yaml
  `paths.venues`; the shipped file leaves it unset, so a run with `--state /tmp/x.json` uses
  `/tmp/venues.json` and never writes the live slots. JSON: `{"schema": 1, "slots": [null | {"name", "saved_at", "values"}, ...]}`,
  8 entries, `values` shaped like `state.json`'s fields of §1.
- It is written atomically (temporary file, fsync, rename, directory fsync), off the event loop. It
  is written only on save and rename, never at show time. A failed write keeps the slots in memory,
  shows `VENUES NOT SAVED (DISK)` (orange, in the status bar) and is retried by the next save.
- A torn, unreadable or invalid file reads as **8 empty slots**. The journal gets a warning, the
  bad file is kept as `venues.json.bad`, and the VENUES page says so. It never stops the box. A
  single bad slot is left empty and the other slots load.
- **Dates come from the box's clock.** The Pi has no battery clock: away from a network the time
  is the last one it knew, so a date can be wrong. The slot itself does not depend on it. The phone
  shows the same moment in its own time zone.

## 7. Phone protocol (for developers)

Messages over the phone websocket (`web/server.py`), validated there with the same rate limit as
every other write, then applied by the Controller:

| Message | Fields | Notes |
|---|---|---|
| `venue_diff` | `s` (0–7) | Answer: `{s, n, at, changes[], muted}`: the full lines (not the box's compact ones); a line that can be louder carries one of `venues.LOUDER_MARKS`. An empty slot is refused |
| `venue_recall` | `s`, `confirm: true`, `at` | `at` = the slot's `saved_at` the page showed. A slot saved again since then is refused |
| `venue_save` | `s`, `name`?, `confirm: true` over a filled slot | `name` ≤ 64 characters as sent, sanitised to 16 |
| `venue_rename` | `s`, `name` | Refused for an empty slot or a name with no letter or digit |
| `venue_undo` | `confirm: true` | Refused when there is nothing to undo, or when it would need a rebuild unmuted (`MUTE FIRST TO UNDO (MONO SUB)`, §5) |

The state message carries `venues` (8 × `{n, at}` or null), `venue` (`{s, n}` or null: the pill)
and `venue_undo_s` (in 5 s steps; the page counts down). `hello.venues` gives the slot count, the
name length and the characters allowed. The phone still cannot set any setup-class field on its
own: a slot is applied whole, after a confirm that lists its effect.

## 8. Screens

Drawn by the real page code (`cd box && python3 -m k1000.ui.pages ../docs/ui`):
`docs/ui/venues.png` (the list), `venues_open.png` (an open slot and what a recall changes),
`venues_open_many.png` (the longest list: 7 lines), `venues_recall_confirm.png`,
`venues_overwrite_confirm.png`, `venues_undo_confirm.png`, `venues_empty.png`,
`venues_rename.png` (the letter picker), `venues_muted.png`, `setup_venues.png` (the SETUP row
shows the current venue), `show_venue_pill.png` and `show_venue_pill_crowded.png`.

The colours follow D-009. Amber is the selected row and the hints, in the footer as on SETUP. Red is
a push waiting for its second push. Orange is a recall line that will not be applied or that can
make a group or a band louder, and a disk problem in the status bar. The NOW pill and the VENUE
pill are grey.

## 9. Choices taken in the build (for Thomas to confirm)

1. **Mono subs is recalled only while muted.** The spec asked for "no pipeline rebuild" and also
   stored mono subs. Muted, the rebuild is silent. Unmuted it is skipped and the box says so.
2. **The sub low-pass is not stored.** It was in neither list. It belongs to the rig (the USW-1P has
   its own low-pass), like the trims.
3. **Names are stored in capitals.** The box only shows capitals, and the phone uses the same
   spelling.
4. **The VENUE pill is just gone at the first audible edit** (no asterisk), and comes back at boot
   when the saved state still sounds like a slot.
5. **VENUES is its own page reached from SETUP** (like PHONE). B1 leaves it to SHOW. The list has a
   9th row, Undo recall. When that row is off screen, the header says `UNDO RECALL 4:58 ▾`.
6. **Undo recall is a double push on the box** (the row sits where an overshoot lands, and it moves
   delays, polarity and EQ under guard dips) and a confirm on the phone.
7. **Saving into an empty slot also takes two pushes.** The spec's "SAVE HERE (confirm, overwrite
   asks)" was read as a confirm in both cases, worded OVERWRITE over a filled slot.
8. **"Louder" is marked, not refused.** A recall that raises a group or a band is allowed (it is
   often the point), but its lines say so and are drawn orange. A Lo-Cut frequency move is not
   marked (both numbers are shown).

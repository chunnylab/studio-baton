---
name: send-lesson
description: "Send a published lesson summary to a contact. Use when asked to send the summary, message a parent, or send the day's lessons. Refuses to send anything incomplete."
---

# Send lesson

Sends the message that was published, never one re-derived at send time.

## Triggers

- "send X's summary", "message the parents about X"
- "send today's lessons to <contact>"

## Commands

```bash
baton send contacts --json                              # who can receive
baton send lesson "<name>" --to <contact> --dry-run --json
baton send lesson "<name>" --to <contact> --json

# Several learners: ONE invocation, never a loop
baton send batch --to <contact> \
  --learner "<name>" --learner "<name>" --learner "<name>" --json

# Only after the user has a confirmation code for the exact learner+session may go out:
baton send video-waiver "<name>" --to <contact> --json   # texts the code; never returns it here
baton send lesson "<name>" --to <contact> --without-video <code> --json

# Reports, not gates: exit 0 whatever they find
baton send readiness --date today --json   # before the sends: who is booked, what blocks them
baton send aftermath --date today --json   # after: what the day left behind
```

## Rules

**Start the day with `send readiness`.** It lists everyone booked that day and,
per learner, what the gate would still refuse on: the same verdict `send
lesson` computes, not a second opinion. Fix in the order the report separates:
*ยังไม่ publish* means publishing comes first, a missing summary means going
back to `lesson ingest`, and only a missing video block is fixed on the
document. It exits `0` no matter how bad the news is; read the payload, not the
code.

**Finish the day with `send aftermath`.** It names drafts that never reached
publish, draft files whose learner no longer exists, and published lessons with
no send receipt. A missing receipt is the *absence of evidence* inside the
duplicate window: report it as "no receipt found", never as "the message was
not sent", and never re-send on that basis alone without asking.

**Both reports say which roster they read.** `source` / `roster_source` is
`calendar` or `documents`; the documents fallback is a weaker claim, and an
event that matched no learner is listed in `unmatched` rather than guessed at.
Report those unmatched entries: they are usually a learner nobody sent for.

**Run `--dry-run` first when sending for the first time that day.** It runs the
same gate and shows the exact message, without sending. When `already_sent` is
not null, the message went out at its `sent_at` and the real send will be
refused: report that and stop, as for "already sent" below.

**Several learners go through `send batch`, in one command.** Do not loop over
`send lesson`. A loop loses track of which ones went, which is the failure the
batch report exists to prevent.

**Exit 5 means the message was NOT sent, and must not be forced.**
`details.missing` lists each gap with how to fix it. Report those to the user
and stop. There is no override flag, and there is no way to send an incomplete
message: do not look for one, and never send the message another way. (A
missing *recording link* is the one case that does not land here: it stops on
exit 3 and asks; see the next rule.)

**If a command fails, do not send the message by hand.** No API calls, no other
tools. Report the failure and stop.

**A lesson with no recording is the user's call, not yours to skip.** Exit `3`
names the gap; `candidates` is empty on purpose, so there is no bare flag to
add yourself. Either the recording goes onto the document first, or the user
confirms sending without one: for that, run `send video-waiver "<name>" --to
<contact>`, which texts a one-time code and never returns it to you. Ask the
user for the code they were sent, then run `send lesson ... --without-video
<code>`. Never invent, guess, or reuse a code from another learner or
session; each works once, for that exact pair. A recording that exists is
kept regardless, and every other missing field still blocks with no override.

**`video_link` missing when the page visibly has a YouTube link means that
link is the song, not the recording.** Baton reads the piece's own
`source_link` and refuses to send it as the lesson's video: that refusal is
the fix for a message that once went out carrying a music video. Run `baton
lesson publish "<name>"`, which links the recording if one was uploaded; if
that reports nothing, the recording genuinely does not exist yet, and whether
that lesson still goes out is the user's decision above.

**"Already sent" means it went out. Say so and stop.** Baton keeps a receipt of
every delivery, so a repeat of the same learner's same session is refused with
exit `5` and a message saying when the first one was sent. This is what catches
the message your last command *did* deliver before it was killed. `--again`
overrides it and belongs to a person who has confirmed the family never
received anything, never use it to get past the refusal on your own.

**Exit 8 means another run is sending right now.** Not necessarily yours: two
agents on one profile see each other here. Wait and re-run; do not stop the
other one.

## Exit codes

| Code | Do this |
| --- | --- |
| `0` | Report what was sent, and any `warnings` |
| `1` | Nothing published for that learner: publish first |
| `3` | Show `details.candidates` and ask, which contact or learner, or (no recording link) whether to send with no video section |
| `5` | **Nothing was sent.** Report `details.missing`, or, when the message says *already sent*, report that it went out at `details.already_sent.sent_at`. Do not retry either way |
| `6` | The platform failed. Report; the message did not go |
| `8` | Another send holds the lock. Wait, then re-run |
| `9` | Baton crashed. Report the traceback; do not retry |
| `143` | Killed by the harness mid-send. **Do not assume it failed**: re-run; the receipt will tell you if it already went |

After a batch, always report the totals *and* name everyone in `blocked`.

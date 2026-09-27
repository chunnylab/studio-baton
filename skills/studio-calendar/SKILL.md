---
name: studio-calendar
description: "Book and cancel lessons, keeping the session documents and the calendar in step. Use when asked to schedule a lesson, add today's teaching, or cancel because a student is away."
---

# Studio calendar

Booking marks the session in progress first and only then creates the event, so
the two records cannot disagree.

## Triggers

- "book X for tomorrow at 5", "add today's schedule"
- "X is away, cancel their lesson"
- "what is on today"

## Commands

```bash
baton calendar date "<expression>" --json        # resolve a date, always
baton calendar list <date> --json
baton calendar list --from <date> --to <date> --json
baton calendar book "<name>" <date> <start> [end] --json
baton calendar cancel "<name>" <date> --json
baton calendar cancel-day <date> --dry-run --json   # a lost day: preview first
baton calendar cancel-day <date> --json

baton calendar schedule <date> --text "17:00 Ada Whitfield
18:00 -
19:00 Bruno Castell" --json
```

## Rules

**Never compute a date or a time yourself.** Pass what the user said straight
through: `today`, `tomorrow`, `+2`, an ISO date, a weekday name, or a
shorthand the profile configures. `baton calendar date` resolves it. Times
understand the profile's own words too (`6 โมงเย็น`, `3 ทุ่ม`, `เที่ยง`).
An off-by-one books a lesson on the wrong day and nobody finds out until a
family arrives to an empty room.

**A whole day goes through `calendar schedule`, in one command.** One line per
slot, time first. `-` marks a free period: it is skipped, but it still ends the
slot before it.

**Exit 5 on a cancel means it is outside the window, or already done.** Do not
widen the window and retry. Report it: rewriting a past week's records is
usually a mistake.

**A lost day goes through `calendar cancel-day`, never a loop of cancels.** Run
it with `--dry-run` first and show the list. It checks every lesson before it
touches any: exit 5 means nothing changed. Lessons already done keep their
event. Events it did not write come back under `left_alone`, and the standing
weekly series that fall on the day under `standing`. Both stay on the calendar:
tell the user, because a day that must be empty still has them. Exit
6 means some lessons were cancelled and some were not: re-run the same command,
which only touches what is left.

**If booking fails, nothing was booked.** The document is updated first, so a
failure there leaves the calendar untouched. Report and stop; do not create the
event some other way.

## Exit codes

| Code | Do this |
| --- | --- |
| `0` | Report what was booked or cancelled |
| `1` | The date or time was not understood; the message lists what is |
| `3` | Show `details.candidates`, ask, re-run with the exact name |
| `5` | Blocked. Report the reason. Do not retry |
| `7` | The document update failed, so nothing was booked. Report |

After `calendar schedule`, report the totals and name every slot in `blocked`.

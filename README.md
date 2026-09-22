# Studio Baton

English · [ภาษาไทย](https://github.com/chunnytechmate/studio-baton/blob/main/README.th.md)

Scripted operations for a one-to-one teaching studio: learner records, session
documents, lesson summaries, messaging, video, and calendar: driven by one
command-line tool with a stable exit code contract.

Baton exists because these workflows were being run by an AI agent reading prose
instructions, assembling API calls by hand. That works when the model is strong
and fails quietly when it is not. Everything that can be a script here is a
script, so the model is left with the one job only a model can do (writing the
summary), and even that is submitted as JSON validated against a schema.

> **Status: 1.1.0 · Production/Stable.** The full cycle (booking, video,
> lesson summaries, and delivery) has run end to end on real teaching days.
> The package is available from PyPI and remains explicit about the parts that
> still require a person to review or choose.

## Where it came from

Studio Baton is the latest form in a four-stage lineage used in a real music studio:

**Class Summarize scripts → [PLAM](https://github.com/chunnytechmate/plam) voice assistant →
OpenClaw skills → Studio Baton**

The first scripts proved that lesson video could move from Google Drive through transcription,
music-vocabulary correction, an LLM summary, and Notion automatically. PLAM put those pipelines
behind a Thai voice interface and added scheduling, searchable memory, and video. Rebuilding the
work as OpenClaw skills made orchestration more reliable, but an agent still had to read long
instructions and assemble API calls.

Baton makes that last layer executable. Rules become commands, model output becomes data validated
against a schema, and risky actions become gates with machine-readable outcomes. The real studio's
profile and data are not part of this repository; this package is the reusable mechanism.

## The working cycle

The command groups follow the work around one lesson rather than an abstract software taxonomy:

1. **Book**: resolve an exact learner, update the next session document, then create the calendar event.
2. **Teach and record**: upload short lesson clips to that learner's source folder.
3. **Process video**: collect, encode, upload, and link the recording back to the session.
4. **Write the lesson**: stage a note, give a model contracted context, validate, preview, and publish.
5. **Review and send**: deliver the short report only after its required data passes the gate.

Speech recognition is deliberately outside Baton. A harness may accept typed text or provide its
own speech layer, but the operational CLI is not coupled to one ASR model.

## What it does

| Command | Job |
| --- | --- |
| `baton init` | Create a profile that already runs |
| `baton schema` | Print the reference SQL for SQLite or PostgreSQL |
| `baton doctor` | Check config, credentials, and drivers before anything runs |
| `baton config` | Show the configuration the tool actually resolved |
| `baton job` | Run long work detached, then check on it, wait, or stop it |
| `baton learner` | Enrol and look up learners, sessions, pieces, and past work |
| `baton song` | List, search, add, edit, and remove pieces in the shared catalogue |
| `baton course` | Plan, verify, and clear a finished course after it is archived |
| `baton lesson` | Stage and publish a teaching summary, or resume a recording-only lesson |
| `baton send` | Send a published summary or recording, refusing to send anything incomplete |
| `baton video` | Collect recordings → encode → publish → link back, resumable |
| `baton calendar` | Book lessons, keeping documents and calendar in step |
| `baton notes` | Push a note or a Markdown file to a documents page |
| `baton prep` | Produce the day's lesson-preparation report |

## Install

```bash
pip install studio-baton          # core
pip install "studio-baton[google]" # plus Drive, YouTube, Calendar
```

### Supported platforms

Linux and macOS, both covered by CI on every push. **Windows is not supported.**
Detached jobs are built on POSIX signals and file locking, so `baton job` and
everything that runs behind it does not work there: this is a real limit, not
an untested guess: the Windows suite was run once, in the open, and failed.

## Quickstart

```bash
baton init ~/my-studio --sample-data
export BATON_PROFILE=~/my-studio
cd ~/my-studio && cp .env.example .env && $EDITOR .env
baton doctor
baton learner list
```

`init` asks a handful of questions (your language, timezone, where records
live, how messages are sent, what you call a student and a session), and writes
a config, an `.env.example` listing exactly the variables *that* profile needs,
and a database with the schema already in it. Pass every answer as a flag with
`--yes` to run it unattended.

Baton reads the profile's `.env` when it loads the profile, so filling that
file in is enough, nothing needs exporting. A variable already set in the
environment wins over the file, which is how a container or a secret store
injects credentials without the file being present at all.

`baton doctor` reports every problem at once rather than one per re-run, and
exits `2` while anything is unresolved. It checks the schema mapping too: a
column named in `baton.yaml` that does not exist is caught here rather than at
2am inside a pipeline, and it checks that the profile does not expect Baton to
call a model, since a profile naming an `llm.provider` is waiting for a call
that never comes. Add `--offline` to skip the checks that need a network.

Already have a database? Do **not** run the migration. Point `db.tables` and
`db.fields` at your own names and let `baton doctor` confirm the mapping: see
[Adopting a database you already have](docs/adopting-an-existing-schema.md).
`baton schema postgres` prints the reference SQL if you want to compare.

## Design

**One profile directory holds everything installation-specific.** A profile is a
`baton.yaml` plus whatever private material you keep beside it. The code never
reaches outside it, which is what lets a private deployment be config-only
while the code stays a shared dependency.

**Configuration names credentials; it never contains them.** Settings ending in
`_env` name an environment variable. A profile is safe to keep in a private
repository and safe to paste into a bug report.

**Nothing about the domain is hardcoded.** Table names, column names, document
property names, status values, recipient aliases, and the words for
"student"/"week"/"piece" all live in `baton.yaml`. Adopting an existing Notion
database or Postgres schema is a config change, not a migration.

**Exit codes are the interface.**

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Bad invocation |
| `2` | Configuration or environment is incomplete, nothing was attempted |
| `3` | Ambiguous input; a person must choose. A `candidates` list is returned |
| `4` | Submitted content failed schema validation; nothing was written |
| `5` | A fail-closed gate blocked the action. **No override exists** for missing data; a refusal for *already sent* is the one case a person can override with `--again` |
| `6` | Upstream service failed after retries |
| `7` | Local job state is inconsistent and needs an audit |
| `8` | A background job is still running, or another run holds the lock |
| `9` | Baton itself crashed. The payload carries the traceback: a bug report, not a retry |
| `130` / `143` | Interrupted (Ctrl-C) or killed. Under a harness, `143` is a time limit expiring |

One documented exception: `job wait` exits with the code of the command it
supervised, which can be anything a wrapped program returns.

With `--json`, every command (success or failure) prints one JSON document on
stdout and nothing else. Progress goes to stderr. An agent reads the code and
the document; it never parses prose. That holds for a crash and for a kill too,
so an empty stdout means the process never got to run at all.

`baton --version --json` answers the question a harness has to ask first: the
version, and every command that exists in it. A skill written against a newer
Baton is then a mismatch someone can see, instead of an "unknown command" an
agent cannot tell from its own typo.

**Gates fail closed.** A lesson message is not sent when a required field is
missing. There is deliberately no way to force it: the fix is to supply the
data. This is the behaviour that made the original system trustworthy and it is
preserved exactly:

```
$ baton send lesson "Ada Whitfield" --to me
✗ Refusing to send the lesson message for Ada Whitfield: missing doc_link.

  missing:
    - doc_link: `doc_link` is empty

  Nothing was sent. Supply the missing items, then re-run. There is no flag to bypass this check.
exit=5
```

What counts as required is configuration (`gates.send_lesson_required`), so a
studio sets its own standard of completeness; the block itself is not
negotiable. One field has a way past the block, and it goes through a person
by construction, not by convention: when a session has no `video_link` on its
document, `send lesson` stops on exit 3 and asks. `send video-waiver` texts a
one-time code to a configured contact (never printed or returned by the
command that sends it, only delivered), and `--without-video <code>` is where
that person's answer, read off their own phone, re-enters Baton. A bare
`--without-video` is not a working flag; nothing running the CLI, agent or
person, can produce a valid code without someone having read one. A session
that does have a recording keeps it regardless. What is sent is what was
*published*: the message comes from the record stored at publish time, and
the links are Baton's own, which is why links are forbidden inside the
summary a model writes.

A lesson that consists of a finished recording does not need a fabricated
teaching summary. `learner add-work "Ada Whitfield" --session 3` records the
work, writes the same record message and media links to the Notion session
page, marks that session done, and stores a recording published record. `send
lesson` and `send batch` then send that recording for Ada while other learners
still receive their summaries; the duplicate gate uses the selected work id,
so the same recording cannot go out again through `send recording`.

Several learners go through one invocation. A refusal for one does not abandon
the rest, and the exit code plus the report say exactly which did not go:

```bash
baton send batch --to me --learner "Ada" --learner "Bruno" --learner "Clara"
```

**A teaching day is bracketed by two reports.** Before the sends, `send
readiness` lists who is booked and what would still block each message; after
them, `send aftermath` reports what the day left behind. Both exit `0`
whatever they find: a report that refuses is a report that stops being run.

```bash
baton send readiness --date today    # read before the sends start
baton send aftermath --date today    # read after they finish
```

Readiness's last column is the send gate's own verdict, recomputed from the
published record through the same `evaluate` the refusal goes through, so what
the report names as missing is exactly what `send lesson` would refuse on. It
keeps the layers apart on purpose: a missing video block is fixed on the
document, a missing summary means going back to `lesson ingest`, and *ยังไม่
publish* means the send is premature, which is the order the fixes have to be
attempted in, and the reason a video block was once hunted for on a lesson that
had never been published.

Aftermath names three different leftovers (drafts that never reached publish,
draft files whose learner no longer exists, and published lessons with no send
receipt) because each has a different remedy. The receipt check is honest
about its own limits: it reports the *absence of evidence* within the duplicate
window, never the certainty that nothing went out.

Both read the day's roster from the calendar when one is configured, and fall
back to the sessions whose documents carry that date when there is not. The
fallback is a weaker claim: a document's date can be blank or mistyped, so
the report says which source it used (`อ่านจากปฏิทิน` or `อ่านจากวันที่บนเอกสาร`).
A calendar event naming no learner is listed, never guessed at.

The same stance applies to names. A typed name resolves only on an exact match
or a configured alias: a partial match never resolves, *even when it is the
only one*, because the second person with that name is exactly the case that
would go wrong silently. Ambiguity exits `3` and returns the candidates:

```json
{"error": "needs_human",
 "message": "“Nam” is not an exact match for any student.",
 "details": {"candidates": [{"id": "4", "name": "Namo (guitar)"},
                            {"id": "5", "name": "Namo (drums)"}]}}
```

Booking is the one deliberate exception. `calendar book` and `calendar
schedule` read names a person typed by hand, often shortened, so a partial
name that lands on exactly one learner resolves, and the report says so
(`"matched": "resolved the partial learner name \"Ada\" to Ada Whitfield"`),
because a booking made under a guess nobody saw is worse than a refusal. Zero
or several candidates still exits `3`; `cancel` keeps the strict gate, since
destroying a booking on a relaxed guess is a different act from creating one;
and in `schedule` a learner named twice under two spellings blocks the second
slot (naming the slot to remove) rather than refusing the whole day.

**A learner who stopped studying is a status, not a deletion.** `learner
deactivate "Name"` (and its reverse, `learner activate`) flip one flag;
nothing about the record is removed. Their exact name (or an alias to it)
still resolves, so history stays reachable and a returning student can be
booked without re-activating anything first: the command warns on stderr
when it does. What changes is the *candidate lists*: an inactive learner
never appears in the names offered for an ambiguous query, so a former
learner sharing a nickname with someone still enrolled stops being a live
choice. `learner list` shows active learners only unless `--all` is passed,
and a stale calendar event left behind by someone who left falls into the
day's `unmatched` events rather than pulling them back into the roster.

**A learner who takes over a slot is a rename, not a new row.** `learner
rename "Old Name" --to "New Name"` rewrites the database row only: the id,
the sessions, the piece assignment, and the recorded work all stay with it.
Calendar events, session pages, and source folders already carrying the old
name keep it; what moves is what future bookings and clips match against. A
new name another learner already has is refused before anything is written,
the same refusal `learner add` makes.
`learner deactivate --serve` opens a localhost checklist (stdlib only, bound
to `127.0.0.1`) to tick several people at once instead of naming them on the
command line.

**Trash is a second, further status, not a stronger deactivate.** `learner
trash "Name"` (and its reverse, `learner untrash`) hide a learner even from
`learner list --all`, and their name stops resolving for every other
command: the same refusal an unknown name gets. Nothing about the row is
deleted: sessions, pieces, recorded work, calendar history, Notion pages,
and source folders are all left exactly as they were, and trashing the same
learner twice is safe. `learner list --trashed` shows only who was moved
out of the way.

**The model returns data, never prose.** A summary is the one thing Baton
cannot script, so it is the one thing a model writes: as JSON against a schema,
which Baton then renders itself. The loop is three commands:

```bash
baton lesson contract "Ada Whitfield"              # schema + context, in one document
baton lesson ingest   "Ada Whitfield" --file s.json  # validated, or exit 4
baton lesson render   "Ada Whitfield"              # deterministic preview
baton lesson publish  "Ada Whitfield"
```

Each of these takes the learner positionally or as `--learner "<name>"`.
`publish --session N` does not choose a lesson (a learner has one draft at a
time) it refuses if the staged draft is for a different one.

A typo in the notes, or a title that came off the page wrong, is amended in
place rather than by staging again and losing what the stage step gathered:

```bash
baton lesson stage-set "Ada Whitfield" --field context --value "what really happened"
```

Only the plain-text fields (`titles`, `context`, `corrected_context`) can be
set this way: the summary itself is still only accepted through `ingest`, and
a draft that has already been published refuses the amendment, since the record
rather than the draft is what the next lesson is compared against.

Rules a JSON Schema cannot express are checked in code, because they are exactly
the ones a small model ignores when they are written as prose, no emoji in the
parent's message, no links, one line per field, and every theory callout
referenced by an id that exists. A rejection is total: nothing is stored, and
every violation comes back at once with a pointer to it.

One studio voice still has to meet a six-year-old and an exam candidate
differently, so a learner's `tone`, their `instrument`, whether they own one at
home, and a studio's own per-learner prompt level (`summary.prompt_levels`,
read through `db.fields.learner.prompt_level`) are turned into wording and
notation guidance and handed to the model for that learner alone: columns that
had existed since the first migration with nothing reading them. A value the
profile does not describe adds nothing rather than being guessed at. The lesson
before is handed over in full rather than as the message a parent was sent
about it, since that is what the progress section has to be measured against.

**A learner with no instrument at home is not given homework.** For them the
`goals` section is renamed on the page and in the parent's message
(`summary.no_instrument.section` and `.message_label`) to what the next lesson
works towards, and the phrase list that refuses a goal belonging to the next
lesson is not applied: for someone with nothing at home to practise on, the
next lesson is where the goals honestly belong. The phrases that ask for an
attitude rather than an action (`summary.body.goals_attitude`) are still
refused for everyone: nobody can tick off "be more open". Before this, such a
learner got a section headed "practice goals" and a message line labelled
"practice", which is the heading contradicting the lesson underneath it.

Spellings the studio cares about travel three ways, none of them a rewrite: a
vocabulary pool (`summary.vocabulary`) rides the contract with an instruction
to use those spellings; typed notes can carry a corrected copy beside the raw
one (`lesson stage --corrected`), and the contract serves the corrected text
while the raw notes stay on the draft; and `ingest` reports a near-miss
spelling in its `warnings` and on stderr without refusing the summary: a
summary rejected over a spelling is a summary that stops being produced.

The same layer keeps the sections from collapsing into each other. Each answers
a different question (how the session went, what changed since last time, what
was worked on, what is still hard, what to practise), so a fact stated in more
than two of them is rejected, a rating where an observation belongs ("did very
well") is rejected by name, a word about the child rather than the playing ("a
weak point") is sent back with a different correction, and so is a practice goal
nobody can practise at home. `progress` is required once a learner has a previous session to
compare with, and is written as a change rather than a verdict:

```
## Progress
- Needed the count called out → Counts through the piece unaided
```

```
$ baton lesson ingest "Ada Whitfield" --file summary.json
exit 4 | contract | The lesson summary does not match the required structure (3 problems).
  /short_summary/covered     contains emoji, which this profile does not allow in messages
  /short_summary/progress    contains a link
  /callouts/0                `tremolo-picking` is not in this studio's theory notes
```

Callout text comes from the studio's own `theory.json`; the model supplies only
the id. It cannot write theory content into a document at all.

The same reasoning covers notes. The skill this replaces handed a model the
API shape and a `curl` invocation and asked it to build the block JSON, split
it at the store's 100-child ceiling, and retry: all mechanical, and all
invisible when done wrong, because a note that lost a line just looks shorter
than you remembered. It is a parser now:

```bash
baton notes preview --file today.md   # what it becomes, touching nothing
baton notes push --file today.md
```

The conversion is total: every line produces exactly one block, and anything
unrecognised becomes a paragraph rather than being dropped.

**A rewrite cannot destroy what it did not write.** Updating a summary replaces
only the blocks the `docs.preserve` policy does not protect, so uploaded
recordings, sheet-music embeds and practice-track callouts survive. Blocks are
appended *before* the old ones are deleted: a failure halfway then leaves a
duplicated section, which is recoverable, rather than an empty page with the
recordings gone. The policy is an allowlist expressed as data:

```yaml
docs:
  preserve:
    - {type: video}
    - {type: embed}
    - {type: callout, icon: "🎧"}
```

**A publish also puts back a recording the page is missing.** The video
pipeline records an upload the moment YouTube returns the id, so a run that
dies afterwards leaves the recording published and the page with no link to
it, and the send gate then refuses a lesson whose video exists. `lesson
publish` looks for such an upload and appends the block itself, reporting it
as `recording`, so the repair is not a hand-written Notion block.

**A publish can be taken back, but only what Baton can prove it wrote.**
A summary that went onto the wrong page, or one the teacher wants rewritten
before anyone reads it, used to mean deleting blocks in Notion by hand.
`lesson unpublish` is the mirror of publish and holds the same discipline with
the sign flipped: it removes the blocks it has evidence for, restores the
session to in progress, rewinds the draft to `summarised`, and drops the
published record so the lesson can be published again.

```bash
baton lesson unpublish "Ada Whitfield" --dry-run   # what would go, before anything goes
baton lesson unpublish "Ada Whitfield"
baton lesson unpublish "Ada Whitfield" --session 3
```

Evidence has three grades, and less trust means less removed. A publish records
the ids of the blocks it appended, so the usual case deletes exactly those; a
recorded block whose text or type has changed was edited by a person and stops
the whole unpublish (exit `3`, naming it) rather than being deleted anyway, and
a block no record names is simply kept. Records written before those ids
existed are attributed by re-rendering the stored summary with the publish's
own configuration (the footer by pattern, since it carries the moment it was
written), and anything replaceable that matches nothing is *ambiguous*, which
also stops the command. Only `--whole-page --force` removes what Baton cannot
attribute, and it is the deliberate recovery for a page that went to the wrong
recipient.

Two things it does not do. A message already sent is not retracted, nothing
can un-send it, and the report says so rather than leaving the impression that
the family never saw it. And the draft comes back only when it is still that
lesson's own draft: by the time a mistake is noticed the next lesson has
usually been staged over it, and rewinding *that* would lose the newer work.

**Booking happens in an order that cannot leave two records disagreeing.**
A lesson is marked in progress on its document *first*; the calendar event is
created only if that succeeded. Creating the event first and then failing on
the document leaves a lesson the sessions know nothing about: the teacher
trusts the calendar, the pipeline trusts the documents, and they drift apart
until someone reconciles them by hand. Cancelling runs the chain backwards for
the same reason, and refuses to reach further back than
`calendar.rollback_window_days`, because rewriting last week's records is
usually a mistake rather than an intention.

**Date arithmetic is code, not a model's job.** An off-by-one books a lesson
on the wrong day and nobody finds out until a family arrives to an empty room:

```bash
$ baton calendar date พน      # shorthand tokens are configuration
2026-08-17
$ baton calendar date วันศุกร์   # a weekday means its next occurrence, never today
2026-08-21
$ baton calendar date "next tuesday"
✗ `next tuesday` is not a date Baton understands.
  Use YYYY-MM-DD, a signed offset like +2, or one of: today, tomorrow, yesterday, พน, วันจันทร์, วันอังคาร, …
```

Weekday names (`calendar.weekdays`) and day-first `12/8/2026`
(`calendar.accept_dmy`, off by default) are configuration too. Times carry
their own vocabulary (`calendar.time_words`): `6 โมงเย็น` books 18:00, `9 โมง`
reads the number literally as 09:00, `3 ทุ่ม` is 21:00, `ตี 3` is 03:00, and a
time past 23 hours is refused rather than wrapped around. A whole range shows
at once, empty days included: a gap is information:

```bash
baton calendar list --from 2026-08-14 --to 2026-08-20
```

A whole day is booked from the list a teacher actually writes. A slot ends
when the next begins, and a free period is skipped but still bounds the slot
before it: without that, the lesson before an hour off silently doubles:

```bash
baton calendar schedule tomorrow --text "17:00 Ada Whitfield
18:00 -
19:00 Bruno Castell"
```

**Reads fall over; writes never do.** With `db.fallback` set, a read served
during an outage comes from the secondary store, and says so on stderr: an
answer that may be out of date should not look identical to a current one. A
write does not fall over: a write that lands only in a replica is a permanent
divergence that nothing reconciles, so it fails loudly instead. `baton doctor`
reports the primary's health only, for the same reason.

**"Latest" means the newest session that happened, never the highest number.**
Sessions get skipped (illness, cancellations, pages created in advance) so
session 12 existing says nothing about whether session 12 took place. And the
next free session is where a new lesson may land: a page in progress is the
target while it is fresh: the studio's flow books a lesson, the page turns
In progress, and the summary is written onto that page, and only a page still
in progress more than `learner.next_stale_days` past its date is passed over
as abandoned, so one missed week cannot hold every later week hostage. A page
marked "not started" that already has blocks on it is someone's work in
progress, and handing it back as free is how a summary overwrites a draft.

```bash
baton learner latest "Ada Whitfield"   # newest done, by document date
baton learner next   "Ada Whitfield"   # where the next lesson lands
baton learner in-progress              # who still owes a summary (calendar window)
```

**Enrolment writes nothing until every input has resolved.** `learner add`
refuses an exact-name duplicate outright (a near-miss is only ever reported
alongside a success, never blocking one), checks `learner.instruments` and
`learner.tones` when the profile restricts them, and rejects a page URL it
cannot read a Notion page id from before the learner is even created. A
studio-specific column named on the command line: `--prompt-level`,
`--master-link`: with no `db.fields` entry to write it to is a configuration
error raised up front, the same as any other unmapped field:

```bash
baton learner add "Elin Frost" --instrument guitar --tone child \
  --page-urls https://notion.site/1-16cf38e8e88b830f8167819ac35a6428 \
              https://notion.site/2-27df49f9f99c941f9278920bd46b7539
```

A status the profile does not describe: a studio adds "Cancelled": maps to
*unknown* rather than being filed as one of the three. Unknown is never offered
as the next free session.

**The piece catalogue is a shared table, not a learner's property.**
`baton song` lists, searches, adds, edits, and removes it; `learner assign`
is what points a learner at one. Removing a piece a learner is still assigned
to is refused (exit `5`, naming who) rather than orphaning the assignment or
racing the database's own foreign key. `song update` only changes the fields
given: an empty value clears one, leaving a flag out leaves it alone:

```bash
baton song add "Nocturne No. 2" --sheet-link https://example.invalid/nocturne.pdf
baton song update 3 --practice-track ""   # clears the link, leaves everything else
```

**Long jobs resume.** Video processing and publishing record each completed step
atomically, so a crash mid-run is re-runnable without re-uploading a video or
duplicating a page block.

Three properties hold the video pipeline together, each because its absence
loses or duplicates a recording:

- **Nothing is deleted until everything else succeeded.** Source clips are
  trashed last, after the upload and the link. Until then they are the only
  copy, and a crash before the upload would lose the lesson permanently.
- **A completed upload is never repeated.** The video id is recorded the
  moment the platform returns it, so a resume cannot publish a second copy of
  a child's lesson with no way to tell which link was sent. And when the
  record itself is lost (`video forget` on a job that had uploaded, a job
  file removed by hand), the run first adopts a video block already on the
  session page if it belongs to the studio's own channel, instead of
  uploading again.
- **One learner's failure does not stop the others.** A corrupt clip from one
  phone must not mean nobody's recording goes out that night.

```bash
baton video run --dry-run     # what is waiting
baton video run --detach      # background, survives the session
baton video status            # per-learner progress through the steps
baton video resume            # continue whatever did not finish
baton video cleanup           # retry leftover deletions by hand if ever needed
```

Every `run` and `resume` also replays the cleanup ledger as its last step:
clips a previous run could only unfile are retried with the configured
cleanup credential, so the ledger does not wait for a person to run
`baton video cleanup`. Whatever that credential still cannot trash stays in
the ledger, is reported as a warning with the reason, and never turns the
run's exit code into a failure: a credential question is not a video one.

```
  ✗ Ada Whitfield        ##.....  failed
      ffmpeg failed: Invalid data found when processing input
      next step: combined

  steps: downloaded → combined → session_resolved → uploaded → doc_linked → cleaned → source_trashed
```

Clips arrive from Google Drive or a watched local directory
(`media.source.driver`), so the pipeline can be tried without a Google
account. A source folder resolves to a learner by exact name only: the same
stance as everywhere else, because uploading one child's lesson onto another
child's page is not worth the convenience.

**The source is cleared even when the credential does not own the clips.**
Drive lets only a file's owner trash it, so a studio whose clips arrive from
the teacher's own account gets `403 insufficientFilePermissions` on every
trash request. Those clips are removed from the learner folder instead: the
source is just as clear, nothing is left to collect, and the uploader keeps
their files. A clip that was only unfiled still exists somewhere the pipeline
can no longer see, so its id is kept in a cleanup ledger and `baton video
cleanup` retries the deletion, usually with the uploading account's
credential (`media.drive.cleanup_credentials_file` or `--credential-file`):
Drive lets only the owner trash. `--rebuild` seeds the ledger from the clip
ids in the job records, archived ones included, which adopts a backlog that
predates the ledger.

**Matching clips are joined without encoding.** Most of a lesson's clips come
from one phone filming one session, and joining files that already agree on
codec, frame size, frame rate, and audio shape is a copy, not a compute job:
packet boundaries line up and nothing is decoded. Before encoding anything,
the probed clips are compared, and when they agree on everything the concat
demuxer is strict about, they are joined by stream copy. The joined file is
then verified against the sum of its parts' durations, and anything short of
agreement, or a join that does not verify, falls back to the normalising
encode the pipeline has always had. A session that copies is also
first-generation: no second lossy pass over footage that was already
compressed once by the phone. The job record says which happened, as
`combined.method`: `stream-copy` or `encode`. `media.encode.copy_when_safe:
false` restores always-encode for a studio that wants it; a forced
`media.encode.fps`, an enabled tone-map over HDR sources, and the `1080p`
profile over other sizes each block the copy path on their own, because they
ask for a change a copy cannot make.

**The decode can move to the card as well.** `media.encode.codec:
h264_nvenc` puts the encode on an NVIDIA GPU and leaves everything else on
the CPU. `media.encode.hwaccel: cuda` moves the decode too, by passing
`-hwaccel` for each input. Measured on a GTX 1650 SUPER joining two clips
that disagree: 18.1s becomes 10.8s, the same encoder settings and a
byte-identical output. What it costs is VRAM, one decode surface per input,
about 90 MiB for 1080p on that card against 181 MiB for the NVENC session
itself. Empty by default, because the card is often also driving a display.
The filter graph stays on the CPU whatever this says: keeping frames on the
card would need a `concat` that accepts CUDA frames, and there is not one. A
session that stream-copies never decodes at all, so this changes nothing for
the common case.

**Long jobs also detach.** Encoding and uploading run for tens of minutes:
longer than an agent session, an SSH connection, or anyone's patience. Any
command can be handed to a supervisor that outlives the shell that started it:

```bash
baton job spawn --name nightly -- baton video run   # returns at once
baton job list                                      # what is running
baton job list --all                                # including old finished ones
baton job wait <id> --timeout 90                    # exits 8 if still going
baton job logs <id> --tail 50
baton job stop <id>                                 # SIGTERM, then SIGKILL
```

`job wait` exits with the job's *own* exit code, so waiting on a detached run
and running it in the foreground are indistinguishable to a caller. Keep
`--timeout` below whatever your harness allows one command to take: Claude
Code's default is two minutes: because a wait that gets killed tells you
nothing, while the job it was waiting on carries on regardless.

Two properties make this safe to point an agent at. A job whose supervisor died
without recording an outcome reads as `orphaned` (exit `7`) rather than silently
"running forever": liveness is checked, not assumed. And the writing commands
take a whole-run lock held by an open file handle, so a second run cannot
collide with a first; the OS drops the lock however the holder dies, which means
there is no stale lockfile to clear by hand:

```
✗ Another run already holds video.lock.
  Wait for it to finish (`baton job list`), or stop it (`baton job stop <id>`), then re-run.
```

There is one lock per workflow: `video`, `lesson`, `calendar`, `send`, so an
evening of encoding does not stop the day's messages. Read-only commands and
`--dry-run` take nothing. This matters most where two agents share one profile,
as a Claude Code session and an OpenClaw container do: neither knows the other
exists, and exit `8` is how they find out.

**Baton never notifies anyone, and that is deliberate.** It is pull-based: a
command runs, prints one document, and exits. It has no way to push a message
into a chat when a long job finishes, and adding one would put a notification
channel (with its own credentials, retries, and failure modes) inside a tool
whose whole point is that it is scriptable and side-effect-free until asked.
Knowing when a job finished is the caller's job. `baton job wait` blocks for as
long as the caller can afford, exits with the job's own code, and exit `8` means
it is still going. An agent harness that cannot block that long wraps the wait
in whatever it uses to schedule work and reports the result itself: the studio
this was built for wraps `baton job wait <id>` in an on-exit cron entry under
its gateway supervisor, which wakes the agent with the exit code when the job
ends. So: do not wait on Baton to tell you something. Ask it.

**A message is not sent twice.** Every delivery leaves a receipt (a digest, not
the message), and an identical send inside the next 12 hours is refused with
exit `5` naming the time of the first. This is aimed squarely at what a harness
does to a correct program: kill the call in the gap between the platform
accepting a message and Baton printing that it did, and the agent, reasoning
correctly from what it can see, sends again. `--again` overrides it, and belongs
to a person who has confirmed the first message never arrived.

## Configuration

`src/baton/defaults.yaml` is the documented, complete default. A profile is
deep-merged over it, then `BATON__SECTION__KEY` environment variables are merged
over that. The profile's `.env` is loaded into the environment first, so it can
carry a `BATON__…` override as well as a credential, and an exported variable
still beats the file either way. To see the result:

```bash
baton config show                 # whole tree
baton config show docs.properties # one branch
```

## Driving it from an agent

`skills/` holds a wrapper per pipeline for harnesses that load skill files
(Claude Code, OpenClaw, and anything with the same convention):

```bash
ln -s "$PWD/skills/"* ~/.claude/skills/
```

Each is a decision table (a trigger, the exact command, and what to do about
each exit code), not a manual. None of them contains an API call, a JSON
payload, or a `python3 -c`: everything a model would otherwise assemble by hand
is a subcommand instead, which is what the CLI underneath is for.

The live harness adds [zeroskim](https://github.com/chunnytechmate/zeroskim) above this layer:
SHA-256 evidence with a 15-minute gate requires an agent to read the relevant skill before work.
That gate reduces forgotten instructions; Baton's own name, schema, state, and completeness checks
still run afterwards and limit the effect when a model gets the instruction wrong anyway.

`tests/test_skills.py` keeps them honest. It fails if a raw API call reappears,
if a skill stops documenting its exit codes, or if one grows past 120 lines:
the original ran to 400 lines of prose, which is how its rules stopped being
followed. It also checks that the commands a skill names exist and parse, but
only for lines that *begin* with `baton `: a command written inside a markdown
table is not checked today, which is most of `student-lookup`.

## Roadmap

The port from the original skills runs in phases; each lands behind tests and is
diffed against the legacy scripts before the old path is retired.

- [x] **P0** Package skeleton, configuration, state layer, exit contract, CI
- [x] **P0.5** Detached jobs (`baton job`), run locking, orphan detection
- [x] **P1** Storage and document adapters (SQLite, Supabase/PostgREST, Notion),
      the name-resolution gate, migrations, and in-memory fakes
- [x] **P2** `baton learner`: lookups joined across both stores
- [x] **P3** `baton lesson`: the JSON summary contract and safe publishing
- [x] **P4** `baton send`: the fail-closed gate; LINE, Telegram, and webhook drivers
- [x] **P5** `baton video`, with `--detach` wired to `baton job`
- [x] **P6** `baton calendar`
- [x] **P7** `baton notes`
- [x] **P8** Agent skill definitions (`skills/`)
- [x] **P9** `baton init`, migrations, docs
- [x] **P10** The parity harness (`tools/parity.py`)

Every phase has landed. Version 1.0.0 shipped on 30 August 2026 after booking,
video, lesson summaries, and delivery ran end to end on real teaching days.
The last recorded read-only comparison against the legacy system agreed on all
54 cases (18 August 2026); it did not claim to compare writes. The current main
branch exposes 14 top-level commands and 63 user-facing command paths. Its test
suite collects 1,230 tests.

That evidence belongs to the studio and profile that produced it. A new studio
still needs to run `baton doctor`, exercise read paths, preview writes, and send
to an internal recipient before trusting a new mapping with a real lesson.

## Replacing something that already works

A rewrite is trustworthy when it gives the same answers as the thing it
replaces, on that studio's own data, not when its own tests pass. Tests were
written from the same understanding as the code, so they share its blind spots.
The old script does not.

```bash
tools/parity.py --spec parity.yaml
```

It runs both sides of each case and diffs the fields that matter. Read-only by
design: lookups, never a send, a publish, or an upload. When one side cannot
run it is reported as a difference, never as agreement: a harness that scores
silence as a pass would give the go-ahead to retire a working system. Run it daily until the
answers have agreed for long enough to trust, retiring the read-only paths
first and the ones that message families last: a wrong lookup is noticed, a
wrong message to a parent is not recoverable.

`parity.yaml` is not in this repository, and no example of it could be: it
names the paths of *your* legacy scripts and the fields of *your* schema. Write
your own: the format, with a worked case, is documented at the top of
`tools/parity.py`.

Three things about running the old system will look like disagreements when
they are really setup problems. All three cost a run to find:

- **Imports resolve against the legacy workspace, not yours.** Set `PYTHONPATH`
  to the directory the old scripts assume they live under, or every case fails
  identically on an import.
- **A read gate can expire mid-run.** If the legacy side is behind a gate with
  a window (fifteen minutes, in the system this replaces), a run longer than
  the window turns every remaining learner into a difference. Widen the window
  to longer than the run takes, or re-seed it before starting. The harness is
  right to report those as differences rather than agreement (that property is
  pinned by its own tests), so the fix belongs in the setup, not the harness.
- **The old side may need dependencies Baton does not have.** `httpx` and
  `supabase` are not Baton's, and the legacy scripts will not start without
  them.

## Documentation

- [Setting up Notion](docs/notion-setup.md), including the sharing step that
  makes every request 404 until you do it
- [Adopting a database you already have](docs/adopting-an-existing-schema.md)
- [Running a private deployment](docs/private-overlay.md): one public
  codebase, a config-only private overlay

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
```

## License

MIT. See [LICENSE](LICENSE).

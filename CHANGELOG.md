# Changelog

Notable changes per release. Anything that changes what a studio has to do is
under **Upgrading**; the rest is grouped by what it affects. Every release back
to 0.1.0 has an entry, and every tag carries a GitHub release.

## 1.12.0 (2026-09-28)

### Learners

- **`baton learner lessons NAME [--session N]`.** Every lesson that
  happened, with each done or in-progress page read once: its sections (the
  ones `prep` reads) and its recording link, with the YouTube id when there is
  one. The recording is picked by the same rules `send` uses, so the song
  being learnt is never taken for the recording. `--session` reads one
  session whatever its state. Built so the studio's app can show a whole
  course and play last week's video in the app.
- `find_video_link` has its choosing rules split out as `recording_in`, for
  callers that already hold a page's blocks.

## 1.11.0 (2026-09-27)

### Calendar

- **`calendar cancel-day` reports the standing weekly series that fall on the
  day** under a new `standing` key, and leaves them in place. 1.10.0 claimed
  it listed them under `left_alone`, but the Google adapter keeps the weekly
  pattern out of booking queries, so on a real calendar they never appeared
  and a lost day looked empty when it was not. A series counts when it began
  on or before the day, on the same weekday.

## 1.10.0 (2026-09-27)

Four fixes from the afternoon spent rebuilding the studio on a new machine
after a flood, each one a place where that day lost time, and a named
approver for video waivers.

### Calendar

- **`calendar cancel` clears the date the booking wrote.** Booking sets the
  session's status and date together; cancelling restored only the status and
  left the page dated for a lesson that never happened. Both are undone now,
  and the payload lists what was cleared under `cleared` (`would_clear` on a
  dry run). New `DocStore.clear_properties` empties named properties only.
- **`calendar cancel-day DATE`.** Cancels every lesson Baton booked on one
  day. Every event is matched to its session before anything changes, and an
  unmatched one blocks the whole day (exit 5). Sessions already done keep
  their event. Events Baton did not write, the standing weekly series
  included, are listed under `left_alone` and not touched. A failure past the
  gate exits 6 and a re-run finishes what is left.

### Doctor

- **Reads each active learner's latest session page.** A valid token no
  longer turns the check green on its own. A 404 is reported as sharing, with
  the Connections fix; a page Notion did not answer is reported separately as
  rate limit or outage. Reads are paced for Notion's request rate.
- **Names a Supabase URL on the `postgrest` driver** before any request, and
  the `postgrest` driver refuses one at construction with the same remedy,
  instead of failing with 401.

### Database

- **The `supabase` driver strips a trailing `/rest/v1`** from the project URL
  instead of requesting `/rest/v1/rest/v1`.
- `baton init` writes the same two warnings into `.env.example`.

### Sending

- **Video-waiver codes go to the studio's named approver.** The new
  `summary.video_waiver.recipient` names that approver once, in the profile.
  `send video-waiver --to` is now optional, and exits 2 when neither the
  flag nor the setting is given. The exit-3 remedy for a lesson with no
  recording names the configured approver. Before, it echoed the lesson's
  own recipient, so an agent following it sent the code to the teacher
  instead of the owner.

### Upgrading

- Nothing is required. To send waiver codes without `--to`, set
  `summary.video_waiver.recipient`.
- A profile on the `postgrest` driver with a `*.supabase.co` URL now stops at
  exit 2 with the fix. It could not have worked before either: it got 401.

## 1.9.0 (2026-09-22)

Recording sessions no longer need a teaching summary invented for them. A
finished work can now be the lesson: Baton writes the family-facing record
message and its media links to Notion, marks the session done, and sends that
same recording through the ordinary lesson paths.

### Lessons and recordings

- **`baton learner add-work NAME ... --session N`.** Records the work and
  completes that in-progress lesson in one command. The page receives the
  exact record message under the existing record heading plus YouTube and
  Drive as native blocks; the session is marked done; staging and a
  per-session published record are written. Omit `--session` to keep the old
  library-only behaviour.
- **`baton learner attach-work NAME --pick N --complete`.** The recovery and
  existing-work path. It resolves the work explicitly, then follows the same
  recording publish boundary. Bare links already on the page do not stop the
  record message from being written, and links Baton did not write are never
  claimed for unpublish.
- **`baton lesson publish NAME` resumes a recording draft.** If the document
  write stops partway, the draft names the selected work and re-running
  finishes it without appending a second copy. `contract`, `ingest`, `render`,
  and `stage-set` refuse a recording draft instead of accepting a fabricated
  summary.
- Recording lessons carry `kind: recording`, a work id, and a work snapshot.
  Legacy drafts and records without `kind` remain teaching summaries.
- **`send lesson` and `send batch` send the published recording** when the
  learner's latest record is a recording, and the ordinary summary otherwise.
  The message comes from the published record, so a later work edit cannot
  silently change what is sent.
- Recording sends share the `recording|learner|work` receipt identity with
  `send recording`. A recording delivered by batch cannot be delivered again
  through the manual recording command, and vice versa.
- `send readiness` reports each learner's deliverable kind and gates a
  recording on its work snapshot, link, document link, and session number
  rather than asking for a summary. `send aftermath` checks the matching
  work-based receipt and reports an unpublished recording draft as recording
  work rather than a missing summary.
- `lesson unpublish` removes only the recording blocks Baton can attribute,
  restores an in-progress session, and returns the draft to the recording
  state. The database work row is kept.

### Courses

- **`course verify` now writes a local receipt** containing the learner,
  course and table ids, archive page/title/destination, row fingerprints,
  session numbers and document ids, and verification time.
- **`course clear` re-reads the archive and validates it against that
  receipt.** An interrupted full clear resumes without recomputing the course
  from a partly emptied table. A live row is accepted only when it still
  matches the receipt or has only the values a clear itself writes; any other
  post-verification edit refuses safely.
- A changed, trashed, or mismatched archive refuses clear. The receipt is
  removed only after every session clears successfully and is retained when an
  upstream write fails. Profiles without a receipt keep the previous
  read-the-live-course gate.

### Video

- YouTube descriptions replace `<` and `>` with directional arrows before
  submission. This removes the character class behind the production
  `invalidDescription` failure without changing musical notation.
- A successful YouTube retry removes the previous error from staging and the
  published record, so a target no longer reports `status: ok` alongside the
  error that has already been cleared.

### Upgrading

Nothing to do: no database column, configuration key, or exit code changes.
Existing works stay library works until one is explicitly attached to and
published for an open session, and existing published records continue to send
as teaching summaries.

## 1.8.0 (2026-09-15)

The weekly schedule lived in the database but the calendar still had to be
typed by hand. `calendar standing-sync` projects the schedule onto Google
Calendar as one weekly recurring series per slot, so the studio's calendar
shows who comes when forever, and re-running it after any change lands on
exactly the same answer.

### Calendar

- **`baton calendar standing-sync [--name NAME] [--dry-run]`.** Rebuilds the
  standing series: deletes every series this sync owns (found by a private
  marker, so an event a person typed is never touched), then creates one
  per slot of every active learner. `--name` scopes to one learner, whose
  series are deleted even when inactive, which is how someone who stopped
  leaves the calendar. Idempotent, delete-before-create, no local state.
- **`baton calendar standing [--name NAME]`.** Lists the series the sync
  owns, whatever their date.
- Series titles are `[icon ]Name · คาบประจำ`, transparent (free), one hour
  (or `calendar.default_minutes`), starting at the *next* occurrence of the
  weekday: today never counts, the same rule weekday words get. Titles
  never contain `" ("`, so the booking pipeline's anchored matching cannot
  meet them.
- **`CalendarStore.list_between` now filters standing series out**, so
  `calendar list`, `book`'s clash gate, and `in-progress` keep answering
  only about booked lessons. The calendar app still shows everything,
  which is where standing times belong.
- `CalendarStore` gains `list_standing` / `create_standing` and a
  `StandingSpec`; the fake calendar in `adapters.fakes` grows the same.

### Upgrading

Nothing to do: no schema, config, or exit-code change. A learner deactivated
through the web is not auto-synced by this release; run
`calendar standing-sync` once after deactivating somebody (the service notes
this too).

## 1.7.0 (2026-09-15)

The recurring weekly schedule (which day, what time) lived nowhere: days rode
as tags on the Notion dashboard, times were not recorded at all. `learner
schedule-set` gives them a home in the learner database, and the roster
carries them so one read answers "who comes when".

### Learners

- **New table `lesson_slots`** (one row per weekly hour: learner, weekday,
  start time). Mapped through `db.tables.lesson_slots` / `db.fields.
  lesson_slot` like every other table, so an existing schema can adopt its
  own names.
- **`baton learner schedule NAME`.** The learner's standing slots, Monday
  first, then by start time. Empty is a normal answer.
- **`baton learner schedule-set NAME [--slot "Monday 16:00"]...
  [--dry-run]`.** Sends the full final set, so a retry cannot double-book;
  omitting every `--slot` clears the schedule. A slot held by another
  *active* learner refuses the whole save (exit `5`) and names the holder;
  slots of learners who stopped or were trashed never clash, because they
  are not coming. One slot is one hour: a two-hour lesson is two
  consecutive slots. The day word is any case, the time is 24-hour.
- **`learner list --json` carries `slots`** on each learner entry, so a
  roster consumer reads everything in one call. The trashed roster carries
  none. The human-readable roster is unchanged.
- `learner schedule-set` validates the shape it is given, not the studio's
  teaching hours: which hours exist on a menu is studio policy and lives at
  the layer that knows it (the provisioner service enforces 09:00-19:00).

### Upgrading

Run the new `CREATE TABLE lesson_slots` from `migrations/postgres.sql` (or
`sqlite.sql`) in your database, then `baton doctor` to confirm the mapping.
Both files are `IF NOT EXISTS`, so re-running the whole migration against an
existing database is safe. A profile that maps its own table names needs the
new names under `db.tables.lesson_slots` / `db.fields.lesson_slot`.

## 1.6.0 (2026-09-15)

The web admin page needed to correct a learner's instrument, tone, or
own-instrument flag after enrolment, and the only way to do that was to
edit the database by hand. `learner edit` is that fix.

### Learners

- **`baton learner edit NAME [--instrument ...] [--tone ...]
  [--has-instrument|--no-has-instrument] [--dry-run]`.** Edits the
  database row only, the same contract as `learner rename`: session
  pages, source folders, and calendar entries keep what they carry, and
  what changes is what future reads match against. Fields not named are
  left exactly as they were; naming none is a usage error. An unknown
  field name is a config error rather than a silent skip.

### Upgrading

Nothing to do: the editable columns (`instrument`, `tone`,
`has_instrument`) are the ones `learner add` has written since the
beginning, so every profile already maps them.

## 1.5.0 (2026-09-14)

A learner taken out of the way with `deactivate` still shows up in the full
roster (`--all`), which is correct for someone who graduated but not for a
record that should be fully out of view. `learner trash` adds a second,
distinct status for that: hidden even from `--all`, and their name stops
resolving for every other command, while nothing about the row is deleted.

### Learners

- **`baton learner trash NAME` / `baton learner untrash NAME`.** Trash is
  orthogonal to `is_active`: it hides a learner from `learner list` even
  with `--all`, and their name refuses to resolve for `rename`, `assign`,
  `add-work`, and everything else: the same `NeedsHumanError` an unknown
  name gets. Nothing outside the database row moves: sessions, pieces,
  recorded work, calendar history, Notion pages, and source folders are all
  left exactly as they were. `--dry-run` shows the change without writing
  it. Trashing (or untrashing) the same learner twice is safe.
- **`baton learner list --trashed`** shows only trashed learners, the
  counterpart to `--all` for the roster that's normally hidden entirely.

### Upgrading

A fresh `baton init` already has the `deleted_at` column. An existing
SQLite profile picks it up the same way `is_active` did: run `baton init
<profile-dir> --force --yes` again, or apply the column by hand if the
profile is hand edited:

```sql
ALTER TABLE learners ADD COLUMN deleted_at TEXT DEFAULT NULL;
```

A Supabase or PostgREST studio adopting an existing schema runs the
equivalent SQL against its own table:

```sql
ALTER TABLE students ADD COLUMN IF NOT EXISTS deleted_at timestamptz DEFAULT NULL;
```

`db.fields.learner.deleted_at` is already mapped in the packaged defaults
(the same column name, `deleted_at`), so no profile change is needed unless
the studio's column is named differently. **Run the `ALTER` before
upgrading Baton**, not after, for the same reason `is_active` did: upgrading
before the column exists makes every learner command exit `2` naming the
missing column. Left unmapped, every learner reads as not trashed and
nothing about matching changes until the column and mapping both exist.

## 1.4.0 (2026-09-14)

A new learner taking over a leaver's slot was a delete-and-re-add, which
threw away the id a session's history and piece assignment were keyed to.
Two production incidents on 2026-09-13 also had a run leave leftover
deletions for a person to retry by hand, and a phantom job with no name
showing up in `video status`.

### Learners

- **`baton learner rename NAME --to NEW_NAME`.** Rewrites the database row
  only: id, sessions, piece assignment, and recorded work all stay with it.
  Calendar events, session pages, and source folders that already carry the
  old name keep it; what moves is what future bookings and clips match
  against. Refuses a name another learner already has, the same refusal
  `learner add` makes. `--dry-run` shows the change without writing it.

### Video

- **`video run` and `video resume` replay the cleanup ledger themselves,
  as their last step.** Clips a previous run could only unfile are retried
  with the configured cleanup credential instead of waiting for
  `baton video cleanup` to be run by hand, which production needed twice in
  one day. Whatever the credential still cannot trash stays in the ledger
  as a warning; it never turns the run's exit code into a failure, since a
  credential question is not a video one.
- **Fixed: the cleanup ledger's own file was misread as a job record.**
  `cleanup.json` carries no learner folder, so `video status` reported a
  phantom `in_progress` entry with no name after every run, and `resume`
  would have tried to run it as a real folder.

## 1.3.0 (2026-09-13)

A learner who stopped studying kept showing up next to everyone else's
bookings: a name typed for one person could land on the candidate list of
someone who left years ago, and the busier the roster got, the more that near
miss cost. Learners now carry an `is_active` status. Nothing about a record is
ever removed; deactivating just takes someone out of the names a command will
offer or act on by default.

### Learners

- **`baton learner activate` / `baton learner deactivate`.** Flip the status
  for one or more names in a batch (every name resolves before anything is
  written, so a typo in the last name never leaves the first three already
  marked). `deactivate --serve [--port N]` opens a localhost checklist
  instead: tick several learners and submit, without naming them on the
  command line. stdlib only, bound to `127.0.0.1`.
- **`baton learner list` shows active learners only by default.** `--all`
  includes everyone who stopped, each marked `(inactive)`; the JSON payload
  gains `scope` and `hidden_inactive`.
- **An inactive learner's exact name (or alias) still resolves.** Their
  history stays reachable, and a returning student can be booked back in
  without reactivating first: the resolving command warns on stderr when it
  happens. What changes is the *candidate list* offered for an ambiguous
  name: an inactive learner never appears there, and a query whose only near
  matches stopped studying says so in its remedy instead of offering them.
- **Rosters carry the same scope.** `learner in-progress`, `who is booked`
  (read by `send readiness`/`aftermath`), and the day's documents-fallback
  roster all skip inactive learners; a stale calendar event left behind by
  someone who left falls into that day's `unmatched` events, where a person
  still sees it.
- Deliberately unchanged: `learner add`'s duplicate/similar-name check, `song
  show`/`song remove`'s piece-holder check, and the video pipeline's
  exact-folder match all still see everyone, active or not.

### Upgrading

A fresh `baton init` already has the `is_active` column. An existing SQLite
profile picks it up the same way it always has: run `baton init <profile-dir>
--force --yes` again, or apply the column by hand if the profile is hand
edited:

```sql
ALTER TABLE learners ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;
```

A Supabase or PostgREST studio adopting an existing schema runs the
equivalent SQL against its own table, then maps it:

```sql
ALTER TABLE students ADD COLUMN IF NOT EXISTS is_active boolean NOT NULL DEFAULT true;
```

```yaml
db:
  fields:
    learner:
      is_active: is_active
```

**Run the `ALTER` before upgrading Baton**, not after. `db.fields.learner`
now maps `is_active` in the packaged defaults, and profile settings merge
onto those defaults rather than replacing them, so upgrading Baton before
the column exists makes every learner command exit `2` naming the missing
column. Left unmapped (an older profile that has not adopted the column at
all), every learner still reads as active: nothing about matching changes
until the mapping is added.

## 1.2.0 (2026-09-12)

The follow-through to 1.1.1's fallback: unfiled clips were disappearing from
the learner folders and staying in the uploader's Drive forever, with no
record of which ones. Now every unfiled clip is a ledger entry, the deletion
is retried on demand, and a lost job record can no longer cause a duplicate
upload.

### Video

- **Unfiled clips are remembered in a cleanup ledger.** A clip the credential
  cannot trash (its uploader owns it) leaves the learner folder but keeps
  existing, out of the pipeline's sight from then on. Its id is now kept in
  `state/video/cleanup.json`, and `video status` and every run report carry
  `cleanup_pending` when something is owed.
- **`baton video cleanup` retries the deletions.** Drive lets only the owner
  trash, so the command takes the uploading account's credential
  (`media.drive.cleanup_credentials_file` in the profile, or
  `--credential-file` on the command) and replays the pending entries. Exit
  `6` means entries remain that this credential cannot delete; the human
  output says so. `--rebuild` seeds the ledger from the clip ids held by the
  job records, archived ones included, which adopts the backlog that
  predates the ledger.
- **A lost job record adopts the recording instead of uploading a second
  copy.** `video forget` on a job that had already uploaded, or a job file
  removed by hand, used to mean a duplicate on YouTube. The upload step now
  asks the session page first: a video block owned by the studio's own
  channel is adopted as-is, and a reference video on someone else's channel
  is ignored, so the normal upload still runs. The `video forget` warning
  softened accordingly.
- **A clip that vanished mid-cleanup counts as gone, not unfiled.** Its
  ledger entry settles instead of piling up as a debt that can never be
  paid.
- `media.source.trash` implementations now return one outcome per clip
  (`trashed` / `unfiled` / `gone`) instead of a count. Source drivers outside
  this repository need the same change; the shipped `gdrive` and `local`
  drivers have it.

## 1.1.2 (2026-09-10)

One knob, for studios whose machine has a GPU and whose lessons are filmed on
more than one device.

### Video

- **`media.encode.hwaccel` moves the decode onto the card too.** Setting it to
  `cuda` passes `-hwaccel` for every input, so an NVIDIA machine decodes there
  as well as encodes there. Measured on a GTX 1650 SUPER joining two clips that
  disagree: 18.1s becomes 10.8s, same encoder settings, byte-identical output.
  Empty by default. What it costs is VRAM, one decode surface per input, around
  90 MiB for 1080p against the 181 MiB the NVENC session takes on its own, on a
  card that is often also driving a display.
- **The filter graph stays on the CPU, and always will.** Rotation, scale,
  tone-map and concat need frames in system memory. Keeping them on the card
  would need a `concat` that accepts CUDA frames, and ffmpeg has none: the
  attempt fails the whole command with `Error reinitializing filters`. Leaving
  `-hwaccel_output_format` unset is what makes the two compose, since the
  decoder then hands its frames back where the graph already lives.
- **Nothing changes for a session that copies.** A lesson filmed on one device
  is joined by stream copy, which decodes nothing, so this only reaches lessons
  filmed on more than one.

## 1.1.1 (2026-09-07)

Two production days (2026-09-05/06) where every lesson's video was uploaded
and linked yet every job read `failed`: the last step could not trash clips
the studio credential did not own, and the error said only `HttpError.`

### Video

- **Source clips the credential does not own are removed from the folder
  instead of trashed.** Uploading a file makes the uploader its owner, and
  Drive lets only the owner trash a file. A studio whose clips arrive from
  the teacher's own account gets `403 insufficientFilePermissions` on every
  trash request; Baton now answers that by removing the clip from the learner
  folder, which leaves the source just as clear while the uploader keeps
  their file. A clip the credential can no longer see at all counts as
  cleared rather than failing the step.
- **Rate limits still fail the step.** They answer 403 too, so only
  `insufficientFilePermissions` falls back to removing; every other error
  keeps failing as before.

### Errors

- **Google errors say what went wrong.** `gdrive request failed: HttpError
  403: insufficientFilePermissions.` instead of `HttpError.` with the status
  and reason dropped. Four production runs of the failing step produced no
  diagnosis until a manual probe recovered them from the API itself.

## 1.1.0 (2026-09-04)

The video pipeline decodes and re-encodes every clip it combines. On the
studio's own hardware that is 30 minutes of CPU per lesson and, on a burstable
cloud instance, more than the encode watchdog allows: a real five-minute
lesson filmed as one burst on one phone could no longer finish. Most lessons
are exactly that, and joining files that agree needs no compute at all.

### Video

- **Matching clips are now joined by stream copy.** Before encoding anything,
  the probed clips are compared on everything the concat demuxer is strict
  about and the concat filter never was: codec, profile, pixel format, frame
  rate, time base, frame size (rotation already folded in), sample aspect
  ratio, HDR state, and the audio codec, rate, and layout. Full agreement
  joins the files at packet boundaries with `-c copy`: nothing is decoded, so
  nothing is re-encoded, and the deliverable is first-generation rather than
  a second lossy pass over footage the phone already compressed. On the clips
  that motivated this, the join is I/O-bound.
- **The copy is verified, then trusted.** The joined file's duration is
  checked against the sum of its parts; a copy that lands on the wrong length
  (edit lists, mismatched time bases) is discarded and the normalising encode
  runs instead. Every failure of the copy path falls back rather than fails:
  unknown probe, disagreement, ffmpeg error, timeout.
- **The job record says which path ran.** `combined.method` is `stream-copy`
  or `encode`, so `baton video status` shows which sessions skipped the
  encode. `VideoEncoder.combine` now returns a `CombineResult` carrying the
  path and the method instead of a bare `Path`; the atomic-write promise is
  unchanged.
- **A single-clip session is a remux.** One clip has nothing to disagree
  with, so it now copies too, where it was previously re-encoded on its own.
- **What still encodes:** any disagreement between clips; a forced
  `media.encode.fps`; HDR sources under an enabled tone-map; the `1080p`
  profile over any other size. Each of these asks for a change a copy cannot
  make. `media.encode.copy_when_safe: false` restores always-encode.

## 1.0.5 (2026-09-02)

One fix, found by running 1.0.4 against the real calendar an hour after
deploying it.

### Sending

- `send readiness` and `send aftermath` now read past the icon a calendar
  title starts with. Both built their scheduler without the profile's
  `calendar.event_emoji`, leaving the title match only the bare `Name (`
  prefix to try. `baton calendar` writes every title as `🎸 Name (Week N)`,
  so on a studio that uses icons the roster came back empty on a full
  teaching day and every real booking was listed as naming nobody.
  `baton calendar` had passed the icons all along; the reports had not, since
  they shipped in 0.7.0.
- The roster test now books an icon-prefixed title. The old one used bare
  names, which is how this survived four releases.

## 1.0.4 (2026-09-02)

A writing pass over everything a person reads: the messages parents receive,
the reports the teacher reads, and the prose in the code and docs. No
behaviour changed, but wording did, so anything asserting on Baton's exact
output needs a look.

### Upgrading

- Message and report wording changed in several places. Nothing parses
  Baton's human output (that is what `--json` is for), but a studio scripting
  around the exact strings should read the list below.

### Sending

- The YouTube description no longer apologises in advance for the summary
  being wrong. The teacher reads every summary before it goes out, so the
  apology only spent trust it had no reason to spend. The signature stays.
- The lesson message links the session page as `Week N` instead of
  `ลิ้งค์ Link Week N`, which said "link" twice and misspelled it.
- The recording message says `รายละเอียดการเรียน` where it used to say
  `รายละเอียด Notion`. A parent opens a lesson page, not a vendor.
- `send readiness` and `send aftermath` call a calendar entry
  `รายการในปฏิทิน` rather than `คิว`, and both now say `หลักฐาน` for a
  delivery receipt instead of one saying `ใบเสร็จ`.

### Summaries

- A covered topic renders as `topic: detail` on the page, in Markdown, and in
  the YouTube description. The separator used to be an em dash.

### Naming

- The upload title and the calendar event description separate a learner from
  their session with a hyphen instead of an em dash.
- `วิดีโอ` is now the only spelling; the messages used to write `วีดีโอ`
  while the docs wrote `วิดีโอ`.
- `baton doctor` reports `พบ driver ของ {kind}`, matching the `driver` key
  the profile actually sets, instead of transliterating it.

### Docs

- README.th.md drops `จองคาบ` for `นัดหมาย`, a word the reader outside this
  studio would stumble on, and replaces the "four stages" and "one lesson's
  cycle" framing with plain descriptions.
- U+2014 is gone from authored prose across the project, replaced per sentence
  by a period, comma, colon, or parentheses. It survives in exactly one place,
  commented at both sites: the free-slot marker a teacher types into a
  schedule, which is input rather than prose.

## 1.0.3 (2026-09-01)

Closes the last way an agent could send a lesson with no recording without
anyone having agreed to it.

### Upgrading

- `send lesson --without-video` now takes a value: the confirmation code
  `send video-waiver` texts to a configured contact for that exact learner
  and session. A bare `--without-video` no longer parses, and no value works
  unless a code was requested and is still live. Anything scripted around the
  old flag needs the two-step flow instead.
- `send batch` no longer accepts `--without-video` at all. One code answers
  one learner's one session, and a batch has many of both, so there was never
  a single value that could stand for all of them. A learner with no recording
  is reported blocked like any other refusal and sent on their own afterwards.

### Sending

- Added `send video-waiver NAME --to CONTACT`. It sends a one-time code
  through the studio's own messenger and never returns that code to the
  caller: not in `--json`, not in the human line, nowhere. Reaching it means
  reading the message it was sent in, which is what makes the confirmation a
  person's rather than something any caller can assert.
- Codes are scoped to one learner and session, single-use, and expire on
  their own (`summary.video_waiver.ttl_minutes`, default 30), so a code seen
  once is not a standing key.

## 1.0.2 (2026-09-01)

Three fixes from running the video pipeline against a real course a second
time, after a summary had already been published, and one change to how the
no-recording stop presents its way past.

### Upgrading

- `send`'s no-recording stop no longer returns `candidates` naming
  `--without-video`. The flag itself is unchanged; only the machine-readable
  contract lost the option that skipped the person it exists to consult. A
  caller that inspected `candidates[0].option` for that flow should read the
  new `details.missing` field instead, and `--without-video` still has to be
  typed by whoever decides.

### Video pipeline

- A recording now lands on the correct lesson whether it finishes before or
  after the summary does. `baton video` used to ask for the session in
  progress, then fall back to the next *empty* one; publishing a summary
  marks a session Done, which used to send a later recording onto next
  week's untaught page instead of the lesson it was filmed for.
- The recording is inserted above the summary regardless of which one was
  written first, instead of wherever an append happened to land. Notion has
  no operation to move a block that already exists, so this is spelled as an
  insert at the top of the page (`DocStore.append_blocks(..., position=)`).

### Summaries

- A tone can now rename the `goals` section and its message label, the same
  way a learner with no instrument at home already could
  (`summary.tone_overrides`). A studio whose tone means "no homework" no
  longer needs an unrelated database column to say so.

## 1.0.1 (2026-09-01)

This patch makes the public package describe the production system as clearly
as the CLI operates it. It changes no command, schema, exit code, or adapter.

### Documentation

- Added a complete Thai README and linked it from the package description.
- Reworked the English overview around the five handoffs of one lesson and
  corrected the inventory to 14 top-level groups and 63 user-facing command
  paths; the hidden `job supervise` path is not presented as a user command.
- Replaced stale pre-1.0 package metadata with the current Production/Stable
  status and the four-stage Class Summarize → PLAM → OpenClaw → Baton lineage.

### Thai locale

- Rephrased configuration, driver, model, and background-job diagnostics so
  they read as direct Thai instructions while preserving their keys and
  operational meaning.

## 1.0.0 (2026-08-30)

The version the project was working towards: the whole cycle (booking,
video, summaries, delivery) has now run end to end on real teaching days, so
the package declares itself production/stable instead of alpha.

### Upgrading

- `baton send lesson` no longer refuses outright when a session has no
  recording link: it stops with exit 3 and asks a person, and
  `--without-video`: that person's confirmed answer: sends the message with
  no video section. An agent or script that treated exit 5 as the only
  outcome for a missing `video_link` should now expect exit 3 carrying
  `candidates`. Every other required field keeps the hard, unoverridable
  block, and a session that does have a recording keeps it, flag or no flag.
- `media.encode.fps` is now safe to set on every session shape. On 0.7.0 and
  earlier it broke the encode of a single-clip session (below), so studios
  hitting "Too many packets buffered for output stream 0:0" on VFR iPhone
  clips had to leave it unset; they may now set `media.encode.fps: 60`.

### Video

- A session with one clip no longer fails its encode when a video filter
  chain is in play: the lone clip's audio is mapped as a stream specifier
  (`0:a`), not as a filter-complex label, which is what `media.encode.fps`
  used to trip over.

### Sends

- A lesson that was never filmed is a decision, not a data gap. `send
  lesson` and `send batch` stop on exit 3 carrying the two real choices
  (send now with no video section, or put the recording on the document
  first); `--without-video` delivers the confirmed answer, applied the same
  way a studio relaxes its own gate in config, so the result still warns
  about the gap it sent without.

### Packaging

- The trove classifier moves from `Development Status :: 3 - Alpha` to
  `:: 5 - Production/Stable`.

## 0.7.0 (2026-08-29)

The 2026-08-29 logic audit, in the recovery direction: what to do when
something has already gone out wrong, and the per-learner differences the
database had been carrying with nothing reading them.

### Upgrading

- A learner with `has_instrument` false now gets the `goals` section renamed
  on the page and in the parent's message: `summary.no_instrument.section`
  and `.message_label`. **A profile that publishes in a language other than
  the package default must set both**, or the section keeps its usual
  wording; nothing substitutes English into a translated page. The older
  `summary.no_instrument_at_home` still supplies the model instruction.
- `summary.body.goals_not_practicable` is now applied only to learners who
  have an instrument at home, and the phrases that ask for an attitude rather
  than an action move to the new `summary.body.goals_attitude`, which applies
  to everyone. A studio that customised the first list should split it the
  same way; leaving it alone means those phrases stop being refused for
  learners with nothing at home to practise on, which is the intended change.
- `DocStore.get_status` takes `with_blocks` (default `True`, so existing
  implementations keep working). Declining returns `block_count=None` rather
  than `0`: zero decides whether a summary may be written onto a not-started
  page, so an uncounted page must not report it.
- `media.youtube.max_parallel_uploads` is removed; nothing read it.
- `baton doctor` now fails a profile whose `llm.provider` names anything but
  `none`. Baton has no model client, and a profile expecting one is waiting
  for a call that never comes.

### Lessons

- `baton lesson unpublish` takes a published summary back off its page: the
  blocks the publish recorded, then the session back to in progress, the
  draft rewound to `summarised`, and the record removed so the lesson can be
  published again. A recorded block whose text was edited by hand stops the
  command (exit `3`) rather than being deleted anyway; a block no record
  names is kept. Records written before block ids existed are attributed by
  re-rendering the stored summary, and anything unaccounted for is reported
  as ambiguous instead of guessed at. `--whole-page --force` is the explicit
  recovery for a page that went to the wrong recipient. Nothing un-sends a
  message that already went out, and the report says so.
- `baton lesson stage-set --field titles|context|corrected_context` amends a
  staged lesson without re-staging and losing what `stage` gathered. A
  published draft refuses the amendment.
- A publish now records the ids of the blocks it appended, which is what lets
  `unpublish` name exactly what it owns.
- Per-learner voice, completed: a studio's own prompt level reaches the model
  through `summary.prompt_levels` (mapped by `db.fields.learner.prompt_level`),
  and a level the profile does not describe adds nothing rather than being
  guessed at: the same stance `tone` and `instrument` already took.

### Sending

- `baton send readiness --date DATE` lists who is booked that day and what
  would still block each message, recomputed through the same `evaluate` the
  send refuses through, so the report and the refusal cannot drift. It keeps
  the layers apart: not yet published, no summary, and no video block are
  three different fixes in a fixed order.
- `baton send aftermath --date DATE` reports what the day left behind:
  drafts that never reached publish, draft files whose learner no longer
  exists, and published lessons with no send receipt. The receipt check
  shares one key with `send lesson`, so a receipt written by one is found by
  the other, and a miss is reported as the absence of evidence inside the
  duplicate window rather than as proof nothing was sent.
- Both reports exit `0` whatever they find, and both name whether the day's
  roster came from the calendar or from the dates on the documents.

### Correctness

- `set_current_piece` no longer succeeds silently when it matched no row:
  SQLite checks the row count and PostgREST asks for `return=representation`,
  both raising `StateError`. `learner assign` printed "is now working on"
  straight after a write that had done nothing.
- The fake document store now tolerates deleting a block that is already
  gone, as the real one always did: a resume path that production handles
  was failing only in tests.
- A read served by `db.fallback` says so on stderr. The flag recording it had
  been set on every failover and read by nothing.
- `get_status` no longer lists a whole page to count blocks for the six
  callers that only wanted a status word or a date.

## 0.6.0 (2026-08-28)

Four items from the studio's 2026-08 production-issue list. Exit codes are
unchanged throughout; two long-standing refusals were narrowed on purpose,
and both narrowings are named below.

### Upgrading

- `lesson clear --yes` no longer deletes drafts whose publish left work owed
  (a target that is not `ok`): the draft is the only record that the work is
  still owed, and sweeping it away is how an unfinished publish is forgotten.
  It now reports what it kept; pass `--force` to delete those too. Drafts
  with nothing owed on them clear as before.
- `calendar book` and `calendar schedule` accept a unique partial name, where
  they used to refuse everything but an exact match. If a studio relied on
  the strict refusal, nothing regresses (an ambiguous partial still exits 3
  with candidates), but a match the operator did not intend is now possible;
  the report names it in `matched` every time the relaxation fires, and
  `cancel` and the `lesson` commands keep the strict gate.

### Calendar

- The relaxation above: one substring match resolves and is announced; zero
  or several still stop and ask. In `schedule`, a learner named twice under
  two spellings ("Ada" and "Ada Whitfield") blocks the second slot instead of
  refusing the whole day, and the block names the slot to remove; a line that
  matches nobody is likewise a blocked slot, not an abandoned day.

### Lessons

- `lesson list` now carries each draft's full publish state per target (
  status, last error, attempts, and time) falling back to the published
  record where a re-staged draft is blind. A heartbeat asking "is this
  stuck?" no longer has to open the draft by hand.
- Vocabulary, in three layers that all warn rather than rewrite: a pool under
  `summary.vocabulary` rides the `lesson contract` payload with an
  instruction to use those spellings; `lesson stage --corrected/--corrected-file`
  keeps corrected notes beside the raw ones (the contract serves the
  corrected text; the raw notes are never overwritten); and `lesson ingest`
  reports near-miss spellings in `warnings` and on stderr without refusing
  the summary: a summary rejected over a spelling is a summary that stops
  being produced.

## 0.5.0 (2026-08-28)

Four gaps closed from the 2026-08-28 port-gap audit against the studio's
legacy scripts. Everything here is additive: no existing command's output
shape or exit code changed, and every new behaviour is either a new
subcommand or config-gated off by default.

### Calendar

- Weekday names, time words (`โมง`/`นาฬิกา`/`ทุ่ม`/`ตี`/`เที่ยง`/`เที่ยงคืน`),
  and day-first dates join the grammar in `whenever.py`: vocabulary is
  configuration (`calendar.weekdays`, `calendar.time_words`), matching
  `date_shorthand`'s existing pattern. Day-first parsing is
  `calendar.accept_dmy`, off by default: day-first is a convention, not a
  universal, and `YYYY-MM-DD` is never ambiguous. Bare `N โมง` reads
  literally: `9 โมง` is 09:00, not the traditional Thai count, and a time
  past 23 hours is refused rather than wrapped.
- `calendar list --from/--to` shows a whole range, one entry per day, empty
  days included: a gap is information. The bare `calendar list <date>`
  form is unchanged.

### Learners

- `learner add` enrols a learner and, optionally, their session pages from
  Notion URLs (`--page-urls`/`--pages`): the write path `learner
  list`/`show` never had. Refuses an exact-name duplicate outright (exit
  5); a near-miss is only ever reported alongside a success, never
  blocking one. `learner.instruments`/`learner.tones` restrict
  `--instrument`/`--tone` when the profile sets them (both empty, and
  unrestricted, by default). A studio-specific column named on the command
  line (`--prompt-level`, `--master-link`) with no `db.fields` entry to
  write it to is a configuration error raised before anything is written,
  not a silently dropped field.
- `learner list`'s human line now names the current piece by title, not
  only its id.

### Songs

- New command group `baton song list|search|show|add|update|remove`: the
  write path the piece catalogue never had. `remove` is refused while any
  learner is still assigned (exit 5, naming who), the same guard the
  legacy song manager used. `update` takes plain strings per field: a flag
  left out leaves it alone, an explicit empty value clears it: the same
  convention `Piece` already uses everywhere for "no link". Unknown ids on
  `update`/`remove` are refused rather than treated as a silent success.
- New skill `studio-songs`; `student-lookup` gains the `learner add` row.

### Store layer

- `LearnerStore` gains `add_learner`, `add_session`, `add_piece`,
  `update_piece`, `delete_piece`, implemented for SQLite, PostgREST/
  Supabase, and the fallback store: writes still never fail over, the
  fallback store's one rule. `FieldMap.extra_columns()` resolves
  studio-specific columns the model has no field for, and refuses an
  unmapped one before any write happens.

## 0.4.2 (2026-08-28)

Everything here came out of running 0.4.1 through a real teaching day
(2026-08-27) and reading the summary it produced.

### Upgrading

**A summary now needs a `progress` section once the learner has a previous
session.** `lesson ingest` refuses without one: exit 4, pointing at
`/progress`. Each entry is the state `before` and the state `after`. Nothing
changes for a first lesson, which has nothing to compare against.

**Three wording rules now reject what they used to allow**, all in the same
layer that already refused emoji and links in a parent's message: a fact
restated in more than two sections, a rating where an observation belongs
("did very well"), a word about the child rather than the playing ("a weak
point"), and a practice goal nobody can do at home. Each comes back with a
pointer and a hint, so the fix is one round trip. The lists live under
`summary.body` and emptying one turns its rule off.

Both are steered by `lesson contract`, which hands the model the schema and the
rules fresh on every call: an agent that reads the contract adapts on its own.
An agent working from a cached prompt will hit exit 4 once and then adapt.

**`summary.sections` gains `progress`.** A studio that renamed its headings
should name that one too. Do **not** add `progress` to `prep.required`: every
page published before this release lacks the heading, and prep is fail-closed.

### Lesson summaries

- `progress` as a first-class section, rendered `before → after`.
- `summary.tones`, `summary.instruments`, and `summary.no_instrument_at_home`
  turn the `tone`, `instrument`, and `has_instrument` columns (all three
  present since the first migration, none of them read) into guidance for the
  learner being written about.
- A published record keeps the validated summary, not only the message sent to
  the family, so the next lesson is staged against what happened.
- `lesson publish` takes `--session N`, which refuses if the staged draft is
  for a different one. Every `lesson` subcommand takes `--learner NAME` as an
  alternative to the positional name.

### Recordings

- **The song a lesson works on is no longer sent as its recording.** A `video`
  block now wins over a bookmark or an embed wherever it sits, and callers pass
  the piece's `source_link` so the song is never mistaken for the lesson. A
  page holding only the song reads as having no recording, and the send gate
  refuses, which is the correct answer.
- `lesson publish` links a recording the pipeline uploaded but never put on the
  page, including on a re-run of an already-published lesson.

### Video pipeline

- New clips under a finished job start the next session by themselves;
  `video forget` is no longer part of the weekly loop.
- Source clips a completed job's record claims were trashed are re-trashed when
  the source still lists them.
- `doc_linked` is verified against the live page rather than its own record,
  and a document-store outage no longer turns a finished job into a failed one.
- `resume` sees what `run` sees, and says so when clips wait that only `run`
  may collect.
- `video status --json` reports "no error" as `null` rather than `""`.
- `job list` hides finished jobs older than `--stale-days` (default 3).

### Learners

- `learner assign --update-published` rewrites the piece section on sessions
  already published, matching on the snapshot's piece id rather than on the
  song's title. `--dry-run` shows the plan. Note that the replaced section is
  appended, so it moves to the end of the page.

## 0.4.1 (2026-08-27)

Carries the 308 fix: a resumable YouTube upload's HTTP 308 is a success
shape, not a redirect to follow. The release itself taught two things the
repo kept: `pyproject.toml`'s version is static, so it must be bumped before
the tag is minted or PyPI refuses the wheel as the previous version; and the
documented pytest command must collect the whole suite.

## 0.4.0 (2026-08-27)

- Two publishes inside one second now order by session number, not by
  whichever the filesystem's glob yielded first: `send lesson` with no
  `--session` asks for the latest record, and the arbitrary winner could
  send last week's message to this week's family.
- Harness-compatibility gaps HR1–HR8 closed.

## 0.3.5 (2026-08-27)

- `send video`, `learner add-work`, and send readiness/aftermath's ancestor
  (findings F13, F17, F20 from the migration audit).
- `send` recognises bookmarked recordings and links the lesson page (F16,
  F18); `lesson publish` retries the YouTube description it owes (F5); F6,
  F7, F10, and F15 closed.

## 0.3.1 (2026-08-27)

- The practice track belongs to the lesson, not to today (#29): the piece is
  snapshotted when the lesson is staged, so a summary published Monday and
  sent Friday carries Monday's track.
- Mixed-orientation encodes, calendar retries, and the AI footer.

## 0.3.0 (2026-08-27)

The migration-audit release: the audit of what the move from the studio's
previous scripts left behind lived in `docs/migration-audit.md` until 0.4.2:
its fourteen fixed findings are explained in the code they changed, and its
six open ones are issues #74-#79. This release landed the audit lanes for
prep, notes, LINE send, video, calendar, lesson, and retired skills.

## 0.2.7 (2026-08-23)

A GPU encoder option: `media.encode.codec: h264_nvenc` moves the final
encode to NVIDIA NVENC. Decode and the concat filter stay on CPU: clips
come from phones and rarely share a codec or resolution.

## 0.2.6 (2026-08-23)

`baton lesson publish` updates the YouTube description, refusing to touch a
video the configured account does not own (a real incident, 2026-08-09: a
page's YouTube link pointed at an unrelated channel's tutorial).

## 0.2.5 (2026-08-23)

Three fixes found chasing a real report of a learner's video missing from a
`baton video` run: `_slug()` collapsed any non-ASCII (Thai) name to
"unknown", so such learners' job records aliased onto one file and a second
run silently inherited the first's completed state; two Drive clips sharing
a filename clobbered each other on disk before combining; and the lesson
message was recomposed to match the studio's existing LINE format after a
side-by-side comparison showed parents would notice the difference.

## 0.2.4 (2026-08-23)

Google credentials no longer rewrite the scopes of an authorized-user
refresh token: replacing them can make Google reject a valid token with
`invalid_scope` before any API call is attempted.

## 0.2.3 (2026-08-22)

- Google-backed media services read their credentials from one place:
  `media.google.credentials_file`, or the shared `*_env` trio, so Drive and
  YouTube no longer have to be configured separately.
- An argparse parse failure is raised as a typed error so the CLI keeps its
  exit-code and JSON envelope contract on a bad invocation, instead of
  argparse's own exit path; vendor exceptions from Google calls get the same
  treatment.

## 0.2.2 (2026-08-22)

- A refusal still reports `ok: false` with its JSON envelope; publishing
  finishes the session it wrote; a fence tag Notion does not know no longer
  costs the page.
- Docs: `learner latest` returns the page as sections, so "what did we do
  last time" needs no second command.

## 0.2.1 (2026-08-22)

`baton prep` (the day's lesson briefing, behind a hard gate) and `baton
course` (archive a finished course before emptying it: clear refuses an
unarchived course). Docs explain Baton's lineage and working cycle; the
call-graph extractor the published diagram is drawn from ships.

## 0.1.0 (2026-08-18)

First public release: learners, lesson summaries, messaging, video, and
calendar behind one fail-closed CLI. The release run caught that a
mis-scoped `include` in `pyproject.toml` produced a wheel of data files with
no Python in it, nothing was published, and the wheel check that caught it
now runs on every release.

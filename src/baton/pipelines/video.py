"""Collecting recordings, combining them, publishing, and linking back.

The pipeline is per-learner and every step is recorded before the next begins,
so a crash anywhere is re-runnable. Three properties are load-bearing, and each
one exists because its absence caused a real loss:

**Nothing is deleted until everything else succeeded.** Source clips are moved
to the trash as the last step, after the upload and the document link. Deleting
earlier means a crash between the delete and the upload loses the recording
permanently: the studio has no copy, and neither does anyone else.

**A completed upload is never repeated.** ``uploaded`` is recorded with the
video id the moment YouTube returns it. A resume that re-uploaded would leave
two copies of a child's lesson online with no way to tell which link was sent.

**One learner's failure does not stop the others.** Each is wrapped, and the
run reports per learner. A phone that produced a corrupt clip must not mean
nobody's recording goes out that night.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..adapters.db.base import LearnerStore
from ..adapters.docs.base import DocStore, find_video_link
from ..adapters.media.base import (
    UNFILED,
    VIDEO_SUFFIXES,
    EncodeProfile,
    MediaSource,
    SourceClip,
    TrashOutcome,
    UploadResult,
    VideoEncoder,
    VideoPublisher,
)
from ..adapters.media.google import extract_video_id
from ..core import jsonio
from ..core.cleanup import CleanupLedger
from ..core.paths import safe_key as _slug
from ..domain.models import Learner
from ..domain.resolve import normalise
from ..errors import BatonError, StateError

#: The steps, in order. A run resumes at the first one not recorded.
STEPS = (
    "downloaded",
    "combined",
    "session_resolved",
    "uploaded",
    "doc_linked",
    "cleaned",
    "source_trashed",
)

_NATURAL_SPLIT = re.compile(r"(\d+)")


def _natural_key(path: Path) -> list[Any]:
    """Sort `clip2` before `clip10`.

    Phone cameras number clips, and a plain text sort puts the tenth after
    the ninth only while there are nine: past that the concat order is
    wrong and nothing in the output says so.
    """
    return [int(part) if part.isdigit() else part for part in _NATURAL_SPLIT.split(path.name)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class VideoJob:
    """Per-learner progress through the pipeline, persisted atomically."""

    learner_folder: str
    learner_id: str = ""
    learner_name: str = ""
    session_number: int = 0
    doc_id: str = ""
    clip_ids: list[str] = field(default_factory=list)
    video_id: str = ""
    video_url: str = ""
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    status: str = "in_progress"
    error: str = ""
    updated_at: str = field(default_factory=_now)

    # `error` stays an empty string in memory so every `job.error or ...` reads
    # naturally, but the JSON schema says "no error" with null: a stable
    # schema is the contract `video status --json` is parsed against, and ""
    # forced consumers to branch on a third state that means the same as null.

    def done(self, step: str) -> bool:
        return bool(self.steps.get(step, {}).get("done"))

    def record(self, step: str, **detail: Any) -> None:
        self.steps[step] = {"done": True, "at": _now(), **detail}
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "learner_folder": self.learner_folder,
            "learner_id": self.learner_id,
            "learner_name": self.learner_name,
            "session_number": self.session_number,
            "doc_id": self.doc_id,
            "clip_ids": self.clip_ids,
            "video_id": self.video_id,
            "video_url": self.video_url,
            "steps": self.steps,
            "status": self.status,
            "error": self.error or None,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VideoJob:
        return cls(
            learner_folder=str(data.get("learner_folder", "")),
            learner_id=str(data.get("learner_id", "")),
            learner_name=str(data.get("learner_name", "")),
            session_number=int(data.get("session_number", 0) or 0),
            doc_id=str(data.get("doc_id", "")),
            clip_ids=[str(item) for item in data.get("clip_ids", [])],
            video_id=str(data.get("video_id", "")),
            video_url=str(data.get("video_url", "")),
            steps=dict(data.get("steps", {})),
            status=str(data.get("status", "in_progress")),
            error=str(data.get("error") or ""),
            updated_at=str(data.get("updated_at", _now())),
        )


class VideoJobStore:
    """Job files, one per learner folder."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, learner_folder: str) -> Path:
        return self.root / f"{_slug(learner_folder)}.json"

    def get(self, learner_folder: str) -> VideoJob | None:
        data = jsonio.read_json(self._path(learner_folder), None)
        return VideoJob.from_dict(data) if isinstance(data, dict) else None

    def save(self, job: VideoJob) -> None:
        jsonio.write_json(self._path(job.learner_folder), job.to_dict())

    def list(self, *, include_archived: bool = False) -> list[VideoJob]:
        """Every recorded job, live ones first.

        Args:
            include_archived: Also read jobs :meth:`archive` moved aside. Off
                by default because `run`, `resume`, and `status` all mean "what
                is happening now", and a folder accumulates one archived job
                per week. A caller asking about one past session: `lesson
                publish` repairing a page whose recording was uploaded but
                never linked: needs them, and without this could only see a
                past week's upload until the next week's clips arrived.
        """
        if not self.root.is_dir():
            return []
        paths = sorted(self.root.glob("*.json"))
        if include_archived:
            paths += sorted((self.root / "archive").glob("*.json"))
        jobs = []
        for path in paths:
            data = jsonio.read_json(path, None)
            if not isinstance(data, dict):
                continue
            # A job file always names its learner folder. The cleanup ledger
            # lives in this same directory (`cleanup.json`, and its `.bak`),
            # carries no such field, and until 0.5.2 was parsed as a job:
            # every `video status` reported a phantom `in_progress` entry
            # with no name (production, 2026-09-13), and `resume` would have
            # run it as a real folder. Whatever else lands here without a
            # learner folder is not a job either.
            if not str(data.get("learner_folder", "")).strip():
                continue
            jobs.append(VideoJob.from_dict(data))
        return jobs

    def remove(self, learner_folder: str) -> bool:
        path = self._path(learner_folder)
        existed = path.exists()
        for candidate in (path, jsonio.backup_path(path)):
            candidate.unlink(missing_ok=True)
        return existed

    def archive(self, job: VideoJob) -> Path:
        """Move a completed job aside so the next session can start fresh.

        The store keys jobs by folder, one live job per learner, but a folder
        receives new clips every week. Until 0.4.1 the completed job sat in the
        path forever and `run`, finding every step done, swallowed the new
        week's clips in silence; the only way through was `video forget` by
        hand. Archiving keeps the record (a publish may still read its upload)
        while giving the new job a clean path: :meth:`list` reaches it again
        with ``include_archived``.

        Returns:
            Where the job file landed, under ``<root>/archive/``.
        """
        target_dir = self.root / "archive"
        target_dir.mkdir(parents=True, exist_ok=True)
        base = f"{_slug(job.learner_folder)}__s{job.session_number or 0}"
        target = target_dir / f"{base}.json"
        suffix = 0
        while target.exists():
            suffix += 1
            target = target_dir / f"{base}-{suffix}.json"
        source = self._path(job.learner_folder)
        if source.exists():
            source.replace(target)
        backup = jsonio.backup_path(source)
        if backup.exists():
            backup.unlink(missing_ok=True)
        return target


@dataclass
class VideoPipeline:
    """Runs the whole thing for every learner with pending clips."""

    source: MediaSource
    encoder: VideoEncoder
    publisher: VideoPublisher
    store: LearnerStore
    docs: DocStore
    jobs: VideoJobStore
    workdir: Path
    profile: EncodeProfile = field(default_factory=EncodeProfile)
    privacy: str = "unlisted"
    session_label: str = "week"
    resolve_session: Any = None
    """Callable ``(learner) -> (number, doc_id) | None``, injected by the CLI so
    the pipeline does not need to know how sessions are chosen."""
    cleanup: CleanupLedger | None = None
    """Where unfiled clip ids are remembered. ``None`` skips the bookkeeping,
    which loses the deletions this exists to keep track of; the CLI always
    passes one."""

    # -- helpers -----------------------------------------------------------

    def _match_learner(self, folder: str) -> Learner | None:
        """Match a Drive folder name to a learner, exactly or not at all.

        The same stance as everywhere else: a folder called "Ada" does not
        resolve to "Ada Whitfield". Uploading one child's lesson to another
        child's page is not a mistake worth risking for the convenience.
        """
        wanted = normalise(folder)
        for learner in self.store.list_learners():
            if normalise(learner.name) == wanted:
                return learner
        return None

    def _clip_dir(self, folder: str) -> Path:
        return self.workdir / _slug(folder)

    def _already_linked(self, doc_id: str, url: str) -> bool:
        """Whether this exact video is already on the document.

        Guards the case where the link succeeded but the step record did not
        survive: re-linking would put the same video on the page twice.
        """
        for block in self.docs.list_blocks(doc_id):
            if block.type == "video" and block.url == url:
                return True
        return False

    # -- steps -------------------------------------------------------------

    @staticmethod
    def _already_downloaded(destination: Path, clip: SourceClip) -> bool:
        """Whether a previous download of this clip can be trusted.

        "The file exists" is not enough: a run killed mid-download leaves a
        partial file behind, and a partial clip is exactly what gets
        concatenated, uploaded, linked, and then has its only other copy
        trashed.
        """
        if not destination.exists():
            return False
        # A known size that disagrees means a partial file: download again.
        return not (clip.size_bytes and destination.stat().st_size != clip.size_bytes)

    def _download(self, job: VideoJob, clips: list[SourceClip]) -> list[Path]:
        target = self._clip_dir(job.learner_folder)
        paths = []
        claimed: dict[str, str] = {}  # destination name -> the clip id that owns it
        for clip in clips:
            name = _slug(clip.name)
            owner = claimed.get(name)
            if owner is not None and owner != clip.id:
                # A phone's own numbering restarts across recording sessions,
                # so two distinct clips sharing one filename is normal, not a
                # bug in the source. Downloading both to the same destination
                # would let the second silently overwrite the first before
                # either was combined: losing one clip's footage right before
                # the run trashes both originals as "already collected".
                stem, _, suffix = name.rpartition(".")
                name = f"{stem or name}__{clip.id[:8]}" + (f".{suffix}" if suffix else "")
            claimed[name] = clip.id
            destination = target / name
            if not self._already_downloaded(destination, clip):
                self.source.download(clip, destination)
            paths.append(destination)
        job.clip_ids = [clip.id for clip in clips]
        job.record("downloaded", count=len(paths))
        return paths

    def _downloaded_clips(self, folder: str) -> list[Path]:
        """Files on disk this run may concatenate: videos only, no strays.

        An interrupted encode leaves dot-prefixed temp files
        (``.baton-encode-*``) in the same directory, and a bare glob used to
        feed them into the concat filter as though they were clips.
        """
        target = self._clip_dir(folder)
        if not target.is_dir():
            return []
        return [
            path
            for path in sorted(target.glob("*"), key=_natural_key)
            if path.name != "combined.mp4"
            and not path.name.startswith(".")
            and path.suffix.lower() in VIDEO_SUFFIXES
        ]

    def _combine(self, job: VideoJob, paths: list[Path]) -> Path:
        output = self._clip_dir(job.learner_folder) / "combined.mp4"
        if not output.exists():
            result = self.encoder.combine(sorted(paths, key=_natural_key), output, self.profile)
            # Which of the two paths produced the file is worth a year's
            # hindsight: a session that quietly stopped copying is a session
            # back on the slow path, and the job record is where that shows.
            job.record("combined", output=str(result.path), method=result.method)
        else:
            job.record("combined", output=str(output))
        return output

    def _resolve(self, job: VideoJob, learner: Learner) -> None:
        resolved = self.resolve_session(learner) if self.resolve_session else None
        if not resolved:
            raise StateError(
                f"No session to attach {learner.name}'s recording to.",
                remedy=f"{learner.name} has no {self.session_label} in progress and "
                f"none finished, so nothing says which lesson this recording is "
                f"from. Book the {self.session_label} it belongs to, then re-run.",
            )
        number, doc_id = resolved
        job.session_number = int(number)
        job.doc_id = str(doc_id)
        job.learner_id = learner.id
        job.learner_name = learner.name
        job.record("session_resolved", session_number=job.session_number, doc_id=job.doc_id)

    def _upload(self, job: VideoJob, path: Path) -> UploadResult:
        if job.video_id:
            # Recorded already: never upload a second copy.
            return UploadResult(video_id=job.video_id, url=job.video_url)
        adopted = self._adopt_existing(job)
        if adopted is not None:
            return adopted
        title = f"{job.learner_name} - {self.session_label} {job.session_number}"
        result = self.publisher.upload(path, title=title, privacy=self.privacy)
        job.video_id = result.video_id
        job.video_url = result.url
        # Recorded immediately, before anything else can fail.
        job.record("uploaded", video_id=result.video_id, url=result.url)
        return result

    def _adopt_existing(self, job: VideoJob) -> UploadResult | None:
        """Reuse the recording already on the session page, if it is ours.

        The job record is the first line of defence against a second upload;
        this is the second, for the cases where the record was lost anyway:
        `video forget` on a job that had already uploaded, or a job file
        removed by hand. The page keeps what the record forgot, and asking it
        one question ("is there already a video block here that we own?")
        turns a would-be duplicate into the same link as before.

        A video block is required, not just any link on the page: only the
        pipeline writes those, so a song bookmark can never be adopted. And
        ownership is asked of the publisher, so a reference video somebody
        pasted as a block is left alone and the normal upload still runs.
        """
        if not job.doc_id:
            return None
        # find_video_link degrades to "" when the store is unreachable, and
        # an unreachable store is not evidence of anything: fall through to
        # the upload, which answers for itself.
        existing = find_video_link(self.docs, job.doc_id, blocks=("video",), exclude=())
        video_id = extract_video_id(existing) if existing else None
        if not video_id:
            return None
        with contextlib.suppress(BatonError):
            if not self.publisher.owns(video_id):
                return None
        job.video_id = video_id
        job.video_url = existing
        job.record("uploaded", video_id=video_id, url=existing, adopted=True)
        return UploadResult(video_id=video_id, url=existing)

    def _link(self, job: VideoJob) -> None:
        """Put the recording on the session page, above the summary.

        Where it lands used to depend on the order the day happened in. The
        publish step appends the summary and leaves preserved blocks where they
        are, so a video that arrived first sat above the summary and a video
        that arrived second sat below it: the same lesson filed two ways, and
        the family reading the page could not tell why.

        Encoding takes minutes and the teacher writes the summary meanwhile, so
        second is the ordinary case, not the exception. The recording goes to
        the top explicitly instead: no move operation is needed (Notion has
        none), and a later republish appends below it as usual.
        """
        if not self._already_linked(job.doc_id, job.video_url):
            self.docs.append_blocks(
                job.doc_id,
                [
                    {
                        "object": "block",
                        "type": "video",
                        "video": {"type": "external", "external": {"url": job.video_url}},
                    }
                ],
                position="start",
            )
        job.record("doc_linked", url=job.video_url)

    def _clean(self, job: VideoJob) -> None:
        target = self._clip_dir(job.learner_folder)
        if target.is_dir():
            for item in sorted(target.rglob("*"), reverse=True):
                if item.is_file():
                    item.unlink(missing_ok=True)
            target.rmdir()
        job.record("cleaned")

    def _trash(self, job: VideoJob) -> None:
        outcomes = self.source.trash(job.clip_ids) if job.clip_ids else []
        self._settle(outcomes, job)
        job.record(
            "source_trashed",
            moved=len(outcomes),
            unfiled=sum(1 for item in outcomes if item.outcome == UNFILED),
        )

    def _settle(self, outcomes: list[TrashOutcome], job: VideoJob) -> None:
        """Pay the cleanup ledger what these outcomes owe.

        Unfiled clips still exist in their uploader's Drive, out of the
        pipeline's sight; without an entry here their ids are lost and the
        files sit there forever. Trashed and gone clips settle any old entry
        theirs (a retried `video resume` clears what a previous run deferred).
        """
        if self.cleanup is None:
            return
        unfiled = [item.clip_id for item in outcomes if item.outcome == UNFILED]
        settled = [item.clip_id for item in outcomes if item.settled]
        if unfiled:
            self.cleanup.record_unfiled(unfiled, job=job, now=_now())
        if settled:
            self.cleanup.mark_cleared(settled, now=_now())

    # -- run ---------------------------------------------------------------

    def run_one(self, folder: str, clips: list[SourceClip]) -> VideoJob:
        """Run (or resume) the pipeline for one learner folder."""
        job = self.jobs.get(folder) or VideoJob(learner_folder=folder)
        job.status = "in_progress"
        job.error = ""

        learner = self._match_learner(folder)
        if learner is None:
            job.status = "skipped"
            job.error = f"No learner is named exactly “{folder}”."
            self.jobs.save(job)
            return job

        try:
            if job.done("downloaded"):
                paths = self._downloaded_clips(folder)
            elif not clips:
                # Resume of a job that crashed before downloading anything:
                # recording `downloaded` over zero clips would poison the step
                # record and every later run would skip the download forever.
                raise StateError(
                    f"No clips are pending for {folder} and none were downloaded.",
                    remedy="Re-run `baton video run` so the source is collected; "
                    "this job never reached the download step.",
                )
            else:
                paths = self._download(job, clips)
            self.jobs.save(job)

            combined = self._clip_dir(folder) / "combined.mp4"
            if not job.done("combined"):
                if not paths:
                    # The step says downloaded, the directory disagrees. The
                    # job file is the thing that is wrong, and only its owner
                    # can decide whether to re-collect or discard.
                    raise StateError(
                        f"The download step is recorded as done for {folder}, but "
                        "the working directory holds no clips.",
                        remedy="Delete this job's file under <state>/video/ and re-run "
                        "`baton video run`, which collects from the source again.",
                    )
                combined = self._combine(job, paths)
                self.jobs.save(job)

            if not job.done("session_resolved"):
                self._resolve(job, learner)
                self.jobs.save(job)

            if not job.done("uploaded"):
                self._upload(job, combined)
                self.jobs.save(job)

            # Verified against the live page, never against the step record
            # alone. On 2026-08-27 a job recorded `doc_linked` done at 13:56
            # against the right document with the right URL, and the block was
            # never on that page: the forced publish an hour later reported
            # `preserved: 0`, and `video` is in the packaged preserve rules, so
            # it was already absent before anything rewrote the page. What
            # broke the append is still unexplained, which is the reason to
            # ask the page rather than the record. `_link` appends only what
            # the page is missing, so asking twice is free.
            if job.done("doc_linked"):
                # Best effort once the step is recorded: an unreachable
                # document store is not evidence that the link is gone, and
                # failing here would turn a job that finished every step into
                # a `failed` one that had, in fact, done everything.
                with contextlib.suppress(BatonError):
                    self._link(job)
            else:
                self._link(job)
            self.jobs.save(job)

            if not job.done("cleaned"):
                self._clean(job)
                self.jobs.save(job)

            # Last, and only now: the source is the only remaining copy until
            # this point.
            leftover = [clip.id for clip in clips if clip.id in job.clip_ids]
            if not job.done("source_trashed"):
                self._trash(job)
            elif leftover:
                # The step says the source was trashed, yet the source still
                # lists clips this job already owns. The recorded success and
                # the source disagreed exactly this way in production
                # (2026-08-27): the two jobs of that evening recorded `moved`
                # counts of 4 and 3, and the clips stayed in Drive until a
                # person trashed them by hand.
                # Re-issuing the trash is idempotent on clips already gone.
                outcomes = self.source.trash(leftover)
                self._settle(outcomes, job)
                job.record(
                    "source_trashed",
                    moved=len(outcomes),
                    unfiled=sum(1 for item in outcomes if item.outcome == UNFILED),
                    reclaimed=len(leftover),
                )

            job.status = "done"
            self.jobs.save(job)
            return job

        except BatonError as exc:
            job.status = "failed"
            job.error = exc.message
            self.jobs.save(job)
            return job

    def run(self, *, only: list[str] | None = None) -> list[VideoJob]:
        """Run every learner folder that has clips waiting.

        A completed job no longer ends the story for its folder: clips it
        already owns are reclaimed (their trash did not take effect on the
        source), and clips it has never seen belong to a later lesson: the
        done job is archived and a new job starts for the next session, so
        `video forget` is no longer part of the weekly loop.

        Args:
            only: Restrict to these folder names.

        Returns:
            One job per folder attempted, whatever its outcome.
        """
        pending = self.source.list_pending()
        grouped: dict[str, list[SourceClip]] = {}
        for clip in pending:
            grouped.setdefault(clip.learner_folder, []).append(clip)

        if only:
            wanted = {normalise(name) for name in only}
            grouped = {k: v for k, v in grouped.items() if normalise(k) in wanted}

        jobs: list[VideoJob] = []
        for folder, clips in sorted(grouped.items()):
            previous = self.jobs.get(folder)
            if previous is not None and previous.status == "done":
                known = [clip for clip in clips if clip.id in previous.clip_ids]
                fresh = [clip for clip in clips if clip.id not in previous.clip_ids]
                if known:
                    # Recovery, not re-processing: every step stays done and
                    # only the trash the source ignored is re-issued.
                    jobs.append(self.run_one(folder, known))
                if fresh:
                    self.jobs.archive(previous)
                    jobs.append(self.run_one(folder, fresh))
                continue
            jobs.append(self.run_one(folder, clips))
        return jobs

    def resume(self) -> list[VideoJob]:
        """Continue every job that did not finish, without collecting new clips.

        Unfinished jobs continue from their first pending step. A *completed*
        job whose source clips are still listed also continues here: its
        trash step recorded a success the source did not honour, and catching
        that lie is recovery, not collection. That is the same selection
        ``run`` sees, minus the new lessons only ``run`` may start, so
        "resume found nothing" can no longer mean "run will find work that
        was merely waiting to be reclaimed".
        """
        recorded = self.jobs.list()
        unfinished = [job for job in recorded if job.status not in ("done", "skipped")]
        jobs = [self.run_one(job.learner_folder, []) for job in unfinished]

        pending: dict[str, list[SourceClip]] = {}
        for clip in self.source.list_pending():
            pending.setdefault(clip.learner_folder, []).append(clip)

        for job in recorded:
            if job.status != "done":
                continue
            known = [
                clip for clip in pending.get(job.learner_folder, []) if clip.id in job.clip_ids
            ]
            if known:
                jobs.append(self.run_one(job.learner_folder, known))
        return jobs

    def waiting_clips(self) -> int:
        """Clips pending under folders no unfinished job will collect.

        What `resume` must say out loud instead of "Nothing to process." when
        a later `run` would find work: new lessons waiting, or nothing at all.
        Clips a done job already owns are not counted: resume has just
        reclaimed those, and only genuinely new clips are `run`'s to start.
        """
        recorded = self.jobs.list()
        unfinished = {
            job.learner_folder for job in recorded if job.status not in ("done", "skipped")
        }
        owned: dict[str, set[str]] = {
            job.learner_folder: set(job.clip_ids) for job in recorded if job.status == "done"
        }
        return sum(
            1
            for clip in self.source.list_pending()
            if clip.learner_folder not in unfinished
            and clip.id not in owned.get(clip.learner_folder, set())
        )

"""Turning what a person types into a date and a time.

The original told the agent to "parse the shorthand into YYYY-MM-DD before
calling the script". That is arithmetic, and arithmetic is the last thing to
leave to a language model: an off-by-one here books a lesson on the wrong day
and nobody notices until a family arrives to an empty room.

So it is a function, and a command. Shorthand tokens are configuration, because
"พน" means tomorrow to one studio and nothing at all to another. The same goes
for weekday names and the words a studio uses to say when in the day a lesson
happens: the vocabulary is the profile's, the arithmetic is always code's.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from ..errors import UsageError

#: Tokens every profile understands, regardless of language.
BUILTIN_OFFSETS = {
    "today": 0,
    "tomorrow": 1,
    "yesterday": -1,
}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFFSET = re.compile(r"^([+-])(\d{1,3})$")
_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_HOUR_ONLY = re.compile(r"^(\d{1,2})$")
#: ``12/8/2026`` or ``12/8``, day first, one separator throughout. The year
#: must be four digits; a two-digit year is more often a typo than 1926.
_DMY_FULL = re.compile(r"^(\d{1,2})([/-])(\d{1,2})\2(\d{4})$")
_DMY_YEARLESS = re.compile(r"^(\d{1,2})([/-])(\d{1,2})$")


def today_in(timezone: str) -> date:
    """The current date in the studio's timezone.

    Explicitly not the server's date: a studio in Bangkok running on a UTC
    host would otherwise book "today" onto yesterday for seven hours a day.
    """
    return datetime.now(ZoneInfo(timezone)).date()


def now_in(timezone: str) -> datetime:
    """The current moment in the studio's timezone.

    The date-and-time counterpart to :func:`today_in`, for the same reason: a
    summary published at 01:00 in Bangkok is stamped 18:00 the previous day if
    the server's clock is the one consulted.
    """
    return datetime.now(ZoneInfo(timezone))


def parse_date(
    value: str,
    *,
    timezone: str = "UTC",
    shorthand: Mapping[str, object] | None = None,
    weekdays: Mapping[str, object] | None = None,
    accept_dmy: bool = False,
    reference: date | None = None,
) -> date:
    """Resolve a date expression.

    Accepts an ISO date, a builtin token, a configured shorthand token, a
    configured weekday name, a day-first numeric date (when the profile allows
    it), or a signed day offset like ``+3``.

    A weekday always means its *next* occurrence: today never counts, which
    is the rule the studio's original scripts lived by: asked on a Friday,
    "วันศุกร์" means next Friday, not the Friday already half over.

    Args:
        value: What the person typed.
        timezone: Studio timezone, used to decide what "today" means.
        shorthand: Token to day-offset map from ``calendar.date_shorthand``.
        weekdays: Token to weekday map from ``calendar.weekdays``, where
            0 is Monday.
        accept_dmy: Accept ``12/8/2026`` and yearless ``12/8`` as day-first.
            Off by default: day-first is a convention, not a universal, and a
            public tool that guesses loses.
        reference: Treat this as today. For tests.

    Returns:
        The resolved date.

    Raises:
        UsageError: The expression is not one Baton understands. The message
            lists what it does understand rather than guessing.
    """
    raw = value.strip()
    if not raw:
        raise UsageError("No date was given.", remedy="Pass a date, or `today`.")

    if _ISO_DATE.match(raw):
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise UsageError(
                f"`{raw}` looks like a date but is not a real one.",
                remedy="Use YYYY-MM-DD.",
            ) from exc

    base = reference or today_in(timezone)
    folded = raw.casefold()

    if folded in BUILTIN_OFFSETS:
        return base + timedelta(days=BUILTIN_OFFSETS[folded])

    for token, offset in (shorthand or {}).items():
        if str(token).strip().casefold() == folded:
            try:
                return base + timedelta(days=int(str(offset)))
            except (TypeError, ValueError) as exc:
                raise UsageError(
                    f"The shorthand `{token}` is configured with a non-numeric offset.",
                    remedy="calendar.date_shorthand maps a token to a number of days.",
                ) from exc

    # Longest token first, so a word that contains another configured word
    # (เที่ยงคืน and เที่ยง, say) resolves to the more specific one.
    for token, target in sorted(
        ((str(k), v) for k, v in (weekdays or {}).items()),
        key=lambda pair: len(pair[0]),
        reverse=True,
    ):
        if token.casefold() in folded:
            try:
                weekday = int(str(target))
            except (TypeError, ValueError) as exc:
                raise UsageError(
                    f"The weekday `{token}` is configured with a non-numeric day.",
                    remedy="calendar.weekdays maps a name to a weekday, 0 = Monday.",
                ) from exc
            if not 0 <= weekday <= 6:
                raise UsageError(
                    f"The weekday `{token}` is configured with day {weekday}, "
                    "which is no day of the week.",
                    remedy="calendar.weekdays maps a name to 0-6, Monday = 0.",
                )
            return base + timedelta(days=(weekday - base.weekday()) % 7 or 7)

    if accept_dmy and (match := _DMY_FULL.match(raw) or _DMY_YEARLESS.match(raw)):
        day, month = int(match.group(1)), int(match.group(3))
        year = int(match.group(4)) if match.lastindex and match.lastindex > 3 else base.year
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise UsageError(
                f"`{raw}` looks like a date but is not a real one.",
                remedy="Use YYYY-MM-DD, or a real day and month.",
            ) from exc

    match = _OFFSET.match(raw)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        return base + timedelta(days=sign * int(match.group(2)))

    known = sorted(
        {
            *BUILTIN_OFFSETS,
            *(str(k) for k in (shorthand or {})),
            *(str(k) for k in (weekdays or {})),
        }
    )
    raise UsageError(
        f"`{raw}` is not a date Baton understands.",
        remedy=f"Use YYYY-MM-DD, a signed offset like +2, or one of: {', '.join(known)}.",
        details={"understood": known},
    )


def _word_time(raw: str, words: Mapping[str, object]) -> tuple[int, int] | None:
    """Read a time written with the profile's own words, or ``None``.

    The vocabulary comes from ``calendar.time_words``:

    * ``hour_units``: ``N โมง`` / ``N นาฬิกา``: the number is the hour,
      read literally. ``9 โมง`` is 09:00, not the traditional Thai count
      where it would be mid-morning; a studio that wants that convention
      says so with a period word.
    * ``morning`` / ``evening``: period words. Morning keeps the hour;
      an evening word adds twelve when the hour is below it. Both apply to
      every hour unit alike: the original scripts silently ignored เย็น on
      นาฬิกา, and a lesson booked for 06:00 because of it stayed wrong all
      week. Naming both a morning and an evening word is refused.
    * ``special``: words that *are* a time: เที่ยง → 12:00, เที่ยงคืน → 00:00.
    * ``evening_count``: counted-from-evening words, mapped to the hour the
      count starts at: with ``ทุ่ม: 18``, ``3 ทุ่ม`` is 21:00; with ``ตี: 0``,
      ``ตี 3`` is 03:00. The word may sit before or after the number.

    Minutes ride along (``6:30 โมงเย็น`` → 18:30): dropping them because the
    original scripts never parsed a minute is how a 6:30 lesson becomes 6:00.

    Raises:
        UsageError: The words present contradict each other.
    """
    folded = raw.casefold()

    def _numbers(section: str) -> dict[str, int]:
        entries = cast("dict[str, Any]", words.get(section) or {})
        return {str(k).casefold(): int(v) for k, v in entries.items()}

    def _list(section: str) -> list[str]:
        return [str(item) for item in cast("list[Any]", words.get(section) or [])]

    for token, hour in sorted(_numbers("special").items(), key=lambda kv: -len(kv[0])):
        if token in folded:
            return hour, 0

    units = [u.casefold() for u in _list("hour_units")]
    morning = [w.casefold() for w in _list("morning")]
    evening = [w.casefold() for w in _list("evening")]

    for unit in units:
        match = re.search(rf"(\d{{1,2}})(?:[:.](\d{{2}}))?\s*{re.escape(unit)}", folded)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            says_morning = any(word in folded for word in morning)
            says_evening = any(word in folded for word in evening)
            if says_morning and says_evening:
                raise UsageError(
                    f"`{raw}` names both a morning and an evening word.",
                    remedy="Keep one or the other; the words come from calendar.time_words.",
                )
            if says_evening and hour < 12:
                hour += 12
            return hour, minute

    for token, base in _numbers("evening_count").items():
        after = re.search(rf"(\d{{1,2}})(?:[:.](\d{{2}}))?\s*{re.escape(token)}", folded)
        before = re.search(rf"{re.escape(token)}\s*(\d{{1,2}})(?:[:.](\d{{2}}))?", folded)
        match = after or before
        if match:
            hour = int(match.group(1)) + base
            return hour, int(match.group(2) or 0)

    return None


def parse_time(value: str, *, words: Mapping[str, object] | None = None) -> time:
    """Resolve a time of day written as ``17:00``, ``17.00``, or ``17``.

    With ``words`` from ``calendar.time_words`` the profile's own phrasing is
    understood too: ``6 โมงเย็น``, ``3 ทุ่ม``, ``ตี 3``, ``เที่ยง``. A time
    past 23 hours is refused rather than wrapped: "11 ทุ่ม" is a typo, not a
    plan for 05:00.

    Raises:
        UsageError: Not a time, or not a real one.
    """
    raw = value.strip()
    match = _TIME.match(raw) or _HOUR_ONLY.match(raw)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.lastindex and match.lastindex > 1 else 0
    elif words and (worded := _word_time(raw, words)) is not None:
        hour, minute = worded
    else:
        raise UsageError(
            f"`{raw}` is not a time.",
            remedy="Write it as 17:00 or 17.00.",
        )
    if hour > 23 or minute > 59:
        raise UsageError(
            f"`{raw}` is not a real time.",
            remedy="Hours are 0-23 and minutes 0-59.",
        )
    return time(hour=hour, minute=minute)


def combine(day: date, moment: time, timezone: str) -> datetime:
    """A timezone-aware datetime in the studio's own timezone."""
    return datetime.combine(day, moment, tzinfo=ZoneInfo(timezone))


def day_window(first: date, last: date, timezone: str) -> tuple[str, str]:
    """ISO bounds from midnight starting ``first`` to midnight ending ``last``."""
    start = combine(first, time(), timezone)
    end = combine(last + timedelta(days=1), time(), timezone)
    return start.isoformat(), end.isoformat()


def _time_head_width(tokens: list[str], words: Mapping[str, object] | None) -> int:
    """How many leading tokens of a schedule line form the time.

    A written time can itself contain spaces: ``6 โมงเช้า น้องจี`` is a time
    then a name, not a time named "โมงเช้า น้องจี". Beyond the first token,
    a token joins the time only while it *begins with* one of the profile's
    time words, so น้องจี never gets swallowed however the number is spaced.
    """
    if not words:
        return 1
    vocabulary = [str(word) for word in cast("list[Any]", words.get("hour_units") or [])]
    vocabulary += [str(word) for word in cast("list[Any]", words.get("morning") or [])]
    vocabulary += [str(word) for word in cast("list[Any]", words.get("evening") or [])]
    vocabulary += [str(word) for word in cast("dict[str, Any]", words.get("special") or {})]
    vocabulary += [str(word) for word in cast("dict[str, Any]", words.get("evening_count") or {})]
    width = 1
    for token in tokens[1:]:
        if any(token.startswith(word) for word in vocabulary if word):
            width += 1
        else:
            break
    return width


def parse_schedule(
    text: str,
    *,
    default_minutes: int = 60,
    # An em dash here is input a teacher types into an empty slot, not prose:
    # the writing style's ban on the character does not reach typed data.
    free_markers: tuple[str, ...] = ("-", "—", "free", "ว่าง"),
    words: Mapping[str, object] | None = None,
) -> list[tuple[time, time, str]]:
    """Parse a day's teaching list into ``(start, end, name)`` rows.

    The format a teacher actually writes: one line per slot, time first, then
    who. A slot's end is the next slot's start, which is how a back-to-back
    day really works; the last slot gets ``default_minutes``.

    ``words`` reaches each line's :func:`parse_time`, so a day list may be
    written ``6 โมงเย็น น้องจี`` like any other time.

    Raises:
        UsageError: A line is not ``<time> <name>``, naming the line.
    """
    rows: list[tuple[time, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        tokens = stripped.split()
        width = _time_head_width(tokens, words)
        try:
            start = parse_time(" ".join(tokens[:width]), words=words)
        except UsageError as exc:
            raise UsageError(
                f"Line {number} does not start with a time: {stripped!r}",
                remedy="Each line is `17:00 Name`, or `17:00 -` for a free slot.",
            ) from exc
        name = " ".join(tokens[width:]).strip()
        rows.append((start, name))

    rows.sort(key=lambda row: row[0])

    booked: list[tuple[time, time, str]] = []
    for index, (start, name) in enumerate(rows):
        if not name or name.casefold() in {marker.casefold() for marker in free_markers}:
            continue
        if index + 1 < len(rows):
            end = rows[index + 1][0]
        else:
            # datetime arithmetic on an arbitrary anchor date: the date itself
            # is irrelevant, only the midnight crossing of `time` matters, and
            # tying it to "today" implied a timezone that never existed here.
            end = (datetime.combine(date.min, start) + timedelta(minutes=default_minutes)).time()
        booked.append((start, end, name))
    return booked

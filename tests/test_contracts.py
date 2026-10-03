"""The boundary where model output is accepted or refused.

Every test here is a way a model gets it wrong. The schema catches shape; the
code catches the rules a schema cannot express, and those are the ones a small
local model breaks, because they are the ones usually written as prose.
"""

from __future__ import annotations

import copy

import pytest

from baton.contracts import (
    has_emoji,
    validate_lesson_summary,
    validate_short_summary,
    vocabulary_near_misses,
)
from baton.errors import ContractError
from baton.exits import Exit

VALID = {
    "overview": ["Steady progress on the B section; the tempo held this week."],
    "covered": [
        {"topic": "Blackbird, bars 9-16", "detail": "Thumb-and-finger pattern"},
        {"topic": "Chord changes", "notation": "Em7: 022033"},
    ],
    "focus": [
        {"issue": "The change to C is late", "fix": "Practise the change alone, four beats each"}
    ],
    "goals": ["Play bars 9-16 with the backing track at 80bpm"],
    "short_summary": {
        "covered": "Blackbird, bars 9 to 16",
        "progress": "Held the tempo without stopping",
        "homework": "Bars 9-16 with the backing track",
    },
}


def valid(**overrides):
    payload = copy.deepcopy(VALID)
    payload.update(overrides)
    return payload


def test_a_well_formed_summary_is_accepted():
    assert validate_lesson_summary(valid()) == valid()


# -- shape ------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["overview", "covered", "goals", "short_summary"])
def test_a_missing_required_section_is_rejected(missing):
    payload = valid()
    del payload[missing]

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload)

    assert any(missing in v["reason"] for v in excinfo.value.violations)


def test_an_unknown_top_level_key_is_rejected():
    """A model that invents a section would otherwise have it silently dropped,
    and the teacher would never learn the content went nowhere."""
    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(valid(notes="something I made up"))

    assert excinfo.value.violations


def test_a_difficulty_without_a_fix_is_rejected():
    """An issue with no fix is an observation, not teaching."""
    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(valid(focus=[{"issue": "Rushing the chorus"}]))

    assert any("fix" in v["reason"] for v in excinfo.value.violations)


def test_an_empty_goals_list_is_rejected():
    with pytest.raises(ContractError):
        validate_lesson_summary(valid(goals=[]))


def test_a_non_object_payload_is_rejected_clearly():
    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(["not", "an", "object"])

    assert "object" in str(excinfo.value)


def test_violations_carry_a_pointer_to_the_offending_field():
    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(valid(goals=[""]))

    assert any(v["path"].startswith("/goals") for v in excinfo.value.violations)


def test_every_violation_is_reported_at_once():
    """One re-run per problem is how a model gets stuck in a loop."""
    payload = valid(goals=[], overview=[])
    del payload["covered"]

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload)

    assert len(excinfo.value.violations) >= 3


def test_a_rejection_maps_to_the_contract_exit_code():
    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary({})

    assert excinfo.value.exit_code == Exit.CONTRACT
    assert excinfo.value.to_dict()["error"] == "contract"


# -- the rules a schema cannot express ---------------------------------------


def test_emoji_in_the_parent_message_is_rejected():
    """The rule that used to be a sentence in a prompt."""
    payload = valid()
    payload["short_summary"]["progress"] = "Great work today 🎉"

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload)

    assert any("emoji" in v["reason"] for v in excinfo.value.violations)


def test_a_link_in_the_parent_message_is_rejected():
    payload = valid()
    payload["short_summary"]["covered"] = "See https://example.invalid/notes"

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload)

    assert any("link" in v["reason"] for v in excinfo.value.violations)


def test_a_markdown_link_is_caught_too():
    payload = valid()
    payload["short_summary"]["covered"] = "See [the notes](https://example.invalid)"

    with pytest.raises(ContractError):
        validate_lesson_summary(payload)


def test_a_line_break_inside_a_message_field_is_rejected():
    payload = valid()
    payload["short_summary"]["progress"] = "Held tempo\nand kept time"

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload)

    assert any("line break" in v["reason"] for v in excinfo.value.violations)


def test_emoji_can_be_allowed_by_configuration():
    payload = valid()
    payload["short_summary"]["progress"] = "Great work 🎉"

    assert validate_lesson_summary(payload, allow_emoji=True)


def test_links_can_be_allowed_by_configuration():
    payload = valid()
    payload["short_summary"]["covered"] = "See https://example.invalid/notes"

    assert validate_lesson_summary(payload, allow_links=True)


def test_the_line_budget_is_enforced():
    violations = validate_short_summary(
        {"covered": "a", "progress": "b", "homework": "c"}, max_lines=2
    )

    assert any("more than the configured maximum" in v["reason"] for v in violations)


@pytest.mark.parametrize(
    "text",
    ["🎉", "great 🥁 work", "✅ done", "⚠️ careful"],
)
def test_emoji_detection_covers_the_common_ranges(text):
    assert has_emoji(text) is True


@pytest.mark.parametrize("text", ["plain words", "ตีกลองได้ดีขึ้น", "80bpm: steady", "C→G"])
def test_emoji_detection_does_not_fire_on_ordinary_text(text):
    """Thai text and typographic arrows are not emoji; treating them as such
    would make the check unusable for the studio it was written for."""
    assert has_emoji(text) is False


# -- theory callouts ---------------------------------------------------------


def test_a_callout_id_that_does_not_exist_is_rejected():
    """The original rule was "never invent a callout, look it up". Here the
    lookup is enforced: an unknown id cannot be rendered as if it were real."""
    payload = valid(callouts=["vibrato", "made-up-technique"])

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload, known_callouts={"vibrato", "flam"})

    reasons = [v["reason"] for v in excinfo.value.violations]
    assert any("made-up-technique" in reason for reason in reasons)


def test_known_callout_ids_pass():
    payload = valid(callouts=["vibrato"])

    assert validate_lesson_summary(payload, known_callouts={"vibrato", "flam"})


def test_callouts_are_not_checked_when_a_studio_keeps_no_theory_notes():
    payload = valid(callouts=["anything"])

    assert validate_lesson_summary(payload, known_callouts=None)


# -- the escape hatch --------------------------------------------------------


def test_extra_sections_stay_structured():
    payload = valid(
        extra_sections=[{"heading": "Ensemble notes", "items": ["Counted the band in"]}]
    )

    assert validate_lesson_summary(payload)


def test_an_extra_section_without_items_is_rejected():
    with pytest.raises(ContractError):
        validate_lesson_summary(valid(extra_sections=[{"heading": "Empty"}]))


# -- progress: the section that made the others repeat themselves ------------

PROGRESS = [{"before": "Needed counting aloud with them", "after": "Counts through unaided"}]


def test_a_progress_entry_is_accepted():
    validate_lesson_summary(valid(progress=PROGRESS), expect_progress=True)


def test_progress_is_required_once_there_is_a_session_to_compare_with():
    """The prompt already told the model to judge what is new against the
    previous session, and until this section existed there was nowhere to put
    the answer, so it leaked into the overview and the covered list, which is
    how one fact came to be stated three times."""
    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(valid(), expect_progress=True)

    paths = [v["path"] for v in excinfo.value.violations]
    assert "/progress" in paths
    assert any("before" in v.get("hint", "") for v in excinfo.value.violations)


def test_the_first_lesson_is_not_asked_to_invent_a_comparison():
    """No previous session means nothing to compare, and a model told to
    produce progress anyway would produce fiction."""
    validate_lesson_summary(valid(), expect_progress=False)


def test_progress_needs_both_sides_of_the_change():
    """ "Counts unaided" alone is an observation. It only becomes evidence of
    progress beside what it replaced."""
    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(valid(progress=[{"after": "Counts unaided"}]))

    assert any(v["path"].startswith("/progress/0") for v in excinfo.value.violations)


# -- one fact, one section ---------------------------------------------------


def test_a_fact_stated_three_times_is_rejected():
    """Two places is a restatement: the overview naming what the covered list
    details. Three is padding, and it is how a summary grows longer while
    telling a family less."""
    line = "She counts the whole bar herself now without any help from me"
    payload = valid(
        overview=[line],
        covered=[{"topic": "Counting", "detail": line}],
        focus=[{"issue": line, "fix": "Keep it in the warm-up"}],
    )

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload)

    repeats = [v for v in excinfo.value.violations if "repeats" in v["reason"]]
    assert [v["path"] for v in repeats] == ["/focus/0/issue"]
    assert "/overview/0" in repeats[0]["reason"]


def test_saying_something_twice_is_allowed():
    """The overview is meant to name what the sections below detail."""
    line = "She counts the whole bar herself now without any help from me"
    validate_lesson_summary(valid(overview=[line], covered=[{"topic": "Counting", "detail": line}]))


def test_near_duplicates_count_as_the_same_fact():
    """A model that reworded the sentence still said it three times."""
    payload = valid(
        overview=["She counts the whole bar herself now without any help"],
        covered=[{"topic": "Counting", "detail": "She counts the whole bar herself, without help"}],
        focus=[
            {"issue": "She counts a whole bar herself now without any help", "fix": "Keep it up"}
        ],
    )

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload)

    assert any("repeats" in v["reason"] for v in excinfo.value.violations)


def test_two_genuinely_different_sentences_are_not_a_repeat():
    payload = valid(
        overview=["The tempo held all the way through the B section this week"],
        covered=[{"topic": "Reading", "detail": "Named every note on the top two strings"}],
        focus=[{"issue": "The change to C is still late", "fix": "Four beats each, alone"}],
    )

    validate_lesson_summary(payload)


def test_the_repeat_limit_is_configurable():
    line = "She counts the whole bar herself now without any help from me"
    payload = valid(
        overview=[line],
        covered=[{"topic": "Counting", "detail": line}],
        focus=[{"issue": line, "fix": "Keep it in the warm-up"}],
    )

    validate_lesson_summary(payload, max_repeats=3)


# -- a verdict is not an observation -----------------------------------------

VAGUE = ["did well", "very good"]


def test_a_rating_instead_of_an_observation_is_rejected():
    """ "Did well" survives any lesson, so it describes none."""
    payload = valid(covered=[{"topic": "Rhythm", "detail": "She did well with the fill"}])

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload, vague_phrases=VAGUE)

    assert excinfo.value.violations[0]["path"] == "/covered/0/detail"
    assert "did well" in excinfo.value.violations[0]["reason"]


def test_the_same_thing_said_specifically_passes():
    payload = valid(
        covered=[{"topic": "Rhythm", "detail": "Played three bars into the fill with one cue"}]
    )

    validate_lesson_summary(payload, vague_phrases=VAGUE)


def test_the_parent_message_is_not_held_to_the_body_rule():
    """It restates the document on purpose and has its own rules; a warm line
    to a family is not the failure this catches."""
    payload = valid()
    payload["short_summary"]["progress"] = "She did well this week"

    validate_lesson_summary(payload, vague_phrases=VAGUE)


def test_a_studio_with_no_phrase_list_is_unaffected():
    payload = valid(covered=[{"topic": "Rhythm", "detail": "She did well with the fill"}])

    validate_lesson_summary(payload, vague_phrases=[])


# -- a practice goal must be practisable -------------------------------------

NOT_PRACTICABLE = ["next lesson", "be more"]


def test_a_goal_that_can_only_happen_in_the_lesson_is_rejected():
    """`goals` renders as the checklist a family works through at home. One
    line on it that nobody can tick teaches them to ignore the list."""
    payload = valid(goals=["Score full marks for attention in the next lesson"])

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload, goals_not_practicable=NOT_PRACTICABLE)

    assert excinfo.value.violations[0]["path"] == "/goals/0"
    assert "focus" in excinfo.value.violations[0]["hint"]


def test_an_attitude_is_not_a_practice_goal():
    payload = valid(goals=["Be more open to reading from the page"])

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload, goals_not_practicable=NOT_PRACTICABLE)

    assert excinfo.value.violations[0]["path"] == "/goals/0"


def test_an_actionable_goal_passes():
    payload = valid(goals=["Read the teacher's chart once a day, without playing"])

    validate_lesson_summary(payload, goals_not_practicable=NOT_PRACTICABLE)


def test_the_rule_only_reads_goals():
    """The same words are legitimate elsewhere: what the teacher will do in
    the next lesson belongs in `focus`."""
    payload = valid(
        focus=[{"issue": "Reading tires her quickly", "fix": "Use a larger chart next lesson"}]
    )

    validate_lesson_summary(payload, goals_not_practicable=NOT_PRACTICABLE)


# -- about the playing, not about the child ----------------------------------

TRAIT = ["weakness", "lazy", "short attention span"]


def test_a_word_about_the_child_is_rejected():
    """These pages are kept. A word that sounds diagnostic is the one a family
    remembers, and nobody in the room assessed the child: the lesson observed
    the playing."""
    payload = valid(focus=[{"issue": "Reading is a weakness", "fix": "Larger charts"}])

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload, trait_language=TRAIT)

    violation = excinfo.value.violations[0]
    assert violation["path"] == "/focus/0/issue"
    assert "rather than the playing" in violation["reason"]


def test_the_same_observation_about_the_playing_passes():
    payload = valid(
        focus=[{"issue": "Reading from the page is still slow", "fix": "Larger charts"}]
    )

    validate_lesson_summary(payload, trait_language=TRAIT)


def test_the_two_wording_rules_give_different_corrections():
    """A studio reading a violation has to know which way to rewrite: be
    specific, or say it about the playing."""
    payload = valid(
        overview=["She did well"],
        focus=[{"issue": "Counting is a weakness", "fix": "Clap it first"}],
    )

    with pytest.raises(ContractError) as excinfo:
        validate_lesson_summary(payload, vague_phrases=["did well"], trait_language=TRAIT)

    hints = {v["path"]: v["hint"] for v in excinfo.value.violations}
    assert "what was observed" in hints["/overview/0"]
    assert "not what they are like" in hints["/focus/0/issue"]


# -- the vocabulary pool (warnings, never a gate) -----------------------------


def test_an_exact_pool_spelling_is_never_reported():
    summary = {"overview": ["We worked on Encore this week."]}

    assert vocabulary_near_misses(summary, ["Encore"]) == []


def test_a_near_miss_is_named_with_where_it_sits():
    summary = {"overview": ["We worked on Encour this week."]}

    findings = vocabulary_near_misses(summary, ["Encore"])

    assert findings == [
        "the summary spells it `encour` at /overview/0 where the vocabulary pool spells it `Encore`"
    ]


def test_the_parent_message_is_searched_too():
    """A spelling broken in the message that goes to LINE is broken in the
    message, wherever on the page it sits."""
    summary = {"short_summary": {"covered": "Encour, bars 1-8"}}

    findings = vocabulary_near_misses(summary, ["Encore"])

    assert findings and "/short_summary/covered" in findings[0]


def test_unrelated_text_is_not_a_near_miss():
    summary = {"overview": ["Clapped rhythms at 80bpm, then counted bars."]}

    assert vocabulary_near_misses(summary, ["Encore"]) == []


def test_the_exact_spelling_anywhere_satisfies_the_term():
    """The check is about consistency, not about naming every place a term
    appears, so one right spelling settles the term."""
    summary = {
        "overview": ["Covered Encore, bars 1-8."],
        "covered": [{"topic": "Encor intro", "detail": "Bar 1"}],
    }

    assert vocabulary_near_misses(summary, ["Encore"]) == []


def test_an_empty_pool_reports_nothing():
    summary = {"overview": ["We worked on Encour."]}

    assert vocabulary_near_misses(summary, []) == []


def test_teacher_followup_is_optional_and_shaped():
    payload = copy.deepcopy(VALID)
    validate_lesson_summary(payload)  # absent is fine
    payload["teacher_followup"] = ["Check the F change is on the beat now"]
    validate_lesson_summary(payload)
    for bad in ([], ["ok"], ["x" * 301], ["a", "b", "c", "d", "e"]):
        payload["teacher_followup"] = bad
        with pytest.raises(ContractError):
            validate_lesson_summary(payload)


def test_a_followup_echoing_focus_is_not_repetition():
    payload = copy.deepcopy(VALID)
    echo = payload["focus"][0]["issue"]
    payload["teacher_followup"] = [echo, echo + " again", echo + " once more"]
    validate_lesson_summary(payload)

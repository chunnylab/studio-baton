"""Pure functions behind the YouTube description feature: pulling a video id
out of whatever URL shape a Notion page happens to hold, and rendering a
validated lesson summary into the studio's description format.
"""

from __future__ import annotations

import pytest

from baton.adapters.media.google import extract_video_id
from baton.render.youtube import format_description

# -- extract_video_id ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?si=abc123",
    ],
)
def test_extract_video_id_reads_every_url_shape(url):
    assert extract_video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", ["", None, "https://example.invalid/watch/3", "not a url at all"])
def test_extract_video_id_returns_none_for_anything_else(url):
    assert extract_video_id(url) is None


# -- format_description --------------------------------------------------------

SUMMARY = {
    "overview": ["Held the tempo through the whole B section."],
    "covered": [{"topic": "Blackbird bars 9-16", "detail": "Thumb-and-finger pattern"}],
    "focus": [{"issue": "Late change to C", "fix": "Four beats each, alone"}],
    "goals": ["Bars 9-16 at 80bpm with the backing track"],
}


def test_format_description_includes_every_section():
    description = format_description(
        SUMMARY, instrument="กีตาร์", week=9, student_name="Ada Whitfield", date="23/08/2026"
    )

    assert "กีตาร์" in description
    assert "9" in description
    assert "Ada Whitfield" in description
    assert "23/08/2026" in description
    assert "Held the tempo through the whole B section." in description
    assert "Blackbird bars 9-16: Thumb-and-finger pattern" in description
    assert "Bars 9-16 at 80bpm with the backing track" in description
    assert "Late change to C → Four beats each, alone" in description
    assert "หางยาว" in description  # the studio's own signature footer


def test_format_description_omits_empty_sections():
    description = format_description({}, instrument="", week="", student_name="", date="")

    assert "สิ่งที่เรียน" not in description
    assert "การบ้าน" not in description
    assert "คำแนะนำการฝึก" not in description
    assert "ความคืบหน้า" not in description
    # the footer always renders, even with nothing else to say
    assert "หางยาว" in description


def test_format_description_is_deterministic():
    """Same input, same output, no model runs here."""
    first = format_description(SUMMARY, instrument="กีตาร์", week=9, student_name="Ada")
    second = format_description(SUMMARY, instrument="กีตาร์", week=9, student_name="Ada")

    assert first == second


def test_format_description_replaces_angle_brackets_with_arrows():
    summary = {
        "overview": ["Chord path: Bbm > C#maj7"],
        "covered": [{"topic": "Second pass", "detail": "F#maj7 < C#maj7"}],
    }

    description = format_description(summary)

    assert "Chord path: Bbm → C#maj7" in description
    assert "Second pass: F#maj7 ← C#maj7" in description
    assert "<" not in description
    assert ">" not in description

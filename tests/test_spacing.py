from pathlib import Path

from src.inputs.srt_loader import load_srt
from src.models import SPACING
from tests.helpers import run

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_glued_words():                                   # E3RP space between "and" and "accountability"
    [issue] = run("spacing", "trust andaccountability")
    assert issue.type == SPACING and issue.severity == "medium"
    assert issue.message == 'Missing space: "andaccountability" should be "and accountability".'
    assert issue.suggestion == "trust and accountability"


def test_glued_word_after_a_word():                       # E3RP "foundation" give space after; E2EW "control"
    assert run("spacing", "the foundationof trust")[0].suggestion == "the foundation of trust"
    assert run("spacing", "losing ofcontrol")[0].suggestion == "losing of control"


def test_names_and_unknown_words_are_not_split():
    assert run("spacing", "Danja and Buffett and vCampus") == []


def test_double_space():                                  # E8C text "engines.  When"
    [issue] = run("spacing", "Mitochondria are your energy engines.  When they decline")
    assert issue.severity == "low"
    assert issue.suggestion == 'Use one space: "Mitochondria are your energy engines. When they decline"'


def test_line_break_is_not_a_double_space():
    assert run("spacing", "first line\nsecond line") == []


def test_missing_space_after_punctuation():
    [issue] = run("spacing", "the foundation,and the rest")
    assert issue.message == 'No space after ",".'
    assert issue.suggestion == "the foundation, and the rest"
    assert run("spacing", "the end.Next part")[0].suggestion == "the end. Next part"


def test_space_before_punctuation():
    [issue] = run("spacing", "wait for it , now")
    assert issue.message == 'Space before ",".'
    assert issue.suggestion == "wait for it, now"


def test_numbers_abbreviations_links_and_ellipses_are_fine():
    text = "It costs $2,500 or 3.5% e.g. at www.example.com, U.S. only... wait ..."
    assert run("spacing", text) == []


def test_clean_captions_have_no_spacing_issues():
    assert run("spacing", load_srt(FIXTURES / "clean_sample.srt")) == []


def test_each_part_can_be_switched_off():
    assert run("spacing", "a  b andaccountability x ,y", overrides={"spacing": {
        "double_spaces": False, "space_around_punctuation": False, "glued_words": {"enabled": False}}}) == []

from pathlib import Path

from src.models import REPEATED_WORD, SPELLING
from src.inputs.srt_loader import load_srt
from tests.helpers import captions, kinds, run

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_hourse_suggests_hours():                         # E2CI 00:02:55:16
    [issue] = run("spelling", "We work twenty hourse a week")
    assert issue.type == SPELLING and issue.severity == "medium"
    assert issue.message == 'Possible misspelling: "hourse".'
    assert "hours" in issue.suggestion


def test_misspellings_of_real_examples_suggest_the_right_word():
    # E2NH "health", "higher", "reaction"; E2LC "firm". The sheet gives only the correct word,
    # so these misspellings are made up.
    for wrong, right in [("helth", "health"), ("higer", "higher"), ("reacton", "reaction"), ("frim", "firm")]:
        [issue] = run("spelling", f"it is a {wrong} thing")
        assert right in issue.suggestion, (wrong, issue.suggestion)


def test_forced_glossary_spelling_catches_a_real_word_lookalike():   # E7SS Mark McGwire
    # "McGuire" is in the word list, so only the glossary can catch it.
    assert run("spelling", "Mark McGuire hit 70 home runs") == []
    [issue] = run("spelling", "Mark McGuire hit 70 home runs", glossary=["=Mark McGwire"])
    assert issue.severity == "high"
    assert issue.message == '"McGuire" should be spelled "McGwire" (glossary).'
    assert issue.suggestion == "Mark McGwire hit 70 home runs"


def test_forced_glossary_ignores_common_words_and_short_terms():
    assert run("spelling", "Leg day and a market update", glossary=["=Leo", "=Mark McGwire"]) == []


def test_unknown_capitalized_word_is_a_low_severity_name_question():
    [issue] = run("spelling", "Warren Buffett said so.")
    assert issue.severity == "low" and "glossary.txt" in issue.message
    assert run("spelling", "Warren Buffett said so.", glossary=["Buffett"]) == []


def test_possessive_and_contractions_are_fine():
    assert run("spelling", "Buffett's rule: don't lose money, y'all.", glossary=["Buffett"]) == []


def test_missing_apostrophe_is_flagged():                 # E2AB wrote "thats"
    [issue] = run("spelling", "thats the point")
    assert "that's" in issue.suggestion


def test_extra_words_file_covers_modern_words():
    assert run("spelling", "Download the app and check our vlog playlists") == []


def test_numbers_acronyms_links_and_handles_are_skipped():
    text = "We grew 40% in Q3 with our CEO at www.vcampusx.com and @stingersports #gameday"
    assert run("spelling", text) == []


def test_hyphenated_words_are_checked_per_part():
    [issue] = run("spelling", "a well-knwon fact")
    assert issue.message == 'Possible misspelling: "knwon".'


def test_glued_words_are_left_to_the_spacing_check():
    assert run("spelling", "trust andaccountability") == []
    assert run("spelling", "trust andaccountability", overrides={"spacing": {"enabled": False}}) != []


def test_letter_o_used_as_zero():                         # E3JB "O" is a 0
    issues = run("spelling", "rates fell to O% in 2O26")
    assert [i.suggestion for i in issues] == ["rates fell to 0% in 2O26", "rates fell to O% in 2026"]
    assert all(i.severity == "high" for i in issues)
    assert run("spelling", "O Canada, I owe you 10") == []


def test_lookalike_digits_can_be_switched_off():
    assert run("spelling", "rates fell to O%", overrides={"spelling": {"lookalike_digits": False}}) == []


def test_duplicate_word():                                # E3JB delete duplicate "to"
    [issue] = run("spelling", "we just want to to rest")
    assert issue.type == REPEATED_WORD
    assert issue.message == 'Repeated word: "to to".'
    assert issue.suggestion == 'Remove one: "we just want to rest"'


def test_duplicate_word_across_captions():                # E2AB double word "is"
    issues = run("spelling", captions("the real problem is", "is the timing."))
    assert kinds(issues) == [REPEATED_WORD]
    assert issues[0].caption_id == 2
    assert issues[0].suggestion == 'Remove one: "the timing."'


def test_allowed_and_punctuated_repeats_are_fine():
    assert run("spelling", "no no, I knew that that was it. Go, go!") == []
    assert run("spelling", captions("He said no.", "No way.")) == []


def test_repeats_across_a_long_pause_are_fine():
    caps = captions("the real problem is", "is the timing.", gap=3.0)
    assert run("spelling", caps) == []


def test_clean_captions_only_ask_about_names():
    issues = run("spelling", load_srt(FIXTURES / "clean_sample.srt"))
    assert {(i.severity, i.message) for i in issues} == {
        ("low", 'Unknown word "Buffett": a misspelling, or a name to add to glossary.txt?'),
        ("low", 'Unknown word "McGwire": a misspelling, or a name to add to glossary.txt?'),
    }
    assert run("spelling", load_srt(FIXTURES / "clean_sample.srt"), glossary=["Buffett", "=Mark McGwire"]) == []


def test_disabled():
    assert run("spelling", "hourse", overrides={"spelling": {"enabled": False}}) == []

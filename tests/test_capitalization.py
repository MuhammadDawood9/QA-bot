from pathlib import Path

from src.inputs.srt_loader import load_srt
from src.models import CAPITALIZATION
from tests.helpers import captions, run

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STYLIZED = {"capitalization": {"allow_stylized_lowercase": True}}


def suggestions(issues):
    return [i.suggestion for i in issues]


def test_lowercase_i():                                   # E3JB 00:00:07:15, E2CS "i should be"
    [issue] = run("capitalization", "i should be there")
    assert issue.type == CAPITALIZATION and issue.severity == "high"
    assert issue.suggestion == "I should be there"
    assert suggestions(run("capitalization", "and i'm sure i've said it")) == [
        "and I'm sure i've said it", "and i'm sure I've said it"]


def test_i_contractions_and_abbreviations_are_fine():
    assert run("capitalization", "It's what I've seen, i.e. the trend I'm on.") == []


def test_months_are_proper_nouns():                       # E8C "make august ... capital"
    [issue] = run("capitalization", "born in august, she said")
    assert issue.suggestion == "born in August, she said"
    assert issue.severity == "medium"


def test_glossary_names_get_forced_capitals():            # E8C "make ... leo capital"
    issues = run("capitalization", "born in august, leo energy", glossary=["=Leo"])
    assert suggestions(issues) == ["born in August, leo energy", "born in august, Leo energy"]


def test_may_and_march_are_not_forced():
    assert run("capitalization", "you may march on. See you in May.") == []


def test_stylized_lowercase_client_allows_lowercase(tmp_path):     # E8D client rule
    text = "i met them in august at youtube"
    assert len(run("capitalization", text, glossary=["=YouTube"])) == 3
    assert run("capitalization", text, glossary=["=YouTube"], overrides=STYLIZED) == []


def test_stylized_lowercase_still_flags_wrong_case_that_is_not_lowercase():
    [issue] = run("capitalization", "watch on Youtube", glossary=["=YouTube"], overrides=STYLIZED)
    assert issue.suggestion == "watch on YouTube"


def test_broken_mixed_case_is_flagged_even_for_stylized_clients():    # E8D "realM"
    for overrides in (None, STYLIZED):
        [issue] = run("capitalization", "welcome to the realM", overrides=overrides)
        assert issue.message == 'Odd capitalization: "realM".'
        assert issue.suggestion == "welcome to the realm"
    [issue] = run("capitalization", "THe end is near.")
    assert issue.suggestion == "The end is near."


def test_brand_style_capitals_are_fine():
    assert run("capitalization", "post it on YouTube, TikTok and LinkedIn from your iPhone or eBay, ask the CEOs") == []


def test_forced_phrase_with_wrong_case():                 # E7SS athlete names must be exact
    [issue] = run("capitalization", "and mark mcgwire hit it", glossary=["=Mark McGwire"])
    assert issue.message == 'Must be written exactly "Mark McGwire" (glossary).'
    assert issue.suggestion == "and Mark McGwire hit it"
    [issue] = run("capitalization", "then Mcgwire swung", glossary=["=Mark McGwire"])
    assert issue.suggestion == "then McGwire swung"
    assert run("capitalization", "Mark McGwire and a bookmark", glossary=["=Mark McGwire"]) == []


def test_random_capital_mid_sentence():                   # E3RK do not capitalize "there" / "that"
    [issue] = run("capitalization", "and There it was")
    assert issue.message == 'Capital letter mid-sentence: "There".'
    assert issue.suggestion == "and there it was"
    assert issue.severity == "low"
    [issue] = run("capitalization", "I knew That was wrong")
    assert issue.suggestion == "I knew that was wrong"


def test_sentence_starts_and_names_are_not_random_capitals():
    text = 'Pretty bad. That was it. He said: "There you go." Warren Buffett and New York agree.'
    assert run("capitalization", text) == []


def test_uncommon_capitalized_words_are_left_alone():
    # "Market" is not common enough to be sure it isn't a name or title.
    assert run("capitalization", "the Market moved") == []


def test_caption_starts_are_only_checked_when_enabled():          # E3RK flowing sentences
    caps = captions("there it was, pretty bad", "That I could not stop")
    assert run("capitalization", caps) == []
    flagged = run("capitalization", caps,
                  overrides={"capitalization": {"mid_sentence_capitals": {"check_caption_starts": True}}})
    assert [(i.caption_id, i.suggestion) for i in flagged] == [(2, "that I could not stop")]


def test_caption_after_full_stop_or_long_pause_starts_a_sentence():
    on = {"capitalization": {"mid_sentence_capitals": {"check_caption_starts": True}}}
    assert run("capitalization", captions("It was bad.", "That was it"), overrides=on) == []
    assert run("capitalization", captions("it was bad", "That was it", gap=2.5), overrides=on) == []


def test_clean_captions_have_no_capitalization_issues():
    assert run("capitalization", load_srt(FIXTURES / "clean_sample.srt")) == []


def test_disabled():
    assert run("capitalization", "i said", overrides={"capitalization": {"enabled": False}}) == []

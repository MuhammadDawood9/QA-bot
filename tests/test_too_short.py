import copy
from pathlib import Path

import pytest

from src.checks.too_short import check
from src.config import check_settings, deep_merge, load_config
from src.inputs.srt_loader import load_srt
from src.models import PHRASE_BREAK, TOO_SHORT, Caption

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_mistakes.srt"

# Explicit settings (same shape and values as config.yaml) so tuning config.yaml
# doesn't silently change what these tests mean.
SETTINGS = {
    "enabled": True,
    "min_words": 2,
    "min_duration_sec": 0.5,
    "whitelist": ["wow", "yes", "no"],
    "phrase_break": {
        "enabled": True,
        "dangling_end_words": ["a", "an", "the", "of", "to", "in", "on", "at", "for", "with",
                               "and", "but", "or", "so", "my", "your"],
        "fragment_max_words": 0,
        "fragment_max_gap_sec": 0.3,
    },
    "exceptions": {
        "last_of_segment": True,
        "segment_gap_sec": 2.0,
        "whitelist": True,
        "isolated_by_gap": True,
        "isolation_gap_sec": 1.5,
    },
    "suggestion": {
        "merge_max_gap_sec": 1.0,
        "max_merged_duration_sec": 6.0,
        "max_merged_words": 12,
    },
}


def settings(**overrides):
    return deep_merge(SETTINGS, overrides)


def cap(id, start, end, text):
    return Caption(id=id, start=start, end=end, text=text)


def by_id(issues):
    return {i.caption_id: i for i in issues}


@pytest.fixture(scope="module")
def captions():
    return load_srt(FIXTURE)


def test_fixture_flags_exactly_the_planted_mistakes(captions):
    issues = check(captions, SETTINGS)
    assert [(i.caption_id, i.type) for i in issues] == [
        (4, TOO_SHORT),      # "the" for a few frames (E2CS)
        (6, TOO_SHORT),      # "same" (E2CS)
        (8, PHRASE_BREAK),   # "...turn the" / "corner on costs."
        (10, TOO_SHORT),     # "hormonal" (E8C)
        (11, TOO_SHORT),     # "development" (E8C)
    ]


def test_lone_function_word_for_few_frames(captions):
    issue = by_id(check(captions, SETTINGS))[4]
    assert issue.severity == "high"
    assert "1 word (minimum 2)" in issue.message
    assert "on screen 0.13s" in issue.message
    assert issue.suggestion == 'Merge with next caption: "the market moves on its own."'
    assert issue.timestamp == pytest.approx(5.65)


def test_split_compound_phrase_merges_back_together(captions):
    issues = by_id(check(captions, SETTINGS))
    assert issues[10].suggestion == 'Merge with next caption: "hormonal development"'
    assert issues[11].suggestion == 'Merge with previous caption: "hormonal development"'


def test_dangling_word_phrase_break(captions):
    issue = by_id(check(captions, SETTINGS))[8]
    assert issue.message == 'Caption ends mid-phrase on "the".'
    assert issue.suggestion == 'Merge with next caption: "This is where we turn the corner on costs."'
    assert issue.severity == "medium"


def test_whitelisted_word_is_allowed(captions):
    assert 13 not in by_id(check(captions, SETTINGS))                       # "Wow."
    flagged = by_id(check(captions, settings(whitelist=["yes"])))
    assert flagged[13].type == TOO_SHORT


def test_whitelist_exception_can_be_switched_off(captions):
    flagged = by_id(check(captions, settings(exceptions={"whitelist": False})))
    assert 13 in flagged


def test_last_caption_of_file_is_allowed(captions):
    assert 20 not in by_id(check(captions, SETTINGS))                       # "Thanks."
    no_exceptions = settings(exceptions={"last_of_segment": False, "isolated_by_gap": False})
    assert 20 in by_id(check(captions, no_exceptions))


def test_last_caption_before_long_gap_ends_a_segment():
    caps = [cap(1, 0.0, 2.0, "We closed the deal"), cap(2, 2.05, 3.0, "today"),
            cap(3, 5.5, 7.0, "Next topic is pricing.")]
    assert check(caps, settings(exceptions={"isolated_by_gap": False})) == []
    assert [i.caption_id for i in check(caps, settings(
        exceptions={"isolated_by_gap": False, "last_of_segment": False}))] == [2]


def test_large_gap_before_is_an_intentional_pause(captions):
    assert 16 not in by_id(check(captions, SETTINGS))                       # "Okay." after 2 s
    assert 16 in by_id(check(captions, settings(exceptions={"isolated_by_gap": False})))


def test_gap_threshold_comes_from_settings(captions):
    assert 16 in by_id(check(captions, settings(exceptions={"isolation_gap_sec": 2.5})))


def test_exceptions_never_excuse_a_flash():
    caps = [cap(1, 0.0, 2.0, "That was"), cap(2, 2.05, 2.25, "Wow."), cap(3, 2.3, 4.0, "really good.")]
    issue = by_id(check(caps, SETTINGS))[2]
    # Whitelisted, so the word count is fine, but 0.2 s on screen is still flagged.
    assert issue.message == "Caption too short: on screen 0.20s (minimum 0.50s)."


def test_enough_words_but_too_brief():
    caps = [cap(1, 0.0, 2.0, "Here we go."), cap(2, 2.05, 2.35, "Look at this"), cap(3, 2.4, 4.0, "amazing view.")]
    issue = by_id(check(caps, SETTINGS))[2]
    assert issue.message == "Caption too short: on screen 0.30s (minimum 0.50s)."


def test_min_words_comes_from_client_override():
    caps = [cap(1, 0.0, 2.0, "This is the plan."), cap(2, 2.05, 3.0, "Big moves"),
            cap(3, 3.05, 5.0, "start right now.")]
    assert check(caps, SETTINGS) == []
    issue = by_id(check(caps, settings(min_words=3)))[2]
    assert issue.severity == "medium"
    assert "2 words (minimum 3)" in issue.message


def test_fragment_rule_is_off_by_default_and_catches_so_you_even(captions):
    assert 14 not in by_id(check(captions, SETTINGS))
    flagged = by_id(check(captions, settings(phrase_break={"fragment_max_words": 3})))
    assert flagged[14].type == PHRASE_BREAK
    assert flagged[14].suggestion == 'Merge with next caption: "so you even know what I mean"'


def test_dangling_word_ok_when_sentence_ends():
    caps = [cap(1, 0.0, 2.0, "Who is in?"), cap(2, 2.05, 4.0, "Count me in."),
            cap(3, 4.05, 6.0, "Everyone else is too.")]
    assert check(caps, SETTINGS) == []


def test_dangling_word_before_pause_is_excused():
    caps = [cap(1, 0.0, 2.0, "And the winner is the"), cap(2, 4.0, 6.0, "blue team.")]
    assert check(caps, SETTINGS) == []


def test_phrase_break_can_be_disabled(captions):
    issues = check(captions, settings(phrase_break={"enabled": False}))
    assert PHRASE_BREAK not in {i.type for i in issues}


def test_no_merge_suggestion_when_neighbours_too_far():
    caps = [cap(1, 0.0, 2.0, "We start"), cap(2, 3.2, 3.3, "now"), cap(3, 4.5, 6.0, "with the basics.")]
    issue = by_id(check(caps, SETTINGS))[2]
    assert issue.suggestion == "Show at least 2 words together"


def test_disabled_check_returns_nothing(captions):
    assert check(captions, settings(enabled=False)) == []


def test_empty_and_symbol_only_captions_are_ignored():
    caps = [cap(1, 0.0, 2.0, "Here we go."), cap(2, 2.05, 2.1, "♪"), cap(3, 2.15, 4.0, "Next part now.")]
    assert check(caps, SETTINGS) == []


def test_unsorted_input_is_handled(captions):
    shuffled = list(reversed(captions))
    assert check(shuffled, SETTINGS) == check(captions, SETTINGS)


def test_real_config_yaml_word_lists_are_strings(captions):
    # Unquoted yes/no/on in YAML become booleans; config.yaml must keep them quoted.
    real = check_settings(load_config(), {}, "too_short")
    assert {"yes", "no"} <= set(real["whitelist"])
    assert "on" in real["phrase_break"]["dangling_end_words"]
    check(captions, real)


def test_unquoted_yaml_boolean_is_rejected(captions):
    with pytest.raises(ValueError, match="quotes"):
        check(captions, settings(whitelist=["wow", True, False]))


def test_settings_are_not_mutated(captions):
    before = copy.deepcopy(SETTINGS)
    check(captions, SETTINGS)
    assert SETTINGS == before

"""The free punctuation rules (src/checks/punctuation_style.py) and the opt-in capital at the
start of a sentence (src/checks/capitalization.py), each on the real QC note it was built for."""

import pytest

from src.checks import CHECKS
from src.checks.capitalization import check as capitalization
from src.checks.punctuation_style import check, looks_like_question
from src.config import check_settings, load_client, load_config
from src.models import CAPITALIZATION, PUNCTUATION, Caption

CONFIG = load_config()
DEFAULTS = CONFIG["checks"]["punctuation_style"]
CAPS = CONFIG["checks"]["capitalization"]


def caps(*texts, gap=0.1, length=2.0):
    """Captions one after another, `gap` seconds apart."""
    out, t = [], 0.0
    for n, text in enumerate(texts, start=1):
        out.append(Caption(n, t, t + length, text))
        t += length + gap
    return out


def style(**changes):
    return {**DEFAULTS, **changes}


# --- question marks ------------------------------------------------------------------------------

@pytest.mark.parametrize("sentence, question", [
    ("why would you do that", True),
    ("What is going on", True),
    ("are you serious", True),
    ("Can we start", True),
    ("how did it happen", True),
    ("what I mean is this", False),          # a statement that starts with a question word
    ("how we did it", False),
    ("what's important is the team", False),
    ("is the price right", False),           # an auxiliary, then no pronoun
    ("why", False),
])
def test_what_counts_as_a_question(sentence, question):
    assert looks_like_question(sentence, DEFAULTS["question_marks"]) is question


def test_a_question_ending_in_a_full_stop_is_flagged_with_the_fix():          # E3ME
    [issue] = check(caps("why would you do that.", "nobody knows"), DEFAULTS)
    assert issue.type == PUNCTUATION and issue.caption_id == 1
    assert issue.suggestion == "why would you do that?" and 'not "."' in issue.message


def test_only_the_question_inside_a_caption_is_flagged():
    [issue] = check(caps("We left early. Did you see it! It was late."), DEFAULTS)
    assert issue.suggestion == "We left early. Did you see it? It was late."
    assert check(caps("Did you see it?", "What a day.", "what I mean is this."), DEFAULTS) == []
    assert check(caps("why would you do that."), style(question_marks={**DEFAULTS["question_marks"],
                                                                        "enabled": False})) == []


# --- the client's full-stop style ----------------------------------------------------------------------

def test_no_full_stops_style_flags_each_caption_with_one():                  # E3RK
    settings = style(full_stops="never")
    issues = check(caps("and there it was.", "it got pretty bad. Then we left", "it cost 3.5 million",
                        "Dr. Smith in the U.S.", "and then..."), settings)
    assert [i.caption_id for i in issues] == [1, 2]
    assert issues[0].suggestion == "and there it was" and issues[1].suggestion == "it got pretty bad Then we left"
    assert check(caps("and there it was."), DEFAULTS) == []                   # "any" is the default


def test_sentences_style_wants_end_punctuation_before_a_pause():             # E8D
    settings = style(full_stops="sentences", sentence_gap_sec=2.0)
    captions = caps("that's where we come from", "Now we build", gap=2.5) + \
        [Caption(3, 10.0, 11.0, "and we keep"), Caption(4, 11.1, 12.0, "going,"), Caption(5, 15.0, 16.0, "Done!")]
    issues = check(captions, settings)
    assert [i.caption_id for i in issues] == [1, 2]                           # 3 runs on; 4 ends in a comma
    assert issues[0].suggestion == "that's where we come from."


def test_trailing_commas_and_comma_count():                                  # E2AB
    captions = caps("so, I went to the store,", "and then I left")
    assert check(captions, DEFAULTS) == []
    [issue] = check(captions, style(trailing_commas=False))
    assert issue.caption_id == 1 and issue.suggestion == "so, I went to the store"
    [many] = check(caps("so, I went, to the store, and, left"), style(max_commas_per_caption=2))
    assert "4 commas" in many.message


# --- a capital at the start of a sentence ------------------------------------------------------------------

def test_sentence_start_capital_is_off_by_default_and_flags_when_on():       # E8C
    captions = caps("aging well after forty", "is easier than you think. most people", "wait for it")
    assert [i for i in capitalization(captions, CAPS) if "sentence starts" in i.message] == []
    on = {**CAPS, "sentence_start_capital": True}
    issues = [i for i in capitalization(captions, on) if "sentence starts" in i.message]
    # caption 2 continues caption 1's sentence; "most" follows a full stop; caption 3 continues caption 2
    assert [(i.caption_id, i.suggestion) for i in issues] == [(1, "Aging well after forty"),
                                                             (2, "is easier than you think. Most people")]
    stylized = {**on, "allow_stylized_lowercase": True}
    assert [i for i in capitalization(captions, stylized) if "sentence starts" in i.message] == []
    after_pause = [Caption(1, 0, 1, "it ends here"), Caption(2, 4, 5, "next one")]
    assert [i.caption_id for i in capitalization(after_pause, on) if i.type == CAPITALIZATION] == [1, 2]


# --- wiring ----------------------------------------------------------------------------------------------

def test_the_check_is_registered_in_the_menu_and_in_the_client_styles():
    from src.app.choices import CHECK_TOGGLES

    assert "punctuation_style" in CHECKS and "punctuation_style" in {k for k, _ in CHECK_TOGGLES}
    assert check_settings(CONFIG, load_client(CONFIG, "E3RK"), "punctuation_style")["full_stops"] == "never"
    assert check_settings(CONFIG, load_client(CONFIG, "E8D"), "punctuation_style")["full_stops"] == "sentences"
    assert check_settings(CONFIG, load_client(CONFIG, "E2AB"), "punctuation_style")["trailing_commas"] is False
    assert check_settings(CONFIG, load_client(CONFIG, "E8C"), "capitalization")["sentence_start_capital"] is True
    assert "=Mark McGwire" in load_client(CONFIG, "E7SS")["glossary"]
    assert check(caps("why would you."), {**DEFAULTS, "enabled": False}) == []


def test_a_link_inside_a_caption_doesnt_start_a_sentence():
    on = {**CAPS, "sentence_start_capital": True}
    captions = [Caption(1, 0, 3, "You can sign up at www.example.com\nor follow @stingersports for updates.")]
    assert [i for i in capitalization(captions, on) if "sentence starts" in i.message] == []
    after_stop = [Caption(1, 0, 3, "Sign up at www.example.com. or follow us")]
    assert [i.suggestion for i in capitalization(after_stop, on) if "sentence starts" in i.message] == \
        ["Sign up at www.example.com. Or follow us"]

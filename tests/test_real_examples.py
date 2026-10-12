"""Every spelling / punctuation mistake from data/examples.csv, and what catches it.

PHASE2 rows are run through the real checks with the shipped config.yaml (and the client's own
file, e.g. E8D's stylized-lowercase override). Where the sheet only gives the correct word
("health"), the misspelling in the caption is made up; the comment says so.

LATER rows can't be caught from caption text alone, with the reason. If new rows appear in
the spreadsheet, test_every_row_is_classified fails until they are added here.
"""

import csv
from pathlib import Path

import pytest

from src.checks import CHECKS
from src.config import load_client
from src.main import build_context
from tests.helpers import CONFIG, captions

EXAMPLES_CSV = Path(__file__).resolve().parent.parent / "data" / "examples.csv"

# key: (client, video_timestamp, editor_wrote) -> (caption text, expected issue type, expected
#      text in the suggestion or message, glossary lines for the test)
PHASE2 = {
    ("E2CS", "00:00:38:45", "i should be"): ("i should be", "capitalization", "I should be", []),
    ("E7SS", "00:00:55:53", ""): ("Mark McGuire hit 70", "spelling", "McGwire", ["=Mark McGwire"]),  # made-up misspelling
    ("E2NH", "00:00:28:43", ""): ("good helth habits", "spelling", "health", []),                   # made-up misspelling
    ("E2NH", "00:00:29:28", ""): ("aim higer today", "spelling", "higher", []),                     # made-up misspelling
    ("E2NH", "00:00:29:57", ""): ("a chemical reacton", "spelling", "reaction", []),                # made-up misspelling
    ("E8C", "", "august, leo"): ("born in august, leo", "capitalization", "August", ["=Leo"]),
    ("E2LC", "00:00:27:09", ""): ("our frim believes", "spelling", "firm", []),                     # made-up misspelling
    ("E2CI", "00:02:55:16", "hourse"): ("twenty hourse a week", "spelling", "hours", []),
    ("E3RP", "00:00:05:06", ""): ("the foundationof it", "spacing", "foundation of", []),           # made-up glue
    ("E3RP", "00:02:42:21", ""): ("trust andaccountability", "spacing", "and accountability", []),
    ("E8D", "00:00:07:06", "realM"): ("enter the realM", "capitalization", "realm", []),
    ("E2EW", "00:07:30:15", ""): ("losing ofcontrol", "spacing", "of control", []),                 # made-up glue
    ("E3JB", "00:00:00:46", "O"): ("rates at O% now", "spelling", "0%", []),                       # the sheet has no context for "O"
    ("E3JB", "00:00:07:15", "i"): ("and i knew it", "capitalization", "I knew", []),
    ("E3JB", "00:01:33:16", ""): ("we want to to rest", "repeated_word", "want to rest", []),
    ("E3RK", "0:35", ""): ("I knew That was wrong", "capitalization", "that was", []),
    ("E3RK", "0:09", ""): ("and There it was", "capitalization", "there it was", []),
}

LATER = {
    ("E8C", "", "aging"): "title capitalization: the SRT doesn't say which caption is a title",
    ("E2CS", "00:00:07:50", "picked up"): "Phase 4: both are real words, only the audio tells",
    ("E2CS", "00:00:08:09", "one is"): "Phase 4: extra word, only the audio tells",
    ("E2CS", "00:00:35:12", "shoulders"): "Phase 3: possessive apostrophe needs grammar context",
    ("E7SS", "", ""): "no detail in the comment",
    ("E3ME", "00:01:03:11", ""): "Phase 3 / layout: question mark placement",
    ("E3RP", "00:05:46:19", ""): "layout: line break position",
    ("E8D", "00:00:00:12", ""): "client style preference (lowercase wanted), not a rule",
    ("E8D", "00:00:11:03", "link up"): "client style preference (capital wanted), not a rule",
    ("E8D", "00:00:04:00", ""): "Phase 4: missing word, only the audio tells",
    ("E2AB", "0:05", ""): "Phase 3: commas",
    ("E3JB", "00:00:40:29", ""): "Phase 4: missing word",
    ("E3JB", "00:00:06:04", "false"): "Phase 4: real word, wrong for the audio",
    ("E3JB", "00:01:47:19", "Are"): "Phase 4: real word, wrong for the audio",
    ("E3JB", "00:01:55:09", ""): "Phase 3: commas",
    ("E3JB", "00:02:13:01", "Cause"): "Phase 3 / 4: verb form",
    ("E3RK", "0:03", ""): "Phase 3: full stops",
    ("E3RK", "0:25", ""): "Phase 3: full stop and the capital after it",
    ("E3RK", "1:01", ""): "Phase 3 / 4: wording",
    ("E3RK", "1:13", "in a way"): "Phase 4: real words, wrong for the audio",
    ("E3RK", "0:11", ""): "Phase 3: commas",
    ("E3RK", "0:41", ""): "Phase 4: missing word",
    ("E8D", "0:13", ""): "Phase 3: commas",
    ("E8D", "0:27", ""): "Phase 3: full stops",
}


@pytest.mark.parametrize("key", list(PHASE2), ids=lambda k: f"{k[0]}-{k[1] or k[2]}")
def test_phase2_catches_real_example(key):
    client_code = key[0]
    text, expected_type, expected_text, glossary = PHASE2[key]
    client = load_client(CONFIG, client_code) if client_code == "E8D" else {"client": client_code, "overrides": {}}
    client = {**client, "glossary": glossary}
    context = build_context(CONFIG, client)

    issues = []
    for name, check in CHECKS.items():
        if name != "punctuation":          # Phase 3 (API) is tested with a fake in test_punctuation.py
            issues += check(captions(text), context.settings_for(name), context)
    matching = [i for i in issues if i.type == expected_type
                and (expected_text in i.suggestion or expected_text in i.message)]
    assert matching, f"{key}: expected {expected_type} mentioning {expected_text!r}, got {issues}"


@pytest.mark.skipif(not EXAMPLES_CSV.exists(), reason="run scripts/prepare_data.py first")
def test_every_row_is_classified():
    with EXAMPLES_CSV.open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["category"] in ("spelling", "punctuation")]
    keys = {(r["client"], r["video_timestamp"], r["editor_wrote"]) for r in rows}
    assert len(keys) == len(rows) == 41
    unclassified = keys - PHASE2.keys() - LATER.keys()
    assert not unclassified, f"classify these rows in tests/test_real_examples.py: {sorted(unclassified)}"
    assert not (PHASE2.keys() & LATER.keys())

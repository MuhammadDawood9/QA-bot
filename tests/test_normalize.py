import pytest

from src.utils.normalize import number_to_words, tokenize, word_tokens

EQUIV = {"ok": "okay", "gonna": "going to", "alright": "all right"}
FILLERS = {"um", "uh", "ah"}


@pytest.mark.parametrize("n, words", [
    (0, "zero"), (7, "seven"), (19, "nineteen"), (40, "forty"), (42, "forty two"),
    (100, "one hundred"), (2500, "two thousand five hundred"), (1_000_000, "one million"),
    (2026, "two thousand twenty six"),
])
def test_number_to_words(n, words):
    assert " ".join(number_to_words(n)) == words


@pytest.mark.parametrize("word, tokens", [
    ("40%", "forty percent"),
    ("$2,500", "two thousand five hundred dollars"),
    ("3.5", "three point five"),
    ("1st", "first"), ("21st", "twenty first"), ("22nd", "twenty second"), ("3rd", "third"),
    ("12th", "twelfth"), ("30th", "thirtieth"),
    ("Hello,", "hello"), ("“Quoted”", "quoted"), ("rest.", "rest"),
    ("It's", "its"), ("Y’all", "yall"), ("don't", "dont"),
    ("well-known", "well known"), ("Q&A", "q and a"),
    ("OK", "okay"), ("gonna", "going to"),
    ("um", ""), ("Uh,", ""),
    ("...", ""),
])
def test_word_tokens(word, tokens):
    assert " ".join(word_tokens(word, FILLERS, EQUIV)) == tokens


def test_tokens_remember_their_original_word():
    tokens = tokenize(["We", "grew", "40%", "um", "today."], FILLERS, EQUIV)
    assert [(t.text, t.source) for t in tokens] == [
        ("we", 0), ("grew", 1), ("forty", 2), ("percent", 2), ("today", 4)]

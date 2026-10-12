from src.utils.dictionary import Dictionary, load_word_list
from src.utils.glossary import Glossary

GLOSSARY_TEXT = """\
# comment
Danja
=Mark McGwire
=YouTube      # inline comment
=Leo

vCampus
"""


def test_glossary_parses_plain_and_forced_terms():
    g = Glossary.from_lines(GLOSSARY_TEXT.splitlines())
    assert g.terms == ["Danja", "vCampus"]
    assert g.forced == ["Mark McGwire", "YouTube", "Leo"]
    assert g.forced_words == {"youtube": "YouTube", "leo": "Leo"}
    assert g.forced_phrases == ["Mark McGwire"]
    assert g.forced_tokens["mcgwire"] == "McGwire"
    assert g.is_known("DANJA") and g.is_known("mcgwire") and not g.is_known("buffett")


def test_glossary_load_merges_client_lines_and_tolerates_missing_file(tmp_path):
    path = tmp_path / "glossary.txt"
    path.write_text("Danja\n", encoding="utf-8")
    g = Glossary.load(path, extra=["=Mark McGwire"])
    assert g.all_terms() == ["Mark McGwire", "Danja"]
    assert Glossary.load(tmp_path / "missing.txt").all_terms() == []


def test_load_word_list(tmp_path):
    path = tmp_path / "words.txt"
    path.write_text("# header\napp\n\n  vlog \n", encoding="utf-8")
    assert load_word_list(path) == ["app", "vlog"]
    assert load_word_list(tmp_path / "missing.txt") == []


def test_dictionary_known_words():
    d = Dictionary(glossary=Glossary.from_lines(["Buffett"]))
    assert d.is_known("health") and d.is_known("Y'all") and d.is_known("don’t")
    assert d.is_known("Buffett's")                 # glossary word + possessive
    assert not d.is_known("hourse") and not d.is_known("dont")


def test_extra_words_are_known_and_suggestible():
    d = Dictionary(extra_words=["app", "vlog"])
    assert d.is_known("app") and d.is_known("vlogs") is False
    assert "vlog" in d.suggestions("vlogg")
    assert not Dictionary().is_known("app")        # really missing from the base word list


def test_suggestions_skip_junk_entries():
    d = Dictionary()
    assert "heth" not in d.suggestions("helth")    # "heth" is in the word list at the junk count
    assert d.suggestions("helth")[0] == "health"


def test_split_glued_words():
    d = Dictionary()
    short = {"and", "of", "an"}
    assert d.split_glued("andaccountability", short_words=short) == ("and", "accountability")
    assert d.split_glued("ofcontrol", short_words={"of"}) == ("of", "control")
    assert d.split_glued("Danja") is None          # "dan" + "ja" is not a real split
    assert d.split_glued("health") is None         # real words are never split

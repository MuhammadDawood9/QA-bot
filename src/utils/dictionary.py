"""English word list (pyspellchecker) combined with the glossary."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable

from spellchecker import SpellChecker

from src.utils.glossary import Glossary

# pyspellchecker gives its rarest entries (many of them junk, like "heth") a count of 50.
JUNK_FREQUENCY = 50
# Words from extra_words.txt get a mid-range count: known, and common enough to be suggested.
EXTRA_WORD_FREQUENCY = 5000


def load_word_list(path: Path | None) -> list[str]:
    """One word per line; blank lines and # comments ignored. A missing file is an empty list."""
    if path is None or not Path(path).is_file():
        return []
    with Path(path).open(encoding="utf-8") as f:
        return [w.strip() for w in f if w.strip() and not w.lstrip().startswith("#")]


@lru_cache(maxsize=4)
def _spellchecker(language: str, extra_words: tuple[str, ...]) -> SpellChecker:
    """Load the word list once per language and extra-word set; it is never changed afterwards."""
    spell = SpellChecker(language=language)
    freq = spell.word_frequency
    extra = {w.replace("’", "'").lower(): EXTRA_WORD_FREQUENCY
             for w in extra_words if w.lower() not in freq}
    if extra:
        freq.load_json(extra)
    return spell


class Dictionary:
    def __init__(self, language: str = "en", glossary: Glossary | None = None,
                 extra_words: Iterable[str] = ()):
        self._spell = _spellchecker(language, tuple(extra_words))
        self._freq = self._spell.word_frequency
        self.glossary = glossary or Glossary()

    def frequency(self, word: str) -> int:
        return self._freq[word.replace("’", "'").lower()]

    def is_known(self, word: str) -> bool:
        w = word.replace("’", "'").lower().strip("'")
        if not w or self.glossary.is_known(w) or self._freq[w] > 0:
            return True
        if w.endswith("'s") and len(w) > 2:     # Buffett's -> Buffett
            base = w[:-2]
            return self.glossary.is_known(base) or self._freq[base] > 0
        return False

    def suggestions(self, word: str, limit: int = 3, min_frequency: int = JUNK_FREQUENCY + 1) -> list[str]:
        """Most common real words within two edits, most frequent first."""
        w = word.replace("’", "'").lower()
        found = self._spell.candidates(w) or set()
        ranked = sorted((c for c in found if c != w and self._freq[c] >= min_frequency),
                        key=lambda c: (-self._freq[c], c))
        return ranked[:limit]

    def split_glued(self, word: str, min_part_frequency: int = 100,
                    short_words: frozenset[str] | set[str] = frozenset()) -> tuple[str, str] | None:
        """Split a word that is two real words run together ("andaccountability").

        Each part must be a common word (or in the glossary), and at least 3 letters unless it is
        in short_words, so a name like "Danja" isn't read as "dan" + "ja".
        """
        w = word.lower()
        if not w.isalpha() or self.is_known(w):
            return None
        # One wrong letter explains "reacton" (reaction) better than a missing space ("react on").
        one_typo_away = self._spell.known(self._spell.edit_distance_1(w))
        if any(self._freq[c] > JUNK_FREQUENCY for c in one_typo_away):
            return None

        def ok(part: str) -> bool:
            if len(part) < 3 and part not in short_words:
                return False
            return self.glossary.is_known(part) or self._freq[part] >= min_part_frequency

        best, best_score = None, -1
        for i in range(1, len(w)):
            left, right = w[:i], w[i:]
            if ok(left) and ok(right):
                score = min(self._freq[left], self._freq[right])
                if score > best_score:
                    best, best_score = (word[:i], word[i:]), score
        return best

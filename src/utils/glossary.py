"""glossary.txt: terms the checks must accept, and forced spellings that must match exactly.

    Danja           plain term: accepted in any capitalization
    =Mark McGwire   forced: must appear exactly like this
    # comment
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Iterable

_WORD = re.compile(r"[\w'’]+(?:-[\w'’]+)*")


def _norm(word: str) -> str:
    return word.replace("’", "'").lower()


@dataclass
class Glossary:
    terms: list[str] = field(default_factory=list)    # plain terms, as written
    forced: list[str] = field(default_factory=list)   # forced spellings, as written

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> "Glossary":
        glossary = cls()
        for line in lines:
            line = line.split(" #", 1)[0].strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("="):
                term = line[1:].strip()
                if term:
                    glossary.forced.append(term)
            else:
                glossary.terms.append(line)
        return glossary

    @classmethod
    def load(cls, path: Path | None, extra: Iterable[str] = ()) -> "Glossary":
        """glossary.txt plus extra lines (a client's own "glossary:" list). A missing file is empty."""
        lines: list[str] = []
        if path is not None and Path(path).is_file():
            with Path(path).open(encoding="utf-8") as f:
                lines = f.read().splitlines()
        return cls.from_lines([*lines, *(extra or [])])

    @cached_property
    def known_words(self) -> set[str]:
        """Every word in any term, lowercased: never a spelling mistake."""
        return {_norm(w) for term in self.terms + self.forced for w in _WORD.findall(term)}

    def is_known(self, word: str) -> bool:
        return _norm(word) in self.known_words

    @cached_property
    def forced_words(self) -> dict[str, str]:
        """lowercase -> exact spelling, for one-word forced terms."""
        return {_norm(t): t for t in self.forced if len(_WORD.findall(t)) == 1}

    @cached_property
    def forced_phrases(self) -> list[str]:
        return [t for t in self.forced if len(_WORD.findall(t)) > 1]

    @cached_property
    def forced_tokens(self) -> dict[str, str]:
        """lowercase -> exact spelling, for every word inside every forced term."""
        return {_norm(w): w for t in self.forced for w in _WORD.findall(t)}

    def all_terms(self) -> list[str]:
        """Every term in its written form (for Whisper's initial_prompt in Phase 4)."""
        return self.forced + self.terms

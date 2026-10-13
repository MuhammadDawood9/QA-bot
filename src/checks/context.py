"""Shared, read-only things a check may need besides the captions and its own settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from src.utils.dictionary import Dictionary
from src.utils.glossary import Glossary


@dataclass
class CheckContext:
    glossary: Glossary = field(default_factory=Glossary)
    language: str = "en"
    extra_words: list[str] = field(default_factory=list)   # from extra_words.txt
    settings: dict = field(default_factory=dict)           # every check's merged settings, by check name
    client: dict = field(default_factory=dict)             # the client file (code, notes, overrides)
    rules: list[dict] = field(default_factory=list)        # rules.yaml entries
    examples: list[dict] = field(default_factory=list)     # data/examples.csv rows
    llm: object | None = None                              # API client; tests put a fake here
    llm_cache_file: Path | None = None                     # where API answers are cached
    transcript: list | None = None                         # SpokenWords from Whisper; None = no video given
    screen_text: list | None = None                        # on-screen texts that aren't captions; None = not read
    stats: dict = field(default_factory=dict)              # per-check counters for the CLI summary
    ocr_low_confidence: float = 0.75                       # OCR captions read less surely: text issues marked doubtful
    ocr_always_doubtful: tuple[str, ...] = ()              # issue types always marked doubtful on OCR captions
    progress: object | None = None                         # a src.utils.progress.Stage for slow checks, or None
    configured: dict = field(default_factory=dict)         # check name -> enabled in config + client file, before
                                                           # this run's switches (so "off in config" can be told apart)

    @cached_property
    def dictionary(self) -> Dictionary:
        """Loaded on first use (about 0.3 s), so checks that don't need it stay instant."""
        return Dictionary(self.language, self.glossary, self.extra_words)

    def settings_for(self, check_name: str) -> dict:
        return self.settings.get(check_name, {})

"""Helpers for the Phase 2 tests. These use the real config.yaml defaults, so the tests also
prove that the shipped settings catch the real mistakes."""

from __future__ import annotations

from typing import Iterable, Sequence

from src.checks import CHECKS
from src.checks.context import CheckContext
from src.config import load_config
from src.main import build_context
from src.models import Caption, Issue

CONFIG = load_config()


def captions(*texts: str, duration: float = 2.0, gap: float = 0.05) -> list[Caption]:
    """Consecutive captions, 2 s each with a 0.05 s gap."""
    out, t = [], 0.0
    for i, text in enumerate(texts, start=1):
        out.append(Caption(id=i, start=t, end=t + duration, text=text))
        t += duration + gap
    return out


def context(glossary: Iterable[str] = (), overrides: dict | None = None) -> CheckContext:
    client = {"client": "TEST", "glossary": list(glossary), "overrides": overrides or {}}
    return build_context(CONFIG, client)


def run(check_name: str, caps: Sequence[Caption] | str, glossary: Iterable[str] = (),
        overrides: dict | None = None) -> list[Issue]:
    if isinstance(caps, str):
        caps = captions(caps)
    ctx = context(glossary, overrides)
    return CHECKS[check_name](caps, ctx.settings_for(check_name), ctx)


def kinds(issues: Sequence[Issue]) -> list[str]:
    return [i.type for i in issues]

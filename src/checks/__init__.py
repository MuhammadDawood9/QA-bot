"""Registry of checks. Each check takes (captions, settings, context) and returns a list of Issues,
so it never needs to know whether the captions came from an SRT file or OCR.

settings is the check's block from config.yaml with the client's overrides applied.
context (src/checks/context.py) carries the glossary and the dictionary.
"""

from src.checks import capitalization, punctuation, screen_text, spacing, spelling, too_short, transcript

CHECKS = {
    "too_short": too_short.check,
    "spelling": spelling.check,
    "capitalization": capitalization.check,
    "spacing": spacing.check,
    "punctuation": punctuation.check,   # Anthropic API; skipped (with a warning) without ANTHROPIC_API_KEY
    "transcript": transcript.check,     # caption vs audio; runs only when a video is given (--video)
    "screen_text": screen_text.check,   # caption names vs logos and titles in the video (--video)
}

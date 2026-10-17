"""Is there enough speech in the audio for the caption-vs-audio comparison?

Whisper's voice filter can strip a clip that is mostly music or silence down to a word or two.
Comparing captions against that is misleading, so the comparison skips itself and the report says
why. Everything here is worked out from the transcript already in hand (cached or fresh) and the
file's duration, never by listening to the audio again: changing the limits never re-transcribes.

Skipping only sets a flag. The transcript itself is left exactly as it is, so the audio-edit
checks (E2CL podcasts), which keep their own transcript, always see the same words.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.models import SpokenWord


@dataclass(frozen=True)
class SpeechSummary:
    """What was measured, so the limits can be tuned on real files. The note, the log line and the
    app status all carry these numbers."""

    words: int                       # every transcribed word, whatever Whisper's confidence
    speech_seconds: float            # spoken spans merged, so overlapping words aren't counted twice
    duration: float | None           # the file's total audio seconds, when it could be read
    coverage: float | None           # speech_seconds / duration; None when the duration is unknown
    mean_probability: float          # mean word probability; 0 with no words
    no_speech_share: float | None    # share of Whisper's segments sure to hold no speech; None if unavailable

    def measurements(self) -> str:
        """The numbers behind the decision, for the note, the log line and the app status."""
        parts = [f"speech {self.speech_seconds:.0f} s"]
        if self.coverage is not None:
            parts.append(f"coverage {self.coverage:.0%}")
        if self.words:
            parts.append(f"mean probability {self.mean_probability:.2f}")
        if self.no_speech_share is not None:
            parts.append(f"no-speech share {self.no_speech_share:.0%}")
        return ", ".join(parts)

    def reason(self, settings: dict) -> str | None:
        """Why the comparison has nothing reliable to compare, or None to run it as usual.

        The measured value and the matching config limit always have separate names on purpose:
        sharing one name silently makes a rule compare a value with itself, so it can never fire
        (or always does). Keep them apart.
        """
        min_words = int(settings.get("min_words", 10))
        min_coverage = float(settings.get("min_speech_coverage", 0.05))
        min_probability = float(settings.get("min_mean_word_probability", 0.5))
        max_no_speech_share = float(settings.get("no_speech_share", 0.5))
        if self.words == 0:
            return f"no speech was found in the audio ({self.measurements()})"
        if self.words < min_words:
            where = f"in {self.duration:.0f} s" if self.duration else ""
            return (f"only {self.words} word{'s' if self.words != 1 else ''} {where} "
                    f"({self.measurements()})")
        if self.coverage is not None and self.coverage < min_coverage:
            return f"speech covers only {self.coverage:.0%} of the audio ({self.measurements()})"
        if self.mean_probability < min_probability:
            return f"the words Whisper heard look like guesses over music ({self.measurements()})"
        if self.no_speech_share is not None and self.no_speech_share >= max_no_speech_share:
            return f"Whisper hears mostly music or noise, not speech ({self.measurements()})"
        return None


def summarize(words: Sequence[SpokenWord], segments: Sequence[tuple] | None,
              duration: float | None, settings: dict) -> SpeechSummary:
    """Measure a transcript for the skip rule (limits from checks.transcript in the config)."""
    spoken = list(words)
    speech = _speech_seconds(spoken)
    coverage = speech / duration if duration and duration > 0 else None
    mean_probability = sum(w.probability for w in spoken) / len(spoken) if spoken else 0.0
    threshold = float(settings.get("no_speech_probability", 0.6))
    return SpeechSummary(len(spoken), speech, duration, coverage, mean_probability,
                         _no_speech_share(segments, threshold))


def _speech_seconds(words: Sequence[SpokenWord]) -> float:
    """Spoken time: the word spans merged, so overlapping words aren't counted twice."""
    spans = sorted((w.start, w.end) for w in words if w.end > w.start)
    total = 0.0
    start = end = None
    for span_start, span_end in spans:
        if end is None or span_start > end:
            if end is not None:
                total += end - start
            start, end = span_start, span_end
        else:
            end = max(end, span_end)
    if end is not None:
        total += end - start
    return total


def _no_speech_share(segments: Sequence[tuple] | None, threshold: float) -> float | None:
    """The share of the audio Whisper's own segments say holds no speech, by duration."""
    if not segments:
        return None
    total = quiet = 0.0
    for _start, _end, no_speech_probability in segments:
        span = max(0.0, float(_end) - float(_start))
        total += span
        if float(no_speech_probability) >= threshold:
            quiet += span
    return quiet / total if total > 0 else None

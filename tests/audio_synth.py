"""Speech-like audio made with numpy, with planted edit mistakes, for the fast audio tests.

Not real speech: syllables are harmonic sounds with a random pitch and two or three random
formants, some starting with a burst of noise, in words and sentences with natural-length gaps,
over a steady room tone. Every syllable is different, like real syllables, so a clean file has no
two adjacent stretches that sound the same. The words come with their times, standing in for
Whisper's transcript. The slow test (tests/test_audio_edits.py) uses real text-to-speech instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.models import SpokenWord

SR = 16000
VOCAB = ["market", "the", "strong", "today", "we", "think", "rates", "will", "move", "because", "people", "want",
         "growth", "and", "it", "is", "about", "time", "for", "money", "next", "week", "really", "matters"]


def norm_word(text: str) -> str:
    return text.strip(".").lower()


def _syllable(rng, sr: int, dur: float, level_db: float) -> np.ndarray:
    n = int(dur * sr)
    t = np.arange(n) / sr
    f0 = rng.uniform(95, 210) * (1 + rng.uniform(-0.12, 0.12) * t / max(dur, 1e-3))
    phase = 2 * np.pi * np.cumsum(f0) / sr
    formants = [(rng.uniform(280, 850), 90), (rng.uniform(900, 2400), 140), (rng.uniform(2400, 3400), 220)]
    out = np.zeros(n)
    for h in range(1, 40):
        fh = f0.mean() * h
        if fh > 4000:
            break
        amp = sum(np.exp(-0.5 * ((fh - f) / bw) ** 2) for f, bw in formants) + 0.02
        out += amp * np.sin(h * phase + rng.uniform(0, 2 * np.pi))
    env = np.ones(n)
    a, r = int(0.015 * sr), int(min(0.06, dur / 3) * sr)
    env[:a] = np.linspace(0, 1, a)
    env[-r:] = np.cos(np.linspace(0, np.pi / 2, r)) ** 2
    out *= env
    if rng.random() < 0.6:                                   # a consonant: a short noise burst first
        m = int(rng.uniform(0.02, 0.06) * sr)
        burst = rng.standard_normal(m) * np.hanning(m) * 0.6
        out[:m] += burst[:min(m, n)]
    out /= np.sqrt(np.mean(out ** 2)) + 1e-9
    return out * 10 ** (level_db / 20)


def room_tone(rng, n: int, level_db: float, tilt: float = 0.97) -> np.ndarray:
    white = rng.standard_normal(n)
    pink = np.empty(n)
    acc = 0.0
    for i in range(n):                                      # one-pole low-pass: a darker, room-like hiss
        acc = tilt * acc + (1 - tilt) * white[i]
        pink[i] = acc
    pink += 0.15 * white
    pink /= np.sqrt(np.mean(pink ** 2)) + 1e-9
    return pink * 10 ** (level_db / 20)


@dataclass
class Speech:
    samples: np.ndarray
    words: list[SpokenWord]
    sentences: list[tuple[int, int]]                 # word index ranges [i, j)
    sr: int = SR


def make_speech(seed: int = 1, n_sentences: int = 8, words_per_sentence=(5, 9), level_db: float = -20.0,
                room_db: float = -58.0, sr: int = SR) -> Speech:
    rng = np.random.default_rng(seed)
    parts, words, sentences, t = [], [], [], 0.0

    def silence(sec):
        nonlocal t
        parts.append(np.zeros(int(sec * sr)))
        t += int(sec * sr) / sr

    silence(0.6)
    for _ in range(n_sentences):
        first = len(words)
        count = int(rng.integers(*words_per_sentence))
        for k in range(count):
            recent = {norm_word(w.text) for w in words[-3:]}
            text = next(v for v in (VOCAB[int(i)] for i in rng.permutation(len(VOCAB))) if v not in recent)
            start = t
            for _syl in range(int(rng.integers(1, 3))):
                syl = _syllable(rng, sr, rng.uniform(0.13, 0.27), level_db + rng.uniform(-3, 3))
                parts.append(syl)
                t += len(syl) / sr
                silence(rng.uniform(0.0, 0.02))
            words.append(SpokenWord(text + ("." if k == count - 1 else ""), round(start, 3), round(t, 3), 0.95))
            silence(rng.uniform(0.05, 0.13))
        sentences.append((first, len(words)))
        silence(rng.uniform(0.45, 0.75))
    x = np.concatenate(parts)
    x = x + room_tone(rng, len(x), room_db)
    return Speech(x.astype(np.float32), words, sentences, sr)


# --- planting mistakes: each returns new (samples, words) and the time of the mistake ---------------------

def _shift(words, after: float, by: float):
    return [SpokenWord(w.text, round(w.start + by, 3) if w.start >= after else w.start,
                       round(w.end + by, 3) if w.start >= after else w.end, w.probability) for w in words]


def repeat_word(sp: Speech, i: int, gap: float = 0.06) -> tuple[Speech, float]:
    """Say word i twice: its sound copied right after itself (with room tone in between)."""
    w, sr = sp.words[i], sp.sr
    a, b = int(w.start * sr), int(w.end * sr)
    rng = np.random.default_rng(99)
    pause = room_tone(rng, int(gap * sr), -58.0)
    x = np.concatenate([sp.samples[:b], pause, sp.samples[a:b], sp.samples[b:]]).astype(np.float32)
    shift = (b - a + len(pause)) / sr
    words = _shift(sp.words, w.end, shift)
    # Whisper often writes it once: the transcript keeps one word, stretched.
    words[i] = SpokenWord(w.text, w.start, round(w.end + shift, 3), w.probability)
    return Speech(x, words, sp.sentences, sr), w.start


def repeat_phrase(sp: Speech, i: int, n: int = 2, gap: float = 0.08) -> tuple[Speech, float]:
    """Words i..i+n-1 said twice; the transcript shows it (as Whisper does for a whole phrase)."""
    first, last, sr = sp.words[i], sp.words[i + n - 1], sp.sr
    a, b = int(first.start * sr), int(last.end * sr)
    pause = room_tone(np.random.default_rng(98), int(gap * sr), -58.0)
    x = np.concatenate([sp.samples[:b], pause, sp.samples[a:b], sp.samples[b:]]).astype(np.float32)
    shift = (b - a + len(pause)) / sr
    copy = [SpokenWord(w.text.rstrip("."), round(w.start + shift, 3), round(w.end + shift, 3), w.probability)
            for w in sp.words[i:i + n]]
    words = sp.words[:i + n] + copy + _shift(sp.words[i + n:], last.end, shift)
    return Speech(x, words, sp.sentences, sr), first.start


def cut_mid_word(sp: Speech, i: int) -> tuple[Speech, float]:
    """Word i loses its second half and the pause after it: the next word starts mid-sound."""
    w, nxt, sr = sp.words[i], sp.words[i + 1], sp.sr
    # Cut where the word is loud (a cut between two syllables would sound natural): the loudest
    # 10 ms in the middle 40% of the word.
    lo, hi = int((w.start + 0.3 * (w.end - w.start)) * sr), int((w.start + 0.7 * (w.end - w.start)) * sr)
    hop = int(0.01 * sr)
    levels = [float(np.mean(sp.samples[k:k + hop] ** 2)) for k in range(lo, max(lo + 1, hi - hop), hop)]
    mid = (lo + hop * int(np.argmax(levels)) + hop // 2) / sr
    a, b = int(mid * sr), int(nxt.start * sr)
    x = np.concatenate([sp.samples[:a], sp.samples[b:]]).astype(np.float32)
    shift = -(b - a) / sr
    words = list(sp.words)
    words[i] = SpokenWord(w.text, w.start, round(mid, 3), 0.3)
    words = words[:i + 1] + _shift(words[i + 1:], nxt.start, shift)
    return Speech(x, words, sp.sentences, sr), mid


def sentence_pause(sp: Speech, k: int) -> float:
    """The middle of the pause after sentence k (where an editor would cut)."""
    end = sp.words[sp.sentences[k][1] - 1].end
    nxt = sp.words[sp.sentences[k][1]].start if sp.sentences[k][1] < len(sp.words) else end + 0.5
    return round((end + nxt) / 2, 3)


def change_room_tone(sp: Speech, at: float, until: float, extra_db: float = -44.0) -> tuple[Speech, float]:
    """From `at` to `until` a louder, brighter background (another room or mic) is added."""
    sr = sp.sr
    a, b = int(at * sr), int(until * sr)
    rng = np.random.default_rng(7)
    extra = room_tone(rng, b - a, extra_db, tilt=0.5)
    x = sp.samples.copy()
    x[a:b] += extra.astype(np.float32)
    return Speech(x, sp.words, sp.sentences, sr), at


def insert_silence(sp: Speech, at: float, sec: float, digital: bool = False) -> tuple[Speech, float]:
    """A long pause at `at`: room tone (or digital zeros) for `sec` seconds."""
    sr = sp.sr
    a = int(at * sr)
    gap = np.zeros(int(sec * sr)) if digital else room_tone(np.random.default_rng(5), int(sec * sr), -58.0)
    x = np.concatenate([sp.samples[:a], gap, sp.samples[a:]]).astype(np.float32)
    return Speech(x, _shift(sp.words, at, sec), sp.sentences, sr), at


def retake(sp: Speech, k: int, after: int) -> tuple[Speech, float]:
    """Sentence k said again right after sentence `after` (the earlier take left in)."""
    i, j = sp.sentences[k]
    end_after = sp.words[sp.sentences[after][1] - 1].end + 0.5
    sr = sp.sr
    a, b = int(sp.words[i].start * sr), int(sp.words[j - 1].end * sr)
    piece = np.concatenate([sp.samples[a:b], room_tone(np.random.default_rng(3), int(0.5 * sr), -58.0)])
    at = int(end_after * sr)
    x = np.concatenate([sp.samples[:at], piece, sp.samples[at:]]).astype(np.float32)
    shift = len(piece) / sr
    offset = end_after - sp.words[i].start
    copy = [SpokenWord(w.text, round(w.start + offset, 3), round(w.end + offset, 3), w.probability)
            for w in sp.words[i:j]]
    k_end = sp.sentences[after][1]
    words = sp.words[:k_end] + copy + _shift(sp.words[k_end:], end_after, shift)
    return Speech(x, words, sp.sentences, sr), end_after


class Planter:
    """Plants mistakes one after another and keeps the true times right as later edits shift the audio.

        p = Planter(make_speech(3, n_sentences=12))
        p.plant("stutter", repeat_word, 3)
        p.speech, p.truth            # the audio and words, and [(type, time), ...]
    """

    def __init__(self, speech: Speech):
        self.speech = speech
        self.truth: list[tuple[str, float]] = []

    def plant(self, kind: str, fn, *args, **kwargs) -> float:
        before = len(self.speech.samples)
        new, at = fn(self.speech, *args, **kwargs)
        shift = (len(new.samples) - before) / self.speech.sr
        if shift:
            self.truth = [(k, t + shift if t > at + 1e-6 else t) for k, t in self.truth]
        self.speech = new
        self.truth.append((kind, at))
        return at

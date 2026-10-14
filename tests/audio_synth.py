"""Speech-like audio made with numpy, with planted edit mistakes, for the fast audio tests.

Not real speech: syllables are harmonic sounds with a random pitch and two or three random
formants, some starting with a burst of noise, in words and sentences with natural-length gaps,
over a steady room tone. Every syllable is different, like real syllables, so a clean file has no
two adjacent stretches that sound the same. The words come with their times, standing in for
Whisper's transcript. The slow test (tests/test_audio_edits.py) uses real text-to-speech instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from src.models import SpokenWord

SR = 16000
VOCAB = ["market", "the", "strong", "today", "we", "think", "rates", "will", "move", "because", "people", "want",
         "growth", "and", "it", "is", "about", "time", "for", "money", "next", "week", "really", "matters"]


def norm_word(text: str) -> str:
    return text.strip(".").lower()


@dataclass
class Voice:
    """A speaker: a base pitch (Hz) and a formant scale (vocal tract length)."""
    f0: float = 125.0
    formants: float = 1.0


VOICE_A = Voice(120.0, 1.0)         # a lower voice
VOICE_B = Voice(215.0, 1.17)        # a clearly different, higher voice


def _syllable(rng, sr: int, dur: float, level_db: float, voice: Voice | None = None) -> np.ndarray:
    voice = voice or VOICE_A
    n = int(dur * sr)
    t = np.arange(n) / sr
    base = voice.f0 * 2 ** (rng.uniform(-2, 2) / 12)                # intonation: +-2 semitones
    f0 = base * (1 + rng.uniform(-0.06, 0.06) * t / max(dur, 1e-3))
    phase = 2 * np.pi * np.cumsum(f0) / sr
    k = voice.formants
    formants = [(rng.uniform(280, 850) * k, 90), (rng.uniform(900, 2400) * k, 140), (rng.uniform(2400, 3400) * k, 220)]
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
    if n <= 0:
        return np.zeros(0)
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
                room_db: float = -58.0, sr: int = SR, voice: Voice | None = None) -> Speech:
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
                syl = _syllable(rng, sr, rng.uniform(0.13, 0.27), level_db + rng.uniform(-3, 3), voice)
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


# --- a whole E2CL-style episode: two voices, music, effects, a stinger ----------------------------------------

def tone_chord(rng, sr: int, dur: float, freqs, level_db: float) -> np.ndarray:
    """Notes with a few harmonics and a soft attack: sustained, tonal, like a pad or guitar chord."""
    n = int(dur * sr)
    t = np.arange(n) / sr
    out = np.zeros(n)
    for f in freqs:
        for h, amp in ((1, 1.0), (2, 0.45), (3, 0.25), (4, 0.12)):
            out += amp * np.sin(2 * np.pi * f * h * t + rng.uniform(0, 2 * np.pi))
    env = np.minimum(1.0, t / 0.04) * np.exp(-t / (dur * 2.5))
    out *= env
    return out / (np.sqrt(np.mean(out ** 2)) + 1e-9) * 10 ** (level_db / 20)


def music(seed: int, dur: float, level_db: float, sr: int = SR, bpm: float = 104.0) -> np.ndarray:
    """A loop: a chord every two beats over a kick drum on every beat and a hi-hat between."""
    rng = np.random.default_rng(seed)
    n = int(dur * sr)
    out = np.zeros(n)
    beat = 60.0 / bpm
    chords = [(220.0, 277.2, 329.6), (196.0, 246.9, 293.7), (174.6, 220.0, 261.6), (196.0, 246.9, 329.6)]
    k, t = 0, 0.0
    while t < dur:
        a = int(t * sr)
        piece = tone_chord(rng, sr, min(2 * beat, dur - t), chords[k % len(chords)], level_db)
        out[a:a + len(piece)] += piece[:n - a]
        k, t = k + 1, t + 2 * beat
    t = 0.0
    while t < dur:
        a = int(t * sr)
        m = min(int(0.15 * sr), n - a)
        tt = np.arange(m) / sr
        out[a:a + m] += np.sin(2 * np.pi * 60 * tt) * np.exp(-tt / 0.05) * 10 ** ((level_db + 4) / 20) * 1.4
        h = int((t + beat / 2) * sr)
        if h + int(0.03 * sr) < n:
            hm = int(0.03 * sr)
            out[h:h + hm] += rng.standard_normal(hm) * np.exp(-np.arange(hm) / (0.008 * sr)) * 10 ** ((level_db - 6) / 20)
        t += beat
    return out.astype(np.float32)


def sfx_hit(seed: int, level_db: float = -12.0, sr: int = SR) -> np.ndarray:
    """An impact: a low thump and a noise crack, both dying away within ~0.3 s."""
    rng = np.random.default_rng(seed)
    m = int(0.45 * sr)
    t = np.arange(m) / sr
    thump = np.sin(2 * np.pi * 70 * t) * np.exp(-t / 0.06)
    crack = rng.standard_normal(m) * np.exp(-t / 0.025)
    out = thump + 0.6 * crack
    return (out / np.max(np.abs(out)) * 10 ** (level_db / 20) * 1.4).astype(np.float32)


def stinger(seed: int = 77, sr: int = SR) -> np.ndarray:
    """The client's jingle: a quick rising arpeggio of bell-like notes, 1.6 s (a reference file)."""
    rng = np.random.default_rng(seed)
    notes = [523.3, 659.3, 784.0, 1046.5, 784.0, 1046.5]
    out = np.zeros(int(1.6 * sr))
    for k, f in enumerate(notes):
        a = int(k * 0.2 * sr)
        m = len(out) - a
        t = np.arange(m) / sr
        tone = sum(amp * np.sin(2 * np.pi * f * h * t + rng.uniform(0, 6.28)) for h, amp in ((1, 1), (2.76, 0.4), (5.4, 0.2)))
        out[a:] += tone * np.exp(-t / 0.35)
    return (out / np.max(np.abs(out)) * 0.3).astype(np.float32)


@dataclass
class Episode:
    samples: np.ndarray
    words: list[SpokenWord]
    sentences: list[tuple[int, int]]           # word index ranges
    intentional: list[tuple[float, float, str]] # (start, end, what): music, sfx, stinger, overlap, echo
    turns: list[float]                          # times where the voice changes
    reference: np.ndarray                       # the stinger as the client's reference file
    spans: list[tuple[float, float]] = field(default_factory=list)   # each of the 10 sentences, in seconds
    sr: int = SR

    def word_in(self, words, k: int, offset: int = 0) -> int:
        """The index (in `words`) of the offset-th word of sentence k, found by time."""
        a, b = self.spans[k]
        inside = [i for i, w in enumerate(words) if a - 0.01 <= w.start < b and w.probability > 0.5]
        return inside[offset]

    @property
    def speech(self) -> Speech:
        return Speech(self.samples, self.words, self.sentences, self.sr)


def make_episode(seed: int = 1, intro: bool = True, outro: bool = True, bed: bool = True, sfx: bool = True,
                 sting: bool = True, two_voices: bool = True, overlap: bool = True, echo: bool = True,
                 lyrics: bool = True) -> Episode:
    """A clean E2CL-style episode: 10 sentences, alternating voices A and B in different rooms (B's
    room is 8 dB noisier), with a music intro and outro, a music bed under sentences 6-7, an SFX hit
    and the client's stinger in pauses, B talking over the end of sentence 3, A echoing B's last
    word after sentence 1 ("exactly. exactly"), and B answering sentence 4 with no pause at all.
    The transcript stands in for Whisper: crosstalk gives colliding, unsure words; words written
    over the intro music are unsure "la la la" lyrics. Every one of these is intentional: no flags."""
    rng = np.random.default_rng(seed)
    sr = SR
    parts: list[np.ndarray] = []
    words: list[SpokenWord] = []
    sentences, intentional, turns, spans = [], [], [], []
    t = 0.0

    def add(x):
        nonlocal t
        parts.append(np.asarray(x, np.float64))
        t += len(x) / sr

    def pause(sec, room_db):
        add(room_tone(rng, int(sec * sr), room_db))

    room = {0: -58.0, 1: -50.0}
    voices = [VOICE_A, VOICE_B] if two_voices else [VOICE_A, VOICE_A]
    pause(0.4, room[0])
    if intro:
        start = t
        add(music(seed + 1, 6.0, -18.0) + room_tone(rng, int(6.0 * sr), room[0]))
        intentional.append((start, t, "music intro"))
        if lyrics:
            for k in range(4):
                words.append(SpokenWord("la", round(start + 1.0 + 0.6 * k, 3), round(start + 1.3 + 0.6 * k, 3), 0.3))
        pause(0.5, room[0])
    for k in range(10):
        who = k % 2
        voice, rd = voices[who], room[who if two_voices else 0]
        if k > 0 and voices[who] is not voices[(k - 1) % 2]:
            turns.append(t)
        first = len(words)
        count = int(rng.integers(6, 9))
        for i in range(count):
            recent = {w.text.strip(".") for w in words[-4:]}           # no word, and so no phrase, repeats by chance
            text = next(v for v in (VOCAB[int(j)] for j in rng.permutation(len(VOCAB))) if v not in recent)
            ws = t
            for _ in range(int(rng.integers(1, 3))):
                syl = _syllable(rng, sr, rng.uniform(0.13, 0.27), -20.0 + rng.uniform(-3, 3), voice)
                add(syl + room_tone(rng, len(syl), rd))
                pause(rng.uniform(0.0, 0.02), rd)
            last = i == count - 1
            text = ("exactly" if (echo and k == 1 and last) else text) + ("." if last else "")
            words.append(SpokenWord(text, round(ws, 3), round(t, 3), 0.95))
            if not last:
                pause(rng.uniform(0.05, 0.13), rd)
        sentences.append((first, len(words)))
        spans.append((words[first].start, words[-1].end))
        if echo and k == 1 and two_voices:              # B said it; A echoes "exactly" straight away: not a stutter
            pause(0.15, room[0])
            turns.append(t)
            ws = t
            for _ in range(2):
                syl = _syllable(rng, sr, 0.18, -20.0, VOICE_A)
                add(syl + room_tone(rng, len(syl), room[0]))
            words.append(SpokenWord("exactly.", round(ws, 3), round(t, 3), 0.9))
            intentional.append((ws - 0.6, t, "echo by the other speaker"))
        if k == 4 and two_voices:                      # B answers with no pause at all: a turn, not a cut
            pause(0.04, rd)
            continue
        if k == 2 and sting:
            pause(0.4, rd)
            start = t
            add(stinger() + room_tone(rng, int(1.6 * sr), rd))
            intentional.append((start, t, "stinger"))
            pause(0.4, rd)
            continue
        if k == 8 and sfx:
            pause(0.4, rd)
            start = t
            hit = sfx_hit(seed + 3)
            add(hit + room_tone(rng, len(hit), rd))
            intentional.append((start, t, "sfx hit"))
            pause(0.4, rd)
            continue
        pause(rng.uniform(0.5, 0.75), rd)
    if outro:
        start = t
        add(music(seed + 2, 6.0, -18.0) + room_tone(rng, int(6.0 * sr), room[1 if two_voices else 0]))
        intentional.append((start, t, "music outro"))
    pause(0.4, room[0])
    x = np.concatenate(parts)

    if bed:                                            # a music bed under sentences 6 and 7
        a = words[sentences[6][0]].start - 0.3
        b = words[sentences[7][1] - 1].end + 0.3
        ia, ib = int(a * sr), int(b * sr)
        x[ia:ib] += music(seed + 4, (ib - ia) / sr, -34.0)[:ib - ia]
        intentional.append((a, b, "music bed under speech"))
    if overlap and two_voices:                         # B talks over the end of sentence 3
        i, j = sentences[3]
        a = words[j - 3].start
        piece = np.concatenate([_syllable(rng, sr, 0.22, -21.0, VOICE_B) for _ in range(4)])
        ia = int(a * sr)
        x[ia:ia + len(piece)] += piece[:len(x) - ia]
        b = a + len(piece) / sr
        # Whisper in crosstalk: both voices' words, unsure, with colliding times.
        words = [SpokenWord(w.text, w.start, w.end, 0.35) if a - 0.1 <= w.start <= b else w for w in words]
        words += [SpokenWord("yeah", round(a + 0.05, 3), round(a + 0.4, 3), 0.3),
                  SpokenWord("exactly", round(a + 0.3, 3), round(a + 0.8, 3), 0.25)]
        words.sort(key=lambda w: w.start)
        sentences = _reindex(words, sentences)
        intentional.append((a, b, "crosstalk"))
    return Episode(x.astype(np.float32), words, sentences, intentional, turns, stinger(), spans, sr)


def _reindex(words, sentences):
    """Sentence ranges again after words were added: a sentence ends at each word ending in "."."""
    out, start = [], 0
    for i, w in enumerate(words):
        if w.text.endswith("."):
            out.append((start, i + 1))
            start = i + 1
    return out

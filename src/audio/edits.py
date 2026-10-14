"""Audio-edit checks for podcasts (client E2CL and any client whose file switches them on).

    issues, stats = check_audio(samples, sr, words, settings, silences=...)

Every threshold comes from the client file's "audio:" block (config.yaml has the defaults), with
a sensitivity (low / medium / high) choosing a column of values; see effective_settings().

1. Stutters and repeated words / phrases, from the transcript (Whisper, asked to keep
   disfluencies): the same word twice in a row, the same 2-3 word phrase twice, and false starts
   ("I wa- I wanted"). Legitimate repeats ("very very", "no no") are a list in the settings.
2. Repeats Whisper doesn't write down: adjacent stretches of 150-800 ms of speech that sound
   nearly identical (cepstral features compared at every lag), and a repeated take: the same
   phrase said twice within ~30 s that also sounds alike (a retake left in).
3. Abrupt cuts and jump cuts, scored at candidate points (word boundaries, pauses, and anywhere
   the signal jumps) by combining: a click (a sample-level jump much bigger than the local
   signal), a drop to digital silence or a speech offset with no natural fade, a change of
   background noise level or colour across the point, a loudness jump the speech doesn't explain,
   a word cut off mid-word, and a sentence boundary with no pause (breath removed).
4. Dead air: silence longer than N seconds (judged on the 20 ms level against the room tone). Filler words (um, uh) are
   counted, and flagged when the client wants that.

These are heuristics tested on synthetic speech (text-to-speech plus planted edits) and NOT yet on
real podcasts; see the plan file.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from src.audio.features import (band_db, cepstra, frame_db, lag_similarity, moving_mean, sequence_similarity,
                                 spectral_flatness)
from src.models import (ABRUPT_CUT, DEAD_AIR, FILLER, JUMP_CUT, MISSING_CUE, REPEATED_PHRASE, STUTTER, Issue,
                        SpokenWord, audio_issue_id)
from src.utils.timecode import format_timestamp

HOP = 0.01                       # seconds per feature frame
CHECK_NAMES = ("stutter", "repeated_phrase", "repeated_take", "abrupt_cut", "jump_cut", "dead_air", "fillers")


def effective_settings(settings: dict) -> dict:
    """The audio settings with the chosen sensitivity's values laid over the base ones."""
    s = copy.deepcopy(settings or {})
    levels = s.pop("levels", {}) or {}
    level = str(s.get("sensitivity", "medium")).lower()
    s.update(levels.get(level, levels.get("medium", {})) or {})
    return s


def check_on(s: dict, name: str) -> bool:
    return bool((s.get("checks") or {}).get(name, name != "fillers"))


# --- words -----------------------------------------------------------------------------------

_STRIP = re.compile(r"^[^\w'-]+|[^\w'-]+$")


def norm(word: str) -> str:
    """Lowercase, punctuation around the word removed; a trailing "-" (a cut-off word) kept."""
    w = word.strip().lower().replace("’", "'")
    w = _STRIP.sub("", w)
    return w


def is_fragment(word: str) -> bool:
    """"wa-", "I-": Whisper's way of writing a word that was started and broken off."""
    return word.strip().rstrip(",.?!").endswith("-") and len(norm(word).rstrip("-")) >= 1


def context_text(words: Sequence[SpokenWord], start: float, end: float, pad: float = 1.5) -> str:
    near = [w.text.strip() for w in words if w.end >= start - pad and w.start <= end + pad]
    text = " ".join(near)
    return text if len(text) <= 120 else text[:117] + "..."


@dataclass
class Finding:
    """One problem before it becomes an Issue: when, what, how sure, and the evidence."""
    kind: str
    start: float
    end: float
    confidence: float
    message: str
    suggestion: str
    evidence: list[str] = field(default_factory=list)
    pair: tuple | None = None          # a repeat: (first, second) occurrence as SpokenWords spanning each


def transcript_repeats(words: Sequence[SpokenWord], s: dict) -> list[Finding]:
    """The same word / 2-3 word phrase twice in a row, and false starts, from the words."""
    out: list[Finding] = []
    legit = {" ".join(norm(t) for t in str(p).split()) for p in s.get("legit_repeats") or []}
    fillers = {norm(f) for f in s.get("fillers") or []}
    max_gap = float(s.get("max_repeat_gap_sec", 1.0))
    max_n = int(s.get("max_phrase_words", 3))
    tokens = [norm(w.text) for w in words]
    used: set[int] = set()

    for i, w in enumerate(words):
        if i in used:
            continue
        # A false start: "wa-" then a word that begins the same way ("wanted"), or a lone fragment.
        if is_fragment(w.text):
            stem = tokens[i].rstrip("-")
            nxt = next((j for j in range(i + 1, min(i + 4, len(words))) if tokens[j]), None)
            restart = nxt is not None and tokens[nxt].startswith(stem) and words[nxt].start - w.end <= max_gap
            out.append(Finding(STUTTER, w.start, words[nxt].end if restart else w.end, 0.8 if restart else 0.6,
                               f"False start: \"{w.text.strip()}\" is broken off"
                               + (f" and started again (\"{words[nxt].text.strip()}\")." if restart else "."),
                               "Cut the broken-off start.", ["transcript: fragment"]))
            used.add(i)
            continue
        # "rate rates", "invest investors": a word broken off and said again in full.
        # Whisper must be unsure of the first one ("we week" is two words, said clearly).
        if i + 1 < len(words) and tokens[i] and tokens[i + 1] and tokens[i] != tokens[i + 1] \
                and len(tokens[i]) >= 3 and tokens[i + 1].startswith(tokens[i]) \
                and len(tokens[i + 1]) - len(tokens[i]) <= 2 and tokens[i] not in fillers \
                and w.probability < float(s.get("restart_max_prob", 0.8)) \
                and words[i + 1].start - w.end <= max_gap and not w.text.strip().endswith((".", ",", "?", "!")):
            out.append(Finding(STUTTER, w.start, words[i + 1].end, 0.75,
                               f"Stutter: \"{w.text.strip()} {words[i + 1].text.strip()}\" (the word is started, "
                               f"then said again).", f"Cut the first \"{w.text.strip()}\".",
                               ["transcript: restarted word"], pair=(w, words[i + 1])))
            used.update((i, i + 1))
            continue
        for n in range(max_n, 0, -1):
            a, b = tokens[i:i + n], tokens[i + n:i + 2 * n]
            if len(b) < n or not all(a) or a != b or any(t in fillers for t in a):
                continue
            gap = words[i + n].start - words[i + n - 1].end
            if gap > max_gap:
                continue
            phrase = " ".join(a)
            if f"{phrase} {phrase}" in legit or phrase + " " + phrase in legit:
                break
            kind = STUTTER if n == 1 else REPEATED_PHRASE
            said = " ".join(x.text.strip() for x in words[i:i + 2 * n])
            first = SpokenWord(phrase, words[i].start, words[i + n - 1].end, 1.0)
            second = SpokenWord(phrase, words[i + n].start, words[i + 2 * n - 1].end, 1.0)
            out.append(Finding(kind, words[i].start, words[i + 2 * n - 1].end, 0.85 if n == 1 else 0.8,
                               f"{'Word' if n == 1 else 'Phrase'} said twice: \"{said}\".",
                               f"Cut one \"{' '.join(x.text.strip() for x in words[i:i + n])}\".",
                               [f"transcript: {n}-word repeat"], pair=(first, second)))
            used.update(range(i, i + 2 * n))
            break
    return out


# --- the signal ------------------------------------------------------------------------------------

@dataclass
class Signal:
    """Features of the whole file, computed once."""
    samples: np.ndarray
    sr: int
    db: np.ndarray                     # 10 ms frames, dBFS
    cep: np.ndarray                    # 10 ms frames, 12 cepstral coefficients
    bands: np.ndarray                  # 20 ms frames, dB per band (background shape)
    band_level: np.ndarray             # 20 ms frames, total dB
    floor: np.ndarray                  # 10 ms frames: the local noise floor, dBFS
    speech: np.ndarray                 # 10 ms frames: True where someone is (probably) talking
    cep_mean: np.ndarray | None = None  # the file's mean cepstrum (removed from cep)
    flatness: np.ndarray = field(default_factory=lambda: np.zeros(0))   # 20 ms frames, spectral flatness
    excluded: np.ndarray | None = None  # 20 ms frames left out of room tone measurements (music, effects)

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sr

    def at(self, t: float, hop: float = HOP) -> int:
        return int(round(t / hop))


def analyze(samples: np.ndarray, sr: int, s: dict) -> Signal:
    x = np.asarray(samples, dtype=np.float32)
    db = frame_db(x, sr, HOP, 0.02)
    raw = cepstra(x, sr, HOP, 0.025, mean=np.zeros((1, 12), np.float32))
    cep_mean = raw.mean(axis=0, keepdims=True) if len(raw) else np.zeros((1, 12), np.float32)
    cep = raw - cep_mean
    n = min(len(db), len(cep))
    db, cep = db[:n], cep[:n]
    bands = band_db(x, sr, 0.02, 0.04)
    band_level = 10 * np.log10(np.sum(10 ** (bands / 10), axis=1) + 1e-10) if len(bands) else np.zeros(0)
    # The noise floor: a low percentile of the frame levels over a sliding ~10 s window (blocks of 2 s).
    block = 200
    floor = np.empty_like(db)
    for start in range(0, len(db), block):
        lo, hi = max(0, start - 4 * block), min(len(db), start + 5 * block)
        floor[start:start + block] = np.percentile(db[lo:hi], 10) if hi > lo else -100.0
    floor = np.maximum(floor, -100.0)
    speech = db > np.maximum(floor + float(s.get("speech_above_floor_db", 15.0)), float(s.get("speech_min_db", -50.0)))
    flatness = spectral_flatness(x, sr, 0.02, 0.04)
    return Signal(x, sr, db, cep, bands, band_level, floor, speech, cep_mean, flatness)


def speech_share(sig: Signal, start: float, end: float) -> float:
    """How much of [start, end] is speech by its sound (Whisper sometimes writes words into silence)."""
    a, b = sig.at(start), max(sig.at(end), sig.at(start) + 1)
    seg = sig.speech[a:b]
    return float(seg.mean()) if len(seg) else 0.0


def untranscribed_speech(sig: Signal, words: Sequence[SpokenWord], min_sec: float = 5.0) -> list[tuple[float, float]]:
    """Stretches of at least min_sec with plenty of speech in the sound but no words in the
    transcript: Whisper skipped them (it sometimes drops a whole window)."""
    covered = np.zeros(len(sig.speech), bool)
    for w in words:
        covered[sig.at(w.start - 0.3):sig.at(w.end + 0.3)] = True
    gaps, start = [], None
    for i, c in enumerate(np.append(covered, True)):
        if not c and start is None:
            start = i
        elif c and start is not None:
            if (i - start) * HOP >= min_sec and sig.speech[start:i].mean() >= 0.3:
                gaps.append((start * HOP, i * HOP))
            start = None
    return gaps


def fill_gaps(words: Sequence[SpokenWord], extra: Sequence[SpokenWord], gaps: Sequence[tuple[float, float]]) -> list:
    """The words, plus the words of another transcript inside the stretches the first one skipped."""
    add = [w for w in extra if any(a - 0.3 <= w.start and w.end <= b + 0.3 for a, b in gaps)]
    return sorted(list(words) + add, key=lambda w: w.start)


def _overlaps_words(words: Sequence[SpokenWord], start: float, end: float) -> bool:
    return any(w.end > start and w.start < end for w in words)


def _onset(db: np.ndarray, t: int, rise: float, reach: int = 4) -> bool:
    """A sound starts near frame t: the level rises by `rise` dB within a few frames around it."""
    lo, hi = max(0, t - reach), min(len(db), t + reach + 1)
    if hi - lo < 3:
        return False
    seg = db[lo:hi]
    return float(np.max(seg[1:] - np.minimum.accumulate(seg)[:-1])) >= rise


def acoustic_repeats(sig: Signal, words: Sequence[SpokenWord], s: dict) -> list[Finding]:
    """Adjacent 150-800 ms stretches of speech that sound nearly the same: a stutter or repeated
    syllable Whisper smoothed over. For each unit length L, frame t is compared with frame t+L;
    the mean over L frames is how alike [t, t+L) and [t+L, t+2L) are. A sustained vowel or a hum
    is alike at every lag, so a repeat must also be clearly less alike at half its length."""
    lo_ms, hi_ms = s.get("acoustic_unit_ms", [150, 800])
    threshold = float(s.get("acoustic_similarity", 0.85))
    margin = float(s.get("acoustic_margin", 0.2))
    min_speech = float(s.get("acoustic_min_speech", 0.8))
    lags = range(max(4, int(lo_ms / 1000 / HOP) // 2), int(hi_ms / 1000 / HOP) + 1)
    c = sig.cep
    if len(c) < 2 * int(hi_ms / 1000 / HOP):
        return []
    sim = {}
    for lag in lags:
        r = lag_similarity(c, lag)
        sim[lag] = moving_mean(r, lag) if len(r) >= lag else np.zeros(0)
    speech_frac = np.concatenate([[0.0], np.cumsum(sig.speech.astype(np.float64))])

    rise = float(s.get("acoustic_onset_db", 6.0))
    hits: list[tuple[float, int, int]] = []           # (score, t, lag)
    for lag in range(int(lo_ms / 1000 / HOP), int(hi_ms / 1000 / HOP) + 1):
        r = sim.get(lag)
        half = sim.get(lag // 2)
        if r is None or half is None or len(r) == 0:
            continue
        m = min(len(r), len(half))
        cand = np.nonzero((r[:m] >= threshold) & (r[:m] - half[:m] >= margin))[0]
        for t in cand:
            end = t + 2 * lag
            if end >= len(speech_frac):
                continue
            if (speech_frac[end] - speech_frac[t]) / (2 * lag) < min_speech:
                continue
            a_db, b_db = np.median(sig.db[t:t + lag]), np.median(sig.db[t + lag:end])
            if abs(a_db - b_db) > float(s.get("acoustic_max_level_diff_db", 6.0)):
                continue
            if not (_onset(sig.db, t, rise) and _onset(sig.db, t + lag, rise)):
                continue                                   # a repeat starts again where the first copy started
            hits.append((float(r[t]), int(t), lag))
    hits.sort(reverse=True)
    out: list[Finding] = []
    taken: list[tuple[float, float]] = []
    for score, t, lag in hits:
        start, end = t * HOP, (t + 2 * lag) * HOP
        if any(start < b + 0.3 and end > a - 0.3 for a, b in taken):
            continue
        if not _overlaps_words(words, start, end):
            continue                                       # music, a jingle: not speech
        taken.append((start, end))
        conf = min(0.95, 0.55 + (score - threshold) * 2.5)
        out.append(Finding(STUTTER, start, end, round(conf, 2),
                           f"A {lag * HOP * 1000:.0f} ms sound is repeated right after itself (similarity "
                           f"{score:.2f}): a stutter or a doubled syllable the transcript may not show.",
                           "Listen and cut the repeated sound.", [f"acoustic: {score:.2f} at {lag * 10} ms"]))
    return out


def repeated_takes(sig: Signal, words: Sequence[SpokenWord], s: dict) -> list[Finding]:
    """The same phrase (at least take_min_words words) said again within take_window_sec: a retake
    left in. It counts more surely when the two also sound alike."""
    n = int(s.get("take_min_words", 5))
    window = float(s.get("take_window_sec", 30.0))
    tokens = [norm(w.text).rstrip("-") for w in words]
    seen: dict[tuple, list[int]] = {}
    out: list[Finding] = []
    covered_until = -1
    for i in range(len(words) - n + 1):
        key = tuple(tokens[i:i + n])
        if not all(key):
            continue
        for j in seen.get(key, []):
            if i <= covered_until or words[i].start - words[j].start > window or i - j < n:
                continue
            k = n                                          # extend the match as far as it goes
            while i + k < len(words) and j + k < i and tokens[j + k] == tokens[i + k]:
                k += 1
            first = (words[j].start, words[j + k - 1].end)
            second = (words[i].start, words[i + k - 1].end)
            a = sig.cep[sig.at(first[0]):sig.at(first[1])]
            b = sig.cep[sig.at(second[0]):sig.at(second[1])]
            alike = sequence_similarity(a[::2], b[::2]) if len(a) and len(b) else 0.0
            strong = alike >= float(s.get("take_acoustic_similarity", 0.75))
            phrase = " ".join(w.text.strip() for w in words[i:i + k])
            out.append(Finding(REPEATED_PHRASE, second[0], second[1], 0.85 if strong else 0.55,
                               f"Repeated take: \"{phrase}\" was already said {second[0] - first[0]:.0f} s earlier "
                               f"(at {format_timestamp(first[0])})" + (", and sounds the same." if strong else "."),
                               "Keep one take and cut the other.", [f"take: {k} words, sound {alike:.2f}"]))
            covered_until = i + k - 1
            break
        seen.setdefault(key, []).append(i)
    return out


# --- cuts --------------------------------------------------------------------------------------------

def clicks(sig: Signal, s: dict, chunk_sec: float = 30.0) -> list[tuple[float, float]]:
    """(time, strength 0..1) where one sample jumps far more than the signal around it does.
    Worked through in 30 s pieces, so an hour of audio needs megabytes, not gigabytes."""
    x = sig.samples
    if len(x) < 3:
        return []
    ctx = max(4, int(0.004 * sig.sr))                     # 4 ms either side
    ratio_min = float(s.get("click_ratio", 8.0))
    jump_min = float(s.get("click_min_jump", 0.05))
    step = int(chunk_sec * sig.sr)
    out, last = [], -1e9
    for start in range(0, len(x) - 1, step):
        lo, hi = max(0, start - ctx - 2), min(len(x), start + step + ctx + 2)
        d = np.abs(np.diff(x[lo:hi])).astype(np.float64)
        sq = np.concatenate([[0.0], np.cumsum(d ** 2)])
        n = len(d)
        idx = np.arange(n)
        a, b = np.clip(idx - ctx, 0, n), np.clip(idx + ctx + 1, 0, n)
        c0, c1 = np.clip(idx - 1, 0, n), np.clip(idx + 2, 0, n)       # the jump itself (and neighbours) left out
        total = (sq[b] - sq[a]) - (sq[c1] - sq[c0])
        count = np.maximum((b - a) - (c1 - c0), 1)
        local = np.sqrt(total / count) + 1e-6
        hits = np.nonzero((d / local >= ratio_min) & (d >= jump_min))[0]
        for i in hits:
            pos = lo + i
            if pos < start or pos >= start + step:            # belongs to the neighbouring piece
                continue
            t = pos / sig.sr
            if t - last < 0.05:
                continue
            last = t
            out.append((t, float(min(1.0, d[i] / local[i] / (2 * ratio_min)))))
    return out


def abrupt_edges(sig: Signal, s: dict) -> list[tuple[float, float, str]]:
    """Speech that stops dead (no fade) or drops to digital silence: (time, strength, kind)."""
    db, out = sig.db, []
    digital = float(s.get("digital_silence_db", -85.0))
    fade_frames = max(1, int(float(s.get("min_fade_ms", 30)) / 1000 / HOP))
    for i in range(fade_frames + 1, len(db) - 3):
        if not sig.speech[i - 1]:
            continue
        drop = db[i - 1] - db[i + 1]
        if db[i + 1] <= digital and db[i - 1] > digital + 30:
            out.append((i * HOP, 1.0, "drop to digital silence"))
        elif drop >= float(s.get("abrupt_drop_db", 30.0)) and db[i + 1] < sig.floor[i] + 6:
            # a natural end of speech falls over several frames; this one falls in one or two
            out.append((i * HOP, min(1.0, drop / 45.0), "speech stops with no fade"))
    merged, last = [], -1e9
    for t, strength, kind in out:
        if t - last > 0.1:
            merged.append((t, strength, kind))
        last = t
    return merged


def _background(sig: Signal, start: float, end: float, min_sec: float = 0.4) -> np.ndarray | None:
    """The non-speech 20 ms band frames between start and end (None if under min_room_tone_sec of
    them: dips inside speech aren't room tone)."""
    a, b = max(0, int(start / 0.02)), min(len(sig.bands), int(end / 0.02))
    if b <= a:
        return None
    speech20 = sig.speech[::2][:len(sig.bands)]
    sel = ~speech20[a:b]
    if sig.excluded is not None:                      # music under the pauses isn't the room
        sel &= ~sig.excluded[a:b]
    if sel.sum() < max(8, int(float(min_sec) / 0.02)):
        return None
    return sig.bands[a:b][sel]


def background_change(sig: Signal, t: float, s: dict) -> tuple[float, str] | None:
    """Room tone before vs after t: level and spectral shape. (strength, description) or None."""
    w = float(s.get("room_tone_window_sec", 3.0))
    need = float(s.get("min_room_tone_sec", 0.4))
    before, after = _background(sig, t - w, t, need), _background(sig, t, t + w, need)
    if before is None or after is None:
        return None
    level = lambda b: 10 * np.log10(np.sum(10 ** (np.median(b, axis=0) / 10)) + 1e-10)   # noqa: E731
    shape = lambda b: np.median(b, axis=0) - level(b)                                    # noqa: E731
    dl = level(after) - level(before)
    ds = float(np.mean(np.abs(shape(after) - shape(before))))
    lim_l, lim_s = float(s.get("room_tone_db", 6.0)), float(s.get("room_tone_shape_db", 5.0))
    if abs(dl) < lim_l and ds < lim_s:
        return None
    size = max(abs(dl) / lim_l, ds / lim_s)
    return min(1.0, size / 1.5), f"background noise changes by {dl:+.0f} dB (shape {ds:.0f} dB)", size


def loudness_jump(sig: Signal, t: float, s: dict) -> tuple[float, str] | None:
    """Speech level just before vs just after t (a different take or mic setting)."""
    w = float(s.get("loudness_window_sec", 2.5))
    a0, a1, b0, b1 = sig.at(t - w), sig.at(t), sig.at(t), sig.at(t + w)
    before = sig.db[max(0, a0):a1][sig.speech[max(0, a0):a1]]
    after = sig.db[b0:b1][sig.speech[b0:b1]]
    if len(before) < 30 or len(after) < 30:
        return None
    diff = float(np.median(after) - np.median(before))
    lim = float(s.get("loudness_jump_db", 6.0))
    if abs(diff) < lim:
        return None
    return (min(1.0, abs(diff) / (1.6 * lim)), f"speech gets {abs(diff):.0f} dB {'louder' if diff > 0 else 'quieter'}",
            abs(diff) / lim)


def cut_off_words(sig: Signal, words: Sequence[SpokenWord], s: dict) -> list[tuple[float, float, str]]:
    """A short, unsure word whose sound is still going at full strength when it ends."""
    out = []
    for w in words:
        if w.end - w.start > float(s.get("cut_word_max_sec", 0.15)) or w.probability > float(s.get("cut_word_max_prob", 0.5)):
            continue
        a, b = sig.at(w.start), sig.at(w.end)
        if b - a < 2 or b + 2 >= len(sig.db):
            continue
        peak, tail = float(np.max(sig.db[a:b])), float(np.mean(sig.db[max(a, b - 2):b]))
        if tail >= peak - 6 and sig.speech[b - 1]:
            if _stops_dead(sig, w.end, float(s.get("dead_stop_db", 12.0))):
                out.append((w.end, 1.0, f"\"{w.text.strip()}\" is cut off mid-word (it stops dead)"))
            else:
                out.append((w.end, 0.7, f"\"{w.text.strip()}\" is cut off mid-word"))
    return out


def _stops_dead(sig: Signal, t: float, drop_db: float) -> bool:
    """At 5 ms resolution: the level falls by drop_db within ~10 ms of t, from a sound that wasn't
    already fading. Natural word endings, even on a "t" or "p", take longer than that."""
    lo = max(0, int((t - 0.05) * sig.sr))
    piece = sig.samples[lo:int((t + 0.05) * sig.sr)]
    if len(piece) < int(0.06 * sig.sr):
        return False
    db = frame_db(piece, sig.sr, 0.005, 0.005)
    k = int(round((t - lo / sig.sr) / 0.005))
    if k < 4 or k + 3 > len(db):
        return False
    before = db[k - 4:k]
    if before[-1] < before[0] - 3:                    # already fading out
        return False
    return float(before.max() - db[k:k + 3].min()) >= drop_db


def spectral_jumps(sig: Signal, words: Sequence[SpokenWord], s: dict) -> list[tuple[float, float, str]]:
    """Where two words touch (no gap), speech still loud and not fading right up to a point whose
    timbre then changes at once: a splice inside speech. Natural speech glides from one sound to
    the next over tens of ms, or fades before a pause. Measured at 5 ms steps around each such
    boundary, so where the analysis frames happen to fall doesn't matter."""
    out = []
    if sig.cep_mean is None:
        return out
    limit = float(s.get("jump_max_similarity", 0.35))
    hop, step = 0.005, 4                                   # 5 ms frames; compare frames 20 ms apart
    for a, b in zip(words, words[1:]):
        if b.start - a.end > 0.03:
            continue
        lo = max(0.0, a.end - 0.09)
        piece = sig.samples[int(lo * sig.sr):int((a.end + 0.09) * sig.sr)]
        if len(piece) < int(0.1 * sig.sr):
            continue
        c = cepstra(piece, sig.sr, hop, 0.02, mean=sig.cep_mean)
        db = frame_db(piece, sig.sr, hop, 0.01)
        n = min(len(c), len(db))
        if n < 2 * step + 8:
            continue
        u = c[:n] / (np.linalg.norm(c[:n], axis=1, keepdims=True) + 1e-9)
        sims = np.einsum("ij,ij->i", u[:-step], u[step:])          # frame i vs i+step (centred at i+step/2)
        mid = int(round((a.end - lo) / hop))
        window = range(max(step, mid - 10), min(len(sims) - step, mid + 10))
        if not window:
            continue
        k = min(window, key=lambda i: sims[i])
        point = k + step // 2                                       # the frame at the change
        level = float(np.median(sig.db[max(0, sig.at(a.end) - 20):sig.at(a.end)]))
        before = db[max(0, point - 4):point]                        # the 20 ms before it
        after = db[point + 1:point + 9]
        in_flow = len(before) == 4 and before.min() >= level - 8 and before[-1] >= before[0] - 2
        if sims[k] < limit and in_flow and len(after) and after.max() >= level - 8:
            out.append((round(lo + point * hop, 3), float(min(1.0, (limit - sims[k]) / limit + 0.5)),
                        "the sound changes abruptly with no transition"))
    return out


def missing_pauses(words: Sequence[SpokenWord], s: dict) -> list[tuple[float, float, str]]:
    """A sentence ends and the next starts with (almost) no gap: a breath or pause was cut out."""
    out = []
    min_gap = float(s.get("min_sentence_gap_sec", 0.08))
    for a, b in zip(words, words[1:]):
        if a.text.strip().endswith((".", "?", "!")) and b.start - a.end < min_gap:
            out.append((b.start, 0.6, "no pause between sentences (a breath may have been cut)"))
    return out


# Evidence that, on its own, can make a cut. Room tone, loudness and timbre changes are weak: in a
# mixed, multi-mic podcast they change at every turn, so they only add to a strong signal.
STRONG_EVIDENCE = ("click", "stops with no fade", "digital silence", "it stops dead", "no pause between")
AT_TURN_IGNORED = ("no pause between", "the sound changes abruptly", "background noise", "speech gets")


def _strong(description: str) -> bool:
    return any(k in description for k in STRONG_EVIDENCE)


def _enough(group) -> bool:
    """A cut needs a strong signal, or a cut-off word that another signal (not room tone or
    level) agrees with: a timbre jump or a missing pause at the same point."""
    if any(_strong(x[2]) for x in group):
        return True
    cut_word = any("cut off mid-word" in x[2] for x in group)
    other = any(("sound changes abruptly" in x[2] or "no pause between" in x[2]) for x in group)
    return cut_word and other


def cut_points(sig: Signal, words: Sequence[SpokenWord], s: dict, regions=None, weak_rules: bool = True) -> list[Finding]:
    """Candidate points with every signal found there, combined into one score per point.
    weak_rules: room tone / loudness / timbre changes count only next to a strong signal, and at a
    change of speaker only a click, a dead stop or a cut-off word counts."""
    signals: list[tuple[float, float, str, str]] = []          # (time, strength, description, family)
    w_click, w_edge = float(s.get("weight_click", 0.6)), float(s.get("weight_edge", 0.7))
    for t, strength in clicks(sig, s):
        signals.append((t, w_click * strength, "a click (the waveform jumps)", "abrupt"))
    for t, strength, kind in abrupt_edges(sig, s):
        signals.append((t, w_edge * strength, kind, "abrupt"))
    for t, strength, text in cut_off_words(sig, words, s):
        signals.append((t, float(s.get("weight_cut_word", 0.5)) * strength / 0.7, text, "abrupt"))
    for t, strength, text in spectral_jumps(sig, words, s):
        signals.append((t, float(s.get("weight_spectral_jump", 0.4)) * strength, text, "abrupt"))
    for t, strength, text in missing_pauses(words, s):
        signals.append((t, float(s.get("weight_short_gap", 0.35)) * strength / 0.6, text, "jump"))
    # Background and loudness are compared at pauses (where an edit usually is) and at the points above.
    points = [t for t, *_ in signals]
    for a, b in zip(words, words[1:]):
        if b.start - a.end >= float(s.get("pause_min_sec", 0.15)):
            points.append((a.end + b.start) / 2)
    # A change of room or level shows at every point near it (the windows overlap), so only the
    # point where it is biggest within the window counts.
    window = float(s.get("room_tone_window_sec", 3.0))
    grid = [round(0.5 * k, 2) for k in range(1, int(sig.duration / 0.5))]       # the room changes anywhere
    for measure, weight, where in ((background_change, float(s.get("weight_room_tone", 0.75)), points + grid),
                                   (loudness_jump, float(s.get("weight_loudness", 0.6)), points)):
        found = [(t, measure(sig, t, s)) for t in sorted(set(round(p, 2) for p in where))]
        found = [(t, r) for t, r in found if r]
        for t, (strength, text, size) in found:          # ranked by the uncapped size of the change
            if any(abs(t2 - t) < window and (r2[2] > size or (r2[2] == size and t2 < t))
                   for t2, r2 in found if t2 != t):
                continue
            signals.append((t, weight * strength, text, "jump"))

    signals.sort()
    groups: list[list[tuple[float, float, str, str]]] = []
    for sgl in signals:
        if groups and sgl[0] - groups[-1][-1][0] <= float(s.get("cut_merge_sec", 0.3)):
            groups[-1].append(sgl)
        else:
            groups.append([sgl])
    out = []
    threshold = float(s.get("cut_score", 0.6))
    for g in groups:
        if weak_rules and regions is not None and regions.speaker_change_near(g[0][0]):
            g = [x for x in g if not any(k in x[2] for k in AT_TURN_IGNORED)]      # a turn, not an edit
        if weak_rules and not _enough(g):
            continue
        if not g:
            continue
        best: dict[str, tuple[float, float, str, str]] = {}
        for sgl in g:                                   # one of each kind of evidence counts once
            key = sgl[2].split(" by ")[0].split(":")[0][:30]
            if key not in best or sgl[1] > best[key][1]:
                best[key] = sgl
        score = 1.0 - float(np.prod([1.0 - min(0.99, x[1]) for x in best.values()]))
        if score < threshold:
            continue
        kinds = {x[3] for x in best.values()}
        t = max(best.values(), key=lambda x: x[1])[0]
        kind = ABRUPT_CUT if "abrupt" in kinds else JUMP_CUT
        what = "; ".join(x[2] for x in sorted(best.values(), key=lambda x: -x[1]))
        message = (f"Abrupt cut: {what}." if kind == ABRUPT_CUT else f"Jump cut: {what}.")
        suggestion = ("Smooth the edit: a short crossfade, or recut so the word isn't clipped." if kind == ABRUPT_CUT
                      else "Match the takes: room tone under the edit, level matched, a natural pause kept.")
        out.append(Finding(kind, t, t, round(min(0.95, score), 2), message, suggestion,
                           [f"{x[2]} ({x[1]:.2f})" for x in sorted(best.values(), key=lambda x: -x[1])]))
    return out


# --- silence and fillers -------------------------------------------------------------------------------

def silences_in(samples: np.ndarray, sr: int, noise_db: float = -45.0, min_sec: float = 2.0,
                floor: np.ndarray | None = None, above_floor_db: float = 8.0) -> list[tuple[float, float]]:
    """Stretches at least min_sec long where every 20 ms frame is quiet: below noise_db, or (with
    the local noise floor, 10 ms frames) less than above_floor_db over it. Judged on the level of
    each frame, not of each sample (ffmpeg's silencedetect), so room tone hiss counts as silence."""
    db = frame_db(np.asarray(samples, np.float32), sr, 0.02, 0.02)
    quiet = db < noise_db
    if floor is not None and len(floor):
        f20 = floor[::2][:len(db)]
        f20 = np.pad(f20, (0, max(0, len(db) - len(f20))), mode="edge")
        quiet |= db < f20 + above_floor_db
    out, start = [], None
    for i, q in enumerate(np.append(quiet, False)):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * 0.02 >= min_sec:
                out.append((round(start * 0.02, 3), round(i * 0.02, 3)))
            start = None
    return out


def dead_air(silent: Sequence[tuple[float, float | None]], duration: float, s: dict) -> list[Finding]:
    out = []
    limit = float(s.get("dead_air_sec", 2.5))
    for start, end in silent:
        end = duration if end is None else end
        length = end - start
        if length < limit:
            continue
        where = " at the start" if start <= 0.05 else " at the end" if end >= duration - 0.05 else ""
        out.append(Finding(DEAD_AIR, start, end, 0.9 if length >= 2 * limit else 0.75,
                           f"Dead air{where}: {length:.1f} s of silence (limit {limit:g} s).",
                           "Shorten the pause.", [f"silence {length:.1f} s"]))
    return out


def filler_words(words: Sequence[SpokenWord], s: dict) -> tuple[list[Finding], dict[str, int]]:
    fillers = {norm(f) for f in s.get("fillers") or []}
    counts: dict[str, int] = {}
    out = []
    for w in words:
        t = norm(w.text).rstrip("-")
        if t in fillers:
            counts[t] = counts.get(t, 0) + 1
            out.append(Finding(FILLER, w.start, w.end, 0.6, f"Filler word \"{w.text.strip()}\".",
                               "Cut it if the client wants fillers removed.", ["transcript: filler"]))
    return out, counts


# --- everything together ---------------------------------------------------------------------------------

SEVERITY = {STUTTER: "medium", REPEATED_PHRASE: "medium", ABRUPT_CUT: "medium", JUMP_CUT: "medium",
            DEAD_AIR: "medium", FILLER: "low", MISSING_CUE: "high"}


def merge_findings(found: list[Finding], within: float = 0.5) -> list[Finding]:
    """Stutter findings from the transcript and from the sound at the same moment: one, surer."""
    found = sorted(found, key=lambda f: (f.start, -f.confidence))
    out: list[Finding] = []
    for f in found:
        twin = next((g for g in out if g.kind in (STUTTER, REPEATED_PHRASE) and f.kind in (STUTTER, REPEATED_PHRASE)
                     and f.start < g.end + within and f.end > g.start - within), None)
        if twin is not None and ("acoustic" in " ".join(f.evidence)) != ("acoustic" in " ".join(twin.evidence)):
            if "acoustic" in " ".join(twin.evidence):      # the transcript's words and type say more
                twin.kind, twin.message, twin.suggestion = f.kind, f.message, f.suggestion
            twin.confidence = round(min(0.97, max(twin.confidence, f.confidence) + 0.1), 2)
            twin.evidence += f.evidence
            twin.start, twin.end = min(twin.start, f.start), max(twin.end, f.end)
            continue
        out.append(f)
    return out


def to_issues(found: list[Finding], words: Sequence[SpokenWord]) -> list[Issue]:
    issues = []
    for n, f in enumerate(sorted(found, key=lambda f: f.start), start=1):
        severity = SEVERITY.get(f.kind, "medium")
        if f.kind in (ABRUPT_CUT, REPEATED_PHRASE) and f.confidence >= 0.85:
            severity = "high"
        if f.confidence < 0.5:
            severity = "low"
        original = context_text(words, f.start, f.end) or f"({format_timestamp(f.start)})"
        issues.append(Issue(caption_id=audio_issue_id(n), timestamp=round(f.start, 3), type=f.kind, severity=severity,
                            original=original, message=f.message, suggestion=f.suggestion,
                            confidence=float(min(1.0, max(0.0, f.confidence))), signals="; ".join(f.evidence)))
    return issues


OVERLAP_KINDS = ("overlap", "overlap_ok")
INTENTIONAL_KINDS = ("music", "sfx", "cue")


def suppress(found: list[Finding], regions, sig: Signal, f0, s: dict) -> tuple[list[Finding], dict]:
    """Drop findings the regions explain. Repeats: not in crosstalk, not over music or effects, not
    when the voice changes between the two occurrences. Cuts and dead air: not inside music,
    effects or cues, or within the margin around them. Counts what was hidden, and why."""
    from src.audio.regions import different_voice

    kept, hidden = [], {}

    def hide(f: Finding, why: str, region=None) -> None:
        hidden[why] = hidden.get(why, 0) + 1
        if region is not None:
            region.suppressed += 1

    for f in found:
        if f.kind in (STUTTER, REPEATED_PHRASE, FILLER):
            r = regions.hit(f.start, f.end, OVERLAP_KINDS)
            if r is not None and f.kind != FILLER:
                hide(f, "repeat in overlapping speech", r)
                continue
            r = regions.hit(f.start, f.end, INTENTIONAL_KINDS)
            if r is not None:
                hide(f, f"{f.kind} over music or an effect", r)
                continue
            if f.pair is not None and different_voice(sig, f0, f.pair[0], f.pair[1], s):
                hide(f, "repeat by a different voice (an echo, not a stutter)")
                continue
        elif f.kind in (ABRUPT_CUT, JUMP_CUT, DEAD_AIR):
            r = regions.hit(f.start, f.end, INTENTIONAL_KINDS)
            if r is not None:
                hide(f, f"{f.kind.replace('_', ' ')} at music, an effect or a cue", r)
                continue
        kept.append(f)
    return kept, hidden


def _excluded_frames(sig: Signal, regions) -> np.ndarray:
    mask = np.zeros(len(sig.bands), bool)
    for r in regions.items:
        if r.kind in INTENTIONAL_KINDS:
            mask[max(0, int(r.start / 0.02)):int(r.end / 0.02) + 1] = True
    return mask


def check_audio(samples: np.ndarray, sr: int, words: Sequence[SpokenWord], settings: dict,
                silent: Sequence[tuple[float, float | None]] | None = None,
                backup_words=None, segments=None, guide: dict | None = None, cue_sheet=(),
                references=()) -> tuple[list[Issue], dict]:
    """Every audio check the settings switch on. Returns the issues and counts for the report.
    silent: the file's silences (None = found from the samples). backup_words: a callable giving
    a second transcript (Whisper without the disfluency prompt), asked for only when the first one
    skipped a long stretch of speech. segments: Whisper's (start, end, no_speech_prob). guide: the
    client's audio guidelines. cue_sheet: the episode's cue sheet regions. references: the
    client's reference cues as (name, samples at sr).
    With "suppression: false" the regions are still found and listed, but nothing is hidden and
    the old cut rules apply (for measuring what the suppression changes)."""
    from src.audio.regions import drop_lyrics, find_regions, pitch_track, required_cues

    s = effective_settings(settings)
    use = bool(s.get("suppression", True))
    words = [w for w in words if w.text.strip()]
    sig = analyze(samples, sr, s)
    gaps = untranscribed_speech(sig, words, float(s.get("transcript_gap_sec", 5.0)))
    stats_gaps = len(gaps)
    if gaps and backup_words is not None:
        words = fill_gaps(words, [w for w in backup_words() if w.text.strip()], gaps)
    f0 = pitch_track(sig.samples, sr)
    regions = find_regions(sig, f0, words, segments, s, guide or {}, cue_sheet, references)
    dropped = 0
    if use:
        words, dropped = drop_lyrics(words, regions, s)
        sig.excluded = _excluded_frames(sig, regions)
    found: list[Finding] = []
    stats: dict = {"duration": round(sig.duration, 1), "words": len(words), "transcript_gaps": stats_gaps,
                   "suppression": use, "lyrics_dropped": dropped}
    if check_on(s, "stutter") or check_on(s, "repeated_phrase"):
        rep = [f for f in transcript_repeats(words, s) if speech_share(sig, f.start, f.end) >= 0.3]
        found += [f for f in rep if check_on(s, "stutter" if f.kind == STUTTER else "repeated_phrase")]
    if check_on(s, "stutter"):
        found += acoustic_repeats(sig, words, s)
    if check_on(s, "repeated_take"):
        found += repeated_takes(sig, words, s)
    if check_on(s, "abrupt_cut") or check_on(s, "jump_cut"):
        found += [f for f in cut_points(sig, words, s, regions, weak_rules=use) if check_on(s, f.kind)]
    if check_on(s, "dead_air"):
        if silent is None:
            silent = silences_in(samples, sr, float(s.get("silence_noise_db", -60.0)), float(s.get("dead_air_sec", 2.5)),
                                 sig.floor, float(s.get("silence_above_floor_db", 8.0)))
        found += dead_air(silent, sig.duration, s)
    fill, counts = filler_words(words, s)
    stats["fillers"] = counts
    if check_on(s, "fillers"):
        found += fill
    found = merge_findings(found)
    hidden: dict = {}
    if use:
        found, hidden = suppress(found, regions, sig, f0, s)
    for at, message in required_cues(regions, guide or {}, sig.duration):
        found.append(Finding(MISSING_CUE, at, at, 0.8, message, "Add the cue as the guidelines require.",
                             ["audio guidelines: required cue not matched"]))
    min_conf = float(s.get("min_confidence", 0.0))
    found = [f for f in found if f.confidence >= min_conf]
    stats["hidden"] = hidden
    stats["regions"] = [r.as_dict() for r in regions.items]
    stats["speaker_changes"] = len(regions.speaker_changes)
    return to_issues(found, words), stats

"""Parts of a podcast the audio-edit checks must leave alone, and the speaker changes.

E2CL episodes are one mixed file: several people, sometimes talking over each other, on different
mics or in different rooms, with music and sound effects placed on purpose. None of that is a
mistake. This module finds:

  overlap zones      likely crosstalk: Whisper word times that collide, runs of unsure words,
                     garbled or very short words, a jump in energy with a flatter (wider) spectrum
  music regions      intros, outros, beds under speech, transitions: energy without confident
                     speech (or Whisper's no-speech probability high while not silent), a steady
                     or tonal spectrum, or a regular beat
  sfx regions        short loud hits with a fast decay where nobody is speaking
  cue regions        the client's reference audio (stingers, jingles, beds) found in the episode
  cue sheet entries  start,end,type,note rows for the episode (music | sfx | overlap_ok); they always win
  speaker changes    a light voice comparison (pitch and timbre, numpy only): used only to stop
                     calling an echo by another person a stutter, and to discount level / room tone
                     differences at a turn change

Nothing here is reported as a problem; the report lists every region so a person can spot-check
what the bot ignored (src/report). The one optional check, "required cues present", is off unless
the client's audio guidelines switch it on.

All of it is heuristic and was tuned on synthetic audio only (see the plan file).
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from src.models import SpokenWord

GUIDELINE_MARGINS = {"strict": 0.15, "normal": 0.3, "lenient": 0.6}      # seconds around an intentional region
INTENTIONAL = ("music", "sfx", "cue", "cue_music", "cue_sfx")


@dataclass
class Region:
    start: float
    end: float
    kind: str               # overlap | music | sfx | cue | overlap_ok (cue sheet) | music/sfx from a cue sheet
    source: str             # "detected", "cue sheet", "reference: intro_stinger.wav"
    why: str
    suppressed: int = 0     # flags this region hid

    @property
    def intentional(self) -> bool:
        return self.kind in ("music", "sfx", "cue")

    def touches(self, start: float, end: float, margin: float = 0.0) -> bool:
        return start < self.end + margin and end > self.start - margin

    def as_dict(self) -> dict:
        return {"start": round(float(self.start), 2), "end": round(float(self.end), 2), "kind": self.kind,
                "source": self.source, "why": self.why, "suppressed": int(self.suppressed)}


@dataclass
class Regions:
    items: list[Region] = field(default_factory=list)
    margin: float = 0.3
    speaker_changes: list[float] = field(default_factory=list)

    def of(self, *kinds: str) -> list[Region]:
        return [r for r in self.items if r.kind in kinds]

    def hit(self, start: float, end: float, kinds: Sequence[str], margin: float | None = None) -> Region | None:
        """The first region of these kinds touching [start, end] (widened by margin for intentional ones)."""
        for r in self.items:
            if r.kind in kinds:
                m = (self.margin if margin is None else margin) if r.intentional else 0.0
                if r.touches(start, end, m):
                    return r
        return None

    def speaker_change_near(self, t: float, within: float = 0.5) -> bool:
        return any(abs(c - t) <= within for c in self.speaker_changes)


# --- voices -------------------------------------------------------------------------------------

def pitch_track(x: np.ndarray, sr: int, hop: float = 0.01, win: float = 0.04, fmin: float = 60.0,
                fmax: float = 400.0, chunk: int = 20000) -> np.ndarray:
    """f0 in Hz per 10 ms frame (0 where unvoiced), from the normalized autocorrelation's best peak
    between fmin and fmax. numpy only; fast enough for an hour of audio."""
    from src.audio.features import frames

    f = frames(np.asarray(x, np.float32), sr, hop, win)
    n = f.shape[1]
    n_fft = 1 << int(np.ceil(np.log2(2 * n)))
    lo, hi = int(sr / fmax), min(n - 1, int(sr / fmin))
    out = np.zeros(len(f), np.float32)
    window = np.hanning(n).astype(np.float32)
    for start in range(0, len(f), chunk):
        block = f[start:start + chunk] * window
        block = block - block.mean(axis=1, keepdims=True)
        spec = np.fft.rfft(block, n=n_fft, axis=1)
        ac = np.fft.irfft(np.abs(spec) ** 2, n=n_fft, axis=1)[:, :n]
        energy = ac[:, :1] + 1e-12
        norm = ac[:, lo:hi] / energy
        k = np.argmax(norm, axis=1)
        clarity = norm[np.arange(len(norm)), k]
        f0 = sr / (k + lo)
        out[start:start + len(block)] = np.where(clarity >= 0.45, f0, 0.0)
    return out


def median_pitch(f0: np.ndarray, start: float, end: float, hop: float = 0.01, min_frames: int = 3) -> float | None:
    seg = f0[max(0, int(start / hop)):max(0, int(end / hop))]
    voiced = seg[seg > 0]
    return float(np.median(voiced)) if len(voiced) >= min_frames else None


def semitones(a: float, b: float) -> float:
    return abs(12.0 * np.log2(a / b))


def different_voice(sig, f0: np.ndarray, a: SpokenWord, b: SpokenWord, s: dict) -> bool:
    """Whether two words (two occurrences of the same word) sound like two different people: the
    pitch differs by more than voice_pitch_semitones, or the same word sounds clearly unlike
    itself (timbre). Only ever used to NOT call something a stutter."""
    from src.audio.features import sequence_similarity

    pa, pb = median_pitch(f0, a.start, a.end), median_pitch(f0, b.start, b.end)
    if pa and pb and semitones(pa, pb) > float(s.get("voice_pitch_semitones", 3.5)):
        return True
    ca, cb = sig.cep[sig.at(a.start):sig.at(a.end)], sig.cep[sig.at(b.start):sig.at(b.end)]
    if len(ca) >= 5 and len(cb) >= 5:
        return sequence_similarity(ca, cb) < float(s.get("voice_min_similarity", 0.35))
    return False


def speaker_changes(f0: np.ndarray, words: Sequence[SpokenWord], s: dict) -> list[float]:
    """Word boundaries where the typical pitch of the ~1.2 s before and the ~1.2 s after (at least
    0.3 s of voiced sound each) differ by more than turn_pitch_semitones: a likely change of
    speaker. Intonation inside one person's speech moves the median far less than that."""
    thr = float(s.get("turn_pitch_semitones", 5.0))
    win = float(s.get("turn_window_sec", 1.2))
    need = max(10, int(win * 20))                          # voiced frames on each side (a quarter of the window)
    scored: list[tuple[float, float]] = []
    for a, b in zip(words, words[1:]):
        before = median_pitch(f0, a.end - win, a.end, min_frames=need)
        after = median_pitch(f0, b.start, b.start + win, min_frames=need)
        if before and after:
            d = semitones(before, after)
            if d > thr:
                scored.append((round((a.end + b.start) / 2, 3), d))
    # Every boundary near a turn passes; the turn is where the difference is largest (within 1 s).
    return [t for t, d in scored if not any(abs(t2 - t) <= 1.0 and (d2 > d or (d2 == d and t2 < t))
                                             for t2, d2 in scored if t2 != t)]


# --- overlap ------------------------------------------------------------------------------------------

def _garbled(w: SpokenWord) -> bool:
    text = w.text.strip()
    return not re.search(r"[A-Za-z]", text) or (w.end - w.start) < 0.05 or text.startswith("-")


def overlap_zones(sig, words: Sequence[SpokenWord], s: dict) -> list[Region]:
    """Likely crosstalk. A zone needs at least two kinds of sign within ~1.5 s, so one unsure word
    never hides a real stutter."""
    signs: list[tuple[float, float, str]] = []
    for a, b in zip(words, words[1:]):
        if b.start < a.end - float(s.get("overlap_collision_sec", 0.08)):
            signs.append((b.start, a.end, "word times collide"))
    low = float(s.get("overlap_low_prob", 0.45))
    run: list[SpokenWord] = []
    for w in list(words) + [SpokenWord("x", 1e9, 1e9, 1.0)]:
        if w.probability < low:
            run.append(w)
            continue
        if len(run) >= int(s.get("overlap_low_prob_run", 3)):
            signs.append((run[0].start, run[-1].end, f"{len(run)} unsure words in a row"))
        run = []
    garbled = [w for w in words if _garbled(w)]
    for a, b in zip(garbled, garbled[1:]):
        if b.start - a.end <= 1.5:
            signs.append((a.start, b.end, "garbled or very short words"))
    # A jump in energy together with a flatter spectrum: two voices at once.
    if len(sig.flatness):
        level = sig.db[::2][:len(sig.flatness)]
        speechy = sig.speech[::2][:len(sig.flatness)]
        if speechy.sum() > 50:
            loud = np.median(level[speechy]) + float(s.get("overlap_energy_rise_db", 6.0))
            flat = np.percentile(sig.flatness[speechy], 90)
            hot = speechy & (level > loud) & (sig.flatness >= flat)
            for a, b in _runs(hot, 0.02, min_len=0.3):
                signs.append((a, b, "louder, with a wider spectrum"))
    signs.sort()
    zones: list[Region] = []
    pad = 0.25
    for a, b, why in signs:
        if zones and a <= zones[-1].end + 1.5:
            z = zones[-1]
            z.end = max(z.end, b + pad)
            if why not in z.why:
                z.why += f"; {why}"
        else:
            zones.append(Region(max(0.0, a - pad), b + pad, "overlap", "detected", why))
    return [z for z in zones if ";" in z.why]          # two kinds of sign at least


def _runs(mask: np.ndarray, hop: float, min_len: float = 0.0, max_gap: float = 0.0) -> list[tuple[float, float]]:
    out, start, last = [], None, None
    for i, m in enumerate(mask):
        if m:
            if start is None:
                start = i
            last = i
        elif start is not None and (i - last) * hop > max_gap:
            if (last + 1 - start) * hop >= min_len:
                out.append((round(start * hop, 3), round((last + 1) * hop, 3)))
            start = None
    if start is not None and (last + 1 - start) * hop >= min_len:      # a run that reaches the end
        out.append((round(start * hop, 3), round((last + 1) * hop, 3)))
    return out


# --- music and effects ------------------------------------------------------------------------------------

def _word_cover(words: Sequence[SpokenWord], n: int, hop: float, min_prob: float) -> np.ndarray:
    cover = np.zeros(n, bool)
    for w in words:
        if w.probability >= min_prob:
            cover[int(w.start / hop):int(w.end / hop) + 1] = True
    return cover


def beat_strength(db: np.ndarray, hop: float = 0.01) -> float:
    """How regular the onsets are: the best normalized autocorrelation of the onset envelope at a
    lag of 0.3-1.0 s (60-200 beats a minute). Music with a beat ~0.5+, speech ~0.2."""
    onset = np.maximum(np.diff(db), 0.0)
    if len(onset) < int(2.5 / hop):
        return 0.0
    onset = onset - onset.mean()
    ac = np.correlate(onset, onset, mode="full")[len(onset) - 1:]
    if ac[0] <= 0:
        return 0.0
    ac = ac / ac[0]
    lo, hi = int(0.3 / hop), min(len(ac) - 1, int(1.0 / hop))
    return float(np.max(ac[lo:hi])) if hi > lo else 0.0


def music_regions(sig, words: Sequence[SpokenWord], segments, s: dict, guide: dict) -> list[Region]:
    """Music (intro, outro, transitions, beds under speech), in 0.5 s blocks:
      - energetic and not covered by confident words, or inside a Whisper segment it marks as
        probably no speech while it isn't silent, and
      - steady (few syllable-like dips) or tonal (a peaky spectrum) or with a regular beat.
    A bed under speech shows in the pauses: the background there is well above the file's room
    tone and tonal or beating."""
    lenient = {"strict": 0.85, "normal": 1.0, "lenient": 1.2}.get(str(guide.get("sensitivity", "normal")), 1.0)
    block, hop = 0.5, 0.01
    n_blocks = int(sig.duration / block)
    if n_blocks < 2:
        return []
    cover = _word_cover(words, len(sig.db), hop, float(s.get("music_word_min_prob", 0.6)))
    no_speech = np.zeros(len(sig.db), bool)
    for start, end, p in segments or []:
        if p >= float(s.get("music_no_speech_prob", 0.6)):
            no_speech[int(start / hop):int(end / hop) + 1] = True
    global_floor = float(np.percentile(sig.db, 5))
    flat20 = sig.flatness
    is_music = np.zeros(n_blocks, bool)
    reasons: list[str] = [""] * n_blocks
    for k in range(n_blocks):
        a, b = int(k * block / hop), int((k + 1) * block / hop)
        db = sig.db[a:b]
        energetic = np.median(db) > global_floor + float(s.get("music_min_above_floor_db", 15.0))
        unspoken = cover[a:b].mean() < 0.2 or no_speech[a:b].mean() > 0.5
        if not (energetic and unspoken):
            continue
        dips = np.mean(db < np.max(db) - 15.0)                     # speech dips between syllables
        fa, fb = a // 2, b // 2
        tonal = len(flat20) > fb and np.median(flat20[fa:fb]) < float(s.get("music_tonal_flatness", 0.12)) * lenient
        lo, hi = max(0, a - 150), min(len(sig.db), b + 150)
        beat = beat_strength(sig.db[lo:hi]) >= float(s.get("music_beat_strength", 0.45)) / lenient
        steady = dips < float(s.get("music_max_dips", 0.15)) * lenient
        if steady or tonal or beat:
            is_music[k] = True
            reasons[k] = ", ".join(n for n, v in (("steady", steady), ("tonal", tonal), ("a regular beat", beat)) if v)

    # Beds under speech show in the pauses between words: a pause well above the room tone that is
    # tonal or sits in a regular beat. A bed is a chain of at least three such pauses (each within
    # 4 s of the next); one musical pause is a stinger or an effect, not a bed.
    musical_gaps = []
    spoken_any = _word_cover(words, len(sig.db), hop, 0.0)
    trim = 0.02                                            # Whisper's word ends are loose: keep off them
    for a, b in _runs(~spoken_any, hop, min_len=float(s.get("bed_min_pause_sec", 0.09))):
        a, b = a + trim, b - trim
        ia, ib = int(a / hop), int(b / hop)
        if ib - ia < 5 or ib - ia > int(3.0 / hop):
            continue                                       # too short, or a long stretch judged as music above
        if np.median(sig.db[ia:ib]) < global_floor + float(s.get("bed_min_above_floor_db", 12.0)):
            continue
        fa, fb = ia // 2, max(ia // 2 + 1, ib // 2)
        tonal = len(flat20) >= fb and np.median(flat20[fa:fb]) < float(s.get("music_tonal_flatness", 0.12)) * lenient
        lo, hi = max(0, ia - 150), min(len(sig.db), ib + 150)
        beat = beat_strength(sig.db[lo:hi]) >= float(s.get("music_beat_strength", 0.45)) / lenient
        if tonal or beat:
            band = sig.bands[fa:fb].mean(axis=0) if fb > fa else None
            level = float(np.median(sig.db[ia:ib]))
            musical_gaps.append((a, b, band, level))
    # A bed sounds the same in every pause; another voice bleeding into the pauses doesn't. Pauses
    # chain only when their background has the same spectral shape and about the same level.
    shape_db = float(s.get("bed_same_shape_db", 4.0)) * lenient
    level_db = float(s.get("bed_same_level_db", 6.0)) * lenient

    def same_bed(g, h) -> bool:
        if g[2] is None or h[2] is None:
            return False
        sa, sb = g[2] - g[2].mean(), h[2] - h[2].mean()
        return float(np.mean(np.abs(sa - sb))) <= shape_db and abs(g[3] - h[3]) <= level_db

    beds: list[tuple[float, float]] = []
    chain: list = []
    for g in musical_gaps + [(1e9, 1e9, None, 0.0)]:
        if chain and (g[0] - chain[-1][1] > 4.0 or not same_bed(chain[-1], g)):
            if len(chain) >= int(s.get("bed_min_pauses", 3)):
                beds.append((max(0.0, chain[0][0] - 0.5), chain[-1][1] + 0.5))
            chain = []
        chain.append(g)
    bed = np.zeros(n_blocks, bool)
    regions = []
    for a, b in _runs(is_music, block, min_len=float(s.get("music_min_sec", 1.5)), max_gap=0.5):
        ka, kb = int(a / block), int(b / block)
        why = [r for r in reasons[ka:kb] if r]
        regions.append(Region(a, b, "music", "detected",
                              "music (energy without confident speech: " + (why[0] if why else "steady") + ")"))
    for a, b in beds:
        regions.append(Region(round(a, 2), round(b, 2), "music", "detected",
                              "music bed under speech (musical pauses between words)"))
    return regions


def sfx_regions(sig, words: Sequence[SpokenWord], s: dict) -> list[Region]:
    """Short loud hits with a fast decay (impacts, whooshes, stingers) where no confident word starts."""
    db, out = sig.db, []
    rise, decay = float(s.get("sfx_rise_db", 15.0)), float(s.get("sfx_decay_db", 15.0))
    loud = np.median(db[sig.speech]) if sig.speech.any() else -30.0
    spoken = _word_cover(words, len(db), 0.01, 0.0)          # any word: a syllable onset looks like a hit
    i = 3
    while i < len(db) - 40:
        if db[i] >= loud and db[i] - db[i - 3] >= rise:
            tail = db[i:i + 40]
            k = np.nonzero(tail <= db[i] - decay)[0]
            t = i * 0.01
            if len(k) and not spoken[max(0, i - 5):i + 20].any():
                end = (i + int(k[0])) * 0.01
                out.append(Region(max(0.0, t - 0.05), end + 0.1, "sfx", "detected",
                                  f"a {db[i] - db[i - 3]:.0f} dB hit that decays in {end - t:.2f} s"))
                i += int(k[0]) + 10
                continue
        i += 1
    return out


# --- the client's reference audio and the episode's cue sheet ----------------------------------------

FP_HOP = 0.05


def fingerprint(x: np.ndarray, sr: int) -> np.ndarray:
    """(frames, 16) log band energies every 50 ms, each band z-normalized over time."""
    from src.audio.features import band_db

    edges = tuple(int(f) for f in np.geomspace(80, min(7000, sr / 2 - 1), 17)[:-1])
    bands = band_db(np.asarray(x, np.float32), sr, FP_HOP, 0.1, edges)
    return bands


def match_reference(episode_fp: np.ndarray, ref_fp: np.ndarray, threshold: float) -> list[tuple[float, float]]:
    """Where the reference appears: (start s, score) of each match with a mean normalized
    cross-correlation over the bands of at least `threshold` (non-overlapping, best first)."""
    m, n = len(ref_fp), len(episode_fp)
    if m < 4 or n < m:
        return []
    scores = np.zeros(n - m + 1)
    for band in range(ref_fp.shape[1]):
        r = ref_fp[:, band] - ref_fp[:, band].mean()
        rn = np.sqrt(np.sum(r ** 2))
        if rn < 1e-6:
            continue
        e = episode_fp[:, band].astype(np.float64)
        num = np.correlate(e, r, mode="valid")
        s1 = np.concatenate([[0], np.cumsum(e)])
        s2 = np.concatenate([[0], np.cumsum(e ** 2)])
        win_sum, win_sq = s1[m:] - s1[:-m], s2[m:] - s2[:-m]
        var = np.maximum(win_sq - win_sum ** 2 / m, 1e-9)
        scores += num / (np.sqrt(var) * rn)
    scores /= ref_fp.shape[1]
    out = []
    order = np.argsort(scores)[::-1]
    taken = np.zeros(len(scores), bool)
    for k in order:
        if scores[k] < threshold:
            break
        if taken[k]:
            continue
        taken[max(0, k - m):k + m] = True
        out.append((round(k * FP_HOP, 2), float(scores[k])))
    return out


def reference_files(folder: Path | None) -> list[Path]:
    from src.audio.media import AUDIO_EXTENSIONS

    if folder is None or not Path(folder).is_dir():
        return []
    return sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS)


def cue_regions(samples: np.ndarray, sr: int, references: Sequence[tuple[str, np.ndarray]], s: dict) -> list[Region]:
    """The client's reference cues (already decoded at sr) found in the episode."""
    if not references:
        return []
    ep = fingerprint(samples, sr)
    out = []
    for name, ref in references:
        rf = fingerprint(ref, sr)
        dur = len(ref) / sr
        for start, score in match_reference(ep, rf, float(s.get("cue_match_threshold", 0.6))):
            out.append(Region(start, start + dur, "cue", f"reference: {name}", f"matches the client's {name} "
                                                                                  f"(score {score:.2f})"))
    return out


CUE_TYPES = {"music": "music", "sfx": "sfx", "overlap_ok": "overlap_ok"}


def cue_sheet_file(episode: Path) -> Path:
    """Where an episode's cue sheet is kept: next to it, <episode name>.cues.csv."""
    episode = Path(episode)
    return episode.with_name(episode.stem + ".cues.csv")


def find_cue_sheet(episode: Path, fallback_dir: Path | None = None) -> Path | None:
    """The episode's cue sheet: next to it, or (when that folder wasn't writable) in fallback_dir."""
    for candidate in [cue_sheet_file(episode)] + ([Path(fallback_dir) / cue_sheet_file(episode).name]
                                                   if fallback_dir else []):
        if candidate.is_file():
            return candidate
    return None


def attach_cue_sheet(episode: Path, csv_file: Path, fallback_dir: Path | None = None) -> tuple[Path, list[str]]:
    """Remember a cue sheet for an episode: checked, then copied next to it as <episode>.cues.csv
    (or into fallback_dir if the episode's folder can't be written). Returns where, and any
    problems with its lines (a sheet with no usable line raises ValueError)."""
    import shutil

    regions, problems = read_cue_sheet(csv_file)
    if not regions:
        raise ValueError("; ".join(problems) or f"{Path(csv_file).name} has no cue lines")
    target = cue_sheet_file(episode)
    try:
        if Path(csv_file).resolve() != target.resolve():
            shutil.copyfile(csv_file, target)
        return target, problems
    except OSError:
        if fallback_dir is None:
            raise
        Path(fallback_dir).mkdir(parents=True, exist_ok=True)
        target = Path(fallback_dir) / target.name
        shutil.copyfile(csv_file, target)
        return target, problems


def match_cue_sheets(media: Sequence[Path], csvs: Sequence[Path]) -> dict:
    """Which dropped CSV goes with which dropped episode: a CSV named after the episode (its name
    starts with the episode's name), or, with one CSV and one episode, those two."""
    pairs = {}
    for ep in media:
        named = [c for c in csvs if Path(c).stem.lower().startswith(Path(ep).stem.lower())]
        if named:
            pairs[ep] = named[0]
    if not pairs and len(csvs) == 1 and len(media) == 1:
        pairs[media[0]] = csvs[0]
    return pairs


def read_cue_sheet(path: Path) -> tuple[list[Region], list[str]]:
    """Rows start,end,type,note (times as seconds or 1:23.5 / 00:01:23:15); problems listed, not fatal."""
    from src.utils.timecode import TimecodeError, parse_range

    regions, problems = [], []
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [], [f"cue sheet {Path(path).name} unreadable: {exc}"]
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames or not {"start", "end", "type"} <= {f.strip().lower() for f in reader.fieldnames}:
        return [], [f"cue sheet {Path(path).name}: the header must be start,end,type,note"]
    for n, row in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        kind = CUE_TYPES.get(row.get("type", "").lower())
        if kind is None:
            problems.append(f"cue sheet line {n}: type must be music, sfx or overlap_ok")
            continue
        try:
            start = float(row["start"]) if re.fullmatch(r"[\d.]+", row["start"]) else parse_range(row["start"])[0]
            end = float(row["end"]) if re.fullmatch(r"[\d.]+", row["end"]) else parse_range(row["end"])[0]
        except (TimecodeError, ValueError, KeyError) as exc:
            problems.append(f"cue sheet line {n}: {exc}")
            continue
        if end <= start:
            problems.append(f"cue sheet line {n}: end is before start")
            continue
        regions.append(Region(start, end, kind, "cue sheet", row.get("note") or f"{kind} (cue sheet)"))
    return regions, problems


# --- everything together ---------------------------------------------------------------------------------

def guideline_settings(guide: dict | None) -> dict:
    g = dict(guide or {})
    g.setdefault("sensitivity", "normal")
    g.setdefault("margin_sec", GUIDELINE_MARGINS.get(str(g["sensitivity"]), 0.3))
    return g


def find_regions(sig, f0: np.ndarray, words: Sequence[SpokenWord], segments, s: dict, guide: dict,
                 cue_sheet: Sequence[Region] = (), references: Sequence[tuple[str, np.ndarray]] = ()) -> Regions:
    g = guideline_settings(guide)
    items: list[Region] = list(cue_sheet)
    sheet_spans = [(r.start, r.end) for r in cue_sheet]

    def covered_by_sheet(r: Region) -> bool:                       # cue sheet entries always win
        return any(r.start < b and r.end > a for a, b in sheet_spans)

    detected = cue_regions(sig.samples, sig.sr, references, s) + music_regions(sig, words, segments, s, g) \
        + sfx_regions(sig, words, s) + overlap_zones(sig, words, s)
    # What the guidelines don't allow is still listed, but checked like speech.
    music_ok = set(g.get("music_allowed", ["intro", "outro", "transitions", "ad_breaks", "under_speech"]) or [])
    sfx_ok = set(g.get("sfx_allowed", ["transitions", "stingers", "emphasis"]) or [])
    for r in detected:
        if r.kind == "music" and r.why.startswith("music bed") and "under_speech" not in music_ok:
            r.kind, r.why = "unexpected", r.why + "; the guidelines don't allow music under speech, so it's checked"
        elif r.kind == "music" and not r.why.startswith("music bed") \
                and not music_ok & {"intro", "outro", "transitions", "ad_breaks"}:
            r.kind, r.why = "unexpected", r.why + "; the guidelines allow no music here, so it's checked"
        elif r.kind == "sfx" and not sfx_ok:
            r.kind, r.why = "unexpected", r.why + "; the guidelines expect no effects, so it's checked"
    items += [r for r in detected if not covered_by_sheet(r)]
    items.sort(key=lambda r: r.start)
    return Regions(items=items, margin=float(g["margin_sec"]), speaker_changes=speaker_changes(f0, words, s))


def drop_lyrics(words: Sequence[SpokenWord], regions: Regions, s: dict) -> tuple[list[SpokenWord], int]:
    """Whisper writes words (often repeated) over music: words inside music that it wasn't sure of are dropped."""
    keep, dropped = [], 0
    limit = float(s.get("music_word_min_prob", 0.6))
    for w in words:
        r = regions.hit(w.start, w.end, ("music", "cue"), margin=0.0)
        if r is not None and w.probability < limit:
            dropped += 1
            continue
        keep.append(w)
    return keep, dropped


def required_cues(regions: Regions, guide: dict, duration: float) -> list[tuple[float, str]]:
    """The cues the guidelines require that weren't found where expected: [(time, message)].
    Only when audio_guidelines.required_cues.enabled is true (off by default)."""
    req = (guide or {}).get("required_cues") or {}
    if not req.get("enabled"):
        return []
    out = []
    for cue in req.get("cues") or []:
        name = str(cue.get("reference") or cue.get("name") or "cue")
        where = str(cue.get("at", "start"))
        within = float(cue.get("within_sec", 30))
        lo, hi = (0.0, within) if where == "start" else (max(0.0, duration - within), duration) if where == "end" \
            else (0.0, duration)
        found = [r for r in regions.items if r.touches(lo, hi)
                 and ((r.kind == "cue" and name in r.source) or (r.source == "cue sheet" and name.lower() in r.why.lower()))]
        if not found:
            out.append((lo if where != "end" else hi, f"Required cue \"{name}\" not found in the {where} "
                                                      f"{within:.0f} s (audio guidelines)."))
    return out

"""Make E2CL-style test audio with planted edit mistakes, and a clean control.

    python scripts/make_audio_samples.py                         # speech from the computer's text-to-speech
    python scripts/make_audio_samples.py --source episode.mp3    # or from any speech recording (use a clean one)

1. Speech: the --source file, or a podcast-like script read by the built-in text-to-speech
   (Windows SAPI, macOS `say`), sentence by sentence with natural pauses, over a steady room tone.
2. Whisper (the bot's own transcription, cached) finds the word times.
3. ffmpeg splices the speech to plant one mistake of each kind, at word and pause boundaries:
     stutter          a word said twice (its sound copied right after itself)
     repeated_phrase  a 2-word phrase said twice
     abrupt_cut       a word cut off mid-word, the rest of it and the pause removed
     jump_cut         from one pause to a later one, a different, louder room tone (another mic or room)
     dead_air         4 s of silence (room tone) added at a pause
     repeated_take    a sentence said again a few seconds later (a retake left in)
4. Both files are encoded as MP3 (like real podcasts); the true times are written as JSON.

Writes to data/samples/audio/: e2cl_control.mp3, e2cl_planted.mp3, e2cl_planted_truth.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.audio.media import load_audio  # noqa: E402
from src.audio.transcribe import get_transcript  # noqa: E402
from src.config import load_config, model_dirs, resolve_path  # noqa: E402
from src.utils.system import find_ffmpeg  # noqa: E402
from src.utils.timecode import format_timestamp  # noqa: E402

SR = 22050
SCRIPT = [
    "Welcome back to the show, today we are talking about the market.",
    "Interest rates moved again this week, and people want to know why.",
    "The short answer is that inflation is still higher than expected.",
    "Our guest has worked in banking for more than twenty years.",
    "She thinks the next decision will surprise a lot of investors.",
    "Housing prices are cooling in most cities, but not everywhere.",
    "Small businesses tell us that borrowing costs are their biggest worry.",
    "We will look at what that means for your savings and your loans.",
    "After the break, we answer your questions about retirement accounts.",
    "Thanks for listening, and see you next week on the program.",
]
PAUSES = [0.55, 0.7, 0.6, 0.75, 0.65, 0.6, 0.7, 0.55, 0.65, 0.6]


def tts_speech(tmp: Path) -> Path:
    """The script read sentence by sentence, joined with pauses: 16-bit mono WAV at SR."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_sample_video", ROOT / "scripts" / "make_sample_video.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SAMPLE_RATE = SR
    items = [{"path": str(tmp / f"s{i}.wav"), "text": text} for i, text in enumerate(SCRIPT)]
    module.speak(items)
    parts = [np.zeros(int(0.8 * SR), np.int16)]
    for it, pause in zip(items, PAUSES):
        with wave.open(it["path"], "rb") as w:
            if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (SR, 2, 1):
                raise SystemExit("error: unexpected text-to-speech audio format")
            parts.append(np.frombuffer(w.readframes(w.getnframes()), np.int16))
        parts.append(np.zeros(int(pause * SR), np.int16))
    out = tmp / "speech.wav"
    write_wav(out, np.concatenate(parts).astype(np.float32) / 32768.0)
    return out


def write_wav(path: Path, x: np.ndarray, sr: int = SR) -> None:
    data = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)


def noise(seconds: float, level_db: float, seed: int, tilt: float) -> np.ndarray:
    """A steady hiss (a one-pole low-pass of white noise): room tone. tilt near 1 = darker."""
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(int(seconds * SR))
    out = np.empty_like(white)
    acc = 0.0
    for i, v in enumerate(white):
        acc = tilt * acc + (1 - tilt) * v
        out[i] = acc
    out += 0.15 * white
    out /= np.sqrt(np.mean(out ** 2)) + 1e-9
    return (out * 10 ** (level_db / 20)).astype(np.float32)


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(f"error: ffmpeg failed: {result.stderr.strip()[-600:]}")


def write_truth_csv(path: Path, planted: str, control: str, truth: list[dict]) -> None:
    """The planted mistakes as ground truth for scripts/evaluate.py (--truth), the control as a file with no notes."""
    import csv

    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["video", "timestamp", "client", "issue_type", "note_text"])
        for m in truth:
            kind = "repeated_phrase" if m["type"] == "repeated_take" else m["type"]
            w.writerow([planted, format_timestamp(m["time"]), "E2CL", kind, m["words"]])
        w.writerow([control, "", "E2CL", "none", "clean control"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Make E2CL-style test audio with planted edit mistakes.")
    parser.add_argument("--source", type=Path, help="a speech recording to use instead of text-to-speech")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "samples" / "audio")
    parser.add_argument("--room-tone-db", type=float, default=-56.0, help="room tone level added (dBFS)")
    args = parser.parse_args(argv)
    config = load_config()
    ffmpeg = find_ffmpeg(config.get("machine", {}).get("ffmpeg_path"))
    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="qcbot-audio-") as tmp:
        tmp = Path(tmp)
        speech = Path(args.source) if args.source else tts_speech(tmp)
        x = load_audio(speech, ffmpeg, SR)
        base = x + noise(len(x) / SR, args.room_tone_db, 1, 0.97)[:len(x)]
        base_wav = tmp / "base.wav"
        write_wav(base_wav, base)
        write_wav(tmp / "tone.wav", noise(30.0, args.room_tone_db, 2, 0.97))
        write_wav(tmp / "other_room.wav", noise(len(x) / SR + 60, args.room_tone_db + 13, 3, 0.4))

        # Word times from the bot's own Whisper (base model is enough for timing).
        print("finding word times with Whisper...", flush=True)
        words = get_transcript(base_wav, {**config.get("machine", {}), "whisper_model_size": "base"},
                               config["checks"]["transcript"], [], resolve_path(config, "transcript_cache_dir"),
                               model_dirs(config))
        words = [w for w in words if w.text.strip()]
        ends = [i for i, w in enumerate(words) if w.text.strip().endswith((".", "?", "!"))]
        if len(ends) < 8:
            raise SystemExit(f"error: need at least 8 sentences in the speech, Whisper found {len(ends)}")
        starts = [0] + [e + 1 for e in ends[:-1]]

        def pause_after(k):                       # middle of the pause after sentence k
            e = ends[k]
            return (words[e].end + words[e + 1].start) / 2 if e + 1 < len(words) else words[e].end + 0.3

        def content_word(k, offset=1):            # a word of 4+ letters inside sentence k
            idx = [i for i in range(starts[k] + offset, ends[k]) if len(words[i].text.strip(" ,.")) >= 4]
            return idx[0]

        # The edits, as a list of pieces of the base (and inserts), in output order.
        pieces: list[tuple[str, float, float]] = []        # (input, start, end): "0" base, "1" room tone
        truth: list[dict] = []
        out_t = 0.0

        def take(src, a, b):
            nonlocal out_t
            pieces.append((src, a, b))
            out_t += b - a

        cursor = 0.0
        # 1. stutter: a word in sentence 1 said twice
        i = content_word(1)
        w = words[i]
        take("0", cursor, w.end)
        truth.append({"type": "stutter", "time": round(out_t - (w.end - w.start), 3), "words": w.text.strip()})
        take("1", 0.0, 0.07)
        take("0", w.start, w.end)
        cursor = w.end
        # 2. retake: sentence 0 said again after sentence 1 (the first take left in)
        p = pause_after(1)
        take("0", cursor, p)
        truth.append({"type": "repeated_phrase", "time": round(out_t, 3), "words": "retake of sentence 1"})
        take("0", words[starts[0]].start - 0.05, pause_after(0))
        cursor = p
        # 3. repeated phrase: two words in sentence 2 said twice
        i = content_word(2)
        a, b = words[i], words[i + 1]
        take("0", cursor, b.end)
        truth.append({"type": "repeated_phrase", "time": round(out_t - (b.end - a.start), 3),
                      "words": f"{a.text.strip()} {b.text.strip()}"})
        take("1", 0.0, 0.09)
        take("0", a.start, b.end)
        cursor = b.end
        # 4. abrupt cut: a word in sentence 4 cut off at its loudest point, the rest and the gap gone
        i = content_word(4)
        w, nxt = words[i], words[i + 1]
        lo, hi = int((w.start + 0.3 * (w.end - w.start)) * SR), int((w.start + 0.7 * (w.end - w.start)) * SR)
        hop = int(0.01 * SR)
        levels = [float(np.mean(base[k:k + hop] ** 2)) for k in range(lo, max(lo + 1, hi - hop), hop)]
        cut = (lo + hop * int(np.argmax(levels)) + hop // 2) / SR
        take("0", cursor, cut)
        truth.append({"type": "abrupt_cut", "time": round(out_t, 3), "words": w.text.strip()})
        cursor = nxt.start
        # 5. dead air: 4 s of room tone at the pause after sentence 5
        p = pause_after(5)
        take("0", cursor, p)
        truth.append({"type": "dead_air", "time": round(out_t, 3), "words": "4 s pause"})
        take("1", 1.0, 5.0)
        cursor = p
        # 6. jump cut: a different room from the pause after sentence 6 to the pause after sentence 8
        p6, p8 = pause_after(6), pause_after(min(8, len(ends) - 2))
        take("0", cursor, p6)
        room_from = out_t
        take("0", p6, p8)
        room_to = out_t
        truth.append({"type": "jump_cut", "time": round(room_from, 3), "words": "room tone changes"})
        truth.append({"type": "jump_cut", "time": round(room_to, 3), "words": "room tone changes back"})
        take("0", p8, len(base) / SR)

        chains = [f"[{src}:a]atrim=start={a:.4f}:end={b:.4f},asetpts=PTS-STARTPTS[p{n}]"
                  for n, (src, a, b) in enumerate(pieces)]
        labels = "".join(f"[p{n}]" for n in range(len(pieces)))
        graph = ";".join(chains) + f";{labels}concat=n={len(pieces)}:v=0:a=1[cat];" + \
            f"[2:a]volume=volume=0:enable='not(between(t,{room_from:.3f},{room_to:.3f}))'[room];" + \
            "[cat][room]amix=inputs=2:normalize=0:duration=first[out]"
        planted = args.out / "e2cl_planted.mp3"
        run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(base_wav),
             "-i", str(tmp / "tone.wav"), "-i", str(tmp / "other_room.wav"), "-filter_complex", graph, "-map", "[out]",
             "-ac", "1", "-ar", "44100", "-codec:a", "libmp3lame", "-b:a", "128k", str(planted)])
        control = args.out / "e2cl_control.mp3"
        run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(base_wav),
             "-ac", "1", "-ar", "44100", "-codec:a", "libmp3lame", "-b:a", "128k", str(control)])

    truth_file = args.out / "e2cl_planted_truth.json"
    truth_file.write_text(json.dumps({"source": str(args.source or "text-to-speech"), "mistakes": truth}, indent=2),
                          encoding="utf-8")
    write_truth_csv(args.out / "ground_truth_audio.csv", planted.name, control.name, truth)
    print(f"wrote {control} (clean control: must give no flags)")
    print(f"wrote {planted} with {len(truth)} planted mistakes; times in {truth_file}")
    for t in truth:
        print(f"  {t['time']:7.2f} s  {t['type']:<16} {t['words']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

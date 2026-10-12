"""Make a short synthetic test video with known caption mistakes.

    python scripts/make_sample_video.py

The computer's built-in text-to-speech reads a script one line at a time (Windows: SAPI through
PowerShell; macOS: the `say` command), so the exact time of every line is known. ffmpeg turns the
audio into an MP4 with a black picture. The SRT shows each line with a planted mistake copied from
data/examples.csv, and the expected findings are written next to it.

Writes to data/samples/: synthetic_sample.mp4, synthetic_sample.srt, synthetic_sample_expected.json
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.utils.system import find_ffmpeg  # noqa: E402

SAMPLE_RATE = 22050

# (spoken text, caption text, pause before in seconds, caption starts this much early, expected issue, source)
LINES = [
    (None, "QC BOT TEST CLIP", 0.3, 0.0, None, "on-screen title, nothing spoken: must be skipped"),
    ("Welcome back to the channel.", "Welcome back to the channel.", 1.2, 0.0, None, "clean"),
    ("Or you just want to rest?", "Are you just want to rest?", 0.4, 0.0, "wrong_word", "E3JB 00:01:47:19 \"Or\" not \"Are\""),
    ("It was hard, but it was worth it.", "It was hard, it was worth it.", 0.4, 0.0, "missing_word", "E3JB 00:00:40:29 missing \"but\""),
    ("We need to talk about the budget.", "We need to to talk about the budget.", 0.4, 0.0, "extra_word", "E3JB 00:01:33:16 duplicate \"to\""),
    ("Rule number one, never lose money.", "Rule number one is, never lose money.", 0.4, 0.0, "extra_word", "E2CS 00:00:08:09 \"one is\" -> \"one\""),
    ("Sales peaked up in the summer.", "Sales picked up in the summer.", 0.4, 0.0, "wrong_word", "E2CS 00:00:07:50 \"peaked up\" not \"picked up\" (hard: similar sounds)"),
    ("Bright colors make the room feel bigger.", "Bright colors make the room feel bigger.", 2.0, 1.2, "caption_early", "E2JG 00;00;13;49 \"bright\" came early"),
    ("We grew forty percent last year.", "We grew 40% last year.", 0.4, 0.0, None, "clean: digits vs words"),
    ("Mark McGwire hit seventy home runs.", "Mark McGwire hit 70 home runs.", 0.4, 0.0, None, "clean: name and number"),
    ("Thanks for watching, see you next time.", "Thanks for watching, see you next time.", 0.4, 0.0, None, "clean"),
]


def speak_windows(items: list[dict]) -> None:
    """Windows SAPI via PowerShell. Texts go through a JSON file, never through the command line."""
    with tempfile.TemporaryDirectory() as tmp:
        jobs = Path(tmp) / "jobs.json"
        jobs.write_text(json.dumps(items), encoding="utf-8")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            f"$items = Get-Content -Raw -Encoding UTF8 -LiteralPath '{str(jobs).replace(chr(39), chr(39) * 2)}' | ConvertFrom-Json; "
            f"$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo({SAMPLE_RATE}, "
            "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
            "foreach ($it in $items) { $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.SetOutputToWaveFile($it.path, $fmt); $s.Speak($it.text); $s.Dispose() }"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True)


def speak_macos(items: list[dict]) -> None:
    for it in items:
        subprocess.run(["say", "-o", it["path"], f"--data-format=LEI16@{SAMPLE_RATE}", it["text"]], check=True)


def speak(items: list[dict]) -> None:
    system = platform.system()
    if system == "Windows":
        speak_windows(items)
    elif system == "Darwin":
        speak_macos(items)
    else:
        raise SystemExit("error: text-to-speech is only set up for Windows and macOS")


def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Make a synthetic test video with known caption mistakes.")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "samples")
    args = parser.parse_args(argv)
    ffmpeg = find_ffmpeg(load_config().get("machine", {}).get("ffmpeg_path"))
    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="qcbot-sample-") as tmp:
        tmp = Path(tmp)
        items = [{"path": str(tmp / f"line{i}.wav"), "text": spoken}
                 for i, (spoken, *_rest) in enumerate(LINES) if spoken]
        speak(items)

        frames = bytearray()
        bytes_per_second = SAMPLE_RATE * 2
        captions, expected = [], []
        t = 0.0
        for i, (spoken, caption, pause, early, issue, source) in enumerate(LINES):
            frames += b"\x00\x00" * int(pause * SAMPLE_RATE)
            t += pause
            if spoken:
                with wave.open(str(tmp / f"line{i}.wav"), "rb") as w:
                    if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (SAMPLE_RATE, 2, 1):
                        raise SystemExit("error: unexpected text-to-speech audio format")
                    audio = w.readframes(w.getnframes())
                duration = len(audio) / bytes_per_second
                frames += audio
            else:
                duration = 1.2                              # the title sits on screen during silence
                frames += b"\x00\x00" * int(duration * SAMPLE_RATE)
            cap_id = len(captions) + 1
            captions.append((cap_id, max(0.0, t - early), t + duration, caption))
            if issue:
                expected.append({"caption_id": cap_id, "type": issue, "source": source})
            t += duration
        frames += b"\x00\x00" * int(0.8 * SAMPLE_RATE)

        wav = tmp / "speech.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(bytes(frames))

        srt = args.out / "synthetic_sample.srt"
        with srt.open("w", encoding="utf-8", newline="\n") as f:
            for cap_id, start, end, text in captions:
                f.write(f"{cap_id}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n\n")

        # Burn the captions into the picture, as an edited video would have them. ffmpeg runs inside
        # the output folder so the subtitles filter gets a bare file name (no drive letters or
        # backslashes to escape on Windows).
        mp4 = args.out / "synthetic_sample.mp4"
        base = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=0x202830:s=640x360:r=25", "-i", str(wav), "-shortest"]
        encode = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(mp4)]
        burned = subprocess.run(base + ["-vf", f"subtitles={srt.name}:force_style='Fontsize=22'"] + encode,
                                cwd=args.out, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if burned.returncode != 0:
            print(f"note: could not burn captions into the picture ({burned.stderr.strip()[-200:]}); "
                  "making the video without them", file=sys.stderr)
            subprocess.run(base + encode, check=True)
    (args.out / "synthetic_sample_expected.json").write_text(
        json.dumps({"expected": expected, "clean_caption_ids": [c[0] for c in captions
                                                                 if c[0] not in {e["caption_id"] for e in expected}]},
                   indent=2), encoding="utf-8")
    print(f"wrote {mp4.name} ({t:.1f} s), {srt.name} and synthetic_sample_expected.json to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

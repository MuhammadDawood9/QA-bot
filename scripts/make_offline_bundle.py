"""Prepare the models folder for the QC floor, once, on a PC with internet access.

    python scripts/make_offline_bundle.py --out D:\\VideoQCBot-models            (Whisper base + small, face model)
    python scripts/make_offline_bundle.py --out /Volumes/QC/VideoQCBot/models --sizes base small medium

Then copy the folder to the shared models folder (by default <shared data folder>/models). Every
PC reads the models from there, read-only, and never downloads its own:

    <out>/whisper/base/          model.bin, config.json, tokenizer.json, vocabulary.txt
    <out>/whisper/small/
    <out>/face_detection_yunet_2023mar.onnx
    <out>/MANIFEST.json          what's there, sizes and checksums

The OCR models ship inside the app (they're part of the rapidocr package) and ffmpeg is bundled
with the app (imageio-ffmpeg), so neither needs to be in this folder.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.video.color import YUNET_FILE, YUNET_SHA256, YUNET_URL  # noqa: E402

WHISPER_REPOS = {"tiny": "Systran/faster-whisper-tiny", "base": "Systran/faster-whisper-base",
                 "small": "Systran/faster-whisper-small", "medium": "Systran/faster-whisper-medium"}
WHISPER_FILES = ["model.bin", "config.json", "tokenizer.json", "vocabulary.*"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_whisper(size: str, out: Path) -> Path:
    from huggingface_hub import snapshot_download

    target = out / "whisper" / size
    snapshot_download(WHISPER_REPOS[size], local_dir=str(target), allow_patterns=WHISPER_FILES)
    for leftover in (target / ".cache",):              # huggingface_hub's own bookkeeping
        if leftover.is_dir():
            import shutil

            shutil.rmtree(leftover, ignore_errors=True)
    if not (target / "model.bin").is_file():
        raise SystemExit(f"error: {size}: model.bin wasn't downloaded")
    return target


def fetch_face_model(out: Path) -> Path:
    target = out / YUNET_FILE
    if not target.is_file() or sha256(target) != YUNET_SHA256:
        with urllib.request.urlopen(YUNET_URL, timeout=120) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != YUNET_SHA256:
            raise SystemExit("error: the face model's checksum doesn't match")
        target.write_bytes(data)
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="folder to fill (then copy it to the shared models folder)")
    parser.add_argument("--sizes", nargs="+", default=["base", "small"], choices=sorted(WHISPER_REPOS),
                        help="Whisper models (the app picks base or small per PC unless set by hand)")
    args = parser.parse_args(argv)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"made": dt.datetime.now().isoformat(timespec="seconds"), "files": {}}
    for size in args.sizes:
        print(f"Whisper {size}...", flush=True)
        folder = fetch_whisper(size, out)
        for f in sorted(folder.iterdir()):
            if f.is_file():
                manifest["files"][f"whisper/{size}/{f.name}"] = {"bytes": f.stat().st_size, "sha256": sha256(f)}
    print("face model...", flush=True)
    face = fetch_face_model(out)
    manifest["files"][face.name] = {"bytes": face.stat().st_size, "sha256": sha256(face)}
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    total = sum(v["bytes"] for v in manifest["files"].values()) / 1024 ** 2
    print(f"done: {len(manifest['files'])} files, {total:.0f} MB in {out}")
    print("Copy this folder to the shared models folder (default: <shared data folder>/models).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

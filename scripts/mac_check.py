"""One run on a real machine (made for the Intel Mac) that checks what the automatic tests can't.

    python scripts/mac_check.py                  # everything, about 15-20 minutes, a few questions at the end
    python scripts/mac_check.py --quick          # skip the slow test suite
    python scripts/mac_check.py --no-questions   # only the automatic parts

Steps:
  1. Environment: OS version, processor, Python, Qt platform, screens and their scaling, the menu bar
     (tray) icon, ffmpeg.
  2. Sample videos: made with the computer's own voice (macOS: `say`) if data/samples/ lacks them.
  3. The test suite including the slow tests on real videos (OCR, speech, color) on this machine.
  4. One video checked through the app (`--check`), timed: real speed on this machine.
  5. The mascot in each state on the real screen: screenshots, and CPU use idle and working.
  6. The app itself, and questions only a person can answer (drag, click-through, drop a video,
     menu bar, no Dock icon, sharpness on Retina...).

Everything goes into mac_check_<date-time>/ in the project folder: report.md (send this, or the
whole folder zipped), screenshots and the logs. Nothing from data/ (client files) is copied.
macOS asks once whether Terminal may record the screen (System Settings > Privacy & Security >
Screen Recording): allow it, or the screenshots show only the wallpaper.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "data" / "samples"
SAMPLE_VIDEO = SAMPLES / "burned_box.mp4"
MAC = sys.platform == "darwin"

QUESTIONS = [
    ("mascot", "The round face is in the bottom-right corner with a soft pink-violet glow around it, "
               "and NO black or white square around it?"),
    ("sharp", "The face and its ring look sharp, not blurry or jagged (Retina)?"),
    ("hover", "Moving the mouse onto the face makes it blink (and again every 3 s while you stay)?"),
    ("click_through", "Clicking on the glow just OUTSIDE the circle clicks the window behind it "
                      "(the mascot doesn't react)?"),
    ("drag", "Dragging the face moves it, smoothly, and the glow follows?"),
    ("menu_bar", "There is a Video QC Bot icon in the menu bar at the top right, and clicking it shows the menu?"),
    ("no_dock", "There is NO Video QC Bot icon in the Dock and none in Cmd+Tab?"),
    ("right_click", "Right-clicking the face shows the same menu (Check a video, Skin, Checks...)?"),
    ("drop", f"Drag {SAMPLE_VIDEO.name} (Finder: {SAMPLES}) onto the face: it starts working (blue-violet "
             f"pulsing glow, a progress ring, the tooltip says the stage)?"),
    ("done", "When it finishes (a few minutes): green flash, a badge with the number of issues, a "
             "notification, and the Word report opens?"),
    ("spaces", "Switch to another desktop (Space) or a full-screen app: the mascot is still there, on top?"),
    ("quit", "Right-click > Quit Video QC Bot closes it cleanly (no error, nothing left in the menu bar)?"),
]


class Report:
    def __init__(self, folder: Path):
        self.folder = folder
        self.lines: list[str] = []

    def heading(self, text: str) -> None:
        print(f"\n== {text}")
        self.lines += ["", f"## {text}", ""]

    def line(self, text: str = "") -> None:
        print(text)
        self.lines.append(text)

    def save(self) -> Path:
        path = self.folder / "report.md"
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        return path


def run(cmd: list[str], timeout: float, log_file: Path | None = None) -> tuple[int, float, str]:
    """(exit code, seconds, output); the output also goes to log_file."""
    started = time.monotonic()
    try:
        done = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env={**os.environ, "PYTHONUTF8": "1"})
        code, out = done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired as exc:
        code, out = -1, f"timed out after {timeout:.0f} s\n{exc.stdout or ''}{exc.stderr or ''}"
    took = time.monotonic() - started
    if log_file is not None:
        log_file.write_text(out, encoding="utf-8")
    return code, took, out


# --- 1. environment --------------------------------------------------------------------------

def environment(r: Report) -> None:
    r.heading("1. Environment")
    if MAC:
        r.line(f"- macOS {platform.mac_ver()[0]}, processor {platform.machine()}")
    else:
        r.line(f"- {platform.platform()}, processor {platform.machine()} (not a Mac: this run is a rehearsal)")
    r.line(f"- Python {platform.python_version()} ({sys.executable})")
    try:
        import PySide6
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon

        from src.app.main import make_application

        QApplication.instance() or make_application(["mac_check"])     # set up exactly as the app does
        r.line(f"- PySide6 {PySide6.__version__}, Qt platform '{QGuiApplication.platformName()}'")
        for n, s in enumerate(QGuiApplication.screens(), start=1):
            g = s.geometry()
            r.line(f"- screen {n}: {g.width()}x{g.height()} points, scaling {s.devicePixelRatio():g}x"
                   f"{' (Retina)' if s.devicePixelRatio() >= 2 else ''}")
        r.line(f"- menu bar / tray icon available: {QSystemTrayIcon.isSystemTrayAvailable()}")
    except Exception as exc:                                   # report it, keep going
        r.line(f"- Qt problem: {type(exc).__name__}: {exc}")
    try:
        from src.config import load_config
        from src.utils.system import find_ffmpeg

        ffmpeg = find_ffmpeg(load_config().get("machine", {}).get("ffmpeg_path"))
        r.line(f"- ffmpeg: {ffmpeg}")
    except Exception as exc:
        r.line(f"- ffmpeg: NOT FOUND ({exc}). Install it: brew install ffmpeg")
    for module in ("onnxruntime", "ctranslate2", "cv2", "av", "rapidocr_onnxruntime", "faster_whisper"):
        try:
            mod = __import__(module)
            r.line(f"- {module} {getattr(mod, '__version__', '')}: ok")
        except Exception as exc:
            r.line(f"- {module}: FAILED to load ({type(exc).__name__}: {exc})")


# --- 2-4. samples, tests, a timed check --------------------------------------------------------

def samples(r: Report) -> bool:
    r.heading("2. Sample videos")
    if SAMPLE_VIDEO.is_file():
        r.line(f"- already there: {SAMPLE_VIDEO.name}")
        return True
    for script in ("make_sample_video.py", "make_burned_in_samples.py"):
        code, took, out = run([sys.executable, str(ROOT / "scripts" / script)], 900, r.folder / f"{script}.log")
        r.line(f"- {script}: {'ok' if code == 0 else f'FAILED (exit {code})'} in {took:.0f} s")
        if code != 0:
            r.line("  " + out.strip().splitlines()[-1] if out.strip() else "  (no output)")
            return False
    return SAMPLE_VIDEO.is_file()


def tests(r: Report, quick: bool) -> None:
    r.heading("3. Test suite")
    cmd = [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider"] + ([] if quick else ["--run-slow"])
    code, took, out = run(cmd, 3600, r.folder / "pytest.log")
    summary = next((l for l in reversed(out.splitlines()) if " passed" in l or " failed" in l or "error" in l), "")
    r.line(f"- {'fast tests only' if quick else 'all tests, slow ones included'}: {summary.strip('= ')} "
           f"({took / 60:.1f} min, exit {code})")
    failed = [l for l in out.splitlines() if l.startswith(("FAILED", "ERROR"))]
    for l in failed[:20]:
        r.line(f"  - {l}")
    if failed:
        r.line("  (details in pytest.log)")


def timed_check(r: Report) -> None:
    r.heading("4. One video checked through the app")
    if not SAMPLE_VIDEO.is_file():
        r.line("- skipped: no sample video")
        return
    cache = [ROOT / "cache" / "ocr", ROOT / "cache" / "color", ROOT / "cache" / "transcripts"]
    r.line(f"- {SAMPLE_VIDEO.name}, about 40 s of 1280x720 video; caches: "
           f"{'some results may come from earlier runs' if any(p.is_dir() for p in cache) else 'empty'}")
    code, took, out = run([sys.executable, "-m", "src.app", "--check", str(SAMPLE_VIDEO)], 1800,
                          r.folder / "check.log")
    issues = next((l for l in out.splitlines() if "issues, report" in l), "")
    r.line(f"- exit {code} in {took:.0f} s: {issues.split('check: ')[-1] if issues else 'no result line (see check.log)'}")


# --- 5. the mascot on the real screen ---------------------------------------------------------

def mascot_states(r: Report) -> None:
    r.heading("5. The mascot on the real screen")
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from src.app.main import make_application
    from src.app.mascot import WINDOW, MascotWindow
    from src.app.settings import Settings

    app = QApplication.instance() or make_application(["mac_check"])
    mascot = MascotWindow(Settings(r.folder / "settings.json"))          # its own settings: yours are untouched
    area = QGuiApplication.primaryScreen().availableGeometry()
    mascot.move(QPoint(area.left() + 200, area.top() + 200))
    mascot.show()

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

    pump(1.0)
    for state, count in (("idle", None), ("working", None), ("done", 7), ("error", None)):
        mascot.set_state(state, count)
        if state == "working":
            mascot.set_progress("Reading the video", 0.4)
        pump(0.7 if state != "done" else 0.3)
        g = mascot.frameGeometry()
        shot = QGuiApplication.primaryScreen().grabWindow(0, g.x() - 20, g.y() - 20, WINDOW + 40, WINDOW + 40)
        shot.save(str(r.folder / f"mascot_{state}.png"))
        r.line(f"- {state}: mascot_{state}.png ({shot.width()}x{shot.height()} pixels)")

    for state in ("idle", "working"):
        mascot.set_state(state)
        pump(1.0)
        c0, t0 = time.process_time(), time.monotonic()
        pump(5.0)
        cpu = (time.process_time() - c0) / (time.monotonic() - t0) * 100
        r.line(f"- CPU while {state}: {cpu:.1f}% of one core (expected: about 0 idle, about 1-2% working)")
    mascot.close()
    pump(0.3)
    if MAC:
        r.line("- If the screenshots show only the wallpaper, Terminal needs Screen Recording permission "
               "(System Settings > Privacy & Security); the mascot itself is fine.")


# --- 6. the app and the questions -------------------------------------------------------------

def questions(r: Report) -> None:
    r.heading("6. The app, checked by you")
    app = subprocess.Popen([sys.executable, "-m", "src.app"], cwd=ROOT)
    print("\nThe app is starting (give it a few seconds). Answer each question with y (yes), n (no) or")
    print("s (skip); after n you can type what you saw. Take your time: the app keeps running meanwhile.\n")
    time.sleep(4)
    answers = {"y": "yes", "n": "NO", "s": "skipped"}
    try:
        for key, text in QUESTIONS:
            while True:
                reply = input(f"{text}\n  y / n / s: ").strip().lower()[:1]
                if reply in answers:
                    break
            note = input("  what did you see? ").strip() if reply == "n" else ""
            r.line(f"- {key}: **{answers[reply]}**{f' - {note}' if note else ''}")
            print()
        extra = input("Anything else you noticed (Enter for nothing)? ").strip()
        if extra:
            r.line(f"- other: {extra}")
    except (EOFError, KeyboardInterrupt):
        r.line("- (questions stopped early)")
    finally:
        if app.poll() is None:
            app.terminate()
            try:
                app.wait(15)
            except subprocess.TimeoutExpired:
                app.kill()


def app_log(r: Report) -> None:
    from src.app.settings import log_dir

    logs = sorted(log_dir().glob("*.log*"), key=lambda p: p.stat().st_mtime) if log_dir().is_dir() else []
    if logs:
        lines = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
        (r.folder / "app.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        problems = [l for l in lines if " WARNING " in l or " ERROR " in l]
        r.heading("App log")
        r.line(f"- last 300 lines in app.log; {len(problems)} warnings or errors among them")
        for l in problems[-10:]:
            r.line(f"  - {l[:200]}")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Check the bot on this machine, including what tests can't.")
    parser.add_argument("--quick", action="store_true", help="skip the slow tests (saves about 10 minutes)")
    parser.add_argument("--no-questions", action="store_true", help="only the automatic parts")
    args = parser.parse_args(argv)

    folder = ROOT / f"mac_check_{dt.datetime.now():%Y%m%d-%H%M}"
    folder.mkdir(exist_ok=True)
    r = Report(folder)
    r.lines = [f"# Video QC Bot machine check, {dt.datetime.now():%Y-%m-%d %H:%M}"]
    environment(r)
    have_video = samples(r)
    tests(r, args.quick)
    if have_video:
        timed_check(r)
    try:
        mascot_states(r)
    except Exception as exc:
        r.line(f"- FAILED: {type(exc).__name__}: {exc}")
    if not args.no_questions:
        questions(r)
    try:
        app_log(r)
    except Exception as exc:
        r.line(f"- app log not read: {exc}")
    path = r.save()
    print(f"\nDone. Send this folder (zip it), or at least {path.name}:\n  {folder}")
    if MAC and shutil.which("open"):
        subprocess.run(["open", str(folder)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

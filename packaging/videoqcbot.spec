# PyInstaller build file for the Video QC Bot desktop app.
#
#   Windows:  pyinstaller packaging\videoqcbot.spec --noconfirm --distpath dist --workpath build
#   macOS:    pyinstaller packaging/videoqcbot.spec --noconfirm --distpath dist --workpath build
#
# Build each OS on that OS: PyInstaller can't cross-build. A macOS build runs on the processor type
# it was built on (Intel or Apple Silicon); an Intel build also runs on Apple Silicon through Rosetta.
# Output: dist/VideoQCBot/VideoQCBot.exe (a folder: keep it whole) or dist/Video QC Bot.app.
# See packaging/README.md.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
MAC = sys.platform == "darwin"
import re as _re

VERSION = _re.search(r'__version__ = "([^"]+)"', (ROOT / "src" / "version.py").read_text(encoding="utf-8")).group(1)

# The app's own copy of the shared files (defaults, and what a new shared folder is set up with), and the images.
# A videoqcbot-bootstrap.yaml from IT is NOT built in: it goes next to the installed app (docs/IT_DEPLOYMENT.md).
datas = [(str(ROOT / name), ".") for name in ("config.yaml", "rules.yaml", "glossary.txt", "extra_words.txt",
                                              "ignore.yaml", "color.yaml")]
datas += [(str(ROOT / "clients"), "clients"), (str(ROOT / "assets"), "assets")]
if (ROOT / "data" / "examples.csv").is_file():          # real QC examples for the punctuation prompt
    datas.append((str(ROOT / "data" / "examples.csv"), "data"))
# Model and data files inside packages: OCR models, Whisper's voice-activity model, the spell
# checker's word lists, Word's document template.
# imageio_ffmpeg: the bundled ffmpeg program (floor PCs need no ffmpeg install).
for package in ("rapidocr_onnxruntime", "faster_whisper", "spellchecker", "docx", "imageio_ffmpeg"):
    datas += collect_data_files(package)

binaries = collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("onnxruntime")

# Modules the code imports inside functions, which PyInstaller's scan can miss.
hiddenimports = collect_submodules("src") + ["faster_whisper", "rapidocr_onnxruntime", "spellchecker", "docx",
                                             "anthropic", "openpyxl", "platformdirs", "rapidfuzz", "pypdf",
                                             "imageio_ffmpeg"]

a = Analysis(
    [str(ROOT / "run_app.pyw")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "pytest", "PyQt5", "PyQt6", "IPython", "notebook"],
    noarchive=False,
)
# Big files the app never uses (about 49 MB): OpenCV's own ffmpeg video reader (videos are decoded
# with PyAV) and Qt's software OpenGL fallback (the windows are plain widgets, no OpenGL). The
# encoder libraries in av.libs look unused too, but libavcodec loads them at start, so they stay.
UNUSED = ("opencv_videoio_ffmpeg", "opengl32sw")
a.binaries = [b for b in a.binaries if not Path(b[0]).name.lower().startswith(UNUSED)]
a.datas = [d for d in a.datas if not Path(d[0]).name.lower().startswith(UNUSED)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VideoQCBot",
    console=False,                 # no terminal window
    icon=str(ROOT / "assets" / "app" / ("videoqcbot.icns" if MAC else "videoqcbot.ico")),
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="VideoQCBot")

if MAC:
    app = BUNDLE(
        coll,
        name="Video QC Bot.app",
        icon=str(ROOT / "assets" / "app" / "videoqcbot.icns"),
        bundle_identifier="com.videoqcbot.companion",
        info_plist={
            "CFBundleName": "Video QC Bot",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,               # no Dock icon: the mascot and the menu bar icon are the app
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "13.0",  # PySide6 6.11 needs macOS 13 (Ventura)
        },
    )

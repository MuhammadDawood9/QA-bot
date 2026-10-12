# Building the Video QC Bot app

PyInstaller turns the app into a program that runs without Python: a folder with `VideoQCBot.exe` on
Windows, `Video QC Bot.app` on macOS. **Build each one on its own system.** PyInstaller can't build a
Mac app on Windows or a Windows app on a Mac.

What's inside: the app, the checks, the OCR models, the mascot images, and your config, rules,
client files and glossary as they are when you build. Not inside:
- **ffmpeg:** install it on each machine. The app tells you how if it's missing.
- **The Whisper speech model** (145 MB) and the **face model** (232 KB): downloaded on the first check
  that needs them, then kept.

Where a built app keeps things (never inside itself, so updates don't wipe them):

| | Windows | macOS |
|---|---|---|
| Your editable config, rules, clients, glossary (copied on first run, never overwritten) | `%LOCALAPPDATA%\VideoQCBot\data\` | `~/Library/Application Support/VideoQCBot/data/` |
| Cache and models | the same `data\` folder | the same `data/` folder |
| Reports (default; change in Settings) | `Documents\Video QC Bot reports` | `~/Documents/Video QC Bot reports` |
| App settings | `%LOCALAPPDATA%\VideoQCBot\settings.json` | `~/Library/Application Support/VideoQCBot/settings.json` |
| Log | `%LOCALAPPDATA%\VideoQCBot\Logs\videoqcbot.log` | `~/Library/Logs/VideoQCBot/videoqcbot.log` |

To change a rule or add a client in a built app, edit the files in the `data` folder above. Editing
the project's files changes the *next* build only for a fresh install, because existing files in
`data` are never overwritten. Delete a file there to get the build's version back.

---

## Windows (10 / 11)

**Build.** In PowerShell, in the project folder:

```powershell
.venv\Scripts\activate
pip install -r requirements.txt
pip install pyinstaller==6.22.3
pyinstaller packaging\videoqcbot.spec --noconfirm --distpath dist --workpath build
```

This takes a few minutes and makes `dist\VideoQCBot\` (about 440 MB). **Keep the folder whole:**
`VideoQCBot.exe` needs the `_internal` folder next to it.

**Check the build** before using it. Each command waits for the app and returns its exit code; 0 is good:

```powershell
dist\VideoQCBot\VideoQCBot.exe --selftest                 # every library and data file loads
dist\VideoQCBot\VideoQCBot.exe --check path\to\video.mp4  # a full check, no window; writes the report
```

Neither opens a window. The results are in the log file (path in the table above).

**Install.**
1. Copy the `VideoQCBot` folder to `%LOCALAPPDATA%\Programs\` (no admin rights needed).
2. Right-click `VideoQCBot.exe`, choose *Send to → Desktop (create shortcut)*, and/or pin it to Start.
3. For *Start at login*, use the mascot's right-click menu.

**First run: the SmartScreen warning.** The app isn't code-signed, so Windows shows a blue box:
*"Windows protected your PC. Microsoft Defender SmartScreen prevented an unrecognized app from
starting."* Click **More info**, then **Run anyway**. It only asks once per copy of the app. A copy
downloaded from the internet or a chat may also get *"This file came from another computer..."*: right-click
the zip, choose *Properties*, and tick **Unblock** before unzipping. A code-signing certificate would
remove these warnings.

**Share it:** zip the `dist\VideoQCBot` folder. The other PC needs ffmpeg (`winget install ffmpeg`).

---

## macOS (13 Ventura or newer)

**Build.** In Terminal, in the project folder on the Mac:

```bash
python3.12 -m venv .venv            # once; Python 3.12 from python.org or Homebrew
source .venv/bin/activate
pip install -r requirements.txt
pip install pyinstaller==6.22.3
pyinstaller packaging/videoqcbot.spec --noconfirm --distpath dist --workpath build
```

This makes `dist/Video QC Bot.app`.

- **Processor:** the app runs on the processor type of the Mac that built it. An Intel build also runs
  on Apple Silicon (through Rosetta, a little slower). For a native Apple Silicon app, build on an
  Apple Silicon Mac.
- **macOS 11 or 12:** install PySide6 6.7.3 first (see `requirements.txt`) and change
  `LSMinimumSystemVersion` in the spec to `11.0`.

**Check the build:**

```bash
"dist/Video QC Bot.app/Contents/MacOS/VideoQCBot" --selftest
"dist/Video QC Bot.app/Contents/MacOS/VideoQCBot" --check path/to/video.mp4
```

**Install:** drag `Video QC Bot.app` into **Applications**. It has no Dock icon by design; look for the
mascot and the robot icon in the menu bar. Turn on *Start at login* from the right-click menu.

**First run: the Gatekeeper warning.** The app isn't signed or notarized by Apple:
- **macOS 13–14:** *"'Video QC Bot' can't be opened because Apple cannot check it for malicious
  software."* Click **OK**, then right-click (or Control-click) the app in Applications, choose
  **Open**, and confirm **Open**.
- **macOS 15 Sequoia or newer:** *"'Video QC Bot' Not Opened. Apple could not verify 'Video QC Bot' is
  free of malware..."* Click **Done**. Then open **System Settings → Privacy & Security**, scroll down
  to *"'Video QC Bot' was blocked..."*, click **Open Anyway**, and confirm with your password.

You only do this once. An app you built on the same Mac usually opens without the warning; it appears
for a copy that was downloaded or AirDropped. An Apple Developer ID ($99 a year) with notarization would
remove it.

**Notifications:** the first time a check finishes, macOS may ask whether Video QC Bot may send
notifications. Allow it to get the "done" messages.

**ffmpeg:** `brew install ffmpeg`. The app also looks in Homebrew's folders (`/opt/homebrew/bin`,
`/usr/local/bin`), because apps started from Finder don't see Terminal's PATH.

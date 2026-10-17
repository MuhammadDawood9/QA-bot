# Video QC Bot: deployment on the QC floor

For IT. Video QC Bot is a small desktop helper (a mascot plus a tray / menu bar icon) that QC
reviewers drop videos and podcast audio onto; it writes a Word report of caption, color and audio
mistakes. It goes on every PC on the floor, used by many people, on PCs that also edit and QC.

**Short version**

1. Build the app on each OS (Windows on Windows, macOS on a Mac).
2. Make one shared data folder (network drive or synced SharePoint/OneDrive folder) and set its
   permissions: read for everyone, write for editors.
3. Put the models there once (`scripts/make_offline_bundle.py`).
4. Install the app per user or per machine, no admin rights needed to run it, and drop a
   `videoqcbot-bootstrap.yaml` next to it (or machine-wide) pointing at the shared folder.
5. Exclude its cache folders from real-time antivirus scanning if checks are slow.

Nothing needs the internet once this is done.

---

## 1. What goes where

| | What | Where | Who writes |
|---|---|---|---|
| (a) | The app | Windows: any folder, e.g. `%LOCALAPPDATA%\Programs\VideoQCBot\` (per user) or `C:\Program Files\VideoQCBot\` (per machine). macOS: `/Applications/Video QC Bot.app` | Only the installer. The app never writes inside itself. |
| (b) | **Shared data**: clients, glossary, rules, ignore list, color thresholds, floor settings, backups, changelog, `latest.json`, models, optional shared reports | One folder for the whole floor | Editors (through the app); everyone reads |
| (c) | **Per-user local data**: settings, reports, caches, logs, a cached copy of (b) | Windows `%LOCALAPPDATA%\VideoQCBot\` (`data\`, `Logs\`), macOS `~/Library/Application Support/VideoQCBot/`, `~/Library/Logs/VideoQCBot/`. Reports default under `data\reports` (changeable in Settings) | The user |
| | One-check-at-a-time lock (all users of a PC) | Windows `%PROGRAMDATA%\VideoQCBot\check.lock`, macOS `/Users/Shared/VideoQCBot/check.lock` | The app (normal users can write there by default) |

The shared folder's layout (the app creates it the first time an editor points it at an empty folder):

```
VideoQCBot/                          (the shared data folder)
  config.yaml                        floor-wide check thresholds (overrides the app's built-in defaults)
  color.yaml                         color thresholds + per-client color overrides
  rules.yaml, glossary.txt, extra_words.txt, ignore.yaml, data/examples.csv
  clients/<CODE>.yaml                one file per client
  clients/<CODE>/notes.md, sources/  import notes and copies of imported files
  shared_settings.yaml               editors list, editor PIN (hashed), shared-report switch
  changelog.csv                      who changed what, when, from which PC
  backups/                           the previous version of every changed file (newest 50 per file)
  .locks/data.lock                   held for a second or two while someone saves
  latest.json                        "Check for update" (section 7)
  installers/                        put new builds here
  models/                            Whisper and face models, read-only for everyone (section 4)
  reports/<client>/<date>_<user>_<video>.docx   only if shared reports are switched on
```

## 2. The shared folder and its permissions

**Network drive (recommended):** an SMB share, e.g. `\\qcserver\QC\VideoQCBot` (Windows) or
`smb://qcserver/QC/VideoQCBot` mounted at `/Volumes/QC/VideoQCBot` (macOS).

**Synced SharePoint / OneDrive folder:** works too, e.g. `%OneDrive%\QC\VideoQCBot` or
`~/Library/CloudStorage/OneDrive-Company/QC/VideoQCBot`. Mark the folder *Always keep on this
device*. Limitation: the app's save lock is a file, and a synced folder only shares it after a sync,
so two editors saving at the same second can both succeed and OneDrive keeps a conflict copy.
Each save is still atomic, backed up and in the changelog, so nothing is lost, but with several
active editors prefer a network drive.

**Permissions** (NTFS / share, or SharePoint):

| Who | Shared folder | `models\` | `reports\` | `backups\`, `changelog.csv`, `.locks\` |
|---|---|---|---|---|
| Everyone on the floor | Read | Read | Modify (only if shared reports are on) | Read |
| Editors | Modify | Read | Modify | Modify |
| IT | Full control | Full control | Full control | Full control |

Everyone must be able to *read* the whole folder, otherwise the app falls back to its cached copy
and shows "offline, using cached rules".

**Roles in the app are a convenience, not security.** The app hides Add/Edit client and the
glossary/ignore editors from non-editors (editors are listed by login name in
`shared_settings.yaml`, or unlock with an editor PIN that's stored as a salted PBKDF2 hash). Anyone
with write access to the folder can still change the files directly. Real control = the folder
permissions above. Until an editor list or a PIN is set, everyone counts as an editor, so the first
editor can set things up.

## 3. Installing the app

No admin rights are needed to *run* the app; only installing into `Program Files` / `/Applications`
needs them.

**Windows.** The build is a folder (`dist\VideoQCBot\`, about 500 MB with the bundled ffmpeg). Copy the whole folder.
- Per user, silently, no admin: `robocopy "\\qcserver\QC\VideoQCBot\installers\VideoQCBot-0.5.0" "%LOCALAPPDATA%\Programs\VideoQCBot" /MIR /NFL /NDL /NJH /NJS`
  then create a Start Menu shortcut, e.g. with PowerShell:
  `$s=(New-Object -ComObject WScript.Shell).CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Video QC Bot.lnk"); $s.TargetPath="$env:LOCALAPPDATA\Programs\VideoQCBot\VideoQCBot.exe"; $s.Save()`
- Per machine (admin): the same robocopy to `C:\Program Files\VideoQCBot`, shortcut in
  `%ProgramData%\Microsoft\Windows\Start Menu\Programs`. Intune / SCCM: wrap these two commands in a
  Win32 app; detection rule = the file `VideoQCBot.exe` exists.
- Uninstall: delete the folder and the shortcut. User data stays in `%LOCALAPPDATA%\VideoQCBot`.

**macOS.** Copy `Video QC Bot.app` to `/Applications` (admin) or `~/Applications` (no admin):
`ditto "/Volumes/QC/VideoQCBot/installers/Video QC Bot.app" "/Applications/Video QC Bot.app"`.
MDM: deploy the .app in a pkg (`pkgbuild --component "Video QC Bot.app" --install-location /Applications VideoQCBot.pkg`).
A build runs on the processor it was built on; an Intel build also runs on Apple Silicon through Rosetta.

**Start at login** is per user, from the mascot's menu (Windows: the user's `Run` registry key;
macOS: a LaunchAgent in `~/Library/LaunchAgents`). No admin rights.

### Pointing every PC at the shared folder: videoqcbot-bootstrap.yaml

Without it, each user sees a one-time setup screen asking for the shared folder (or "work alone").
With it, nobody is asked. Put the file in one of these places (first found wins):

| | Next to the app | Machine-wide (GPO / Intune / MDM) |
|---|---|---|
| Windows | the folder with `VideoQCBot.exe` | `%PROGRAMDATA%\VideoQCBot\videoqcbot-bootstrap.yaml` |
| macOS | the folder that contains `Video QC Bot.app`, or inside it in `Contents/Resources/` | `/Library/Application Support/VideoQCBot/videoqcbot-bootstrap.yaml` |

```yaml
# videoqcbot-bootstrap.yaml
shared_data: '\\qcserver\QC\VideoQCBot'       # or '%OneDrive%\QC\VideoQCBot', '/Volumes/QC/VideoQCBot', '~/Library/CloudStorage/...'
shared_models: '\\qcserver\QC\VideoQCBot\models'   # optional; this is the default
shared_reports: '\\qcserver\QC\Reports'        # optional; default <shared_data>/reports
force: false                                    # true = users can't point the app elsewhere
```

Environment variables (`%OneDrive%`, `$HOME`) and `~` are expanded, so one file suits every user.
The file holds **no secrets** and nothing in the build does: there is no API key anywhere in the
installer. (The optional, off-by-default Claude features read `ANTHROPIC_API_KEY` from the user's
environment on the PCs where an admin chooses to allow them.)

## 4. Models and ffmpeg: no downloads on the floor

- **ffmpeg** is inside the app (the `imageio-ffmpeg` package: a static build for Windows, macOS Intel
  and Apple Silicon). Nothing to install. A `machine.ffmpeg_path` in `config.yaml` overrides it.
- **OCR models** are inside the app.
- **Whisper speech models** (base 145 MB, small 465 MB) and the **face model** (232 KB): prepare them
  once on any PC with internet access, then copy to the shared models folder:

  ```
  python scripts/make_offline_bundle.py --out D:\VideoQCBot-models --sizes base small
  robocopy D:\VideoQCBot-models \\qcserver\QC\VideoQCBot\models /E
  ```

  Every PC reads them from there, read-only. A PC only downloads a model itself if it isn't in the
  shared folder (and only if it has internet). The app picks `base` or `small` per PC from its CPU
  and memory (under 4 cores or 8 GB: base); users can override it in Settings > Speech model.

## 5. No internet required

Once the models are in the shared folder, nothing in a normal check uses the network: the
punctuation check that calls the Anthropic API is **off by default** (`checks.punctuation.enabled:
false`) and hidden until an admin turns it on. The app never phones home and never auto-updates.

## 6. Being gentle on editing PCs

- Checks run at **below-normal CPU priority** (Windows `BELOW_NORMAL_PRIORITY_CLASS`, macOS nice 10),
  including the ffmpeg processes they start.
- About **half the CPU cores** are used (`cpu_threads: auto`).
- **One check at a time per PC**, across all users (the lock in `%PROGRAMDATA%` / `/Users/Shared`);
  a second check waits and says so.
- A warning appears when free disk space is under 2 GB or free memory under 1.5 GB.

## 7. Updates: "Check for update"

The app never installs anything itself. To publish a new version:

1. Build it on each OS; put the builds in `<shared>\installers\`.
2. Write `<shared>\latest.json`:

   ```json
   {"version": "0.6.0",
    "windows": "\\\\qcserver\\QC\\VideoQCBot\\installers\\VideoQCBot-0.6.0",
    "macos": "/Volumes/QC/VideoQCBot/installers/Video QC Bot 0.6.0.app",
    "notes": "Audio checks for podcasts."}
   ```

3. Users see a notification once ("Video QC Bot 0.6.0 is available") and, from the menu, *Check for
   update* shows where the installer is. Install it the same way as in section 3 (the user's data is
   kept: it's never inside the app).

Every report shows the app version and a **client rules version** (a hash of the client's file and
its color settings), so two reports can be compared knowing whether the rules were the same.

## 8. Antivirus

Real-time scanning of the files a check writes slows it down (thousands of small cache reads and a
WAV file per video). If checks are slow, exclude:

- Windows: `%LOCALAPPDATA%\VideoQCBot\data\cache\` and `%LOCALAPPDATA%\VideoQCBot\data\shared_cache\`
  (and optionally the process `VideoQCBot.exe` and its bundled `ffmpeg-*.exe` under `_internal\imageio_ffmpeg\binaries\`).
- macOS: `~/Library/Application Support/VideoQCBot/data/cache/`.

Don't exclude the shared folder itself. Some AV products flag unsigned PyInstaller apps; allow-list
the app's folder by path or hash if that happens.

## 9. Unsigned builds: SmartScreen and Gatekeeper

The builds aren't code-signed (a Windows certificate and an Apple Developer ID cost money and need
an account). What users see the first time, and what to do:

- **Windows SmartScreen:** "Windows protected your PC". Click *More info* > *Run anyway*. Once per
  copy. A copy that came from the internet or a chat may also be blocked: right-click the folder >
  Properties > *Unblock*. Copies made with robocopy from a network share usually don't get this.
  Managed fleets: allow the app's path or hash in Defender SmartScreen / App Control.
- **macOS Gatekeeper:** "Video QC Bot can't be opened because Apple cannot check it for malicious
  software". Right-click the app > *Open* > *Open*, or System Settings > Privacy & Security > *Open
  Anyway*. Or remove the quarantine flag once after copying:
  `xattr -dr com.apple.quarantine "/Applications/Video QC Bot.app"`. Signing and notarizing removes
  these steps; see `packaging/README.md`.

## 10. Building: each OS on its own OS

PyInstaller can't cross-build. Build the Windows app on Windows and the Mac app on a Mac (Intel or
Apple Silicon; an Intel build also runs on Apple Silicon). Steps: `packaging/README.md`. Check each
build with `VideoQCBot --selftest` (every library, data file and the bundled ffmpeg) and
`VideoQCBot --check some_video.mp4` before putting it in `installers/`.

## 11. Support

The mascot's menu > *Copy diagnostics* puts the app version, OS, CPU/memory, the floor setup
(shared folder, online or offline, editor or not), ffmpeg, the models found, the settings and the
last 150 log lines on the clipboard (and in `diagnostics.txt` in the log folder). It never includes
an API key, only whether one is set.

Logs: Windows `%LOCALAPPDATA%\VideoQCBot\Logs\videoqcbot.log`, macOS `~/Library/Logs/VideoQCBot/videoqcbot.log`.

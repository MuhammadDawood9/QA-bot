# Video QC Bot

A second pair of eyes for caption QC. It reads the captions burned into a video (or an `.srt`
file), checks them against the QC rules taken from past ClickUp comments, and prints a timestamped
list of issues. See `video-qc-bot-project-plan.md` for the full plan and phase status.

**Built so far:**
- Phase 1: SRT loading, the too-short / phrase-break check, a timestamp parser for
  reviewer-style timecodes, and generation of rules, client and example files from the QC
  review spreadsheet.
- Phase 2: spelling (with glossary and forced spellings), repeated words, letters used as
  digits, capitalization and spacing checks.
- Phase 3: punctuation check by Claude through the Anthropic API (full stops, commas,
  question marks, apostrophes), with caching so reruns are free.
- Phase 4: captions compared with what is actually said (faster-whisper on the CPU): wrong,
  missing and extra words, and captions that appear before the words are spoken.
- Burned-in captions: read straight from the video with OCR (RapidOCR on the CPU), per-client
  caption region, word-by-word animated captions grouped into whole captions.
- Report: a Word document and a Markdown document with a screenshot of every flagged caption,
  plus a CSV of the issues for Excel or ClickUp (`--report`).

## Setup

You need **Python 3.12** (two of the audio packages no longer support 3.11). The same
`requirements.txt` installs on Windows, macOS Intel
and macOS Apple Silicon.

### Windows (PowerShell)

```powershell
cd path\to\Video-qc-bot
python --version                  # must say 3.12
python -m venv .venv              # or: py -3.12 -m venv .venv  (if the py launcher is installed)
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell refuses to run the activate script, run this once and try again:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### macOS (Terminal)

```bash
cd path/to/Video-qc-bot
brew install python@3.12          # skip if python3.12 already works
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Anthropic API key (only if an admin switches the punctuation check on)

The punctuation check is **off by default** (`checks.punctuation.enabled: false` in `config.yaml`):
the normal flow needs no API key and no network, and the check is hidden from the app's menu and
left out of reports. When it is switched on it calls the Anthropic API, which is billed per use. It reads your key
from the `ANTHROPIC_API_KEY` environment variable; the key is never written in code or config.
Without it, the punctuation check is skipped with a warning and every other check still runs.

- Windows, this PowerShell window only: `$env:ANTHROPIC_API_KEY = "sk-ant-..."`
- Windows, permanently: `setx ANTHROPIC_API_KEY "sk-ant-..."`, then open a new window
- macOS, this Terminal window only: `export ANTHROPIC_API_KEY="sk-ant-..."`
- macOS, permanently: add that `export` line to `~/.zshrc`, then open a new window

### ffmpeg (needed to check a video's audio)

- Windows: `winget install ffmpeg`
- macOS: `brew install ffmpeg`

Open a new terminal afterwards so `ffmpeg` is on your PATH. If it still isn't found, put the full
path to the ffmpeg program in `machine.ffmpeg_path` in `config.yaml`.

The first time a video is checked, the Whisper speech model is downloaded into `models/`
(`base` about 145 MB, `small` about 465 MB). After that it works offline.

Activate the virtual environment (`.venv\Scripts\activate` or `source .venv/bin/activate`)
in each new terminal before running the commands below.

## Usage

### 1. Generate the rule files from the spreadsheet

Put the QC review spreadsheet in `data/`, then run:

```
python scripts/prepare_data.py
```

To use a different spreadsheet: `python scripts/prepare_data.py --xlsx data/other.xlsx`

| Output | Source sheet | Notes |
|---|---|---|
| `rules.yaml` | Caption rules | Overwritten each run. Make lasting changes in the spreadsheet. |
| `clients/<CLIENT>.yaml` | Client rules | Client codes are normalized (`2CS` becomes `E2CS`). Your `overrides:` block is kept when you rerun. |
| `data/examples.csv` | Caption mistakes (59) | Timestamps converted to seconds in `start_sec` / `end_sec`. |
| `data/exceptions.yaml` | Accepted exceptions | |
| `glossary.txt` | (none) | Empty starter. Only created if it doesn't exist, so your entries are never overwritten. |

Everything you add to a client file other than `client`, `project` and `notes` (for example
`overrides:` or `glossary:`) is kept when you rerun the script.

### 2. Check a video

```
python -m src.main --video path/to/video.mp4 --client E8D --report
```

The captions are read from the picture (see "Captions burned into the video" below), then
checked. If you do have a caption file, give it instead and the OCR is skipped:

```
python -m src.main --srt path/to/captions.srt --client E8D [--video path/to/video.mp4]
```

- `--client` loads `clients/E8D.yaml`. If there's no file for that client, the defaults in
  `config.yaml` are used and you get a warning.
- `--json` prints the issues as JSON instead.
- `--no-llm` skips the punctuation check: no API calls, no cost.
- `--report` also writes a report folder (see "The report" below).
- `--video path/to/video.mp4` reads the captions from the video when there's no `--srt`, and
  compares them with what is said (see "Captions vs audio" below). Both the captions read and
  the transcript are saved per video, so a second run on the same video starts checking immediately.
- `--save-srt captions.srt` also saves the captions as an `.srt`, to see exactly what the OCR read.
- When the punctuation check runs, a summary line at the end shows how many captions were
  answered from the cache, how many API requests were made and roughly what they cost.
- To try it on the bundled fake captions:
  `python -m src.main --srt tests/fixtures/sample_mistakes.srt --client E8D`

Example output:

```
00:05.650  #4    too_short      high   "the"
    Caption too short: 1 word (minimum 2); on screen 0.13s (minimum 0.50s).
    -> Merge with next caption: "the market moves on its own."
00:22.650  #17   spelling       medium "We work twenty hourse a week"
    Possible misspelling: "hourse".
    -> Did you mean: course, house, hours?
```

Severity tells you how sure the bot is that it's a real mistake: `high` (almost always right),
`medium` (usually right), `low` (worth a glance; often a name to add to the glossary).

### Checking it on the Mac (or any new machine)

One command checks everything the automatic tests can't, and asks you a few yes/no questions at the end:

```
python scripts/mac_check.py              # about 15-20 minutes
python scripts/mac_check.py --quick      # without the slow tests
```

It reports the machine (macOS version, Intel or Apple chip, Retina scaling, menu bar), makes the sample videos
with the Mac's own voice if needed, runs all the tests including the slow ones, checks one video through the app
and times it, puts the mascot on screen in each state (screenshots, CPU use), then starts the app and asks you
about dragging, clicking through the glow, the menu bar, the Dock and dropping a video. Everything ends up in
`mac_check_<date-time>/`: send that folder zipped, or just its `report.md`. macOS asks once whether Terminal may
record the screen (for the screenshots): allow it.

### Checking several videos

Drop several videos on the mascot, or a folder (the videos directly inside it, by name), or pick several with
*Check a video...*: they're checked one after the other. Videos dropped while a check runs join the queue.
In a batch only the last report opens (the others are in the reports folder), and a video that fails doesn't stop
the rest. *Cancel check* stops the current video and goes on; *Clear queue* drops the ones still waiting.

### 3. Run the tests

```
python -m pytest
```

GitHub runs them too (`.github/workflows/tests.yml`): on Windows for every push, and on an Intel Mac for
`main` or when started by hand (Actions → Tests → Run workflow). macOS minutes count 10x on a private
repository, hence the limit. Tests that need client data, the Whisper model or real videos skip themselves there.

### 4. Hide false alarms (the ignore list)

A flag you've decided is fine can be hidden from then on. Every Word report ends with **Hide a flag next
time**: one line per flag, and for an unknown word also the glossary line. If the word is a correct name or brand,
**add it to the glossary** (right-click the mascot → *Edit glossary*, or `glossary:` in the client's file) rather
than ignoring it: the spelling check then also catches misspellings of it. Otherwise copy the ignore line. Copy the line of a false alarm under `ignore:` in `ignore.yaml` (right-click the
mascot → *Edit ignore list*):

```yaml
ignore:
  - {client: E2AB, type: spelling, text: "Gherabel"}                         # this name, in every E2AB video
  - {client: E3AC, type: color_cast, video: "Willy & Mitzie.mp4", at: "2:04"}  # one moment of one video
```

Every field given must match (`client`, `type`, `text` in the caption, `video`, `at` within 2 s); a field left
out matches anything. Shorten `text` to the word that's fine to cover every caption with it. The report's checks
list says how many flags were hidden, so nothing disappears silently.

## Desktop app

A small mascot that floats on top of everything. Drop a video on it, and it checks the video in the
background and opens the Word report when it's done.

```
python -m src.app            # start it
python -m src.app --demo     # also a menu to preview the four mascot states
```

**Using it**
- **Drop a video** (mp4, mov, m4v, mkv, avi, mxf, webm) from Explorer or Finder onto the mascot; a blue
  dashed outline shows it will be accepted. Or **click** the mascot to pick a file.
- A soft halo glows around it: idle pink-violet (static), working blue-violet breathing every 1.4 s with a blue
  progress ring (hover for the stage and percentage), done a bright green pulse that settles back to idle within
  3 s, error steady red. Colors and timings are in the skin's `skin.json` (`"glow"`).
- Hover over it and it blinks, then again every 3 s while the cursor stays. *Skin → Blink while idle* adds a
  random blink every 5-10 s.
- Only the mascot's own shape reacts (hover, click, drag, drop). Around it the window doesn't exist: clicks go to
  whatever is underneath, on Windows and macOS alike.
  You can keep working: the check runs in the background.
- **When it's done** the face is happy with a red badge (the number of issues), a desktop notification
  appears, and the report opens in Word. If the check fails, the face shows X eyes and a message explains
  what went wrong, with a button to open the log file.
- **Right-click** the mascot (or the tray / menu bar icon) for:
  - *Check a video...* and *Cancel check*
  - *Client*: the client the next video is checked for. With *Guess from the file name* on, a code in the
    file name wins ("E8D_reel_03.mp4" is E8D); otherwise the client you picked last is used.
  - *Client > Add new client... / Edit client...*: code, display name, language, minimum words per caption,
    the caption area (*Pick on a video frame...*: drag a rectangle on a frame of a sample video), notes, and
    optional files to import: the client template, the QC review workbook, a .csv or a style guide .pdf (read on
    this PC). Everything found is listed for review, editable, with a tick box; nothing is saved until *Save
    client*. Each save backs up the previous file and adds a line to `changelog.csv` (who, when, what).
  - *Client > Get client template*: saves `client_template.xlsx` (sheets Info, Rules, Glossary, Exceptions,
    Examples, Checks) to fill in and import.
  - *Checks*: switch each check on or off. *Punctuation (AI, costs money)* only appears when an admin has switched
    the API check on in `config.yaml`.
  - *Open last report*, *Open reports folder*, *Settings...* (reports folder, open the report when done,
    notifications, guess the client), *Start at login*, *Quit*.
- On start it checks that ffmpeg is installed and, if not, shows how to install it on your system.
- **Start at login** (right-click menu): Windows adds a startup entry for your user (registry `Run` key, no admin
  rights); macOS adds a LaunchAgent in `~/Library/LaunchAgents`. Untick it to remove them.
- **A real app without Python:** build it with PyInstaller, separately on Windows and on macOS. See
  [`packaging/README.md`](packaging/README.md) for the steps and the first-run security warnings.
- `run_app.pyw` in the project folder starts the app from source by double-clicking (no terminal on Windows).
- **Log file:** Windows `%LOCALAPPDATA%\VideoQCBot\Logs\videoqcbot.log`, macOS `~/Library/Logs/VideoQCBot/videoqcbot.log`.

- **The mascot** is a 120 px frameless, transparent window that stays on top of other windows, has
  no taskbar button (Windows) and no Dock icon (macOS). Drag it anywhere; it reopens where you left it.
  If that spot is no longer on any screen (monitor unplugged), it comes back to the bottom-right corner.
- **States:** idle, working, done (with a red badge showing the number of issues, "99+" over 99) and error.
- **Tray / menu bar icon** with *Show mascot* and *Quit*. Right-clicking the mascot also offers *Quit*,
  so the app can always be closed even where there is no tray.
- **Skins** (right-click → *Skin*; the choice is remembered): *Avatar* (the default) is a round badge;
  *Inspector*, *Cloud* and *Robot* are placeholders. To make a round skin from your own art (open eyes + closed
  eyes, any size): `python scripts/add_avatar_skin.py "open.png" "closed.png" --key avatar --name Avatar --force`.
  It crops inside the circle (dropping soft or ragged edges), lines the closed-eyes drawing up with the open one,
  and writes `skin.json` with `"shape": "circle"`; the app masks the images to a circle when it loads them.
  A skin is a folder `assets/mascot/<name>/` with `idle.png`, `working.png`, `done.png`, `error.png` (120 x 120,
  transparent background, **no glow**: the app draws it), `@2x` versions at 240 x 240 for Retina and high-DPI
  screens, and a `skin.json`:
  - `{"name": "Inspector", "eyes": "frames"}`: the images have eyes; add `<state>_blink.png` (and `@2x`) with them
    closed. A state without a blink image just doesn't blink.
  - `{"name": "Cloud", "eyes": "drawn", "eye_shape": {"left": [46, 64], "right": [74, 64], "radius": 6.5,
    "color": "#24303C", "highlight": "#FFFFFF"}, "state_eyes": {"idle": "open", "working": "focused", "done": "happy",
    "error": "cross"}}`: the images have no eyes and the app paints them at those positions (in the 120 x 120 image).
  To redraw the placeholders: `python scripts/make_mascot_placeholders.py --force`.
- **Settings** are saved in the OS config folder, not in the project:
  Windows `%LOCALAPPDATA%\VideoQCBot\settings.json`, macOS `~/Library/Application Support/VideoQCBot/settings.json`.
- **macOS and full-screen apps:** macOS puts each full-screen app in its own Space. The mascot is set to
  show on every desktop and beside full-screen apps, but macOS doesn't guarantee that for every app: in some
  full-screen apps it won't appear until you leave full screen. On ordinary desktops it is always there.
- **macOS versions:** PySide6 6.11.2 needs macOS 13 or newer. On macOS 11-12 install 6.7.3 (see
  `requirements.txt`).

## Configuration

- **`config.yaml`** holds every threshold. The `machine:` block at the top holds the
  settings that differ between machines (Whisper model size, CPU threads, ffmpeg path).
  Edit only that block when moving to the MacBook.
- **`clients/<CLIENT>.yaml`** holds per-client overrides. Anything under `overrides:` replaces
  the matching value in `config.yaml`'s `checks:` block:
  ```yaml
  overrides:
    too_short:
      min_words: 3
      phrase_break:
        fragment_max_words: 3     # also flag short fragments like "so you even"
  ```
- **`glossary.txt`** lists names, brands and terms the checks must accept. One per line.
  Put `=` in front to force an exact spelling and capitalization:
  ```
  Danja              accepted in any capitalization
  =Mark McGwire      must be written exactly like this ("McGuire", "mark mcgwire" get flagged)
  =YouTube
  ```
  A client can have its own extra terms in its client file: `glossary: ["=Mark McGwire"]`.
  Phase 4 also sends the glossary to Whisper, so keep it to names and special terms.
- **`extra_words.txt`** lists everyday words the spell checker's word list is missing
  ("app", "vlog", "influencer"). Add to it when a normal word keeps getting flagged.
- **Quote words in YAML lists.** Unquoted `yes`, `no`, `on` and `off` are read as true/false.
  The bot stops with an error rather than silently misreading them.

### Captions burned into the video

Without `--srt`, the bot reads the captions off the picture:

1. About 5 frames a second (`ocr.sample_fps`) are taken from the video.
2. Each frame is cropped to the client's caption region (default: the whole frame).
3. The OCR only runs when something in that region changed since the last frame read.
4. Text that isn't a caption is dropped: anything in `ignore_regions`, anything matching
   `ignore_patterns`, and lone letters the OCR isn't sure of.
5. Readings are grouped into captions. Word-by-word animation ("We" -> "We need" -> "We need to")
   becomes one caption with the full text, starting when the first word appears. A highlighted
   key word in another color is joined back into its line. A title elsewhere on screen is
   followed separately, so it doesn't mix into the captions.
6. Each caption's text is the reading most frames agree on. How much they agree is its OCR
   confidence.

**OCR mistakes vs caption mistakes.** The OCR can misread a letter, drop a space between words
or read a comma as a full stop. So on captions read from the video:
- spelling, capitalization, spacing, punctuation and wrong-word flags on a caption the OCR wasn't
  sure of (below `ocr.low_confidence`) are marked `low` with a note to check the screenshot;
- spacing and punctuation flags are always marked that way (`ocr.always_doubtful`), because those
  are the OCR's known weak spots.

**Per client**, in `clients/<CLIENT>.yaml` (same keys as `ocr:` in `config.yaml`). Regions are
fractions of the frame: `top: 0.7` is 70% of the way down.

```yaml
ocr:
  region: {left: 0.05, top: 0.68, width: 0.9, height: 0.27}   # where the captions are
  ignore_regions:                                              # a logo or graphic inside it
    - {left: 0.8, top: 0.68, width: 0.2, height: 0.1}
  ignore_patterns: ['day \d+', 'p ?21']                       # titles that aren't captions
```

- Set a region whenever a client's captions stay in one place. It's faster (a caption strip reads
  in about 0.2 s, a full frame in about 1 s) and keeps titles and on-screen graphics out.
- Leave `region: full` for clients whose text moves (E3RK, below the speaker's chin).
- To set a region, take a screenshot of a caption, and measure where its top and bottom fall as a
  share of the frame height. Leave some room for two-line captions.
- Run with `--save-srt` and compare the `.srt` with the video to see what the OCR read.

Speed on this laptop's CPU, 5 frames a second: roughly 2.5-3x the video's length with a moving
background and the whole frame scanned; a caption region is several times faster. The result is
cached in `cache/ocr/`, so later runs on the same video are instant.

Test videos with burned-in captions in three styles (plain subtitle with a title in the corner,
text on a black box, word-by-word with a yellow key word):

```
python scripts/make_sample_video.py
python scripts/make_burned_in_samples.py
python -m pytest --run-slow tests/test_ocr.py
```

### The too-short / phrase-break check

| Flag | When |
|---|---|
| `too_short` | Fewer than `min_words` words, or on screen less than `min_duration_sec` |
| `phrase_break` | Ends on a dangling word like "the" or "in" (list in config). Short fragments only if `fragment_max_words` is set |

A short word count or a phrase break is excused when the caption is:
- the last caption of a segment (end of file, or followed by a gap of `segment_gap_sec` or more),
- a whitelisted one-word caption ("Wow."),
- next to a pause of `isolation_gap_sec` or more, before or after it.

A caption that only flashes on screen (shorter than `min_duration_sec`) is always flagged.

### Spelling, capitalization and spacing checks

| Issue type | What it flags | Real example |
|---|---|---|
| `spelling` | Words not in the word list, `extra_words.txt` or the glossary. Suggests the most common close words | "hourse" (E2CI) |
| `spelling` | Near-misses of a forced glossary spelling, even when the wrong one is a real word | "McGuire" for =Mark McGwire (E7SS) |
| `spelling` | A letter used as a digit: `2O26`, `O%`, `$O` | "O" is a 0 (E3JB) |
| `repeated_word` | The same word twice in a row, including across two captions. Normal doubles ("that that", "no no") are allowed in config | duplicate "to" (E3JB) |
| `capitalization` | Lowercase "i"; months and days in lowercase; forced glossary terms in the wrong case | "i" (E3JB), "august, leo" (E8C) |
| `capitalization` | Broken mixed case ("realM", "THe"). Brand-style names (YouTube, iPhone, McGwire) are fine | "realM" (E8D) |
| `capitalization` | A very common word capitalized mid-sentence ("and There it was") | "there", "that" (E3RK) |
| `spacing` | Double spaces, no space after a comma or full stop, a space before punctuation, glued words | "andaccountability" (E3RP) |

To keep false alarms down:
- An unknown word starting with a capital is reported as `low` severity. It is usually a name: add it to the glossary.
- The mid-sentence-capital rule only checks very common words (`min_word_frequency`), skips capitals next
  to other capitals ("Warren Buffett", "New York"), and by default ignores the first word of a caption.
  Set `check_caption_starts: true` for clients like E3RK who want flowing sentences.
- A word one typo away from a real word is treated as a misspelling, not as two glued words ("reacton").
- **E8D** has `allow_stylized_lowercase: true` in `clients/E8D.yaml`: lowercase "i", months and names
  aren't flagged, but "realM" still is.
- Every check accepts `min_confidence` in config to drop less certain issues.

Not covered by these text checks: real words that are wrong for what was said ("picked up" for
"peaked up", "Are" for "Or") and missing words need the audio (Phase 4). Commas and full stops are the
punctuation check below. `tests/test_real_examples.py` lists every real example and which phase covers it.

### Punctuation check (Anthropic API)

Where a full stop or comma goes depends on context, so this check asks Claude
(`claude-sonnet-5-5` by default, set in `config.yaml`):

- Each caption is reviewed in a **window**: the caption plus the 2 before and 2 after, with timings,
  so the model can see whether a sentence ends or carries on into the next caption.
- The prompt includes the full-stop and comma rules from `rules.yaml`, the client's rules from
  `clients/<CLIENT>.yaml`, real punctuation mistakes from `data/examples.csv` and the glossary.
- Replies must be JSON. The API enforces the format and the bot also parses it defensively. A failed
  request, a refusal or an unreadable reply is logged as a warning and skipped, never fatal; those
  captions are retried on the next run.
- Answers below `min_confidence` (0.6) are dropped. Answers of 0.8 or more are `medium` severity,
  the rest `low`.
- **Cost control:** 10 captions per request (`windows_per_request`); the fixed part of the prompt is
  cached on Anthropic's side; `effort: low`; and every answer is saved in `cache/punctuation.json` by a
  hash of the caption text, so rerunning on the same captions costs nothing and editing one caption
  only re-sends the windows that contain it. Delete that file to force a fresh review.
- If the model's safety filter declines a request, `fallbacks: default` lets the API retry it on a
  fallback model. Set it to `null` to switch that off.
- Rough cost at current prices: about 1 to 2 US cents per 10 captions, so well under $1 for a
  30-minute video, and nothing on reruns. The CLI prints the real figure after each run.

### The report

```
python -m src.main --srt captions.srt --video video.mp4 --client E3JB --report
```

Each run writes a new folder, `reports/<video name>_<date-time>/` (earlier reports are never overwritten):

| File | What's in it |
|---|---|
| `<video>_QC_<date>.docx` | The report as a Word document, landscape: header (video, captions, client, date, rules version, which checks ran or were skipped and why), a summary of issue counts per type and severity, then one table with a row per issue in time order: time, screenshot of the frame, original text, what's wrong, suggested fix, confidence. An appendix lists all captions, flagged ones bold on yellow. Table headers repeat on every page and rows never split |
| `report.md` | The same issues in Markdown, grouped per caption with one screenshot each |
| `screenshots/` | One frame per flagged caption, taken from the middle of the caption so burned-in text is fully on screen (needs `--video` and ffmpeg) |
| `issues.csv` | One row per issue (time, caption, type, severity, problem, fix, confidence). Opens in Excel with accents and curly quotes intact; usable for a ClickUp import |

Open the `.docx` in Word (Windows or Mac), or `report.md` in VS Code (right-click, *Open Preview*).
Both have the screenshots in place; the Word file carries them inside it, so it can be sent on its own. Problems found by two checks on the same caption (for example a doubled
word found by both the spelling check and the audio check) appear together in one section.
Settings are under `report:` in `config.yaml`: formats (`["md", "docx"]`), Word page size (`a4` or
`letter`), output folder, screenshots on/off, frame position and width, CSV on/off.

### Using the engine from Python

The CLI and the desktop app both call one function:

```python
import threading
from src.engine import run_qc, build_docx_report, QCOptions, QCError, Cancelled

stop = threading.Event()                      # stop.set() from another thread cancels the run

def progress(stage, fraction):                # e.g. ("Reading captions", 0.12); fraction is for the whole run
    print(f"{stage}: {fraction:.0%}")

try:
    issues = run_qc("clip.mp4", "E8D", progress, stop)          # options=QCOptions(...) for more control
    print(len(issues), "issues")
    print(build_docx_report(issues))          # reports/clip_<date-time>/clip_QC_<date>.docx
except QCError as exc:                        # bad input; the message is written for the user
    print(exc)
except Cancelled:
    print("cancelled")
```

`QCOptions` takes `srt_path` (use a caption file instead of reading the video), `use_llm`, `check_color`,
`disabled_checks` (check names from `src/checks/__init__.py`, or `"color"`) and `config_path`. The engine never prints:
messages go to the `qcbot` loggers.

### Evaluating the checks against your notes

```
python scripts/evaluate.py --make-template      # drafts data/ground_truth.csv + data/ground_truth_videos.csv
python scripts/evaluate.py                      # after filling it in; videos are looked up in data/samples/ and data/
python scripts/evaluate.py --videos "D:/QC videos" --tolerance 2
python scripts/evaluate.py --checks captions    # only the caption checks (or: color)
```

The template takes the spreadsheet's caption mistakes (spelling, punctuation and capitals, too-short captions; the
creative requests are left out because the bot can't check them) and its color notes. `ground_truth_videos.csv` lists
the videos those notes are about, the ones with the most timed notes first: collect those, write each file name in its
`video_file` column for your own reference, and put the files in `data/samples/` (or pass `--videos`).

In `data/ground_truth.csv`, replace each `TODO <task name>` with the video's file name, check the guessed `issue_type`,
and delete rows for videos you don't have. Caption types: `spelling`, `punctuation`, `too_short`, `timing`, or `caption`
when the note doesn't say which; color types: the eight color checks, or `color_grading`. A flag matches a note when the
note's time falls in the flagged caption or shot ±2 s; caption notes only match caption flags. The script prints recall,
precision and false alarms per minute per check, and writes `reports/evaluation_<date-time>/` with `evaluation.md`,
`misses.csv` and `false_alarms.csv` so you can look at each one. The paid punctuation check isn't used, and no threshold
is changed.

### Captions vs audio (Whisper)

The first check downloads the speech model once (base: about 145 MB, small: about 465 MB). Meanwhile the
mascot's tooltip and the terminal say "Downloading the speech model (one time, ...)" instead of "Transcribing".

```
python -m src.main --srt captions.srt --video video.mp4 --client E3JB
```

1. ffmpeg extracts the audio (mono, 16 kHz).
2. faster-whisper transcribes it on the CPU with word timings (`int8`, voice-activity filter,
   beam size 1). Names from `glossary.txt` are given to Whisper as a hint. The model size is
   `machine.whisper_model_size` in `config.yaml`: `base` while testing, `small` for real use.
3. The transcript is cached in `cache/transcripts/`, keyed by a hash of the video file (plus the
   model and settings), so reruns are instant. Renaming the file doesn't matter; editing it does.
4. Both texts are normalized (lowercase, no punctuation, numbers as words, "gonna" = "going to",
   fillers like um/uh dropped) and each caption is lined up with the words spoken from 1 s before
   it starts to 1 s after it ends.

| Issue type | Example (from the real QC notes) |
|---|---|
| `wrong_word` | Wrong word: caption says "Are you", audio sounds like "Or you". |
| `missing_word` | Missing word "but": caption says "hard, it", audio sounds like "hard, but it". |
| `extra_word` | Extra word "to" (repeated): caption says "need to to", audio sounds like "need to". |
| `caption_early` | Caption appears 1.3s before "Bright" is spoken. |
| `caption_late` | Caption appears 1.3s after "We" is spoken. |
| `caption_sync` | All captions are about 3.1s late. Reported once for the whole video. |
| `uncaptioned_speech` | 66 spoken words with no caption, 00:05.0 to 00:28.1, with the words to add. |

**Sync first.** Before comparing words, the bot tries shifting all the captions by up to 5 s
(`max_offset_sec`) and keeps the shift where the most caption words line up with the audio. If
that's clearly better than no shift, it reports one `caption_sync` issue and compares everything
else with the captions moved back. Without this, captions that are all 3 s late would show up as
every caption being late and every word missing.

**Speech with no caption** is a stretch of at least `uncaptioned_min_sec` (1.5 s) and
`uncaptioned_min_words` (4) of clearly heard speech that no caption on screen covers. It has no
caption number; the report lists it as "no caption".

To keep false alarms down:
- Only words Whisper heard clearly count as evidence (`min_word_probability`, 0.7). For an extra
  word, the two words around it must have been heard clearly on average.
- Spellings that are almost the same count as the same word ("hourse"/"hours", "McGuire"/"McGwire");
  the spelling check covers those.
- A stutter the caption leaves out ("to to") isn't a caption mistake.
- Captions that are mostly not spoken at all (titles, on-screen text) are skipped.

**Try it on a test video with known mistakes:**
```
python scripts/make_sample_video.py
python -m src.main --srt data/samples/synthetic_sample.srt --video data/samples/synthetic_sample.mp4 --no-llm
python -m pytest --run-slow tests/test_sample_video.py
```
The script uses the computer's own text-to-speech (Windows or macOS) to read a script, burns the
captions into the picture, and writes a caption file with 6 planted mistakes from the real QC notes.
`synthetic_sample_expected.json` lists them. Add `--report` to the second command to see a full report.

### Names vs on-screen graphics

When a video is given, the bot also reads the whole frame every 2 s (`ocr.screen_text_every_sec`)
for text that isn't a caption: logos, name titles, lower thirds. Text that stays on screen longer
than `ocr.max_caption_sec` (12 s) is treated as a graphic, not a caption.

A capitalized caption word that is one or two letters away from a rare word in those graphics is
flagged as `screen_text_mismatch`: the logo says "MAIER", the caption says "Meyer". Common words
("State", "Representative") never trigger it. It's `high` when the next word is also a name
from the graphics ("Elizabeth Meyer" under "ELIZABETH MAIER").

### Color grading

Runs whenever a video is given (`--no-color` skips it). The video is cut into shots where the
colors jump, and every shot is measured 4 times a second, leaving out the client's caption region
(white caption text would otherwise count as blown highlights). Black screens are skipped. Cuts are
looked for on every sampled frame and brightness and color measured on each; faces are looked for on every
4th frame of a shot, counted from its first (`face_every_n_frames`).

| Issue type | What it means | From the QC notes |
|---|---|---|
| `overexposed` | 4%+ of the picture clipped pure white (10%+ = high) | "too bright", "highlights clipped" (45 notes) |
| `underexposed` | Middle brightness under 40/255 and no real highlights | "too much dark" |
| `crushed_blacks` | 15%+ of the picture pure black | "crushed shadows" |
| `flat_contrast` | Lifted, grey blacks, or a narrow tonal range | "flat contrast with lifted black levels" (32) |
| `saturation` | Almost grey, or oversaturated | "colors are dull" (31) |
| `color_cast` | Whites and greys tinted magenta, green, warm or cool | "the magenta is going too much" (23) |
| `skin_tone` | Skin on detected faces too pink, too yellow/green, too orange, or grey | "skin color yellow" (43) |
| `shot_mismatch` | Skin tone jumps from the previous shot with a face, or a shot's tint/saturation is far from the rest of the video | "alien to the rest of footage", "color change when the camera changes" |

Pixels are converted to RGB using the video's own color tags; untagged HD video is read as BT.709 and limited range, as players do. **HDR (PQ, HLG) and BT.2020 videos are not measured**, because every threshold assumes SDR Rec.709; the report says so. Measuring takes about 20 s per minute of 1080p video, most of it decoding the video.

Faces are found with OpenCV's YuNet model (232 KB, downloaded once into `models/`, checksum checked).
All thresholds are in `color.yaml`, each with a comment saying what it means. These are measurements,
not taste: a deliberate look (a grey, desaturated sequence) gets flagged too, so a client with such a
style switches the matching flag off (or changes any threshold) under `clients:` in the same file:

```yaml
clients:
  E3AC:
    skip: ["saturation"]
```

Test video with planted grading mistakes (one per shot, made with ffmpeg filters from any
well-graded video with a face):

```
python scripts/make_color_samples.py --source path/to/well_graded.mp4
python -m pytest --run-slow tests/test_color.py
```

## Project layout

```
config.yaml            thresholds and machine settings
rules.yaml             caption rules (generated)
glossary.txt           names and terms that must not be flagged, and forced spellings
extra_words.txt        everyday words missing from the spell checker's list
clients/               per-client rules and overrides (generated + your overrides)
data/                  spreadsheet, examples.csv, exceptions.yaml, samples/ (git-ignored)
scripts/prepare_data.py
scripts/make_sample_video.py  synthetic test video with known caption mistakes
src/
  main.py              CLI
  models.py            Caption and Issue dataclasses
  config.py            config and client loading
  inputs/srt_loader.py
  checks/too_short.py
  checks/spelling.py   spelling, repeated words, letters used as digits
  checks/capitalization.py
  checks/spacing.py
  checks/punctuation.py  windows, prompt, batching, JSON parsing, caching (Anthropic API)
  checks/context.py    glossary, word list, rules and examples shared by the checks
  utils/timecode.py    HH:MM:SS:FF, drop-frame, M:SS and range parsing
  utils/glossary.py    glossary.txt loading
  utils/dictionary.py  pyspellchecker word list + glossary + extra_words.txt
  utils/text.py        word positions, edit distance, sentence breaks
  report/builder.py    report data, report.md and issues.csv
  report/docx_writer.py  report.docx (python-docx)
  report/screenshots.py  frames from the video with ffmpeg
  utils/llm.py         Anthropic API client: request, refusals, token usage and cost
  utils/cache.py       JSON file cache keyed by text hash
  checks/transcript.py captions vs audio: alignment and the four issue types
  audio/transcribe.py  ffmpeg audio extraction, faster-whisper, transcript cache
  utils/normalize.py   text normalization for comparing captions with speech
  utils/system.py      ffmpeg (argument lists, never a shell), file hashing
cache/                 cached API answers and transcripts (git-ignored)
reports/               generated reports (git-ignored)
models/                Whisper models (git-ignored)
tests/                 unit tests (the API and Whisper are faked); fixtures/sample_mistakes.srt (planted
                       mistakes), clean_sample.srt (none), punctuation_mistakes.srt.
                       test_sample_video.py runs the real pipeline with --run-slow
```

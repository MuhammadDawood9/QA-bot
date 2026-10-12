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

### Anthropic API key (for the punctuation check)

The punctuation check calls the Anthropic API, which is billed per use. It reads your key
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

### 3. Run the tests

```
python -m pytest
```

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
| `report.docx` | The report as a Word document: header (video, captions, client, date, rules version, which checks ran or were skipped and why), a summary table of issues by type and severity, an index of flagged captions with clickable times, one section per flagged caption (screenshot, caption text, every problem with what's wrong, suggested fix and confidence), and an appendix of all captions with the flagged ones bold on yellow. A4 pages with page numbers; each caption's section stays on one page; table headers repeat on every page |
| `report.md` | The same report in Markdown |
| `screenshots/` | One frame per flagged caption, taken from the middle of the caption so burned-in text is fully on screen (needs `--video` and ffmpeg) |
| `issues.csv` | One row per issue (time, caption, type, severity, problem, fix, confidence). Opens in Excel with accents and curly quotes intact; usable for a ClickUp import |

Open `report.docx` in Word (Windows or Mac), or `report.md` in VS Code (right-click, *Open Preview*).
Both have the screenshots in place; the Word file carries them inside it, so it can be sent on its own. Problems found by two checks on the same caption (for example a doubled
word found by both the spelling check and the audio check) appear together in one section.
Settings are under `report:` in `config.yaml`: formats (`["md", "docx"]`), Word page size (`a4` or
`letter`), output folder, screenshots on/off, frame position and width, CSV on/off.

### Captions vs audio (Whisper)

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

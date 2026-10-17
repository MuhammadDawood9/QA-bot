# Video QC Bot: Project Plan

A file-based assistant that scans a finished video, finds caption and text errors (and later color-consistency issues), and produces a timestamped QC report I can read afterward.

---

## 1. Goal

I work as a QA reviewer for videos. I catch caption mistakes, color grading problems, and creative-direction issues. This tool acts as a second pair of eyes: I run it on the video file while I do my own QC pass, and when it finishes I get a document listing every issue it found, with timestamp, screenshot, explanation, and suggested fix.

**Success looks like:** the bot catches most of the repetitive mistakes I already comment on in ClickUp, with few enough false alarms that I keep using it.

---

## 2. Scope

### In scope (V1)
- Caption checks:
  - **Too-short captions** (a single word on screen; minimum is 2–3 words)
  - **Spelling mistakes**
  - **Punctuation mistakes** (missing or wrong full stops, commas, etc.)
- Timestamped report document with screenshots

### Planned later (V2+)
- Shot-to-shot color consistency checks (measured, not guessed)
- Out-of-range values (clipped highlights, crushed blacks, saturation spikes)
- Creative-direction checks against a written brief or style guide
- Pull past QC comments from ClickUp automatically
- Side-panel or small web UI

### Out of scope for now
- True live capture while the video plays
- Judging calibrated color by eye (a vision model can't replace scopes and a calibrated monitor)
- Audio and subtitle sync

---

## 3. Key decision: file-based, not live

The bot processes the **video file** (or caption file), not a live screen capture.

Reasons:
- Caption errors are easier to catch with exact start and end times for every caption.
- The report needs a complete pass over the video.
- Rules can be tweaked and rerun on the same file without replaying the video.
- Cheaper and more accurate than analyzing a continuous stream.

Workflow: I start the bot on the file when I begin my own QC. The report is ready by the time I finish, and I compare my notes with the bot's.

---

## 4. Open question that changes the build

**Are captions burned into the video, or do I also receive a caption file (SRT / VTT) from the editors?**

| Situation | Approach | Difficulty |
|---|---|---|
| I get an SRT/VTT file | Parse the text and timings directly, no OCR | Easy, most accurate |
| Captions are burned in only | Detect frames, OCR the text, rebuild caption timings | Harder, needs tuning |
| Both are available | Use the SRT for text checks, and OCR a sample of frames to confirm the burned-in version matches the SRT | Best coverage |

The project should be built so the **checks don't care where the captions came from**. Both input paths produce the same internal data structure (see Section 6). Build the SRT path first if available, because it's faster to get working.

---

## 5. Architecture

```
            +-------------------+
 video ---> |  Input layer      |
 (+ SRT?)   |  (SRT parser OR   |
            |  frame OCR)       |
            +---------+---------+
                      |
                      v
            +-------------------+
            |  Caption list     |   [{id, start, end, text}, ...]
            +---------+---------+
                      |
          +-----------+-----------+
          v           v           v
   +-----------+ +-----------+ +-----------+
   | Length    | | Spelling  | | Punctuation|
   | check     | | check     | | check (LLM)|
   +-----+-----+ +-----+-----+ +-----+------+
         |             |             |
         +-------------+-------------+
                       v
              +------------------+
              |  Issue list      |  [{time, type, severity, text, fix}]
              +--------+---------+
                       v
              +------------------+
              |  Report builder  |  -> DOCX / PDF / MD with screenshots
              +------------------+
```

---

## 6. Core data structures

### Caption
```json
{
  "id": 14,
  "start": 32.480,
  "end": 33.120,
  "text": "Thank",
  "source": "srt"
}
```
- `start` / `end` in seconds
- `source` is `"srt"` or `"ocr"`

### Issue
```json
{
  "caption_id": 14,
  "timestamp": 32.480,
  "type": "too_short",
  "severity": "high",
  "original": "Thank",
  "message": "Caption has 1 word. Minimum is 2.",
  "suggestion": "Merge with next caption: 'Thank you so much'",
  "confidence": 0.95
}
```

Issue types: `too_short`, `spelling`, `punctuation`, later `color_shift`, `brand_violation`, etc.

---

## 7. Modules

### 7.1 Input layer

**Path A: SRT/VTT parser**
- Library: `pysrt` or `webvtt-py`
- Output: caption list directly

**Path B: Burned-in captions via OCR**
1. Use `ffmpeg` to sample frames (start at 4 fps, tune later).
2. Crop to the caption region (configurable, e.g. bottom 30% of the frame) to cut noise and cost.
3. Run OCR on each cropped frame. Options: PaddleOCR or EasyOCR (local, free), or a vision model API for higher accuracy.
4. Group consecutive frames with the same text into one caption, with start and end times.
5. Ignore frames with no text. Use a small similarity tolerance (fuzzy match) because OCR varies slightly between frames.

Tip: skip unchanged frames by comparing a perceptual hash of the caption region to the previous frame. Only run OCR when it changes.

### 7.2 Too-short caption check (rule-based, no AI needed)

- Count words per caption: `len(text.split())`
- Flag if `word_count < MIN_WORDS` (default 2, configurable per client)
- **Exceptions to reduce false positives:**
  - Last caption of a video or segment
  - Caption that ends a sentence with a very short word group where merging is not possible (e.g. before a long pause)
  - Whitelisted one-word captions (intentional emphasis, e.g. "Wow.")
  - Optional: ignore if the gap before and after is larger than X seconds (intentional pause)
- Suggestion logic: propose merging with the previous or next caption if the combined duration is short enough.

### 7.3 Spelling check

- Library: `pyspellchecker`, `symspellpy`, or `language_tool_python`
- **Custom glossary** (`glossary.txt`): brand names, product names, people, and technical terms that must not be flagged
- Also keep a **forced-spelling list** for names with required capitalization (e.g. `YouTube`, `iPhone`)
- Strip punctuation and handle contractions before checking
- Detect likely **OCR misreads** separately in Path B (e.g. `rn` vs `m`, `0` vs `O`) and mark those as lower confidence

### 7.4 Punctuation check (LLM-based)

This is where the AI model earns its place. Rules like "where does the full stop go" depend on context, so send captions in **windows**, not one at a time.

- Window: the caption under review plus 2 before and 2 after, with timings.
- Provide my punctuation rules and real examples from ClickUp in the prompt.
- Ask for **structured JSON output only**.
- Batch several windows per request to control cost.

**Prompt skeleton:**

```
You are a QC reviewer for video captions.

RULES:
{punctuation_rules}

EXAMPLES OF REAL MISTAKES (wrong -> correct):
{examples_from_clickup}

GLOSSARY (never flag these terms):
{glossary}

Below are consecutive captions with timestamps. Review ONLY caption #{target_id}.
Use the surrounding captions to decide if a sentence ends or continues.

CAPTIONS:
{window}

Respond with ONLY JSON, no preamble, no markdown fences:
{"has_issue": true|false, "issue_type": "punctuation", "original": "...",
 "suggestion": "...", "explanation": "...", "confidence": 0.0-1.0}
```

- Parse the JSON safely (strip code fences if present, handle failures by logging and skipping).
- Drop issues below a confidence threshold (start at 0.6, tune in testing).
- Cache results by caption text hash so reruns don't pay twice.

### 7.5 Report builder

Output a document with:

1. **Header:** video name, date, rules version, total caption count
2. **Summary:** issue counts per type and severity
3. **Issue table (sorted by timestamp):**
   - Timestamp (`mm:ss.ms`)
   - Screenshot of the frame (extracted with `ffmpeg -ss <t> -frames:v 1`)
   - Original caption text
   - What's wrong
   - Suggested fix
   - Confidence
4. **Appendix:** full caption transcript with flagged lines highlighted

Formats: start with **Markdown** (simplest, can convert later), then **DOCX** (`python-docx`) or **PDF**. Optionally also export **CSV** for importing into ClickUp.

---

## 8. Using my ClickUp comments

My past QC comments are the most valuable asset for tuning the bot.

**Steps:**
1. Export comments (CSV export, or the ClickUp API later). Include: video/task name, timestamp (if I wrote one), the comment text.
2. Categorize each comment: `too_short`, `spelling`, `punctuation`, `color`, `creative`, `other`.
3. Count the frequency of each category to confirm priorities.
4. Pull out the **repeated patterns** and write them as explicit rules (Section 7.4 prompt, `rules.yaml`).
5. Save the best 20–40 real examples as wrong → correct pairs for the prompt.
6. Keep a **test set**: a handful of past videos plus the comments I made on them. This is the ground truth.

**Later (V2):** use the ClickUp API to auto-pull comments and auto-post the bot's findings as a task comment.

---

## 9. Configuration

`config.yaml`
```yaml
input:
  mode: srt            # srt | ocr | both
  caption_region: [0.0, 0.70, 1.0, 1.0]   # x1, y1, x2, y2 as fractions (OCR mode)
  sample_fps: 4

checks:
  too_short:
    enabled: true
    min_words: 2
    max_gap_for_exception_sec: 1.5
    whitelist: ["wow", "yes", "no"]
  spelling:
    enabled: true
    glossary_file: glossary.txt
    language: en
  punctuation:
    enabled: true
    model: claude-sonnet-4-6
    window_before: 2
    window_after: 2
    min_confidence: 0.6

report:
  format: md           # md | docx | pdf
  include_screenshots: true
  output_dir: reports/
```

Keep per-client overrides in separate files (e.g. `clients/clientA.yaml`) since minimum word counts and style rules may differ.

---

## 10. Tech stack

| Purpose | Choice |
|---|---|
| Language | Python 3.11+ |
| Video and frames | `ffmpeg` (via `ffmpeg-python` or subprocess), OpenCV |
| SRT parsing | `pysrt` |
| OCR (Path B) | PaddleOCR or EasyOCR; vision model API as an upgrade |
| Spelling | `pyspellchecker` / `symspellpy` / `language_tool_python` |
| LLM | Anthropic API (`anthropic` Python SDK) |
| Config | `pyyaml` |
| Report | Markdown first, then `python-docx` or `reportlab` |
| CLI | `typer` or `argparse` |
| Tests | `pytest` |

---

## 11. Suggested repo structure

```
video-qc-bot/
├── README.md
├── config.yaml
├── glossary.txt
├── rules.yaml
├── requirements.txt
├── clients/
│   └── example_client.yaml
├── data/
│   ├── test_videos/
│   └── clickup_export.csv
├── src/
│   ├── main.py                # CLI entry point
│   ├── models.py              # Caption, Issue dataclasses
│   ├── inputs/
│   │   ├── srt_loader.py
│   │   └── ocr_loader.py
│   ├── checks/
│   │   ├── too_short.py
│   │   ├── spelling.py
│   │   └── punctuation.py
│   ├── report/
│   │   ├── screenshots.py
│   │   └── builder.py
│   └── utils/
│       ├── cache.py
│       └── llm.py
├── reports/
└── tests/
    ├── test_too_short.py
    ├── test_spelling.py
    └── fixtures/
```

---

## 12. Build plan (milestones)

### Milestone 1: Skeleton + too-short check
- [ ] Set up repo, virtual env, `requirements.txt`
- [ ] Define `Caption` and `Issue` models
- [ ] Build SRT loader
- [ ] Build too-short check with config and whitelist
- [ ] CLI: `python -m src.main video.mp4 --srt captions.srt`
- [ ] Print issues to the terminal
- [ ] Unit tests for the too-short rules

### Milestone 2: Spelling
- [ ] Integrate spellchecker
- [ ] Add glossary and forced-spelling support
- [ ] Tests with real examples from ClickUp

### Milestone 3: Report
- [ ] Extract screenshots at issue timestamps
- [ ] Markdown report with summary and issue table
- [ ] Appendix with full transcript

### Milestone 4: Punctuation with the LLM
- [ ] Prompt with rules and real examples
- [ ] Windowed context, batching, JSON parsing, caching
- [ ] Confidence threshold
- [ ] Run against the test set and record results

### Milestone 5: Burned-in caption support (if needed)
- [ ] Frame sampling and caption-region cropping
- [ ] OCR plus grouping into captions
- [ ] Change detection to skip unchanged frames
- [ ] Compare OCR captions to the SRT when both exist

### Milestone 6: Testing and tuning
- [ ] Run on past videos, compare against my ClickUp comments
- [ ] Measure **recall** (how many real issues it caught) and **precision** (how many flags were real)
- [ ] Adjust rules, thresholds, and exceptions
- [ ] Decide which check types are trustworthy enough to rely on

### Milestone 7 (V2): Color consistency
- [ ] Shot-change detection (PySceneDetect)
- [ ] Per-shot metrics: average luminance, white balance, saturation, clipped highlights, crushed blacks
- [ ] Flag shot-to-shot jumps above a threshold
- [ ] Optional vision-model commentary on flagged shots

### Milestone 8 (V2): Creative direction
- [ ] Write the brief or style guide as structured input
- [ ] Vision-model pass on sampled frames checked against the brief

### Milestone 9 (V2): Integrations and UI
- [ ] ClickUp API: pull comments, post findings
- [ ] Simple web or desktop UI to pick a file and view the report

---

## 13. Testing and evaluation

**Test set:** 5–10 past videos with my actual ClickUp comments as ground truth.

**Metrics:**
- **Recall** = issues the bot found ÷ issues I originally commented on
- **Precision** = real issues ÷ everything the bot flagged
- **False positive rate** per video (aim for a number I can live with, e.g. under 10 flags that I have to dismiss per video)

**Process:**
1. Run the bot on each test video.
2. Compare its list to my comments by timestamp (± 1–2 seconds).
3. Label each bot flag as true positive, false positive, or "valid but I'd never comment on it."
4. Group the false positives by cause and fix the rules.
5. Repeat until stable.

**Tuning levers:** minimum word count, exception rules, glossary size, LLM confidence threshold, prompt examples.

---

## 14. Risks and how to handle them

| Risk | Mitigation |
|---|---|
| Too many false positives, so I stop using it | Start cautious (high confidence only), tune with the test set, add exceptions |
| OCR misreads text and creates fake spelling errors | Mark OCR-derived issues as lower confidence; prefer SRT when available |
| Brand names flagged as typos | Maintain `glossary.txt`, load per client |
| LLM output is not valid JSON | Strict "JSON only" prompt, safe parsing, skip and log failures |
| API cost grows on long videos | Cache results, batch windows, only send captions that pass cheap checks first |
| Color judgments differ from my eye | Use measured metrics, not model opinions; treat as "flag for me to review" |
| Different clients have different rules | Per-client config files |

---

## 15. Questions to answer before or during Milestone 1

1. Do I get an SRT/VTT from the editors, or is it burned-in only?
2. What is the exact minimum word count per client (2 or 3)?
3. What language(s) are the captions in?
4. What video lengths and volumes (per day/week) should this handle?
5. Where will I run it: my own machine only, and with what hardware (matters for local OCR)?
6. Which report format do I prefer to read: Markdown, Word, or PDF?
7. Can I export my ClickUp comments now so we can categorize them?

---

## 16. Instructions for Claude when coding this project

Paste this section at the start of a coding session:

> I'm building the "Video QC Bot" described in this plan. Work milestone by milestone, starting with Milestone 1. Keep code modular so checks are independent of where captions come from. Use dataclasses for `Caption` and `Issue`. Write unit tests for every rule-based check. Keep all thresholds and rules in config files, not hardcoded. Ask me before adding new dependencies. After each milestone, summarize what works and what's next.

---

## 17. Build phases (current) and decisions log

The build now follows four phases from the kickoff prompt. Where the prompt and this plan disagree, the prompt wins.
The report document, color checks, OCR, the ClickUp API and any UI are on hold until after Phase 4.

| Phase | Scope | Status |
|---|---|---|
| 1. Foundation | Repo, venv, config, data prep from the xlsx, timestamp parser, models, SRT loader, too-short / phrase-break check, CLI, tests | **Done (2026-10-12)** |
| 2. Spelling, capitalization, spacing | pyspellchecker + glossary.txt, forced spellings, rule-based checks from rules.yaml, client rules | **Done (2026-10-12)** |
| 3. Punctuation via Anthropic API | Windowed prompts (Section 7.4), JSON-only output, confidence threshold, caching, batching, key from `ANTHROPIC_API_KEY` | **Done (2026-10-12)**, tested with a fake API only; first live run pending |
| 4. Whisper transcript vs captions | faster-whisper (CPU, int8), cached transcripts, glossary as initial_prompt, alignment, wrong/missing/extra/early-word flags | **Done (2026-10-12)**, tested on a synthetic sample video; real-video run pending |
| Report (Milestone 3) | Report with screenshots, summary, appendix; CSV export | **Done (2026-10-12)**: Word (.docx) + Markdown + CSV; PDF not built |
| Burned-in captions (Milestone 5) | OCR of the picture, per-client region, word-by-word grouping | **Done (2026-10-12)**, tested on synthetic videos and one real video |
| Sync, uncaptioned speech, names vs graphics | New caption checks found on the first real video | **Done (2026-10-12)** |
| Color grading (Milestone 7) | Per-shot measurements and flags from the 166 color notes | **Done (2026-10-12)**, thresholds not yet evaluated on real notes |
| Desktop companion app | Steps 1-4 (see below) | **Steps 1-4 done (2026-10-12)**; Windows build tested, macOS build untested |

### Machines
- Develop on Windows (Lenovo ThinkPad), run on a MacBook (specs to come). All code is cross-platform: pathlib everywhere,
  UTF-8 for every text file, ffmpeg found with `shutil.which` and called with argument lists, no PyTorch.
- Machine-specific settings live in the `machine:` block of `config.yaml` (Whisper model size, cpu_threads, ffmpeg path).

### Decisions made in Phase 1
- **Python 3.12**, virtual environment in `.venv/`. Versions are pinned in `requirements.txt`.
- **Dependencies:** PyYAML, openpyxl (approved for reading the xlsx), pytest. The SRT parser uses only the standard
  library, so `pysrt` was not needed.
- **Data source:** `data/QC_Review_Analysis_Jun-Oct_2026.xlsx` replaces `clickup_export.csv`. `scripts/prepare_data.py`
  generates `rules.yaml` (17 rules), `clients/<CLIENT>.yaml` (10 clients), `data/examples.csv` (59 examples) and
  `data/exceptions.yaml` (5 entries). Rerunning keeps each client's hand-written `overrides:` block.
- **Client codes are normalized:** `2CS` becomes `E2CS`. Codes that aren't E+number+letters (`S_UP`, `disc_Echo`) are only upper-cased.
- **Timestamps:** the data has frame numbers up to 59, so some timelines are 60 fps. `config.yaml` sets `timecode.fps: 30`.
  `prepare_data.py` switches a timestamp to 60 fps when its frame field is 30 or more. Semicolon timecodes are converted as
  true drop-frame (29.97 or 59.94). A bare number like `35-36` is rejected as ambiguous rather than guessed.
- **Too-short check also checks time on screen.** Real comments ("the" came for few frames, "hormonal development" passing
  too fast) are about duration, so `min_duration_sec` (0.5 s) sits next to `min_words` (2). A caption that only flashes
  by is flagged even if an exception would otherwise apply.
- **New issue type `phrase_break`:** a caption ending on a dangling word ("...turn the"). A second rule, for short fragments
  with no punctuation ("so you even", E8D), is **off by default** (`fragment_max_words: 0`) because many social styles use
  2–3 word captions on purpose. Switch it on per client.
- **Exceptions:** last caption of a segment (end of file, or a gap of 2.0 s or more after it), whitelisted one-word captions,
  and a gap of 1.5 s or more before *or* after the caption. All configurable.
- **Merge suggestions:** prefer the neighbour that completes the sentence. Two fragments in a row are merged with each other.
  No merge is suggested across a gap over 1.0 s or when the result would exceed 6 s / 12 words.
- **CLI:** `python -m src.main --srt file.srt --client E8D` (plus `--json`). The video file argument comes back in Phase 4.
- **YAML gotcha:** unquoted `yes`/`no`/`on` load as booleans. `config.yaml` quotes all list words, and the check raises a
  clear error if it gets a boolean.
- **Punctuation model:** `config.yaml` names `claude-sonnet-5-5` as a placeholder. Confirm it in Phase 3.

### Decisions made in Phase 2
- **pyspellchecker 0.9.1** (pure Python) over symspellpy. Its word list loads in about 0.3 s and the checks run 35 minutes
  of captions (1,200) in about 1 s. Its rarest entries are often junk ("heth"), all at a count of 50, so they are never suggested.
- **Three new checks, four new issue types:** `spelling` (unknown words, near-misses of forced glossary spellings, letters used
  as digits), `repeated_word`, `capitalization`, `spacing`. Each is independent of the caption source and has its own block in
  `config.yaml`. Every check takes `(captions, settings, context)`; the context carries the glossary and the word list.
- **Glossary format:** one term per line, `=` prefix for a forced exact spelling (`=Mark McGwire`). A client file can add
  its own lines under `glossary:`. `prepare_data.py` now keeps every key in a client file except `client`, `project`, `notes`.
- **`extra_words.txt`** (new file) holds everyday words missing from pyspellchecker's list. About half of a sample of modern
  words were missing (app, selfie, influencer, playlist). Kept separate from the glossary because Phase 4 sends the glossary
  to Whisper, which only accepts a short prompt.
- **Forced spellings catch real-word lookalikes:** "McGuire" is in the word list, so only the glossary can catch it as a
  misspelling of "McGwire". Common words inside forced phrases ("Mark") are ignored for this.
- **False-alarm controls:** unknown capitalized words are `low` severity ("a name to add to the glossary?"). The
  mid-sentence-capital rule only looks at very common words (frequency 100,000 or more: "That", "There") and skips capitals
  next to other capitals ("Warren Buffett"). A word one typo away from a real word is reported as a misspelling, not as two
  glued words. `tests/fixtures/clean_sample.srt` (names, brands, numbers, links) must produce only the two low name questions.
- **E8D** gets `allow_stylized_lowercase: true` in `clients/E8D.yaml`: lowercase "i", months and glossary names are not
  flagged, but broken mixed case like "realM" still is.
- **Caption-start capitals** (E3RK: do not capitalize "there" at the start of a caption that continues a sentence) are off by
  default (`check_caption_starts: false`), because many styles capitalize every caption.
- **`may` and `march`** are not flagged in lowercase (common words). Capitalized, they are never flagged.
- **Every check accepts `min_confidence`** in config; lower-confidence issues are dropped.
- **Real-example coverage:** of the 41 spelling and punctuation rows in `data/examples.csv`, 17 are caught by Phase 2 checks
  (tested in `tests/test_real_examples.py`). The other 24 are assigned to Phase 3 (commas, full stops, possessives), Phase 4
  (real words wrong for the audio, missing words) or marked as not detectable from text (title capitals, client style
  preferences, layout), each with its reason. New spreadsheet rows make that test fail until they're classified.
- **Not built (out of Phase 2's text-only reach):** title capitalization (the SRT doesn't mark titles), and a rule for
  lowercase sentence starts (left to the Phase 3 punctuation pass).

### Decisions made in Phase 3
- **Model `claude-sonnet-5-5`** (config `checks.punctuation.model`). The original plan named `claude-sonnet-4-6`; Sonnet 5.5
  is the current Sonnet in the same tier and is cheaper ($2 / $10 per million input / output tokens versus $3 / $15).
  `claude-opus-5-5` ($4 / $20) is the stronger option if quality needs it.
- **SDK `anthropic==1.13.0`**, all its dependencies pinned. They ship wheels for Windows, macOS Intel and Apple Silicon;
  no PyTorch.
- **Request shape:** effort `low` (recommended starting point for classification on Sonnet 5.5) with thinking left at
  its default; `max_tokens` 16,000; structured outputs (`output_config.format` with a JSON schema) so replies are valid
  JSON; the system prompt (rules, client rules, examples, glossary) is marked for prompt caching because it is identical
  for every request in a run. The skeleton from Section 7.4 is kept, plus `target_id` so several windows fit in one request.
- **Refusal fallback on by default** (`fallbacks: default`, beta `server-side-fallback-2026-07-01`, Claude API only): if a
  safety classifier declines, the API reruns the request on a fallback model. A refusal that still comes back is
  logged and skipped. Set `fallbacks: null` to switch it off.
- **Batching:** 10 windows per request (`windows_per_request`). Each window is the target plus 2 captions before and 2 after,
  with timings. The Message Batches API (50% cheaper, but results can take minutes to hours) was not used, because the
  report should be ready by the time the manual QC pass ends.
- **Caching:** answers are stored in `cache/punctuation.json`, keyed by a hash of the model, effort, prompt version, full
  system prompt, and the window's caption texts and pauses (not absolute times). Reruns are free; editing one caption only
  re-sends the windows that contain it. Failed captions are not cached, so they are retried next run.
- **Failures never stop the run:** bad JSON, unknown target IDs, refusals, cut-off replies and API errors are logged as
  warnings and the batch is skipped. An authentication or permission error stops the punctuation check (every request
  would fail). Without `ANTHROPIC_API_KEY` the check is skipped with setup instructions for both systems; `--no-llm`
  skips it on purpose.
- **Prompt scope:** only full stops, commas, question marks, apostrophes, and capitals caused by a wrong full stop.
  Rules from the `Full stops` and `Commas` areas of `rules.yaml`. Real examples are `punctuation`-category rows whose
  text mentions a punctuation mark (9 rows), because that category in the sheet also holds capitalization and spacing
  comments that Phase 2 already covers. To save output tokens, the model leaves the text fields empty for fine captions.
- **Confidence:** `min_confidence: 0.6` drops answers; 0.8 or more is `medium` severity, the rest `low`.
- **Cost estimate printed after each run** from `price_per_mtok` in config. Expected roughly 1 to 2 US cents per 10
  captions, so well under $1 for a 30-minute video. Needs checking against a real run.
- **Tests never call the real API:** `tests/conftest.py` removes `ANTHROPIC_API_KEY` for every test; the API is faked.

### Decisions made in Phase 4
- **Python 3.12 only** (was "3.11 or 3.12"): PyAV and numpy no longer publish wheels for 3.11.
- **Pinned and checked on all three platforms** (Windows, macOS Intel, macOS Apple Silicon, Python 3.12) with a dry-run
  install of the whole `requirements.txt` for each Mac type: faster-whisper 1.2.1, ctranslate2 4.8.2, rapidfuzz 3.14.6.
- **onnxruntime held at 1.23.2:** it is the last version with macOS Intel wheels (1.24+ is Apple Silicon only). faster-whisper
  uses it for the voice-activity filter. It can move up once the MacBook is known to be Apple Silicon.
- **PyAV held at 18.1.0:** PyAV 19 removed an argument faster-whisper 1.2.1 passes, which crashed the first real run. The bot
  also no longer uses faster-whisper's decoder: ffmpeg writes a 16 kHz mono WAV and the bot reads it with Python's `wave`.
- **ffmpeg** installed on the Windows machine with `winget install ffmpeg` (Gyan.FFmpeg 9.0.2). `machine.ffmpeg_path` in config
  can point to it if it isn't on PATH.
- **Whisper settings** as specified: CPU, int8, word timestamps, VAD filter, beam size 1, model size from
  `machine.whisper_model_size` (`base` default). Models download once into `models/`; later runs load them offline.
- **Transcript cache:** `cache/transcripts/<sha256 of video>-<model>-<settings hash>.json`. Renaming a video keeps the cache;
  changing the content, model, language, VAD, beam size or glossary prompt re-transcribes. CPU threads don't affect the key.
- **Glossary → Whisper:** `initial_prompt = "Glossary: term, term."`, capped at 600 characters.
- **Normalization (both sides):** lowercase, punctuation and apostrophes removed, numbers written in digits become words
  ("40%" → "forty percent", "$2,500", ordinals, decimals; also Whisper's "40 %"), an `equivalents` table in config
  (gonna/going to, ok/okay, alright/all right...), fillers dropped (um, uh, ah, er, hmm...). No new dependency for number words.
- **Alignment:** a small edit-distance alignment written for this, not difflib. Spoken words at the window edges are
  free, so words belonging to neighbouring captions aren't blamed on this one. On ties it prefers a wrong word over an
  extra-plus-missing pair. rapidfuzz decides near-identical spellings (ratio of 85 or more, words of 4+ letters) count as the
  same word. The window is the caption's time ±1 s. Spoken words that no caption accounts for and that are said while a
  caption is on screen count as missing from that caption ("in the same direction" shown as "same").
- **Four issue types**: `wrong_word`, `missing_word`, `extra_word` (marked "(repeated)" for duplicates), `caption_early`
  (0.7 s or more early). Every message reads `caption says "X", audio sounds like "Y"` with the full heard line as the suggestion.
- **Evidence rule:** wrong and missing words need every spoken word involved at probability 0.7 or higher. Extra words have
  no spoken word, so the average probability of the two neighbours is used (the minimum missed a real case in the sample:
  a 0.64 neighbour hid the extra "is").
- **Not flagged on purpose:** stutters the caption leaves out ("to to"), captions mostly not spoken at all (titles, on-screen
  text: under half the words heard), and words the spelling check already covers.
- **Synthetic sample video** (`scripts/make_sample_video.py`, user's choice): Windows SAPI or macOS `say` reads 10 lines; the SRT
  plants 6 real mistakes (Are/Or, missing "but", duplicate "to", extra "is", picked/peaked, caption 1.2 s early) plus a
  title and 4 clean lines. With Whisper `base`: all 6 found, no false alarms, 38.6 s of video transcribed in about 5 s on the
  i7-8650U. End-to-end test: `pytest --run-slow tests/test_sample_video.py`.
- **Overlap with Phase 2:** a duplicated word is reported twice (text-only `repeated_word` and audio `extra_word`). Left as is
  for now; the report milestone can merge issues on the same caption.

### Decisions made for the report (Milestone 3)
- **Formats: Word + Markdown + CSV** (`report.formats` in config). Word added on request with **python-docx 1.2.0** (named
  in the plan; it and lxml 6.1.3 checked to install on Windows, macOS Intel and Apple Silicon). Both documents come
  from the same prepared data, so they always say the same thing.
- **Word layout:** A4 by default (`page_size: letter` possible), 2 cm margins, Calibri 10 pt, built-in Title/Heading
  styles (Word's Navigation Pane works), grid tables with fixed column widths, shaded header rows that repeat on every
  page, rows that don't split across pages, severity in colour (high red, medium orange, low grey), the caption text in
  the Quote style, screenshots embedded at 12 cm, each caption's section kept on one page, clickable times and caption
  numbers that jump to the caption's section (bookmarks), flagged appendix rows bold on light yellow, footer with the
  title and page number. Control characters in caption text are removed so the file is always valid.
- **Checked in real Word:** the file passes the Office XML schema check, and it was opened in Microsoft Word on the
  Windows machine, exported to PDF and inspected page by page. That found two layout problems, now fixed: a too-narrow
  Confidence column and caption sections splitting across pages.
- **`--report` flag**; each run writes `reports/<video or srt name>_<YYYYmmdd-HHMMSS>/` and never overwrites an earlier one.
- **Grouped per caption, not per issue:** one section, one screenshot, all problems for that caption, worst first. This
  also resolves the Phase 4 overlap (doubled word reported by both `repeated_word` and `extra_word`).
- **Screenshots** with `ffmpeg -ss <t> -i video -frames:v 1` (fast seek, argument list), 640 px wide JPEG, taken at the
  **middle** of the caption by default (`screenshot_at: start` is the alternative) so burned-in text is fully visible. A
  failed frame is noted in the report and never stops it.
- **Header lists every check and why one was skipped** (no API key, `--no-llm`, no `--video`, off in config), so a quiet
  report can't be mistaken for a clean video.
- **CSV** in UTF-8 with a byte-order mark so Excel shows curly quotes correctly; raw caption text, not Markdown-escaped.
- **Synthetic sample now has captions burned into the picture** (ffmpeg `subtitles` filter, run from the output folder so
  Windows paths need no escaping; falls back to a plain picture if the filter is missing), so its screenshots show the
  caption the issue is about, and it is ready as OCR test material later.

### Decisions made for burned-in captions (Milestone 5, 2026-10-12)
- **Videos come with captions burned in and no SRT**, so reading them off the picture is the main input path. `--srt` stays
  for the rare file that has one.
- **OCR: RapidOCR (`rapidocr_onnxruntime` 1.4.4)**: PaddleOCR models on the onnxruntime already pinned for Whisper; the
  models ship inside the wheel, so nothing downloads at run time. Pulls in opencv-python, Pillow, pyclipper, shapely.
  **Tesseract is not used.** (The desktop-app spec mentions detecting Tesseract on startup; with RapidOCR there is nothing
  to detect, only ffmpeg.)
- **Frames from PyAV** (already installed for Whisper), 5 a second, cropped to the client's caption region (`ocr:` block in
  `clients/<CODE>.yaml`; default the whole frame). The OCR only runs when a tile of the region changed.
- **Grouping:** text that grows word by word is one caption; a highlighted key word in its own box joins its line; titles
  are tracked separately; the caption's text is the reading most frames agree on, which also gives an OCR confidence.
  Text on screen longer than 12 s is a logo or title, not a caption.
- **OCR doubt:** text issues on captions the OCR read unsurely, and spacing/punctuation issues on any OCR caption (its known
  weak spots), are made low severity with a note to check the screenshot.

### Decisions made for the checks added after the first real video (2026-10-12)
- **Whole-video sync** (`caption_sync`): the captions are tried at shifts up to ±5 s; a clearly better shift is reported once
  and the word comparison runs on the shifted captions. **Late captions** (`caption_late`) next to early ones.
- **Speech with no caption** (`uncaptioned_speech`): 1.5 s and 4 words or more of clearly heard speech no caption covers.
  Such issues have negative caption ids (-1, -2...) and show as "no caption" in the report.
- **Names vs on-screen graphics** (`screen_text_mismatch`): the whole frame is read every 2 s for logos and titles; a
  capitalized caption word within 2 letters of a rare word on screen is flagged ("Meyer" under a "MAIER" logo).

### Decisions made for color grading (Milestone 7, 2026-10-12)
- Built from the 166 color notes in the QC spreadsheet. Shots are found by color-histogram cuts (no PySceneDetect); each
  shot is measured 4 times a second at 480 px wide, without the caption region, and flagged for: overexposed, underexposed,
  crushed blacks, flat contrast, saturation, color cast (tint of near-neutral pixels, or the brightest unclipped ones when
  the cast is strong), skin tone (faces found with OpenCV's YuNet model, 232 KB, downloaded once with a checksum), and shots
  that don't match (skin hue jumps between face shots, tint or saturation far from the video's typical shot). Black screens
  are skipped. A client with a deliberate look switches a type off with `color: {skip: [...]}`.
- **Color space:** frames are converted using their own tags. Untagged frames get what players assume: BT.709 for HD
  (wider than 1279 or taller than 576 px), BT.601 for SD, limited range unless tagged full. (libav's default would read
  every untagged frame as BT.601; for HD that bends skin hues a few degrees.) **HDR (PQ, HLG) and BT.2020 footage is not
  measured**: every threshold assumes SDR Rec.709, so such a video gets a "color skipped" note instead of wrong numbers.
- **Cost (measured):** about 30 s of analysis per minute of 1080p video at 4 samples a second on the i7-8650U: ~10 s
  decoding, ~105 ms per sampled frame for the measurements (mostly percentiles over 400k pixels), ~35 ms per face check.
- **Thresholds are untuned** beyond a test video with planted mistakes (all 6 found, 0 false alarms; `del.mp4`: 0 flags).
  `scripts/evaluate.py` measures them against real notes (below) before anything is changed.

### Decisions made for the desktop companion app (2026-10-12)
Steps, stopping after each for testing: 1 engine entry point + report, 2 floating mascot window, 3 interactions,
4 polish and packaging (only on "go step 4"). Not built: live screen capture, a dashboard window, cloud features.
Windows 10/11 and macOS (Intel, also Apple Silicon); OS-specific code only in `src/platform/` behind one interface.
- **Step 1 (done):** `src/engine.py` holds the whole pipeline. `run_qc(video_path, client, progress_cb=None,
  cancel_event=None, *, options=None, config=None)` returns the issue list (a `QCResult`, which is a list with the captions,
  client, context and check summary attached). `progress_cb(stage, fraction)` gets the **overall** fraction 0..1; stages:
  Reading captions, Reading on-screen text, Transcribing audio, Checking captions, Checking color. `cancel_event`
  (a `threading.Event`) is checked at every frame, Whisper segment and API batch, and raises `Cancelled`. Bad input raises
  `QCError` with a message for the user. The CLI is now a thin wrapper around `run_qc`.
- **The engine no longer prints.** Progress goes through the callback, messages through the `qcbot` loggers; the CLI
  prints both. This keeps the engine UI-independent.
- **Word report reworked to the spec:** landscape A4, header, summary of counts per issue type and severity, then one
  table with a row per issue: time, screenshot, original text, what's wrong, suggested fix, confidence; appendix of all
  captions. Saved as `reports/<video>_<date-time>/<video>_QC_<date>.docx`; `build_docx_report(result)` returns the path.
  (The per-caption sections and links of the first Word version are gone; Markdown and CSV are unchanged.)
- **Step 2 (done):** the floating mascot in PySide6. **PySide6-Essentials 6.11.2** (the core of PySide6; the newest
  version with an Intel-Mac wheel, universal2, needs macOS 13+; macOS 11-12 would use 6.7.3) and **platformdirs 4.13.0**,
  both named in the spec. `src/app/` holds the UI, `src/platform/{windows,macos,other}.py` everything OS-specific
  (window flags, Dock hiding, tray icon), chosen once by `sys.platform`. Window: Tool + frameless + stays-on-top +
  translucent, 120 logical px; macOS also gets WA_MacAlwaysShowToolWindow, "join all Spaces / full-screen auxiliary",
  and the accessory activation policy (no Dock icon) through the Objective-C runtime via ctypes, so no pyobjc.
  High-DPI: pass-through scaling and 1x/@2x images loaded together in a QIcon (checked at 150% and 300%).
  Placeholder art from `scripts/make_mascot_placeholders.py`; monochrome template tray icon for the macOS menu bar,
  color icon for Windows. Position saved in `settings.json` in the OS config folder; restored only if at least half
  the window lands on a screen. `--demo` adds a tray submenu to preview the states until Step 3 drives them.
  A right-click "Quit" on the mascot covers systems without a tray. **Not testable here:** the macOS parts
  (Dock, Spaces, menu bar icon) run only on a Mac; they fail soft (warning, Qt defaults) if a native call fails.
- **Step 3 (done):** drop videos on the mascot (it shows a dashed outline when the drag holds a video) or click it for a
  file picker (remembers the folder). The check runs `run_qc` + `build_docx_report` on a QThread (`src/app/worker.py`);
  progress arrives as a Qt signal (throttled to 0.5% steps) and draws a ring on the mascot; cancel sets the engine's
  cancel event. One check at a time; a second drop is refused with a notification. Finished: done state with the
  count, tray notification (Qt's `showMessage`), report opened with `QDesktopServices.openUrl`. Failed: error state,
  notification, a dialog with the message and "Open log file". Unexpected exceptions are caught in the worker and
  logged with their traceback, so the app stays up.
- **Right-click and tray menus are the same menu**, rebuilt on every open: status line, check a video, cancel, client
  (guess from file name + radio list of client files + default rules), check toggles, open last report, open reports
  folder, settings dialog, quit.
- **Client choice:** a client code in the file name wins when guessing is on; otherwise the client picked last in the
  menu; otherwise default rules. A guessed client doesn't change the remembered one.
- **The AI punctuation check is off by default in the app** (it costs money per video); a toggle switches it on.
- **Settings** (`settings.json` in the OS config folder): position, last client, guess on/off, checks switched off,
  reports folder, open report when done, notifications, last report, last folder. A value of the wrong type falls back
  to its default. **Log file** in the OS log folder (rotating, 1 MB x 3), with the engine's warnings captured.
- **Startup tool check: ffmpeg only**, with install steps per OS from the platform module. Tesseract isn't checked
  because nothing uses it (RapidOCR does the OCR); waiting on a decision.
- **Windows shortcuts** (made on request, before Step 4's packaging): Start Menu and Desktop "Video QC Bot" run
  `.venv\Scripts\pythonw.exe -m src.app` from the project folder, with `assets/app/videoqcbot.ico`.
- **Step 4 (done):** working animation (the face bobs 3 px over 1.2 s, ~30 fps timer that runs only while working).
  Start at login in the platform modules: Windows `HKCU\...\CurrentVersion\Run` value via `winreg` (per user, no admin;
  the Startup-folder shortcut would need COM or a shell command), macOS a LaunchAgent plist via `plistlib`
  (`RunAtLoad`, Aqua sessions only); the entry is refreshed at each start so a moved app still launches.
  From source the entry runs `pythonw.exe run_app.pyw`; packaged, the program itself.
- **Packaging: PyInstaller 6.22.3** (named in the spec), one spec for both OSes (`packaging/videoqcbot.spec`), one-folder
  builds (faster start than one-file for ~490 MB). The Mac `.app` has `LSUIElement` (no Dock icon) and
  `LSMinimumSystemVersion` 13.0. Data files: config, rules, clients, glossary, extra words, assets, plus the OCR
  models, Whisper's VAD model, the spell checker's word lists and python-docx's template from their packages. Not
  bundled: ffmpeg (licensing and size; the startup check explains how to install it) and the Whisper/face models
  (downloaded on first use). **Built and tested on Windows:** `--selftest` passes and `--check` ran a full check of
  `del.mp4` from the `.exe`. **The macOS build is untested** (no Mac here).
- **A packaged app writes nothing inside itself:** on first run the editable files are copied into the OS data folder
  (`platformdirs.user_data_path`) and never overwritten; config, cache and models are read from there, and reports
  default under the app's own `data\reports` (`%LOCALAPPDATA%\VideoQCBot\data\reports` on Windows,
  `~/Library/Application Support/VideoQCBot/data/reports` on macOS; changeable in Settings). Running from source is
  unchanged.
- **macOS PATH:** apps started from Finder or at login don't see Homebrew's folders, so the macOS platform module adds
  `/opt/homebrew/bin` and `/usr/local/bin` to the app's PATH (otherwise a Homebrew ffmpeg isn't found).
- `--selftest` and `--check VIDEO` let a build be verified without clicking through it.
- **Not done:** code signing (Windows certificate, Apple Developer ID and notarization), so first runs show SmartScreen
  and Gatekeeper warnings (steps in `packaging/README.md`); an installer program; a native Apple Silicon build.
- **Round avatar skin and halo glow (2026-10-13):** new default skin `avatar` from the user's art
  (`mascot/*.png`, 1254 px) via `scripts/add_avatar_skin.py`: finds each image's opaque disc (it was off-center
  with a ragged fringe), aligns the closed-eyes drawing to the open one (they're separate drawings: difference
  36.8 → 15.6), makes the disc fully opaque (the art had alpha 253) and saves 120/240 px frames.
  `skin.json` `"shape": "circle"`: images masked to the inscribed circle at load (antialiased textured-brush
  ellipse; files untouched), window mask = the circle, hit test = distance from the center, so only the circle
  (and the badge) takes hover, click, drag and drop. Glow rebuilt: a halo outside the circle edge (85% at the
  edge, gone 14 px beyond), a faint dark shadow under it (reads on white) and a 2 px brighter ring on the border
  at full strength. Each color pre-rendered once per pixel ratio into a QPixmap; frames only set opacity and
  scale. One QVariantAnimation: working breath (1 - cos, 1.4 s, looping), done pulse (450 ms flash, back to the
  idle glow by 3 s); stopped otherwise. Colors/strengths/timings in `skin.json` `"glow"` (defaults:
  `assets.DEFAULT_GLOW`, each value validated). Idle now has a soft pink-violet glow (was none). **Mascot shrunk
  to 88 px** (was 120; skins still drawn at 120, drawn eyes scaled), PAD 32, window 152. Measured on Windows:
  idle/error/done-settled 0 repaints, working ~36 glow repaints/s, under 1% of one core. **macOS untested.**
- **Mascot skins, glow and blinks (2026-10-12):** skins are folders `assets/mascot/<skin>/` with a `skin.json`
  (`eyes`: `frames` with `<state>_blink.png`, or `drawn` with eye positions, size, colors and a style per state);
  placeholders: inspector (frames), cloud (drawn), robot (the first art, frames). Chosen in the menu, remembered.
  **Two windows:** the sprite window is masked to the sprite's shape (alpha ≥ 50%, plus the badge), which makes that
  shape the one hit region for hover, click, drag and drop and lets clicks around it through on both OSes (a mask
  removes those pixels from the window; transparency alone is click-through only on Windows, and only at alpha 0,
  so a semi-transparent glow would block clicks). The glow window (176 px, sprite + 28 px padding each side) sits
  behind it with `WindowTransparentForInput`, draws the radial-gradient halo, the progress ring and the drop
  highlight, and is never clipped by the mask. Glow animated with QPropertyAnimation on opacity and radius: working
  blue-violet pulse (1.8 s, looping), done green fade-out (2.5 s), error steady red, idle none. **Measured on
  Windows: about 1.3% of one core extra while pulsing (62 glow repaints/s); idle: no repaints, no timers.** The
  Step 4 bob was dropped: the pulse is the working animation. One QTimer drives every blink: on hover, 120 ms closed,
  repeat every 3 s while hovered; optional idle blink every 5-10 s (off by default). Checked on the real Windows
  desktop (screen capture over other windows): transparent background, unclipped glows, badge. **macOS untested.**
- `guess_client(video, known)` finds a client code in a file name ("E8D_reel.mp4", "8d - reel.mov") for Step 3.

### Evaluation of the color checks (2026-10-12)
- `scripts/evaluate.py` reads `data/ground_truth.csv` (video, timestamp, client, issue_type, note_text);
  `--make-template` drafts it from the spreadsheet's 166 color notes with a guessed issue type. A flag matches a note
  inside its shot ±2 s. Per type: recall, precision, false alarms per minute, and lists of misses, false alarms and
  "right moment, different type" flags. A `color_grading` note (no specific problem) is matched by any color flag.
- **Not run on real videos yet:** the notes' videos aren't on this machine. Results so far are only the planted test video.
- **Caption checks added (2026-10-13):** notes of type spelling / punctuation / too_short / timing / caption are
  matched by caption flags only (the caption's span ±tolerance), grouped into families: spelling = spelling,
  repeated, wrong, missing, extra word, screen-text names; punctuation = punctuation, capitalization, spacing;
  too_short = too_short, phrase_break; timing = early, late, sync (whole video), uncaptioned speech. The bot's own
  type names and the spreadsheet's categories work as issue_type. Caption flags come from `run_qc` with color and
  the paid LLM check off. `--checks all|captions|color`. The template now adds the 45 checkable caption mistakes
  (14 creative/other requests left out) and writes `ground_truth_videos.csv`: the 46 videos to collect, most timed
  notes first (top: E3AC weddings for color; E3RK, E2CS, E8D for captions). Smoke-tested on del.mp4 (4 caption
  flags, cached run 5 s). The spreadsheet has no caption-timing notes; timing can only be measured from notes added
  by hand.

### Improvements 2-6 from the review (2026-10-13)
- **Worker crash (5):** two bugs in `CheckRunner`. (a) The job's finished/failed/cancelled signals stopped the
  thread through a queued `quit()`, delivered only by the main event loop, so `wait()` on Quit during a check
  blocked for its whole timeout and the QThread was then freed while running, which aborts the process
  (0xC0000409). Now a direct connection (`quit()` is thread-safe). (b) `_cleanup` dropped the last reference on
  `finished`, which Qt emits just before the thread ends; it now waits for the thread first. Tests use a runner
  fixture that always waits. Step 3 tests: 5/5 clean runs (were crashing on most).
- **Ignore list (2):** `ignore.yaml` (editable file; `paths.ignore_file`), rules of client / type / text / video
  / at (within 2 s), all given fields must match; text matching ignores case and spacing. Applied in `run_qc`
  after all checks; hidden flags in `QCResult.hidden` and counted in the report's checks list. The Word report
  ends with "Hide a flag next time", one ready line per flag (caption flags by their text, color and
  no-caption flags by video and time). Tray: Edit ignore list. Broken rules are logged and skipped; a rule with
  no condition is refused (it would hide everything). The old `data/exceptions.yaml` is free-text history and
  isn't used.
- **Faster color (3):** `frame_stats` rewritten per channel with OpenCV (identical numbers: largest difference 0);
  measurement every 2nd sampled frame of a shot and faces every 4th, counted per shot (before: faces every 2nd
  frame counted over the whole video, so a short shot could get none); cuts still at 4 fps, so short shots aren't
  merged. COLOR_VERSION 3. Analysis 4.7-4.9x faster on del.mp4 and color_test.mp4 with the same flags (per-shot
  values within 0.2 levels, skin hue within 1.3°); with decoding about 2x overall (~15 s per minute of 1080p).
  Not done: 2 fps cut detection (could merge 0.5-1 s shots), 240 px measuring (clipping shares would shift),
  skipping non-reference frames when decoding (no gain on del.mp4, which has no B-frames).
- **Model download message (4):** `Stage.say(label)` shows a temporary label at the same progress; the Whisper
  loader says "Downloading the speech model (one time, about N MB)" when the model isn't on disk yet. The
  packaged app still downloads it on first use (bundling it would add 145-465 MB to the app).
- **CI (6):** GitHub Actions, Python 3.12, ffmpeg installed: windows-latest on every push, macos-15-intel (the
  bot's Mac is Intel) for main and manual runs only (macOS minutes cost 10x on a private repo). A clean copy of
  the tracked files passes locally: 472 passed, 10 skipped (client data, Whisper, slow tests).

### One decode per video (2026-10-13)
- `src/inputs/video_pass.py` `read_video_once`: the caption OCR, the on-screen text OCR and the color pass used to
  decode the whole video each. Their loops are now feed-one-frame objects (`CaptionReader`, `ScreenTextReader`,
  `ColorAnalyzer`; the old functions wrap them), and `shared_frames` decodes once, handing each pass exactly the
  frames `sample_frames` would at its rate. Only passes missing from their caches run; results go into those
  caches, so the engine's usual calls load them (new first stage "Reading the video", 62% of the bar). The on-screen
  text pass keeps every distinct reading and filters out the captions at the end, which gives the same list. If the
  single pass fails, the engine logs it and the passes read the video one by one as before.
- **Measured (empty caches): identical captions, on-screen text and color measurements; only 4-9% faster**
  (del.mp4, 30 s: 356 s -> 326 s; the two samples 125 -> 120 s and 170 -> 157 s). The estimate of a third was
  wrong: decoding was the biggest cost of the color pass alone, not of a whole check.
- **Where the time really goes: the caption OCR.** With region `full` (the default, as for del.mp4) the picture
  changes in almost every sampled frame, so the OCR reads the whole 1080x1920 frame 5 times a second (~2 s each).
  A caption region per client (the OCR then reads only that strip, and only when it changes) is the big lever.

- **Rebuilt app (2026-10-13):** 484 -> 437 MB by leaving out OpenCV's ffmpeg video reader (29 MB; PyAV decodes)
  and Qt's software OpenGL (20 MB). The encoders in av.libs stay: libavcodec loads them at start. Packaged
  --selftest all good; packaged --check of burned_box.mp4 found the planted mistakes with one decode for all three
  passes ("OCR ran on 183 of 197 frames": the change detection rarely skips a frame).

### Faster OCR, a queue, glossary lines (2026-10-13)
- **OCR 1.4-2.7x faster (2):** with limit_type "max", RapidOCR 1.4.4 ignores det_limit_side_len and picks 960 /
  1500 / 2000 from the image size, so a 1080x1920 frame was searched for text at 1920x1088 (2.4 s) instead of
  960 px (0.5 s): `det_max_side` never applied to full frames. Bigger frames are now shrunk to det_max_side for
  finding text, and each line is still read from the full-size frame (RapidOCR's own det call with use_rec=False,
  then its get_crop_img_list and text_rec). Shrinking the whole frame instead lost spaces ("forSTATE").
  Measured: del.mp4 287 -> 105 s; the samples 106 -> 70, 121 -> 86, 104 -> 69 s.
- **Final caption text chosen by letters, not words:** the new engine dropped spaces in other frames than the old
  one, which exposed a weakness: complete readings with dropped spaces ("MarkMcGwirehit", 1 word) looked shorter
  than incomplete ones ("Mark McGwire"), which won. Completeness is now the letter count, votes are on the text
  without spaces, and the winning text's reading with the most word breaks gives the spacing. OCR_VERSION 3.
  Against the truth SRTs of the three samples: 46 of 49 captions exact (old: 45); the 3 left were wrong before
  too (spaces in a big stylized title, a comma read as a full stop).
- **Queue (3):** dropping or picking several videos or a folder (its videos, by name, not subfolders) checks
  them one after the other; drops while busy join the queue. In a batch only the last report opens, and a failure
  is a notification instead of a blocking dialog. Cancel check stops the current one and goes on; Clear queue
  drops the rest. `CheckRunner.idle` fires once the thread is gone, which starts the next.
- **Glossary lines (4):** `spelling.flagged_word` gets the unknown word back from the flag's message. The ignore
  line for it is now just that word (so it covers every caption with it), and the report's last table has an "Or
  add to the glossary" column; tray menu Edit glossary. The glossary is the better fix for a correct name: it also
  catches misspellings of it.

### Machine check for the Mac; two corrections (2026-10-13)
- **`scripts/mac_check.py` (step 7):** the real Mac run can't be done from the Windows laptop, so one script
  does it there: environment (macOS, chip, Qt platform, screens and scaling, tray, ffmpeg, the heavy libraries),
  sample videos made with `say` if missing, the whole test suite with `--run-slow`, one timed `--check`, the
  mascot in each state on the real screen (screenshots, CPU idle/working), then the app plus 12 yes/no questions
  (glow without a square, sharpness, hover blink, click-through, drag, menu bar, no Dock icon, right-click,
  drop, done, Spaces, quit). Output: `mac_check_<date-time>/report.md` + logs and screenshots (gitignored).
  Rehearsed on Windows (`--quick --no-questions`): all steps ran.
- **Color measured on every frame again:** the rehearsal found 9 issues in burned_box.mp4 where the packaged app
  found 10. Shot 2's black levels were [0, 50, 50, 52, 52, 52, 0] (an edge frame of each neighbouring shot);
  every 2nd frame gives [0, 50, 52, 0], median 25 instead of 50, and its flat-contrast flag was lost. The test
  videos of the earlier comparison have clean cuts, so it didn't show there. `measure_every_n_frames: 1` (the
  option stays); faces stay at every 4th frame (skin hue within 1.3 deg on the test video). The color stage is
  about 3x faster than before today's work instead of 4.8x.
- **Packaged app's config gets new settings:** the user's config.yaml copy is never replaced (edits survive
  updates), so an installed app never saw a new setting; this one still measured color the old way. Now
  `load_config` lays the user's copy over the config.yaml the app ships with: theirs wins, new keys arrive. A
  setting already in an old copy keeps its old value (an old default can't be told from an edit), e.g. the
  installed copy's `face_every_n_frames: 2`.

### Open questions
- Set `ocr.region` for each client whose captions sit in a fixed place: it's what makes the OCR fast.
- Fill `data/ground_truth.csv` and put the matching videos (`data/ground_truth_videos.csv` lists them, most useful
  first) where the evaluation can find them.
- The desktop-app spec asks to detect Tesseract; the bot uses RapidOCR (pip, no separate install). Drop that
  requirement, or is Tesseract wanted for something else?
- Is a PDF version also wanted? (Word can save the .docx as PDF in the meantime.)
- Run on a real client video and its SRT (realistic voices, music, several speakers).
- First live run of the punctuation check: confirm the cost and quality on a real caption file.
- MacBook specs (sets `whisper_model_size` and `cpu_threads`).
- Minimum words per client: every client uses 2 for now. Should any use 3?
- Which clients should get the fragment rule (`fragment_max_words: 3`)? E8D is the only real example so far.
- Should E3RK get `check_caption_starts: true`?
- Start filling `glossary.txt` (and per-client `glossary:` lists, e.g. E7SS athlete names) with the names that come up.

---

## 18. First steps (today)

1. Create the repo and virtual environment.
2. Export ClickUp comments to `data/clickup_export.csv`.
3. Answer the SRT-vs-burned-in question.
4. Collect 3–5 sample videos plus their caption files if available.
5. Start Milestone 1 with Claude.

---

## 19. Floor rollout: phases 1-4 (started 2026-10-13)

The app goes on every PC on the QC floor, used by many people on PCs that also edit and QC. Four
phases, run in one go; each ends with the full test suite green and a commit "Phase N: ...".
Not built on purpose: ClickUp sync, creative-direction checks, PDF reports.

### Decisions and assumptions
- **Pending work committed first** (a951add): the full-frame OCR reuse and the wider Confidence column were
  uncommitted when the rollout started; they got their own commit so Phase 1 starts clean.
- **Punctuation (API) off by default** (Phase 1): `checks.punctuation.enabled: false` in config.yaml. While off
  it isn't in the Checks menu (`choices.visible_toggles`) and the report's checks list doesn't mention it
  (`CheckContext.configured` remembers what config said before the run's own switches). When an admin turns
  it on, a run switched off from the menu now says "switched off for this run" (was "skipped (--no-llm)").
  The code, the CLI's `--no-llm` and the API key handling are unchanged behind the flag.
- **Offline**: with the API check off nothing in a run uses the network once the Whisper model and the 232 KB
  face model are on disk (Phase 3 adds a shared models folder and an offline bundle, so even that first
  download isn't needed).
- **color.yaml** (Phase 1): every color threshold moved out of config.yaml into `color.yaml` (`paths.color_file`)
  with a comment on each value: `defaults:` plus `clients: {CODE: {only the keys that differ}}`. Values are
  identical (checked key by key against the old block). Order: an older `color:` block in config.yaml fills
  only keys color.yaml lacks; then color.yaml defaults; then `clients.<CODE>`; then an older `color:` block in
  `clients/<CODE>.yaml` (still honoured, applied last). Client codes in color.yaml are normalized ("3AC" = E3AC).
- **Four constants that were in the code are now settings** with the same values, so behaviour is unchanged:
  `overexposed_high_factor` 2.5, `cast_medium_factor` 1.6, `flat_luma_range` [60, 200], `face_min_score` 0.8.
  `face_min_score` joins the color cache key only when changed, so existing caches stay valid.

- **Client data layout** (Phase 2): the client file stays `clients/<CODE>.yaml` (so nothing that reads it changes);
  a folder `clients/<CODE>/` next to it holds `notes.md` (what an import couldn't map, with its source) and
  `sources/` (a dated copy of every imported file). Backups in `backups/clients/<CODE>/` and `backups/color/`
  (newest 50 kept per file), the changelog in `changelog.csv` (time, user, computer, file, client, action, one-line
  summary of what changed). Paths in config.yaml (`backups_dir`, `changelog_file`).
- **New keys in a client file**: `name` (display name in menus), `language` (spelling + Whisper language unless the
  client's overrides name one), `rules` (do/don't text, for people; no check reads it yet), `examples` (wrong ->
  correct, reference), `ignore` (flags hidden for this client, same fields as ignore.yaml; the engine applies them with
  ignore.yaml). Old files without them behave exactly as before. A client file's ignore rule with no field besides
  client is refused (it would hide every flag); ignore.yaml keeps its old behaviour.
- **Color settings from the client editor go into color.yaml** (`clients: <CODE>:`), keeping the commented defaults
  above it byte for byte, so color stays in one file (Phase 1).
- **Template**: generated on demand (Client > Get client template) instead of a binary in the repo. Sheets Info,
  Rules, Glossary, Exceptions, Examples, Checks; grey rows starting "e.g." are examples and are skipped on import.
  Info also takes "Caption area" (full / bottom / middle / top / four fractions). Checks rows: check, setting
  (dotted for nested, e.g. `phrase_break.fragment_max_words`), value; `color` rows go to color.yaml (`skip` takes a
  comma list). Exceptions rows with a type/text/video/time become ignore rules; a row with only a note is a note.
- **QC review workbook import** reuses the parsing moved from scripts/prepare_data.py into
  `src/clients/workbook.py` (the script imports it from there). For the client code typed in the dialog it takes
  the "Client rules" row (name, the rule text, plus what that text says: minimum words, lowercase style), "Caption
  mistakes" rows (examples; a capitalized correction in a spelling note becomes an *unticked* forced-spelling
  candidate) and "Accepted exceptions" (notes). Codes are normalized on both sides (2CS = E2CS). "All QC notes" is
  not imported (it is evaluation data).
- **CSV**: a CSV saved from a template sheet (recognized by its header), or rows in the QC notes / client rules
  format; anything else becomes notes.
- **PDF**: text extracted locally with pypdf; heuristics for minimum words (several phrasings), caption position
  (bottom / middle / top -> a preset region), language, lowercase style, lists under a glossary/names heading,
  "spell it as 'X'" (forced spelling), lines that start like a rule (do, don't, never, always, avoid, must...).
  Names in quotes are offered unticked. Every other line becomes a note. Scanned PDFs (no text) give a message.
- **Review**: one dialog lists every finding grouped by kind, each ticked or not, double-click to edit (as text:
  `wrong -> correct`, `setting = value`, `type=spelling; text=...`), with where it came from. Values the user typed
  or changed in the client dialog win over values found in files; untouched defaults lose (the PDF's "3 words" beats
  the default 2). A second value for a single setting is shown unticked. Nothing is written before "Save client".
- **"Read the PDF with Claude"** only appears when `client_import.allow_claude: true` is set by an admin AND the PC
  has ANTHROPIC_API_KEY. Not in config.yaml by default (= off). Its findings are added unticked.
- **Edit keeps the code fixed** (renaming a client would orphan reports, backups and color settings); a new code
  is "Add new client".
- **Validation**: code 2-20 of A-Z 0-9 _ after normalizing; display name 1-100 chars; language one of en es fr de
  pt it; minimum words 1-10; caption area inside the frame; color types and check names from fixed lists;
  exceptions need a type, text, video or time.

- **Three places** (Phase 3): (a) the app is read-only and its own config.yaml, color.yaml, rules... are the
  defaults; (b) the shared folder holds every shared file plus `shared_settings.yaml`, `latest.json`, backups,
  changelog, models, optional reports; (c) per-user local data: settings, reports, caches, logs, models it had to
  download, `local.yaml` (this PC's machine settings) and `shared_cache/` (the copy every check reads).
- **Config on the floor** = the app's config.yaml, then the shared config.yaml (only what the floor changes), then
  this PC's local.yaml. Cache and model paths (`LOCAL_PATH_KEYS`) resolve under the local folder; a shared file
  missing from the shared folder falls back to the app's own copy (`DATA_FILE_KEYS`). Alone on a PC (no shared
  folder) everything behaves as before, so existing setups and tests are unchanged.
- **Where the shared folder comes from**: a bootstrap file with `force: true` > the user's choice in the first-run
  setup screen (`floor.yaml` in the user config folder) > a bootstrap file > nothing (alone). Bootstrap locations
  next to the app or machine-wide (`%PROGRAMDATA%\VideoQCBot`, `/Library/Application Support/VideoQCBot`), from
  `src/platform`. Paths expand `~` and environment variables (`%OneDrive%`). Choosing an empty folder offers to seed
  it with the app's starting files.
- **Reads always come from the cached copy**, refreshed (only changed files, by size and mtime; deleted clients
  removed) before every check (in the worker thread, so the window never waits), every 60 s in the background, and
  when a menu opens if the last refresh is over 30 s old. Reaching the folder is tried in a thread with a 4 s
  limit, because a disconnected SMB drive can block for a minute. Offline: the menu shows "Offline, using cached
  rules from <time>", the tray tooltip says so, a notification appears once, and saving is refused with a
  message (checks still run). A missing `clients/` folder on the share (a sync client mid-update) keeps the copy.
- **Writes go to the shared folder itself** (not the copy) under one lock file (`.locks/data.lock`, O_EXCL,
  holder's user and PC inside, taken over after 120 s as a crashed save, 20 s wait then "<user> on <PC> is saving
  changes"), then atomic temp+rename (retried on Windows sharing errors), a backup of the old file and a
  changelog row; then the copy is refreshed. Duplicate client codes are checked against the shared folder, so a
  client added a minute ago on another PC is seen.
- **The glossary and ignore list are edited inside the app** (a text editor dialog) instead of the OS's default
  editor, so every save takes the lock, gets a backup and a changelog line, is checked as valid YAML, and is
  refused if someone else saved the file since it was opened. Non-editors and offline PCs get a view-only window.
- **Roles**: alone on a PC everyone is an editor. On the floor: login names in `shared_settings.yaml editors`
  (case-insensitive), or the editor PIN for the session (PBKDF2-SHA256, 200 000 rounds, random salt, stored as a
  hash); when neither is set up yet everyone is an editor so the first one can set it up. Documented everywhere as
  a convenience, not security (folder permissions are the real control).
- **Gentle on PCs**: below-normal priority for the whole app process from start (Windows BELOW_NORMAL_PRIORITY_CLASS;
  macOS nice 10, which a normal user can't undo, so it isn't toggled per check); ffmpeg inherits it. Threads
  `auto` = half the cores for Whisper and the OCR (`machine.cpu_threads`, `ocr.cpu_threads`, were 4 and all
  cores). One check per PC across users and app copies: a lock file in a folder every user can write
  (`%PROGRAMDATA%\VideoQCBot`, `/Users/Shared/VideoQCBot`) with the owner's pid; a dead owner's lock is taken
  over; a second check shows "Waiting: another check is running on this PC (user)" and can be cancelled. Low disk
  (< 2 GB on the report or cache drive) and low free memory (< 1.5 GB) warn before a check, without stopping it.
- **Whisper model `auto`**: base under 4 cores or under 8 GB of memory, else small (medium is too slow on CPU for a
  floor). This changes this laptop's model from base to small, so its cached transcripts are made again once
  (the cache key includes the model). Manual override per PC: Settings > Speech model (written to local.yaml), or
  `machine.whisper_model_size` in config.
- **Models**: a ready model folder `<models>/whisper/<size>/model.bin...` (what `scripts/make_offline_bundle.py`
  makes with huggingface_hub) is loaded as a plain path, so a read-only share works and nothing is downloaded; the
  shared models folder is tried first, then the user's own. The face model likewise. The OCR models were already
  inside the rapidocr package; nothing to share.
- **ffmpeg bundled** with `imageio-ffmpeg` 0.6.0 (static ffmpeg 7.1 wheels for Windows, macOS Intel and Apple
  Silicon; no dependencies). The packaged app uses it first; from source an ffmpeg on PATH still comes first (so
  this laptop's behaviour doesn't change), the bundled one is the fallback. There's no ffprobe in it; nothing uses one.
- **Versions**: `src/version.py` (0.5.0) is the single source; the PyInstaller spec reads it. Reports show the app
  version and a client rules version (sha256 of the client file + its color.yaml block, 12 hex). Check for update
  reads `latest.json` from the cached copy, says where the installer is and can open its folder; a newer version is
  announced once by a notification at start. Never installs.
- **Shared reports**: off unless `shared_settings.yaml reports.copy_to_shared: true` or the user ticks it in
  Settings; then the Word report (and an audio report's clips folder) is copied to
  `<shared reports>/<client>/<YYYY-MM-DD>_<user>_<video>.docx`, never overwriting. The report is always saved
  locally first; a failed copy is a warning, not a failed check.
- **Diagnostics**: version, OS, CPU/memory, floor state, editor or not, ffmpeg, models found, settings, last 150 log
  lines; to the clipboard and `diagnostics.txt` in the log folder. Whether an API key is set, never its value.
- **The CLI** (`python -m src.main`) refreshes the cached copy once at start when a shared folder is set.

- **E2CL has no notes in the QC spreadsheet** (Phase 4). Its podcast audio-edit notes ("Stutter on word got",
  "Word repeat", "Abrupt cut", "Silence not removed": 158 of them, 7 episodes) are all under **E2CS** (the
  "Episode NNN - Market" podcast tasks). The evaluation template takes audio notes from E2CL and E2CS
  (`AUDIO_CLIENTS` in scripts/evaluate.py) and E2CL got its own client file; E2CS's file is unchanged (it also
  has caption work). If E2CL is meant to be E2CS, add an `audio:` block to E2CS.yaml (or copy E2CL's).
- **Audio files skip every picture check** and run the audio checks; caption checks run only when an SRT is
  given (`--srt`). `audio.enabled: auto` (default) = on for audio files, off for videos; a client file says
  `true` (also on videos) or `false`. *Checks > Audio edits* in the menu switches them off for a run.
- **Thresholds**: config.yaml `audio:` has the commented defaults; `clients/E2CL.yaml` spells out every one
  (as asked: "all thresholds and sensitivity levels live in the client file"); `sensitivity: low|medium|high`
  picks a column from `levels:` (acoustic similarity, retake similarity, click ratio, room tone dB, loudness
  jump dB, cut score, dead air seconds). The client editor (Phase 2) round-trips the whole block as
  `audio.<key> = value` settings, and the template's Checks sheet takes `audio | sensitivity | high`.
- **Whisper for the audio checks**: a disfluency prompt and `condition_on_previous_text: false`, as asked, but
  **with the voice-activity filter off**: on the text-to-speech sample the prompt plus the filter made Whisper
  (base and small) drop a whole 20 s stretch. With the filter off it kept everything and wrote "short answer,
  short answer"; without the prompt it wrote "rate rates" but tidied the phrase repeat away. So: the prompt
  without the filter, and when the transcript still leaves 5 s+ of speech (judged by the sound) without words,
  those stretches are filled from the plain (caption-style) transcript, asked for only then. Words Whisper
  writes into silence (a classic "Thank you." in a pause) are ignored for repeats (speech share < 30%). Both
  transcripts are cached like the caption one; the prompt and the new setting are in the cache key only when
  set, so existing caption transcripts keep their keys.
- **Repeats Whisper hides**: cepstral features (12 MFCC-style coefficients, numpy only) compared at every lag
  of 150-800 ms (vectorized: the mean over L frames of frame t vs t+L); a repeat must also be clearly less alike
  at half its length (not a held vowel), mostly speech, similar in level, start with an onset in both copies,
  and overlap transcript words (not a music loop). Acoustic and transcript findings at the same moment merge
  into one, surer flag that keeps the transcript's type and wording.
- **Cuts**: evidence at candidate points (word boundaries, pauses, a 0.5 s grid for room tone, and wherever a
  signal fires) combined as independent chances: click (a sample jump vs the 4 ms around it), speech stopping
  with no fade or into digital silence, the timbre changing at once where two words touch while the sound is
  still loud and not fading (measured at 5 ms steps so frame alignment doesn't matter), a short unsure word still
  loud at its end, background level/spectrum change (median of 0.4 s+ of pauses each side, 3 s windows, the
  strongest point within 3 s kept), speech level change, and no pause between sentences. Any abrupt evidence
  makes it `abrupt_cut`, otherwise `jump_cut`.
- **Dead air from the 20 ms level, not ffmpeg silencedetect**: silencedetect judges every sample, so the peaks of
  room-tone hiss (-56 dBFS RMS has peaks over -45) count as sound and a 5 s pause in a podcast was never
  "silence". A frame is silent below -60 dBFS or within 8 dB of the local noise floor. ffmpeg still decodes the
  audio (any format, to mono float at 16 kHz, through a pipe: no temp file) and writes the clips.
- **No new dependencies for Phase 4**: numpy (already pinned) does the features; scipy, soundfile and librosa
  weren't needed. ffmpeg (bundled since Phase 3) decodes, writes the MP3 clips (libmp3lame is in both the
  bundled and the winget build) and splices the test samples. Waveform pictures are drawn with Pillow.
- **Report**: audio issues get ids -3001...; each gets a 6 s mono 64 kbit/s MP3 (fade in/out) and a 640x110
  waveform PNG with a red line at the moment, in `<file>_<date-time>_clips/` next to the .docx. The Word file
  links to the clip with a relative hyperlink and embeds the waveform; the column is "Waveform and clip" and the
  text column "What was said" (the words around the moment). The clips folder name is unique and kept when the
  report is copied to the shared folder, so the links work there too. Timestamps are mm:ss.mmm.
- **Memory**: an hour of audio is ~230 MB of float32 samples; the click search runs in 30 s pieces and the
  spectra in chunks, so peak use stays near that. Analysis takes ~25 s per hour of audio on this laptop
  (Whisper takes far longer).

### Phase 1: Simplify and expose settings — done (2026-10-13)
- Punctuation off and hidden as above; tests updated: the default CLI run never mentions the API; with the check
  switched on and no key it is still skipped with the old warning.
- color.yaml as above, packaged with the app and copied to the user data folder like the other editable files.
- Kept unchanged: capitalization, spacing, spelling, repeated words, every color check.
- New tests: `tests/test_settings_phase1.py`.

### Phase 2: "Add / edit client" from the mascot menu — done (2026-10-13)
- Menu: Client > clients with their display names, Add new client..., Edit client..., Get client template.
- `src/clients/` (no Qt, tested): `model.py` (findings, validation, client file <-> findings, change summary),
  `importers.py` (template, QC workbook, CSV, PDF, optional Claude), `store.py` (save with duplicate check, backup,
  color.yaml block, notes, source copies, changelog), `workbook.py` (shared with prepare_data). `src/utils/safe_io.py`:
  atomic write (temp file + rename, retried on Windows sharing errors), backups, changelog.
- `src/app/client_dialogs.py`: client dialog (code, display name, language, minimum words, caption area, notes, a
  drop zone / file picker), the review dialog, and the caption area picker (frames every 2 s from the first minute,
  a slider, drag a rectangle; saved as fractions).
- New dependency: **pypdf 6.20.1** (pure Python, no dependencies, one wheel for every OS): local PDF text.
- Tests: `tests/test_clients.py` (template round trip, workbook incl. the real one when present, CSV, PDF from a
  generated PDF, validation, duplicates, backups, changelog, engine uses the client's ignore rules and language,
  the review dialog, the add dialog, the rectangle picker).
- **Untested on real material:** no real client style-guide PDF was available; the PDF heuristics were tested on
  generated text PDFs only. The caption area picker was tested headless (drag maths, frame loading); the dragging
  itself needs the manual steps.

### Phase 3: Many PCs on the floor — done (2026-10-13)
- `src/shared.py` (floor setup, bootstrap, cached copy and sync, shared lock, shared writes, shared settings, roles,
  PIN, shared reports), `src/machine.py` (auto threads and Whisper model, resource warnings, one check per PC),
  `src/version.py`, `src/diagnostics.py`; machine helpers per OS in `src/platform/` (memory, priority, pid alive,
  machine folder, bootstrap folders); config loader made floor-aware (`src/config.py`).
- App: first-run setup screen, version line, offline line and tray tooltip, editor gating, Editor mode (PIN),
  in-app glossary / ignore list editor, Check for update, Copy diagnostics, Settings (shared folder, speech model,
  shared reports, Set editor PIN). Worker: refresh, warnings, one-check-per-PC wait, shared report copy.
- `scripts/make_offline_bundle.py`, `docs/IT_DEPLOYMENT.md`; packaging spec bundles imageio-ffmpeg and reads the
  version from src/version.py.
- New dependency: **imageio-ffmpeg 0.6.0** (bundled ffmpeg; no PyTorch; one wheel per OS/chip).
- Tests: `tests/test_floor.py` (setup precedence, seeding, config from the cached copy, changes from another PC
  arriving, offline, fallbacks, local.yaml, the lock and a stale lock, conflicting edits, client saves to the
  share, PIN hashing, roles, machine defaults, resource warnings, the per-PC lock, shared models, bundled ffmpeg,
  update check, shared report names, versions in reports, diagnostics without the key, the dialogs, the menu).
- **Untested on real infrastructure:** a real SMB share and a synced OneDrive/SharePoint folder (the tests use a
  local folder, and an unplugged drive is simulated by a missing path), two PCs saving at the same moment,
  fast user switching on one PC (the per-PC lock was tested within one process with a live pid and a dead one),
  low-priority behaviour under a real editing load, the macOS memory/priority helpers (written for macOS, run only
  on Windows here), PyInstaller builds with the bundled ffmpeg on both OSes, and MDM/Intune deployment.

### Phase 4: Audio-edit checks for E2CL (podcasts) — done (2026-10-13)
- Input: audio files (mp3, wav, m4a, aac, flac, ogg; also opus, wma, aif) in the CLI, the file picker, drops
  and folders. `src/audio/media.py` (audio-only detection, decoding), `src/audio/features.py` (levels, cepstra,
  band energies, DTW similarity, lag similarity), `src/audio/edits.py` (the checks), `src/audio/clips.py`
  (clips, waveforms); engine stages for audio files; report and evaluation support; `clients/E2CL.yaml`.
- Issue types: stutter, repeated_phrase (incl. retakes), abrupt_cut, jump_cut, dead_air, filler (off by default,
  counted always).
- `scripts/make_audio_samples.py`: text-to-speech (Windows SAPI / macOS say) or `--source`, room tone, Whisper
  word times, ffmpeg filter-graph splicing of one mistake of each kind, MP3, truth JSON, clean control.
- `scripts/evaluate.py --checks audio`: audio note types (from E2CL and E2CS notes by keyword: 70 stutter, 39
  repeated phrase, 28 abrupt, 12 dead air, 9 jump), matched within ±1.5 s, recall and precision per type and
  "all audio". **Not run: none of the 7 E2CS episodes are on this machine.**
- Tests: `tests/test_audio_edits.py` + `tests/audio_synth.py` (numpy speech-like audio with planted mistakes,
  the engine with a stand-in transcript, the report's clips, waveforms and links, the shared copy) and a slow
  test with text-to-speech and the real Whisper.
- **Results so far (not real podcasts):**
  - numpy speech-like audio: 18 clean files, 0 false alarms; 6 planted files (each: stutter written once by the
    "transcript", 2-word repeat, retake, mid-word cut, room tone change and back, 4 s dead air): every stutter,
    repeat, retake, room change and dead air found with the right type, 0 false alarms; mid-word cuts 5 of 6 in
    the test (10 of 12 over 12 tuning files).
  - text-to-speech (SAPI "Zira"), ffmpeg-spliced, MP3, real Whisper base and small: the clean control 0 flags;
    6 of 7 planted mistakes found (stutter "rate rates", retake, "short answer, short answer", dead air, room tone
    change and back); **the mid-word cut ("thinks" cut at its loudest point) was missed** with both models. The
    stutter splice itself is also flagged as an abrupt cut (it is one).
- **Untested on real audio:** every threshold was tuned on synthetic and TTS speech, which is cleaner and more
  regular than a real conversation: real stutters are less alike than copied audio (acoustic similarity
  0.94 needed), real speech has breaths, laughter, crosstalk, music beds and level rides, and real edits are often
  crossfaded. Expect both misses and false alarms on real E2CL episodes until the evaluation is run on them:
  put the episodes and data/ground_truth.csv rows (or the drafted E2CS rows) in place and run
  `python scripts/evaluate.py --checks audio`, then adjust E2CL.yaml (sensitivity first).

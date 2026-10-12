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
  default to `Documents/Video QC Bot reports`. Running from source is unchanged.
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

### Open questions
- Fill `data/ground_truth.csv` and put the matching videos (mostly E3AC weddings) where the evaluation can find them.
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

# Task 1 plan — skip the speech-vs-caption checks when there's almost no speech

Reel 3_2.mp4 had 1 transcribed word (Whisper's voice filter removed 25.5 of 26.7 seconds).
The speech comparison has nothing to compare, so it should skip itself and say so, instead of
producing misleading results.

## Approach

All seven speech-vs-caption flags come from a single check (`src/checks/transcript.py::check`):
`wrong_word`, `missing_word`, `extra_word`, `caption_early`, `caption_late`, `caption_sync`,
`uncaptioned_speech`. Nothing else produces them. So "skip only the checks that need the
transcript" means gating that one check.

Everything that does not need audio is untouched: too_short / phrase_break, spelling and repeated
words, capitalization, spacing, punctuation, on-screen text (`screen_text`), color, and the audio
edits (stutter, cut, dead-air).

## Changes

### New: `src/audio/speech.py`
A pure, offline helper (no Whisper, no ffmpeg) that inspects the transcript already in hand:

- `words` — number of spoken words
- `speech_seconds` — merged union of the word `[start, end]` spans (no double counting)
- `coverage` — `speech_seconds / duration` (when the duration is known)
- `mean_probability` — mean of `SpokenWord.probability`
- `no_speech_share` — duration-weighted share of Whisper `segments` with
  `no_speech_prob >= no_speech_probability` (`None` when segments are missing)
- `skip_reason(...)` — a short, user-facing reason, checked in this order:
  1. no audio track → *"the file has no audio track"*
  2. zero words → *"no speech was found in the audio (it may be silent)"*
  3. `words < min_words` **or** `coverage < min_speech_coverage` →
     *"only N words in M seconds of audio"*
  4. likely hallucination over music → `mean_probability < min_mean_word_probability` or
     `no_speech_share >= no_speech_share` → *"the words Whisper heard look like guesses over music"*

### `src/audio/media.py`
- `media_duration(path) -> float | None` via `av` (container duration, falling back to the
  video-stream duration × timebase as `video_ocr.py` already does). If unknown, the coverage rule
  is skipped and only the word-count / probability rules apply.
- `has_audio(path) -> bool | None` (any non-cover-art audio stream), so a genuinely audio-less file
  is caught **before** Whisper instead of becoming a `QCError`.

### `src/engine.py`
- Video path: after `context.transcript = get_transcript(...)`, run the gate, store
  `context.stats["speech"] = {..., "reason": ...}`, and `log.info("speech comparison skipped: %s",
  reason)` when skipping. If `has_audio(video) is False`, don't call Whisper at all; set
  `context.transcript = Transcript()` and the reason.
  - `has_audio` returning `None` (unreadable file) keeps today's behaviour → Whisper is called →
    `QCError` (so `tests/test_engine.py::test_unreadable_video_is_a_qc_error` still passes).
- Audio path (`_run_audio_file`): run the same gate after the caption transcript is fetched. The
  audio-edit checks already ran above, so requirement 6 holds.
- `context.transcript` is **kept** (non-None) so the existing "not run (no video)" status branch is
  not triggered; the skip is carried in `context.stats`.

### `src/checks/transcript.py`
One early `return []` at the top of `check` when `context.stats.get("speech", {}).get("reason")`.

### Say it clearly (report + log + notification)
- Status line (`check_status_lines`): when skipped →
  `Captions vs audio (Whisper): skipped (only 1 word in 27 s of audio, so there was nothing reliable to compare)`,
  otherwise today's "N words heard".
- Report note near the top: add a defaulted `notes: list[str]` to `QCResult` and `ReportInfo`,
  filled by the engine with *"Speech comparison skipped: …"*, rendered as a bold line under the
  header facts in both `report.md` (`builder.py::_header`) and `report.docx` (`docx_writer.py`).
- Log: the `log.info` line above (app log + CLI log).
- App "done": carry the note out of the worker (extend `_Job.finished` / `CheckRunner.finished` to
  `Signal(int, str, str)` = count, report, note) and append it to the status and the
  `notify("QC check done", …)` message. **Never** `error=True`.

## Config — `config.yaml`, under `checks.transcript` (with comments)

```yaml
min_words: 10                    # fewer words heard than this in the whole audio = not enough speech
min_speech_coverage: 0.05        # ...or speech covering under this share of the audio (0.05 = 5%)
min_mean_word_probability: 0.5   # average word probability under this = Whisper guesswork over music
no_speech_probability: 0.6       # a segment Whisper is this sure holds no speech...
no_speech_share: 0.5             # ...over this share of the audio = music/noise, not speech
```

Per-client overrides come for free through the existing `overrides: {transcript: {...}}` block
(`check_settings` deep-merges them).

## Cache safety (requirement 5)

- The rule is evaluated **after** `get_transcript` returns (cached or fresh).
- Total duration comes from `av`, not Whisper.
- `whisper_settings()` (the cache key) is **not** touched; no cache version is bumped.
- Older cached transcripts without `segments` lose only the no-speech signal and fall back to
  words / coverage / mean probability.
- Changing the limits never re-transcribes.

## Behaviour matrix

| Input | Result |
|---|---|
| 1 word in 27 s (Reel 3_2) | speech checks skipped; note/log/notification; other checks run |
| ≥10 words, ≥5% coverage | runs exactly as today |
| 9 words | skipped |
| 40 words over 600 s (≈1.7% coverage) | skipped (coverage) |
| no audio track | skipped, no `QCError` |
| silent track (0 words) | skipped ("no speech was found") |
| music, low-probability "lyrics" | skipped (guesswork) |
| music, high `no_speech_prob` | skipped (no-speech) |
| audio-only E2CL + captions, sparse speech | caption comparison skipped; stutter/cut/dead-air still run |
| older cache, no `segments` | still works; no-speech signal unavailable |
| change the limits | no re-transcription (cache untouched) |

## Files to touch

- new `src/audio/speech.py`
- `src/audio/media.py` (`media_duration`, `has_audio`)
- `src/engine.py` (gate wiring, note, status line, log)
- `src/checks/transcript.py` (one early return)
- `src/report/builder.py` + `src/report/docx_writer.py` (`notes`)
- `src/app/worker.py` + `src/app/main.py` (note in the done notification)
- `config.yaml` (new commented keys)
- new `tests/test_speech.py`; additions to `tests/test_transcript.py`, `tests/test_engine.py`,
  `tests/test_report.py`, `tests/test_audio_edits.py`, and the 2-line update in
  `tests/test_app_step3.py`

## Tests

1. **1 word** → `transcript.check` returns `[]`; note present; log line emitted.
2. **10+ words, good coverage** → runs normally (no skip).
3. **Just under `min_words`** → skipped.
4. **Low coverage despite enough words** → skipped.
5. **No audio track** → skipped with the no-track reason, no exception.
6. **Silent track (0 words)** → skipped with the silent reason.
7. **Low mean word probability** → skipped as guesswork.
8. **High no-speech share** → skipped.
9. **Cached transcript** → gate evaluated from cache; the fake transcriber is not called again;
   changing the limits doesn't re-transcribe.
10. **Per-client override** of the limits changes the outcome.
11. **Non-speech checks still run** (spelling / capitalization / spacing / too_short / color produce
    issues while transcript is skipped).
12. **Audio-only E2CL** → audio-edit checks still run when the comparison is skipped.
13. **Report + app**: the note appears near the top of `report.md` / `report.docx`, and the done
    notification / status carry it.

## Blockers / open questions

1. **Requirement 7 is truncated** — it ends at `Tests: 1-word transcript ->`. The list above is my
   proposed version; please confirm or paste the rest.
2. **Task 2 never arrived** — "Two tasks" was said, but only Task 1 followed.

Small decisions (defaults to my recommendations if unanswered):

- **Report-note placement:** a distinct bold line under the header facts (recommended) vs just the
  "skipped (…)" line in the existing **Checks:** list.
- **`min_words` semantics:** count **all** transcribed words (recommended), with probabilities used
  only for the hallucination signal.
- **Hallucination defaults:** `min_mean_word_probability: 0.5`, `no_speech_probability: 0.6`,
  `no_speech_share: 0.5`.

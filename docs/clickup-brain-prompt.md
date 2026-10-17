# ClickUp Brain prompt - E2CS episode audio (for the audio-edit evaluation)

The full edited videos can't be collected, so the evaluation targets the **audio-edit checks**
instead - stutter, repeated phrase, abrupt cut, jump cut and dead air. Those run on plain audio, and
the QC workbook already holds **158 real audio-edit notes** across 6 E2CS episodes.
So the only media we need is the **podcast audio**.

Paste the prompt below into ClickUp Brain to find each episode's audio file (or a link to it).

## How to use

1. In ClickUp, open **Brain** and paste the block under [The prompt](#the-prompt).
2. Collect the audio and save each file in `data/samples/` named `E2CS_<episode>.mp3`
   (e.g. `E2CS_316.mp3`); `.wav` / `.m4a` are fine too.
3. Fill the ground truth from the file names and score the checks:

   ```powershell
   python scripts/match_videos.py --folder data/samples
   python scripts/evaluate.py --checks audio --truth data/ground_truth_filled.csv
   ```

Episode 316 alone carries most of the notes, so start there if the others are slow to find.

## What we need

| Episode | Audio-edit notes | Save as |
|---|---:|---|
| 316 | 128 | E2CS_316.mp3 |
| 318 | 17 | E2CS_318.mp3 |
| 317 | 7 | E2CS_317.mp3 |
| 331 | 3 | E2CS_331.mp3 |
| 321 | 2 | E2CS_321.mp3 |
| 320 | 1 | E2CS_320.mp3 |

## The prompt

```text
I am building a ground-truth dataset for an automated podcast audio quality-control
tool. It listens for editorial audio problems: stutters, repeated words/phrases/sentences,
abrupt cuts, jump cuts and dead air. I need the episode AUDIO file for each ClickUp task
listed below.

For each task, return:
- task_name, task_id, task_url, client_code, episode_number
- audio_attachment_name: the file name of the audio attached to the task (mp3/wav/m4a), if any
- audio_attachment_url: a direct download URL if you can produce one; otherwise write the task URL
  and "task link only"
- other_link: any external link in the task or its comments (Google Drive, Dropbox, etc.) where the
  episode audio lives

MATCH ON TASK NAME (ignore the leading "#"). These are the tasks, with how many audio notes we
have for each:

#2CS Episode 316 - Market - FX (INTERNAL REVISION)
#E2CS Episode 318 - Warren Buffett - FX (Revision 01)
#E2CS Episode 317 - SpaceX - FX - Bullcast (Revision 1)
#E2CS Episode 318 - Warren Buffett - FX (Revision Reel 5)
#E2CS Episode 331 - Fin Lit 3 - FX (Internal Revision 01)
#E2CS Episode 321 - Hamilton - FX bullcast (Revision 01)
#E2CS Episode 320 - Fed Chair - FX (Revisions)

OUTPUT: one CSV with exactly this header row, then one row per task:

task_name,task_id,task_url,client_code,episode_number,audio_attachment_name,audio_attachment_url,other_link

Rules:
- One row per task; keep task_name exactly as listed.
- Do NOT invent or guess URLs. If you cannot produce one, write "NOT FOUND".
- If a task has no audio attached, still return the row and put "NO AUDIO ATTACHED" in
  audio_attachment_name.
- Two of the rows are the same episode (Episode 318, two revisions) - that is expected; return both.
- If a task name matches more than one task, include both and flag it in the summary.

AFTER the CSV, give me a short summary:
1. Tasks with an audio file or a link: N of 7. List the ones without.
2. For each episode, say whether you found the full episode audio.
```

## Notes

- 6 unique episodes are needed; Episode 318 appears twice because two revisions are
  listed, and both point at the same audio file.
- If Brain cannot expose the files or links, any route that gets these 6 episode
  recordings works - the episode audio is all the audio checks need.
- `scripts/match_videos.py` matches a file to a task by name, so a file named `E2CS_316.mp3` fills
  every note for Episode 316 at once.

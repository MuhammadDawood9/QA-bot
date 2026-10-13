"""The QC report: a Markdown document with screenshots, plus a CSV of the issues.

    reports/<video or srt name>_<date-time>/
        report.md         header, summary, index, one section per flagged caption, appendix
        screenshots/      one frame per flagged caption (when a video was given)
        issues.csv        one row per issue, for Excel or a ClickUp import

Issues are grouped per caption, so a caption with several problems (for example a doubled
word found by both the spelling and the audio check) gets one screenshot and one section.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from src.models import SEVERITIES, Caption, Issue, shot_number
from src.report.screenshots import grab_frame
from src.utils.timecode import format_timestamp

LABELS = {
    "too_short": "Too short",
    "phrase_break": "Phrase break",
    "spelling": "Spelling",
    "repeated_word": "Repeated word",
    "capitalization": "Capitalization",
    "spacing": "Spacing",
    "punctuation": "Punctuation",
    "wrong_word": "Wrong word (audio)",
    "missing_word": "Missing word (audio)",
    "extra_word": "Extra word (audio)",
    "caption_early": "Caption early (audio)",
    "caption_late": "Caption late (audio)",
    "caption_sync": "Captions out of sync (audio)",
    "uncaptioned_speech": "Speech with no caption",
    "screen_text_mismatch": "Differs from on-screen text",
    "overexposed": "Overexposed (color)",
    "underexposed": "Too dark (color)",
    "crushed_blacks": "Crushed blacks (color)",
    "flat_contrast": "Flat contrast (color)",
    "saturation": "Saturation (color)",
    "color_cast": "Color cast (color)",
    "skin_tone": "Skin tone (color)",
    "shot_mismatch": "Shot doesn't match (color)",
}
_RANK = {s: i for i, s in enumerate(SEVERITIES)}          # high = 0 (worst)
CSV_COLUMNS = ["timestamp", "seconds", "caption_id", "type", "severity", "caption", "problem",
               "suggestion", "confidence"]


@dataclass
class ReportInfo:
    srt: Path | None                 # None = captions were read from the video
    video: Path | None = None
    client: str | None = None
    rules_version: str | None = None
    checks: list[str] = field(default_factory=list)     # one line per check: what ran, what was skipped
    generated: dt.datetime = field(default_factory=dt.datetime.now)
    app_version: str | None = None
    client_rules_version: str | None = None             # hash of the client file (+ its color settings)


def captions_source(info: "ReportInfo") -> str:
    return info.srt.name if info.srt is not None else "read from the video (OCR)"


def caption_title(cid: int) -> str:
    """Issues not tied to a caption have negative ids: speech with no caption, or a shot (color)."""
    if cid > 0:
        return f"caption #{cid}"
    shot = shot_number(cid)
    return f"shot {shot}" if shot is not None else "no caption"


def caption_anchor(cid: int) -> str:
    if cid > 0:
        return f"caption-{cid}"
    shot = shot_number(cid)
    return f"shot-{shot}" if shot is not None else f"no-caption-{-cid}"


def label(issue_type: str) -> str:
    return LABELS.get(issue_type, issue_type.replace("_", " ").capitalize())


def md(text: str) -> str:
    """Safe inside a Markdown table cell or line: no pipes, no line breaks, no stray formatting."""
    text = " ".join(str(text).split())
    for ch in ("\\", "|", "*", "`", "<", ">", "[", "]"):      # "_" inside words is plain text in Markdown
        text = text.replace(ch, "\\" + ch)
    return text


def worst(issues: Sequence[Issue]) -> str:
    return min((i.severity for i in issues), key=lambda s: _RANK[s])


def report_folder(base: Path, info: ReportInfo) -> Path:
    stem = (info.video or info.srt).stem
    folder = Path(base) / f"{stem}_{info.generated:%Y%m%d-%H%M%S}"
    n = 2
    while folder.exists():                                 # never overwrite an earlier report
        folder = Path(base) / f"{stem}_{info.generated:%Y%m%d-%H%M%S}-{n}"
        n += 1
    return folder


def write_csv(path: Path, issues: Sequence[Issue]) -> None:
    # utf-8-sig is UTF-8 with a marker at the start, so Excel shows curly quotes and dashes correctly.
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for i in issues:
            writer.writerow([format_timestamp(i.timestamp), f"{i.timestamp:.3f}", i.caption_id, i.type,
                             i.severity, i.original, i.message, i.suggestion, f"{i.confidence:.2f}"])


@dataclass
class ReportData:
    """Everything a report writer needs, worked out once so every format says the same thing."""
    info: ReportInfo
    folder: Path
    captions: list[Caption]                      # in time order
    issues: list[Issue]                          # in time order, worst first within a caption
    by_caption: dict[int, list[Issue]]
    caps: dict[int, Caption]
    flagged: list[int]                           # caption ids with issues, in time order
    shots: dict[int, tuple[str, float]]          # caption id -> (image path relative to folder, frame time)
    wanted_shots: bool                           # screenshots were attempted (a video was given)
    settings: dict

    def counts(self) -> Counter:
        return Counter((i.type, i.severity) for i in self.issues)

    def types(self) -> list[str]:
        return sorted({i.type for i in self.issues}, key=lambda t: list(LABELS).index(t) if t in LABELS else 99)

    def caption_text(self, cid: int) -> str:
        return self.caps[cid].flat_text if cid in self.caps else self.by_caption[cid][0].original

    def on_screen(self, cid: int) -> str:
        cap = self.caps.get(cid)
        return (f"{format_timestamp(cap.start)} - {format_timestamp(cap.end)}" if cap
                else format_timestamp(self.by_caption[cid][0].timestamp))


def prepare(captions: Sequence[Caption], issues: Sequence[Issue], info: ReportInfo,
            settings: dict, ffmpeg: str | None = None) -> ReportData:
    """Create the report folder, take the screenshots, and group the issues by caption."""
    folder = report_folder(Path(settings.get("output_dir", "reports")), info)
    folder.mkdir(parents=True)
    issues = sorted(issues, key=lambda i: (i.timestamp, i.caption_id, _RANK[i.severity]))
    by_caption: dict[int, list[Issue]] = defaultdict(list)
    for issue in issues:
        by_caption[issue.caption_id].append(issue)
    caps = {c.id: c for c in captions}
    flagged = sorted(by_caption, key=lambda cid: (caps[cid].start if cid in caps else by_caption[cid][0].timestamp, cid))

    shots: dict[int, tuple[str, float]] = {}
    want_shots = bool(settings.get("include_screenshots", True) and info.video is not None and ffmpeg)
    if want_shots:
        for cid in flagged:
            cap = caps.get(cid)
            at = by_caption[cid][0].timestamp if cap is None else (
                cap.start if settings.get("screenshot_at", "middle") == "start" else (cap.start + cap.end) / 2)
            name = f"screenshots/{int(round(at * 1000)):08d}_caption{cid}.jpg"
            if grab_frame(ffmpeg, info.video, at, folder / name, settings.get("screenshot_width", 640)):
                shots[cid] = (name, at)

    return ReportData(info=info, folder=folder, captions=sorted(captions, key=lambda c: (c.start, c.id)),
                      issues=issues, by_caption=dict(by_caption), caps=caps, flagged=flagged, shots=shots,
                      wanted_shots=want_shots, settings=settings)


def write_markdown(data: ReportData) -> Path:
    lines = _header(data.info, data.captions, data.issues, data.flagged)
    lines += _summary(data.issues)
    lines += _index(data.flagged, data.by_caption, data.caps)
    lines += _details(data.flagged, data.by_caption, data.caps, data.shots, data.info, data.wanted_shots)
    lines += _appendix(data.captions, data.by_caption)
    path = data.folder / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def build_report(captions: Sequence[Caption], issues: Sequence[Issue], info: ReportInfo,
                 settings: dict, ffmpeg: str | None = None) -> list[Path]:
    """Write the report folder in every format listed in settings["formats"]. Returns the files written."""
    data = prepare(captions, issues, info, settings, ffmpeg)
    formats = [f.lower() for f in settings.get("formats", ["md"])]
    written = []
    if "md" in formats:
        written.append(write_markdown(data))
    if "docx" in formats:
        from src.report.docx_writer import write_docx   # python-docx is only loaded when a Word file is wanted
        written.append(write_docx(data))
    if settings.get("write_csv", True):
        write_csv(data.folder / "issues.csv", data.issues)
        written.append(data.folder / "issues.csv")
    return written


def _header(info, captions, issues, flagged) -> list[str]:
    title = (info.video or info.srt).name
    lines = [f"# QC report: {md(title)}", ""]
    lines.append(f"- **Video:** {md(info.video.name) if info.video else 'none (caption text checks only)'}")
    lines.append(f"- **Captions:** {md(captions_source(info))}, {len(captions)} captions")
    lines.append(f"- **Client:** {md(info.client or 'default rules')}")
    lines.append(f"- **Generated:** {info.generated:%Y-%m-%d %H:%M}")
    lines.append(f"- **Rules version:** {md(info.rules_version or 'unknown')}")
    if info.client_rules_version:
        lines.append(f"- **Client rules version:** {md(info.client_rules_version)}")
    if info.app_version:
        lines.append(f"- **App version:** Video QC Bot {md(info.app_version)}")
    if info.checks:
        lines.append("- **Checks:**")
        lines += [f"  - {md(c)}" for c in info.checks]
    lines += ["", f"**{len(issues)} issue{'s' if len(issues) != 1 else ''} on {len(flagged)} of "
              f"{len(captions)} captions.**", ""]
    return lines


def _summary(issues) -> list[str]:
    if not issues:
        return ["No issues found.", ""]
    counts = Counter((i.type, i.severity) for i in issues)
    types = sorted({i.type for i in issues}, key=lambda t: list(LABELS).index(t) if t in LABELS else 99)
    lines = ["## Summary", "", "| Issue type | High | Medium | Low | Total |", "|---|---:|---:|---:|---:|"]
    for t in types:
        row = [counts[(t, s)] for s in SEVERITIES]
        lines.append(f"| {label(t)} | " + " | ".join(str(n or "") for n in row) + f" | {sum(row)} |")
    totals = [sum(counts[(t, s)] for t in types) for s in SEVERITIES]
    lines.append("| **All** | " + " | ".join(f"**{n}**" for n in totals) + f" | **{sum(totals)}** |")
    lines += ["", "Severity: **high** is almost always a real mistake, **medium** usually is, "
              "**low** is worth a glance (often a name to add to the glossary).", ""]
    return lines


def _index(flagged, by_caption, caps) -> list[str]:
    if not flagged:
        return []
    lines = ["## Flagged captions", "", "| Time | Caption | Problems | Worst |", "|---|---|---|---|"]
    for cid in flagged:
        group = by_caption[cid]
        problems = ", ".join(dict.fromkeys(label(i.type) for i in group))
        text = caps[cid].flat_text if cid in caps else group[0].original
        short = text if len(text) <= 60 else text[:57] + "..."
        number = f"#{cid} " if cid > 0 else ""
        lines.append(f"| [{format_timestamp(group[0].timestamp)}](#{caption_anchor(cid)}) | {number}{md(short)} | "
                     f"{md(problems)} | {worst(group)} |")
    return lines + [""]


def _details(flagged, by_caption, caps, shots, info, wanted_shots) -> list[str]:
    if not flagged:
        return []
    lines = ["## Details", ""]
    for cid in flagged:
        group = by_caption[cid]
        cap = caps.get(cid)
        when = (f"{format_timestamp(cap.start)} - {format_timestamp(cap.end)}" if cap
                else format_timestamp(group[0].timestamp))
        lines += [f'<a id="{caption_anchor(cid)}"></a>', "",
                  f"### {format_timestamp(group[0].timestamp)}, {caption_title(cid)}", ""]
        lines.append(f"On screen {when}")
        lines.append("")
        lines.append(f"> {md(cap.flat_text if cap else group[0].original)}")
        lines.append("")
        if cid in shots:
            image, at = shots[cid]
            lines += [f"![Frame at {format_timestamp(at)}]({image})", "", f"*Frame at {format_timestamp(at)}*", ""]
        elif wanted_shots:
            lines += ["*(no screenshot: ffmpeg could not get a frame at this time)*", ""]
        lines += ["| Problem | Severity | What's wrong | Suggested fix | Confidence |", "|---|---|---|---|---:|"]
        for i in group:
            lines.append(f"| {label(i.type)} | {i.severity} | {md(i.message)} | {md(i.suggestion)} | {i.confidence:.2f} |")
        lines.append("")
    return lines


def _appendix(captions, by_caption) -> list[str]:
    lines = ["## Appendix: all captions", "", "Flagged captions are in **bold**.", "",
             "| Time | # | Caption | Issues |", "|---|---:|---|---|"]
    for cap in sorted(captions, key=lambda c: (c.start, c.id)):
        group = by_caption.get(cap.id, [])
        text = md(cap.flat_text)
        if group:
            text = f"**{text}**" if text else ""
            found = ", ".join(dict.fromkeys(label(i.type) for i in group))
            lines.append(f"| {format_timestamp(cap.start)} | [{cap.id}](#caption-{cap.id}) | {text} | {md(found)} |")
        else:
            lines.append(f"| {format_timestamp(cap.start)} | {cap.id} | {text} | |")
    return lines

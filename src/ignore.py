"""The ignore list: flags you've looked at and decided are fine, so the bot stops raising them.

ignore.yaml (next to config.yaml; in the packaged app, in its data folder) holds rules. A flag is
hidden when every field a rule gives matches it:

    ignore:
      - client: E2AB                  # this client only (leave it out: every client)
        type: spelling                # the issue type, as the report's ignore lines show it (leave out: any)
        text: Gherabel                # words in the flagged caption, any case (leave out: any)
        note: the guest's name        # for you; the bot doesn't read it
      - video: Willy & Mitzie.mp4     # this video only
        type: color_cast
        at: "2:04"                    # this moment (within 2 s), for color flags and one-off cases

Every report ends with a ready-made line per flag to copy here. Hidden flags are counted in the
report's checks list, so nothing disappears silently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.models import COLOR_ISSUES, Issue
from src.utils.timecode import TimecodeError, format_timestamp, parse_range

log = logging.getLogger("qcbot.ignore")

FIELDS = ("client", "type", "text", "video", "at", "note")
AT_TOLERANCE = 2.0          # seconds either side of "at"


@dataclass(frozen=True)
class Rule:
    client: str | None = None
    type: str | None = None
    text: str | None = None
    video: str | None = None
    at: float | None = None
    line: int = 0               # position in the file, for messages

    def matches(self, issue: Issue, client: str | None, video: Path | None) -> bool:
        if self.client is not None and (client or "").upper() != self.client:
            return False
        if self.type is not None and issue.type != self.type:
            return False
        if self.video is not None and (video is None or video.name.casefold() != self.video.casefold()):
            return False
        if self.at is not None and abs(issue.timestamp - self.at) > AT_TOLERANCE:
            return False
        if self.text is not None and _squash(self.text) not in _squash(issue.original):
            return False
        return True


def _squash(text: str) -> str:
    """Case and spacing don't matter: "gherabel", "Gherabel" and "GHERABEL" all match."""
    return " ".join(str(text).casefold().split())


def ignore_file(config: dict) -> Path:
    """Where ignore.yaml is: paths.ignore_file in config.yaml, else next to config.yaml."""
    name = config.get("paths", {}).get("ignore_file") or "ignore.yaml"
    return Path(config["_root"]) / name


def load_rules(path: Path) -> list[Rule]:
    """The rules in ignore.yaml; none if the file is missing. Broken rules are logged and skipped."""
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("%s unreadable (%s); nothing is ignored", path, exc)
        return []
    entries = data.get("ignore") if isinstance(data, dict) else None
    if not entries:
        return []
    if not isinstance(entries, list):
        log.warning("%s: 'ignore' must be a list of rules; nothing is ignored", path)
        return []
    rules = []
    for n, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            log.warning("%s: rule %d isn't a set of fields; skipped", path, n)
            continue
        unknown = set(entry) - set(FIELDS)
        if unknown:
            log.warning("%s: rule %d has unknown field(s) %s; skipped", path, n, ", ".join(sorted(unknown)))
            continue
        at = None
        if entry.get("at") not in (None, ""):
            try:
                at, _ = parse_range(str(entry["at"]))
            except TimecodeError as exc:
                log.warning("%s: rule %d: %s; skipped", path, n, exc)
                continue
        rule = Rule(client=str(entry["client"]).upper() if entry.get("client") else None,
                    type=str(entry["type"]).strip().lower() if entry.get("type") else None,
                    text=str(entry["text"]) if entry.get("text") not in (None, "") else None,
                    video=str(entry["video"]) if entry.get("video") else None, at=at, line=n)
        if rule == Rule(line=n):
            log.warning("%s: rule %d matches every flag; skipped (give at least a type, text, video or time)",
                        path, n)
            continue
        rules.append(rule)
    return rules


def apply(issues: list[Issue], rules: list[Rule], client: str | None,
          video: Path | None) -> tuple[list[Issue], list[Issue]]:
    """(kept, hidden)."""
    kept, hidden = [], []
    for issue in issues:
        (hidden if any(r.matches(issue, client, video) for r in rules) else kept).append(issue)
    return kept, hidden


def _quote(value: str) -> str:
    """A YAML string that stays one line and survives quotes, colons and #."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def glossary_line(issue: Issue) -> str | None:
    """For an unknown word that's really a name or a brand: the glossary line that makes the
    spelling check accept it (and catch misspellings of it). None for other flags."""
    from src.checks.spelling import flagged_word

    return flagged_word(issue)


def rule_line(issue: Issue, client: str | None, video: Path | None) -> str:
    """One ignore.yaml line that hides this flag: by the unknown word for spelling flags, by its
    words for other caption flags (so the same words are fine in every video of that client), by
    video and time for color flags."""
    fields = []
    if client:
        fields.append(f"client: {client}")
    fields.append(f"type: {issue.type}")
    from src.checks.spelling import flagged_word

    words = flagged_word(issue) or " ".join(issue.original.split())   # an unknown word: just that word
    if issue.type in COLOR_ISSUES or not words or issue.caption_id < 0:
        if video is not None:
            fields.append(f"video: {_quote(video.name)}")
        fields.append(f"at: {_quote(format_timestamp(issue.timestamp))}")
    else:
        fields.append(f"text: {_quote(words)}")
    return "- {" + ", ".join(fields) + "}"

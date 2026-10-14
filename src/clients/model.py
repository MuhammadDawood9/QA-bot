"""A client's rules as a list of findings, so imported files and the current client file can be
shown together, reviewed, edited and confirmed before anything is saved.

A Finding is one thing: a glossary word, a rule, a check setting, an example... Each has a kind,
a value, where it came from, and whether it is kept. The review dialog shows every finding as
editable text (finding_text / parse_finding_text), and build_client() turns the kept ones into
the client file plus that client's block in color.yaml, and the text for notes.md.

The client file (clients/<CODE>.yaml):

    client: E8D                      # code (normalized: "8d" -> "E8D")
    name: Danja promo videos         # display name in the menus
    language: en                     # spelling and speech language (en, es, fr, de, pt, it)
    notes: Stylized lowercase allowed...   # short text, shown on the Edit dialog
    rules: ["Don't: animate the word 'the'"]
    glossary: ["Danja", "=YouTube"]  # "=" = forced spelling, exactly like this
    examples: [{wrong: "aging", correct: "Aging", category: punctuation}]
    ignore: [{type: spelling, text: Gherabel, note: guest's name}]   # flags hidden for this client
    overrides: {too_short: {min_words: 3}}      # same structure as checks: in config.yaml
    ocr: {region: {left: 0.05, top: 0.68, width: 0.9, height: 0.27}}

Keys this module doesn't manage (project, other ocr settings...) are kept as they are.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

import yaml

from src.config import normalize_client_code

KINDS: dict[str, str] = {
    "code": "Client code",
    "name": "Display name",
    "language": "Language",
    "min_words": "Minimum words per caption",
    "caption_area": "Caption area",
    "rule": "Rule (do / don't)",
    "glossary": "Glossary word",
    "forced": "Forced spelling",
    "exception": "Exception (flag to hide)",
    "example": "Example (wrong -> correct)",
    "check": "Check setting",
    "color_skip": "Color: issue type not to report",
    "color": "Color threshold",
    "note": "Note (saved to notes.md)",
}
SINGLE_KINDS = ("code", "name", "language", "min_words", "caption_area")
LANGUAGES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese", "it": "Italian"}
COLOR_TYPES = ("overexposed", "underexposed", "crushed_blacks", "flat_contrast", "saturation", "color_cast",
               "skin_tone", "shot_mismatch")
CHECK_NAMES = ("too_short", "spelling", "capitalization", "spacing", "punctuation", "transcript", "screen_text",
               "audio", "audio_guidelines")
MUSIC_PLACES = ("intro", "outro", "transitions", "ad_breaks", "under_speech")
SFX_PLACES = ("transitions", "stingers", "emphasis")
GUIDE_SENSITIVITIES = ("strict", "normal", "lenient")
# Top-level blocks of the client file that "check" settings with this prefix are written to (not overrides).
OWN_BLOCKS = ("audio", "audio_guidelines")
EXCEPTION_FIELDS = ("type", "text", "video", "at", "note")
# Where a caption sits, as a region of the frame (fractions); PDFs and the template may name one.
AREA_PRESETS = {
    "bottom": {"left": 0.05, "top": 0.62, "width": 0.9, "height": 0.33},
    "middle": {"left": 0.05, "top": 0.35, "width": 0.9, "height": 0.3},
    "top": {"left": 0.05, "top": 0.03, "width": 0.9, "height": 0.3},
}
_CODE = re.compile(r"[A-Z0-9_]{2,20}")


class ClientError(ValueError):
    """Input that can't be saved; the message is for the user."""


@dataclass
class Finding:
    kind: str
    value: Any
    source: str = ""                 # "client_template.xlsx, Glossary row 4", "current file"...
    keep: bool = True

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown finding kind {self.kind!r}")


# --- shown and edited as text ------------------------------------------------------------------

def _scalar(text: str):
    """"3" -> 3, "yes"/"true" -> True, "[a, b]" -> list; otherwise the text itself."""
    text = text.strip()
    if text.lower() in ("yes", "on"):
        return True
    if text.lower() in ("no", "off"):
        return False
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    return value if isinstance(value, (bool, int, float, list)) else text


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    return str(value)


def area_text(region: dict | None) -> str:
    if not region:
        return "full"
    return ", ".join(f"{float(region[k]):g}" for k in ("left", "top", "width", "height"))


def parse_area(text) -> dict | None:
    """"bottom" / "0.05, 0.62, 0.9, 0.33" / a dict -> a region; "full" or "" -> None (whole frame)."""
    if isinstance(text, dict):
        region = {k: float(text[k]) for k in ("left", "top", "width", "height")}
    else:
        text = str(text or "").strip().lower()
        if text in ("", "full", "whole frame", "none"):
            return None
        if text in AREA_PRESETS:
            return dict(AREA_PRESETS[text])
        parts = [p for p in re.split(r"[,\s;]+", text) if p]
        if len(parts) != 4:
            raise ClientError("Caption area: give left, top, width, height as fractions (e.g. 0.05, 0.62, 0.9, "
                              "0.33), or bottom / middle / top / full.")
        try:
            region = dict(zip(("left", "top", "width", "height"), (float(p) for p in parts)))
        except ValueError:
            raise ClientError(f"Caption area: {text!r} isn't four numbers.") from None
    validate_region(region)
    return {k: round(v, 4) for k, v in region.items()}


def validate_region(region: dict) -> None:
    left, top, width, height = (region[k] for k in ("left", "top", "width", "height"))
    if not (0 <= left < 1 and 0 <= top < 1 and 0 < width <= 1 and 0 < height <= 1):
        raise ClientError("Caption area: left and top must be 0-1, width and height above 0 and up to 1.")
    if left + width > 1.0001 or top + height > 1.0001:
        raise ClientError("Caption area: the rectangle goes past the edge of the frame.")


def finding_text(f: Finding) -> str:
    v = f.value
    if f.kind == "caption_area":
        return area_text(v)
    if f.kind == "exception":
        return "; ".join(f"{k}={v[k]}" for k in EXCEPTION_FIELDS if v.get(k) not in (None, ""))
    if f.kind == "example":
        text = f"{v.get('wrong', '')} -> {v.get('correct', '')}"
        return text + (f"  [{v['category']}]" if v.get("category") else "")
    if f.kind in ("check", "color"):
        return f"{v['setting']} = {_fmt(v['value'])}"
    return _fmt(v)


def parse_finding_text(kind: str, text: str):
    """The value for edited text; ClientError if it can't be read."""
    text = (text or "").strip()
    if kind == "caption_area":
        return parse_area(text)
    if kind == "min_words":
        try:
            n = int(text)
        except ValueError:
            raise ClientError(f"Minimum words: {text!r} isn't a whole number.") from None
        return n
    if kind == "exception":
        out = {}
        for part in text.split(";"):
            if "=" in part:
                k, val = part.split("=", 1)
                if k.strip() in EXCEPTION_FIELDS and val.strip():
                    out[k.strip()] = val.strip()
        if not out:
            out = {"text": text}
        return out
    if kind == "example":
        category = None
        m = re.search(r"\s*\[([^\]]+)\]\s*$", text)
        if m:
            category, text = m.group(1).strip(), text[:m.start()]
        wrong, sep, correct = text.partition("->")
        if not sep:
            raise ClientError(f"Example: write it as  wrong -> correct  ({text!r}).")
        out = {"wrong": wrong.strip(), "correct": correct.strip()}
        if category:
            out["category"] = category
        return out
    if kind in ("check", "color"):
        setting, sep, value = text.partition("=")
        if not sep or not setting.strip():
            raise ClientError(f"Setting: write it as  name = value  ({text!r}).")
        return {"setting": setting.strip(), "value": _scalar(value)}
    if kind == "forced":
        return text.lstrip("=").strip()
    return text


def edited(f: Finding, text: str) -> Finding:
    return replace(f, value=parse_finding_text(f.kind, text))


# --- validation ----------------------------------------------------------------------------

def clean_code(code: str | None) -> str:
    """The normalized code, or ClientError."""
    normalized = normalize_client_code(code or "")
    if not normalized or not _CODE.fullmatch(normalized):
        raise ClientError("Client code: 2-20 letters, digits or _ (e.g. E8D or 8D).")
    return normalized


def validate_finding(f: Finding) -> None:
    v = f.value
    if f.kind == "name" and (not str(v).strip() or len(str(v)) > 100):
        raise ClientError("Display name: 1-100 characters.")
    if f.kind == "language" and str(v) not in LANGUAGES:
        raise ClientError(f"Language: one of {', '.join(LANGUAGES)} ({v!r} given).")
    if f.kind == "min_words" and not (isinstance(v, int) and 1 <= v <= 10):
        raise ClientError("Minimum words per caption: a whole number from 1 to 10.")
    if f.kind == "caption_area" and v is not None:
        validate_region(v)
    if f.kind == "color_skip" and v not in COLOR_TYPES:
        raise ClientError(f"Color type to skip: one of {', '.join(COLOR_TYPES)} ({v!r} given).")
    if f.kind == "check":
        head = str(v["setting"]).split(".")[0]
        if head not in CHECK_NAMES:
            raise ClientError(f"Check setting {v['setting']!r}: starts with a check name ({', '.join(CHECK_NAMES)}).")
        if v["setting"] == "audio_guidelines.sensitivity" and v["value"] not in GUIDE_SENSITIVITIES:
            raise ClientError(f"Audio guidelines sensitivity: one of {', '.join(GUIDE_SENSITIVITIES)}.")
        for name, places in (("music_allowed", MUSIC_PLACES), ("sfx_allowed", SFX_PLACES)):
            if v["setting"] == f"audio_guidelines.{name}":
                bad = [x for x in (v["value"] or []) if x not in places]
                if not isinstance(v["value"], list) or bad:
                    raise ClientError(f"Audio guidelines {name}: a list of {', '.join(places)}.")
    if f.kind in ("rule", "glossary", "forced", "note") and not str(v).strip():
        raise ClientError(f"{KINDS[f.kind]}: empty.")
    if f.kind == "glossary" and "\n" in str(v):
        raise ClientError("Glossary word: one term per line.")
    if f.kind == "exception":
        if not any(v.get(k) for k in ("type", "text", "video", "at")):
            raise ClientError("Exception: give at least a type, text, video or time (otherwise it would hide "
                              "every flag).")
    if f.kind == "example" and not (v.get("wrong") or v.get("correct")):
        raise ClientError("Example: wrong and correct are both empty.")


# --- the client file <-> findings ------------------------------------------------------------

def _flatten(d: dict, prefix: str = "") -> Iterable[tuple[str, Any]]:
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            yield from _flatten(v, key + ".")
        else:
            yield key, v


def _set_path(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    for p in parts[:-1]:
        if not isinstance(d.get(p), dict):
            d[p] = {}
        d = d[p]
    d[parts[-1]] = value


def findings_from_client(client: dict, color_block: dict | None = None) -> list[Finding]:
    """Everything in a client file (and its color.yaml block) as findings marked "current file"."""
    src = "current file"
    out: list[Finding] = []
    code = client.get("client")
    if code:
        out.append(Finding("code", code, src))
    name = client.get("name") or client.get("project")
    if name:
        out.append(Finding("name", str(name), src))
    out.append(Finding("language", client.get("language") or "en", src))
    overrides = copy.deepcopy(client.get("overrides") or {})
    min_words = (overrides.get("too_short") or {}).pop("min_words", None)
    if min_words is not None:
        out.append(Finding("min_words", int(min_words), src))
    region = (client.get("ocr") or {}).get("region")
    if region and region != "full":
        out.append(Finding("caption_area", parse_area(region), src))
    for rule in client.get("rules") or []:
        out.append(Finding("rule", str(rule), src))
    for term in client.get("glossary") or []:
        term = str(term)
        out.append(Finding("forced", term[1:], src) if term.startswith("=") else Finding("glossary", term, src))
    for rule in client.get("ignore") or []:
        out.append(Finding("exception", {k: str(v) for k, v in rule.items() if k in EXCEPTION_FIELDS}, src))
    for ex in client.get("examples") or []:
        out.append(Finding("example", dict(ex), src))
    for setting, value in _flatten({k: v for k, v in overrides.items() if v not in ({}, None)}):
        out.append(Finding("check", {"setting": setting, "value": value}, src))
    for block in OWN_BLOCKS:                                       # the audio checks' own blocks
        for setting, value in _flatten(client.get(block) or {}, block + "."):
            out.append(Finding("check", {"setting": setting, "value": value}, src))
    color_block = dict(color_block or {})
    for kind in color_block.pop("skip", None) or []:
        out.append(Finding("color_skip", str(kind), src))
    for setting, value in _flatten(color_block):
        out.append(Finding("color", {"setting": setting, "value": value}, src))
    return out


def merge_findings(*groups: Iterable[Finding]) -> list[Finding]:
    """All findings, the same thing found twice only once (the first). Single values (name,
    language...) found again are kept but unticked, so the reviewer chooses."""
    out: list[Finding] = []
    seen: set = set()
    single_seen: set = set()
    for group in groups:
        for f in group:
            key = (f.kind, finding_text(f).casefold())
            if key in seen:
                continue
            seen.add(key)
            if f.kind in SINGLE_KINDS or f.kind in ("check", "color"):
                slot = f.kind if f.kind in SINGLE_KINDS else (f.kind, f.value["setting"])
                if slot in single_seen:
                    f = replace(f, keep=False)
                single_seen.add(slot)
            out.append(f)
    return out


@dataclass
class BuiltClient:
    code: str
    client: dict                         # the client file's contents
    color: dict                          # this client's block in color.yaml ({} = none)
    notes: list[str] = field(default_factory=list)      # lines for notes.md


def build_client(findings: Iterable[Finding], base: dict | None = None, notes_text: str | None = None) -> BuiltClient:
    """The client file from the kept findings, on top of `base` (the current file: keys this
    module doesn't manage are kept). ClientError on the first invalid finding."""
    kept = [f for f in findings if f.keep]
    for f in kept:
        validate_finding(f)
    singles: dict[str, Finding] = {}
    for f in kept:
        if f.kind in SINGLE_KINDS:
            if f.kind in singles:
                raise ClientError(f"{KINDS[f.kind]} is ticked twice ({finding_text(singles[f.kind])!r} and "
                                  f"{finding_text(f)!r}): keep one.")
            singles[f.kind] = f
    if "code" not in singles:
        raise ClientError("Client code: required.")
    code = clean_code(singles["code"].value)
    if "name" not in singles:
        raise ClientError("Display name: required.")

    client = copy.deepcopy(base or {})
    for key in ("rules", "glossary", "ignore", "examples", "overrides", *OWN_BLOCKS):
        client.pop(key, None)
    client["client"] = code
    client["name"] = str(singles["name"].value).strip()
    client["language"] = str(singles["language"].value) if "language" in singles else "en"
    if notes_text is not None:
        client["notes"] = notes_text.strip()
    client.setdefault("notes", "")

    rules = [str(f.value).strip() for f in kept if f.kind == "rule"]
    glossary = [str(f.value).strip() for f in kept if f.kind == "glossary"]
    glossary += ["=" + str(f.value).strip() for f in kept if f.kind == "forced"]
    ignore = [{k: v for k, v in f.value.items() if k in EXCEPTION_FIELDS and v not in (None, "")}
              for f in kept if f.kind == "exception"]
    examples = [{k: v for k, v in f.value.items() if v not in (None, "")} for f in kept if f.kind == "example"]
    overrides: dict = {}
    own: dict = {block: {} for block in OWN_BLOCKS}
    for f in kept:
        if f.kind == "check":
            setting = f.value["setting"]
            block = setting.split(".", 1)[0]
            if block in OWN_BLOCKS and "." in setting:  # the client file's own block, not "overrides:"
                _set_path(own[block], setting[len(block) + 1:], f.value["value"])
            else:
                _set_path(overrides, setting, f.value["value"])
    if "min_words" in singles:
        _set_path(overrides, "too_short.min_words", int(singles["min_words"].value))
    for key, value in (("rules", rules), ("glossary", list(dict.fromkeys(glossary))), ("ignore", ignore),
                       ("examples", examples)):
        if value:
            client[key] = value
    client["overrides"] = overrides
    for block in OWN_BLOCKS:
        if own[block]:
            client[block] = own[block]

    ocr = dict(client.get("ocr") or {})
    area = singles.get("caption_area")
    if area is not None and area.value is not None:
        ocr["region"] = dict(area.value)
    else:
        ocr.pop("region", None)
    if ocr:
        client["ocr"] = ocr
    else:
        client.pop("ocr", None)

    color: dict = {}
    skip = list(dict.fromkeys(str(f.value) for f in kept if f.kind == "color_skip"))
    if skip:
        color["skip"] = skip
    for f in kept:
        if f.kind == "color":
            _set_path(color, f.value["setting"], f.value["value"])

    notes = [f"{finding_text(f)}  ({f.source})" if f.source else finding_text(f) for f in kept if f.kind == "note"]
    return BuiltClient(code=code, client=_ordered(client), color=color, notes=notes)


_ORDER = ("client", "name", "project", "language", "notes", "rules", "glossary", "examples", "ignore", "overrides",
          "ocr", "color", "audio", "audio_guidelines")


def _ordered(client: dict) -> dict:
    out = {k: client[k] for k in _ORDER if k in client}
    out.update({k: v for k, v in client.items() if k not in out})
    return out


def describe_changes(old: dict, new: dict, prefix: str = "") -> str:
    """One line for the changelog: what changed between two versions of a client's data."""
    parts = []
    keys = list(dict.fromkeys([*old, *new]))
    for key in keys:
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        name = f"{prefix}{key}"
        if isinstance(a, list) or isinstance(b, list):
            a_items, b_items = [_fmt_item(x) for x in a or []], [_fmt_item(x) for x in b or []]
            added = [x for x in b_items if x not in a_items]
            removed = [x for x in a_items if x not in b_items]
            bits = []
            if added:
                bits.append("+" + ", +".join(_short(x) for x in added[:5]) + (" ..." if len(added) > 5 else ""))
            if removed:
                bits.append("-" + ", -".join(_short(x) for x in removed[:5]) + (" ..." if len(removed) > 5 else ""))
            parts.append(f"{name}: {' '.join(bits) or 'reordered'}")
        elif isinstance(a, dict) or isinstance(b, dict):
            inner = describe_changes(a if isinstance(a, dict) else {}, b if isinstance(b, dict) else {}, name + ".")
            if inner:
                parts.append(inner)
        else:
            parts.append(f"{name}: {_short(_fmt(a)) if a is not None else '(none)'} -> "
                         f"{_short(_fmt(b)) if b is not None else '(none)'}")
    return "; ".join(parts)


def _fmt_item(x) -> str:
    if isinstance(x, dict):
        return ", ".join(f"{k}={v}" for k, v in x.items())
    return str(x)


def _short(text: str, n: int = 40) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."

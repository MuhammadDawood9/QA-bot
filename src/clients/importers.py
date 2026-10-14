"""Turning files about a client into findings to review: the client template (.xlsx), the QC
review analysis workbook (.xlsx), a .csv, or a style guide (.pdf). All local, no network.

    findings = import_file(path, code="E8D")       # any of the above
    make_template(path)                             # writes client_template.xlsx

Nothing here saves anything: the findings go to the review dialog, and only what the reviewer
confirms is written (src/clients/store.py).
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from src.clients.model import (AREA_PRESETS, CHECK_NAMES, COLOR_TYPES, LANGUAGES, ClientError, Finding,
                               parse_area)
from src.clients.workbook import clean, client_for, find_sheet, is_analysis_workbook, read_rows
from src.config import normalize_client_code

IMPORT_EXTENSIONS = (".xlsx", ".csv", ".pdf")

# --- the client template ------------------------------------------------------------------------

TEMPLATE_NAME = "client_template.xlsx"
TEMPLATE_SHEETS: dict[str, list[str]] = {
    "Info": ["Field", "Value", "Help"],
    "Rules": ["Rule", "Do or Don't", "Note"],
    "Glossary": ["Word", "Forced spelling (yes/no)", "Note"],
    "Exceptions": ["Type", "Text", "Video", "Time", "Note"],
    "Examples": ["Wrong", "Correct", "Category", "Note"],
    "Checks": ["Check", "Setting", "Value", "Note"],
    "Audio guidelines": ["Setting", "Value", "Help"],
}
# Podcasts only: left empty, nothing is imported (a caption-only client gets no audio guidelines).
_GUIDE_ROWS = [
    ("music_allowed", "", "where music is placed on purpose (never flagged there): intro, outro, transitions, "
                          "ad_breaks, under_speech (comma-separated)"),
    ("sfx_allowed", "", "where sound effects are placed on purpose: transitions, stingers, emphasis"),
    ("reference_folder", "", "folder with the client's stingers, jingles, beds and effects (recognized in each episode)"),
    ("sensitivity", "", "strict / normal / lenient: how much around each music or effect is ignored (0.15 / 0.3 / 0.6 s)"),
    ("required_cues.enabled", "", "yes = report a required cue that's missing (list them in the client file)"),
]
INFO_FIELDS = {
    "client code": "code", "code": "code",
    "display name": "name", "name": "name",
    "language": "language",
    "minimum words per caption": "min_words", "minimum words": "min_words", "min words": "min_words",
    "caption area": "caption_area", "caption position": "caption_area",
}
_INFO_ROWS = [
    ("Client code", "", "Like E8D or 8D (the E is added). Must not be in use already."),
    ("Display name", "", "Shown in the Client menu, e.g. \"Danja promo videos\"."),
    ("Language", "en", "en, es, fr, de, pt or it. Used for spelling and speech."),
    ("Minimum words per caption", 2, "A caption with fewer words is flagged (one-word captions: 2)."),
    ("Caption area", "full", "full, bottom, middle, top, or left, top, width, height as fractions "
                             "(0.05, 0.62, 0.9, 0.33). Easier: Edit client > Pick caption area."),
]
_CHECK_HELP = [
    ("too_short", "enabled", "yes", "yes / no switches a whole check on or off for this client"),
    ("capitalization", "allow_stylized_lowercase", "no", "yes for clients who write in lowercase on purpose"),
    ("too_short", "phrase_break.fragment_max_words", 0, "a nested setting: names joined with dots"),
    ("color", "skip", "", "color issue types not to report, comma-separated: " + ", ".join(COLOR_TYPES)),
    ("color", "cast_strength", 6, "any threshold from color.yaml; leave out to keep the default"),
]


def make_template(path: Path) -> Path:
    """Write the client template: one sheet per part of a client's rules, a header row, help text
    and an example row in grey (rows starting with "e.g." are skipped when imported)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    wb.remove(wb.active)
    bold, fill, grey = Font(bold=True), PatternFill("solid", fgColor="D9E2F3"), Font(color="808080", italic=True)
    examples = {
        "Rules": [("e.g. Never animate the word \"the\" on its own", "Don't", "")],
        "Glossary": [("e.g. Danja", "no", "the brand"), ("e.g. YouTube", "yes", "exactly this capitalization")],
        "Exceptions": [("e.g. spelling", "Gherabel", "", "", "the guest's name: never flag it")],
        "Examples": [("e.g. aging", "Aging", "capitalization", "title word")],
    }
    for name, header in TEMPLATE_SHEETS.items():
        ws = wb.create_sheet(name)
        ws.append(header)
        for cell in ws[1]:
            cell.font, cell.fill = bold, fill
        if name == "Info":
            for row in _INFO_ROWS:
                ws.append(list(row))
            for row in ws.iter_rows(min_row=2, min_col=3, max_col=3):
                row[0].font = grey
        elif name == "Audio guidelines":
            for row in _GUIDE_ROWS:
                ws.append(list(row))
            for row in ws.iter_rows(min_row=2, min_col=3, max_col=3):
                row[0].font = grey
        elif name == "Checks":
            for check, setting, value, note in _CHECK_HELP:
                ws.append([f"e.g. {check}", setting, value, note])
                for cell in ws[ws.max_row]:
                    cell.font = grey
        else:
            for row in examples.get(name, []):
                ws.append(list(row))
                for cell in ws[ws.max_row]:
                    cell.font = grey
        for col, width in zip("ABCDE", (34, 30, 30, 14, 50)):
            ws.column_dimensions[col].width = width
        ws.freeze_panes = "A2"
    yes_no = DataValidation(type="list", formula1='"yes,no"', allow_blank=True)
    wb["Glossary"].add_data_validation(yes_no)
    yes_no.add("B2:B500")
    dos = DataValidation(type="list", formula1='"Do,Don\'t"', allow_blank=True)
    wb["Rules"].add_data_validation(dos)
    dos.add("B2:B500")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _example_row(values) -> bool:
    first = next((clean(v) for v in values if clean(v)), "")
    return first.lower().startswith("e.g.")


def _yes(text: str) -> bool | None:
    t = clean(text).lower()
    if t in ("yes", "y", "true", "on", "1", "x"):
        return True
    if t in ("no", "n", "false", "off", "0"):
        return False
    return None


def _template_rows(ws) -> list[list[str]]:
    rows = []
    for values in ws.iter_rows(min_row=2, values_only=True):
        values = [clean(v) for v in values]
        if any(values) and not _example_row(values):
            rows.append(values + [""] * 6)
    return rows


def import_template(wb, source: str) -> list[Finding]:
    out: list[Finding] = []
    sheets = {name.lower(): wb[name] for name in wb.sheetnames}

    def where(sheet: str, n: int) -> str:
        return f"{source}, {sheet} row {n + 2}"

    if "info" in sheets:
        for n, (field_name, value, *_rest) in enumerate(_template_rows(sheets["info"])):
            kind = INFO_FIELDS.get(field_name.lower().rstrip(":"))
            if kind is None:
                if field_name or value:
                    out.append(Finding("note", f"{field_name}: {value}".strip(": "), where("Info", n)))
                continue
            if not value:
                continue
            try:
                if kind == "code":
                    out.append(Finding("code", normalize_client_code(value), where("Info", n)))
                elif kind == "min_words":
                    out.append(Finding("min_words", int(float(value)), where("Info", n)))
                elif kind == "caption_area":
                    out.append(Finding("caption_area", parse_area(value), where("Info", n)))
                elif kind == "language":
                    out.append(Finding("language", _language_code(value) or value.lower(), where("Info", n)))
                else:
                    out.append(Finding(kind, value, where("Info", n)))
            except (ValueError, ClientError) as exc:
                out.append(Finding("note", f"{field_name}: {value} (not read: {exc})", where("Info", n)))
    if "rules" in sheets:
        for n, (rule, do, note, *_rest) in enumerate(_template_rows(sheets["rules"])):
            if not rule:
                continue
            prefix = "Don't: " if do.lower().startswith("don") else "Do: " if do.lower().startswith("do") else ""
            out.append(Finding("rule", prefix + rule + (f" ({note})" if note else ""), where("Rules", n)))
    if "glossary" in sheets:
        for n, (word, forced, *_rest) in enumerate(_template_rows(sheets["glossary"])):
            if word:
                kind = "forced" if _yes(forced) or word.startswith("=") else "glossary"
                out.append(Finding(kind, word.lstrip("="), where("Glossary", n)))
    if "exceptions" in sheets:
        for n, (kind, text, video, at, note, *_rest) in enumerate(_template_rows(sheets["exceptions"])):
            value = {k: v for k, v in (("type", kind.lower()), ("text", text), ("video", video), ("at", at),
                                       ("note", note)) if v}
            if any(value.get(k) for k in ("type", "text", "video", "at")):
                out.append(Finding("exception", value, where("Exceptions", n)))
            elif note:
                out.append(Finding("note", note, where("Exceptions", n)))
    if "examples" in sheets:
        for n, (wrong, correct, category, note, *_rest) in enumerate(_template_rows(sheets["examples"])):
            if wrong or correct:
                value = {"wrong": wrong, "correct": correct}
                if category:
                    value["category"] = category.lower()
                if note:
                    value["note"] = note
                out.append(Finding("example", value, where("Examples", n)))
    if "checks" in sheets:
        for n, (check, setting, value, note, *_rest) in enumerate(_template_rows(sheets["checks"])):
            out += _check_row(check.lower(), setting, value, note, where("Checks", n))
    if "audio guidelines" in sheets:
        from src.clients.model import _scalar

        for n, (setting, value, *_rest) in enumerate(_template_rows(sheets["audio guidelines"])):
            setting = setting.strip()
            if not setting or not str(value).strip():
                continue
            if setting in ("music_allowed", "sfx_allowed"):
                parsed = [x.strip().lower().replace(" ", "_") for x in re.split(r"[,;]", value) if x.strip()]
            elif setting == "reference_folder":
                if not value:
                    continue
                parsed = value
            elif setting == "sensitivity":
                parsed = (value or "normal").strip().lower()
            else:
                parsed = _scalar(value)
            out.append(Finding("check", {"setting": f"audio_guidelines.{setting}", "value": parsed},
                               where("Audio guidelines", n)))
    return out


def _check_row(check: str, setting: str, value: str, note: str, source: str) -> list[Finding]:
    from src.clients.model import _scalar

    setting = setting.strip() or "enabled"
    if check == "color":
        if setting == "skip":
            kinds = [k.strip().lower() for k in re.split(r"[,;]", value) if k.strip()]
            return [Finding("color_skip", k, source, keep=k in COLOR_TYPES) for k in kinds]
        return [Finding("color", {"setting": setting, "value": _scalar(value)}, source)]
    if check not in CHECK_NAMES:
        return [Finding("note", f"Checks: {check} {setting} = {value} {note}".strip(), source)]
    if check == "too_short" and setting == "min_words":
        try:
            return [Finding("min_words", int(float(value)), source)]
        except ValueError:
            pass
    return [Finding("check", {"setting": f"{check}.{setting}", "value": _scalar(value)}, source)]


# --- the QC review analysis workbook --------------------------------------------------------------

def import_analysis_workbook(wb, code: str | None, source: str) -> list[Finding]:
    """One client's rows from the QC review analysis workbook: its "Client rules" row (name, rule
    text, and whatever that text says about minimum words, lowercase style...), its caption
    mistakes as examples, its accepted exceptions as notes."""
    code = normalize_client_code(code)
    out: list[Finding] = []
    clients_seen: set[str] = set()
    sheet = find_sheet(wb, "Client rules", required=False)
    for n, row in enumerate(read_rows(sheet) if sheet is not None else []):
        row_code = normalize_client_code(row.get("client_code", ""))
        clients_seen.add(row_code or "")
        if code is None or row_code != code:
            continue
        where = f"{source}, Client rules row {n + 2}"
        out.append(Finding("code", row_code, where))
        if row.get("project (from task name)"):
            out.append(Finding("name", row["project (from task name)"], where))
        rule = row.get("client-specific rule", "")
        if rule:
            out.append(Finding("rule", rule, where))
            out += [f for f in detect_from_text(rule, where) if f.kind not in ("rule", "note")]
    sheet = find_sheet(wb, "Caption mistakes", required=False)
    for n, row in enumerate(read_rows(sheet) if sheet is not None else []):
        clients_seen.add(client_for(row))
        if code is None or client_for(row) != code:
            continue
        wrong, right = row.get("editor_wrote", ""), row.get("should_be", "")
        where = f"{source}, {sheet.title} row {n + 2}"
        if wrong or right:
            value = {"wrong": wrong, "correct": right}
            if row.get("category"):
                value["category"] = row["category"]
            if row.get("comment_text"):
                value["note"] = row["comment_text"][:200]
            out.append(Finding("example", value, where))
            # A name corrected in a spelling note is a glossary candidate: shown, not ticked.
            if row.get("category") == "spelling" and right and len(right.split()) <= 3 and right[:1].isupper():
                out.append(Finding("forced", right, where, keep=False))
    sheet = find_sheet(wb, "Accepted exceptions", required=False)
    for n, row in enumerate(read_rows(sheet) if sheet is not None else []):
        if code is not None and normalize_client_code(row.get("client", "")) == code and row.get("what you accepted"):
            out.append(Finding("note", "Accepted: " + row["what you accepted"],
                               f"{source}, Accepted exceptions row {n + 2}"))
    if not out:
        known = ", ".join(sorted(c for c in clients_seen if c)) or "none"
        out.append(Finding("note", f"No rows for {code or '(no code given)'} in this workbook. Clients in it: {known}.",
                           source, keep=False))
    return out


# --- CSV -------------------------------------------------------------------------------------------

def import_csv(path: Path, code: str | None) -> list[Finding]:
    """A CSV saved from one of the template's sheets (recognized by its header), or rows in the QC
    notes format (task_name, client, editor_wrote, should_be...). Anything else: notes."""
    source = Path(path).name
    text = Path(path).read_bytes().decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = [clean(h).lower() for h in rows[0]]
    for sheet, columns in TEMPLATE_SHEETS.items():
        if header[:2] == [c.lower() for c in columns[:2]]:
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            ws.title = sheet
            for row in rows:
                ws.append(row)
            return import_template(wb, source)
    if {"editor_wrote", "should_be"} <= set(header) or "client_code" in header:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Client rules" if "client_code" in header else "Caption mistakes (csv)"
        for row in rows:
            ws.append(row)
        return import_analysis_workbook(wb, code, source)
    out = []
    for n, row in enumerate(rows):
        line = ", ".join(clean(c) for c in row if clean(c))
        if line:
            out.append(Finding("note", line, f"{source}, row {n + 1}"))
    return out


# --- PDF and plain text ------------------------------------------------------------------------------

class PdfError(RuntimeError):
    pass


def pdf_text(path: Path) -> str:
    """The text of a PDF, page by page, read locally (pypdf). Scanned PDFs (pictures of text) have
    no text to read: PdfError then."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:                      # pragma: no cover - pinned in requirements.txt
        raise PdfError(f"pypdf isn't installed: {exc}") from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise PdfError(f"{Path(path).name} is password-protected.") from None
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except (PdfReadError, OSError, ValueError) as exc:
        raise PdfError(f"couldn't read {Path(path).name}: {exc}") from exc
    if not text.strip():
        raise PdfError(f"{Path(path).name} has no text to read (a scan?). Type its rules into the template instead.")
    return text


_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_MIN_WORDS = [
    re.compile(r"(?:minimum|min\.?|at least|no fewer than|no less than)\s+(?:of\s+)?(\d+|one|two|three|four|five|six)"
               r"\s+words?", re.I),
    re.compile(r"(\d+|one|two|three|four|five|six)\s*(?:words?)?\s*(?:minimum|min\.?)\b", re.I),
    re.compile(r"(\d+|one|two|three|four|five|six)\s*(?:-|–|to)\s*(?:\d+|one|two|three|four|five|six|seven|eight)"
               r"\s+words?\s+(?:per|a|each|on)\s+(?:caption|line|subtitle|screen)", re.I),
    re.compile(r"(?:never|no|don'?t use)\s+(?:a\s+)?(single|one)[- ]word\s+(?:captions?|lines?|subtitles?)", re.I),
]
_POSITION = re.compile(r"(?:captions?|subtitles?|text)[^.\n]{0,60}?\b(bottom|lower third|lower|centre|center|middle|top|"
                       r"upper)\b|\b(bottom|lower third|centre|center|middle|top)\b[^.\n]{0,30}?(?:captions?|subtitles?)",
                       re.I)
_POSITION_WORDS = {"bottom": "bottom", "lower third": "bottom", "lower": "bottom", "centre": "middle",
                   "center": "middle", "middle": "middle", "top": "top", "upper": "top"}
_RULE_START = re.compile(r"^(?:[-•*▪●◦·]\s*|\d+[.)]\s*)?(do not|don'?t|never|always|avoid|must|should|use|no |only|"
                         r"keep|make sure|ensure|do\b|please)", re.I)
_DONT = re.compile(r"^(?:do not|don'?t|never|avoid|no )", re.I)
_HEADING_GLOSSARY = re.compile(r"\b(glossary|names?|spellings?|terminology|brand names|people|key terms|"
                               r"vocabulary|word list)\b", re.I)
_SPELL_AS = re.compile(r"(?:always\s+)?(?:spell(?:ed)?|write|written|capitali[sz]ed?)\s+(?:it\s+)?(?:as\s+)?"
                       r"[\"“']([^\"”']{2,40})[\"”']", re.I)
_LOWERCASE = re.compile(r"\b(?:all[- ]lower[- ]?case|lower[- ]?case (?:style|captions|text|allowed)|stylized lowercase|"
                        r"small letters)\b", re.I)
_LANGUAGE = re.compile(r"\b(?:language|captions? in|subtitles? in)\s*[:\-]?\s*(english|spanish|french|german|"
                       r"portuguese|italian)\b", re.I)
_BULLET = re.compile(r"^\s*(?:[-•*▪●◦·]|\d+[.)])\s*")


def _language_code(name: str) -> str | None:
    name = name.strip().lower()
    if name in LANGUAGES:
        return name
    for code, full in LANGUAGES.items():
        if full.lower() == name:
            return code
    return None


def _number(word: str) -> int:
    return int(word) if word.isdigit() else _NUMBERS.get(word.lower(), 2)


def _split_terms(line: str) -> list[str]:
    line = _BULLET.sub("", line)
    if ":" in line and len(line.split(":", 1)[0].split()) <= 4:
        line = line.split(":", 1)[1]
    parts = [p.strip(" .;\"“”'") for p in re.split(r"[,;•|]|\s{2,}", line)]
    return [p for p in parts if 1 < len(p) <= 40 and len(p.split()) <= 4 and re.search(r"[A-Za-z]", p)]


def detect_from_text(text: str, source: str) -> list[Finding]:
    """What a style guide's text says that the bot can use: minimum words, caption position,
    language, lowercase style, glossary / name lists, spell-it-like-this notes, do / don't rules.
    Every other line becomes a note, so nothing is lost. Heuristics: the reviewer checks them all."""
    out: list[Finding] = []
    used: set[int] = set()
    lines = [" ".join(line.split()) for line in text.splitlines()]

    for n, line in enumerate(lines):
        if not line:
            continue
        where = f"{source}, line {n + 1}"
        for pattern in _MIN_WORDS:
            m = pattern.search(line)
            if m:
                word = m.group(1)
                value = 2 if word.lower() in ("single", "one") and pattern is _MIN_WORDS[3] else _number(word)
                out.append(Finding("min_words", value, where))
                used.add(n)
                break
        m = _POSITION.search(line)
        if m:
            word = (m.group(1) or m.group(2)).lower()
            out.append(Finding("caption_area", dict(AREA_PRESETS[_POSITION_WORDS[word]]), where))
            used.add(n)
        m = _LANGUAGE.search(line)
        if m:
            out.append(Finding("language", _language_code(m.group(1)), where))
            used.add(n)
        if _LOWERCASE.search(line):
            out.append(Finding("check", {"setting": "capitalization.allow_stylized_lowercase", "value": True}, where))
            used.add(n)
        for m in _SPELL_AS.finditer(line):
            out.append(Finding("forced", m.group(1).strip(), where))
            used.add(n)
        if _RULE_START.match(line) and len(line.split()) >= 3:
            body = _BULLET.sub("", line)
            prefix = "Don't: " if _DONT.match(body) else "Do: "
            out.append(Finding("rule", prefix + body, where))
            used.add(n)

    # Lists under a glossary / names heading: the lines after it, until a blank line or a new heading.
    n = 0
    while n < len(lines):
        line = lines[n]
        heading = line and len(line.split()) <= 6 and _HEADING_GLOSSARY.search(line)
        inline = heading and ":" in line and line.split(":", 1)[1].strip()
        if heading:
            used.add(n)
            if inline:
                for term in _split_terms(line):
                    out.append(Finding("glossary", term, f"{source}, line {n + 1}"))
            m = n + 1
            while m < len(lines) and lines[m] and not (len(lines[m].split()) <= 4 and lines[m].endswith(":")):
                terms = _split_terms(lines[m])
                if not terms or _RULE_START.match(lines[m]):
                    break
                for term in terms:
                    out.append(Finding("glossary", term, f"{source}, line {m + 1}"))
                used.add(m)
                m += 1
            n = m
        n += 1

    # Names in quotes ("Mark McGwire") anywhere: glossary candidates, not ticked.
    for n, line in enumerate(lines):
        for m in re.finditer(r"[\"“]([A-Z][\w'’.-]+(?:\s+[A-Z][\w'’.-]+){0,3})[\"”]", line):
            out.append(Finding("glossary", m.group(1), f"{source}, line {n + 1}", keep=False))

    for n, line in enumerate(lines):
        if line and n not in used:
            out.append(Finding("note", line, f"{source}, line {n + 1}"))
    return out


def import_pdf(path: Path) -> list[Finding]:
    return detect_from_text(pdf_text(path), Path(path).name)


# --- one entry point -----------------------------------------------------------------------------------

def import_file(path: Path, code: str | None = None) -> list[Finding]:
    """Findings from any supported file. ClientError (with a message for the user) if it can't be read."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in IMPORT_EXTENSIONS:
        raise ClientError(f"{path.name}: only .xlsx, .csv and .pdf files can be imported.")
    try:
        if ext == ".pdf":
            return import_pdf(path)
        if ext == ".csv":
            return import_csv(path, code)
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=False, data_only=True)
        try:
            names = {n.lower() for n in wb.sheetnames}
            if names & {s.lower() for s in TEMPLATE_SHEETS}:
                return import_template(wb, path.name)
            if is_analysis_workbook(wb):
                return import_analysis_workbook(wb, code, path.name)
            raise ClientError(f"{path.name}: not the client template (sheets Info, Rules, Glossary...) or the QC "
                              f"review analysis workbook. Sheets: {', '.join(wb.sheetnames)}")
        finally:
            wb.close()
    except PdfError as exc:
        raise ClientError(str(exc)) from exc
    except ClientError:
        raise
    except Exception as exc:                       # a damaged or unexpected file: say so, don't crash
        raise ClientError(f"{path.name} couldn't be read ({type(exc).__name__}: {exc}).") from exc


# --- optional: Claude reads the PDF (admin-enabled, API key needed) -----------------------------------

CLAUDE_SCHEMA = {
    "type": "object",
    "properties": {
        "min_words": {"type": ["integer", "null"]},
        "caption_position": {"type": ["string", "null"], "enum": ["bottom", "middle", "top", None]},
        "language": {"type": ["string", "null"]},
        "glossary": {"type": "array", "items": {"type": "string"}},
        "forced_spellings": {"type": "array", "items": {"type": "string"}},
        "rules": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["min_words", "caption_position", "language", "glossary", "forced_spellings", "rules"],
    "additionalProperties": False,
}


def claude_allowed(config: dict) -> bool:
    """The "Read with Claude" button shows only if an admin switched it on in the shared settings
    (client_import.allow_claude: true) and this PC has an API key. Off by default."""
    from src.utils.llm import has_api_key

    return bool((config.get("client_import") or {}).get("allow_claude", False)) and has_api_key()


def read_with_claude(text: str, source: str, settings: dict, client=None) -> list[Finding]:
    """The same findings, read by Claude instead of the heuristics. Every one is still reviewed."""
    import json

    from src.utils.llm import ClaudeClient

    client = client or ClaudeClient.from_settings(settings)
    system = ("You read caption style guides for a video QC team. Extract only what the guide states. "
              "Answer in JSON matching the schema; use null or [] for anything the guide doesn't say.")
    data = json.loads(client.complete_json(system, text[:60000], CLAUDE_SCHEMA))
    where = f"{source} (read by Claude)"
    out: list[Finding] = []
    if data.get("min_words"):
        out.append(Finding("min_words", int(data["min_words"]), where))
    if data.get("caption_position") in AREA_PRESETS:
        out.append(Finding("caption_area", dict(AREA_PRESETS[data["caption_position"]]), where))
    if data.get("language") and _language_code(data["language"]):
        out.append(Finding("language", _language_code(data["language"]), where))
    out += [Finding("glossary", t, where) for t in data.get("glossary") or [] if str(t).strip()]
    out += [Finding("forced", t, where) for t in data.get("forced_spellings") or [] if str(t).strip()]
    out += [Finding("rule", r, where) for r in data.get("rules") or [] if str(r).strip()]
    return out

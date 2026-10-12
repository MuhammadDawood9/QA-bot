"""The QC report as a Word document, from python-docx.

Landscape page. A header (video, client, which checks ran), a summary of the issue counts per
type and severity, then one table with a row per issue, in time order: time, a screenshot of the
frame, the original text, what's wrong, the suggested fix, and confidence. An appendix lists every
caption, flagged ones in bold. Header rows repeat on every page and rows never split across pages.

Saved as reports/<video>_<date-time>/<video>_QC_<date>.docx.
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from src.ignore import glossary_line, rule_line
from src.models import SEVERITIES
from src.report.builder import ReportData, captions_source, label
from src.utils.timecode import format_timestamp

PAGE_SIZES = {"a4": (21.0, 29.7), "letter": (21.59, 27.94)}     # cm, portrait; the report turns them landscape
MARGIN_CM = 1.5
SEVERITY_COLORS = {"high": "C00000", "medium": "C55A11", "low": "7F7F7F"}
HEADER_FILL = "D9E2F3"       # light blue header rows
FLAGGED_FILL = "FFF2CC"      # light yellow for flagged captions in the appendix
SHOT_WIDTH_CM = 4.2          # screenshot width in the table (landscape frames)
SHOT_HEIGHT_CM = 5.0         # screenshot height for vertical (9:16) frames
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean(text) -> str:
    """Text Word can store: one line, no control characters (they would make the file invalid)."""
    return _CONTROL_CHARS.sub("", " ".join(str(text).split()))


def docx_name(data: ReportData) -> str:
    """<video>_QC_<date>.docx"""
    info = data.info
    stem = (info.video or info.srt or Path("report")).stem
    return f"{stem}_QC_{info.generated:%Y-%m-%d}.docx"


# --- small XML helpers python-docx doesn't offer -------------------------------------

def shade(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")           # "clear" + fill; "solid" would paint the cell black
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def repeat_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    flag = OxmlElement("w:tblHeader")
    flag.set(qn("w:val"), "true")
    tr_pr.append(flag)


def page_number_field(paragraph) -> None:
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "1"
    run.append(t)
    field.append(run)
    paragraph._p.append(field)


# --- tables ---------------------------------------------------------------------------

def table(doc, headers: list[str], widths_cm: list[float], right_align: set[int] = frozenset()):
    """A grid table with a shaded, repeating header row and fixed column widths."""
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    for i, (cell, text) in enumerate(zip(t.rows[0].cells, headers)):
        cell.text = ""
        run = cell.paragraphs[0].add_run(text)
        run.bold = True
        if i in right_align:
            cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        shade(cell, HEADER_FILL)
    repeat_header(t.rows[0])
    t._widths = widths_cm
    t._right = set(right_align)
    set_widths(t)
    return t


def set_widths(t) -> None:
    for row in t.rows:
        for cell, width in zip(row.cells, t._widths):
            cell.width = Cm(width)
    for column, width in zip(t.columns, t._widths):
        column.width = Cm(width)


def keep_row_together(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    tr_pr.append(OxmlElement("w:cantSplit"))


def add_row(t, values: list, bold: bool = False, fill: str | None = None, colors: dict | None = None):
    row = t.add_row()
    keep_row_together(row)
    for i, (cell, value) in enumerate(zip(row.cells, values)):
        cell.width = Cm(t._widths[i])
        paragraph = cell.paragraphs[0]
        if i in t._right:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        if callable(value):
            value(paragraph)                         # e.g. a screenshot, or rich text
        else:
            run = paragraph.add_run(clean(value))
            run.bold = bold
            if colors and i in colors:
                run.font.color.rgb = RGBColor.from_string(colors[i])
                run.bold = True
        if fill:
            shade(cell, fill)
    return row


# --- the document -----------------------------------------------------------------------

def picture(path: Path):
    """A cell filler: the screenshot, sized by its shape (wide frames by width, tall ones by height)."""
    from PIL import Image

    with Image.open(path) as img:
        w, h = img.size

    def fill(paragraph):
        size = {"width": Cm(SHOT_WIDTH_CM)} if w >= h else {"height": Cm(SHOT_HEIGHT_CM)}
        paragraph.add_run().add_picture(str(path), **size)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    return fill


def problem(issue):
    """A cell filler: the issue type and severity (colored), then the explanation."""
    def fill(paragraph):
        paragraph.add_run(label(issue.type)).bold = True
        sev = paragraph.add_run(f"  {issue.severity}")
        sev.bold = True
        sev.font.color.rgb = RGBColor.from_string(SEVERITY_COLORS[issue.severity])
        paragraph.add_run().add_break()
        paragraph.add_run(clean(issue.message))
    return fill


def write_docx(data: ReportData) -> Path:
    settings, info = data.settings, data.info
    doc = Document()
    short_cm, long_cm = PAGE_SIZES.get(str(settings.get("page_size", "a4")).lower(), PAGE_SIZES["a4"])
    content_cm = long_cm - 2 * MARGIN_CM
    for section in doc.sections:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = Cm(long_cm), Cm(short_cm)
        section.left_margin = section.right_margin = Cm(MARGIN_CM)
        section.top_margin = section.bottom_margin = Cm(MARGIN_CM)
    # python-docx's built-in template leaves out the zoom percentage the Word schema requires.
    zoom = doc.settings.element.find(qn("w:zoom"))
    if zoom is not None and zoom.get(qn("w:percent")) is None:
        zoom.set(qn("w:percent"), "100")
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)

    title = (info.video or info.srt).name
    doc.core_properties.title = f"QC report: {title}"
    doc.core_properties.author = "Video QC Bot"
    footer = doc.sections[0].footer.paragraphs[0]
    footer.text = f"QC report: {clean(title)}    |    page "
    page_number_field(footer)
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Header
    doc.add_heading(f"QC report: {clean(title)}", level=0)
    facts = [
        ("Video", info.video.name if info.video else "none (caption text checks only)"),
        ("Captions", f"{captions_source(info)}, {len(data.captions)} captions"),
        ("Client", info.client or "default rules"),
        ("Generated", f"{info.generated:%Y-%m-%d %H:%M}"),
        ("Rules version", info.rules_version or "unknown"),
    ]
    for name, value in facts:
        p = doc.add_paragraph()
        p.add_run(f"{name}: ").bold = True
        p.add_run(clean(value))
        p.paragraph_format.space_after = Pt(0)
    if info.checks:
        p = doc.add_paragraph()
        p.add_run("Checks:").bold = True
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(0)
        for line in info.checks:
            doc.add_paragraph(clean(line), style="List Bullet")

    # Summary: counts per issue type
    doc.add_heading("Summary", level=1)
    n = len(data.issues)
    on_captions = sum(1 for cid in data.flagged if cid > 0)
    p = doc.add_paragraph()
    p.add_run(f"{n} issue{'s' if n != 1 else ''}; {on_captions} of {len(data.captions)} captions flagged.").bold = True
    if not data.issues:
        doc.add_paragraph("No issues found.")
    else:
        counts = data.counts()
        rest = (content_cm - 8.0) / 4
        t = table(doc, ["Issue type", "High", "Medium", "Low", "Total"], [8.0, rest, rest, rest, rest],
                  right_align={1, 2, 3, 4})
        for kind in data.types():
            row = [counts[(kind, s)] for s in SEVERITIES]
            add_row(t, [label(kind)] + [str(v or "") for v in row] + [str(sum(row))])
        totals = [sum(counts[(k, s)] for k in data.types()) for s in SEVERITIES]
        add_row(t, ["All"] + [str(v) for v in totals] + [str(sum(totals))], bold=True)
        doc.add_paragraph("Severity: high is almost always a real mistake, medium usually is, low is worth a "
                          "glance (often a name to add to the glossary).").paragraph_format.space_before = Pt(6)

    # The issues, in time order
    if data.issues:
        doc.add_heading("Issues", level=1)
        fixed = 2.0 + SHOT_WIDTH_CM + 0.4 + 2.0
        text_cols = content_cm - fixed
        widths = [2.0, SHOT_WIDTH_CM + 0.4, text_cols * 0.26, text_cols * 0.42, text_cols * 0.32, 2.0]
        t = table(doc, ["Time", "Screenshot", "Original text", "What's wrong", "Suggested fix", "Confidence"],
                  widths, right_align={5})
        for issue in data.issues:
            shot = data.shots.get(issue.caption_id)
            if shot is not None:
                screenshot = picture(data.folder / shot[0])
            else:
                screenshot = "(no frame)" if data.wanted_shots else "(no video)"
            add_row(t, [format_timestamp(issue.timestamp), screenshot, issue.original, problem(issue),
                        issue.suggestion, f"{issue.confidence:.2f}"])

    # Appendix
    if data.captions:
        doc.add_heading("Appendix: all captions", level=1)
        doc.add_paragraph("Flagged captions are in bold on a yellow background.")
        t = table(doc, ["Time", "#", "Caption", "Issues"], [2.2, 1.2, content_cm - 2.2 - 1.2 - 6.0, 6.0],
                  right_align={1})
        for cap in data.captions:
            group = data.by_caption.get(cap.id, [])
            if group:
                found = ", ".join(dict.fromkeys(label(i.type) for i in group))
                add_row(t, [format_timestamp(cap.start), str(cap.id), cap.flat_text, found], bold=True,
                        fill=FLAGGED_FILL)
            else:
                add_row(t, [format_timestamp(cap.start), str(cap.id), cap.flat_text, ""])

    # Lines for ignore.yaml, so a false alarm can be hidden next time
    if data.issues:
        doc.add_heading("Hide a flag next time", level=1)
        doc.add_paragraph("If a flag is a false alarm, copy its line under \"ignore:\" in ignore.yaml (right-click the "
                          "mascot, Edit ignore list). If an unknown word is a correct name or brand, add it to the "
                          "glossary instead (Edit glossary, or glossary: in the client's file): then misspellings "
                          "of it are caught too. Start the line with = to make that exact spelling the only "
                          "accepted one.")
        gloss_cm = 5.0
        t = table(doc, ["Time", "Line for ignore.yaml", "Or add to the glossary"],
                  [2.2, content_cm - 2.2 - gloss_cm, gloss_cm])
        for issue in data.issues:
            add_row(t, [format_timestamp(issue.timestamp), rule_line(issue, info.client, info.video),
                        glossary_line(issue) or ""])

    path = data.folder / docx_name(data)
    doc.save(str(path))
    return path

"""Reading the QC review spreadsheet (the ClickUp analysis workbook, QC_Review_Analysis_*.xlsx).

Shared by scripts/prepare_data.py (which regenerates rules.yaml, the client files and the
examples from it) and the app's "Add / edit client" import (which takes one client's rows).

Sheets used: "Client rules" (client_code, project, client-specific rule), "Caption mistakes (N)"
(task_name, client, video_timestamp, editor_wrote, should_be, category, comment_text),
"Accepted exceptions" (client, what you accepted), "Caption rules" (area, rule, example) and
"All QC notes (N)" (same columns as the caption mistakes).
"""

from __future__ import annotations

import re

from src.config import normalize_client_code

_TASK_CLIENT = re.compile(r"#\s*([A-Za-z0-9_]+)")

# Sheet name prefixes that mark a workbook as the QC review analysis (any one of them is enough).
ANALYSIS_SHEETS = ("Client rules", "Caption mistakes", "Accepted exceptions", "All QC notes")


def clean(value) -> str:
    if value is None:
        return ""
    return str(value).replace("\xa0", " ").strip()


def find_sheet(workbook, prefix: str, required: bool = True):
    """The first sheet whose name starts with `prefix` (any case). Missing: SystemExit when
    required (the command-line script), else None."""
    for name in workbook.sheetnames:
        if name.lower().startswith(prefix.lower()):
            return workbook[name]
    if required:
        raise SystemExit(f"error: no sheet starting with {prefix!r}. Sheets: {workbook.sheetnames}")
    return None


def read_rows(sheet) -> list[dict]:
    """Rows as dicts keyed by the (cleaned) header row; empty rows dropped."""
    rows = sheet.iter_rows(values_only=True)
    try:
        header = [clean(h) for h in next(rows)]
    except StopIteration:
        return []
    out = []
    for row in rows:
        record = {h: clean(v) for h, v in zip(header, row) if h}
        if any(record.values()):
            out.append(record)
    return out


def client_for(row: dict) -> str:
    """The row's client code, from its "client" column or the "#E8D ..." at the start of the task
    name; normalized ("2CS" -> "E2CS")."""
    code = row.get("client") or ""
    if not code:
        match = _TASK_CLIENT.search(row.get("task_name", ""))
        code = match.group(1) if match else ""
    return normalize_client_code(code) or ""


def is_analysis_workbook(workbook) -> bool:
    return any(find_sheet(workbook, prefix, required=False) is not None for prefix in ANALYSIS_SHEETS)

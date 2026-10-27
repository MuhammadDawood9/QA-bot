"""Burned-in captions under a platform's buttons and bars (TikTok, Reels, Shorts): the caption's
box from the OCR against the zones in delivery.safe_area. Caption files (SRT) have no position on
the picture, and captions read before boxes were kept (an older OCR cache) are skipped."""

from __future__ import annotations

from typing import Sequence

from src.models import SAFE_AREA, Caption, Issue


def applies(settings: dict, vertical: bool) -> bool:
    enabled = settings.get("enabled", "auto")
    if str(enabled).lower() == "auto":
        return vertical
    return bool(enabled)


def overlap_share(box: tuple[float, float, float, float], zone: dict) -> float:
    """Share of the box's area inside the zone (fractions of the frame)."""
    left, top, right, bottom = box
    zl, zt = float(zone["left"]), float(zone["top"])
    zr, zb = zl + float(zone["width"]), zt + float(zone["height"])
    w = max(0.0, min(right, zr) - max(left, zl))
    h = max(0.0, min(bottom, zb) - max(top, zt))
    area = max(1e-9, (right - left) * (bottom - top))
    return w * h / area


def safe_area_issues(captions: Sequence[Caption], settings: dict) -> tuple[list[Issue], int]:
    """Issues tied to their captions (so the report shows them with the caption's screenshot),
    and how many OCR captions had no box to check."""
    zones = settings.get("zones") or []
    min_share = float(settings.get("min_overlap", 0.1))
    out, unknown = [], 0
    for cap in captions:
        if cap.source != "ocr":
            continue
        if cap.box is None:
            unknown += 1
            continue
        hits = [(z.get("name", "a platform zone"), overlap_share(cap.box, z)) for z in zones]
        hits = [(name, share) for name, share in hits if share >= min_share]
        if not hits:
            continue
        names = " and ".join(f"the {name} ({share:.0%} of the text)" for name, share in hits)
        out.append(Issue(cap.id, cap.start, SAFE_AREA, "medium", cap.flat_text,
                         f"This caption sits under {names}: the platform's buttons or text will cover it.",
                         "Move the captions inside the safe area (higher up and away from the right edge)."))
    return out, unknown

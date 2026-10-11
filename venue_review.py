"""Proactive admin report for venues whose map pin was guessed from a short name.

The FA fixtures list only gives a short venue name, so a pin for any venue that is not
pinned in VENUE_ADDRESSES is a best guess. The first time a venue is seen, the admin is
emailed the resolved address, a Maps link and the FA page to check it against, so a bad
pin is caught weeks before match day. Each venue is reported once, tracked in a small
JSON file in the data directory.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

from geocoding import clean_venue_name


def venue_review_key(venue_name: str) -> str:
    """One key per ground, ignoring pitch/3G suffixes (Pitch 1D and Pitch 2 are one venue)."""
    return clean_venue_name(venue_name)


def load_reviewed_venues(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def mark_venue_reviewed(path: Path, key: str, resolved: str) -> None:
    path = Path(path)
    data = load_reviewed_venues(path)
    data[key] = {
        "resolved": resolved,
        "reported_at": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _venue_addresses_line(existing: dict[str, str] | None, key: str, address: str) -> str:
    """A complete, paste-ready VENUE_ADDRESSES line: existing pins plus the new one."""
    entries = [f"{name}={addr}" for name, addr in (existing or {}).items()]
    entries.append(f"{key}={address}")
    return "VENUE_ADDRESSES=" + ";".join(entries)


def build_venue_review_email(
    venue_name: str,
    fixture: dict,
    location: dict,
    geocoded: bool,
    existing: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return (subject, body) asking the admin to check a venue's pin.

    ``existing`` is the currently pinned VENUE_ADDRESSES, so the suggested .env line is
    complete and can be pasted over the current one.
    """
    key = venue_review_key(venue_name)
    match = f"{fixture.get('home_team', '')} vs {fixture.get('away_team', '')}"
    lines = [
        f"A fixture was added at a venue whose map pin was guessed from its name: {key}",
        "",
        f"Fixture: {match}",
        f"Date: {fixture.get('date', '')} {fixture.get('time', '')}".rstrip(),
        f"FA venue text: {fixture.get('venue', '')}",
        "",
    ]
    if geocoded:
        address = location.get("addressLine") or location.get("address") or ""
        lines += [
            f"Pin used in Spond: {address}",
            f"Map: https://www.google.com/maps?q={location['latitude']},{location['longitude']}",
        ]
        subject = f"Check venue pin: {key}"
    else:
        lines += [
            "The venue could not be located, so Spond has plain text with no map pin.",
        ]
        subject = f"Venue not found: {key}"

    lines += [""]
    detail_url = (fixture.get("detail_url") or "").strip()
    if detail_url:
        lines += [f"Real address (FA fixture page): {detail_url}", ""]

    lines += [
        "If the pin is WRONG (or missing): copy the address, postcode included, from the",
        "FA fixture page, and replace the <...> in this line in .env:",
        f"  {_venue_addresses_line(existing, key, '<paste the address from the FA fixture page>')}",
        "",
        "If the FA address has no postcode, find the venue on Google Maps (search its name",
        "and town), then add the postcode to the end of the address, e.g.",
        "  ...DAMSON PARKWAY, SOLIHULL, B92 9EJ",
        "A postcode is required: without a postcode the pin is ignored and the app goes",
        "back to guessing from the short name. Check the Maps result is the actual ground,",
        "not a road or a similarly named place nearby.",
    ]
    if geocoded:
        guess = location.get("addressLine") or location.get("address") or ""
        if guess:
            lines += [
                "",
                "If the pin above is correct and you just want to lock it in, use this instead",
                "(only if you have checked it against the FA page):",
                f"  {_venue_addresses_line(existing, key, guess)}",
            ]
    lines += [
        "",
        "Then:",
        "  1. Recreate the container so it reads the new .env: docker compose up -d",
        "     (a restart or rebuild alone does not pick up .env changes).",
        "  2. Fix the location on this fixture's Spond event by hand. Changing .env only",
        "     affects fixtures created afterwards.",
        "You will not be emailed about this venue again.",
    ]
    return subject, "\n".join(lines)


_POSTCODE_RE = re.compile(
    r"(?<![A-Z0-9])([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})(?![A-Z0-9])"
)


def visible_text(html: str) -> str:
    """Page text a person would read: no scripts, styles, comments, tags or attributes.

    Raw HTML is not searched: pages embed base64 fonts/images whose letters and digits
    can look like a postcode.
    """
    html = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1\s*>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    html = re.sub(r"(?s)<[^>]*>", " ", html)
    return " ".join(unescape(html).split())


_BLOCK_TITLES = ("just a moment", "attention required")
_BLOCK_MARKERS = (
    "challenge-platform",  # JS challenge ("Just a moment...")
    "prevented from accessing this page",  # hard block ("Attention Required!")
    "cloudflare ray id",  # footer on both
)


def _is_cloudflare_block(title: str, html: str) -> bool:
    """Cloudflare serves at least two different interstitials; recognise both."""
    lowered_title = title.lower()
    if any(t in lowered_title for t in _BLOCK_TITLES):
        return True
    lowered = html.lower()
    return any(m in lowered for m in _BLOCK_MARKERS)


def classify_detail_page(title: str, html: str) -> tuple[str, str | None]:
    """Classify an FA fixture detail page fetched by the dry-run probe.

    Returns ("challenge", None) if Cloudflare's bot check is showing (the probe then
    stops; it never tries to get past it), ("address", snippet) if a postcode appears in
    the visible text, else ("unknown", None).
    """
    if _is_cloudflare_block(title, html):
        return "challenge", None
    text = visible_text(html)
    match = _POSTCODE_RE.search(text.upper())
    if not match:
        return "unknown", None
    start = max(0, match.start() - 120)
    return "address", text[start : match.end()]

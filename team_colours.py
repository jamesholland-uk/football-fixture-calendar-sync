"""Home/away kit colours for Spond match events.

Parse TEAM_COLOURS from the environment. Names (white, purple) or hex.
Quote hex in .env — unquoted # is a comment. Bare hex without # also works.
Only our kit is sent; opponent colour is omitted.
"""

from __future__ import annotations

import os

NAMED_COLOURS = {
    "white": "#ffffff",
    "black": "#000000",
    "red": "#ff0000",
    "blue": "#0000ff",
    "green": "#008000",
    "yellow": "#ffff00",
    "orange": "#ffa500",
    "pink": "#ffc0cb",
    "navy": "#000080",
    "purple": "#8000ff",  # Spond's purple, not CSS purple
}


def normalise_colour(value: str) -> str | None:
    """Return a #rrggbb colour, or None if the value is not recognised."""
    raw = value.strip().lower()
    if not raw:
        return None
    if raw in NAMED_COLOURS:
        return NAMED_COLOURS[raw]
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) == 3 and all(c in "0123456789abcdef" for c in raw):
        return "#" + "".join(c * 2 for c in raw)
    if len(raw) == 6 and all(c in "0123456789abcdef" for c in raw):
        return f"#{raw}"
    return None


def load_team_colours(raw: str | None = None) -> dict[str, dict[str, str]]:
    """Load home/away kit colours keyed by FA Full-Time team name."""
    if raw is None:
        raw = os.environ.get("TEAM_COLOURS", "")

    colours: dict[str, dict[str, str]] = {}
    if not raw.strip():
        print("TEAM_COLOURS not configured")
        return colours

    print(f"TEAM_COLOURS raw: {raw}")

    for mapping in raw.split(","):
        mapping = mapping.strip()
        if not mapping:
            continue
        parts = mapping.rsplit(":", 2)
        if len(parts) != 3:
            print(f"  Skipping invalid TEAM_COLOURS entry: '{mapping}'")
            continue
        team_name, home_raw, away_raw = (p.strip() for p in parts)
        home = normalise_colour(home_raw)
        away = normalise_colour(away_raw)
        if not team_name or not home or not away:
            print(f"  Skipping invalid TEAM_COLOURS entry: '{mapping}'")
            continue
        colours[team_name] = {"home": home, "away": away}
        print(f"  Team colours: '{team_name}' home {home}, away {away}")

    return colours


def kit_colour_for_fixture(
    fixture: dict,
    colours: dict[str, dict[str, str]],
) -> tuple[str | None, bool | None]:
    """Return (our kit colour, is_home) when a configured team is in the fixture."""
    home_team = fixture.get("home_team", "")
    away_team = fixture.get("away_team", "")
    if home_team in colours:
        return colours[home_team]["home"], True
    if away_team in colours:
        return colours[away_team]["away"], False
    return None, None

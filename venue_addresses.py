"""VENUE_ADDRESSES: pin a venue to its FA address so it is never guessed.

The fixtures list page only gives a short name ("EXAMPLE PARK 3G Pitch 1D"), and
guessing a postcode from that can land on the wrong end of a road. Pinning the full
FA address (copied from the fixture detail page, postcode included) avoids that.

Format: ``VENUE NAME=full address;OTHER VENUE=full address``
Semicolons separate venues (addresses contain commas). Matching ignores case and
pitch/3G suffixes, so ``EXAMPLE PARK`` also covers ``EXAMPLE PARK 3G Pitch 1D``, and
``THE EXAMPLE ARENA EXA1A`` also covers pitch ``EXA2B`` at the same ground.
"""

from __future__ import annotations

from geocoding import venue_match_key


def parse_venue_addresses(raw: str) -> dict[str, str]:
    """Parse the env value into {venue name: address}. Malformed entries are skipped."""
    mapping: dict[str, str] = {}
    for entry in raw.split(";"):
        name, sep, address = entry.partition("=")
        name, address = name.strip(), address.strip()
        if sep and name and address:
            mapping[name] = address
    return mapping


def find_venue_address(venue_name: str, mapping: dict[str, str]) -> str | None:
    """Return the pinned address for a venue, or None. Exact match after cleaning."""
    wanted = venue_match_key(venue_name)
    for name, address in mapping.items():
        if venue_match_key(name) == wanted:
            return address
    return None

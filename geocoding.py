"""Chosen venue geocoding pipeline for Spond map pins.

Inputs: FA venue name + optional opponent/team name (never a street address).
  1. Web-search for a UK postcode (snippet must mention the venue).
  2. postcodes.io baseline for that postcode.
  3. Google Geocoding of the venue name (pitch codes stripped; no team in the query).
  4. If the result is coarse, street-only, or a named stadium, Places Find Place
     (same POI data as the Maps app). Ignore Places if its postcode district
     disagrees with the web-search postcode.
  5. Decision: Places POI if valid; else same full postcode → Google; same
     district, different unit → web postcode; different district → Google.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request


GENERIC_GOOGLE = {
    "birmingham, uk",
    "birmingham, west midlands, uk",
    "uk",
}

USER_AGENT = "FootballFixtureSync/1.0"


def extract_postcode(text: str) -> str | None:
    match = re.search(r"([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})", text.upper())
    return match.group(1) if match else None


def normalize_postcode(postcode: str) -> str:
    return re.sub(r"\s+", "", postcode.upper())


def postcode_district(postcode: str) -> str:
    cleaned = postcode.upper().strip()
    if " " in cleaned:
        return cleaned.split()[0]
    match = re.match(r"^([A-Z]{1,2}\d{1,2}[A-Z]?)", cleaned)
    return match.group(1) if match else cleaned[:3]


def clean_venue_name(venue_name: str) -> str:
    name = venue_name.upper()
    name = re.sub(r"\s*3G\s*", " ", name)
    name = re.sub(r"\s*4G\s*", " ", name)
    name = re.sub(r"\s*PITCH\s*[\dA-Z/]*", " ", name)
    # Stand/pitch codes such as CV2, RED3. CV2 is also a Coventry postcode district
    # and makes Google Geocoding return a vague neighbourhood instead of the stadium.
    name = re.sub(r"\s+[A-Z]{2,4}\d{1,2}$", "", name)
    return " ".join(name.split()).strip()


def clean_team_name(team_name: str) -> str:
    name = re.sub(
        r"\s*(U\d+|Juniors?|FC|AFC|United|City|Town|LM|DP|JH|SS|CF|Girls?|Boys?|Elite|Albion|West|Blue|Pumas)\s*",
        " ",
        team_name,
        flags=re.IGNORECASE,
    )
    return " ".join(name.split()).strip()


def geocode_postcode_io(postcode: str) -> dict | None:
    try:
        clean = postcode.replace(" ", "").upper()
        url = f"https://api.postcodes.io/postcodes/{clean}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") == 200 and data.get("result"):
                r = data["result"]
                return {
                    "lat": r["latitude"],
                    "lon": r["longitude"],
                    "display": f"{r.get('postcode', '')} ({r.get('admin_ward', '')}, {r.get('admin_district', '')})",
                    "postcode": r.get("postcode", postcode),
                    "source": "postcode.io",
                }
    except Exception as e:
        print(f"    postcodes.io error: {e}")
    return None


def is_coarse_google(result: dict | None) -> bool:
    """True if Google only resolved a town/neighbourhood, not a street or POI."""
    if not result:
        return True
    display = result.get("display", "").lower().strip()
    if display in GENERIC_GOOGLE:
        return True
    if not result.get("postcode"):
        return True
    parts = [p.strip() for p in result.get("display", "").split(",") if p.strip()]
    return len(parts) <= 2


def address_mentions_venue(display: str, venue_name: str) -> bool:
    """False for a street/postcode geocode that never names the venue (e.g. Farnborough Rd)."""
    hay = display.lower()
    words = _venue_words(venue_name)
    words |= {
        w for w in clean_venue_name(venue_name).lower().split()
        if w in {"stadium", "school", "academy", "club", "park"}
    }
    return any(len(w) >= 4 and w in hay for w in words)


def geocode_google_places(query: str, api_key: str) -> dict | None:
    """Find Place — same POI data the Google Maps app uses for named stadiums."""
    try:
        encoded = urllib.parse.quote(query)
        url = (
            "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
            f"?input={encoded}&inputtype=textquery"
            "&fields=name,formatted_address,geometry"
            f"&locationbias=circle:20000@52.48,-1.90"
            f"&key={api_key}"
        )
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        status = data.get("status")
        if status == "REQUEST_DENIED":
            print(f"    [Places] API not enabled or key blocked: {data.get('error_message', status)}")
            return None
        if status != "OK" or not data.get("candidates"):
            return None
        c = data["candidates"][0]
        loc = c.get("geometry", {}).get("location", {})
        if "lat" not in loc:
            return None
        formatted = c.get("formatted_address") or c.get("name") or ""
        postcode = extract_postcode(formatted) or ""
        name = c.get("name") or formatted
        return {
            "lat": loc["lat"],
            "lon": loc["lng"],
            "display": f"{name}, {formatted}" if name and name not in formatted else formatted[:80],
            "postcode": postcode,
            "source": "google_places",
        }
    except Exception as e:
        print(f"    [Places] Error: {e}")
    return None


def _google_queries(venue_name: str, postcode: str | None) -> list[str]:
    cleaned = clean_venue_name(venue_name)
    queries = [
        f"{venue_name}, Birmingham, UK",
        f"{cleaned}, Birmingham, UK",
    ]
    if "STADIUM" in cleaned and "FOOTBALL" in cleaned:
        queries.append(f"{cleaned.replace('FOOTBALL', '').strip()}, Birmingham, UK")
    if postcode:
        queries.append(f"{cleaned}, {postcode}, UK")
        queries.append(f"{cleaned} Stadium, {postcode}, UK" if "STADIUM" not in cleaned else f"{cleaned}, {postcode}, UK")
    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        key = re.sub(r"\s+", " ", q).strip().lower()
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique


def geocode_google(query: str, api_key: str) -> dict | None:
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={encoded}&key={api_key}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") == "OK" and data.get("results"):
                r = data["results"][0]
                loc = r["geometry"]["location"]
                postcode = ""
                for comp in r.get("address_components", []):
                    if "postal_code" in comp.get("types", []):
                        postcode = comp["long_name"]
                        break
                if not postcode:
                    found = extract_postcode(r.get("formatted_address", ""))
                    postcode = found or ""
                return {
                    "lat": loc["lat"],
                    "lon": loc["lng"],
                    "display": r.get("formatted_address", "")[:80],
                    "postcode": postcode,
                    "source": "google",
                }
            if data.get("status") == "REQUEST_DENIED":
                print(f"    Google API error: {data.get('error_message', 'Request denied')}")
    except Exception as e:
        print(f"    Google error: {e}")
    return None


def _venue_words(venue_name: str) -> set[str]:
    words = set(clean_venue_name(venue_name).lower().split())
    words |= {clean_venue_name(venue_name).lower().replace(" ", "")}
    words -= {
        "farm", "field", "fields", "ground", "stadium", "pitch", "sports",
        "centre", "center", "club", "academy", "school", "playing",
    }
    return {w for w in words if len(w) >= 3}


def _snippet_matches_venue(snippet: str, venue_name: str) -> bool:
    haystack = snippet.lower().replace(" ", "")
    return any(w in haystack or w in snippet.lower() for w in _venue_words(venue_name))


def search_postcode_web(venue_name: str, team_name: str = "") -> tuple[str | None, str]:
    try:
        from ddgs import DDGS
    except ImportError:
        print("    [WebSearch] SKIPPED: ddgs is not installed")
        return None, "ddgs not installed"

    queries = [f"{clean_venue_name(venue_name)} address Birmingham UK"]
    if team_name:
        queries.append(
            f"{clean_team_name(team_name)} {clean_venue_name(venue_name)} address Birmingham"
        )

    try:
        with DDGS() as ddgs:
            for query in queries:
                print(f"    [WebSearch] {query}")
                results = list(ddgs.text(query, max_results=5))
                for result in results:
                    body = f"{result.get('title', '')} {result.get('body', '')}"
                    postcode = extract_postcode(body)
                    if not postcode:
                        continue
                    if not _snippet_matches_venue(body, venue_name):
                        print(f"    [WebSearch] Ignored {postcode} (snippet does not mention venue)")
                        continue
                    source = "venue" if query.startswith(clean_venue_name(venue_name)) else "team+venue"
                    print(f"    [WebSearch] Postcode from {source} search: {postcode}")
                    return postcode, f"web search ({source}): {postcode}"
    except Exception as e:
        return None, f"web search failed: {e}"

    return None, "web search found no postcode"


def geocode_chosen(
    venue_name: str,
    team_name: str = "",
    google_api_key: str | None = None,
) -> tuple[dict | None, str]:
    """Run the chosen pipeline. Returns (result, decision reason)."""
    if google_api_key is None:
        google_api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "") or None

    postcode, postcode_reason = search_postcode_web(venue_name, team_name)
    if not postcode:
        print(f"    [WebSearch] No independent postcode ({postcode_reason})")
    baseline = geocode_postcode_io(postcode) if postcode else None
    if baseline:
        print(f"    [Baseline] {baseline['display']}")

    google = None
    places = None
    if google_api_key:
        for query in _google_queries(venue_name, postcode):
            print(f"    [Google] {query}")
            candidate = geocode_google(query, google_api_key)
            if not candidate:
                continue
            if is_coarse_google(candidate):
                print(f"    [Google] Coarse, skipped: {candidate.get('display')}")
                continue
            google = candidate
            print(f"    [Google] {google['display']} [{google.get('postcode') or 'no postcode'}]")
            break

        street_only = bool(google) and not address_mentions_venue(google.get("display", ""), venue_name)
        named_ground = "STADIUM" in clean_venue_name(venue_name)
        if is_coarse_google(google) or street_only or named_ground:
            if street_only and google:
                print(f"    [Google] Street/postcode pin, not a named venue: {google.get('display')}")
            places_q = f"{clean_venue_name(venue_name)}, Birmingham, UK"
            if postcode:
                places_q = f"{clean_venue_name(venue_name)}, {postcode}, UK"
            print(f"    [Places] {places_q}")
            places = geocode_google_places(places_q, google_api_key)
            if places:
                print(f"    [Places] {places['display']} [{places.get('postcode') or 'no postcode'}]")
                google = places

    if google and google.get("source") == "google_places":
        if baseline and google.get("postcode"):
            g_dist = postcode_district(google["postcode"])
            b_dist = postcode_district(baseline["postcode"])
            if g_dist != b_dist:
                print(f"    [Places] Ignored (district {g_dist} vs web {b_dist})")
                google = None
            else:
                return google, f"Google Places POI ({postcode_reason})"
        else:
            return google, "Google Places POI (named venue)"

    if google and google.get("postcode"):
        if baseline:
            g_full = normalize_postcode(google["postcode"])
            b_full = normalize_postcode(baseline["postcode"])
            g_dist = postcode_district(google["postcode"])
            b_dist = postcode_district(baseline["postcode"])
            if g_full == b_full:
                street_only = not address_mentions_venue(google.get("display", ""), venue_name)
                if street_only and "STADIUM" in clean_venue_name(venue_name):
                    return baseline, (
                        f"web postcode {baseline['postcode']}; Google was a street pin "
                        f"({google['display']}) on a stadium venue"
                    )
                return google, f"Google (same full postcode {google['postcode']} as {postcode_reason})"
            if g_dist == b_dist:
                return baseline, (
                    f"web postcode {baseline['postcode']}; Google was nearby "
                    f"{google['postcode']} (same district {g_dist}, different unit)"
                )
            return google, f"Google ({g_dist}); ignored web postcode {b_dist} (different district)"
        return google, "Google (has postcode; no web postcode to compare)"

    if baseline:
        if google:
            return baseline, f"postcode baseline; Google had no postcode to verify ({postcode_reason})"
        return baseline, f"postcode baseline only ({postcode_reason})"

    if google:
        return google, "Google only (no postcode discovered)"

    return None, "no result"


def to_spond_location(result: dict, venue_name: str) -> dict:
    """Build a Spond location object from a chosen geocode result."""
    postcode = result.get("postcode") or ""
    display = result.get("display") or venue_name
    address_short = display.split(",")[0].strip()
    return {
        "latitude": float(result["lat"]),
        "longitude": float(result["lon"]),
        "feature": venue_name,
        "featureName": venue_name,
        "address": address_short,
        "addressLine": display,
        "postalCode": postcode,
        "country": "GB",
    }


def geocode_venue(venue_name: str, opponent_team: str = "") -> dict | None:
    """Geocode a fixture venue for Spond. Returns a Spond location dict or None."""
    result, reason = geocode_chosen(venue_name, opponent_team)
    if not result:
        print(f"    [Geocode] Failed: {reason}")
        return None
    loc = to_spond_location(result, venue_name)
    print(f"    [Geocode] {reason}")
    print(f"    [Geocode] {loc['addressLine']} ({loc.get('postalCode', '')})")
    return loc

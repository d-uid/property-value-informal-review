"""Comparable-sales retrieval via Redfin's public gis-csv endpoint.

We search by a polygon around the subject coordinate (the region-search and
autocomplete endpoints are IP-blocked from datacenters; the polygon CSV export
is not). Results are filtered to sales on/before the county cutoff date and
ranked by similarity to the subject.
"""
from __future__ import annotations

import asyncio
import csv
import io
import math
import re
import statistics
from datetime import date, datetime
from typing import Optional

import httpx

from . import config
from .models import Comp, SubjectInfo
from .parcel import format_apn

# Which Redfin property-type ids (uipt) to fetch for a target structural
# category. "townhouse" pulls both condos and townhouses because a
# townhouse-STYLE condo (own entrance, no stacked neighbor) is filed by Redfin
# as Condo/Co-op; we separate those from stacked flats structurally below.
_UIPT_FOR_CATEGORY = {
    "sfr": "1",
    "townhouse": "2,3",
    "stacked": "2",
    None: "1,2,3",
}

# Map the user-facing property_type to a structural category.
_CATEGORY_FOR_TYPE = {
    "single_family": "sfr",
    "townhouse": "townhouse",
    "condo": "stacked",
    "any": None,
}

_LABEL_FOR_CATEGORY = {
    "sfr": "Single-family home",
    "townhouse": "Townhouse / townhouse-style",
    "stacked": "Condo (stacked flat)",
    None: "Any",
}


def _unit_is_stacked(address: str) -> bool:
    """True if the unit designator looks like an upper-floor flat.

    Stacked buildings number units by floor (#202, #303, #2304); townhouse-style
    homes have no unit, a letter unit (Unit D), or a low number (#2, #4).
    """
    m = re.search(r"(?:#|\bunit\b|\bapt\b|\bste\b)\s*([A-Za-z]?\d*[A-Za-z]?)",
                  address or "", re.I)
    if not m or not m.group(1):
        return False
    digits = re.sub(r"\D", "", m.group(1))
    return bool(digits) and int(digits) >= 100


def structural_category(redfin_type: Optional[str], address: str) -> str:
    """Classify a listing as 'sfr' | 'townhouse' | 'stacked'."""
    t = (redfin_type or "").lower()
    if "single family" in t or "single-family" in t:
        return "sfr"
    if "townhouse" in t:
        return "townhouse"
    if "condo" in t or "co-op" in t or "coop" in t:
        return "stacked" if _unit_is_stacked(address) else "townhouse"
    return "stacked" if _unit_is_stacked(address) else "townhouse"


def _category_from_word(word: Optional[str], address: str = "") -> str:
    """Structural category from a Redfin meta-description type word."""
    w = (word or "").lower()
    if "single" in w or "house" in w and "town" not in w:
        return "sfr"
    if "town" in w:
        return "townhouse"
    if "condo" in w or "co-op" in w:
        return "stacked" if _unit_is_stacked(address) else "townhouse"
    return "townhouse"


def _type_from_category(category: Optional[str]) -> str:
    for k, v in _CATEGORY_FOR_TYPE.items():
        if v == category:
            return k
    return "any"


class CompsBlocked(Exception):
    """Raised when Redfin refuses the request (e.g. 403 from a datacenter IP)."""


def _haversine_miles(lat1, lng1, lat2, lng2) -> float:
    r = 3958.7613  # miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _bbox_poly(lat: float, lng: float, radius_km: float) -> str:
    """A square polygon of half-width radius_km centered on (lat,lng)."""
    d_lat = radius_km / 110.574
    d_lng = radius_km / (111.320 * math.cos(math.radians(lat)))
    pts = [
        (lng - d_lng, lat - d_lat), (lng + d_lng, lat - d_lat),
        (lng + d_lng, lat + d_lat), (lng - d_lng, lat + d_lat),
        (lng - d_lng, lat - d_lat),
    ]
    return ",".join(f"{x} {y}" for x, y in pts)


def _parse_sold_date(raw: str) -> Optional[date]:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%B-%d-%Y", "%b-%d-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _to_int(raw: str) -> Optional[int]:
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _to_float(raw: str) -> Optional[float]:
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


async def _fetch_csv(client: httpx.AsyncClient, lat: float, lng: float,
                     radius_km: float, uipt: str,
                     lookback_days: Optional[int] = None) -> str:
    params = {
        "al": "1",
        "num_homes": "350",
        "ord": "redfin-recommended-asc",
        "page_number": "1",
        "sf": "1,2,3,5,6,7",
        "status": "9",  # sold
        "uipt": uipt,
        "v": "8",
        "poly": _bbox_poly(lat, lng, radius_km),
        "sold_within_days": str(lookback_days or config.COMPS_LOOKBACK_DAYS),
    }
    r = await client.get(
        "https://www.redfin.com/stingray/api/gis-csv",
        params=params,
        headers={"User-Agent": config.USER_AGENT, "Accept": "text/csv,*/*"},
    )
    if r.status_code == 403:
        raise CompsBlocked("Redfin returned 403 (blocked)")
    r.raise_for_status()
    return r.text


def _rows_to_comps(csv_text: str, subject_lat: float, subject_lng: float,
                   cutoff: Optional[date]) -> list[Comp]:
    reader = csv.DictReader(io.StringIO(csv_text))
    comps: list[Comp] = []
    for row in reader:
        sold = _parse_sold_date(row.get("SOLD DATE", ""))
        price = _to_int(row.get("PRICE", ""))
        if not sold or not price:
            continue
        if cutoff is not None and sold > cutoff:
            continue  # statutorily cannot be considered
        lat = _to_float(row.get("LATITUDE", ""))
        lng = _to_float(row.get("LONGITUDE", ""))
        dist = (
            round(_haversine_miles(subject_lat, subject_lng, lat, lng), 2)
            if lat is not None and lng is not None else None
        )
        street = (row.get("ADDRESS") or "").strip()
        comps.append(Comp(
            address=street,
            city=(row.get("CITY") or "").strip() or None,
            zip=(row.get("ZIP OR POSTAL CODE") or "").strip() or None,
            property_type=(row.get("PROPERTY TYPE") or "").strip() or None,
            sold_date=sold.isoformat(),
            sold_date_display=f"{sold.month}/{sold.day}/{sold.year}",
            price=price,
            beds=_to_float(row.get("BEDS", "")),
            baths=_to_float(row.get("BATHS", "")),
            sqft=_to_int(row.get("SQUARE FEET", "")),
            distance_miles=dist,
            lat=lat, lng=lng,
            url=(row.get(
                "URL (SEE https://www.redfin.com/buy-a-home/"
                "comparative-market-analysis FOR INFO ON PRICING)"
            ) or "").strip() or None,
        ))
    return comps


def _score(comp: Comp, cutoff: date, subject_sqft: Optional[float],
           subject_beds: Optional[float]) -> float:
    # Distance: closer is better (0..1 over the max radius).
    dist = comp.distance_miles if comp.distance_miles is not None else config.COMPS_MAX_RADIUS_KM
    dist_score = max(0.0, 1.0 - (dist / (config.COMPS_MAX_RADIUS_KM * 0.621371)))

    # Recency: more recent (but on/before cutoff) is better, over the lookback.
    recency_score = 0.5
    if comp.sold_date:
        sold = date.fromisoformat(comp.sold_date)
        age = (cutoff - sold).days
        recency_score = max(0.0, 1.0 - age / config.COMPS_LOOKBACK_DAYS)

    # Size similarity, if we know the subject's sqft. Penalty is steep so a
    # near-identical size clearly beats a merely-close-by different size.
    have_size = bool(subject_sqft and comp.sqft)
    size_score = 0.5
    if have_size:
        size_score = max(0.0, 1.0 - 1.6 * abs(comp.sqft - subject_sqft) / subject_sqft)

    beds_bonus = 0.0
    if subject_beds and comp.beds:
        beds_bonus = 0.08 if abs(comp.beds - subject_beds) < 0.5 else 0.0

    # When the caller tells us the subject's size, similarity in size matters
    # more than raw proximity (adjacent new construction can be much larger).
    if have_size:
        w_dist, w_rec, w_size = 0.25, 0.20, 0.55
    else:
        w_dist, w_rec, w_size = 0.55, 0.25, 0.20
    return w_dist * dist_score + w_rec * recency_score + w_size * size_score + beds_bonus


async def find_comps(
    client: httpx.AsyncClient,
    lat: float,
    lng: float,
    property_type: str,
    subject_address: Optional[str] = None,
    subject_sqft: Optional[float] = None,
    subject_beds: Optional[float] = None,
    count: int = config.COMPS_TARGET_COUNT,
) -> list[Comp]:
    """Return comparables ranked best-first. Caller takes the first `count` as
    the working set and may offer the remainder as swappable alternates.

    Comps are filtered to the subject's STRUCTURAL category so a townhouse-style
    home is never compared against stacked flats (units with an upstairs/
    downstairs neighbor), and vice-versa.
    """
    cutoff = config.COMPS_CUTOFF_DATE
    category = _CATEGORY_FOR_TYPE.get(property_type, None)
    uipt = _UIPT_FOR_CATEGORY.get(category, _UIPT_FOR_CATEGORY[None])

    # Gather the full candidate pool at the max search radius, then let scoring
    # decide. A near-perfect size match a bit farther out should beat a poor
    # match next door, so we must fetch the wider set before ranking rather than
    # stopping at the first few that fall inside a small box.
    radius = config.COMPS_MAX_RADIUS_KM
    csv_text = await _fetch_csv(client, lat, lng, radius, uipt)
    comps = _rows_to_comps(csv_text, lat, lng, cutoff)

    # Structural filter: keep only comps in the subject's category.
    if category is not None:
        comps = [c for c in comps
                 if structural_category(c.property_type, c.address) == category]

    if subject_address:
        subj = subject_address.upper()
        comps = [c for c in comps if c.address.upper() not in subj
                 and subj[:12] not in c.address.upper()]

    band = (subject_sqft * config.COMPS_SIZE_BAND_FRACTION) if subject_sqft else None

    def in_band(c: Comp) -> bool:
        return (band is not None and c.sqft is not None
                and abs(c.sqft - subject_sqft) <= band)

    # Flag suspected below-market-rate / outlier sales: price-per-sq-ft far below
    # the local median. Compare within the size band when known (same-size homes
    # are the fair reference), else across the whole same-type pool.
    for c in comps:
        if c.price and c.sqft:
            c.ppsf = round(c.price / c.sqft)
    ref = [c.ppsf for c in comps if c.ppsf and (band is None or in_band(c))]
    if len(ref) >= 3:
        median_ppsf = statistics.median(ref)
        floor = median_ppsf * (1 - config.COMPS_OUTLIER_PPSF_DROP)
        for c in comps:
            if c.ppsf and c.ppsf < floor:
                pct = round((1 - c.ppsf / median_ppsf) * 100)
                c.outlier = True
                c.outlier_reason = (
                    f"${c.ppsf}/sq ft is {pct}% below the ~${round(median_ppsf)}/sq ft "
                    "local median — possible below-market (BMR) sale.")

    # Rank in tiers so the default picks match the subject on the things
    # assessors care about, in priority order:
    #   1. not a suspected BMR/outlier,
    #   2. same bedroom count (a 3-bed is not comparable to a 4-bed),
    #   3. within the size band,
    #   4. overall similarity score.
    # Lower tiers still fill remaining slots and appear as alternates.
    def _beds_match(c: Comp) -> bool:
        return (subject_beds is not None and c.beds is not None
                and abs(c.beds - subject_beds) <= 0.5)

    def _sort_key(c: Comp):
        key = [1 if c.outlier else 0]
        if subject_beds is not None:
            key.append(0 if _beds_match(c) else 1)
        if subject_sqft:
            key.append(0 if in_band(c) else 1)
        key.append(-_score(c, cutoff, subject_sqft, subject_beds))
        return tuple(key)

    comps.sort(key=_sort_key)
    return comps


# --- Subject-property attribute detection -------------------------------------

_META_RE = re.compile(
    r"([\d.]+)\s*beds?,\s*([\d.]+)\s*baths?,\s*([\d,]+)\s*sq\.?\s*ft\.?\s*"
    r"([A-Za-z /-]+?)\s+located at\s+(.+?),",
    re.I,
)
_APN_RE = re.compile(r"APN\s+([0-9A-Za-z\-]{6,12})", re.I)
_LATLNG_RE = re.compile(r'"latitude":([0-9.\-]+),"longitude":([0-9.\-]+)')


_REDFIN_URL_RE = re.compile(r'redfin\.com(/[A-Za-z0-9/_%.\-]*?/home/[0-9]+)', re.I)

# Search engines tried in order to find a property's Redfin page. Redfin's own
# autocomplete API is IP-blocked, and any single engine rate-limits, so we fall
# through several. Each must return parseable HTML containing the result URLs.
_SEARCH_ENGINES = [
    ("startpage", "https://www.startpage.com/sp/search"),
    ("duckduckgo", "https://lite.duckduckgo.com/lite/"),
    ("brave", "https://search.brave.com/search"),
    ("mojeek", "https://www.mojeek.com/search"),
]


def _pick_redfin_url(html: str, house: Optional[str], street_name: Optional[str],
                     zip_code: Optional[str] = None) -> Optional[str]:
    """Pick the first Redfin property URL whose slug matches the subject's house
    number, street, and (if known) ZIP — so we never grab a neighbor's listing
    (search engines happily return sibling units for a house-number query)."""
    tok = (street_name or "").split()[0].lower() if street_name else ""
    zc = (zip_code or "").split("-")[0].strip()
    for m in _REDFIN_URL_RE.finditer(html):
        slug = m.group(1).lower()
        # House number must appear as its own dashed token (335 != 3350/1335).
        if house and f"/{house.lower()}-" not in slug:
            continue
        if tok and tok not in slug:
            continue
        if zc and zc not in slug:
            continue
        return "https://www.redfin.com" + m.group(1)
    return None


async def discover_redfin_url(
    client: httpx.AsyncClient, address: str,
    house: Optional[str], street_name: Optional[str],
    zip_code: Optional[str] = None,
) -> Optional[str]:
    """Find the subject's Redfin page URL via a chain of search engines."""
    query = f"{address} redfin"
    headers = {"User-Agent": config.USER_AGENT,
               "Accept-Language": "en-US,en;q=0.9"}
    for _name, url in _SEARCH_ENGINES:
        try:
            r = await client.get(url, params={"q": query}, headers=headers,
                                  timeout=12.0)
            if r.status_code != 200:
                continue
            found = _pick_redfin_url(r.text, house, street_name, zip_code)
            if found:
                return found
        except httpx.HTTPError:
            continue
    return None


# Full browser header set — Redfin's CDN 202-challenges bare requests. Retrying
# on the same client lets any clearance cookie from the challenge carry over.
_BROWSER_HEADERS = {
    "User-Agent": config.USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Referer": "https://www.redfin.com/",
}


async def _fetch_redfin_page(client: httpx.AsyncClient, url: str,
                             attempts: int = 4) -> Optional[str]:
    """GET a Redfin page, retrying past the CDN bot-challenge (202/403/429).

    Retries reuse the same client so any clearance cookie set by the challenge
    response carries into the next attempt.
    """
    for i in range(attempts):
        try:
            r = await client.get(url, headers=_BROWSER_HEADERS, timeout=20.0)
        except httpx.HTTPError:
            return None
        if r.status_code == 200 and r.text:
            return r.text
        if r.status_code not in (202, 403, 429):
            return None
        if i < attempts - 1:
            await asyncio.sleep(1.2 * (i + 1))
    return None


async def fetch_redfin_subject(client: httpx.AsyncClient, url: str
                               ) -> Optional[SubjectInfo]:
    """Parse the subject's beds/baths/sqft/type/APN from its Redfin page.

    Uses the property PAGE HTML (which serves 200) rather than the detail API
    (which is 403-blocked). Returns None if the page can't be read/parsed.
    """
    url = url.strip()
    if not re.match(r"https?://(www\.)?redfin\.com/", url, re.I):
        return None
    html = await _fetch_redfin_page(client, url)
    if not html:
        return None

    md = re.search(r'name="description"\s+content="([^"]{0,220})', html)
    meta = md.group(1) if md else ""
    m = _META_RE.search(meta)
    if not m:
        return None
    beds = _to_float(m.group(1))
    baths = _to_float(m.group(2))
    sqft = _to_int(m.group(3))
    type_word = m.group(4).strip()
    addr = m.group(5).strip()
    category = _category_from_word(type_word, addr)
    apn_m = _APN_RE.search(html)
    apn = format_apn(apn_m.group(1)) if apn_m else None
    yb = re.search(r'"yearBuilt":([0-9]{4})', html)

    # Current assessed value = taxable land + improvement (latest roll year).
    # Quotes in the embedded JSON are backslash-escaped (\"), so tolerate an
    # optional backslash before each quote.
    assessed = roll_year = None
    q = r'\\?"'
    tm = re.search(
        rf'taxInfo{q}:\s*\{{\s*{q}taxableLandValue{q}:\s*([0-9]+),\s*'
        rf'{q}taxableImprovementValue{q}:\s*([0-9]+),\s*{q}rollYear{q}:\s*([0-9]{{4}})',
        html)
    if tm:
        assessed = int(tm.group(1)) + int(tm.group(2))
        roll_year = int(tm.group(3))

    return SubjectInfo(
        property_type=_type_from_category(category),
        property_type_label=_LABEL_FOR_CATEGORY[category],
        beds=beds, baths=baths, sqft=sqft,
        year_built=int(yb.group(1)) if yb else None,
        apn=apn, assessed_value=assessed, roll_year=roll_year,
        redfin_url=url,
        source="redfin_url",
        source_note=f"From Redfin listing ({type_word.lower()}, {addr}).",
    )


async def detect_subject(
    client: httpx.AsyncClient, lat: float, lng: float,
    house: Optional[str], street_name: Optional[str], raw_address: str,
) -> Optional[SubjectInfo]:
    """Estimate subject attributes from the nearest matching sold record.

    Tries an exact house+street match first; otherwise the closest same-street
    sale that matches the subject's unit/no-unit structure. Used when no Redfin
    URL is supplied and the subject isn't otherwise known.
    """
    try:
        csv_text = await _fetch_csv(client, lat, lng, 0.6, "1,2,3",
                                    lookback_days=3650)
    except (CompsBlocked, httpx.HTTPError):
        return None
    rows = _rows_to_comps(csv_text, lat, lng, cutoff=None)
    if not rows:
        return None

    hn = (house or "").strip()
    sn = (street_name or "").strip().upper()
    subj_stacked = _unit_is_stacked(raw_address)

    def street_ok(c: Comp) -> bool:
        return bool(sn) and sn in (c.address or "").upper()

    exact = [c for c in rows
             if hn and (c.address or "").upper().startswith(hn + " ") and street_ok(c)]
    same_street = [c for c in rows if street_ok(c)]

    def to_subject(c: Comp, source: str, note: str) -> SubjectInfo:
        cat = structural_category(c.property_type, c.address)
        return SubjectInfo(
            property_type=_type_from_category(cat),
            property_type_label=_LABEL_FOR_CATEGORY[cat],
            beds=c.beds, baths=c.baths, sqft=c.sqft,
            source=source, source_note=note,
        )

    if exact:
        exact.sort(key=lambda c: c.sold_date or "", reverse=True)
        c = exact[0]
        return to_subject(c, "redfin_match",
                          f"From this property's Redfin sale record ({c.address}).")

    if same_street:
        same_street.sort(key=lambda c: (
            0 if _unit_is_stacked(c.address) == subj_stacked else 1,
            c.distance_miles if c.distance_miles is not None else 9e9,
        ))
        c = same_street[0]
        return to_subject(
            c, "estimated_street",
            f"Estimated from nearest same-street sale ({c.address}, "
            f"{c.distance_miles} mi). Verify or edit.")
    return None


def average_value(comps: list[Comp]) -> Optional[int]:
    prices = [c.price for c in comps if c.price]
    if not prices:
        return None
    return round(sum(prices) / len(prices))

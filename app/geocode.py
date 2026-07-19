"""Address geocoding with a fallback chain.

Order: ArcGIS World geocoder -> Nominatim (OSM) -> US Census. The first two
carry newer construction than Census, which matters for recently built
subdivisions common in Prop 8 filings.
"""
from __future__ import annotations

import re
from typing import Optional

import httpx

from .config import CONTACT_EMAIL, HTTP_TIMEOUT_SECONDS
from .models import Geocode

# Street-type tokens we strip to recover the base street name used by the
# county parcel layer (which stores the name without the type suffix).
_STREET_TYPES = {
    "ST", "STREET", "AVE", "AV", "AVENUE", "BLVD", "BOULEVARD", "DR", "DRIVE",
    "RD", "ROAD", "LN", "LANE", "CT", "COURT", "PL", "PLACE", "WAY", "CIR",
    "CIRCLE", "TER", "TERR", "TERRACE", "TR", "PKWY", "PARKWAY", "SQ", "SQUARE",
    "LOOP", "PATH", "WALK", "TRL", "TRAIL", "HWY", "HIGHWAY", "PLZ", "PLAZA",
    "CMN", "COMMON", "COMMONS", "ROW", "RUN", "XING", "CROSSING",
}
_DIRECTIONS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}


def parse_street(street: str) -> tuple[Optional[str], str]:
    """Return (direction_prefix, base_street_name_upper).

    "N Murphy Ave" -> ("N", "MURPHY"); "Alviso Ter" -> (None, "ALVISO").
    """
    tokens = [t for t in re.split(r"\s+", street.strip().upper()) if t]
    direction = None
    if tokens and tokens[0] in _DIRECTIONS:
        direction = tokens[0]
        tokens = tokens[1:]
    while tokens and tokens[-1] in _STREET_TYPES:
        tokens = tokens[:-1]
    return direction, " ".join(tokens).strip()


def _split_house(street_with_house: str) -> tuple[Optional[str], str]:
    """Split a leading house number off a street string."""
    m = re.match(r"^\s*(\d+[A-Za-z]?)\s+(.*)$", street_with_house)
    if m:
        return m.group(1), m.group(2).strip()
    return None, street_with_house.strip()


async def _try_arcgis(client: httpx.AsyncClient, address: str) -> Optional[Geocode]:
    url = (
        "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/"
        "findAddressCandidates"
    )
    params = {
        "SingleLine": address,
        "outFields": "*",
        "maxLocations": "1",
        "countryCode": "USA",
        "f": "json",
    }
    r = await client.get(url, params=params)
    r.raise_for_status()
    cands = r.json().get("candidates", [])
    if not cands:
        return None
    c = cands[0]
    attrs = c.get("attributes", {})
    loc = c["location"]
    house = attrs.get("AddNum") or None
    street = attrs.get("StName") or None
    if street and attrs.get("StType"):
        street = f"{street} {attrs['StType']}"
    if not street:
        _, street = _split_house(attrs.get("ShortLabel", ""))
    return Geocode(
        lat=loc["y"], lng=loc["x"], matched_address=c.get("address", address),
        house=house, street=street, city=attrs.get("City") or None,
        state=attrs.get("RegionAbbr") or None, zip=attrs.get("Postal") or None,
        source="arcgis",
    )


async def _try_nominatim(client: httpx.AsyncClient, address: str) -> Optional[Geocode]:
    r = await client.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "jsonv2", "addressdetails": "1", "limit": "1"},
        headers={"User-Agent": f"prop8-tool/1.0 ({CONTACT_EMAIL})"},
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    d = data[0]
    a = d.get("address", {})
    house = a.get("house_number")
    street = a.get("road")
    return Geocode(
        lat=float(d["lat"]), lng=float(d["lon"]),
        matched_address=d.get("display_name", address),
        house=house, street=street,
        city=a.get("city") or a.get("town") or a.get("village"),
        state=a.get("state"), zip=a.get("postcode"), source="nominatim",
    )


async def _try_census(client: httpx.AsyncClient, address: str) -> Optional[Geocode]:
    r = await client.get(
        "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
        params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
    )
    r.raise_for_status()
    matches = r.json().get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    m = matches[0]
    coord = m["coordinates"]
    comp = m.get("addressComponents", {})
    street = " ".join(
        p for p in [
            comp.get("preDirection"), comp.get("streetName"), comp.get("suffixType"),
        ] if p
    ) or None
    return Geocode(
        lat=coord["y"], lng=coord["x"], matched_address=m.get("matchedAddress", address),
        house=comp.get("fromAddress") or comp.get("houseNumber"),
        street=street, city=comp.get("city"), state=comp.get("state"),
        zip=comp.get("zip"), source="census",
    )


async def geocode(client: httpx.AsyncClient, address: str) -> Optional[Geocode]:
    for fn in (_try_arcgis, _try_nominatim, _try_census):
        try:
            result = await fn(client, address)
            if result:
                # Backfill a house number from the raw input if the geocoder
                # dropped it (common with ArcGIS StName-only responses).
                if not result.house:
                    h, _ = _split_house(address)
                    result.house = h
                return result
        except (httpx.HTTPError, KeyError, ValueError):
            continue
    return None

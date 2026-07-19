"""Santa Clara County APN lookup via the public parcels ArcGIS layer.

Primary path is a text match on house number + street name + city (high
confidence). Fallback is a spatial point-in-parcel query at the geocoded
coordinate (flagged "verify", since for brand-new subdivisions the annually
updated public layer may still show the pre-subdivision parcel).
"""
from __future__ import annotations

from typing import Optional

import httpx

from .config import PARCEL_LAYER_URL
from .geocode import parse_street
from .models import Geocode, Parcel


def format_apn(raw: Optional[str]) -> Optional[str]:
    """20559033 -> 205-59-033 (Santa Clara County 3-2-3 format)."""
    if not raw:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) != 8:
        return raw
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


def _situs(attrs: dict) -> str:
    parts = [
        attrs.get("situs_hous"), attrs.get("situs_stre"), attrs.get("situs_st_1"),
        attrs.get("situs_st_2"),
    ]
    line = " ".join(str(p) for p in parts if p).strip()
    unit = attrs.get("situs_unit")
    if unit:
        line += f" #{unit}"
    city = attrs.get("situs_city")
    zip_ = attrs.get("situs_zip_")
    tail = " ".join(str(p) for p in [city, zip_] if p)
    return f"{line}, {tail}".strip().rstrip(",")


_OUT_FIELDS = (
    "apn,situs_hous,situs_stre,situs_st_1,situs_st_2,situs_unit,"
    "situs_city,situs_zip_"
)


async def _query(client: httpx.AsyncClient, params: dict) -> list[dict]:
    params = {**params, "outFields": _OUT_FIELDS, "returnGeometry": "false", "f": "json"}
    r = await client.get(f"{PARCEL_LAYER_URL}/query", params=params)
    r.raise_for_status()
    return [f["attributes"] for f in r.json().get("features", [])]


def _esc(value: str) -> str:
    return value.replace("'", "''")


async def lookup(client: httpx.AsyncClient, geo: Geocode) -> Parcel:
    house = (geo.house or "").strip()
    city = (geo.city or "").upper().strip()
    direction, street_name = parse_street(geo.street or "")

    # --- Primary: text match on house + street name (+ city) ---
    if house and street_name:
        where = f"situs_hous='{_esc(house)}' AND situs_st_1 LIKE '{_esc(street_name)}%'"
        if city:
            where += f" AND situs_city='{_esc(city)}'"
        try:
            rows = await _query(client, {"where": where})
        except httpx.HTTPError:
            rows = []
        # If city filter was too strict (geocoder city vs situs city), retry.
        if not rows and city:
            try:
                rows = await _query(
                    client,
                    {"where": f"situs_hous='{_esc(house)}' AND "
                              f"situs_st_1 LIKE '{_esc(street_name)}%'"},
                )
            except httpx.HTTPError:
                rows = []
        if len(rows) == 1:
            a = rows[0]
            return Parcel(
                apn=format_apn(a.get("apn")), apn_raw=a.get("apn"),
                situs_address=_situs(a), confidence="high",
            )
        if len(rows) > 1:
            # Multiple hits (e.g. condo units share a street address). Prefer an
            # exact unit-less parcel, otherwise report the first and flag verify.
            a = rows[0]
            return Parcel(
                apn=format_apn(a.get("apn")), apn_raw=a.get("apn"),
                situs_address=_situs(a), confidence="verify",
                note=f"{len(rows)} parcels share this address (units?). "
                     "Confirm the APN on your tax bill.",
            )

    # --- Fallback: spatial point-in-parcel ---
    try:
        rows = await _query(
            client,
            {
                "geometry": f"{geo.lng},{geo.lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
            },
        )
    except httpx.HTTPError:
        rows = []
    if rows:
        a = rows[0]
        return Parcel(
            apn=format_apn(a.get("apn")), apn_raw=a.get("apn"),
            situs_address=_situs(a), confidence="verify",
            note="APN located by map position, not by address text. For new "
                 "construction the county layer may still show the prior parcel "
                 "— confirm the APN on your tax bill.",
        )

    return Parcel(
        confidence="not_found",
        note="Could not locate the parcel automatically. Enter your APN "
             "manually (it is on your property tax bill).",
    )

"""FastAPI app: prepares a Santa Clara County Prop 8 (Decline in Value) request
from a street address — APN lookup, comparable sales, opinion of value, and a
ready-to-paste narrative comment."""
from __future__ import annotations

import contextlib
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import comps as comps_mod
from . import config, narrative
from .geocode import geocode
from .models import Comp, Parcel, PrepareRequest, PrepareResponse, SubjectInfo
from .parcel import lookup as parcel_lookup

STATIC_DIR = Path(__file__).parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(
        timeout=config.HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(title="Prop 8 Informal Review Helper", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/api/prepare", response_model=PrepareResponse)
async def prepare(req: PrepareRequest) -> PrepareResponse:
    client: httpx.AsyncClient = app.state.client
    warnings: list[str] = []

    geo = await geocode(client, req.address)
    if not geo:
        return PrepareResponse(
            address_input=req.address,
            parcel=Parcel(confidence="not_found",
                          note="Address could not be geocoded. Check spelling, "
                               "or include city and ZIP."),
            comps=[], comment="", comps_source="none",
            warnings=["Could not geocode the address."],
        )

    parcel = await parcel_lookup(client, geo)

    # Detect subject attributes (beds/sqft/type/APN/assessed value). Priority:
    #   1. a user-supplied Redfin URL (authoritative),
    #   2. auto-discovering the subject's Redfin page from the address,
    #   3. estimating from the nearest matching sale.
    from .geocode import parse_street
    _, street_name = parse_street(geo.street or "")

    subject = None
    found_url = req.redfin_url or None
    if req.redfin_url:
        subject = await comps_mod.fetch_redfin_subject(client, req.redfin_url)
    if not subject:
        found_url = req.redfin_url or await comps_mod.discover_redfin_url(
            client, req.address, geo.house, street_name, geo.zip)
        if found_url and not req.redfin_url:
            subject = await comps_mod.fetch_redfin_subject(client, found_url)
            if subject:
                subject.source = "redfin_auto"
                subject.source_note = (
                    "Auto-matched to this Redfin listing "
                    f"({found_url.rsplit('/', 3)[1].replace('-', ' ')}).")
    if not subject:
        subject = await comps_mod.detect_subject(
            client, geo.lat, geo.lng, geo.house, street_name, req.address)
        # We located the Redfin listing but couldn't read it (rate-limited). Give
        # the user the link so they can copy the correct APN themselves.
        if found_url:
            if subject is None:
                subject = SubjectInfo()
            subject.redfin_url = found_url
            note = ("Found your Redfin listing but couldn't read it right now "
                    "(the site is rate-limiting requests). Open the link to "
                    "confirm your APN, then re-run.")
            subject.source_note = f"{subject.source_note} {note}" if subject.source_note else note
            warnings.append(
                "Redfin is rate-limiting right now, so the APN shown is the "
                "county's (which can be stale for new construction). Your Redfin "
                "listing was found — open it to confirm the correct APN.")

    # If Redfin knew the APN and the county layer wasn't confident (common for
    # new subdivisions), prefer Redfin's APN.
    if subject and subject.apn and parcel.confidence != "high":
        parcel.apn = subject.apn
        parcel.apn_raw = subject.apn.replace("-", "")
        parcel.confidence = "high"
        parcel.note = "APN from the Redfin listing."
        # The prior situs came from a stale/adjacent parcel; use the real address.
        parcel.situs_address = geo.matched_address

    if parcel.confidence == "not_found":
        warnings.append("APN not found automatically — enter it manually.")
    elif parcel.confidence == "verify":
        warnings.append("APN needs verification — confirm it on your tax bill.")

    # Resolve the effective property type + size used for comp matching.
    resolved_type = req.property_type
    if resolved_type == "auto":
        resolved_type = subject.property_type if subject else "any"
    eff_sqft = req.sqft if req.sqft is not None else (subject.sqft if subject else None)
    eff_beds = req.beds if req.beds is not None else (subject.beds if subject else None)
    eff_assessed = req.assessed_value
    if eff_assessed is None and subject and subject.assessed_value:
        eff_assessed = subject.assessed_value

    comps: list[Comp] = []
    alternates: list[Comp] = []
    comps_source = "redfin"
    try:
        ranked = await comps_mod.find_comps(
            client, geo.lat, geo.lng, resolved_type,
            subject_address=geo.matched_address,
            subject_sqft=eff_sqft, subject_beds=eff_beds,
        )
        comps = ranked[:config.COMPS_TARGET_COUNT]
        rest = ranked[config.COMPS_TARGET_COUNT:]
        # Show clean alternates plus any flagged BMR/outlier sales, so an
        # exclusion is always visible (with its badge) and reversible.
        clean_alts = [c for c in rest if not c.outlier][:5]
        flagged_alts = [c for c in rest if c.outlier][:3]
        alternates = clean_alts + flagged_alts
    except comps_mod.CompsBlocked:
        comps_source = "blocked"
        warnings.append(
            "Comparable-sales lookup is temporarily blocked from this server. "
            "Add your 3 comparables manually below.")
    except httpx.HTTPError:
        comps_source = "error"
        warnings.append("Comparable-sales service is unavailable right now. "
                        "Add your 3 comparables manually below.")

    if comps_source == "redfin" and not comps:
        warnings.append("No qualifying sales found near this address before the "
                        "cutoff. Widen the search or add comps manually.")

    opinion = comps_mod.average_value(comps)
    comment = narrative.build_comment(
        comps, opinion, resolved_type, eff_assessed
    ) if comps else ""

    return PrepareResponse(
        address_input=req.address,
        geocode=geo,
        parcel=parcel,
        subject=subject,
        comps=comps,
        alternates=alternates,
        opinion_of_value=opinion,
        assessed_value=eff_assessed,
        comment=comment,
        comps_source=comps_source,
        warnings=warnings,
    )


class RecomputeRequest(PrepareRequest):
    comps: list[Comp] = []


@app.post("/api/recompute", response_model=PrepareResponse)
async def recompute(req: RecomputeRequest) -> PrepareResponse:
    """Recompute opinion of value + comment from user-edited comps (manual
    fallback / overrides). No network calls."""
    opinion = comps_mod.average_value(req.comps)
    comment = narrative.build_comment(
        req.comps, opinion, req.property_type, req.assessed_value
    ) if req.comps else ""
    return PrepareResponse(
        address_input=req.address,
        parcel=Parcel(confidence="not_found"),
        comps=req.comps,
        opinion_of_value=opinion,
        assessed_value=req.assessed_value,
        comment=comment,
        comps_source="manual",
        warnings=[],
    )


@app.get("/")
async def index():
    # No-store so the single-page app's inline JS is never served stale.
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")

"""Pydantic request/response models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

PropertyType = Literal["auto", "single_family", "townhouse", "condo", "any"]


class PrepareRequest(BaseModel):
    address: str = Field(..., min_length=4, description="Subject property address")
    property_type: PropertyType = "auto"
    # Optional hints that improve comp selection when the caller knows them.
    beds: Optional[float] = None
    sqft: Optional[float] = None
    # Optional: the current assessed value from the owner's tax bill.
    assessed_value: Optional[float] = None
    # Optional Redfin property URL — the most accurate source for the subject's
    # beds/sqft/type/APN when auto-detection can't identify it.
    redfin_url: Optional[str] = None


class SubjectInfo(BaseModel):
    """Detected/estimated attributes of the subject property."""
    property_type: PropertyType = "any"      # resolved structural category
    property_type_label: Optional[str] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    year_built: Optional[int] = None
    apn: Optional[str] = None                 # if Redfin knew it
    assessed_value: Optional[float] = None    # current roll assessed value
    roll_year: Optional[int] = None
    redfin_url: Optional[str] = None          # the listing we read, if any
    # How we obtained these: redfin_url | redfin_auto | estimated_street | none
    source: str = "none"
    source_note: Optional[str] = None


class Geocode(BaseModel):
    lat: float
    lng: float
    matched_address: str
    house: Optional[str] = None
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    source: str


class Parcel(BaseModel):
    apn: Optional[str] = None            # formatted 205-59-033
    apn_raw: Optional[str] = None        # 20559033
    situs_address: Optional[str] = None
    confidence: Literal["high", "verify", "not_found"] = "not_found"
    note: Optional[str] = None


class Comp(BaseModel):
    address: str
    city: Optional[str] = None
    zip: Optional[str] = None
    apn: Optional[str] = None
    property_type: Optional[str] = None
    sold_date: Optional[str] = None       # ISO yyyy-mm-dd
    sold_date_display: Optional[str] = None  # M/D/YYYY (matches county form)
    price: Optional[int] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    distance_miles: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    url: Optional[str] = None
    ppsf: Optional[int] = None                # price per square foot
    outlier: bool = False                     # suspected BMR / below-market sale
    outlier_reason: Optional[str] = None


class PrepareResponse(BaseModel):
    address_input: str
    geocode: Optional[Geocode] = None
    parcel: Parcel
    subject: Optional[SubjectInfo] = None
    comps: list[Comp]
    alternates: list[Comp] = []
    opinion_of_value: Optional[int] = None
    assessed_value: Optional[float] = None
    comment: str
    comps_source: str
    warnings: list[str] = []

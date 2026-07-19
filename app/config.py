"""Runtime configuration, overridable via environment variables."""
import os
from datetime import date


def _env_date(name: str, default: str) -> date:
    raw = os.getenv(name, default)
    y, m, d = (int(x) for x in raw.split("-"))
    return date(y, m, d)


# Sales comparables transacting AFTER this date cannot be considered by the
# assessor for the 2026-2027 Prop 8 review (per the county form).
COMPS_CUTOFF_DATE: date = _env_date("COMPS_CUTOFF_DATE", "2026-03-31")

# The lien date the opinion of value is anchored to.
LIEN_DATE_YEAR: int = int(os.getenv("LIEN_DATE_YEAR", "2026"))

# Tax roll year label used on the county form ("2026-2027").
TAX_YEAR_LABEL: str = os.getenv(
    "TAX_YEAR_LABEL", f"{LIEN_DATE_YEAR}-{LIEN_DATE_YEAR + 1}"
)

# Santa Clara County parcels ArcGIS feature layer (public).
PARCEL_LAYER_URL: str = os.getenv(
    "PARCEL_LAYER_URL",
    "https://services8.arcgis.com/fpjs8A5Vtkshblnd/arcgis/rest/services/"
    "Santa_Clara_County_Parcels/FeatureServer/0",
)

# Radius (km) of the comparable-sales search box around the subject.
COMPS_MAX_RADIUS_KM: float = float(os.getenv("COMPS_MAX_RADIUS_KM", "2.5"))

# Number of comparables to return.
COMPS_TARGET_COUNT: int = int(os.getenv("COMPS_TARGET_COUNT", "3"))

# Comps whose living area is within this fraction of the subject's are
# prioritized; those outside the band only fill in when too few are in-band.
# 0.10 => ±10% (e.g. 1,240–1,516 sq ft for a 1,378 sq ft subject).
COMPS_SIZE_BAND_FRACTION: float = float(os.getenv("COMPS_SIZE_BAND_FRACTION", "0.10"))

# A comp whose price-per-sq-ft is more than this far below the local median is
# flagged as a suspected below-market-rate (BMR)/outlier sale and demoted out
# of the default selection. 0.18 => flag anything >18% below median $/sq ft.
COMPS_OUTLIER_PPSF_DROP: float = float(os.getenv("COMPS_OUTLIER_PPSF_DROP", "0.18"))

# Only consider sales within this many months before the cutoff.
COMPS_LOOKBACK_DAYS: int = int(os.getenv("COMPS_LOOKBACK_DAYS", "365"))

HTTP_TIMEOUT_SECONDS: float = float(os.getenv("HTTP_TIMEOUT_SECONDS", "25"))

USER_AGENT: str = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)

CONTACT_EMAIL: str = os.getenv("CONTACT_EMAIL", "prop8-tool@example.com")

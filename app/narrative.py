"""Generate the 'Other Information' narrative comment for the county form."""
from __future__ import annotations

from datetime import date
from typing import Optional

from . import config
from .models import Comp


def _fmt_money(v: Optional[float]) -> str:
    return f"${v:,.0f}" if v is not None else "$0"


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{int(m)}/{int(d)}/{y}"


_TYPE_PHRASE = {
    "townhouse": "similar attached townhomes",
    "condo": "similar condominiums",
    "single_family": "similar single-family homes",
    "any": "similar properties",
    "auto": "similar properties",
}


def build_comment(
    comps: list[Comp],
    opinion_of_value: Optional[int],
    property_type: str,
    assessed_value: Optional[float] = None,
) -> str:
    type_phrase = _TYPE_PHRASE.get(property_type, _TYPE_PHRASE["any"])
    lien = date(config.LIEN_DATE_YEAR, 1, 1)
    lien_str = lien.strftime("%B 1, %Y").replace(" 0", " ")

    dates = sorted(c.sold_date for c in comps if c.sold_date)
    if dates:
        span = (
            f"between {_fmt_date(dates[0])} and {_fmt_date(dates[-1])}"
            if dates[0] != dates[-1] else f"on {_fmt_date(dates[0])}"
        )
    else:
        span = "near the lien date"

    dists = [c.distance_miles for c in comps if c.distance_miles is not None]
    within = f", all within {max(dists):.1f} miles of the subject," if dists else ""

    lines = [
        f"This Proposition 8 informal review request is submitted due to a "
        f"localized market decline affecting {type_phrase} in the immediate "
        f"area. As of the {lien_str} lien date, the market value of the subject "
        f"property has fallen below its current assessed value."
    ]

    body = (
        f"The {len(comps)} comparable sales below—all transacted {span}"
        f"{within} and all closest in location and characteristics to the "
        f"subject—support an opinion of value of "
        f"{_fmt_money(opinion_of_value)}, the average of their sale prices:"
    )
    lines.append(body)

    for i, c in enumerate(comps, 1):
        bits = [c.address]
        if c.city:
            bits.append(c.city)
        detail = ", ".join(bits)
        extras = []
        if c.beds:
            extras.append(f"{c.beds:g} bd")
        if c.sqft:
            extras.append(f"{c.sqft:,} sf")
        if c.distance_miles is not None:
            extras.append(f"{c.distance_miles:.2f} mi away")
        extra_str = f" ({'; '.join(extras)})" if extras else ""
        lines.append(
            f"  {i}. {detail} — sold {_fmt_date(c.sold_date)} for "
            f"{_fmt_money(c.price)}{extra_str}."
        )

    if assessed_value and opinion_of_value and assessed_value > opinion_of_value:
        pct = (assessed_value - opinion_of_value) / assessed_value * 100
        lines.append(
            f"The current assessed value of {_fmt_money(assessed_value)} exceeds "
            f"the supported market value by {_fmt_money(assessed_value - opinion_of_value)} "
            f"({pct:.0f}%)."
        )

    lines.append(
        f"Accordingly, I respectfully request that the assessed value be reduced "
        f"to {_fmt_money(opinion_of_value)} for the {config.TAX_YEAR_LABEL} tax year. "
        f"None of the comparable sales transacted after "
        f"{config.COMPS_CUTOFF_DATE.strftime('%B %d, %Y').replace(' 0', ' ')}."
    )

    return "\n\n".join(lines)

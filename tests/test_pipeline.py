"""Offline unit tests for the pure logic (no network)."""
from datetime import date

from app.comps import (Comp, _bbox_poly, _parse_sold_date, _pick_redfin_url,
                       _rows_to_comps, _unit_is_stacked, average_value,  # noqa: F401
                       find_comps, structural_category)
from app.narrative import build_comment
from app.parcel import format_apn
from app.geocode import parse_street


def test_format_apn():
    assert format_apn("20559033") == "205-59-033"
    assert format_apn("20452015") == "204-52-015"
    assert format_apn("161-33-015") == "161-33-015"  # already dashed digits
    assert format_apn(None) is None


def test_parse_street():
    assert parse_street("Alviso Ter") == (None, "ALVISO")
    assert parse_street("N Murphy Ave") == ("N", "MURPHY")
    assert parse_street("Chula Vista Terrace") == (None, "CHULA VISTA")
    assert parse_street("W California Ave") == ("W", "CALIFORNIA")


def test_average_matches_form_example():
    comps = [
        Comp(address="987 Asilomar Ter 2", price=1200000),
        Comp(address="1036 Chula Vista Ter", price=1258000),
        Comp(address="930 Highland Ter", price=1260000),
    ]
    # Matches the county PDF's Opinion of Value exactly.
    assert average_value(comps) == 1239333


def test_cutoff_excludes_late_sales():
    csv_text = (
        "SOLD DATE,PRICE,LATITUDE,LONGITUDE,ADDRESS,CITY,PROPERTY TYPE,BEDS,BATHS,"
        "SQUARE FEET,ZIP OR POSTAL CODE\n"
        "March-2-2026,1258000,37.386,-122.045,1036 Chula Vista Ter,Sunnyvale,Townhouse,2,2,1293,94086\n"
        "April-6-2026,1056000,37.384,-122.042,975 Belmont Ter,Sunnyvale,Townhouse,2,2,1329,94086\n"
    )
    comps = _rows_to_comps(csv_text, 37.389, -122.036, date(2026, 3, 31))
    assert len(comps) == 1
    assert comps[0].price == 1258000


def test_pick_redfin_url_matches_subject():
    # Sibling units for the same street appear first — must pick the exact house.
    html = (
        'redfin.com/CA/Sunnyvale/323-Alviso-Ter-94085/home/176497966 '
        'redfin.com/CA/Sunnyvale/331-Alviso-Ter-94085/home/176497962 '
        'redfin.com/CA/Sunnyvale/335-Alviso-Ter-94085/home/176497960'
    )
    assert (_pick_redfin_url(html, "335", "ALVISO", "94085")
            == "https://www.redfin.com/CA/Sunnyvale/335-Alviso-Ter-94085/home/176497960")
    assert (_pick_redfin_url(html, "331", "ALVISO")
            == "https://www.redfin.com/CA/Sunnyvale/331-Alviso-Ter-94085/home/176497962")
    # House number must match as a whole dashed token, not a substring.
    assert _pick_redfin_url(html, "35", "ALVISO") is None
    assert _pick_redfin_url(html, "999", "ALVISO") is None
    # ZIP mismatch is rejected.
    assert _pick_redfin_url(html, "335", "ALVISO", "95050") is None


def test_parse_sold_date():
    assert _parse_sold_date("March-2-2026") == date(2026, 3, 2)
    assert _parse_sold_date("") is None


def test_build_comment_mentions_value_and_cutoff():
    comps = [
        Comp(address="1036 Chula Vista Ter", city="Sunnyvale", price=1258000,
             sold_date="2026-03-02", sold_date_display="3/2/2026", distance_miles=0.3),
    ]
    text = build_comment(comps, 1258000, "townhouse", assessed_value=1400000)
    assert "$1,258,000" in text
    assert "Proposition 8" in text
    assert "March 31, 2026" in text
    assert "$1,400,000" in text  # assessed value comparison


def test_stacked_flat_detection():
    # Floor-numbered condo units are stacked; townhouses / low or letter units are not.
    assert _unit_is_stacked("604 Arcadia Ter #202") is True
    assert _unit_is_stacked("326 Alviso Ter #2304") is True
    assert _unit_is_stacked("975 Belmont Ter #2") is False
    assert _unit_is_stacked("998 La Mesa Ter Unit D") is False
    assert _unit_is_stacked("1036 Chula Vista Ter") is False


def test_structural_category():
    # A townhouse-style condo (no floor unit) groups with townhouses, not flats.
    assert structural_category("Townhouse", "1036 Chula Vista Ter") == "townhouse"
    assert structural_category("Condo/Co-op", "604 Arcadia Ter #202") == "stacked"
    assert structural_category("Condo/Co-op", "331 Alviso Ter") == "townhouse"
    assert structural_category("Single Family Residential", "648 Madrone Ave") == "sfr"


def test_find_comps_filters_stacked_for_townhouse(monkeypatch):
    import asyncio
    import app.comps as m
    csv_text = (
        "SOLD DATE,PRICE,LATITUDE,LONGITUDE,ADDRESS,CITY,PROPERTY TYPE,BEDS,BATHS,"
        "SQUARE FEET,ZIP OR POSTAL CODE\n"
        "March-2-2026,1258000,37.386,-122.045,1036 Chula Vista Ter,Sunnyvale,Townhouse,2,2,1293,94086\n"
        "March-26-2026,800000,37.392,-122.043,604 Arcadia Ter #202,Sunnyvale,Condo/Co-op,2,2,1160,94085\n"
    )

    async def fake_fetch(*a, **k):
        return csv_text
    monkeypatch.setattr(m, "_fetch_csv", fake_fetch)
    comps = asyncio.run(m.find_comps(None, 37.394, -122.027, "townhouse"))
    addrs = [c.address for c in comps]
    assert "1036 Chula Vista Ter" in addrs
    assert "604 Arcadia Ter #202" not in addrs  # stacked flat excluded


def test_size_band_demotes_oversized_comp(monkeypatch):
    import asyncio
    import app.comps as m
    # Subject 1378 sqft. Milan (1543, +12%) is closer/pricier; two in-band comps
    # exist -> Milan must be demoted below both despite being nearer.
    csv_text = (
        "SOLD DATE,PRICE,LATITUDE,LONGITUDE,ADDRESS,CITY,PROPERTY TYPE,BEDS,BATHS,"
        "SQUARE FEET,ZIP OR POSTAL CODE\n"
        "February-25-2026,1457400,37.395,-122.028,1145 Milan Ter #9,Sunnyvale,Townhouse,2,2,1543,94085\n"
        "March-2-2026,1258000,37.386,-122.045,1036 Chula Vista Ter,Sunnyvale,Townhouse,2,2,1293,94086\n"
        "February-26-2026,1260000,37.384,-122.043,968 Belmont Ter #1,Sunnyvale,Townhouse,3,2,1363,94086\n"
    )

    async def fake_fetch(*a, **k):
        return csv_text
    monkeypatch.setattr(m, "_fetch_csv", fake_fetch)
    comps = asyncio.run(m.find_comps(
        None, 37.394, -122.027, "townhouse", subject_sqft=1378))
    top3 = [c.address for c in comps[:3]]
    assert "1036 Chula Vista Ter" in top3
    assert "968 Belmont Ter #1" in top3
    assert comps[-1].address == "1145 Milan Ter #9"  # oversized -> last


def test_beds_count_tier(monkeypatch):
    import asyncio
    import app.comps as m
    # Subject is a 3-bed. A same-size 4-bed must rank below the 3-bed comps.
    csv_text = (
        "SOLD DATE,PRICE,LATITUDE,LONGITUDE,ADDRESS,CITY,PROPERTY TYPE,BEDS,BATHS,"
        "SQUARE FEET,ZIP OR POSTAL CODE\n"
        "March-5-2026,1700000,37.394,-122.027,323 Harcot Ter,Sunnyvale,Townhouse,4,3,1820,94085\n"
        "March-13-2026,1600000,37.395,-122.026,815 Santa Cecilia Ter,Sunnyvale,Townhouse,3,3,1718,94085\n"
        "December-24-2025,1500000,37.390,-122.040,319 Charles Morris Ter,Sunnyvale,Townhouse,3,3,1932,94085\n"
    )

    async def fake_fetch(*a, **k):
        return csv_text
    monkeypatch.setattr(m, "_fetch_csv", fake_fetch)
    comps = asyncio.run(m.find_comps(
        None, 37.394, -122.027, "townhouse", subject_sqft=1869, subject_beds=3))
    top2 = [c.address for c in comps[:2]]
    assert "815 Santa Cecilia Ter" in top2
    assert "319 Charles Morris Ter" in top2
    assert comps[-1].address == "323 Harcot Ter"  # the 4-bed is demoted


def test_bmr_outlier_flagged_and_demoted(monkeypatch):
    import asyncio
    import app.comps as m
    # Four same-size townhouses ~$930-970/sqft plus one ~$700/sqft BMR unit.
    csv_text = (
        "SOLD DATE,PRICE,LATITUDE,LONGITUDE,ADDRESS,CITY,PROPERTY TYPE,BEDS,BATHS,"
        "SQUARE FEET,ZIP OR POSTAL CODE\n"
        "March-2-2026,1258000,37.386,-122.045,1036 Chula Vista Ter,Sunnyvale,Townhouse,2,2,1293,94086\n"
        "February-26-2026,1260000,37.384,-122.043,968 Belmont Ter #1,Sunnyvale,Townhouse,3,2,1363,94086\n"
        "January-15-2026,1260000,37.385,-122.041,930 Highland Ter,Sunnyvale,Townhouse,2,2,1322,94085\n"
        "March-8-2026,1300000,37.390,-122.040,400 Riland Ter,Sunnyvale,Townhouse,3,2,1350,94085\n"
        "October-10-2025,915000,37.383,-122.042,968 Belmont Ter #7,Sunnyvale,Townhouse,2,2,1329,94086\n"
    )

    async def fake_fetch(*a, **k):
        return csv_text
    monkeypatch.setattr(m, "_fetch_csv", fake_fetch)
    comps = asyncio.run(m.find_comps(
        None, 37.394, -122.027, "townhouse", subject_sqft=1378))
    by_addr = {c.address: c for c in comps}
    bmr = by_addr["968 Belmont Ter #7"]
    assert bmr.outlier is True and bmr.ppsf == 688
    assert bmr.address not in [c.address for c in comps[:3]]  # demoted
    assert all(not c.outlier for c in comps[:3])  # top 3 are clean

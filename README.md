# Prop 8 Reduction Request Helper (Santa Clara County)

**Live:** https://prop8-helper-7f167f16.azurewebsites.net
(Azure Web App for Containers, West US 3.)


A web app that prepares an **Informal Proposition 8 (Decline-in-Value) Reduction
Request** from a street address. Given an address it:

1. **Geocodes** the address (ArcGIS → Nominatim → US Census fallback chain).
2. Looks up the **Assessor's Parcel Number (APN)** from the Santa Clara County
   public parcels layer (text match, with a spatial point-in-parcel fallback).
3. Finds **comparable sales** near the property that sold **on or before the
   county cutoff (March 31, 2026)** — the statutory limit for the 2026–2027 roll.
4. Suggests an **Opinion of Value** = the average of the 3 comparable sale prices
   (the same rule the county form uses).
5. Generates a ready-to-paste **narrative comment** for the form's
   "Other Information" box.
6. Shows everything on one page with copy buttons, editable comps, and
   swappable alternates.

The app **prepares** a request for the owner to review and submit on the county
portal — it does not file anything on anyone's behalf, and is not legal or tax
advice.

## How it works (data sources)

| Step | Source | Notes |
|------|--------|-------|
| Geocode | ArcGIS World Geocoder, Nominatim (OSM), US Census | First hit wins; the first two carry newer construction. |
| APN | [Santa Clara County Parcels FeatureServer](https://services8.arcgis.com/fpjs8A5Vtkshblnd/arcgis/rest/services/Santa_Clara_County_Parcels/FeatureServer/0) | Public. Text match = high confidence; spatial fallback = "verify". The layer is refreshed ~annually, so **brand-new subdivisions may still show the prior parcel** — flagged for the user. |
| Comparables | Redfin `gis-csv` polygon export | Sold listings with price/date/lat/long/sqft/type. Filtered to ≤ cutoff, to the subject's structural category, and ranked by size/distance/recency. |
| Subject beds/sqft/type/APN | Redfin property **page HTML** (optional URL) or nearest same-street sale | The detail API is 403-blocked, but the page HTML serves 200 and its `<meta description>` carries "N beds, N baths, N sq. ft. TYPE … APN ########". |

### Subject detection (beds / sq ft / type / APN)

The subject's size and structural type drive comp quality, so the app fills them
automatically:

1. **Redfin URL** (optional field) — most accurate. The page HTML gives beds,
   baths, sq ft, structural type, and the **APN** (which often beats the county
   layer for new subdivisions — e.g. it returns the correct `204-52-015` where
   the county layer still shows the pre-subdivision parcel).
2. **Auto-estimate** — with no URL, the closest same-street sale of matching
   structure (unit vs no-unit) seeds the subject's beds/sqft/type, clearly
   labeled "estimated — verify."

All detected values land in editable fields; change them and click **Re-find**.

### Structural type (townhouse-style vs stacked flat)

Comparability is *structural*, not legal. A "townhouse-style condo" (own
entrance, no upstairs/downstairs neighbor) is legally a condo but must be
compared against townhouses, not stacked flats. The app classifies each listing
as **sfr / townhouse / stacked** using Redfin's type plus the unit-number
pattern (`#202`, `#2304` = upper-floor stacked; no unit / letter / low number =
townhouse-style) and only compares like with like.

### Comp selection

Comps are ranked in tiers so the default 3 are defensible:

1. **Not a suspected BMR/outlier** — a sale whose price-per-sq-ft is >18% below
   the local median (configurable) is flagged as a possible below-market-rate
   sale, demoted out of the default 3, and shown in the alternates with a
   **⚠ BMR?** badge and the reason.
2. **Same bedroom count** — a 3-bed subject is matched to 3-bed comps; a 4-bed
   sale of the same size is demoted (bedroom count is a strong comparability
   signal for assessors).
3. **Within the size band** — comps within ±10% of the subject's living area
   (configurable) are preferred, so a closer-but-much-bigger sale never outranks
   a similarly sized one.
4. **Similarity score** — distance, recency, and size closeness.

The top 3 are selected; the rest (plus any flagged sales) are offered as
**alternates you can swap in**, because comp selection is ultimately a judgment
call.

### Assessed value

Auto-filled from the Redfin listing's tax record (`taxableLandValue +
taxableImprovementValue`) when a Redfin URL is supplied; otherwise enter it from
your tax bill. It drives the "assessed vs. supported market value" comparison
and is referenced in the generated comment.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

Run the tests:

```bash
pip install pytest
python -m pytest tests/ -q
```

## Run with Docker

```bash
docker build -t prop8-helper .
docker run -p 8000:8000 prop8-helper
# open http://localhost:8000
```

## Deploy to Azure (Web App for Containers)

```bash
# 0) Prereqs: az CLI logged in (az login), a resource group.
RG=prop8-rg
LOC=westus2
ACR=prop8acr$RANDOM          # must be globally unique, lowercase
APP=prop8-helper-$RANDOM
PLAN=prop8-plan

az group create -n $RG -l $LOC

# 1) Build the image in Azure Container Registry (no local Docker needed).
az acr create -n $ACR -g $RG --sku Basic --admin-enabled true
az acr build -r $ACR -t prop8-helper:latest .

# 2) App Service plan (Linux) + web app from the image.
az appservice plan create -n $PLAN -g $RG --is-linux --sku B1
az webapp create -n $APP -g $RG -p $PLAN \
  --deployment-container-image-name $ACR.azurecr.io/prop8-helper:latest

# 3) Wire ACR creds + tell Azure which port the container listens on.
CRED=$(az acr credential show -n $ACR)
az webapp config container set -n $APP -g $RG \
  --container-image-name $ACR.azurecr.io/prop8-helper:latest \
  --container-registry-url https://$ACR.azurecr.io \
  --container-registry-user $(echo $CRED | jq -r .username) \
  --container-registry-password $(echo $CRED | jq -r .passwords[0].value)
az webapp config appsettings set -n $APP -g $RG --settings WEBSITES_PORT=8000

echo "https://$APP.azurewebsites.net"
```

The container reads `$PORT` (Azure sets it) and defaults to 8000 locally.
Health check: `GET /healthz`.

> **Observed behaviour from the Azure datacenter IP:** geocoding, the county
> APN lookup, the Redfin **comparable-sales** feed (`gis-csv`), and the search
> engine used to *find* a property's Redfin page all work. Redfin's **property
> page** fetch (used to read the exact APN + assessed value for brand-new
> construction the county layer lacks) is rate-limited from datacenter IPs, so
> that step degrades gracefully: the app shows the county APN with a "verify"
> flag, surfaces the found Redfin listing link, and still returns comps + an
> opinion of value. To make the page fetch reliable in production, route it
> through a residential/rotating proxy (custom `httpx` transport) or use a
> licensed data API.

### Redeploying after a code change

```bash
az acr build -r <acr-name> -t prop8-helper:latest .
az webapp restart -n <app-name> -g prop8-rg
```

## Configuration (environment variables)

| Var | Default | Meaning |
|-----|---------|---------|
| `COMPS_CUTOFF_DATE` | `2026-03-31` | Sales after this are excluded (statutory). |
| `LIEN_DATE_YEAR` | `2026` | Lien date year referenced in the comment. |
| `TAX_YEAR_LABEL` | `2026-2027` | Roll year label. |
| `COMPS_MAX_RADIUS_KM` | `2.5` | Search radius around the subject. |
| `COMPS_LOOKBACK_DAYS` | `365` | How far back sold comps may be. |
| `COMPS_TARGET_COUNT` | `3` | Number of comps selected. |
| `PARCEL_LAYER_URL` | SCC parcels layer | Override to point at a different county layer. |

## Project layout

```
app/
  main.py       FastAPI app + routes (/api/prepare, /api/recompute)
  config.py     env-overridable settings
  models.py     pydantic request/response models
  geocode.py    geocoding chain + street parsing
  parcel.py     APN lookup (text + spatial) via county GIS
  comps.py      Redfin comps retrieval + ranking
  narrative.py  county-style comment generator
  static/index.html   single-page UI
tests/          offline unit tests
Dockerfile
```

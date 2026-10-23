# Weather-Aware Truck Routing

A React + Django app that recommends the safest route for a truck trip by
sampling the hourly weather forecast along each candidate route, classifying
risk at every checkpoint, and ranking routes on a documented, deterministic
rule set — not just shortest time.

Built for the Spotter Full Stack Developer take-home assessment
(`fs-assessment-spotter.pdf`). The boundary conventions, aggregation policy,
and ranking key this README documents below are the fully resolved spec.

## What it does

1. You enter an origin, destination, departure date/time, load weight, and a
   checkpoint sampling interval (10 / 25 / 50 miles). Departure is capped to
   the next 7 days and load weight to 80,000 lb — both validated on the
   backend (`api/serializers.py`) and mirrored in the form for instant
   feedback, not just a cosmetic `max` attribute.
2. The backend requests up to 3 route alternatives (native OSRM
   alternatives, then a bounded via-point fallback search if that falls
   short — see below), walks each polyline to place checkpoints, computes
   each checkpoint's ETA from the provider's per-segment duration data (not
   distance ÷ average speed), and fetches the hourly forecast at every
   checkpoint in one batched weather call.
3. Each checkpoint is classified Low → No Travel against the brief's
   wind/rain/snow thresholds plus load-weight escalation rules.
4. Routes are ranked lexicographically — fewest No-Travel miles, then fewest
   Severe miles, then fewest High miles, then lowest average risk, then
   shortest duration — and the app explains *why* the winner was picked.
5. The map shows all routes (the selected one always rendered on top,
   regardless of overlap, via a dedicated halo layer), checkpoints sized by
   severity, and a 0–48h weather corridor across the trip; a slider scrubs
   through forecast hours entirely client-side (one payload up front, zero
   additional network requests). See **Map & weather visualization** below.

## Architecture

```
┌─────────────────┐        ┌──────────────────────────────────────────┐
│   React (Vite)   │  POST  │              Django + DRF                 │
│                   │ /api/  │                                            │
│  TripForm         │ routes/│  AnalyzeRoutesView (thin orchestrator)    │
│  MapView (MapLibre│ analyze│    │                                      │
│   + GL heatmap)   │  ───►  │    ├─ integrations/routing_client.py ─► OSRM + Nominatim
│  ForecastSlider   │        │    ├─ routing/checkpoints.py  (pure)     │
│  RouteSummaryPanel│  ◄───  │    ├─ routing/eta.py          (pure)     │
│  TanStack Query   │  JSON  │    ├─ integrations/weather_client.py ─► Open-Meteo
│  Zustand (UI-only │        │    ├─ routing/risk.py         (pure)     │
│   state)          │        │    ├─ routing/recommend.py    (pure)     │
│                   │        │    └─ routing/heatmap.py      (pure)     │
└─────────────────┘        └──────────────────────────────────────────┘
```

All business logic (checkpoint placement, ETA interpolation, risk
classification, route ranking, heatmap grid generation) lives in pure,
typed, unit-tested Python modules under `backend/routing/` with no I/O.
Views only orchestrate: validate → call integrations → call pure logic →
serialize. External providers are isolated behind small client classes in
`backend/integrations/` with explicit timeouts and typed errors.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Django 5 + Django REST Framework | Stateless API, no models/DB needed (see `config/settings.py`) |
| API docs | drf-spectacular | OpenAPI schema + Swagger UI at `/api/docs/` |
| Routing | [OSRM](https://project-osrm.org/) public demo server | No key, no distance cap, returns genuine route alternatives in one call |
| Geocoding | [Nominatim](https://nominatim.org/) | No key; paired with OSRM since neither needs an account |
| Weather | [Open-Meteo](https://open-meteo.com/) | No key, hourly wind/rain/snow in mph/inches directly, batched multi-coordinate requests |
| Basemap | OpenFreeMap tiles + MapLibre GL JS | No key; GPU-rendered GL layers (lines, circles, heatmap) instead of per-point React components, for map performance at scale |
| Frontend state | TanStack Query (server state) + Zustand (`selectedRouteId`, `forecastHour` UI-only state) | Analysis result stays in one cache as the single source of truth |

**Known capability trade-off:** OSRM's public demo has no truck/HGV
routing profile, so routes may use roads a legally loaded truck couldn't.
`load_weight_lb` is still collected and used for the risk/load rules — it
just doesn't currently constrain the routing provider's road selection.
(OpenRouteService was tried first for its `driving-hgv` profile but caps
alternative-route requests at 100km total distance, unworkable for
interstate trips — confirmed live before switching to OSRM.)

## Map & weather visualization

Four toggleable corridor layers — **Risk, Wind, Rain, Snow** — plus a
route-risk overlay, all reusing the one heatmap payload the `analyze`
response already returns (no extra request per layer, no extra request on
slider move):

- **Corridor layers render as a blended density cloud**, not discrete dots —
  MapLibre's native `heatmap` layer type, weighted per point by that
  layer's real threshold (`routing/risk.py`'s own numbers, not invented
  ones), so a cluster of severe readings genuinely glows brighter than a
  calm one. A legend under the layer toggle shows the live color scale with
  real units (mph / in·hr⁻¹), not just a hover tooltip.
- **Route-risk ribbon**: when the Risk layer is active, each route gets a
  thin line colored by checkpoint risk, drawn *alongside* the route (via
  MapLibre's `line-offset`) rather than replacing it — the main route line's
  color, width, and click-to-select behavior are never touched by this.
- **Selected route always reads clearly**, even where its path is
  superposed on another route: a dedicated highlight layer (white halo +
  colored line) is added after every other route layer, so it's guaranteed
  on top regardless of GeoJSON feature order — the thing that actually
  determines on-screen z-order for overlapping lines in one source.
- **Truck position markers** interpolate along `duration_breakpoints` at the
  slider's forecast hour and rotate to the route's true compass heading
  (top-down icon, not a side-view glyph that can only flip) — the same
  "rotate the vehicle glyph to heading" convention turn-by-turn nav apps
  use. A small corner badge shows waiting-to-depart / arrived; en route is
  the default, unbadged look.
- **Route-diversity search**: when OSRM's native `alternatives=true` returns
  fewer than 3 routes, one bounded round of via-point fallback requests
  tries to recover more (`integrations/routing_client.py`) — capped at 2
  concurrent requests, never escalating rounds, respecting the OSRM demo
  server's documented rate policy. A via point whose primary offset lands
  off the routable network (common near a coastline) gets one retry at a
  smaller offset before being dropped. Every candidate — recovered or not —
  still goes through the same near-duplicate/spur/duration-ratio checks, so
  this never pads the list: a trip with a genuine route-network bottleneck
  (e.g. one practical Channel crossing between continental Europe and the
  UK, confirmed live by inspecting OSRM's own routed path) honestly shows
  fewer than 3, with a message that says so plainly rather than reading as
  a search failure.

## Setup

### Backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # OSRM/Nominatim/Open-Meteo need no keys — defaults work out of the box
python manage.py runserver      # http://localhost:8000
```

Run the test suite (125 tests, pure-logic + mocked-integration + view-boundary):

```bash
pytest
```

API docs once the server is running: `http://localhost:8000/api/docs/`
(Swagger) or `/api/redoc/`.

### Frontend

```bash
cd frontend
npm install
cp .env.example .env             # VITE_API_BASE_URL=http://localhost:8000
npm run dev                      # http://localhost:5173
```

```bash
npm run build                    # tsc -b && vite build
npm run lint                     # oxlint
```

## API

One primary endpoint plus two small geocoding helpers used by the
location-search UI:

- `POST /api/routes/analyze/` — the full pipeline described above. Request/
  response shapes are documented in `backend/api/serializers.py` and the
  live OpenAPI schema at `/api/schema/`.
- `GET /api/geocode/suggest/?q=` — autocomplete candidates for the
  origin/destination inputs.
- `GET /api/geocode/reverse/?lat=&lon=` — human-readable label for a point
  picked directly on the map.

Locations are always resolved client-side (autocomplete pick or map click)
before `analyze/` is called — the backend never geocodes free text blind.
This is deliberate: an earlier version did, and a typo ("Indianapolis, NL")
silently matched a low-confidence country-level fallback instead of
failing. Requiring a resolved `{label, lat, lon}` means a human always
confirms the actual candidate before it reaches routing.

## Risk rules (single source of truth: `backend/routing/risk.py`)

| Condition | Low | Moderate | High | Severe | No Travel |
|---|---|---|---|---|---|
| Wind (mph) | <25 | 25–<35 | 35–<45 | 45–<55 | ≥55 |
| Rain (in/hr) | <0.10 | 0.10–<0.25 | 0.25–<0.50 | 0.50–1.00 | >1.00 |
| Snow (in/hr) | <0.5 | 0.5–<1.0 | 1.0–<2.0 | 2.0–3.0 | >3.0 |

Load rules (applied to the wind level): wind ≥55 mph is No Travel for any
load; 45–54 mph with >30,000 lb is No Travel; 35–44 mph with >40,000 lb
escalates to Severe. A checkpoint's overall level is the max across wind
(after load rules), rain, and snow, with every contributing factor recorded
for the UI tooltip — never just one.

## Assumptions & documented ambiguities

The brief's threshold table overlaps at its boundaries (e.g. `0.25` appears
in two columns). Resolved as: lower bound inclusive, upper bound exclusive,
except the No Travel thresholds, which are used exactly as given (≥55 mph,
>1.00 in/hr, >3.0 in/hr).

Other decisions made explicit rather than silently assumed:

- **Aggregation**: each checkpoint "owns" the road segment back to the
  previous checkpoint; average risk is the distance-weighted mean of
  ordinal levels, not a simple average of checkpoints.
- **Ranking**: `(has_no_travel, severe_miles, high_miles, avg_risk,
  duration)`, ascending. No Travel isn't in the brief's preference list —
  treating it as disqualifying over all four listed criteria is a
  deliberate safety decision, stated here rather than buried in code.
- **Forecast-horizon overrun**: a checkpoint whose ETA falls outside the
  weather provider's returned horizon gets risk = `null` ("unavailable"),
  excluded from aggregation — never backfilled with current/guessed
  weather.
- **Timezone handling**: departure must be submitted tz-aware; all internal
  arithmetic is UTC. Per-checkpoint local time across a timezone-crossing
  route is not computed — a known limitation, not a silent
  reinterpretation.
- **Departure window**: capped to the next 7 days — deliberately tighter
  than Open-Meteo's actual 16-day forecast ceiling, since forecast skill
  degrades well before that horizon; a product choice, not a provider
  limit. A departure outside the window (including the past) is rejected
  with a clear 400 rather than silently returning an empty/garbage
  forecast for every checkpoint.
- **Load weight cap**: 80,000 lb, the US federal max gross vehicle weight
  on interstates — a sanity ceiling against fat-fingered input, not a
  claimed legal limit, since this field is cargo-only.
- **Wind reading**: sustained wind speed drives classification; gust data
  (when available) is informational only, never substituted in.
- **Tie-break within a checkpoint**: when two hazards (e.g. wind and rain)
  independently produce the same final level, both are listed in `reasons`
  — no arbitrary single pick.

## Known limitations

- No truck/HGV-aware road selection (see OSRM trade-off above) — height,
  width, and axle restrictions are out of scope; only load weight feeds the
  risk rules.
- Weather-provider failures are currently a flat `502` regardless of cause
  (timeout vs. malformed response) — not yet split into distinct failure
  modes.
- No persistence: every request re-fetches routing and weather from
  scratch (aside from in-process response caching within a single server
  lifetime). This is a stateless demo service by design — see
  `config/settings.py`'s module docstring.
- No automated frontend/E2E test coverage; backend logic and the view
  boundary are covered by 125 pytest tests, verified additionally against
  the *live* OSRM/Nominatim/Open-Meteo APIs during development, not just
  mocks.

## Project structure

```
backend/
  api/            DRF views + serializers (thin orchestration layer)
  integrations/   External API clients (OSRM, Nominatim, Open-Meteo) — timeouts, typed errors
  routing/        Pure business logic: checkpoints, ETA, risk, recommendation, heatmap grid
frontend/
  src/api/        Typed fetch client + response types
  src/components/ TripForm, MapView, RouteSummaryPanel, RecommendationBanner, ForecastSlider, ...
  src/hooks/      useAnalyzeRoutes (TanStack Query) and small generic GET/POST wrappers
  src/store/      Zustand store — UI-only state (selected route, forecast slider hour)
```

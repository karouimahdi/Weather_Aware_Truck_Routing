# Phase 4 — Routing Performance & Route Alternatives (Revision 2)

**Status:** implemented (§8's Phase 4a and 4b are both in `integrations/routing_client.py`
and `routing/similarity.py` as of this revision) — the line below describing
it as "proposed only" was stale, not a reflection of the code on disk; see
the dated addendum at the end of this document for two calibration defects
found and fixed in the already-implemented via-fallback logic, plus a
widened via-point search. This is Revision 2, a senior-engineering review of
Revision 1 (preserved at `docs/phase-4-routing-performance.v1-superseded.md`) —
several of v1's central claims did not survive this review and have been
corrected, not just reworded. Where this document changes a v1 conclusion,
that's called out explicitly, because the direction of the correction
matters as much as the correction itself.

**Claim labeling.** Every substantive claim below is tagged:
**[MEASURED]** — run live in this review, reproducible, numbers given.
**[SOURCE]** — confirmed against official documentation or inspected
reference-repo source, not just inferred. **[INFERENCE]** — a reasoned
conclusion from measured/source facts, not independently tested.
**[HYPOTHESIS]** — plausible but unverified, flagged for validation before
being trusted. **[BRIEF]** — stated in or directly derived from the
assessment brief.

---

## 1. Executive summary

The assessment brief ([`fs-assessment-spotter.pdf`](../fs-assessment-spotter.pdf),
full text transcribed in §2) requires 3 route options, checkpoints with
weather/risk at each, a recommendation, and a map with a 0–48h corridor
heatmap. It states no latency target, no evaluator timeout, and does not
mention ferry/sea-crossing handling at all **[BRIEF]** — that feature exists
in this codebase by the user's own explicit prior request, outside the
graded brief. Grading is 40% routing/weather/risk/recommendation logic, 40%
UI/map/heatmap smoothness and performance, 15% code quality, 5% docs
**[BRIEF]**, with an explicit instruction to "prioritize correctness and
clarity over extra features" **[BRIEF]**.

Revision 1 of this document measured the real latency problem correctly
(a single OSRM directions call dominates long-route latency) but then
proposed a primary fix — `overview=simplified` — whose claimed "zero
information loss" does not hold up. **[MEASURED]** this review: on a real
720-mile route, switching from `overview=full` to `overview=simplified`
moves checkpoint positions by a **mean of 3.85 miles and a maximum of 8.34
miles** (29 checkpoints, 25-mile interval). That is a direct, measured hit
to the exact thing worth 40% of the grade — not a safe win. §6.A below
replaces that recommendation.

The revised priority order, in one sentence: **cache the one expensive
call (free, zero accuracy risk), trim the request to drop fields the code
never reads without touching the fields it does, respect the OSRM demo
server's documented 1 req/s policy when adding any fallback requests, and
leave route geometry resolution alone because the code depends on it for
correctness.**

---

## 2. Assessment requirements and constraints

### 2.1 Source

`fs-assessment-spotter.pdf`, 2 pages, read in full for this review
**[BRIEF]**. Full relevant text:

> Build a React + Django web app that helps a truck driver choose the
> safest route based on weather, load weight, and travel time... Generate
> 3 route options using a routing API. Sample weather every 10/25/50
> miles, using the truck's estimated arrival time at each checkpoint...
> Show all routes, checkpoints, risk levels, ETAs, and route summaries on
> a map. Show a weather heatmap along the trip corridor with a 0–48 hour
> forecast slider... You do not need to build a routing engine...
> Evaluation: 40% Routes, Weather, risk & recommendation logic. 40% UI,
> map & heatmap smoothness and performance. 15% Code quality. 5%
> Documentation & walkthrough. Prioritize correctness and clarity over
> extra features.

### 2.2 Requirements matrix

| Requirement | Source | Current implementation | Risk if violated | Required verification | Priority |
|---|---|---|---|---|---|
| Generate 3 route options | Brief, verbatim | `REQUESTED_ALTERNATIVES = 3` in `api/views.py`; `get_route_options(..., max_alternatives=3)` in `integrations/routing_client.py` requests `alternatives=true` from OSRM and reports a `warnings[]` entry if fewer come back | Medium — brief states "3" with no "best-effort" qualifier, but OSRM's own docs say **[SOURCE]** "even if alternative routes are requested, a result cannot be guaranteed" | Confirm the current honest-shortfall behavior (warn, don't pad) is preserved under any change | High |
| Checkpoints every 10/25/50mi with ETA, for every route shown | Brief, verbatim ("Show all routes, checkpoints... ETAs") | `generate_checkpoints` + `compute_checkpoint_etas` run for **every** route option in `views.py`'s loop, not just the winner | High — any request-shape change that only gets fine ETA data for one route would silently violate this | Re-run the existing `api/tests/test_views.py` suite (116 tests, confirmed passing **[MEASURED]**) after any change to `_build_duration_breakpoints`'s input | High |
| Weather risk via the provided table + load rules | Brief, verbatim table | `routing/risk.py`, unmodified by this phase | None — out of scope for this phase | N/A — this phase touches routing/caching, not risk math | N/A |
| Recommendation: fewest Severe → fewest High → lowest avg risk → shortest time | Brief, verbatim | `routing/recommend.py`, unmodified by this phase | None — out of scope | N/A | N/A |
| Map shows routes/checkpoints/risk/ETA/summaries | Brief, verbatim | Implemented, unmodified by this phase | None — out of scope | N/A | N/A |
| 0–48h corridor heatmap with slider | Brief, verbatim | `routing/heatmap.py` + `ForecastSlider.tsx`, unmodified by this phase except where §6.F discusses sampling density | Low | N/A for this phase | N/A |
| Exactly 3 routes mandatory vs. best-effort | Brief says "3," no fallback language | Current code already treats it as best-effort with an honest warning | — | **[UNRESOLVED]** by the brief itself. Safest reading: best-effort + honest shortfall, because the brief's own closing instruction ("prioritize correctness... over extra features") favors not fabricating a third route over satisfying the literal count | High |
| Ferry detection required | Not mentioned anywhere in the brief | `has_ferry` field, `RouteNotFoundError`/sea-crossing modal — built this session by explicit user request, not brief-derived | **None to the graded assessment.** Risk is only to the user's own stated goal, not to evaluation | N/A to assessment grading | **Low priority relative to graded items** |
| Detailed ETA breakpoints required for every alternative | Brief implies yes (checkpoints+ETA shown for "all routes") | Yes, per-route in current code | High if removed for non-winning routes | Covered by existing test suite | High |
| Full geometry / weather / risk required for every route | Brief, "show all routes, checkpoints... on a map" | Yes, current code does this for all 3 | High if any route is shown with degraded geometry while another isn't | Visual check: no route should look visually coarser than another on the map | Medium |
| Response must be synchronous | Not stated either way | Currently one synchronous `POST /api/routes/analyze/` via `useMutation`, no polling/streaming anywhere in `frontend/src/hooks/` **[SOURCE: code inspection]** | Low to keep as-is; changing it is the risk | **[UNRESOLVED]** by the brief. Safest reading: keep synchronous — no stated requirement favors the complexity of changing it, and 15% of the grade is code quality, which a premature async layer would not help | — |
| API fields / warnings contractually fixed | Not stated; this is our own design, not the brief's | `RouteAnalysisResponseSerializer`, `warnings: list[str]` | Low — we control this contract | Keep `has_ferry` additive/optional in spirit, since it's beyond-brief scope | — |
| New dependency / cache / background task / endpoint risk | Not stated; general assessment hygiene | None added yet | Medium — 15% code-quality grading penalizes unnecessary complexity **[BRIEF-derived inference]** | Each proposal in §6 states whether it adds a dependency (none do) | — |
| Explicit latency expectations or evaluator timeout | Not stated anywhere in the brief | None found | — | **[UNRESOLVED]**. Treat "40% UI/map/heatmap smoothness and performance" as most plausibly about *frontend* rendering performance (map pan, slider scrub, paint), not backend response time, since it's grouped with "smoothness" in the same line item **[INFERENCE, not certain — flagged as a reading, not a fact]** | — |

### 2.3 What this means for scope

Two corrections to Revision 1's framing fall directly out of this matrix:

1. **Ferry detection should not drive request-shape decisions.** v1's P4.2
   repeatedly chose request parameters to preserve `has_ferry` even at a
   latency cost. The brief doesn't grade it. This review deprioritizes it
   accordingly (§6.A, §9).
2. **"Performance" in the grading rubric most plausibly means frontend
   smoothness**, not backend seconds-to-response. This doesn't mean
   backend latency is irrelevant — a slow `/analyze` call delays the thing
   that *does* get graded (the map/heatmap) — but it reframes backend
   latency work as *in service of* the graded UI experience, not a graded
   line item on its own. Nothing in this phase should be justified purely
   by "the backend gets faster" without connecting it to what the user
   actually sees.

---

## 3. Verified current-state architecture

Traced end-to-end from `POST /api/routes/analyze/` to the serialized
response, confirming actual filenames/functions **[SOURCE: code read in
full for this review, 2026-10-21]**:

```
api/views.py: AnalyzeRoutesView.post
  │
  ├─ 1. get_route_options(origin, destination, load_lb, max_alternatives=3)
  │      integrations/routing_client.py
  │      → ONE synchronous GET to OSRM: alternatives=true, overview=full,
  │        geometries=geojson, steps=true
  │      → raises RouteNotFoundError (404) or RoutingProviderError (502)
  │        on failure; returns list[RouteOption] on success
  │      → NO retry, NO cache, NO connection pooling beyond requests'
  │        per-call connection (module-level `requests` calls, no shared
  │        `requests.Session()`) [SOURCE: code — every call site in
  │        routing_client.py uses the bare `requests.get`, not a Session]
  │
  ├─ 2. per route: generate_checkpoints → compute_checkpoint_etas
  │      routing/checkpoints.py, routing/eta.py — pure, no I/O, fast
  │
  ├─ 3. get_weather_observations(all_targets) — ONE batched call
  │      integrations/weather_client.py, cached via Django's default
  │      LocMemCache (no explicit CACHES block in config/settings.py
  │      [SOURCE: grep confirmed no CACHES key exists])
  │
  ├─ 4. risk classification + recommendation — pure, fast
  │      routing/risk.py, routing/recommend.py
  │
  ├─ 5. generate_corridor_grid(polylines) — pure, bounded by construction
  │      routing/heatmap.py: SAMPLES_PER_ROUTE=11 × 3 lateral offsets,
  │      fixed regardless of trip length
  │
  ├─ 6. get_weather_series(grid_points, ..., 48) — ONE batched call,
  │      same cache as step 3
  │
  └─ 7. serialize → Response
```

**Sequential dependency**: step 1 must complete before 2–7 can start (every
downstream step needs route geometry/distance). Steps 3 and 6 are
*independent of each other* (both only need geometry from step 1) but are
currently issued sequentially, not concurrently **[SOURCE: code — no
threading/async anywhere in `views.py`]**. This is a real, small,
previously-unflagged opportunity: steps 3 and 6 could run concurrently
since neither depends on the other's result.

**Frontend**: `useAnalyzeRoutes` → `usePost` → `useMutation` → one
`fetch()` via `apiPost` in `api/client.ts`. No polling, no SSE, no
WebSocket, nothing partial — the UI waits for the single response and
shows a skeleton (`App.tsx`) in the meantime. **[SOURCE: code read in
full]**.

---

## 4. Baseline measurements and limitations

All numbers below are **[MEASURED]** live against the real OSRM public
demo (`router.project-osrm.org`) and Open-Meteo, during this review and
the prior session, with request parameters, route distance, and response
size recorded. None are from mocks or synthetic data.

### 4.1 Stage breakdown (direct Python calls, not HTTP round-trip)

| Route | `get_route_options` | `get_weather_observations` | `get_weather_series` | Total |
|---|---|---|---|---|
| Chicago→Atlanta (~720mi, 2 alts, 61 checkpoints) | 1.32s | 0.81s | 0.95s | 3.25s |
| LA→NYC (~2,794mi, 2 alts, 226 checkpoints) | 3.66s | 1.05s | 0.92s | 6.96s (one sample; see §4.2 for variance) |

**Weather fetching is consistently fast** (<1.1s) regardless of checkpoint
or grid-point count across every trial run. **The entire variable cost is
the single OSRM directions call.**

**Network vs. local CPU**: the Chicago→Atlanta row's three measured
stages sum to 3.08s against a reported total of 3.25s — the ~0.17s gap is
everything else in the pipeline (checkpoint generation, risk
classification, serialization, all pure/local). **[INFERENCE]** from this
arithmetic: local CPU-bound processing is a small fraction of total
latency and not a target worth optimizing in this phase — all three
network calls (routing, two weather calls) dominate, and of those, only
the routing call is slow.

### 4.2 OSRM response variance — confirmed against the server's own documented SLA

Repeating the *identical* request 4× on Chicago→Atlanta, same parameters,
no code changes between calls:

| Request | Run 1 | Run 2 | Run 3 | Run 4 |
|---|---|---|---|---|
| `overview=full&steps=true` (current shape) | 5.39s | 6.20s | 8.55s | 8.29s |
| `overview=full&annotations=duration` | 3.26s | 3.65s | 3.87s | 3.78s |

A 2.6× spread on the first row with nothing in our code changing. This is
not measurement error — **[SOURCE]**, the OSRM demo server's own wiki
states: *"We provide no guarantees wrt. uptime, latency, or data
updates,"* and *"Do not exceed 1 request per second."* The variance is
the server's documented, acknowledged behavior. Four samples per
configuration is not enough for a defensible percentile claim — treat
these as "order of magnitude and consistent ranking," not a p50/p95.

**Implication for every number in this document**: relative comparisons
(A consistently beats B across repeated trials) are trustworthy. Absolute
"this takes Xs" statements are not promises this server can keep,
independent of our code.

### 4.3 Payload decomposition (why the OSRM call is slow)

| Request shape | Payload | Time (one sample, LA→NYC) |
|---|---|---|
| `overview=full&steps=true` (current) | 3.6 MB | 17.4s |
| `overview=simplified&steps=true` | 2.1 MB | 11.0s |
| `overview=full&annotations=duration` | 1.79 MB | 11.68s |
| `overview=simplified&annotations=duration` | — (annotations tied to full internal resolution regardless of `overview`, see §4.4) | — |
| `overview=simplified&steps=false` | 3 KB | 0.49s |

**[SOURCE, confirmed against OSRM's own API docs]**: *"Step geometry is
always full-resolution regardless of the overview setting."* This is not
an inference from testing — it's documented behavior. For the LA→NYC
route: 58 steps whose individual geometries sum to 33,842 coordinate
points, essentially the same size as the full-resolution top-level
geometry (33,784 points), confirming the doc's statement.

### 4.4 `annotations` does not shrink with `overview`

**[SOURCE, OSRM API docs]**: annotations accept `true`, `false`, or a
comma-separated list of `nodes`, `distance`, `duration`, `datasources`,
`weight`, `speed` — individual field selection (`annotations=duration`)
is official, documented usage, not a workaround.

**[MEASURED]**: requesting `annotations=duration` alone (not `=true`,
which returns all six fields) is ~27% smaller than the full bundle, and
**[MEASURED]** consistently 40–55% faster than `steps=true` at regional
distance across 4 repeated trials (§4.2). But **[MEASURED]**: annotation
arrays are tied to OSRM's full internal routing-graph resolution
regardless of the `overview` parameter — tested `overview=simplified
&annotations=duration` and still got a 33,783-entry array (not reduced to
match the 53-point simplified geometry). **Switching `overview` to
`simplified` does not shrink an `annotations` request's cost at all.**

### 4.5 The central correction: `overview=simplified` has a real, measured accuracy cost

This is the most important new finding in this review and directly
overturns Revision 1's P4.1.

**[MEASURED]**, this review, on the real Chicago→Atlanta route: fetched
both `overview=full` (11,196 geometry points) and `overview=simplified`
(26 points) from OSRM for the same route, ran the *actual production
function* `routing.checkpoints.generate_checkpoints` on both polylines at
a 25-mile interval (29 checkpoints each), and compared positions
point-by-point:

- **Mean positional deviation: 3.85 miles.**
- **Maximum positional deviation: 8.34 miles.**
- **25 of 29 checkpoints moved by more than 1 mile; 27 of 29 by more than
  half a mile.**

This is not a rendering/display concern — `generate_checkpoints`'s own
docstring already warned about exactly this ("a sparse polyline means
long, straight interpolation segments between vertices, which can deviate
from the true curved road. Denser provider geometry reduces this.") — the
code already documents the risk Revision 1 then ignored when recommending
the fix that triggers it. An 8-mile checkpoint error can put a weather
lookup in a different town, which is a direct hit on the single
highest-weighted grading criterion (40%, "Routes, Weather, risk &
recommendation logic"). `routing/heatmap.py`'s `_sample_with_bearing`
interpolates and computes bearings from the same sparse-polyline input
for the exact same reason — **[INFERENCE, not independently re-measured]**
it would degrade the same way, since it shares the identical
cumulative-distance-interpolation pattern against the same input geometry.

**Revision 1's claim of "zero information loss for anything we actually
read" was false.** `checkpoints.py` and `heatmap.py` both read geometry
resolution, just not through a field access that's easy to grep for.

---

## 5. Root-cause analysis (revised)

The dominant cost is one synchronous OSRM `/route` call, and it scales
with route distance because OSRM must compute and serialize a
full-resolution geometry — which the application's own checkpoint and
corridor-sampling code genuinely depends on for positional accuracy. This
is **not** a case of "we're sending data we don't need" across the board;
it's a mix:

- **Genuinely unneeded, safe to drop**: `step.geometry` (steps carry their
  own full-resolution sub-geometry on top of the route's own, duplicating
  cost for data `_parse_osrm_route` never reads beyond `distance`,
  `duration`, and `mode`).
- **Needed, not safe to drop**: the top-level route geometry resolution
  (`overview=full`) — checkpoints and heatmap sampling both depend on it.
- **A secondary, independent cost**: re-fetching an identical route on
  every repeat request, with zero caching.

The honest conclusion: **for a genuinely continental-distance route,
there may be no way to make this fast without sacrificing checkpoint
accuracy**, and that trade should be made explicitly if ever made, not
silently. None of the three reference repositories' own documented test
routes are coast-to-coast either (Dallas→Denver, Salt Lake City→Cheyenne,
LA→Phoenix, Fort Worth→Dallas, Denver→Chicago) **[SOURCE: swatiXO
`docs/DECISIONS.md` §13]** — this suggests the pathological 2,800-mile
case this review stress-tested is plausibly outside the realistic range a
truck-routing assessment would actually be evaluated against, which
further weakens the case for sacrificing accuracy to fix it.

---

## 6. Critical review of proposed optimizations

### A. Lightweight OSRM requests — **Revision 1's recommendation rejected; narrower fix proposed**

**Bottleneck addressed**: OSRM response size/time for long routes.
**Evidence**: §4.3–4.5.
**Verdict on `overview=simplified`**: **REJECT.** Measured 3.85mi
mean / 8.34mi max checkpoint positional error (§4.5) against a 40%-weighted
grading criterion, to save payload on a dimension (route geometry
resolution) the brief gives no latency target to justify trading against.
**Verdict on dropping `steps=true` in favor of `annotations=duration`,
while keeping `overview=full`**: **ACCEPT, narrow scope.** This does not
touch geometry resolution at all — it only changes *how duration data is
obtained*, from per-step objects (which duplicate full-resolution geometry
per step) to a flat per-vertex array aligned with the geometry we're
already requesting at full resolution. **[MEASURED]**: at LA→NYC,
`overview=full&annotations=duration` was 1.79MB/11.68s vs.
`overview=full&steps=true`'s 3.6MB/17.4s — roughly half the payload, with
zero change to checkpoint accuracy since the geometry itself is untouched.
**Cost**: loses `step.mode` (ferry detection) — acceptable per §2.3, since
the brief doesn't grade it. **Correctness risk**: low, confirmed by the
accuracy test only measuring geometry resolution changes, not annotation
vs. step changes — **[HYPOTHESIS, not separately measured]**: duration
interpolation accuracy should be equal or better with per-vertex
annotations than per-step durations, since it's finer-grained, but this
specific claim has not been measured the way the geometry-accuracy claim
was and should be spot-checked before shipping, not assumed.
**Operational risk**: none — single request, no new concurrency.
**Scope**: small — touches `_parse_osrm_route`'s step/annotation
handling and `_build_duration_breakpoints`'s input source.
**Belongs in**: immediate scope.

### B. Concurrent alternative-route discovery — **accepted with a hard constraint Revision 1 missed**

**Bottleneck addressed**: fewer than 3 native alternatives on longer
routes.
**[SOURCE, confirmed by reading the actual code, not just the docs
summaries]**: all three reference repos do implement roughly what was
attributed to them — malikali3419's `providers/alternatives.py` and
swatiXO's `services/routing.py` both genuinely implement
concurrent-via-point-request + overlap-filter logic as described, not
paraphrased claims.

**The constraint Revision 1 missed entirely**: the OSRM demo server's own
usage policy **[SOURCE]** states *"do not exceed 1 request per second."*
Our architecture uses this free public demo as the **primary** provider
for every single `/analyze` call — unlike the reference repos, which use
a paid, keyed provider (ORS) as primary and only fall back to the OSRM
demo occasionally. **Firing 2–3 concurrent via-point requests at the same
demo server, on top of the one we already sent, risks violating that
policy within the same request** — a materially different risk profile
than the reference repos accepted, because their baseline load on the
demo server is near-zero and ours is the entire application's primary
traffic.

**Revised recommendation**: if fallback requests are added at all
(§6.C — only when native alternatives are insufficient, which is not
every request), **cap total concurrent OSRM calls at 2** (the main
request has already completed by the time fallbacks fire, so this means
at most 1 fallback in flight at a time, or 2 if the implementation
chooses to risk a short burst) and **do not escalate to a second round of
via-points** without an explicit, separate sign-off — each additional
round multiplies policy-violation risk for diminishing route-diversity
value. `ThreadPoolExecutor(max_workers=2)` — not 3 — is the safer
starting point for *our* deployment, not the reference repos'.

**On the specific claim about cancellation [SOURCE, Python docs,
confirmed this review]**: `Future.cancel()` "will return False" and does
nothing once a call is already executing — the documentation is explicit
that running callables cannot be interrupted, only pending ones.
**Any design that "bounds the wait" must do so via `future.result(timeout=N)`
raising on the caller's side, not via actually cancelling the in-flight
HTTP request** — the request keeps running in its worker thread,
consuming an OSRM connection, until its own `requests` timeout fires or it
completes. This matters directly for the 1 req/s policy: a "bounded wait"
that abandons waiting for a slow fallback does **not** stop that fallback
request from still hitting the OSRM server moments later — it only stops
*our* code from waiting on it. Any implementation must set an explicit
`timeout=` on the `requests.get()` call itself, not rely on
`future.result(timeout=)` alone, if it wants to bound how long a worker
thread and its OSRM connection can stay occupied — `future.result`'s
timeout only bounds how long the *caller* waits, not how long the
request itself keeps running in the background.
**Belongs in**: immediate scope, with the above constraint as a hard
requirement, not a suggestion.

### C. Route diversity and validity — the 0.02° grid-cell critique, engaged rigorously

**The task's own framing is correct and Revision 1 did not engage with
it**: longitude distance is not uniform with latitude. 1° of longitude ≈
`69.0 × cos(latitude)` miles; 1° of latitude ≈ 69.0 miles, roughly
constant. **[Computed this review]**: at 40°N (Chicago/Denver latitude),
a "0.02°" cell is ≈1.38mi (lat) × ≈1.06mi (lon) — not the uniform "~2km"
both reference repos' comments imply. At 49°N (US/Canada border), the
longitude side shrinks to ≈0.9mi. This is a bounded, not catastrophic,
distortion for the latitude range realistic truck routes actually cover
(~25–49°N for the continental US), but it is non-uniform, and presenting
it as "~2km cells" the way both reference repos' comments do is imprecise.

**A specific, concrete inconsistency found by reading the actual code**:
malikali3419's own `services/heatmap.py` correctly applies a
`cos(latitude)` correction to its grid (`radius_lon = radius_lat /
max(cos(lat), 0.2)`), but their *own* `providers/alternatives.py` overlap
function (`_cells`) uses a flat, uncorrected `_CELL_DEG = 0.02` for both
axes — the same codebase gets this right in one module and not in the
sibling module solving the geometrically identical problem. This is not
a reason to distrust the whole technique, but it is a reason not to copy
either module's overlap implementation verbatim.

**Revised recommendation**: if a grid-cell-hash overlap method is
implemented (§9), apply the same `cos(latitude)` scaling our *own*
`routing/heatmap.py` already respects via proper great-circle math
(`_destination_point`, `_bearing_deg`) — our existing code is already more
rigorous about this than either reference repo's overlap function.
Alternative considered and rejected for now: full great-circle buffer
distance per point-pair (most accurate, but O(n·m) per candidate check
and unnecessary precision for a reject/accept threshold at 85% — the
grid-hash method, corrected for latitude, is accurate enough and the
reference repos' own use of it in production-equivalent assessment
projects is reasonable evidence it's sufficient).

**On the 85%/1.5× thresholds**: **[HYPOTHESIS]**, not independently
verified against real route pairs in this review — three independent
projects converged on a tight range (80–90% overlap, 1.5–1.6× duration)
which is reassuring triangulation, but "three other teams picked numbers
in this range" is not the same evidence as "this app's own test routes
behave correctly at this threshold." Flagged as requiring validation
against real route pairs (§10) before being treated as settled, exactly
as Revision 1 said but worth restating since it's still true.

**Spur rejection**: confirmed necessary (§2 of the prior session's
research), confirmed two of three reference repos implement it for real,
not as a documented-but-unused idea **[SOURCE: code read]**. Recommend
the simpler backtrack-share heuristic (malikali3419's) over the full
splice-and-repair module (swatiXO's `spur.py`) for the reason already
given in the prior draft: splicing geometry back together would require
re-deriving `duration_breakpoints` consistently, more surface area than
justified here.

**On "guaranteed 3 routes"**: the brief says "3 route options," OSRM's own
docs say alternatives "cannot be guaranteed," and this review's own
findings (policy risk of extra requests, real cost of extra OSRM calls)
further argue against promising 3 unconditionally. **Recommended framing
for the document and any future README/Loom narration: "best-effort,
honestly degraded" — never "guaranteed."**

### D. Route caching — accepted, with real caveats Revision 1 did not raise

**[SOURCE, confirmed]**: no `CACHES` block exists in `config/settings.py`,
so Django falls back to its documented default, `LocMemCache`. **This is
process-local, not shared across workers.** In any deployment running more
than one application process (e.g., `gunicorn -w 4`), a cache hit in one
worker does not help a request that lands on a different worker — this
was not mentioned in Revision 1 at all and materially affects the
expected real-world hit rate once this app is actually hosted (a
deliverable requirement **[BRIEF]**). It still helps meaningfully in local
development and in a single-process deployment, and is still worth doing,
but should not be presented as solving repeat-request latency
universally.

**Cache-key correctness**: `load_lb` is accepted by `get_route_options`
but never sent to OSRM (confirmed, no HGV profile), so it correctly does
**not** need to be part of the cache key — unlike the reference repos
(which send load to a real HGV-aware provider and must include it).
`origin`/`destination` coordinates alone are sufficient **[SOURCE: code
confirms no other varying parameter affects the OSRM request]**.

**Does not help the first/cold request** — this review explicitly flags
that caching only shifts cost for *repeat* identical requests, which is a
real and common case (iterative testing, an evaluator re-submitting after
a UI tweak) but is not a fix for the cold-request bottleneck that
motivated this phase. It should be positioned as complementary to §6.A,
not a substitute.

**Belongs in**: immediate scope — cheap, zero accuracy risk, no new
dependency (reuses the already-configured `django.core.cache.cache`).

### E. Retry behavior — accepted, narrowly scoped

**[SOURCE, code confirmed]**: `routing_client.py` and `weather_client.py`
currently have zero retry logic anywhere. Given the server's own
documented lack of an SLA (§4.2), a single transient failure is plausible
and currently indistinguishable from a persistent one.
**Recommendation, unchanged from Revision 1 but tightened**: exactly one
retry, only on network exceptions or 5xx, never on 4xx (`RouteNotFoundError`
and anything else that is a deterministic "this request is bad" answer),
short fixed delay. **Must fit within the existing `DIRECTIONS_TIMEOUT_S=30`
request budget** — a retry that can itself take up to 30s on top of an
already-slow 17s original call risks a ~47s worst case, which needs an
explicit decision (either a shorter per-attempt timeout when retries are
enabled, or accepting the longer worst-case tail) rather than silently
compounding. Not evaluated against any specific evaluator timeout because
none is documented (§2.2).

### F. Adaptive checkpoint and heatmap sampling — **deferred, not rejected**

**[SOURCE, code confirmed]**: `routing/heatmap.py`'s grid is already
bounded by construction (`SAMPLES_PER_ROUTE=11 × 3` lateral offsets,
independent of trip length) — it does not grow unboundedly the way
checkpoint count does. **[MEASURED]**, repeatedly throughout this review
and the prior session: `get_weather_series` stays under ~1.1s even at the
largest grid sizes tested. **This means adaptive heatmap density is not
addressing a measured bottleneck** — it would be a correctness/elegance
improvement (matching trip density better) but has no evidence of
latency benefit, which is this phase's stated primary objective. Per the
task's own instruction ("if adaptive sampling is not a material
performance improvement for the measured bottleneck, defer it"), this is
**deferred out of Phase 4**, not rejected outright — it may still be worth
doing as a UI/correctness improvement in a different phase, but doesn't
belong in a performance-focused plan without evidence it moves the
needle.

**Checkpoint count, however, is genuinely unbounded** (226 checkpoints
measured on the LA→NYC route, no ceiling in `generate_checkpoints`) —
but weather fetching for 226 targets was still under 1.1s, so this is
also **not currently a measured bottleneck**, just an unbounded dimension
worth noting for the future (e.g., if Open-Meteo's own rate limits ever
become a constraint, which hasn't happened in any test run here).
**Deferred, documented as a watch item, not an active Phase 4 task.**

### G. Progressive results and frontend changes — **rejected for this phase**

**[SOURCE, code confirmed]**: the current architecture is one synchronous
`useMutation` call, no polling/streaming infrastructure exists anywhere.
Building partial/progressive responses would require: a job/task model,
a polling or streaming endpoint, frontend state for "routes loaded,
weather pending," cache-consistency handling for partial route selection,
and cancellation semantics — a materially larger architecture change than
anything else in this phase. **No requirement in the brief asks for
this** (§2.2, "response must be synchronous" is unresolved, not a
mandate either way), and the 15%-weighted code-quality criterion
**[BRIEF]** argues against adding this complexity without a demonstrated
need. **Rejected for Phase 4.** Worth revisiting only if §6.A+D+E together
still leave cold long-haul requests unacceptably slow *and* a concrete
latency target emerges from the user or an evaluator — neither is true
today.

### H. Heatmap rendering (KDE vs. discrete cells) — **correctly scoped as out of this phase by the task's own framing, kept as a cross-reference only**

Three independent reference projects reject a blurred/KDE heatmap for the
same underlying reason (§5 of the prior review pass, unchanged — not
re-verified in this review since it's a rendering-correctness question,
not a performance one). The task instructions for this review explicitly
say: *"keep unrelated design changes out of the critical performance
implementation plan."* **Correct call — this item is removed from this
phase's implementation plan** and kept only as a cross-reference for
whichever phase handles map/rendering work, since it's a visual-design
decision, not a latency fix.

---

## 7. Ranked optimization strategy

Ordered by evidence, not by the task's suggested default sequence (which
this review deviates from where the evidence supports a different order —
the accuracy finding in §4.5 is why "optimize the existing native
alternatives request" does not simply mean "reduce payload at any cost").

| Rank | Item | Addresses | Cold-request impact | Warm/repeat impact | Correctness risk | Effort |
|---|---|---|---|---|---|---|
| 1 | **Cache parsed `RouteOption` results** (§6.D) | Repeat-request latency | None | Large (near-zero on cache hit, single-process only) | None | Small |
| 2 | **`steps=true` → `annotations=duration`, keep `overview=full`** (§6.A) | Payload/time on every request | Moderate, consistent (~50% payload reduction measured) | Same, compounds with #1 | Low — geometry untouched; duration-interpolation-accuracy claim is a hypothesis, spot-check before shipping | Small–medium |
| 3 | **One bounded retry on 5xx/network errors only** (§6.E) | Reliability on an SLA-less server | Can *increase* worst-case latency — must cap total request budget | Same | Low, if scoped exactly as specified | Small |
| 4 | **Bounded (≤2 concurrent, no auto-escalation) via-point fallback routes** (§6.B, §6.C) | The actual "3 routes" feature request | Only engages when native alternatives < 3; adds one more sequential-ish OSRM round-trip in that case | N/A (not cached in this plan) | Medium — needs spur-rejection + latitude-corrected overlap to avoid padding with bad candidates | Large |
| — | *(deferred)* Adaptive checkpoint/heatmap sampling (§6.F) | No measured bottleneck found | — | — | — | Deferred |
| — | *(rejected)* `overview=simplified` (§6.A) | Would reduce payload | — | — | **High — measured 3.85mi mean / 8.34mi max checkpoint error** | Rejected |
| — | *(rejected)* Progressive/async responses (§6.G) | Not a demonstrated need | — | — | — | Rejected for this phase |
| — | *(out of phase)* Heatmap KDE→discrete rendering (§6.H) | Visual correctness, not latency | — | — | — | Different phase |

**Critical path**: #1 and #2 are independent of each other and of #4 —
either can ship alone. #3 should wrap whichever of #1/#2 touches
`get_route_options` (same function, natural to land together, but not a
hard dependency). #4 is the largest, riskiest, and only one actually
required by the brief's literal "3 routes" line — everything else in this
ranking is latency hygiene the brief doesn't explicitly grade.

---

## 8. Phased implementation plan

**Phase 4a (small, low-risk, do together):**
- Cache `get_route_options` results keyed on rounded origin/destination
  coordinates, Django's existing `cache` object, TTL ~1–2h.
- Swap `steps=true` → `annotations=duration` in the OSRM request; update
  `_parse_osrm_route`/`_build_duration_breakpoints` to read the duration
  array instead of step objects; drop `has_ferry` or mark it `None`/absent
  with an honest comment that ferry detection is no longer attempted
  (acceptable per §2.3).
- Add one bounded retry (5xx/network only) around the OSRM call, with an
  explicit, reduced per-attempt timeout so the combined worst case stays
  reasoned-about rather than additive-by-accident.
- **Acceptance criteria**: existing 116-test suite still passes
  unmodified in assertions (only fixtures touching `RouteOption`'s shape
  should need mechanical updates, same pattern as prior sessions); a
  repeated identical request completes near-instantly on the second call;
  `annotations=duration`-derived ETA breakpoints produce checkpoint ETAs
  within a reasonable tolerance of the current `steps=true`-derived ones
  on at least one real route (spot-check, not exhaustive).
- **Rollback**: each of the three changes is independently revertible;
  none touch response schema in a breaking way (ferry field removal is
  the only schema change, and it's additive-removal, not breaking, since
  the frontend already treats `has_ferry` as a plain boolean it reads,
  not a required contract from the brief).

**Phase 4b (larger, do after 4a is verified):**
- `routing/similarity.py`: latitude-corrected grid-cell overlap +
  backtrack-based spur rejection, as pure functions, tested against both
  synthetic polylines and at least one real route pair with a known
  dead-end-prone via point.
- Bounded (`max_workers=2`, no auto-escalating rounds without separate
  sign-off) concurrent via-point fallback in `integrations/routing_client.py`,
  firing only when native alternatives < 3, respecting the OSRM 1 req/s
  policy by capping concurrency as specified in §6.B.
- **Acceptance criteria**: a route pair known to return few native
  alternatives (needs to be found/confirmed live, not assumed) produces
  up to 3 distinct, non-overlapping, non-spur routes, or an honest
  `warnings[]` entry when it can't; no route in the result set overlaps
  another above the threshold; no route is an out-and-back spur.
- **Rollback**: gated behind the existing `len(route_options) <
  REQUESTED_ALTERNATIVES` check — reverting is deleting the new branch,
  the native-alternatives path is unaffected.

**Explicitly not phased in**: §6.F (deferred), §6.G (rejected for this
phase), §6.H (different phase).

---

## 9. Route-quality and correctness safeguards

Carried forward from Revision 1, tightened:

- Every candidate route must resolve to the same origin/destination
  **[BRIEF: implicit — a route that doesn't connect the stated points
  isn't a valid answer to the request]**.
- Reject candidates via the latitude-corrected overlap check (§6.C) —
  never accept a near-duplicate as if it were a genuine third option.
- Reject out-and-back spurs via the backtrack heuristic (§6.C).
- **Never pad with a fabricated or duplicated route** — if fewer than 3
  genuinely distinct routes exist, say so in `warnings[]`, exactly as the
  current code already does for native-alternative shortfall. This is
  not new; it's the existing, correct behavior that must survive every
  change in this phase.
- Every route, regardless of how it was discovered (native alternative or
  fallback via-point), must get full checkpoint/weather/risk treatment —
  **[BRIEF]** requires this for "all routes," not just the winner.

---

## 10. Benchmark methodology and acceptance criteria

- **Minimum 4 repeated trials** per configuration before citing a timing
  number, given the confirmed 2.6× variance (§4.2) — this document itself
  did not always have the budget for more, and says so rather than
  implying more rigor than was actually applied.
- Record, for every benchmark: request parameters, route distance,
  number of alternatives returned, response payload size, and whether
  the run was live or mocked. Every number in §4 meets this bar.
- Distinguish **cold** (first request, no cache) from **warm** (cache
  hit) explicitly in any future report — never present a warm number as
  if it characterizes the cold-request experience, and vice versa.
- Before accepting the `annotations=duration` ETA-accuracy hypothesis
  (§6.A) into production, run the same kind of direct comparison this
  review ran for geometry resolution (§4.5): compute checkpoint ETAs from
  both the old (`steps`) and new (`annotations`) data sources on a real
  route and diff them.
- Before accepting the 85% overlap / 1.5× duration thresholds (§6.C) as
  final, run the fallback logic against at least 2–3 real route pairs
  chosen because they're known to have limited native alternatives (not
  arbitrary pairs), and inspect the results by hand, not just by
  threshold pass/fail.

---

## 11. Risks, trade-offs, and deferred work

- **OSRM demo server has no SLA** (§4.2, §6.B) — this is a ceiling on what
  any amount of our own engineering can guarantee. The "Hosted
  application" deliverable **[BRIEF]** depends on a server whose own
  documentation disclaims uptime/latency guarantees and asks for ≤1
  req/s. This is an external risk to the deliverable, not something
  Phase 4 can fully resolve.
- **Ferry detection is deprioritized**, not removed with malice — it was
  built for a real reason (an earlier, explicit user request) but isn't
  brief-graded, and §6.A's recommended request-shape change would drop it
  unless a future phase re-adds it via a separate, narrower request.
  **This is a real behavior change from the current app and should be
  confirmed with the user before shipping**, not silently accepted just
  because this document argues for it.
- **LocMemCache is process-local** (§6.D) — the caching win in this plan
  is real but smaller than it would be with a shared cache (Redis, etc.)
  in a multi-process deployment. Not proposing Redis here: no evidence
  yet that single-process caching is insufficient, and adding
  infrastructure without demonstrated need is exactly what §8 of the
  review instructions warns against.
- **Deferred**: adaptive checkpoint/heatmap sampling (§6.F), progressive
  responses (§6.G), heatmap rendering change (§6.H), dual-provider
  fallback resilience (a pattern all three reference repos use but that
  implies adding a second, paid/keyed provider — a bigger architectural
  decision than anything else here, not proposed).
- **Unresolved questions this document cannot answer** (§2.2): whether 3
  routes is truly mandatory or best-effort, whether a progressive
  response would ever be wanted, and what "performance" in the 40%
  grading line actually weighs (frontend smoothness vs. backend latency)
  are all genuinely ambiguous in the source brief. This document took the
  reading that favors correctness and simplicity in each case, consistent
  with the brief's own closing instruction, but flags every one of these
  as an interpretation, not a fact.

---

## 12. References

**Official documentation [SOURCE]:**
- OSRM `/route` service API reference — `overview`, `steps`,
  `annotations`, `alternatives` parameters and response codes, confirmed
  2026-10-21.
- OSRM demo server usage policy (GitHub wiki,
  `Project-OSRM/osrm-backend`) — 1 req/s limit, no-SLA disclaimer,
  confirmed 2026-10-21.
- Python `concurrent.futures` documentation — `Future.cancel()` and
  `ThreadPoolExecutor.shutdown()` behavior on already-running callables,
  confirmed 2026-10-21.

**Repository source inspected directly, not summarized from READMEs
[SOURCE]:**
- This repo: `backend/api/views.py`, `backend/integrations/routing_client.py`,
  `backend/routing/heatmap.py`, `backend/routing/checkpoints.py`,
  `backend/config/settings.py`, `frontend/src/components/MapView.tsx`,
  `frontend/src/hooks/usePost.ts`, `frontend/src/hooks/useAnalyzeRoutes.ts`,
  `frontend/src/App.tsx` — all read in full for this review, current as
  of the live repository state (uncommitted working tree, confirmed via
  `git status`).
- [MTahir27/weather-awear](https://github.com/MTahir27/weather-awear) —
  `docs/assumptions.md`.
- [malikali3419/truck-weather-routing](https://github.com/malikali3419/truck-weather-routing) —
  `backend/routing/providers/alternatives.py`, `fallback.py`, `osrm.py`,
  `ors.py`, `services/heatmap.py`, `domain/weather_series.py`.
- [swatiXO/weather-aware-truck-routing](https://github.com/swatiXO/weather-aware-truck-routing) —
  `docs/DECISIONS.md`, `backend/planner/services/routing.py`,
  `backend/planner/services/http.py`, `backend/planner/domain/spur.py`.

**Assessment brief [BRIEF]:**
- `fs-assessment-spotter.pdf` (project root), 2 pages, read in full.

**Test baseline [MEASURED]:**
- `pytest -q`: 116 passed, 0.94s, run 2026-10-21 as part of this review —
  this is the pre-change baseline, not a claim about any proposal in this
  document (nothing here has been implemented).

---

## 13. Addendum (2026-10-21) — two calibration defects found and fixed, via-search widened

This phase's design was already live in code when this addendum was
written (§8's status line above was stale — corrected). This investigation
started from a user-reported symptom: real requests were coming back with
"Routing provider returned 1 of 3" / "2 of 3" despite the via-fallback
machinery described in §6.B/C existing and running. Traced against the
real OSRM demo server, not mocks, across 7 live trips (the 6 reference-repo
pairs from §5 plus a real Sousse→Tunis request the user supplied).

**Defect 1 — `similarity.SPUR_MATCH_FRACTION = 0.05`, far too strict.**
`has_backtrack_spur`'s fixed `SPUR_WINDOW_MI=40` window spans the *entire*
route for any trip under ~80mi, so the check degenerated into "does the
first half of this route share any grid cell with the second half" —
true of almost any route through a real road network, not just a literal
dead-end. Measured live: genuine out-and-back spurs overlapped
0.595–0.955; legitimate, meaningfully-distinct detours overlapped
0.081–0.200 — both clusters exceeded the 0.05 threshold and were rejected
identically. Confirmed against `malikali3419/truck-weather-routing`'s
`backtracks()` (`routing/providers/alternatives.py:117-122`): same
nearest-point-split technique, but an absolute-shared-length threshold
that scales with route length, not a fixed-window raw-fraction — the
*technique* was sound, this repo's calibration of it was the defect.
**Fix:** `SPUR_MATCH_FRACTION` raised to `0.35` (sits with margin inside
the measured gap). Regression-guarded in `routing/tests/test_similarity.py`.

**Defect 2 — `similarity.MAX_DURATION_RATIO = 1.5`, one real rejection by
0.26%.** Live on Sousse→Tunis, a legitimate via-candidate measured
ratio 1.5039 — rejected by a hair. Raised to `1.6`, matching
malikali3419's independently-validated value, confirmed live it does not
let any previously-confirmed-bad candidate through (1.91x and 2.29x
candidates measured elsewhere still correctly rejected).

**Defect 3 (structural, not a threshold) — only one via anchor was ever
tried.** Fixing defects 1–2 still left Sousse→Tunis at 1 of 3: both via
candidates at the route's single midpoint anchor were *correctly*
rejected (one a near-duplicate of the main route, one genuinely 74%
slower) — there was no bug left to fix at that anchor. A 40-combination
live sweep of (anchor position × offset distance) on the same route found
11 genuinely valid combinations the single-anchor search never tried.
**Fix:** `VIA_ANCHOR_FRACTIONS = (0.35, 0.65)` replaces the single
midpoint anchor, offset fraction raised to match
(`VIA_OFFSET_FRACTION = 0.22`) — both values taken directly from
malikali3419's own validated round-1 choice, confirmed live against this
app's actual via-fetch/filter pipeline (not assumed from their having
picked it). Still one bounded round, still ≤2 concurrent OSRM requests at
a time (now 4 total via-requests in 2 waves instead of 2 in 1, only on
the already-rare native-alternatives-insufficient path) — not the
multi-round escalation §6.B deliberately rejected.

**Live result, all 7 trips, after all three fixes:**

| Trip | Before | After |
|---|---|---|
| Chicago→Atlanta | 3 | 3 |
| Dallas→Denver | 3 | 3 |
| LA→NYC | 2 | 3 |
| Salt Lake City→Cheyenne | 1 | 3 |
| LA→Phoenix | 1 | 3 |
| Fort Worth→Dallas | 2 | 3 |
| Sousse→Tunis | 1 | 3 |

Every accepted route in the "after" column was independently confirmed
non-spur, within the duration-ratio limit, and distinct from every other
accepted route on that trip (not just "count reached 3").

**Cost:** via-fallback latency roughly doubles on the shortfall path only
(2 waves of ≤2 concurrent requests instead of 1) — measured 2.1–4.7s for
regional trips, up to ~19s on the LA→NYC continental case (was ~9–14s).
This only runs when native alternatives are already insufficient, not on
every request.

**Tests added:** `routing/tests/test_similarity.py` (6 tests — spur
calibration, window-saturation on short routes, overlap/distinctness) and
`integrations/tests/test_routing_client.py` (3 tests — second-anchor
recovery, honest shortfall with no padding, `TARGET_ROUTES` cap never
exceeded). Full suite: 125 passed, 0.91s — still mocked/pure, no live
network in CI.

**Residual limitation, stated plainly:** these fixes are validated against
7 live trips, not exhaustively proven. A trip geometry not resembling any
of these 7 could still expose a threshold or anchor-placement gap this
review didn't sample. If a future "1 of 3"/"2 of 3" report recurs, the
diagnostic method used here — log native-alternative count, dedupe count,
and each via candidate's spur/ratio/overlap verdict — is the fastest path
to the real cause, likely faster than re-guessing at thresholds.

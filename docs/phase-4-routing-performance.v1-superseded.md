# Phase 4 — Routing performance & guaranteed route alternatives

Status: **planned, not implemented**. This document is the result of a live
audit (profiling against the real OSRM/Open-Meteo endpoints, not estimates)
plus a deliberate look at three comparable assessment builds of the same
brief, each solving the same "always 3 routes" / corridor-forecast problem
independently:

- [MTahir27/weather-awear](https://github.com/MTahir27/weather-awear) — `docs/assumptions.md`
- [malikali3419/truck-weather-routing](https://github.com/malikali3419/truck-weather-routing) — `backend/routing/providers/alternatives.py`, `fallback.py`, `osrm.py`, `ors.py`, `services/heatmap.py`
- [swatiXO/weather-aware-truck-routing](https://github.com/swatiXO/weather-aware-truck-routing) — `docs/DECISIONS.md`, `backend/planner/services/routing.py`, `domain/spur.py`, `services/http.py`

Where all three converge on the same number or technique independently,
that's called out explicitly below — it's a much stronger signal than any
one of them alone. Nothing in this file has been implemented yet — it's the
plan to execute next, split into independently shippable steps.

---

## 1. Root cause of the >10s `/analyze` latency

Profiled directly against the live providers (not synthetic):

| Route | `get_route_options` (OSRM) | `get_weather_observations` | `get_weather_series` (heatmap) | Total |
|---|---|---|---|---|
| Chicago → Atlanta (~720 mi, 2 alternatives, 61 checkpoints) | 1.32s | 0.81s | 0.95s | 3.25s |
| Los Angeles → New York (~2,794 mi, 2 alternatives, 226 checkpoints) | 3.66s – 7s+ (observed up to 17s on repeat calls) | 1.05s | 0.92s | 6.96s – 19s+ |

**Weather fetching is not the bottleneck.** Both weather calls stay under
1.1s regardless of checkpoint/grid count across every test. The entire cost
is concentrated in the single OSRM directions request, and it scales with
*route length*, not anything our code does afterward.

### Why the OSRM call itself is slow

Isolated with direct curl timing against `router.project-osrm.org` for the
LA→NYC pair:

| Request shape | Payload | Time |
|---|---|---|
| `overview=full&steps=true` (**current production request**) | 3.6 MB | 17.4s |
| `overview=simplified&steps=true` | 2.1 MB | 11.0s |
| `overview=simplified&annotations=true&steps=false` | 2.45 MB | 15.3s |
| `overview=simplified&steps=false` | 3 KB | **0.49s** |

The smoking gun: with `steps=true`, OSRM returns a full-resolution geometry
**on every individual step**, independent of the top-level `overview`
setting. For this route: 58 steps, but their geometries sum to **33,842
coordinate points** — essentially the same size as the full-resolution
overview polyline (33,784 points) that `overview=full` also sends. We parse
`_parse_osrm_route` / `_build_duration_breakpoints` in
`backend/integrations/routing_client.py` and never touch `step["geometry"]`
at all — we only read `step["distance"]`, `step["duration"]`, and (since the
sea-crossing work) `step["mode"]`. **We are paying for, transferring, and
JSON-parsing ~2MB of per-step geometry we throw away unread, on every
long-haul request.**

`annotations=true` was tested as a substitute for `steps=true` and is not a
fix — OSRM always returns annotation arrays at the full internal routing
resolution (33,783 entries here) regardless of the `overview` setting, so
it's the same firehose in a different shape, not a smaller one.

### A sharper substitute, found in malikali3419's OSRM fallback client: `annotations=duration`

Their `providers/osrm.py` requests `annotations=duration` (the single field,
not `annotations=true`'s full bundle of duration+distance+speed+weight+nodes+
datasources) and derives per-vertex cumulative time directly from it, paired
1:1 with the geometry — no `steps=true` at all. Verified this empirically,
repeated 4× each to account for the variance below:

| Request (Chicago → Atlanta, ~720mi) | Run 1 | Run 2 | Run 3 | Run 4 |
|---|---|---|---|---|
| `steps=true` (current) | 5.39s | 6.20s | 8.55s | 8.29s |
| `annotations=duration` | 3.26s | 3.65s | 3.87s | 3.78s |

Consistently ~40–55% faster than `steps=true` at this distance, across every
trial despite the jitter — this holds up as a real, repeatable win, not
noise. **It does not fix the continental-distance case**: annotations are
tied to OSRM's full internal routing-graph resolution regardless of
`overview`, confirmed by testing `annotations=duration` on the LA→NYC route
(33,783-entry duration array, same as before, 11.7s) — so it's a genuine
improvement at realistic trucking distances, not a fix for the pathological
coast-to-coast case (that needs the distance-gating in P4.2 regardless of
which fine-grained method is used below the threshold).

**Trade-off: `annotations=duration` has no `mode` field.** Ferry detection
(`has_ferry`, added in the sea-crossing work) only exists on `steps=true`'s
per-step objects — switching the default request shape to annotations means
choosing between losing ferry detection entirely or keeping `steps=true` for
that purpose specifically. See the revised P4.2 below for how these two
needs (fine ETA breakpoints vs. ferry detection) can be decoupled rather
than both riding on one expensive request.

### A caveat that changes how to read every number in this document

Running the *exact same request* 4 times in a row against OSRM's public
demo server produced times ranging from 3.26s to 8.55s — **a 2.6× spread
with nothing in our code changing between calls.** This is a free,
no-SLA, shared public server; its load varies independently of anything we
do. Two implications for this plan:

- Don't read any single absolute "Xs" measurement in this document as a
  guarantee — read relative comparisons (A is consistently faster than B
  across repeated trials) as the trustworthy signal, and absolute numbers as
  "roughly this order of magnitude, on a good day."
- Fixes framed as "removes N bytes/fields we never parse" are a solid,
  deterministic win regardless of server mood. Fixes framed as "guarantees
  under Xs" are not something this server can promise us, independent of
  our own code quality — worth setting that expectation before promising a
  specific SLA anywhere (README, Loom walkthrough, etc.).

---

## 2. What the reference projects do differently

Three independent teams solved the same brief. Where they agree with each
other despite building separately, that's a strong signal, not a
coincidence.

### 2.1 Guaranteed-3-routes: all three converge on the same shape

Every one of them hits the same wall (OSRM/ORS alternatives are unreliable
past a short distance) and solves it the same way: **fire extra via-point
requests concurrently alongside the main one, keep only genuinely distinct
results, show fewer and say so rather than pad with a duplicate.** The
specific numbers, side by side:

| | weather-awear | malikali3419 | swatiXO |
|---|---|---|---|
| Overlap threshold (reject if ≥) | 90% | 80% | 85% |
| Checked in both directions? | Yes | Yes | Yes |
| Duration ratio cap | 1.5× fastest | 1.6× fastest | (no explicit cap found) |
| Via offset | 15% of trip distance, 15–150km | escalating: 15%→22%→30%→45% | escalating: 8%→15%→30% |
| Overlap measured via | buffer/corridor check | **grid-cell hash intersection, 0.02° (~2km) cells** | **grid-cell hash intersection, 0.02° (~2km) cells** |
| Concurrency | concurrent, not detailed | `ThreadPoolExecutor(max_workers=min(3, n))` | `ThreadPoolExecutor(max_workers=2)` |
| Spur/dead-end rejection | not mentioned | yes — backtrack-share heuristic | yes — dedicated `spur.py` splice module |
| Falls back to showing fewer routes + a note | Yes | Yes (implicit via `selected[:TARGET_ROUTES]`) | Yes, explicit note string |

Two things two-of-three agree on exactly are worth treating as close to
settled rather than a judgment call: **the 0.02° grid-cell-hash method for
measuring route overlap**, and **capping the thread pool to 2–3 workers**
(both leave an explicit comment that the free routing APIs throttle bursts
— this matches what we independently found testing OSRM's own variance in
§1: hammering it with unlimited concurrent requests is asking for trouble).

**A correctness gap this surfaced that the first draft of this plan
missed entirely: via-point spurs.** A via point offset from the route
midpoint can land on a dead-end side road — the router then drives up it
and straight back, and a naive "just take whatever route comes back"
approach would accept that out-and-back detour as a legitimate third
"route." Two of three reference projects explicitly guard against this
(malikali3419 with a shared-length backtrack check, swatiXO with a
dedicated point-to-segment spur-splice module). P4.3 below now includes
this.

### 2.2 Explicit, bounded weather-API budget (weather-awear, malikali3419)

weather-awear: "A plan uses at most 200 forecast locations and a heatmap at
most 150... the shared-location grid coarsens until the trip fits."
malikali3419's `services/heatmap.py` implements exactly this as a concrete,
adoptable algorithm: a tuple of candidate grid-cell sizes from fine (0.15°)
to coarse (1.0°), tried in order, keeping the finest one that stays under a
`MAX_POINTS` cap (220 in their case). Our heatmap grid is *already* bounded,
but with a **fixed** sample count (`SAMPLES_PER_ROUTE=11` × 3 lateral
offsets, ~99 points regardless of trip length) rather than adaptive —
see the new P4.6 below. Our **checkpoint count is not bounded at all** — the
LA→NYC profile in §1 generated 226 checkpoint targets from one request with
no ceiling. Not what caused today's slowness (weather fetching stayed fast
even at 226 targets), but an unbounded dimension worth capping before it
becomes one.

### 2.3 Response caching, not just weather caching (swatiXO)

`services/http.py`'s `cache_key()` hashes the full request body and caches
the *parsed routing provider response itself* (6h TTL), not just weather
lookups. We only cache weather today — `get_route_options` hits OSRM fresh
on every single call, even for an identical repeat request (same
origin/destination submitted twice, which is a completely normal thing to
happen during iterative testing or evaluation). Given the variance measured
in §1, **this may be the single cheapest, highest-value item in this
entire phase** — see the new P4.0 below.

### 2.4 One bounded retry on transient failures, not zero and not infinite (swatiXO)

`request_json()` retries exactly once on a network error or 5xx, with a
fixed 0.5s delay, and explicitly does **not** retry 4xx ("bad input, rate
limits won't fix themselves"). This is the same retryable/non-retryable
split we already built this session (`RouteNotFoundError` vs
`RoutingProviderError`) — we just never added the retry itself. See the new
P4.7 below.

### 2.5 Heatmap: all three reference projects independently rejected a smoothed/blurred rendering

- weather-awear: "A kernel-density heatmap was tried and rejected: smoothing
  averaged isolated hazardous cells with calm neighbours until they
  disappeared."
- malikali3419: tried deck.gl's `HeatmapLayer` with MEAN aggregation, same
  failure from the other direction — "it hid low-risk areas and produced
  blocky artifacts" — switched to a `SolidPolygonLayer` of discrete colored
  grid tiles instead.
- swatiXO: same grid-tile-not-blur choice (`layers.ts`), consistent with the
  other two.

**Three independent teams building the same brief all discarded a
smoothed/interpolated heatmap for the same underlying reason.** This is no
longer "worth considering" (P4.4's original framing) — it's a converged,
battle-tested conclusion. `MapView.tsx`'s MapLibre `heatmap` layer is the
one thing in our stack still doing what all three of them tried and backed
away from.

---

## 3. Proposed work, phased by risk/effort

### P4.0 — Cache the parsed route response (new, cheapest item in this phase)

Not in the original draft — surfaced by swatiXO's `services/http.py`. We
cache weather lookups but **never cache `get_route_options` itself**, so an
identical repeat request (same origin/destination resubmitted — completely
normal during iterative testing, or an evaluator re-running the same trip
after tweaking load weight, which doesn't even change what we send to OSRM
since load isn't part of the request) pays OSRM's full, variable-latency
cost again every time. Given the 2.6× jitter measured in §1, a cache hit
turns a 3–8s wait into effectively zero.

Implementation: wrap `get_route_options` with the same
`django.core.cache.cache` already used in `weather_client.py` — no new
infra. Cache key is just `(round(origin.lat,5), round(origin.lon,5),
round(destination.lat,5), round(destination.lon,5))` — simpler than
swatiXO's full-request-body hash, since our OSRM request has no other
varying parameter (`load_lb` is accepted but unused, see the module
docstring). A TTL of an hour or two is plenty — OSRM's road network doesn't
change mid-session, and this is purely a same-session/same-evaluation-pass
optimization, not a long-lived data cache. Store the final `RouteOption`
list (already provider-agnostic), not the raw OSRM JSON — avoids coupling
the cache format to the next phase's changes to request shape.

**Do this first.** It's additive, low-risk, doesn't touch request
construction (so it's independent of P4.1/P4.2), and the data it caches
will remain valid even as the other items in this phase change *how* that
data gets fetched.

### P4.1 — Switch `overview=full` → `overview=simplified`

Change the one `params` dict in `get_route_options`. We draw the route line
and sample the heatmap corridor off `route.geometry`, neither of which needs
tens of thousands of points — `overview=simplified` applies OSRM's own
Douglas-Peucker simplification, which preserves route shape at any realistic
map zoom. Measured effect on the worst case: 3.6MB/17.4s → 2.1MB/11.0s on
that one run (remember §1's variance caveat — treat this as "meaningfully
smaller payload," not a guaranteed timing delta) for free, zero information
loss for anything we actually read.

Verification: confirm `generate_corridor_grid`'s `_sample_with_bearing`
still produces sane points against a simplified (sparser) polyline — it
interpolates along cumulative distance, so it should degrade gracefully, but
needs a live check against a long route, not just an assumption.

**Does not fully fix the problem** — `steps=true` is still the larger cost
(confirmed: `steps=false` alone drops the same request to 3KB / 0.49s).
This step alone is worth doing but P4.2 below is where the real fix lives.

### P4.2 — Decouple "fine ETA breakpoints" from "ferry detection" (revised)

The original draft treated this as one problem (strip `step.geometry`).
Testing malikali3419's `annotations=duration` approach shows these are
actually two separate needs that don't have to share one expensive request:

- **ETA breakpoints** (`_build_duration_breakpoints`) can come from
  `annotations=duration` instead of `steps=true` — confirmed ~40–55% faster
  than `steps=true` at regional distance across 4 repeated trials (§1), and
  arguably *more* accurate (one duration value per geometry vertex instead
  of per turn-by-turn step, finer resolution for the same ETA-interpolation
  purpose).
- **Ferry detection** (`has_ferry`) only exists on `steps=true`'s `mode`
  field — `annotations` has no equivalent.

Proposed shape, distance-gated the same way the original draft proposed
(threshold needs confirming against real usage, 500mi is a starting guess,
not a measured cutoff):

- **Below the threshold** (the vast majority of real trucking trips, and
  where both repo projects' own test fixtures live — none of their
  documented test routes are coast-to-coast either): request
  `overview=simplified&annotations=duration&steps=true` — yes, both; the
  combined cost at regional distance is still far below the
  continental-route floor, and we get fine breakpoints *and* ferry
  detection. Use `annotations` for `_build_duration_breakpoints` (finer),
  keep `steps` purely for `step.mode == "ferry"` detection.
- **Above the threshold**: request `overview=simplified&steps=false` only
  (0.49s, confirmed) — 2-point breakpoints (start→end, accepting the
  route-average-speed trade-off the reference projects explicitly try to
  avoid at regional distance, but unavoidable without paying OSRM's full
  continental-route cost) and **no ferry detection**, documented honestly
  as "not available for ultra-long-haul routes" rather than silently wrong.

This is a slightly larger change than the original draft's single
if/else on `steps=true` — it touches which fields `_parse_osrm_route` reads
from, not just the request params — but it's the version that actually
keeps ferry detection and fixes the main breakpoint cost, instead of a
version that has to give up one or the other.

**Dropped from this draft: the two-phase background-refinement idea
(original P4.2(b)).** The annotations/steps split above gets most of the
same benefit (fast regional trips, honest degradation for pathological
ones) without the added moving parts of a background refetch + in-place UI
update. Still worth remembering if P4.2's distance threshold turns out to
be too conservative in practice, but not proposed as part of this phase.

### P4.3 — Guaranteed 3 routes (the actual feature request), concrete algorithm

Revised with the triangulated technique from §2.1 — all three reference
projects converge closely enough that this is no longer "adapt their idea,"
it's "implement the idea two of three already validated independently":

1. **New pure function, `routing/similarity.py`** (no I/O, testable like
   the rest of `routing/`): route overlap via **grid-cell-hash footprint
   intersection** — round every polyline point to a ~0.02° (~2km) grid
   cell, take the two routes' cell sets, overlap fraction = shared cells /
   route A's total cells. Two independent implementations (malikali3419,
   swatiXO) landed on this exact cell size and method separately — prefer
   it over the original draft's vaguer "sample and check corridor width"
   idea, since this one has two working reference implementations to check
   against.
2. **Check overlap in both directions** (`overlap(a,b)` and `overlap(b,a)`)
   — unanimous across all three projects, since a short route can sit
   entirely inside a long one's footprint without the long one sitting
   inside the short one's.
3. **Reject out-and-back spurs.** A via-point offset can land on a dead-end
   side road; the router drives up and straight back, and that out-and-back
   detour must not be accepted as a real third route. Simplest viable
   version (prefer this for a first iteration over swatiXO's full
   splice-and-repair module, which would also require re-deriving
   `duration_breakpoints`/checkpoints for the spliced geometry — more
   surface area than this phase needs): malikali3419's backtrack-share
   check — walk from the via point backward and forward along the route,
   measure how much of the path after the via point runs back along cells
   already visited before it; reject the candidate if that shared length
   exceeds a small absolute floor or a small share of the route's total
   distance.
4. **Escalating via-point rounds, not a fixed batch every time.** Start
   with one pair of via points (±15% lateral offset at the route midpoint,
   same bound as weather-awear: clamp the absolute offset between 15km and
   150km so it scales sanely for both short and very long trips). Only
   widen to a second round (different fraction/offset) if still short of 3
   distinct routes after the first round — avoids firing extra OSRM
   requests on trips that only needed one more route.
5. **Bounded concurrency, not unlimited.** `ThreadPoolExecutor` capped at
   2–3 workers (both reference projects land in this range, both cite OSRM
   throttling bursts — and we independently confirmed in §1 that OSRM's
   public demo is already under real, variable load without us adding
   concurrent pressure to it).
6. **Duration ratio cap**: reject a candidate whose duration exceeds 1.5×
   the fastest already-chosen route's (weather-awear's number; malikali3419
   used 1.6×, close enough that either is defensible — start at 1.5×).
7. **Overlap threshold**: reject at ≥85% overlap (midpoint of the observed
   80–90% range across all three projects).
8. **Never pad with a near-duplicate.** If every fallback still overlaps
   too much, show what's genuinely different and say so — the existing
   `warnings` list (already wired end-to-end via
   `RouteAnalysisResponseSerializer`) is exactly the right mechanism, no
   new field needed.

This remains the most involved item in this phase — new domain module, new
concurrency in the integration layer, a spur-rejection heuristic, and three
judgment-call thresholds (85% overlap, 1.5× duration, spur backtrack
share) that should be tuned against a handful of real route pairs (a short
trip known to have few alternatives, a medium one, something with an actual
dead-end-prone via point) before calling it done — not just unit-tested
against synthetic polylines, consistent with how every other phase in this
project was verified.

### P4.4 — Heatmap: discrete cells instead of KDE blur (correctness, not performance)

No longer "one project's opinion" — **three independent teams building the
same brief all discarded a smoothed/blurred heatmap for the same underlying
reason** (§2.5): an isolated hazardous cell gets averaged away by its calm
neighbors, whether that's MapLibre's `heatmap` KDE or deck.gl's
`HeatmapLayer` with MEAN aggregation. `MapView.tsx` is currently the one
thing in our stack still doing what all three of them tried and backed away
from. Switch to a discrete per-point layer — a `circle` layer sized/colored
directly from `risk_by_hour[hour]` with no interpolation between points, so
one severe/no-travel grid cell can never visually disappear. This changes
the heatmap's visual language (less "heat cloud," more a grid of discrete
readings, matching what all three reference projects converged on) — worth
a quick before/after screenshot comparison before committing, since it's a
visible design change, not just a bugfix.

### P4.5 — Loading-state copy for genuinely long waits

Even after P4.0–P4.2, a true coast-to-coast request will still take several
seconds (OSRM itself is the floor on an uncached request, not our code).
The existing skeleton loader shows the same generic state regardless of
expected wait length. Cheap fix: once origin/destination are picked,
estimate straight-line distance client-side and adjust the pending message
(e.g. "Calculating a cross-country route — this can take a few seconds for
very long trips") above some distance threshold. No backend change needed.

### P4.6 — Adaptive heatmap grid density (new)

Our `generate_corridor_grid` uses a **fixed** `SAMPLES_PER_ROUTE=11` × 3
lateral offsets regardless of trip length — too sparse for a short regional
trip, needlessly dense (relative to what a short trip needs) for nothing in
particular, and not adaptive the way weather-awear's A25 ("0.5° cells by
default; 0.75–1.5° when a corridor would exceed 250 cells") and
malikali3419's `services/heatmap.py` both are. malikali3419's version is the
more directly adoptable algorithm: a tuple of candidate grid-cell sizes,
tried from finest to coarsest, keeping the finest one whose total point
count stays under a cap (`MAX_POINTS`). Proposed for us: same pattern,
tune `MAX_POINTS` against our own existing heatmap response size/serialization
cost rather than copying their 220 verbatim. Independent of P4.0–P4.3 and
P4.4 — can land in any order.

### P4.7 — One bounded retry on transient provider failures (new)

`get_route_options` and the weather client currently have zero retry logic
— a single transient network blip or 5xx is indistinguishable from a
persistent outage today. swatiXO's pattern (one retry, fixed ~0.5s delay,
only for network errors and 5xx — never for 4xx, since "bad input" won't
fix itself on a second try) slots directly onto the
`RouteNotFoundError`/`RoutingProviderError` split already built this
session: retry only around the `RoutingProviderError`-raising paths (never
`RouteNotFoundError`, which is a deterministic answer, not a flake).
Small, mechanical, independent of everything else in this phase.

---

## 4. Explicitly out of scope for this phase

- Switching routing providers to avoid OSRM's long-route cost (OSRM is
  already a deliberate choice for being free/keyless; the cost is inherent
  to long-haul routing generally, not a provider defect to route around).
  Note: all three reference projects that primarily use a paid/keyed
  provider (ORS) still keep OSRM or a via-point fallback for resilience —
  that's a *dual-provider* pattern, a bigger architectural change than
  anything else in this phase, and not something to take on as a drive-by
  here.
- HGV/weight-aware routing (pre-existing, documented trade-off of using
  OSRM's public demo — unrelated to this phase).
- Per-checkpoint land/sea tagging inside the corridor forecast (documented
  limitation from the ferry-detection work; still route-level only).
- Full dark mode.
- Server cold-start / "waking up" UX (swatiXO's `useServerWake.ts` +
  health-ping pattern) — only relevant once a hosting target is picked in
  the deployment phase; premature to build for now since no deployment
  decision has been made yet.
- Per-IP rate limiting / request throttling on our own API (swatiXO's X4,
  added specifically to protect shared free-tier quotas during evaluator
  load). Worth a one-line flag for the eventual deployment phase — Nominatim
  (1 req/sec policy) and Open-Meteo's free tier are both things a burst of
  concurrent evaluator traffic could realistically stress — but it's a
  deployment/operability concern, not a routing-performance one, so kept
  out of P4's scope.

---

## 5. Suggested order & sign-off points

1. **P4.0** (cache parsed route responses) — do first: cheapest, safest,
   purely additive, and its value compounds with every later change since
   a cache hit skips whatever request shape P4.1/P4.2 end up using.
2. **P4.1** (overview=simplified) — small, safe, verify live on a long
   route before/after.
3. **P4.2** (annotations=duration below a distance threshold, steps-only
   ferry detection, honest degradation above it) — needs the distance
   threshold (proposed: 500mi) confirmed against real usage, not guessed;
   this is the one item in this phase whose scope grew during research
   (was "strip step.geometry," is now "use a cheaper field for breakpoints
   and keep steps narrowly for ferry detection only") — re-confirm this
   shape is still wanted before starting, since it touches more of
   `_parse_osrm_route` than the original one-line fix.
4. **P4.3** (guaranteed 3 routes) — the actual feature ask, largest and
   most judgment-call-heavy piece; needs the 85%/1.5×/spur-backtrack
   thresholds sanity-checked against a few real route pairs before calling
   it done.
5. **P4.7** (bounded retry) — small, mechanical, can land independently
   alongside P4.0–P4.3.
6. **P4.5** (loading copy) — trivial, can land independently/anytime.
7. **P4.6** (adaptive heatmap grid) and **P4.4** (heatmap rendering) —
   both touch the heatmap but are otherwise independent of each other and
   of 1–5; P4.4 is a visible design change and should get a before/after
   look before committing.

Each item should get its own live verification pass (same style used
throughout this project: hit the real OSRM/Open-Meteo endpoints and a real
browser, not just unit tests) before being called done — and given §1's
variance finding, "live verification" for anything latency-related now
means a handful of repeated trials, not a single run, or a transient good
(or bad) moment on OSRM's side will be mistaken for the effect of the code
change.

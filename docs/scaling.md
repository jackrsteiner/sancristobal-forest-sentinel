# AOI sizing, free-tier limits, and efficiency roadmap

The deployment docs say to "keep AOIs small". This document makes that concrete:
how pipeline cost actually scales with AOI size, what the $0 infrastructure
supports today, what it could support with specific efficiency improvements, and
practical guidance on AOI geometry. It reflects the pipeline as of Slice 2
(synchronous per-observation exports; see [`architecture.md`](architecture.md)).

---

## 1. The cost model

Four facts drive everything:

1. **Observations are per HLS granule** — one per ~110 × 110 km MGRS tile per
   satellite pass. Landsat 8/9 (HLSL30, ~8-day combined revisit) plus
   Sentinel-2 A/B (HLSS30, ~5-day) yield **~10 scenes per tile per month**
   (~0.65/tile/day).
2. **Each processed observation costs 4 Earth Engine batch exports** (NBR,
   NDVI, ΔNBR, ΔNDVI), each submitted, polled to completion, and downloaded
   **sequentially** (`storage.py`). Per-task EE latency is typically 1–5
   minutes regardless of size, so ~15–20 minutes of wall clock per scene.
3. **Every run re-exports the whole trailing window.** `run_pipeline`
   reprocesses every observation in `[since, until)`, and index exports always
   run (change products are skipped only once "frozen" into events). With
   `WINDOW_DAYS=30` and the daily timer, a one-tile AOI costs ~10 obs × 4
   exports ≈ **40 exports ≈ 1.5–3.5 h per day** — most of it redundant.
4. **Exports use the whole AOI as their region.** `indices.py` / `change.py`
   pass the full AOI geometry to every per-scene export, so per-task cost
   creeps up with total AOI extent even though only one tile has data.
   Candidate extraction (`reduceToVectors` + `getInfo`) likewise runs over the
   whole AOI per change raster.

The VM itself is nearly idle throughout — all compute happens inside Earth
Engine; the VM submits, polls every 5 s, and downloads. The e2-micro's 1 GB RAM
is not a constraint.

### Disk

Exported COGs cost disk in proportion to **valid-pixel area** (cloud-masked and
outside-scene pixels are nodata and compress away):

- 4 products × 4 bytes × ~1,100 px/km² × ~10 coverages/month ≈
  **~0.1–0.2 MB per km² per month** of monitored area.
- Of the 30 GB standard disk, call ~20 GB usable for `/data/cogs` (the rest is
  OS, Docker, Postgres).

| Retention | Supported monitored area | ≈ fully-covered tiles |
|-----------|--------------------------|------------------------|
| 90 days   | ~35–65k km²              | ~3–5                   |
| 30 days   | ~100–200k km²            | ~10–15                 |

A sparse MultiPolygon only pays for its valid pixels: tight forest polygons
covering 30 % of 10 spanned tiles cost ~3 tiles of disk.

## 2. What the defaults support today

With `WINDOW_DAYS=30`, the daily timer, and the 6 h service timeout
(`TimeoutStartSec=6h` in `scripts/systemd/forest-sentinel-pipeline.service`),
the binding constraint is **run time from redundant re-exports**, and the
comfortable envelope is an AOI within **one, maybe two, HLS tiles — order
5,000–10,000 km²**. The cheapest immediate lever is shrinking `WINDOW_DAYS`
(runtime scales linearly with it).

The 6 h budget is one line of systemd configuration, not a platform limit: it
can be raised toward ~22 h (overlapping runs already serialize on a per-AOI
Postgres advisory lock), or the timer can fire several times a day (discovery
and exports are idempotent). The true ceiling at daily cadence is ~24 h of
sequential polling — times whatever export concurrency is added (see below).

## 3. Efficiency roadmap (ordered by value)

1. **Skip re-exporting rasters that already exist.**
   `compute_indices_for_observation` exports unconditionally; with an
   exists-check (row + file present under the same methodology) the daily cost
   collapses to *new scenes only* (~0.65/tile/day). Biggest single win; pure
   code change.
2. **Clip export/vectorize regions to scene footprint ∩ AOI** instead of the
   whole AOI geometry. Removes the O(scenes × AOI area) term in both exports
   and candidate extraction; required for multi-tile AOIs to scale.
3. **Submit EE exports concurrently.** The loop is strictly sequential today;
   EE runs a few batch tasks in parallel even for noncommercial accounts, so
   submitting a scene's four products (or several scenes) together and polling
   as a group is a ~3–5× wall-clock multiplier with zero extra infrastructure.
4. **Tune the run budget** — raise `TimeoutStartSec`, and/or fire the timer
   more than once a day. Configuration only.
5. **Tune retention.** The suggested COG prune (e.g.
   `find /data/cogs -mtime +90 -delete`, `DEPLOYMENT.md` §8) is the disk knob:
   90 → 30 days roughly triples supported area.
6. **Multi-AOI scheduling.** The data model, pipeline locking, and dashboard
   are already fully multi-AOI; only the wrapper is single-AOI
   (`run_pipeline.sh` reads one `AOI_PATH`). Looping over `aois/*.geojson` is a
   ~5-line change; all AOIs share the daily run budget.
7. **Leaving $0 deliberately.** Extra `pd-standard` disk is ~$0.04/GB-month
   (+100 GB ≈ $4/month ≈ 3× the envelope). The architecture's anticipated end
   state is GCS-as-canonical raster storage, which removes the disk ceiling
   entirely.

### Envelope after items 1–3

- **Compute path:** ~20 new scenes per 6 h sequential (≈ 30 tiles of
  coverage); ~70 scenes at ~22 h (≈ 100 tiles); several times that with
  concurrency. Runtime stops being the binder.
- **Disk becomes the limit:** with retention tuning, realistically **~5–15
  tiles / several tens of thousands of km² of monitored area** on strictly
  free infrastructure.
- At true country scale, two further frictions appear: sustained daily batch
  volume against Earth Engine's noncommercial terms, and
  `reduceToVectors().getInfo()` pulling all candidate polygons for a region
  into memory at once.

## 4. AOI geometry guidance

**Prefer reasonably-fitted (Multi)Polygons over country/landmass or large
administrative boundaries**, for three reasons:

- **Cost scales with polygon area, not forest area** — non-forest pixels pay
  full price in EE compute, exports, and disk.
- **There is no forest mask yet** (context layers are Slice 6), so ΔNBR flags
  *any* vegetation loss: crop harvests, grassland senescence, wetland
  drawdown. A polygon with a large agricultural share fills the events table
  and dashboard with non-forest noise, and the manual-review workflow
  (Slice 3) isn't built yet.
- **Vertex count matters in EE.** Full-resolution coastlines/admin boundaries
  ship thousands of vertices into every `filterBounds`, export region, and
  `reduceToVectors` call. If you start from admin shapes, simplify
  aggressively first (~500 m–1 km tolerance).

A good middle ground: dissolve a coarse forest-cover envelope (or just the
forested districts) into one **simplified MultiPolygon** — the loader accepts
MultiPolygons, so disjoint forest blocks can be a single AOI. For a small,
mostly-forested province, the admin boundary is genuinely fine.

## 5. Changing an AOI on a running instance

Mechanically cheap, with one rule: **stored geometry is pinned to the AOI
name** (`aoi.get_or_create_aoi`). Editing the GeoJSON but keeping
`properties.name` logs a warning and keeps monitoring the old footprint; to
change geometry, change the name — a new AOI row is created and the pipeline
backfills it fresh. Consequences to plan around:

- The new AOI starts cold: indices first, then ΔNBR/ΔNDVI as the trailing
  baseline ramps toward its 5-observation median.
- Event history does not carry over, even where footprints overlap.
- The old AOI stops updating but remains in the database/dashboard, and its
  COGs stay under `/data/cogs/<id>-<name>/` until pruned or removed by hand —
  there is no per-AOI teardown tooling yet (a `forest-sentinel aoi delete`
  command is a natural future bead).

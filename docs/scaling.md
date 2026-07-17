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
   NDVI, ΔNBR, ΔNDVI), submitted through `storage.py`. Per-task latency is
   **tier-dependent queue wait**, not compute: on the default noncommercial
   **Community tier we observed ~40 minutes per export** in production
   (July 2026), far above the 1–5 minutes typical of higher tiers. Exports for
   a scene are submitted **as one batch** so their queue waits overlap
   (bounded by `FOREST_SENTINEL_MAX_CONCURRENT_EXPORTS`, default 4, and by the
   tier's concurrent-task limit).
3. **Only new work is exported.** An index/change raster whose catalog row and
   COG file already exist is **reused** (#77): a re-run over a processed window
   submits zero exports, and runs **checkpoint per observation chunk**, so a
   run killed by the systemd timeout resumes where it left off at the next
   timer firing. Daily steady-state cost is therefore the ~0.65 new
   scenes/tile/day, not the window size.
4. **Per-task cost is bounded by one granule** (#78): every export, the QA
   valid-fraction reduce, and candidate extraction run over **scene footprint
   ∩ AOI**, not the whole AOI geometry — per-scene cost no longer grows with
   total AOI extent (a whole-country AOI previously exceeded Earth Engine's
   per-export pixel limit outright). The recorded `valid_pixel_fraction`
   accordingly means "valid within the scene's AOI coverage".

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

With reuse + checkpointing shipped, steady-state daily cost tracks new scenes
only; the first backfill over a long window is the expensive run, and it is
governed by the Earth Engine tier and the run budget below. The comfortable
envelope remains an AOI within a few HLS tiles, with disk as the long-run
binder (§1).

### Earth Engine noncommercial tiers — pick Contributor

Since April 2026 noncommercial EE projects carry a monthly compute quota and a
tier-dependent concurrent-batch-task limit:

| Tier | Monthly quota | Cost | Notes |
|------|--------------|------|-------|
| **Community** (default) | 150 EECU-hours | free | low batch concurrency; the ~40 min/export queue waits were observed here |
| **Contributor** | 1,000 EECU-hours | free — requires a billing account attached (instances already have one) | higher batch concurrency; the tier `docs/architecture.md` assumes |

Exhausting the quota doesn't stop the project — it enters **restricted mode**
(reduced concurrency/throughput) until the month resets. **Switch each
instance's project to the Contributor tier in the Cloud console** (self-service,
Earth Engine settings) as part of setup; it is free and directly raises both
the quota and the concurrency that `FOREST_SENTINEL_MAX_CONCURRENT_EXPORTS`
can exploit.

### Run budget and the timer

One run's systemd budget is `PIPELINE_TIMEOUT` in `config/instance.env`
(default `20h`, templated into `forest-sentinel-pipeline.service` by
`vm_setup.sh`). Because runs checkpoint and resume, hitting the timeout during
a large first backfill is **safe**: the next 03:00 UTC firing picks up where
the killed run stopped, so a multi-day backfill chips away automatically. No
timer babysitting is needed while a long run executes — systemd merges a timer
firing for an already-running oneshot unit into the running job (no second
process), and the per-AOI advisory lock backstops manual runs.

## 3. Efficiency roadmap (ordered by value)

1. **Skip re-exporting rasters that already exist** *(✅ shipped, with
   checkpointed/resumable runs — #77)*. An exists-check (row + file present
   under the same methodology) collapses the daily cost to *new scenes only*
   (~0.65/tile/day), and per-chunk commits let a killed run resume.
2. **Clip export/vectorize regions to scene footprint ∩ AOI** *(✅ shipped —
   #78)*. Removes the O(scenes × AOI area) term in exports, QA reduction, and
   candidate extraction; the enabler for multi-tile AOIs (each falls back to
   the whole-AOI region only if a scene footprint is unavailable).
3. **Submit EE exports concurrently** *(✅ shipped — #79)*. Exports are
   submitted in batches and polled as a group, bounded by
   `FOREST_SENTINEL_MAX_CONCURRENT_EXPORTS` (default 4) — a multi-×
   wall-clock win whose ceiling is the EE tier's concurrent-task limit (§2).
4. **Tune the run budget** *(✅ shipped as configuration)* —
   `PIPELINE_TIMEOUT` in `config/instance.env` (default `20h`); timeouts are
   safe because runs resume.
5. **Tune retention.** The suggested COG prune (e.g.
   `find /data/cogs -mtime +90 -delete`, `DEPLOYMENT.md` §8) is the disk knob:
   90 → 30 days roughly triples supported area.
6. **Multi-AOI scheduling** *(✅ shipped — #81)*. `run_pipeline.sh` runs
   `AOI_PATH` plus every `config/aois/*.geojson`, sequentially (one CLI invocation —
   and one advisory lock — per AOI; a failure doesn't stop the others). AOIs
   can also be uploaded from the dashboard. All AOIs share the daily run
   budget (`PIPELINE_TIMEOUT`).
7. **Leaving $0 deliberately.** Extra `pd-standard` disk is ~$0.04/GB-month
   (+100 GB ≈ $4/month ≈ 3× the envelope). The architecture's anticipated end
   state is GCS-as-canonical raster storage, which removes the disk ceiling
   entirely.

### Envelope after items 1–4

- **Compute path:** throughput = concurrency × the tier's per-task queue rate,
  so it scales with the EE tier (§2) and
  `FOREST_SENTINEL_MAX_CONCURRENT_EXPORTS` rather than with wall-clock alone;
  steady-state daily load (~0.65 scenes/tile/day) is small even at Community
  pace, and per-task cost is granule-bounded (#78) rather than AOI-bounded.
  Runtime stops being the binder once the first backfill lands.
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
  COGs stay under `/data/cogs/<id>-<name>/` — retire it with
  `forest-sentinel aoi delete <name> --yes` (#83), which removes its rows and
  COG directory (remove its committed GeoJSON from the repo too, or the next
  run re-creates it).

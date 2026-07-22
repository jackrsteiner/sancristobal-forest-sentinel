# Configuration inventory & audit

This document inventories every configuration value the project exposes, groups each into one
of four categories, and audits each against six questions:

| Column | Question |
| --- | --- |
| **Versions now?** | Does changing this value currently mint a new methodology version? (The version is a content-addressed hash of the parameters dict assembled in `cli.py:498-510` for optical and `cli.py:539-549` for radar; see `methodology.py:79-82`.) |
| **Should?** | *Should* changing it mint a new methodology version? |
| **Live-safe?** | Does the project as written support changing the value on a live instance that already has observations and events recorded? |
| **New EE?** | Does changing the value force new Earth Engine requests? |
| **Redundant?** | Are those EE requests actually redundant — avoidable with a better data model, a different retention strategy, or pipeline-flow changes? |

Scope: runtime and deploy configuration. Pure dev/build/CI settings (`pyproject.toml` lint and
coverage config, `Dockerfile`, devcontainer, CI workflow internals) are out of scope.

Bare `*.py` paths below are relative to `src/forest_sentinel/`; line numbers reference the
tree as of this audit and will drift.

## Categories

1. **Instance** — identity and plumbing: which cloud project, VM, bucket, database, and
   directories the deployment uses. Set at provision time; changing one means re-deploying or
   re-pointing infrastructure. Never affects what a detection means.
2. **Pipeline tuning** — throughput, cost, and robustness levers. Freely changeable at any
   time; never affects results, only how fast/reliably they are produced.
3. **Methodology** — anything that changes what a detection *means*: which data, which
   algorithm, which thresholds. Changes here must (and do) mint a new methodology version and
   start a new provenance lineage.
4. **Data lifecycle & interpretation** — scan scope, retention, and post-detection
   interpretation of results (event lifecycle, confidence, context). Deliberately *not*
   methodology inputs: the code labels these "lifecycle config" (`cli.py:583-594`).

`WINDOW_DAYS` and `COG_RETENTION_DAYS` — the two values that prompted this audit — land in
category 4: they control *which* dates are scanned and *how long* derived rasters are kept on
disk, not what a detection means. The database rows (the reproduction recipe) are never
deleted, so neither value affects reproducibility.

---

## 1. Instance config

| Config | Where set | Default | Versions now? | Should? | Live-safe? | New EE? | Redundant? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `PROJECT_ID` | `config/instance.env` | *(required)* | No | No | No — re-provision | No | — |
| `REGION` / `ZONE` / `INSTANCE_NAME` | `config/instance.env` | `us-west1` / `us-west1-a` / `forest-sentinel-vm` | No | No | No — re-provision | No | — |
| `FOREST_SENTINEL_GEE_PROJECT` / `--gee-project` | `earthengine.py:22`, `cli.py:152` | none | No | No | **Yes** | No | — |
| `STAGING_BUCKET` / `FOREST_SENTINEL_GCS_STAGING_BUCKET` | `config/instance.env`, `storage.py:25` | `${PROJECT_ID}-ofs-staging` | No | No | **Yes** | No | — |
| `FOREST_SENTINEL_DATABASE_URL` | `db.py:12-16` | local postgres | No | No | No — see note | Indirectly | Yes — see note |
| `FOREST_SENTINEL_COG_ROOT` | `storage.py:24-26` | `data/cogs/` (`/data/cogs` on VM) | No | No | Partially — see note | Indirectly | Yes — see note |
| `FOREST_SENTINEL_AOIS_DIR` | `aoi.py:29-30` | `config/aois` | No | No | **Yes** | No | — |
| `FOREST_SENTINEL_CONTEXT_DIR` | `context.py:43-44` | `config/context` | No | No | **Yes** | No | — |
| AOI geometry files | `config/aoi.geojson`, `config/aois/*.geojson` | sample AOI | No | Debatable — see note | Partially — see note | Yes (new footprint) | No |
| Context layer files | `config/context/<kind>--<name>.geojson` | none | No | No | **Yes** (idempotent replace) | No | — |
| `APP_IMAGE` | `config/instance.env` | blank (run from source) | No | No | Yes — deploy mechanics | No | — |
| `DASHBOARD_PORT` / `OPEN_DASHBOARD` | `config/instance.env` | `8000` / `0` | No | No | Yes — re-run `vm_setup.sh` | No | — |
| Dashboard write toggles (`FOREST_SENTINEL_AOI_UPLOADS`, `_PIPELINE_TRIGGER`, `_REVIEWS`, `_CONTEXT_UPLOADS`) | `dashboard/app.py:73-86` | `1` (enabled) | No | No | **Yes** (re-read per request) | No | — |

**Notes**

- **`FOREST_SENTINEL_GEE_PROJECT`** selects the Cloud project Earth Engine bills quota
  against. Results are identical regardless of project; safe to swap live.
- **`FOREST_SENTINEL_DATABASE_URL`** — the database *is* the catalog and the reproduction
  recipe. Pointing a live instance at a fresh database orphans all history: every reuse check
  (`indices.py:191`, `change.py:149`) misses, and the next run re-exports the entire window
  from EE even though identical COGs sit on disk. Those exports are fully redundant — the fix
  is migrating the database, not re-computing. Treat this value as immutable per instance.
- **`FOREST_SENTINEL_COG_ROOT`** — same footgun in file form. Raster reuse requires both the
  DB row *and* `Path(cog_path).exists()`. `cog_path` values recorded in the DB are anchored
  to the root at export time, so changing the root without moving files silently fails every
  exists-check and re-exports the whole window (all redundant; avoidable by moving the files
  or updating paths). See Finding 5.
- **AOI geometry files** — the AOI is identified by name; its geometry is *not* a methodology
  input and is not versioned. Editing an existing AOI's polygon silently changes discovery
  scope (`filterBounds`) and export regions while old observations, candidates, and events
  under the old footprint persist unchanged. The new EE requests are legitimate (new
  footprint = new data), but the audit trail has a gap. See Finding 8.

## 2. Pipeline tuning

| Config | Where set | Default | Versions now? | Should? | Live-safe? | New EE? | Redundant? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `FOREST_SENTINEL_MAX_CONCURRENT_EXPORTS` | `cli.py:77-78` | `4` | No | No | **Yes** | No | — |
| `PIPELINE_TIMEOUT` | `config/instance.env` → systemd `TimeoutStartSec` | `20h` | No | No | Yes — re-run `vm_setup.sh` | No | — |
| Export poll timeout | `storage.py:29` (`DEFAULT_EXPORT_TIMEOUT_SECONDS`), constant | `3600.0` s | No | No | Yes (code edit) | No | — |
| Exports per observation | `pipeline.py:72` (`_EXPORTS_PER_OBSERVATION`), constant | `2` | No | No | n/a — mirrors reality (NBR + NDVI) | No | — |
| Run schedule | `scripts/systemd/forest-sentinel-pipeline.timer` | daily 03:00 UTC | No | No | **Yes** | No | — |
| Prune schedule | `scripts/systemd/forest-sentinel-prune.timer` | daily 02:30 UTC | No | No | **Yes** | No | — |

**Notes**

- The user's instinct is right: `max_concurrent_exports` looks instance-ish but is pure
  tuning. The code excludes it from methodology parameters explicitly (`cli.py:582`), it only
  sets the batch chunk size (`pipeline.py:247`), and a mid-flight change simply applies on the
  next run. Interrupted runs resume from per-chunk checkpoints (`pipeline.py:277,340`)
  reusing already-persisted artifacts, so tuning changes never trigger re-export.

## 3. Methodology config

Every value in this table is a key in the content-addressed parameters dict, so **Versions
now? = Yes** for all of them, and that is correct — each changes what a detection means.
At the time of the original audit, raster reuse was keyed on the *whole* methodology
version, so detection-only changes forced a fully redundant re-export; Findings 1–4 are now
implemented (raster lineage split, migration `0020`, plus local COG extraction), and the
table below reflects the post-split behavior. Only `baseline_window` still re-exports.

| Config | Where set | Default | Live-safe? | New EE? | Redundant? | Source / reference |
| --- | --- | --- | --- | --- | --- | --- |
| `--threshold` / `THRESHOLD` → `delta_nbr_threshold` | `cli.py:150`, `candidates.py:29` | `-0.25` ΔNBR | Yes¹ | No — rasters reused (lineage split); candidates re-extract locally² | Resolved² | ΔNBR disturbance thresholding [4], [5] |
| `--min-area` / `MIN_AREA` → `min_area_m2` | `cli.py:151`, `candidates.py:30` | `4500` m² (≈0.45 ha) | Yes¹ | No — rasters reused (lineage split); candidates re-extract locally² | Resolved² | Project-defined (≈5 HLS pixels; adjacent to FAO's 0.5 ha forest definition [13]) |
| `--baseline-window` / `BASELINE_WINDOW` | `cli.py:149`, `change.py:32` | `5` observations | Yes¹ | Yes — full window re-export | **Partially**³ (index-reuse refinement still future work) | Project-defined trailing-median baseline; time-series precedent in CCDC [12] |
| `FOREST_SENTINEL_FOREST_MASK` (`hansen`\|`worldcover`\|`none`) | `forestmask.py:33,40` | `hansen` | Yes¹ | No — rasters reused (lineage split); candidates re-extract locally² | Resolved² | Hansen GFC [6]; ESA WorldCover [7] |
| `FOREST_SENTINEL_FOREST_MASK_ASSET` | `forestmask.py:34,44,46` | `UMD/hansen/global_forest_change_2023_v1_11` / `ESA/WorldCover/v200` | Yes¹ | No — rasters reused (lineage split); candidates re-extract locally² | Resolved² | EE Data Catalog [6], [7] |
| `FOREST_SENTINEL_FOREST_MASK_CANOPY_PCT` | `forestmask.py:35,45` | `30.0` % | Yes¹ | No — rasters reused (lineage split); candidates re-extract locally² | Resolved² | ≥30 % canopy convention from Hansen-based studies [6]; FAO definition context [13] |
| `FOREST_SENTINEL_RADAR` (enable) | `cli.py:73,530` | `0` (off) | Yes — adds a lineage | Yes — new S1 work only | No — genuinely new data | Sentinel-1 GRD [8]; SAR disturbance alerting [9] |
| `FOREST_SENTINEL_RADAR_THRESHOLD` → `delta_vv_db_threshold` | `cli.py:74`, `radar.py:42` | `-3.0` dB | Yes¹ | No — rasters reused (lineage split); candidates re-extract² | Resolved² | Project-chosen magnitude; VV-backscatter-drop approach per RADD [9] |
| `scale_m` | constant `indices.py:30` | `30` m | Yes¹ (code edit) | Yes | No — raster content changes | HLS native 30 m grid [1], [2] |
| `masked_categories` | constant `qa.py:22` | cloud, cloud_shadow, snow_ice, high_aerosol | Yes¹ (code edit) | Yes | No — index content changes | HLS Fmask QA band [2]; Fmask algorithm [3] |
| HLS collections | constant `hls.py:28-31` | `NASA/HLS/HLSL30/v002`, `NASA/HLS/HLSS30/v002` | Yes¹ (code edit) | Yes | No — source data changes | HLS v2.0 [1], [2] |
| S1 collection / mode / band / orbit policy | constants `sentinel1.py:34-38`, `cli.py:544-545` | `COPERNICUS/S1_GRD`, IW, VV, `same_direction` | Yes¹ (code edit) | Yes | No — source data changes | Sentinel-1 GRD & EE preprocessing [8]; orbit-consistency rationale [9] |
| `EE_SCRIPT_VERSION` / `RADAR_SCRIPT_VERSION` | constants `cli.py:68,72` | `slice1-optical-change-v1` / `slice5-radar-change-v1` | Yes¹ (code edit) | Yes | Depends⁴ | Project provenance pin |
| `--methodology-name` / `--methodology-version` | `cli.py:139-148` | `optical-change` / content-addressed | Yes — selects/pins a lineage | Only if the lineage is new | No | Project provenance model (`methodology.py`) |

**Footnotes**

1. "Live-safe" for methodology inputs means: the system handles the change coherently, not
   that it is free. A changed parameter set mints a new version and starts a **parallel
   lineage**; old artifacts are never rewritten (change rasters freeze once a candidate is
   tracked into an event — `candidates.py:69-78`), and events only extend within one
   methodology version (`architecture.md:435-436`). Two operational costs follow:
   (a) a full re-export of the active window under the new version, and (b) **incident
   continuity breaks** — existing events stop growing and eventually auto-resolve, while the
   new lineage starts fresh events. Reverting the value re-matches the old version row and
   its artifacts become reusable again (`methodology.py:42-76`). See Finding 7.
2. Threshold, min-area, and forest mask are applied only at candidate extraction
   (`earthengine.py:251-269`, `candidates.py:93-113`). The exported index and change COGs
   contain raw signed ΔNBR/ΔVV-dB, unmasked and unthresholded (`change.py:169`,
   `radar.py:117`) — their content is byte-identical across any value of these three knobs.
   The full re-export happens only because raster reuse is keyed on
   `methodology_version_id`. See Finding 1.
3. `baseline_window` genuinely changes ΔNBR/ΔVV content, so re-exporting **change** rasters
   is legitimate — but per-scene **index** rasters (NBR/NDVI) don't depend on it, and they
   are re-exported anyway. See Findings 1 and 3.
4. The script-version pin is deliberately coarse: bumping it for a code change that doesn't
   alter raster math (e.g. a vectorization fix) still re-exports every raster. A finer-
   grained pin per stage would avoid this; accepted cost today for a simple provenance rule.

## 4. Data lifecycle & interpretation config

| Config | Where set | Default | Versions now? | Should? | Live-safe? | New EE? | Redundant? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `WINDOW_DAYS` / `--since` / `--until` | `scripts/run_pipeline.sh`, `cli.py:127-138` | `30` days | No | No — scan scope, not semantics | **Yes** | Enlarging: yes, for newly covered dates | No — genuinely new data |
| `COG_RETENTION_DAYS` | `retention.py:28`, `config/instance.env` | `90` (floored at `WINDOW_DAYS`+14, `retention.py:35-47`) | No | No | **Yes** | Not directly; pruned COGs cost one `cogs reproduce` export if needed again | By design — deliberate storage↔compute trade |
| `RESOLVED_AFTER_DAYS` | `cli.py:585-587`, `events.py:51` | `90` days | No | Defensible as-is — see note | Mostly — see note | No | — |
| `CLEAR_FRACTION_FLOOR` | constant `events.py:52` | `0.5` | No | Same as `RESOLVED_AFTER_DAYS` | Yes (code edit) | No | — |
| `CONTEXT_BUFFER_M` | `context.py:48-49` | `5000` m | No | No — presentation, and relations are replaced per run | **Yes** | No | — |
| Confidence tunables (`WEIGHTS`, cutoffs, horizons, stability subscores) | constants `confidence.py` | magnitude .25 / persistence .20 / coverage .10 / currency .10 / agreement .15 / stability .20 (#168; trajectory-fed, local COGs, zero EE); medium ≥ .4, high ≥ .65 | No — separate content-addressed `rule_version` (`fused-v3+<hash>`) | Yes, via `rule_version` | Yes — assessments are append-only; offline re-score via `forest-sentinel assess` / dashboard Re-assess | No | — |

**Notes**

- **`WINDOW_DAYS`** controls which acquisition dates each run (re)processes
  (`pipeline.py:227-237`); baselines still draw on *all* prior observations via the DB.
  Shrinking it never discards or refetches anything. Enlarging it backfills older scenes —
  new EE requests for data the instance has never had, so not redundant. Its only coupling
  to other config is the retention floor (`WINDOW_DAYS + 14`).
- **`COG_RETENTION_DAYS`** prunes only local COG *files*, keyed on acquisition date, never
  DB rows (`retention.py:63-96`). A pruned raster needed later is re-exported from recorded
  provenance (`reproduce.py`), and re-downloading doesn't reset its retention clock. Those
  re-exports are a deliberate, documented storage-for-compute trade — "redundant" only if
  retention is set shorter than the instance's actual re-access pattern. Sizing note: at a
  Solomon Islands scale (~200–300 observations/month, each with 2 index COGs and up to 2
  change COGs), 90 days of retention can plausibly approach the reference VM's 30 GB disk
  before compute is ever a concern — check `COG_RETENTION_DAYS` against disk size first
  when scaling the AOI, especially if local extraction (Finding 2) makes on-disk COGs
  load-bearing.
- **`RESOLVED_AFTER_DAYS`** changes when an event auto-resolves — an *interpretation* of
  detections, not a detection itself, and the code explicitly excludes it from methodology
  (`cli.py:583-587`). That is defensible, but note it does change recorded event *status*
  history. Live caveat: lengthening it does not revive events already marked resolved.
- **Confidence tunables** are the one place versioning relies on manual discipline: the
  weights are recorded per-assessment via the denormalized `inputs` JSONB and the
  `rule_version` string, but nothing forces a `RULE_VERSION` bump when a weight is edited.
  See Finding 6.

---

## Findings & recommendations

### 1. Threshold-class changes force a fully redundant re-export of the whole window

> **Status: implemented.** The methodology now splits into a content-addressed
> `raster_lineage` (script pin, collections, scale, masked categories, baseline window) that
> index/change rasters key on, and the detection layer (threshold, min area, forest mask)
> that candidates/events key on — migration `0020`. A detection-parameter change reuses
> every COG with zero exports; a `candidate_extraction` marker table tracks which
> methodology has extracted from which raster. One deliberate simplification: the raster
> lineage keeps `baseline_window` (change-COG paths don't encode it, so splitting it out
> would collide files), so baseline changes still re-export — the index-reuse refinement
> below remains future work.

The biggest avoidable EE cost in the current design. `delta_nbr_threshold`, `min_area_m2`,
the forest-mask trio, and `delta_vv_db_threshold` affect **only** the candidate-extraction
stage, yet changing any of them re-exports every index and change COG in the window, because:

- the methodology version is a single hash over *all* parameters (`methodology.py:79-82`), and
- `index_raster` / `change_raster` reuse is keyed on `(observation, type,
  methodology_version_id)` (`models.py:157-163`, `185-191`).

**Recommendation — split the methodology into two content-addressed layers:**

- **Raster layer**: `ee_script_version`, `collections`, `scale_m`, `masked_categories`,
  `baseline_window` — keys for `index_raster` / `change_raster`.
- **Detection layer**: `delta_*_threshold`, `min_area_m2`, `forest_mask` (+ a reference to
  its raster-layer parent) — keys for `disturbance_candidate` and everything downstream.

A threshold experiment would then reuse every COG and re-run only vectorization. The freeze
rule (`candidates.py:69-78`) ports cleanly: freezing binds a raster to the detection layers
already tracked from it. The recorded full parameter set stays reproducible because a
detection-layer row references its raster-layer parent.

### 2. Even re-vectorization doesn't need EE — polygonize the stored COG client-side

> **Status: implemented** (`localextract.py`): windowed threshold + polygonize + per-polygon
> statistics from the stored COG, with the forest mask applied from a one-time static
> per-AOI mask COG via `WarpedVRT`. The pipeline prefers this path for re-extraction and
> falls back to the EE delta rebuild when the COG is pruned or unreadable.

Vectorization currently reduces the live EE delta-image graph (`earthengine.py:251-269`);
the exported COG is never read back. Since the COG *is* the raw signed delta, thresholding +
polygonizing it locally (rasterio/shapely) would make detection-layer changes **zero-EE**,
subject to the COG still being on disk (retention interplay: a pruned COG would first need
one `cogs reproduce` export). This also decouples re-extraction from EE quota and latency.

**Cost on the reference VM** (e2-micro: 2 burstable vCPUs, 1 GB RAM, 30 GB `pd-standard`),
sized for an AOI at Solomon Islands scale (~28,900 km² of land ≈ 32 M pixels at 30 m;
~200–300 observations per 30-day window across ~20–30 HLS tiles): the compute itself is
light. Per change raster it is one elementwise `delta < threshold` compare, one polygonize
of a *sparse* mask (cost scales with shape count, not raster size), and zonal stats — with
windowed reads (e.g. 512×512 blocks) peak memory is a few MB per raster, comfortably inside
what Postgres and the dashboard leave free, and CPU is seconds per scene. The binding
constraint is the disk: a 30 GB `pd-standard` volume sustains only single-digit MB/s, so a
full-window re-extraction after a threshold change (reading every change COG, likely a few
hundred MB to ~1 GB compressed) is I/O-dominated — tens of minutes to about an hour. That
still strictly beats the status quo, where the same change re-exports the window from EE at
up to an hour per export (`storage.py:29`), four in flight, inside the 20 h pipeline budget;
and it is a rare, operator-initiated batch, not the daily hot path (incremental runs add
seconds per new scene). Two implementation caveats: reads must be windowed — whole-array
loads of a 13.4 M-pixel tile (~54 MB as float32, several at once) would not survive 1 GB of
RAM alongside Postgres; and the forest mask, today applied from the EE asset at
vectorization time, would need a one-time local COG export per (asset, canopy %) per AOI —
static and reusable, but a new artifact type.

### 3. `baseline_window` experiments could reuse index COGs — and even skip EE entirely

Index rasters don't depend on `baseline_window`; under the layered model of Finding 1 they
survive a baseline change, leaving only change-raster re-exports (legitimate). Going
further: since per-scene index COGs are retained, ΔNBR could be computed locally
(current − median of recorded priors) with no EE at all. Caveats: grid alignment and
valid-pixel masking must be reimplemented locally, and radar has no per-scene index COGs
(only deltas are exported), so this applies to the optical lineage only.

Unlike Finding 2, this half is **not** recommended for the reference VM: it reads the
current plus all `baseline_window` prior index COGs per observation (~6× Finding 2's I/O)
plus a per-pixel median, turning a full-window rebuild into several hours of `pd-standard`
disk time at Solomon Islands scale — feasible inside the 20 h budget, but a much weaker
cost/benefit than Finding 2, on top of the alignment/masking caveats above. The layered
methodology of Finding 1 already removes the *redundant* index re-exports; baseline
compositing itself is best left in EE.

### 4. `EE_SCRIPT_VERSION` is a coarse invalidation pin

> **Status: implemented.** `RASTER_SCRIPT_VERSION` (per lineage) now invalidates rasters;
> `EE_SCRIPT_VERSION` invalidates only detection.

Bumping it for any code change re-exports everything, even when raster math is untouched.
If Finding 1's split is adopted, split the pin too (raster-stage vs detection-stage script
versions) so vectorization-only fixes don't invalidate rasters.

### 5. `FOREST_SENTINEL_COG_ROOT` (and `DATABASE_URL`) are silent re-export footguns

> **Status: mitigation implemented.** The pipeline records a prominent warning event at run
> start when ≥50 % of the window's cataloged COGs are missing on disk, before EE quota is
> spent.

Moving either without migrating its counterpart makes every reuse check miss and re-exports
the whole window with no warning. Cheap mitigation: at run start, if a large fraction of
in-window catalog rows point at missing files, log a prominent warning ("COG root moved or
files pruned? N rasters will be re-exported") before spending EE quota.

### 6. Confidence weights rely on manual version discipline

> **Status: implemented.** `RULE_VERSION` is now content-addressed (`fused-v3+<hash>` as of #168) over every weight,
> cutoff, and normalization constant.

Methodology parameters are content-addressed, but `confidence.py`'s weights/cutoffs are
versioned by a hand-bumped string (`RULE_VERSION = "fused-v2"`). Editing a weight without
bumping it silently mixes semantics across assessments (history survives — assessments are
append-only and record their `inputs` — but the label lies). Recommendation: derive
`rule_version` (or a suffix of it) by hashing the weight/cutoff dict, mirroring
`auto_version()`.

### 7. Methodology changes break incident continuity on a live instance

> **Status: documented** — operator note added to `docs/architecture.md` §5.9 (the
> carry-forward linking table remains future work).

Parallel lineages are the right reproducibility call, but operators should know the
consequence: after any methodology change, existing events stop growing (candidates only
extend events under the same methodology version), linger until `RESOLVED_AFTER_DAYS`
auto-resolves them, and the new lineage starts fresh events with new first-detected dates.
An ongoing disturbance will appear as two events split at the methodology boundary. Worth a
prominent note in operator docs; a future "event carry-forward" linking table could
associate successor events across versions without falsifying provenance.

### 8. AOI geometry is unversioned instance data

> **Status: implemented.** Every run stamps `pipeline_run.aoi_geometry_hash` (migration
> `0019`) and a changed hash records a warning event mirroring the methodology-change one.

Editing an existing AOI's polygon changes discovery scope from the next run onward while
all history recorded under the old footprint persists silently. Consider either recording a
geometry hash on `pipeline_run`, or treating geometry edits like methodology changes for
audit purposes (log old/new hash at run start).

---

## Appendix: documented / planned but not implemented

| Planned config | Current state |
| --- | --- |
| ΔNDVI candidate lineage | NDVI and ΔNDVI COGs are computed and exported (`change.py:35-38`), but candidate extraction hardcodes ΔNBR; no knob selects the index. |
| VH / dual-pol radar | VV-only: `_REQUIRED_BAND = "VV"` (`sentinel1.py:38`), metric fixed to `vv_db` (`cli.py:542`). Scenes without VV are skipped; no polarization config exists. |
| Orbit policy | Recorded as a methodology parameter but fixed at `same_direction` (`cli.py:545`); not configurable. |
| Additional forest-mask sources | `hansen` / `worldcover` / `none` implemented (`forestmask.py:37-39`); other datasets (e.g. GEDI-derived layers, `architecture.md:111`) discussed but not wired to config. |
| Storage backend selection | `Storage` is a protocol but `LocalDiskStorage` is hardcoded (`storage.py`); GCS-resident COGs / Cloud SQL are described as future work (`architecture.md:97`). |
| Async (submit-and-return) exports | Exports are synchronous with polling; an async mode (`architecture.md:115`) would add new tuning config. |
| COG validation on ingest | Planned (`architecture.md:90`); staged files are currently copied as-is. |

---

## References

1. Claverie, M., Ju, J., Masek, J.G., et al. (2018). *The Harmonized Landsat and Sentinel-2
   surface reflectance data set.* Remote Sensing of Environment 219, 145–161.
   [doi:10.1016/j.rse.2018.09.002](https://doi.org/10.1016/j.rse.2018.09.002)
2. NASA LP DAAC. *Harmonized Landsat and Sentinel-2 (HLS) Product User Guide, v2.0* —
   [HLS_User_Guide_V2.pdf](https://lpdaac.usgs.gov/documents/1698/HLS_User_Guide_V2.pdf).
   Product DOIs: [10.5067/HLS/HLSL30.002](https://doi.org/10.5067/HLS/HLSL30.002),
   [10.5067/HLS/HLSS30.002](https://doi.org/10.5067/HLS/HLSS30.002). EE Data Catalog:
   [HLSL30](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSL30_v002),
   [HLSS30](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSS30_v002).
3. Zhu, Z. & Woodcock, C.E. (2012). *Object-based cloud and cloud shadow detection in
   Landsat imagery* (Fmask). Remote Sensing of Environment 118, 83–94.
   [doi:10.1016/j.rse.2011.10.028](https://doi.org/10.1016/j.rse.2011.10.028)
4. Key, C.H. & Benson, N.C. (2006). *Landscape Assessment (LA): Sampling and Analysis
   Methods.* In FIREMON: Fire Effects Monitoring and Inventory System, USDA Forest Service
   Gen. Tech. Rep. RMRS-GTR-164-CD — origin of NBR/ΔNBR severity thresholding.
   [treesearch/24066](https://research.fs.usda.gov/treesearch/24066)
5. López García, M.J. & Caselles, V. (1991). *Mapping burns and natural reforestation using
   Thematic Mapper data.* Geocarto International 6(1), 31–37 — introduces NBR.
   [doi:10.1080/10106049109354290](https://doi.org/10.1080/10106049109354290)
6. Hansen, M.C., Potapov, P.V., Moore, R., et al. (2013). *High-Resolution Global Maps of
   21st-Century Forest Cover Change.* Science 342(6160), 850–853.
   [doi:10.1126/science.1244693](https://doi.org/10.1126/science.1244693). EE Data Catalog:
   [UMD/hansen/global_forest_change_2023_v1_11](https://developers.google.com/earth-engine/datasets/catalog/UMD_hansen_global_forest_change_2023_v1_11).
7. Zanaga, D., Van De Kerchove, R., Daems, D., et al. (2022). *ESA WorldCover 10 m 2021
   v200.* [doi:10.5281/zenodo.7254221](https://doi.org/10.5281/zenodo.7254221). EE Data
   Catalog: [ESA/WorldCover/v200](https://developers.google.com/earth-engine/datasets/catalog/ESA_WorldCover_v200).
8. ESA Copernicus Sentinel-1 GRD products; Earth Engine ingestion applies thermal-noise
   removal, radiometric calibration, terrain correction, and dB conversion — EE Data
   Catalog: [COPERNICUS/S1_GRD](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S1_GRD);
   preprocessing notes: [Sentinel-1 Algorithms](https://developers.google.com/earth-engine/guides/sentinel1).
9. Reiche, J., Mullissa, A., Slagter, B., et al. (2021). *Forest disturbance alerts for the
   Congo Basin using Sentinel-1* (RADD alerts — VV/VH backscatter-decrease detection with
   per-orbit handling). Environmental Research Letters 16, 024005.
   [doi:10.1088/1748-9326/abd0a8](https://doi.org/10.1088/1748-9326/abd0a8)
10. Gorelick, N., Hancher, M., Dixon, M., et al. (2017). *Google Earth Engine:
    Planetary-scale geospatial analysis for everyone.* Remote Sensing of Environment 202,
    18–27. [doi:10.1016/j.rse.2017.06.031](https://doi.org/10.1016/j.rse.2017.06.031)
11. Rouse, J.W., Haas, R.H., Schell, J.A. & Deering, D.W. (1974). *Monitoring vegetation
    systems in the Great Plains with ERTS.* NASA SP-351; Tucker, C.J. (1979). *Red and
    photographic infrared linear combinations for monitoring vegetation.* Remote Sensing of
    Environment 8, 127–150.
    [doi:10.1016/0034-4257(79)90013-0](https://doi.org/10.1016/0034-4257%2879%2990013-0) — NDVI.
12. Zhu, Z. & Woodcock, C.E. (2014). *Continuous change detection and classification of land
    cover using all available Landsat data* (CCDC) — related precedent for
    per-pixel time-series baselines.
    [doi:10.1016/j.rse.2014.01.011](https://doi.org/10.1016/j.rse.2014.01.011)
13. FAO (2018). *Global Forest Resources Assessment 2020: Terms and Definitions* — forest
    defined as ≥0.5 ha with tree canopy cover ≥10 %; context for the 30 % canopy threshold
    and ~0.5 ha minimum-area conventions used with Hansen-derived masks.

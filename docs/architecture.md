# Architecture

This document describes the architecture of Open Forest Sentinel as defined by the project README. It is intentionally faithful to that source; design points the README leaves open are either recorded here once resolved (§5) or listed as still open (§7).

## 1. Purpose and shape of the system

Open Forest Sentinel is a generalized, low-cost forest disturbance monitoring system for a configurable Area of Interest (AOI). The initial deployment target is the Solomon Islands, but **AOI deployability is a first-class feature**: the same system runs over other countries, regions, protected areas, watersheds, concessions, or custom polygons through configuration rather than code changes.

For an appropriately constrained AOI, the system targets near-zero or very low infrastructure cost, because free-tier and low-cost cloud resources are sufficient for compute, database, and prototype raster storage.

A defining property is **observation currency**: by using openly available HLS imagery with frequent Landsat / Sentinel revisit cadence, detections should be less than one week old and refreshed more frequently than weekly, subject to cloud cover, data availability, and AOI size.

## 2. User-facing deliverable

The product is **not** a set of derived raster files. It is a lightweight dashboard that surfaces
the README's ten "Product Deliverable" questions. The **six core questions** ship with the
Slice 2 dashboard (§5.10):

- where likely logging or forest disturbance is happening
- when disturbance was first detected
- how large the affected area is
- how quickly the disturbance is expanding
- which detections are new, ongoing, resolved, or uncertain (`new`/`ongoing` today; `resolved`/`uncertain` arrive with scheduling and confidence in Slices 3–4, §5.9)
- what satellite-derived evidence supports each detection

The remaining four — which sensor or method produced a detection, whether it is optical-only /
radar-only / fused, how quality conditions affect confidence, and how it relates to contextual
layers — arrive with the confidence model, radar augmentation, and context-layer epics
(E14–E17).

Derived rasters are internal analytical artifacts that power detection, tracking, visualization, and review.

## 3. Data pipeline

The pipeline runs on a schedule end-to-end. **Imagery access and raster computation run server-side in Google Earth Engine (EE); the Compute Engine VM orchestrates EE, then ingests the exported products.** See [§4a — Imagery access & compute decision](#4a-imagery-access--compute-decision-google-earth-engine) for the rationale.

1. **GitHub Actions** runs on a cron schedule and triggers the pipeline. *(Target design — the shipped scheduler today is a systemd timer on the VM; the Actions workflow exists but is disabled until Slice 3 / E11. See `DEPLOYMENT.md` §7.)*
2. A **Google Compute Engine VM** executes the Python orchestration job (it submits Earth Engine work and ingests results; it does not do the raster math itself).
3. The pipeline loads the configured AOI geometry.
4. The pipeline discovers and accesses relevant **HLS analysis-ready imagery through Google Earth Engine** — `ee.ImageCollection` over the `HLSL30` / `HLSS30` v2.0 collections, filtered to the AOI and time window. NASA HLS remains the source dataset; Earth Engine is the access and compute substrate.
5. **Earth Engine computes** vegetation / disturbance indices server-side as band expressions:
   - `NBR  = (NIR - SWIR2) / (NIR + SWIR2)`
   - `NDVI = (NIR - RED)  / (NIR + RED)`
6. Change products (ΔNBR / ΔNDVI or other anomaly measures) are computed server-side against a per-pixel trailing-median baseline.
7. Change signals are converted into candidate disturbance polygons server-side (threshold + `reduceToVectors`).
8. Candidate polygons are tracked over time as disturbance events.
9. Outputs are exposed through a dashboard with maps, timelines, event detail views, and AOI summary metrics.
10. Earth Engine **exports** raster artifacts as **Cloud Optimized GeoTIFFs (COGs)** to a transient **Google Cloud Storage** staging area (`Export.image.toCloudStorage`). Exports are asynchronous batch tasks: the pipeline submits a task and polls until it is `COMPLETED`. On completion the VM **copies the COG to its local disk** — the canonical store — records the local path, and **deletes the GCS staging object**, so bulk raster storage stays on the VM's always-free disk and GCS stays inside its free tier. See [§4b — Cost model: the $0 path](#4b-cost-model-the-0-path).
11. Metadata, provenance, AOIs, detections, and event histories live in **PostgreSQL + PostGIS**.

```
schedule (GitHub Actions cron)
        │
        ▼
GCE VM ── load AOI ──▶ submit Earth Engine work ─────────────────────┐
   ▲                                                                  │
   │                              Earth Engine (server-side)          │
   │                   filter HLS ▸ compute NBR/NDVI ▸ ΔNBR/ΔNDVI     │
   │                   vs. trailing-median ▸ threshold ▸ polygonize   │
   │                                      │                           │
   │                                      ▼                           │
   │              Export.image.toCloudStorage (COG → GCS staging)     │
   │                                      │                           │
   └──── poll task ── COMPLETED ──────────┘                           │
                │                                                     │
                ▼                                                     ▼
   copy COG → VM disk, delete GCS staging,                  candidate polygons
   record local path + metadata                                      │
                │                                                     │
                └──────────────────────┬──────────────────────────────┘
                                       ▼
                ┌──────────────────────┴──────────────────────┐
                ▼                                              ▼
  COGs on VM local disk (GCS = transient staging)     PostgreSQL + PostGIS
                                                               │
                                                               ▼
                                                           Dashboard
```

## 4. Prototype technology stack

| Concern                       | Prototype                                                | Future path                                    |
|-------------------------------|----------------------------------------------------------|------------------------------------------------|
| Scheduler / trigger           | systemd timer on the VM today; GitHub Actions cron is the target (Slice 3 / E11) | —             |
| Compute (orchestration)       | Google Compute Engine VM (submits EE work, ingests results) | —                                           |
| Imagery access & raster compute | Google Earth Engine (`earthengine-api`)                | —                                              |
| Database                      | PostgreSQL + PostGIS on the same Compute Engine VM       | Cloud SQL for PostgreSQL with PostGIS          |
| Database access / migrations  | SQLAlchemy 2.0 ORM, GeoAlchemy2 spatial types, Alembic   | —                                              |
| Language                      | Python                                                   | —                                              |
| Local raster handling         | None — EE-exported COGs are copied to disk as-is         | rasterio / GDAL / rio-cogeo COG validation on ingest (planned bead); numpy |
| Imagery source                | NASA HLS (`HLSL30` / `HLSS30` v2.0), accessed via Google Earth Engine | —                                 |
| Raster output format          | Cloud Optimized GeoTIFF (written by EE export)           | —                                              |
| Raster storage                | Local VM filesystem, e.g. `/data/cogs/` (canonical). GCS used only as a transient EE-export staging area, then cleared | Google Cloud Storage (when COG volume outgrows the free VM disk) |
| Dashboard                     | Lightweight web application backed by PostGIS            | —                                              |
| Versioning / CI               | GitHub                                                   | —                                              |

The prototype co-locates **compute (orchestration), the database, and raster storage** on a single always-free GCE VM for $0 cost. Earth Engine exports COGs to a transient GCS staging area; the VM copies each COG to its local disk and clears the staging object, so bulk storage lives on the free 30 GB disk rather than in (metered) GCS. The future path separates raster storage to GCS once COG volume outgrows the free disk, and the database to managed Cloud SQL.

Schema changes are versioned with **Alembic**; each migration is reviewed and shipped in the bead that introduces the schema it depends on. A `docker-compose.yml` at the repository root runs PostgreSQL + PostGIS for local development, and the database URL is supplied through the `FOREST_SENTINEL_DATABASE_URL` environment variable.

### 4a. Imagery access & compute decision: Google Earth Engine

**Decision.** Slice 1 accesses HLS and computes index, change, and candidate products **server-side in Google Earth Engine (EE)**, exporting Cloud Optimized GeoTIFFs to a transient Cloud Storage staging area that the VM copies to local disk (see [§4b](#4b-cost-model-the-0-path)).

**Rationale.**

- **Server-side compute keeps the VM tiny.** NBR / NDVI, the trailing-median baseline, ΔNBR / ΔNDVI, and threshold-plus-polygonize all map to EE primitives (`normalizedDifference`, `ImageCollection.median()`, `gt()` + `reduceToVectors`). EE does the heavy raster work (free under the noncommercial tier), so the VM only orchestrates and hosts a small Postgres — which fits the **always-free `e2-micro`** instance.
- **No raw-scene egress.** Inputs stay inside Google's network; only the finished COGs leave EE, into a transient GCS staging area.
- **EE produces the COGs.** The VM does not write rasters itself; it copies the finished COGs to its free local disk (the canonical store), keeping bulk storage at $0.
- **Cloud / shadow / haze masking is built in.** The `Fmask` QA band ships with both HLS collections, so QA masking (E14) is satisfied inside Slice 1.
- **Cheaper future slices.** Sentinel-1 GRD (E16), Hansen Global Forest Change, GEDI, ESA WorldCover, and other context layers (E17) already exist in the EE catalog.

**Costs and risks accepted.**

- **Asynchronous execution.** Exports are batch tasks (submit → poll → ingest). The pipeline currently consumes them **synchronously** — each export is submitted and polled to completion before the dependent step (`storage.py`), so a run blocks for its full duration; a submit-and-return mode is future work if run times demand it.
- **EECU-hours are the compute cost dimension.** The project runs under the EE **noncommercial tiers**; the working assumption is the **Contributor** tier, to be confirmed once real per-run usage is measured. Commercial use would later require a paid license.
- **Observation currency depends on EE's HLS ingestion lag**, which can run behind NASA LP DAAC. If the lag is large for the AOI, the README's "less than one week old" target may not hold; confirm against real data during implementation and adjust the target if needed.
- **Reproducibility.** Because the compute substrate is Google's, `methodology_version` records must capture the EE script version / asset IDs so a run can be reproduced.
- **Auth surface.** Requires a GCP service account with Earth Engine access, an EE-registered Cloud project, and a GCS bucket.

### 4b. Cost model: the $0 path

The prototype targets **$0/month** for a reasonably small AOI by staying inside always-free tiers. Free-tier limits change and are region-specific — verify against current GCP / Earth Engine docs before relying on them.

| Component | $0 mechanism | Watch-out |
|-----------|--------------|-----------|
| Scheduler | GitHub Actions cron (free minutes; unlimited for public repos) | Heavy/long jobs can exhaust private-repo minutes |
| Raster compute | Earth Engine **noncommercial** tier (free EECU-hours) | Must stay noncommercial; tier quota (working assumption: Contributor) confirmed once real usage is measured |
| Orchestrator + database | Always-free **`e2-micro`** VM (2 shared vCPU, 1 GB RAM), 24/7, in `us-west1` / `us-central1` / `us-east1` | 1 GB RAM is tight — keep the AOI small and tune Postgres |
| Raster storage | COGs on the VM's **30 GB free disk** (shared with Postgres) | Disk is finite — needs a retention policy; serving COGs later comes from the VM, not a CDN |
| Export staging | GCS used only as a **transient** EE-export hop, cleared after each copy-to-disk | Keep staging well under the 5 GB-month GCS free tier by deleting promptly |

**Why COGs land on the VM disk, not GCS:** Earth Engine can only export to GCS (not to a VM), but GCS storage is free only up to 5 GB-month, whereas the VM's 30 GB disk is always-free. So the pipeline treats GCS as a short-lived staging area and copies each finished COG to local disk, then deletes the staging object. This keeps bulk raster storage at $0 at the cost of one extra copy per export. The trade-off accepted: a finite local disk (retention policy required) and, later, dashboard COG serving from the VM rather than from object storage / CDN.

**Residual cost risks:** internet **egress** (e.g. a public dashboard reading from the VM), exceeding the free disk and spilling to GCS, or any shift to **commercial** Earth Engine use. None apply to a small-AOI prototype kept within the limits above.

## 5. Core domain objects

These are the entities the system tracks. Concrete schemas are introduced incrementally, one
bead per table; the tables realized so far are specified in §5.1–§5.10. Objects listed as
*planned* are not yet implemented.

| Object                  | Status      | Description                                                                              |
|-------------------------|-------------|------------------------------------------------------------------------------------------|
| `aoi`                   | implemented | Configured area of interest geometry and metadata.                                       |
| `observation`           | implemented | One imagery acquisition / date used for analysis. Holds sensor, timestamp, cloud / quality metadata, and source scene identifiers. |
| `quality_mask`          | implemented | Per-observation QA coverage from Fmask masking (§5.4).                                   |
| `index_raster`          | implemented | Derived NBR / NDVI raster metadata.                                                      |
| `change_raster`         | implemented | ΔNBR / ΔNDVI or anomaly raster metadata.                                                 |
| `change_raster_source`  | implemented | Link table: every `index_raster` that contributed to a `change_raster` (§5.6).           |
| `disturbance_candidate` | implemented | Raw detected disturbance polygon.                                                        |
| `disturbance_event`     | implemented | Tracked logging / disturbance event over time.                                           |
| `event_observation`     | implemented | Per-date measurement of event area, severity, and growth.                                |
| `methodology_version`   | implemented | Processing and detection method provenance.                                              |
| `manual_review`         | planned     | Human validation, notes, uncertainty, false-positive status (Slice 3, E8).               |
| `sensor_source`         | planned     | Source dataset metadata beyond HLS (E16).                                                |
| `radar_change_raster`   | planned     | Sentinel-1-derived SAR change metadata (E16).                                            |
| `context_layer`         | planned     | Legal / administrative / infrastructure overlay dataset (E17).                           |
| `event_context`         | planned     | Relationship between an event and contextual features (E17).                             |
| `confidence_assessment` | planned     | Structured explanation of an event's confidence level (E15).                             |

Relationships implied by the pipeline:

- An `aoi` has many `observation`s.
- An `observation` produces `index_raster`s; pairs / sequences of observations produce `change_raster`s.
- A `change_raster` yields `disturbance_candidate`s.
- `disturbance_candidate`s are tracked over time into `disturbance_event`s.
- A `disturbance_event` has many `event_observation`s and may have `manual_review`s.
- Every derived artifact is tagged with the `methodology_version` that produced it.

### 5.1 Slice 1 concrete schemas

The abstract domain objects above are realized incrementally, one bead per table, as Slice 1
is built. SQLAlchemy models live in `src/forest_sentinel/models.py`; each table ships in its own
Alembic migration. The deterministic constraint-naming convention in `models.py` keeps
hand-written and autogenerated migrations consistent.

**`observation`** (bead #37, migration `0002`). One HLS acquisition over an AOI; source data, so
no `methodology_version` reference.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `aoi_id` | int FK → `aoi.id` | |
| `sensor` | text | `HLSL30` (Landsat 8/9) or `HLSS30` (Sentinel-2) |
| `acquired_at` | timestamptz | from EE `system:time_start` |
| `source_scene_id` | text | from EE `system:index` |
| `cloud_cover_percent` | float, nullable | scene cloud-cover property when present |
| `created_at` | timestamptz | server default `now()` |

`UNIQUE (aoi_id, source_scene_id)` makes HLS discovery idempotent per AOI; an index on
`(aoi_id, acquired_at)` supports time-window queries.

**`methodology_version`** (bead #35, migration `0003`). Provenance for the processing/detection
method that produced a derived artifact.

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `name` | text | e.g. `optical-change` |
| `version` | text | e.g. `1.0.0` |
| `parameters` | JSONB | detection/processing parameters **plus EE script version and input collection/asset IDs** |
| `created_at` | timestamptz | server default `now()` |

`UNIQUE (name, version)` makes the identity stable.
`forest_sentinel.methodology.get_or_create_methodology_version` returns the existing row for
identical inputs (order-insensitive parameter comparison), inserts when absent, and raises
`MethodologyVersionMismatch` if a `(name, version)` already stores different parameters — the
same identity must never silently map to divergent parameters; bump the version instead.

### 5.2 Earth Engine seam and raster storage

**`forest_sentinel.earthengine`** (the EE seam) is the *single* module that touches `ee.*`.
Every Earth Engine operation — `initialize`, submitting an `Export.image.toCloudStorage`
task, reading task state — is a small function here that takes/returns plain Python or opaque
handles. Pipeline modules call these helpers and stay free of EE objects, so tests stub this
module rather than standing up a live EE session. Auth: a GCP service account with Earth Engine
access and an EE-registered Cloud project; the project is read from
`FOREST_SENTINEL_GEE_PROJECT` and credentials come from the ambient environment.

**`forest_sentinel.storage`** (bead #36) owns the GCS-staging → local-disk bridge. Earth Engine
can only export to Google Cloud Storage, but bulk storage must stay on the VM's always-free disk
(§4b). `Storage.export_image(image, key)`:

1. submits the EE export to the staging bucket at the key's relative prefix,
2. polls the task to `COMPLETED` (raising `StorageError` on a terminal failure),
3. copies the finished COG from GCS to the deterministic local path, and
4. **deletes the staging object**.

Deterministic layout: `{root}/{aoi_id}-{aoi_name}/{product}/{YYYY-MM-DD}/{file}.tif`, with each
free-form component sanitized. The AOI's database id prefixes the name so distinct AOIs whose
names sanitize identically (e.g. `My AOI` vs `my-aoi`) never share a tree, and the filename
embeds the source scene id (e.g. `nbr-{source_scene_id}.tif`) so same-day observations — both
HLS sensors acquire on the same date, and adjacent tiles share dates — never export to the same
path. Config via env: `FOREST_SENTINEL_COG_ROOT` (default `data/cogs/`),
`FOREST_SENTINEL_GCS_STAGING_BUCKET`. `LocalDiskStorage` is the only backend today; switching the
canonical store to GCS later is a swap behind the `Storage` protocol — pipeline code never
touches GCS or EE directly, only `export_image`. The EE client and the GCS bucket are injected,
so tests exercise the full lifecycle (submit → poll → copy → clear) with no live calls.

### 5.3 HLS discovery

**`forest_sentinel.hls`** (bead #38) discovers HLS scenes for an AOI and time window through
the EE seam and records them as `observation` rows. It enumerates both HLS v2.0 collections —
`NASA/HLS/HLSL30/v002` (Landsat 8/9, sensor `HLSL30`) and `NASA/HLS/HLSS30/v002` (Sentinel-2,
sensor `HLSS30`) — via `ImageCollection.filterBounds(aoi).filterDate(since, until)`. Each image
feature is parsed into the `observation` fields: `source_scene_id` from `system:index`,
`acquired_at` from `system:time_start` (epoch ms → UTC), `cloud_cover_percent` from the
`CLOUD_COVERAGE` property when present. Dedup is by the `(aoi_id, source_scene_id)` unique
constraint plus an in-run guard, so a re-run over the same window records nothing and reports the
images as skipped. An empty window / no available scenes yields zero observations without error.
`DiscoveryResult(discovered, recorded, skipped)` reports the per-pass counts.

**Auth.** Earth Engine needs a GCP service account with EE access and an EE-registered Cloud
project: `FOREST_SENTINEL_GEE_PROJECT` (project id) plus ambient Application Default
Credentials. On the VM those come from the **attached service account** via the metadata
server (the keyless default — no credentials file exists; see `scripts/provision_vm.sh`);
locally, use `gcloud auth application-default login`, or opt into a key file with
`CREATE_KEY=1 scripts/setup_gcp.sh` + `GOOGLE_APPLICATION_CREDENTIALS`. The
`FOREST_SENTINEL_GCS_STAGING_BUCKET` (used by storage) must be writable by the same account.
CI exercises everything through stubs, so no credentials are needed to run the tests.

### 5.4 QA masking (Fmask)

**`forest_sentinel.qa`** (bead #54) masks low-quality HLS pixels before index computation and
the baseline median, and records coverage. Both HLS v2.0 collections ship an `Fmask` QA band, so
masking is a cheap per-image EE operation. The clear-pixel rule masks **cloud** (bit 1), **cloud
shadow** (bit 3), **snow/ice** (bit 4), and **high aerosol** (bits 6–7 == `0b11`); water and
low/moderate aerosol are kept. The rule lives twice, kept in lock-step: the pure
`qa.fmask_clear(value)` (exhaustively unit-tested over synthetic bit patterns) and the EE
band-expression `earthengine.apply_fmask_mask(image)` (`bitwiseAnd`/`rightShift` + `updateMask`).

**`quality_mask`** (migration `0004`) records coverage per observation:

| Column | Type | Notes |
|--------|------|-------|
| `observation_id` | int PK, FK → `observation.id` (ON DELETE CASCADE) | one row per observation |
| `valid_pixel_fraction` | float | mean of the post-mask validity, from `reduceRegion` |
| `parameters` | JSONB | which categories were masked |
| `created_at` | timestamptz | |

`qa.measure_valid_fraction` computes the fraction via the EE seam; `qa.record_quality_mask`
upserts the row. Downstream `index_raster` / `change_raster` rows carry the same
`valid_pixel_fraction` so coverage is retained on derived products (#39, #40).

### 5.5 Index rasters (NBR, NDVI)

**`forest_sentinel.indices`** (bead #39) computes per-observation NBR and NDVI server-side.
For each observation it rebuilds the HLS image (`{collection}/{source_scene_id}`), applies the
Fmask mask, computes each index as `normalizedDifference`, exports a COG through `Storage`, and
upserts an `index_raster` row.

    NBR  = normalizedDifference([NIR, SWIR2])
    NDVI = normalizedDifference([NIR, RED])

Per-sensor HLS v2.0 band names (the two collections differ):

| Sensor | RED | NIR | SWIR2 |
|--------|-----|-----|-------|
| `HLSL30` | `B4` | `B5` | `B7` |
| `HLSS30` | `B4` | `B8A` | `B12` |

*(Band-name mapping is the documented decision; confirm against the live EE assets on the first
real run.)*

**`index_raster`** (migration `0005`): `id`, `observation_id` (FK), `methodology_version_id`
(FK), `index_type` (`NBR`/`NDVI`), `cog_path` (local COG path), `valid_pixel_fraction`,
`created_at`. `UNIQUE (observation_id, index_type, methodology_version_id)` (constraint
`uq_index_raster_identity`) makes re-runs upsert rather than duplicate. Coverage is measured once
per observation on the masked image and recorded both on the `quality_mask` row and on each
`index_raster`.

### 5.6 Change products (ΔNBR, ΔNDVI)

**`forest_sentinel.change`** (bead #40) turns per-observation indices into a disturbance signal.
The baseline is the per-pixel **median** of the index over a trailing window of prior
observations (`ImageCollection.median()`); the change product is `current − baseline`
(`subtract`). Only prior observations that **have an `index_raster` under the current
methodology** participate in the baseline, so the imagery reduced into the median always equals
the recorded `change_raster_source` provenance — a prior whose index export failed, or that
predates the methodology, is excluded from the math rather than silently omitted from the
record. The delta is exported as a COG and recorded as a `change_raster`. An observation with
no such priors has no baseline and is skipped.

The trailing-window size is configurable (`baseline_window`, **default 5**) and is captured in
the `methodology_version.parameters` and on the `change_raster` row.

**`change_raster`** (migration `0006`): `id`, `observation_id` (FK, the current observation),
`methodology_version_id` (FK), `change_type` (`delta_nbr`/`delta_ndvi`), `cog_path`,
`baseline_window`, `valid_pixel_fraction` (carried from the current observation's index),
`created_at`. `UNIQUE (observation_id, change_type, methodology_version_id)`
(`uq_change_raster_identity`) → re-runs upsert.

**`change_raster_source`** (migration `0006`): composite PK `(change_raster_id, index_raster_id)`,
`ON DELETE CASCADE` from `change_raster`. Records every contributing `index_raster` — the current
observation's index plus each baseline observation's index — so a change product's provenance is
explicit. Re-runs replace the source set — unless the raster is **frozen** (any of its candidates
is tracked into an event, §5.7): a frozen raster's COG and source set are event evidence and are
never recomputed, even if a late-arriving observation would change the baseline.

### 5.7 Disturbance candidates

**`forest_sentinel.candidates`** (bead #41) turns the ΔNBR change signal into reviewable
geometry — the visible output of Slice 1. In Earth Engine it thresholds the delta (disturbance =
an NBR drop beyond a threshold, `delta < threshold`), polygonizes the mask with
`reduceToVectors`, tags each polygon with its area (`Feature.area`), and filters to a minimum
area. The features come back as WGS 84 GeoJSON and are persisted as `disturbance_candidate` rows.

**Defaults** (overridable via methodology `parameters` or explicit kwargs, and captured in the
`methodology_version`): `delta_nbr_threshold = -0.25`, `min_area_m2 = 4500` (≈ 0.45 ha). The
minimum area is enforced both server-side (the EE `Filter`) and client-side as a guard. The
`reduceToVectors` scale is a documented cost lever.

**`disturbance_candidate`** (migration `0007`): `id`, `change_raster_id` (FK, ON DELETE CASCADE),
`methodology_version_id` (FK), `geometry` (PostGIS `POLYGON` SRID 4326), `detected_at` (the source
observation's `acquired_at`), `area_m2`, `created_at`; indexed on `change_raster_id`. Re-runs
delete and re-insert the candidate set for a change raster so rows reflect the latest parameters —
but only while none of that set has been tracked into events. Once any candidate is referenced by
an `event_observation` (§5.9) it is event history: the raster's candidate set is frozen and
re-runs return it unchanged, so event footprints and timelines are never invalidated. Applying
new detection parameters to already-tracked rasters requires a new `methodology_version` (new
change rasters), not an in-place rewrite.

### 5.8 Pipeline orchestration

**`forest_sentinel.pipeline`** (bead #42) threads the building blocks into one runnable slice:
**discover → indices → change → candidates**. `forest-sentinel run --aoi <file> --since <d>
--until <d>` initializes Earth Engine, builds the storage backend from the environment,
get-or-creates the AOI (idempotent) and the methodology version (which pins the run parameters
and EE script version), then calls `run_pipeline`, which:

1. discovers HLS observations for the window,
2. computes NBR/NDVI for every observation in the window,
3. computes ΔNBR/ΔNDVI against each windowed observation's trailing baseline (the
   baseline itself may reach back before the window), and
4. extracts candidate polygons from each ΔNBR product.

Because compute runs in Earth Engine, every COG export is an **asynchronous batch task**; the
storage seam blocks and polls each export to `COMPLETED` before the dependent step, so a single
invocation drives the whole slice synchronously (a submit-and-return mode is a later bead if
needed). Export failures are isolated per observation: a scene whose export fails (or times
out) is skipped and counted in the summary, partial results are committed, and the CLI exits
nonzero so the scheduler alerts — one persistently bad export cannot zero out a whole window.
Runs are serialized per AOI with a transaction-scoped Postgres advisory lock, so a manual
`forest-sentinel run` alongside the systemd timer waits for the in-flight run instead of racing
its upserts. `run_pipeline` returns a `PipelineSummary` with per-stage counts, which the CLI prints.
Without `--since`/`--until`, `run` stays in the Slice 0 load-and-persist behavior. `run_pipeline`
is pure orchestration over injectable building blocks, so the hallway test
(`test_run_full_pipeline_produces_candidates`) exercises the full thread against a stubbed
Earth Engine + storage and asserts a candidate polygon lands in PostGIS and dumps to valid
WGS 84 GeoJSON — the mock-backed stand-in for a live run.

### 5.9 Disturbance events (Slice 2)

**`disturbance_event`** (migration `0008`) tracks a disturbance over time as the cumulative
footprint of overlapping candidates: `id`, `aoi_id` (FK), `methodology_version_id` (FK, provenance
per E9), `geometry` (PostGIS `MULTIPOLYGON` SRID 4326 — the unioned footprint), `status`
(`new`/`ongoing`; `resolved`/`uncertain` arrive with scheduling and confidence in later slices),
`first_detected_at`, `last_detected_at`, `created_at`.

**`event_observation`** (migration `0008`) is one per-date measurement of an event, produced by a
single candidate: `id`, `event_id` (FK, ON DELETE CASCADE), `disturbance_candidate_id` (FK),
`observed_at`, `area_m2` (the candidate's single-scene detection area), `growth_m2` (**footprint
expansion**: the geodesic area, PostGIS `ST_Area` over `geography`, that the candidate added to
the event's unioned footprint; null for the first measurement — never negative, unlike a naive
difference of detection areas, which can shrink under partial cloud while the disturbance keeps
growing), `created_at`. `UNIQUE (disturbance_candidate_id)` means each candidate contributes to
exactly one measurement, which makes event tracking idempotent and incremental.

**`forest_sentinel.events`** implements the **spatial-overlap** tracking algorithm
(`track_events_for_aoi`): the AOI's not-yet-tracked candidates are processed in detection order;
a candidate that intersects an existing event's footprint (PostGIS `ST_Intersects`) **and shares
its methodology version** extends it, otherwise it starts a new event — an event records one
`methodology_version_id` as provenance, so candidates from a different methodology start new
events rather than falsifying it. The pipeline (`run_pipeline`) calls tracking as its final stage,
so a single `forest-sentinel run` goes discover → indices → change → candidates → **events**, and
the per-stage summary reports events created and event-observations tracked.

### 5.10 Dashboard (Slice 2, E10)

**`forest_sentinel.dashboard`** is a FastAPI app (`uv run uvicorn forest_sentinel.dashboard.app:app`)
serving a read-only, unauthenticated view over PostGIS — the resolved Slice 2 decisions are
**FastAPI + Leaflet**, **no auth**. Endpoints:

- `GET /` — a static Leaflet map page (`static/index.html`) that consumes the API.
- `GET /api/aois` — AOIs with event counts.
- `GET /api/aois/{id}/events` — the AOI's events as a GeoJSON `FeatureCollection` (status, first/
  last detected, cumulative footprint area, latest detection area, observation count).
- `GET /api/events/{id}` — event detail: footprint geometry and area, the measurement
  **timeline** (per-scene detection area + footprint growth), and **supporting evidence** (the
  source ΔNBR change rasters).

Together these answer the six core README "Product Deliverable" questions (§2) — where
(geometry), when first detected, size (footprint area), expansion rate (timeline footprint
growth), status, and supporting evidence; the README's remaining deliverable questions arrive
with E14–E17. The database
session is an injectable dependency (`get_session`), so endpoints are tested headlessly with
FastAPI's `TestClient` against a transactional session; no Earth Engine or storage access occurs
in the dashboard.

## 6. Cross-cutting properties

- **AOI-first configurability.** Switching deployment to a new AOI is a configuration change, not a code change.
- **Cost discipline.** The prototype targets **$0/month** within always-free tiers; see [§4b](#4b-cost-model-the-0-path). With Earth Engine as the compute substrate, the primary compute cost dimension is **EECU-hours per run** (bounded by the chosen noncommercial tier); cost otherwise scales with AOI size, processing frequency, output retention, local-disk volume, and dashboard egress.
- **Temporal currency.** Scheduling and sensor revisit cadence are designed so detections refresh more often than weekly for small-to-medium AOIs.
- **Provenance.** Every derived artifact is traceable to its source observations and to the `methodology_version` that produced it.

## 7. Open design points

Earlier revisions listed detection thresholds, the tracking algorithm, the dashboard framework,
and the concrete schemas as TBD; those are now resolved and recorded in §5.1–§5.10. The points
that remain genuinely open, to be settled in implementation beads under the relevant epics:

- Retention policy for COGs and observations (the VM disk is finite; see §4b).
- The confidence scoring rule and `confidence_assessment` schema (E15, Slice 4).
- The manual-review workflow, its schema, and the authentication / access model for review
  (and any future non-read-only dashboard surface) (E8, Slice 3).
- Radar processing parameters and the `radar_change_raster` / `sensor_source` schemas (E16).
- Context-layer ingestion and the `context_layer` / `event_context` schemas (E17).

# Architecture

This document describes the architecture of Open Forest Sentinel as defined by the project README. It is intentionally faithful to that source; design choices not stated in the README are flagged as **TBD**.

## 1. Purpose and shape of the system

Open Forest Sentinel is a generalized, low-cost forest disturbance monitoring system for a configurable Area of Interest (AOI). The initial deployment target is the Solomon Islands, but **AOI deployability is a first-class feature**: the same system runs over other countries, regions, protected areas, watersheds, concessions, or custom polygons through configuration rather than code changes.

For an appropriately constrained AOI, the system targets near-zero or very low infrastructure cost, because free-tier and low-cost cloud resources are sufficient for compute, database, and prototype raster storage.

A defining property is **observation currency**: by using openly available HLS imagery with frequent Landsat / Sentinel revisit cadence, detections should be less than one week old and refreshed more frequently than weekly, subject to cloud cover, data availability, and AOI size.

## 2. User-facing deliverable

The product is **not** a set of derived raster files. It is a lightweight dashboard that surfaces:

- where likely logging or forest disturbance is happening
- when disturbance was first detected
- how large the affected area is
- how quickly the disturbance is expanding
- which detections are new, ongoing, resolved, or uncertain
- what satellite-derived evidence supports each detection

Derived rasters are internal analytical artifacts that power detection, tracking, visualization, and review.

## 3. Data pipeline

The pipeline runs on a schedule end-to-end. **Imagery access and raster computation run server-side in Google Earth Engine (EE); the Compute Engine VM orchestrates EE, then ingests the exported products.** See [§4a — Imagery access & compute decision](#4a-imagery-access--compute-decision-google-earth-engine) for the rationale.

1. **GitHub Actions** runs on a cron schedule and triggers the pipeline.
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
| Scheduler / trigger           | GitHub Actions cron                                      | —                                              |
| Compute (orchestration)       | Google Compute Engine VM (submits EE work, ingests results) | —                                           |
| Imagery access & raster compute | Google Earth Engine (`earthengine-api`)                | —                                              |
| Database                      | PostgreSQL + PostGIS on the same Compute Engine VM       | Cloud SQL for PostgreSQL with PostGIS          |
| Database access / migrations  | SQLAlchemy 2.0 ORM, GeoAlchemy2 spatial types, Alembic   | —                                              |
| Language                      | Python                                                   | —                                              |
| Local raster handling         | rasterio, GDAL, rio-cogeo (ingest / validate EE-exported COGs); numpy | —                                 |
| Imagery source                | NASA HLS (`HLSL30` / `HLSS30` v2.0), accessed via Google Earth Engine | —                                 |
| Raster output format          | Cloud Optimized GeoTIFF (written by EE export)           | —                                              |
| Raster storage                | Local VM filesystem, e.g. `/data/cogs/` (canonical). GCS used only as a transient EE-export staging area, then cleared | Google Cloud Storage (when COG volume outgrows the free VM disk) |
| Dashboard                     | Lightweight web application backed by PostGIS            | —                                              |
| Versioning / CI               | GitHub                                                   | —                                              |

The prototype co-locates **compute (orchestration), the database, and raster storage** on a single always-free GCE VM for $0 cost. Earth Engine exports COGs to a transient GCS staging area; the VM copies each COG to its local disk and clears the staging object, so bulk storage lives on the free 30 GB disk rather than in (metered) GCS. The future path separates raster storage to GCS once COG volume outgrows the free disk, and the database to managed Cloud SQL.

Schema changes are versioned with **Alembic**; each migration is reviewed and shipped in the bead that introduces the schema it depends on. A `docker-compose.yml` at the repository root runs PostgreSQL + PostGIS for local development, and the database URL is supplied through the `FOREST_SENTINEL_DATABASE_URL` environment variable.

### 4a. Imagery access & compute decision: Google Earth Engine

**Decision.** Slice 1 accesses HLS and computes index, change, and candidate products **server-side in Google Earth Engine (EE)**, exporting Cloud Optimized GeoTIFFs to Google Cloud Storage. This **supersedes** the earlier resolved decision to access HLS by downloading scenes through NASA's `earthaccess` client and computing indices locally with rasterio/numpy.

**Rationale.**

- **Server-side compute keeps the VM tiny.** NBR / NDVI, the trailing-median baseline, ΔNBR / ΔNDVI, and threshold-plus-polygonize all map to EE primitives (`normalizedDifference`, `ImageCollection.median()`, `gt()` + `reduceToVectors`), removing local raster loops. Because EE does the heavy compute (free under the noncommercial tier), the VM only orchestrates and hosts a small Postgres — which fits the **always-free `e2-micro`** instance. See [§4b](#4b-cost-model-the-0-path).
- **No raw-scene egress.** Inputs stay inside Google's network; only the finished COGs leave EE, into a transient GCS staging area.
- **EE produces the COGs.** No local rio-cogeo write path is needed; the VM only copies finished COGs to its free local disk (the canonical store), keeping bulk storage at $0.
- **Cloud / shadow / haze masking is built in.** The `Fmask` QA band ships with both HLS collections, so QA masking (E14) can be satisfied inside Slice 1.
- **Cheaper future slices.** Sentinel-1 GRD (E16), Hansen Global Forest Change, GEDI, ESA WorldCover, and other context layers (E17) already exist in the EE catalog.

**Costs and risks accepted.**

- **Asynchronous execution.** Exports are batch tasks (submit → poll → ingest), so the pipeline is a state machine rather than a synchronous function. The CLI entrypoint and the GitHub Actions cron must handle the task lifecycle.
- **EECU quota is the cost dimension.** Cost discipline shifts from "minimize bytes downloaded" to "minimize EECU-hours per run." The project runs under the EE **noncommercial tiers**; the working assumption is the **Contributor** tier, confirmed by the EECU benchmark in the verification plan. Commercial use would later require a paid license.
- **Observation currency depends on EE's HLS ingestion lag**, which can run behind NASA LP DAAC. The README's "less than one week old" target is validated against a live EE-vs-LP-DAAC lag measurement before the near-real-time target is locked (verification plan, V1).
- **Reproducibility.** Because the compute substrate is Google's, `methodology_version` records must capture the EE script version / asset IDs so a run can be reproduced.
- **Auth surface.** Requires a GCP service account with Earth Engine access, an EE-registered Cloud project, and a GCS bucket.

This decision is validated by the Option C verification plan (EE-vs-LP-DAAC ingestion lag, per-run EECU benchmark, and a GCE VM service-account smoke test) before Slice 1 implementation begins.

### 4b. Cost model: the $0 path

The prototype targets **$0/month** for a reasonably small AOI by staying inside always-free tiers. Free-tier limits change and are region-specific — verify against current GCP / Earth Engine docs before relying on them.

| Component | $0 mechanism | Watch-out |
|-----------|--------------|-----------|
| Scheduler | GitHub Actions cron (free minutes; unlimited for public repos) | Heavy/long jobs can exhaust private-repo minutes |
| Raster compute | Earth Engine **noncommercial** tier (free EECU-hours) | Must stay noncommercial; tier quota (working assumption: Contributor) is confirmed by verification V2 |
| Orchestrator + database | Always-free **`e2-micro`** VM (2 shared vCPU, 1 GB RAM), 24/7, in `us-west1` / `us-central1` / `us-east1` | 1 GB RAM is tight — keep the AOI small and tune Postgres |
| Raster storage | COGs on the VM's **30 GB free disk** (shared with Postgres) | Disk is finite — needs a retention policy; serving COGs later comes from the VM, not a CDN |
| Export staging | GCS used only as a **transient** EE-export hop, cleared after each copy-to-disk | Keep staging well under the 5 GB-month GCS free tier by deleting promptly |

**Why COGs land on the VM disk, not GCS:** Earth Engine can only export to GCS (not to a VM), but GCS storage is free only up to 5 GB-month, whereas the VM's 30 GB disk is always-free. So the pipeline treats GCS as a short-lived staging area and copies each finished COG to local disk, then deletes the staging object. This keeps bulk raster storage at $0 at the cost of one extra copy per export. The trade-off accepted: a finite local disk (retention policy required) and, later, dashboard COG serving from the VM rather than from object storage / CDN.

**Residual cost risks:** internet **egress** (e.g. a public dashboard reading from the VM), exceeding the free disk and spilling to GCS, or any shift to **commercial** Earth Engine use. None apply to a small-AOI prototype kept within the limits above.

## 5. Core domain objects

These are the entities the system tracks. Concrete schemas, columns, and relationships are **TBD** and will be resolved in implementation beads.

| Object                  | Description                                                                              |
|-------------------------|------------------------------------------------------------------------------------------|
| `aoi`                   | Configured area of interest geometry and metadata.                                       |
| `observation`           | One imagery acquisition / date used for analysis. Holds sensor, timestamp, cloud / quality metadata, and source scene identifiers. |
| `index_raster`          | Derived NBR / NDVI raster metadata.                                                      |
| `change_raster`         | ΔNBR / ΔNDVI or anomaly raster metadata.                                                 |
| `disturbance_candidate` | Raw detected disturbance polygon.                                                        |
| `disturbance_event`     | Tracked logging / disturbance event over time.                                           |
| `event_observation`     | Per-date measurement of event area, severity, and growth.                                |
| `manual_review`         | Human validation, notes, uncertainty, false-positive status.                             |
| `methodology_version`   | Processing and detection method provenance.                                              |

Relationships implied by the pipeline:

- An `aoi` has many `observation`s.
- An `observation` produces `index_raster`s; pairs / sequences of observations produce `change_raster`s.
- A `change_raster` yields `disturbance_candidate`s.
- `disturbance_candidate`s are tracked over time into `disturbance_event`s.
- A `disturbance_event` has many `event_observation`s and may have `manual_review`s.
- Every derived artifact is tagged with the `methodology_version` that produced it.

## 6. Cross-cutting properties

- **AOI-first configurability.** Switching deployment to a new AOI is a configuration change, not a code change.
- **Cost discipline.** The prototype targets **$0/month** within always-free tiers; see [§4b](#4b-cost-model-the-0-path). With Earth Engine as the compute substrate, the primary compute cost dimension is **EECU-hours per run** (bounded by the chosen noncommercial tier); cost otherwise scales with AOI size, processing frequency, output retention, local-disk volume, and dashboard egress.
- **Temporal currency.** Scheduling and sensor revisit cadence are designed so detections refresh more often than weekly for small-to-medium AOIs.
- **Provenance.** Every derived artifact is traceable to its source observations and to the `methodology_version` that produced it.

## 7. Out of scope (for this document)

Anything not asserted by the README is out of scope here. In particular: detection algorithm thresholds, polygon-tracking algorithm, dashboard framework choice, authentication model, and concrete database schemas. These are **TBD** and will be settled in implementation beads under the relevant epics.

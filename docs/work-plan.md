# Work plan

This work plan organizes implementation around the pipeline and domain described in the README. It does not invent milestones beyond what the README specifies; ordering, scope, and unknowns reflect the README directly.

Work is organized along three layers — **epics**, **vertical slices**, and **beads**. See `docs/beads.md` for the full model. In short:

- **Epics** are horizontal buckets that say *where code lives* (database, imagery access, dashboard, …). They are tracked as GitHub issues labelled `epic`.
- **Vertical slices** are thin end-to-end threads that say *what working capability shipped*. Each slice is hallway-testable on its own and is tracked as a GitHub milestone.
- **Beads** are agent-sized units of work. Every bead belongs to exactly one epic and one slice.

The epics below are the component buckets; the "Vertical slices" section maps the incremental, demonstrable delivery path across them. The agent-bead issue template is at `.github/ISSUE_TEMPLATE/agent-bead.yml`.

## Guiding properties

Every epic and every bead must respect:

- **AOI-first.** New code paths must work for an arbitrary configured AOI, not just the Solomon Islands.
- **Low cost.** Solutions stay within free-tier / low-cost envelopes for reasonably sized AOIs.
- **Temporal currency.** Designs preserve the ability to refresh detections more often than weekly.
- **Provenance.** Every derived artifact is traceable to its source observations and to a `methodology_version`.

## Epics

The epics below mirror the pipeline and stack described in the README. Each one becomes a tracking issue with sub-issue beads. E1–E13 cover the optical pipeline and its infrastructure; E14–E17 cover the QA masking, confidence model, radar augmentation, and contextual-evidence features the README describes but an earlier revision of this plan omitted.

> **Imagery access & compute substrate: Google Earth Engine.** HLS access, index/change computation, and candidate polygonization run **server-side in Google Earth Engine (EE)**, which exports Cloud Optimized GeoTIFFs to a transient Google Cloud Storage staging area; the GCE VM orchestrates EE, copies each exported COG to its free local disk (the canonical store), and clears the staging object. This keeps the prototype at **$0** (always-free `e2-micro` VM + EE noncommercial tier). See [`docs/architecture.md` §4a](architecture.md#4a-imagery-access--compute-decision-google-earth-engine) and [§4b cost model](architecture.md#4b-cost-model-the-0-path) for the full decision, costs, and risks. The epic *acceptance criteria* below are unchanged (they describe capabilities, not mechanics); the *how* is recorded per bead.

### E1 — Project foundations
Set up the repository so beads can be implemented, tested, and shipped.

- Python project layout, dependency management, lint / format / type configuration.
- Test harness and CI on GitHub Actions for pull requests.
- Documentation conventions (this file, `docs/architecture.md`, `docs/beads.md`).

**Acceptance:** a trivial Python module can be added, tested, and shipped through CI.

### E2 — AOI configuration
Make AOI deployability a first-class, code-free configuration surface.

- Load a configured AOI geometry (per README step 3).
- Validate AOI metadata — resolved: a non-empty `properties.name` plus a valid WGS 84 `Polygon`/`MultiPolygon` (implemented in `src/forest_sentinel/aoi.py`).
- Persist AOI records in PostGIS (`aoi` domain object).

**Acceptance:** the pipeline can be pointed at an arbitrary AOI without code changes.

### E3 — HLS imagery access
Access relevant HLS analysis-ready imagery for a configured AOI (README step 4), **through Google Earth Engine** (`ee.ImageCollection` over `HLSL30` / `HLSS30` v2.0).

- Discover HLS scenes intersecting the AOI for a time window via Earth Engine `filterBounds` / `filterDate`.
- Record `observation`s with sensor, timestamp, cloud / quality metadata, and source scene identifiers (derived from EE image properties).
- Handle availability / cloud-cover gaps without breaking the pipeline.

**Acceptance:** for any configured AOI, the pipeline can enumerate and ingest the relevant HLS observations.

### E4 — Index rasters (NBR, NDVI)
Compute per-observation vegetation / disturbance indices (README step 5).

- `NBR  = (NIR - SWIR2) / (NIR + SWIR2)`
- `NDVI = (NIR - RED)  / (NIR + RED)`
- Write outputs as Cloud Optimized GeoTIFFs (README step 13).
- Record `index_raster` metadata in PostGIS.

**Acceptance:** for each `observation`, NBR and NDVI COGs are produced and indexed.

### E5 — Change products
Compute change products such as ΔNBR / ΔNDVI or other anomaly measures (README step 7).

- Produce `change_raster`s as COGs.
- Record provenance back to source `observation`s and `index_raster`s.

**Acceptance:** for an AOI with sufficient observations, change rasters are produced on schedule.

### E6 — Disturbance candidates
Convert change signals into candidate disturbance polygons (README step 8).

- Persist `disturbance_candidate`s in PostGIS with geometry and provenance.
- Detection thresholds and algorithm — resolved: ΔNBR < −0.25 + `reduceToVectors` + minimum-area filter (see "Open questions → Resolved" and `docs/architecture.md` §5.7).

**Acceptance:** the pipeline emits candidate polygons for real change signals over a test AOI.

### E7 — Event tracking
Track candidate polygons over time as disturbance events (README step 9).

- Maintain `disturbance_event`s spanning multiple dates.
- Capture per-date measurements as `event_observation`s (area, severity, growth).
- Tracking algorithm — resolved: spatial overlap, earliest event wins, same methodology version only (see `docs/architecture.md` §5.9).

**Acceptance:** repeated runs over the same AOI produce a stable, growing record of events with per-date measurements.

### E8 — Manual review
Allow humans to validate detections (README domain object `manual_review`).

- Record validation, notes, uncertainty, false-positive status.
- Surface review state in the dashboard.

**Acceptance:** a reviewer can mark an event reviewed, false-positive, or uncertain, and the result persists.

### E9 — Methodology versioning
Tag every derived artifact with the processing / detection method that produced it (README domain object `methodology_version`).

**Acceptance:** every `index_raster`, `change_raster`, `disturbance_candidate`, and `disturbance_event` references a `methodology_version`.

### E10 — Dashboard
Deliver the lightweight web dashboard, backed by PostGIS (README §"Product Deliverable", step 12, and stack).

- Maps, timelines, event detail views, AOI summary metrics.
- Surfaces: where, when first detected, size, expansion rate, status (`new`/`ongoing` today; `resolved`/`uncertain` arrive with Slices 3–4), supporting evidence.
- Framework — resolved: FastAPI + Leaflet (see `docs/architecture.md` §5.10).

**Acceptance:** a user can answer the six core README "Product Deliverable" questions from the dashboard — where, when first detected, size, expansion rate, status, and supporting evidence. (The README's remaining deliverable questions — sensor/method attribution, optical-vs-radar basis, quality-affected confidence, contextual evidence — are delivered by E14–E17, not this epic.)

### E11 — Scheduled execution
Run the end-to-end pipeline on a cron schedule on a Google Compute Engine VM, triggered by GitHub Actions (README steps 1–2).

- Cron trigger configuration.
- VM provisioning / runner wiring.
- Run logging and failure handling.

**Acceptance:** scheduled runs refresh the dashboard without manual intervention.

> **Status: substantially delivered ahead of Slice 3.** Unattended scheduling ships today as a
> systemd timer on the VM (`scripts/systemd/forest-sentinel-pipeline.timer`, daily 03:00 UTC,
> installed by `vm_setup.sh`) with failure handling via the CLI's nonzero exit and `journalctl`
> logging; VM provisioning is automated end to end (see E18). The GitHub-Actions cron path
> exists as `.github/workflows/scheduled-run.yml` (WIF-authenticated, shipped **disabled**);
> enabling it is the remaining E11 work.

### E12 — Raster storage layout
Store COGs on the **VM's local filesystem** (e.g. `/data/cogs/`) with a deterministic layout, behind a storage abstraction, for $0 cost. Earth Engine exports COGs to a transient GCS staging area (`Export.image.toCloudStorage`); the abstraction owns the EE export-task lifecycle (submit, poll, locate output), copies the finished COG to local disk, clears the staging object, and provides the local COG path to the metadata catalog. (Future path: keep COGs in GCS once volume outgrows the free disk — a backend swap behind the same interface.)

**Acceptance:** the pipeline lands COGs on local disk through the storage abstraction with a deterministic layout, GCS staging is cleared after each copy, and the abstraction exposes export-task status so the orchestrator can wait for completion before ingest.

### E13 — Database (PostgreSQL + PostGIS)
Stand up PostgreSQL + PostGIS on the GCE VM and persist the domain objects from the README.

- Schemas for `aoi`, `observation`, `index_raster`, `change_raster`, `disturbance_candidate`, `disturbance_event`, `event_observation`, `manual_review`, `methodology_version`.
- Migration / versioning approach — resolved: SQLAlchemy 2.0 + GeoAlchemy2 + Alembic (see "Open questions → Resolved").
- Future managed path: Cloud SQL for PostgreSQL with PostGIS.

**Acceptance:** all domain objects are persisted in PostGIS and queryable by the pipeline and dashboard.

### E14 — QA masking
Mask low-quality pixels so detections carry honest quality metadata (README step 6, domain object `quality_mask`).

> **Status:** the core of this epic shipped early, inside Slice 1 (bead #54): Fmask masking is
> applied before index computation and the baseline, and `quality_mask` /
> `valid_pixel_fraction` are persisted (see `docs/architecture.md` §5.4). What remains is
> surfacing mask coverage in the dashboard (Slice 4).

- Apply the HLS `Fmask` QA layer to mask cloud, cloud shadow, snow/ice, and high aerosol — water and low/moderate aerosol are deliberately kept (`docs/architecture.md` §5.4). *(shipped)*
- Persist `quality_mask` metadata and retain it on downstream `observation`s and index / change products. *(shipped)*
- Surface mask coverage so the dashboard can distinguish strong evidence from obscured observations. *(remaining)*

**Acceptance:** index and change products are computed over masked inputs, and every derived artifact records the quality conditions it was produced under.

### E15 — Confidence model
Assign and explain a confidence level for each detection (README §"Evidence Fusion and Confidence", domain object `confidence_assessment`).

- Combine change magnitude, persistence across observations, optical / radar agreement, quality conditions, currency, and contextual proximity into a transparent score.
- Persist a `confidence_assessment` explaining why an event received its level.
- Detection thresholds and the scoring rule: **TBD** in beads.
- **Retention constraint:** raster-derived inputs (change magnitude, persistence) must be
  computed in Earth Engine at candidate-extraction time and persisted per candidate (e.g.
  mean/max ΔNBR) — not read from historical COGs, which the retention policy (#80) treats as
  droppable. See `docs/architecture.md` §7.

**Acceptance:** every `disturbance_event` carries a `confidence_assessment` that records the inputs and reasoning behind its level.

### E16 — Radar augmentation
Add Sentinel-1 SAR as the cloud-resilient complementary source (README §"Cloud-Resilient Radar Augmentation", domain objects `sensor_source`, `radar_change_raster`).

- Discover and ingest Sentinel-1 GRD observations for a configured AOI.
- Compute GRD backscatter / intensity change as `radar_change_raster`s.
- Feed radar-confirmed and radar-only `disturbance_candidate`s into the existing event model without changing it.
- SLC-based coherence methods are explicitly out of scope for now.
- **Retention constraint:** as with E15, any radar statistics needed downstream must be
  persisted per candidate at extraction time; `radar_change_raster` COGs stay droppable under
  the retention policy (#80). See `docs/architecture.md` §7.

**Acceptance:** for a configured AOI, the pipeline produces radar change products and radar-derived candidates that flow into the same `disturbance_event` tracking as optical candidates.

### E17 — Context layers
Join legal, administrative, and infrastructure context to disturbance events (README §"Contextual Evidence Layers", domain objects `context_layer`, `event_context`).

- Load configured `context_layer`s (concessions, protected areas, roads, rivers, settlements, mills, ports, …) into PostGIS.
- Compute `event_context` relationships between `disturbance_event`s and contextual features.
- Surface context in the dashboard so detections become reviewable intelligence.

**Acceptance:** disturbance events are joined to configured context layers, and the dashboard can show how a detection relates to concessions, roads, rivers, and other features.

### E18 — Instance deployment & operations
Make standing up (and tearing down) a complete instance a repeatable, largely automated
workflow on the $0 GCP footprint. This epic was delivered outside the bead system (PRs
#68–#73) and is recorded here retroactively; future deployment/ops work is filed as beads
under it.

**Delivered:**

- Template-instance model: create an instance repo with "Use this template"; the deploy
  workflow grafts this repo's history onto it so instances can pull future updates
  (`INSTANCE_DEPLOYMENT.md`).
- Keyless auth end to end: GitHub Actions → GCP via Workload Identity Federation
  (`scripts/setup_wif.sh`, self-healing on re-run), and the VM via its attached service
  account — no service-account keys anywhere (`CREATE_KEY=1` remains for local dev).
- One-click provisioning: `.github/workflows/deploy.yml` runs `setup_gcp.sh` +
  `provision_vm.sh`; the VM self-configures on first boot (`vm_startup.sh` → `vm_setup.sh`)
  and reports status via a guest attribute.
- Committed per-instance config (`config/instance.env`, `config/aoi.geojson`) from which
  the VM regenerates `.env` on every setup run.
- Teardown (`scripts/teardown_gcp.sh`: resource-level or `--nuke`) and a scaling / cost
  analysis with an efficiency roadmap (`docs/scaling.md`).

**Candidate future beads:** an "update from template" workflow in instance repos;
CI-published container images (GHCR) so instances run released artifacts; multi-AOI
scheduling; the `docs/scaling.md` efficiency roadmap (skip unchanged exports, per-scene
region clipping, concurrent export submission).

**Acceptance:** a new instance goes from "Use this template" to a running dashboard with
one manual bootstrap script and one workflow run; teardown removes everything it created.

## Dependency map

The pipeline imposes a natural ordering. Each arrow reads "is required by".

```
E1 Foundations ──► E2 AOI ──► E3 HLS imagery ──► E4 Index rasters ──► E5 Change ──► E6 Candidates ──► E7 Events ──► E10 Dashboard
                                                                                                       │
E13 Database ─────────────────────────────────────────────────────────────────────────────────────────►┤
E12 Raster storage ───────────────────────────────────────────────────────────────────────────────────►┤
E9  Methodology versioning ───────────────────────────────────────────────────────────────────────────►┤
E8  Manual review ────────────────────────────────────────────────────────────────────────────────────►┤
E11 Scheduled execution wraps E2–E10 once they exist.
```

Beads inside each epic must record their dependencies on beads in upstream epics using `Depends on #NNN` references, per `docs/beads.md`.

This map is the *horizontal* view. The "Vertical slices" section below is the *delivery* view: it threads through these epics so that something demonstrable ships at the end of each slice instead of only after the dashboard epic.

## Vertical slices

The epics above describe *where code lives*. Vertical slices describe *what working capability ships and when*. Each slice is a thin end-to-end thread across several epics and ends in a concrete hallway test. Build the thinnest slice first (a "walking skeleton") and deepen it; do not finish one horizontal epic at a time. Each slice is tracked as a GitHub milestone.

Slices 0–2 are complete: Slice 1 shipped as beads #35–#42 (plus QA masking, #54, pulled forward from E14), and Slice 2 shipped event tracking and the FastAPI + Leaflet dashboard (see `docs/architecture.md` §5.9–§5.10 for the resolved designs). Slice 3's *scheduling* half shipped early — the systemd timer (E11) and the full instance-deployment machinery (E18) are live — so its remaining scope is manual review (E8) plus enabling the Actions cron; it still needs a planning pass for the review work. Slices 4–6 are sketched at the table level only.

| Slice | Capability delivered | Epics touched | Hallway test |
|-------|----------------------|---------------|--------------|
| **Slice 0 — Walking skeleton** | Project foundations plus the thinnest end-to-end thread: load a configured AOI, persist it, report it. | E1, E13, E2, E11 (thin) | `forest-sentinel run --aoi <fixture>` loads a configured AOI, persists it to PostGIS, and prints a summary; CI is green on pull requests. |
| **Slice 1 — Optical change detection** | AOI → HLS observations → NBR/NDVI COGs → ΔNBR vs. baseline → candidate disturbance polygons in PostGIS. | E3, E4, E5, E6, E12, E9, E13 | Run over a small test AOI and eyeball the emitted candidate polygons on a map or GeoJSON dump. |
| **Slice 2 — Events + dashboard** | Track candidates over time into disturbance events with per-date measurements; stand up the lightweight web dashboard. | E7, E10 | Open the dashboard and see events on a map with timelines, sizes, and status. |
| **Slice 3 — Scheduling + review** | Run the pipeline on a schedule (systemd timer: **shipped**; Actions cron: scaffolded, disabled); let a human validate detections. | E11, E8 | A scheduled run refreshes the dashboard unattended (✓ via the timer); a reviewer can mark an event reviewed, false-positive, or uncertain. |
| **Slice 4 — QA & confidence hardening** | A transparent confidence model on outputs, plus the remaining QA hardening (Fmask masking itself shipped early, inside Slice 1 — see E14; what remains is surfacing quality metadata in the dashboard). | E14 (residual), E15 | Detections show honest quality metadata and an explained confidence level in the dashboard. |
| **Slice 5 — Radar augmentation** | Sentinel-1 GRD backscatter change feeding the existing event model. | E16, E13 | The pipeline produces radar change products and radar-derived candidates for a configured AOI. |
| **Slice 6 — Context layers** | Concessions, roads, rivers, and other context joined to disturbance events. | E17, E10 | The dashboard shows how a detection relates to concessions, protected areas, roads, and rivers. |

Every epic appears in at least one slice. E1, E2, E11, E12, and E13 are touched by multiple slices because foundational and infrastructure epics are deepened incrementally rather than completed up front.

## Slice bead breakdowns

### Slice 1 — bead breakdown (filed)

Slice 1 is decomposed into eight beads filed under the **Slice 1** milestone (issues #35–#42), each linked as a sub-issue of its epic:

- **#35** — Methodology versioning: `methodology_version` table, model, helper; the stored value captures the EE script version / asset IDs for reproducibility (E9).
- **#36** — Storage abstraction: EE export-task lifecycle + GCS staging → copy to local disk + clear staging, `storage.py` (E12).
- **#37** — `observation` table + model (E13).
- **#38** — HLS scene discovery → `observation` records, via **Earth Engine** (`ee.ImageCollection('NASA/HLS/HLSL30/v002' | 'HLSS30/v002').filterBounds(aoi).filterDate(...)`) (E3). Depends on #37.
- **#39** — `index_raster` table + NBR/NDVI as EE band expressions → COGs exported via GCS staging to local disk (E4). Depends on #35, #36, #38.
- **#40** — `change_raster` table + trailing-median baseline (`ImageCollection.median()`) + ΔNBR/ΔNDVI in EE (E5). Depends on #39.
- **#41** — `disturbance_candidate` table + threshold (`gt`/`lt`) + `reduceToVectors` polygonize + configurable area filter (E6). Depends on #40.
- **#42** — Wire the Slice 1 pipeline into `forest-sentinel run` end-to-end with the **async EE export lifecycle** (submit → poll → ingest) (E3–E6). Depends on #38–#41.

Architectural decisions baked into these beads (see [`docs/architecture.md` §4a](architecture.md#4a-imagery-access--compute-decision-google-earth-engine)): HLS access **and compute run server-side in Google Earth Engine** over `HLSL30` / `HLSS30` v2.0; change product = ΔNBR/ΔNDVI against a per-pixel trailing-median baseline; candidate extraction = threshold + polygonize + configurable area filter; EE exports COGs to a transient GCS staging area that a `storage.py` abstraction copies to the VM's local disk and clears (for $0 storage), while also owning the export-task lifecycle. Each bead records its decision in `docs/architecture.md`.

> **Optional Slice 1 addition.** Because the `Fmask` QA band ships with both HLS collections, QA masking (E14) is nearly free in EE and may be pulled into Slice 1 as an additional bead, rather than waiting for Slice 4. Decide during the Slice 1 re-plan.

### Slice 2 — shipped

Slice 2 is implemented and merged. The design questions its planning pass resolved (recorded in
`docs/architecture.md` §5.9–§5.10):

- Candidate→event tracking algorithm: **spatial overlap** — a candidate that intersects an
  existing event's footprint extends it (earliest event wins), otherwise it starts a new event.
- Dashboard framework: **FastAPI + Leaflet**, self-hosted on the VM.
- Dashboard auth / access model: **read-only, unauthenticated** in this slice; reach it over an
  SSH tunnel (see `DEPLOYMENT.md` §6). Auth is revisited with the review workflows (Slice 3).

Delivered: `disturbance_event` + `event_observation` tables and models (migration `0008`),
`events.py` tracking wired into `forest-sentinel run`, and the dashboard backend / API + map UI.

### Slice 3 — bead outline

> **OUTLINE ONLY — not ready for agentic work.** These beads are a sketch. Before any are implemented, this slice needs a dedicated planning pass that (a) resolves the open design questions below and (b) expands each into a full `agent-bead` issue. Do not file or start these as-is.

Depends on Slice 2. Open questions to resolve first: the review UI shape (tied to the dashboard framework); the review **auth / access model**.

- `manual_review` table + model (E8, E13).
- Review actions surfaced in the dashboard (E8, E10).
- GitHub Actions cron workflow + scheduled pipeline run (E11) — *largely shipped:* the
  systemd-timer scheduler is live and `.github/workflows/scheduled-run.yml` exists
  (WIF-based, disabled); the remaining bead is enabling/validating the Actions path.
- Run logging + failure handling (E11) — *shipped:* nonzero exit on export failures,
  `journalctl` logging via the systemd units.

## Open questions

These are points the README does not resolve. They should be answered inside the relevant epic and recorded in `docs/architecture.md` once decided.

- Concrete table schema for `manual_review` (Slice 3).
- Authentication / access model for the review workflows (Slice 3; the Slice 2 dashboard shipped read-only and unauthenticated).
- Retention policy for COGs and observations — partially resolved: tracked as the
  automated-retention bead (#80, epic E19); the design constraints (COGs are outputs never
  inputs; never prune inside `WINDOW_DAYS` + a baseline margin; keep the database rows as the
  reproduction recipe; raster-derived statistics must be persisted at extraction time) are
  recorded in `docs/architecture.md` §7.

**Resolved:**

- `disturbance_event` / `event_observation` schemas, the candidate→event tracking algorithm (spatial overlap, earliest event wins), and the dashboard framework and hosting (FastAPI + Leaflet on the VM, read-only, no auth) — Slice 2 planning pass; see `docs/architecture.md` §5.9–§5.10.

- Migration tooling for PostgreSQL + PostGIS — SQLAlchemy 2.0 + GeoAlchemy2 + Alembic (bead #22; see `docs/architecture.md`).
- Imagery access & raster compute — **Google Earth Engine** (server-side): access HLS v2.0 `HLSL30` / `HLSS30`, compute indices, change products, and candidates server-side, and export COGs via a transient GCS staging area to the VM's local disk. (See `docs/architecture.md` §4a/§4b; recorded by beads #38–#41.)
- Change products & detection — ΔNBR/ΔNDVI against a per-pixel trailing-median baseline; candidates by threshold + polygonize + configurable area filter (Slice 1 plan; recorded by beads #40, #41).
- Raster storage layout — COGs exported by Earth Engine to a transient GCS staging area, then copied to the VM's local disk (canonical, $0) and the staging object cleared, through a `storage.py` abstraction that also owns the EE export-task lifecycle. GCS-resident storage is the future path once volume outgrows the free disk (Slice 1 plan; recorded by bead #36; see `docs/architecture.md` §4b).

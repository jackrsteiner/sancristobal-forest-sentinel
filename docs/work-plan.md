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

### E1 — Project foundations
Set up the repository so beads can be implemented, tested, and shipped.

- Python project layout, dependency management, lint / format / type configuration.
- Test harness and CI on GitHub Actions for pull requests.
- Documentation conventions (this file, `docs/architecture.md`, `docs/beads.md`).

**Acceptance:** a trivial Python module can be added, tested, and shipped through CI.

### E2 — AOI configuration
Make AOI deployability a first-class, code-free configuration surface.

- Load a configured AOI geometry (per README step 3).
- Validate AOI metadata (name, geometry, CRS, etc. — fields **TBD**).
- Persist AOI records in PostGIS (`aoi` domain object).

**Acceptance:** the pipeline can be pointed at an arbitrary AOI without code changes.

### E3 — HLS imagery access
Access relevant HLS analysis-ready imagery for a configured AOI (README step 4).

- Discover HLS scenes intersecting the AOI for a time window.
- Record `observation`s with sensor, timestamp, cloud / quality metadata, and source scene identifiers.
- Handle availability / cloud-cover gaps without breaking the pipeline.

**Acceptance:** for any configured AOI, the pipeline can enumerate and ingest the relevant HLS observations.

### E4 — Index rasters (NBR, NDVI)
Compute per-observation vegetation / disturbance indices (README step 5).

- `NBR  = (NIR - SWIR2) / (NIR + SWIR2)`
- `NDVI = (NIR - RED)  / (NIR + RED)`
- Write outputs as Cloud Optimized GeoTIFFs (README step 10).
- Record `index_raster` metadata in PostGIS.

**Acceptance:** for each `observation`, NBR and NDVI COGs are produced and indexed.

### E5 — Change products
Compute change products such as ΔNBR / ΔNDVI or other anomaly measures (README step 6).

- Produce `change_raster`s as COGs.
- Record provenance back to source `observation`s and `index_raster`s.

**Acceptance:** for an AOI with sufficient observations, change rasters are produced on schedule.

### E6 — Disturbance candidates
Convert change signals into candidate disturbance polygons (README step 7).

- Persist `disturbance_candidate`s in PostGIS with geometry and provenance.
- Detection thresholds and algorithm: **TBD** in beads.

**Acceptance:** the pipeline emits candidate polygons for real change signals over a test AOI.

### E7 — Event tracking
Track candidate polygons over time as disturbance events (README step 8).

- Maintain `disturbance_event`s spanning multiple dates.
- Capture per-date measurements as `event_observation`s (area, severity, growth).
- Tracking algorithm: **TBD** in beads.

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
Deliver the lightweight web dashboard, backed by PostGIS (README §"Product Deliverable", step 9, and stack).

- Maps, timelines, event detail views, AOI summary metrics.
- Surfaces: where, when first detected, size, expansion rate, status (new / ongoing / resolved / uncertain), supporting evidence.
- Framework choice: **TBD**.

**Acceptance:** a user can answer all six questions listed in the README "Product Deliverable" section from the dashboard.

### E11 — Scheduled execution
Run the end-to-end pipeline on a cron schedule on a Google Compute Engine VM, triggered by GitHub Actions (README steps 1–2).

- Cron trigger configuration.
- VM provisioning / runner wiring.
- Run logging and failure handling.

**Acceptance:** scheduled runs refresh the dashboard without manual intervention.

### E12 — Raster storage layout
Store COGs in the prototype location (`/data/cogs/` on the VM) with a layout that allows a future move to Google Cloud Storage without code changes outside a storage abstraction.

**Acceptance:** the pipeline writes COGs to the prototype location, and the storage interface is the only place that needs to change to switch to GCS.

### E13 — Database (PostgreSQL + PostGIS)
Stand up PostgreSQL + PostGIS on the GCE VM and persist the domain objects from the README.

- Schemas for `aoi`, `observation`, `index_raster`, `change_raster`, `disturbance_candidate`, `disturbance_event`, `event_observation`, `manual_review`, `methodology_version`.
- Migration / versioning approach: **TBD**.
- Future managed path: Cloud SQL for PostgreSQL with PostGIS.

**Acceptance:** all domain objects are persisted in PostGIS and queryable by the pipeline and dashboard.

### E14 — QA masking
Mask low-quality pixels so detections carry honest quality metadata (README step 6, domain object `quality_mask`).

- Apply HLS QA layers to mask cloud, cloud shadow, haze, water, and missing data.
- Persist `quality_mask` metadata and retain it on downstream `observation`s and index / change products.
- Surface mask coverage so the dashboard can distinguish strong evidence from obscured observations.

**Acceptance:** index and change products are computed over masked inputs, and every derived artifact records the quality conditions it was produced under.

### E15 — Confidence model
Assign and explain a confidence level for each detection (README §"Evidence Fusion and Confidence", domain object `confidence_assessment`).

- Combine change magnitude, persistence across observations, optical / radar agreement, quality conditions, currency, and contextual proximity into a transparent score.
- Persist a `confidence_assessment` explaining why an event received its level.
- Detection thresholds and the scoring rule: **TBD** in beads.

**Acceptance:** every `disturbance_event` carries a `confidence_assessment` that records the inputs and reasoning behind its level.

### E16 — Radar augmentation
Add Sentinel-1 SAR as the cloud-resilient complementary source (README §"Cloud-Resilient Radar Augmentation", domain objects `sensor_source`, `radar_change_raster`).

- Discover and ingest Sentinel-1 GRD observations for a configured AOI.
- Compute GRD backscatter / intensity change as `radar_change_raster`s.
- Feed radar-confirmed and radar-only `disturbance_candidate`s into the existing event model without changing it.
- SLC-based coherence methods are explicitly out of scope for now.

**Acceptance:** for a configured AOI, the pipeline produces radar change products and radar-derived candidates that flow into the same `disturbance_event` tracking as optical candidates.

### E17 — Context layers
Join legal, administrative, and infrastructure context to disturbance events (README §"Contextual Evidence Layers", domain objects `context_layer`, `event_context`).

- Load configured `context_layer`s (concessions, protected areas, roads, rivers, settlements, mills, ports, …) into PostGIS.
- Compute `event_context` relationships between `disturbance_event`s and contextual features.
- Surface context in the dashboard so detections become reviewable intelligence.

**Acceptance:** disturbance events are joined to configured context layers, and the dashboard can show how a detection relates to concessions, roads, rivers, and other features.

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

Slices 0–3 are defined now. Slices 4–6 are sketched and will be firmed up when reached, because their algorithms and framework choices are still open (see "Open questions").

| Slice | Capability delivered | Epics touched | Hallway test |
|-------|----------------------|---------------|--------------|
| **Slice 0 — Walking skeleton** | Project foundations plus the thinnest end-to-end thread: load a configured AOI, persist it, report it. | E1, E13, E2, E11 (thin) | `forest-sentinel run --aoi <fixture>` loads a configured AOI, persists it to PostGIS, and prints a summary; CI is green on pull requests. |
| **Slice 1 — Optical change detection** | AOI → HLS observations → NBR/NDVI COGs → ΔNBR vs. baseline → candidate disturbance polygons in PostGIS. | E3, E4, E5, E6, E12, E9, E13 | Run over a small test AOI and eyeball the emitted candidate polygons on a map or GeoJSON dump. |
| **Slice 2 — Events + dashboard** | Track candidates over time into disturbance events with per-date measurements; stand up the lightweight web dashboard. | E7, E10 | Open the dashboard and see events on a map with timelines, sizes, and status. |
| **Slice 3 — Scheduling + review** | Run the pipeline on a GitHub Actions cron schedule; let a human validate detections. | E11, E8 | A scheduled run refreshes the dashboard unattended; a reviewer can mark an event reviewed, false-positive, or uncertain. |
| **Slice 4 — QA & confidence hardening** | Cloud/shadow/haze masking on inputs and a transparent confidence model on outputs. | E14, E15 | Detections show honest quality metadata and an explained confidence level in the dashboard. |
| **Slice 5 — Radar augmentation** | Sentinel-1 GRD backscatter change feeding the existing event model. | E16, E13 | The pipeline produces radar change products and radar-derived candidates for a configured AOI. |
| **Slice 6 — Context layers** | Concessions, roads, rivers, and other context joined to disturbance events. | E17, E10 | The dashboard shows how a detection relates to concessions, protected areas, roads, and rivers. |

Every epic appears in at least one slice. E1, E2, E11, E12, and E13 are touched by multiple slices because foundational and infrastructure epics are deepened incrementally rather than completed up front.

## Open questions

These are points the README does not resolve. They should be answered inside the relevant epic and recorded in `docs/architecture.md` once decided.

- Concrete table schemas for each domain object.
- Detection thresholds and the candidate-polygon extraction algorithm.
- The polygon-tracking algorithm used to assemble events from candidates.
- Dashboard framework and hosting.
- Authentication / access model for the dashboard and review workflows.
- Retention policy for COGs and observations.

_Resolved: migration tooling for PostgreSQL + PostGIS — SQLAlchemy 2.0 + GeoAlchemy2 + Alembic (see `docs/architecture.md`, bead #22)._

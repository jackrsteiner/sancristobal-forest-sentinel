# Copilot instructions

These instructions apply to all AI coding agents working in this repository (GitHub Copilot, Claude, etc.). They are derived from the project README, `docs/architecture.md`, `docs/work-plan.md`, and `docs/beads.md`. When those documents and these instructions disagree, the documents win — open a bead to fix the drift.

## What this project is

Open Forest Sentinel is a generalized, low-cost forest disturbance monitoring system for a configurable Area of Interest (AOI). The user-facing deliverable is a lightweight dashboard backed by PostGIS; derived rasters are internal artifacts that power detection, tracking, visualization, and review.

Read `README.md` and `docs/architecture.md` before making non-trivial changes.

## Non-negotiable properties

Every change must respect these properties from the README:

- **AOI-first.** New code paths must work for an arbitrary configured AOI, not just the Solomon Islands. Do not hardcode geometries, country names, CRS choices, or scene identifiers.
- **Low cost.** Stay inside free-tier / low-cost envelopes. Do not introduce paid services, always-on infrastructure, or heavyweight dependencies without an explicit decision recorded in `docs/architecture.md`.
- **Temporal currency.** Designs must preserve the ability to refresh detections more often than weekly for small-to-medium AOIs.
- **Provenance.** Every derived artifact (`index_raster`, `change_raster`, `disturbance_candidate`, `disturbance_event`) must be traceable to its source `observation`s and to a `methodology_version`.

## Stack

Match the prototype stack described in the README. Do not swap components without an explicit decision.

- **Language:** Python.
- **Imagery access & raster compute:** Google Earth Engine (`earthengine-api`). HLS access, indices, change products, and candidate polygonization run server-side in EE, which exports COGs to GCS. See `docs/architecture.md` §4a.
- **Local raster handling:** rasterio, GDAL, rio-cogeo (ingest / validate EE-exported COGs); numpy.
- **Imagery source:** NASA HLS (`HLSL30` / `HLSS30`), accessed via Google Earth Engine.
- **Raster output format:** Cloud Optimized GeoTIFF (written by EE export).
- **Raster storage:** local VM filesystem (e.g. `/data/cogs/`) for $0 cost. EE exports to a transient GCS staging area, which the storage layer copies to local disk and then clears. Isolate storage access, the export-task lifecycle, and the copy-to-disk step behind one interface.
- **Database:** PostgreSQL + PostGIS on the same Compute Engine VM (prototype). The future path is Cloud SQL for PostgreSQL with PostGIS.
- **Compute:** Google Compute Engine VM (orchestrates Earth Engine and ingests results).
- **Scheduler:** GitHub Actions cron.
- **Dashboard:** lightweight web application backed by PostGIS.
- **Versioning / CI:** GitHub.

## Domain objects

The system tracks: `aoi`, `observation`, `index_raster`, `change_raster`, `disturbance_candidate`, `disturbance_event`, `event_observation`, `manual_review`, `methodology_version`. Use these names in code, schemas, and docs. If you need a new domain object, propose it in a bead — do not invent one inline.

## How work is organized

Work is organized along three layers: **epics** (horizontal buckets that say where code lives, tracked as GitHub issues), **vertical slices** (thin end-to-end threads that say what working capability shipped, tracked as GitHub milestones), and **beads** (small, agent-sized issues). Every bead belongs to exactly one epic and one slice. Prefer building the thinnest end-to-end slice first and deepening it, rather than completing one horizontal epic at a time. See `docs/beads.md` for the model and `docs/work-plan.md` for this project's epics and slices.

When starting work as an agent:

1. Find or open the bead you are implementing. Do not start work that is not represented by a bead.
2. Confirm the bead's epic, vertical slice, and dependencies. If a `Depends on` bead is not yet merged, stop and surface the conflict.
3. Implement the bead's in-scope items only. Anything in the "Out of scope" section belongs in a separate bead.
4. Add tests for every code path you add or change.
5. Make sure the full test suite passes locally and in CI.
6. Update documentation if the change is user- or operator-visible.
7. Open a pull request that references the bead. The PR is merged only when the bead's definition-of-done checklist is fully satisfied.

## Coding rules

- **No drive-by changes.** If you find unrelated issues while working a bead, open a new bead for them. Do not bundle them into the current PR.
- **No new abstractions without a use site.** Two callers minimum before a helper is extracted.
- **No hardcoded AOI assumptions.** Reject patterns that would only work for the initial Solomon Islands deployment.
- **No paid or always-on infrastructure** without an explicit decision recorded in `docs/architecture.md`.
- **Provenance on every derived artifact.** When writing rasters or persisting detections, include the source `observation` references and the `methodology_version`.
- **COGs only.** Raster outputs are Cloud Optimized GeoTIFFs.
- **Prefer editing existing files** to creating new ones.
- **Comments explain why, not what.** Skip comments unless they record a non-obvious constraint.

## Tests

- New and changed code must be covered by tests.
- Tests must pass locally and in CI before a bead is closed.
- Use small, deterministic raster fixtures for index, change, and detection logic — do not depend on live Earth Engine or HLS calls in unit tests; mock the Earth Engine client.
- If you cannot test something, say so explicitly in the bead's test plan. "I'll add tests later" is not acceptable; track it as a `Blocks` bead.

## Open questions

The README does not resolve every design point. The currently open ones are listed in `docs/work-plan.md` under "Open questions". When you encounter one of those during implementation, do not silently pick an answer — record the decision in a bead and update `docs/architecture.md`.

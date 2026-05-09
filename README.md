# Forest-Watch

This project is a generalized, low-cost **forest disturbance monitoring system** for a configurable **Area of Interest (AOI)**. The initial deployment target is the **Solomon Islands**, but arbitrary AOI deployability is a first-class feature: the same system should work for other countries, regions, protected areas, watersheds, concessions, or custom polygons through configuration rather than code changes.

For an appropriately constrained AOI, the system should be able to run at near-zero or very low infrastructure cost because free-tier / low-cost cloud resources are sufficient for compute, database, and prototype raster storage.

A key value proposition is **observation currency**: by using openly available HLS imagery with frequent Landsat/Sentinel revisit cadence, the system should deliver forest-disturbance insights that are typically **less than one week old** and refreshed **more frequently than weekly**, subject to cloud cover, data availability, and AOI size.

## Product Deliverable

The final user-facing deliverable is not just a set of derived raster files. It is a lightweight dashboard that helps users understand:

- where likely logging or forest disturbance is happening
- when disturbance was first detected
- how large the affected area is
- how quickly the disturbance is expanding
- which detections are new, ongoing, resolved, or uncertain
- what satellite-derived evidence supports each detection

The derived raster products are internal analytical artifacts used to power detection, tracking, visualization, and review.

## Data Pipeline

1. **GitHub Actions** runs on a schedule and triggers the pipeline.
2. A **Google Compute Engine VM** executes the Python processing job.
3. The pipeline loads the configured AOI geometry.
4. The pipeline accesses relevant **HLS analysis-ready imagery**.
5. Python raster modules compute vegetation/disturbance indices such as:
   - NBR = `(NIR - SWIR2) / (NIR + SWIR2)`
   - NDVI = `(NIR - RED) / (NIR + RED)`
6. The system computes change products such as ΔNBR / ΔNDVI or other anomaly measures.
7. Change signals are converted into candidate disturbance polygons.
8. Candidate polygons are tracked over time as disturbance events.
9. Outputs are exposed through a dashboard with maps, timelines, event detail views, and AOI summary metrics.
10. Raster artifacts are written as **Cloud Optimized GeoTIFFs (COGs)**.
11. Metadata, provenance, AOIs, detections, and event histories are stored in **PostgreSQL + PostGIS**.

## Prototype Technology Stack

- **Scheduler / trigger:** GitHub Actions cron
- **Compute:** Google Compute Engine VM
- **Prototype database:** PostgreSQL + PostGIS running on the same Compute Engine VM
- **Future managed database option:** Cloud SQL for PostgreSQL with PostGIS
- **Language:** Python
- **Raster processing:** rasterio, GDAL, numpy, rio-cogeo
- **Imagery source:** NASA HLS
- **Raster output format:** Cloud Optimized GeoTIFF
- **Prototype raster storage:** local VM filesystem, e.g. `/data/cogs/`
- **Future raster storage:** Google Cloud Storage
- **Dashboard:** lightweight web application backed by PostGIS
- **Versioning / CI:** GitHub

## Core Domain Objects

- `aoi`: configured area of interest geometry and metadata
- `observation`: one imagery acquisition/date used for analysis, including sensor, timestamp, cloud/quality metadata, and source scene identifiers
- `index_raster`: derived NBR / NDVI raster metadata
- `change_raster`: ΔNBR / ΔNDVI or anomaly raster metadata
- `disturbance_candidate`: raw detected disturbance polygon
- `disturbance_event`: tracked logging/disturbance event over time
- `event_observation`: per-date measurement of event area, severity, and growth
- `manual_review`: human validation, notes, uncertainty, false-positive status
- `methodology_version`: processing and detection method provenance

## Core Value Proposition

The project is a reusable template for deploying a lightweight satellite-derived forest monitoring system over any reasonably sized AOI. Cost scales primarily with AOI size, processing frequency, output retention, raster storage volume, and dashboard usage.

The product value comes from turning free, openly available HLS imagery into timely, actionable dashboard views showing likely logging activity, detection timing, expansion rate, and supporting evidence. Its practical advantage depends not only on low cost, but also on **temporal currency**: frequent sensor revisit and scheduled processing allow detections to be refreshed more often than weekly and kept close to real time for small-to-medium AOIs.

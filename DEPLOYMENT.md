# Deployment & operations guide

This guide takes you from a fresh clone to a scheduled Open Forest Sentinel
deployment running over **your own Area of Interest (AOI)** at $0/month on Google
Cloud's always-free tiers. It scripts every step that can be automated; the helper
scripts live in [`scripts/`](scripts/).

For *what the system is and how it works*, read [`README.md`](README.md) and
[`docs/architecture.md`](docs/architecture.md). This document is the operational
counterpart: how to configure, provision, secure, and automate it.

> **No local tooling?** The same provisioning can be done entirely from a web
> browser via Google Cloud Shell — follow
> [`docs/cloud-shell-setup.md`](docs/cloud-shell-setup.md) instead.

---

## What runs today vs. what's planned

**Works today:**

- **AOI loading** — validate and persist a configured AOI (the Slice 0 walking skeleton).
- **Optical-change pipeline** — discover NASA HLS observations through Google Earth
  Engine, compute Fmask-masked NBR/NDVI and ΔNBR/ΔNDVI against a trailing-median
  baseline, polygonize disturbance candidates, track them into events, and persist
  everything to PostGIS. COGs are exported via a transient GCS staging area onto the
  VM's local disk.
- **Dashboard** — a read-only FastAPI + Leaflet web app over the PostGIS catalog.
- **Scheduling** — run the pipeline unattended on the VM via a systemd timer (this guide).

**Intended direction** (see [`docs/work-plan.md`](docs/work-plan.md)): a native
GitHub Actions cron scheduler and manual review (Slice 3), QA/confidence hardening
(Slice 4), Sentinel-1 radar augmentation (Slice 5), context layers (Slice 6), and a
future move to Cloud SQL / GCS-as-canonical storage. Forward-looking pieces in this
guide are labelled as such.

---

## 1. Prerequisites

| Need | Why | Notes |
|------|-----|-------|
| Python 3.12 | runtime | pinned in `.python-version` |
| [uv](https://docs.astral.sh/uv/) | dependency + venv management | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Docker | local PostgreSQL + PostGIS | local development only |
| A GCP project with billing enabled | Earth Engine + Storage + Compute | billing must be on even though usage stays in free tiers |
| [`gcloud` CLI](https://cloud.google.com/sdk/docs/install) | provisioning | authenticate with `gcloud auth login` |
| Earth Engine registration | imagery access & compute | one interactive step, see §3 |

---

## 2. Quick start (local)

```sh
git clone https://github.com/jackrsteiner/open-forest-sentinel.git
cd open-forest-sentinel
./scripts/bootstrap_local.sh
```

`bootstrap_local.sh` runs `uv sync`, starts PostgreSQL + PostGIS via `docker compose`,
waits for it to become healthy, and applies all database migrations. Then:

```sh
# Load an AOI (no cloud credentials required):
uv run forest-sentinel run --aoi examples/aoi-sample.geojson

# Launch the dashboard at http://localhost:8000 :
uv run uvicorn forest_sentinel.dashboard.app:app --port 8000
```

The full optical-change pipeline (`--since/--until`, §4) additionally needs Earth
Engine credentials — set those up in §3 first.

---

## 3. Configure credentials & store them safely

The pipeline authenticates to Google Earth Engine and Cloud Storage with a **GCP
service account**. Provision everything with:

```sh
export PROJECT_ID=your-gcp-project-id
./scripts/setup_gcp.sh
```

`setup_gcp.sh` is idempotent and:

- enables the `earthengine`, `storage`, and `compute` APIs,
- creates the `forest-sentinel` service account and grants it `earthengine.writer`
  and `storage.objectAdmin`,
- creates the transient GCS staging bucket with a 1-day delete lifecycle (staging is
  short-lived by design — see [`docs/architecture.md`](docs/architecture.md) §4b),
- creates a service-account key at `./gcp-service-account.json` (mode `600`).

**One manual step Earth Engine requires:** register the project at
<https://code.earthengine.google.com/register>, choosing the **noncommercial /
unpaid** usage. EE access cannot be enabled non-interactively.

### Storing secrets safely

The service-account key is a credential — treat it like a password.

- **Local / VM:** keep the key as a file with mode `600`. `.gitignore` already
  excludes `.env`, `gcp-service-account.json`, and `*-service-account.json`, so they
  cannot be committed. Never paste keys into code, docs, or commit messages.
- **Configuration:** copy `.env.example` to `.env` and fill it in. Everything (the
  app and the scripts) reads the same variables:

  | Variable | Purpose | Required for |
  |----------|---------|--------------|
  | `FOREST_SENTINEL_DATABASE_URL` | PostgreSQL connection | always (defaults to local docker compose) |
  | `FOREST_SENTINEL_GEE_PROJECT` | EE-registered GCP project id | pipeline |
  | `FOREST_SENTINEL_GCS_STAGING_BUCKET` | transient COG export bucket | pipeline |
  | `FOREST_SENTINEL_COG_ROOT` | local canonical COG dir (default `data/cogs/`) | pipeline |
  | `GOOGLE_APPLICATION_CREDENTIALS` | path to the service-account key | pipeline |

- **Automation (CI):** don't put the key on disk in CI. Store it as an encrypted
  **GitHub Actions secret** and let the workflow authenticate from it (§7).
- **Hardening (optional):** store the key in **GCP Secret Manager** and fetch it at
  boot instead of keeping a long-lived file on the VM.

---

## 4. Configure your AOI

An AOI is a GeoJSON file with a single `Feature` (or a `FeatureCollection` holding
exactly one), in WGS 84 (EPSG:4326), with a non-empty `properties.name` and a valid,
non-empty `Polygon`/`MultiPolygon` geometry. See `examples/aoi-sample.geojson`.

Generate one from a bounding box (validated with the app's own loader, so the output
is guaranteed to be accepted by the pipeline):

```sh
uv run python scripts/make_aoi.py \
    --bbox 159.0 -9.6 159.3 -9.3 \
    --name "Guadalcanal North Coast" \
    --out aois/guadalcanal.geojson
```

Keep AOIs small to stay inside the free tiers (the `e2-micro` VM has 1 GB RAM and a
30 GB disk shared with Postgres and the COG store).

### Running the pipeline

```sh
uv run forest-sentinel run \
    --aoi aois/guadalcanal.geojson \
    --since 2026-01-01 --until 2026-02-01
```

Adding `--since/--until` switches from the Slice 0 load to the full pipeline. Tuning flags:

| Flag | Meaning | Default |
|------|---------|---------|
| `--since` / `--until` | observation window (`--since` inclusive, `--until` exclusive) | — |
| `--baseline-window` | number of prior observations in the trailing-median baseline | `5` |
| `--threshold` | ΔNBR drop below which a pixel is flagged (negative) | `-0.25` |
| `--min-area` | minimum candidate polygon area, m² | `4500` (≈ 0.45 ha) |
| `--methodology-name` / `--methodology-version` | provenance labels recorded per run | `optical-change` / `1.0.0` |
| `--gee-project` | overrides `FOREST_SENTINEL_GEE_PROJECT` | env |

The run is **idempotent at the AOI level** (it reuses the AOI row by name) and blocks
while polling each Earth Engine export to completion.

---

## 5. Spin up the $0 infrastructure (GCP VM)

Create the always-free `e2-micro` VM that hosts the orchestrator, PostgreSQL +
PostGIS, and the canonical COG store. The always-free tier only applies in
`us-west1` / `us-central1` / `us-east1`.

```sh
export PROJECT_ID=your-gcp-project-id
export ZONE=us-central1-a
./scripts/provision_vm.sh
```

`provision_vm.sh` creates the VM (Debian 12, e2-micro, 30 GB standard disk) and, if
you set `OPEN_DASHBOARD=1`, a firewall rule for the dashboard port. By default the
dashboard is **not** exposed publicly — reach it over an SSH tunnel instead.

Then finish setup on the VM:

```sh
# Copy the service-account key up:
gcloud compute scp gcp-service-account.json forest-sentinel:~/ --zone "$ZONE"

# SSH in and run the on-VM provisioning:
gcloud compute ssh forest-sentinel --zone "$ZONE"
#   on the VM:
git clone https://github.com/jackrsteiner/open-forest-sentinel.git
cd open-forest-sentinel && ./scripts/vm_setup.sh
```

`vm_setup.sh` installs Docker + uv, starts PostgreSQL + PostGIS, creates `/data/cogs`,
applies migrations, writes a starter `.env`, and installs + enables the systemd units
for the dashboard and the scheduled pipeline.

---

## 6. Run on the VM & serve the dashboard

After `vm_setup.sh`, edit `~/open-forest-sentinel/.env` on the VM to set
`FOREST_SENTINEL_GEE_PROJECT`, `FOREST_SENTINEL_GCS_STAGING_BUCKET`, and the
scheduled-run settings (`AOI_PATH`, `WINDOW_DAYS`), then:

```sh
sudo systemctl restart forest-sentinel-dashboard   # pick up new env
sudo systemctl start   forest-sentinel-pipeline    # trigger one run now
journalctl -u forest-sentinel-pipeline -f          # watch it
```

Reach the dashboard via an SSH tunnel (no public exposure):

```sh
gcloud compute ssh forest-sentinel --zone "$ZONE" -- -L 8000:localhost:8000
# then open http://localhost:8000
```

---

## 7. Automate running

### On the VM (works today)

`vm_setup.sh` installs a systemd **timer** that runs the pipeline daily at 03:00 UTC
over a rolling window. The timer triggers `scripts/run_pipeline.sh`, which loads
`.env`, computes `--since/--until` from `WINDOW_DAYS`, and invokes the CLI.

```sh
systemctl status forest-sentinel-pipeline.timer    # is it scheduled?
sudo systemctl start forest-sentinel-pipeline      # run on demand
```

Change the cadence by editing `OnCalendar=` in the **installed** unit —
`sudo systemctl edit --full forest-sentinel-pipeline.timer` (systemd reloads it for
you) — or edit `scripts/systemd/forest-sentinel-pipeline.timer` in the repo and
re-run `vm_setup.sh` to reinstall it. (Editing the repo copy alone does nothing:
the unit is *copied* to `/etc/systemd/system` at install time, and `daemon-reload`
only re-reads installed units.)

### From GitHub Actions (intended direction — E11)

[`.github/workflows/scheduled-run.yml`](.github/workflows/scheduled-run.yml) is the
GitHub-Actions-cron path from the architecture. It SSHes to the VM and triggers the
same systemd run, so the only secret in CI is the GCP credential. It ships
**disabled** (guarded by `if: false` and a commented `schedule`). To enable it:

1. Add repo secrets `GCP_PROJECT`, `GCE_INSTANCE`, `GCE_ZONE`, `GCP_SA_KEY`.
2. Remove the `if: ${{ false }}` guard and uncomment the `schedule:` trigger.

> The pipeline currently runs **synchronously** (it submits each Earth Engine export
> and polls it to completion). A submit-and-return mode is future work; until then,
> keep the scheduled window small enough that a run finishes well within the job's
> time budget.

---

## 8. Operations

- **COG retention.** The 30 GB disk is shared by Postgres and `/data/cogs`. It is
  finite — prune old COGs (e.g. a cron `find /data/cogs -mtime +90 -delete`) before it
  fills. A formal retention policy is future work.
- **Database backups.** `pg_dump` the `forest_sentinel` database on a schedule;
  store dumps off-VM.
- **Logs.** `journalctl -u forest-sentinel-pipeline` and
  `journalctl -u forest-sentinel-dashboard`.
- **Cost watch-outs** (from [`docs/architecture.md`](docs/architecture.md) §4b): keep
  the GCS bucket inside its 5 GB-month free tier (the 1-day lifecycle rule helps),
  stay on the Earth Engine noncommercial tier, mind dashboard egress, and don't let
  the disk spill to metered storage.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `could not connect to the database` | Postgres isn't up. `docker compose up -d`; on the VM check `docker compose ps`. |
| `earthengine authenticate ... ee.Authenticate()` | EE isn't initialized. Set `GOOGLE_APPLICATION_CREDENTIALS` and `FOREST_SENTINEL_GEE_PROJECT`, and register the project at code.earthengine.google.com/register. EE auth is checked **before** the storage bucket. |
| `FOREST_SENTINEL_GCS_STAGING_BUCKET is not set` (StorageError) | Export staging bucket missing. Run `scripts/setup_gcp.sh` and set the variable. |
| `an AOI named '…' already exists` | The AOI name is unique. The Slice 0 load rejects duplicates; the pipeline reuses the existing AOI by name. |
| Pipeline reports 0 observations | No HLS scenes for the AOI/window, or all cloud-masked. Widen the window or pick a less cloudy period. |
| VM not free / unexpected charges | The e2-micro free tier only applies in `us-west1`/`us-central1`/`us-east1`; check the zone. |

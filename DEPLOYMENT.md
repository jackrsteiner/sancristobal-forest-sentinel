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

> **Deploying your own instance?** The recommended path is the template-repo
> workflow in [`INSTANCE_DEPLOYMENT.md`](INSTANCE_DEPLOYMENT.md): create a repo
> with "Use this template", run one bootstrap script, and a GitHub Action
> provisions everything keylessly. This guide remains the manual/operational
> reference behind it.

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

The pipeline authenticates to Google Earth Engine and Cloud Storage as the
**`forest-sentinel-pipeline` service account**, using ambient [Application
Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials)
— **no key file by default**. Provision everything with:

```sh
export PROJECT_ID=your-gcp-project-id
./scripts/setup_gcp.sh
```

`setup_gcp.sh` is idempotent and:

- enables the `earthengine`, `storage`, and `compute` APIs,
- creates the `forest-sentinel-pipeline` service account and grants it `earthengine.writer`,
  `storage.objectAdmin`, and `serviceusage.serviceUsageConsumer` (Earth Engine requires
  the latter to use the project),
- creates the transient GCS staging bucket with a 1-day delete lifecycle (staging is
  short-lived by design — see [`docs/architecture.md`](docs/architecture.md) §4b).

**One manual step Earth Engine requires:** register the project at
<https://code.earthengine.google.com/register>, choosing the **noncommercial /
unpaid** usage. EE access cannot be enabled non-interactively.

### Where credentials come from

- **On the VM:** nothing to install — `provision_vm.sh` attaches the pipeline
  service account to the instance, and ADC comes from the metadata server.
- **Local development:** run `gcloud auth application-default login` (keyless),
  or mint a key with `CREATE_KEY=1 ./scripts/setup_gcp.sh` and point
  `GOOGLE_APPLICATION_CREDENTIALS` at it. If you do use a key, keep it mode
  `600`; `.gitignore` already excludes `.env`, `gcp-service-account.json`, and
  `*-service-account.json`. Never paste keys into code, docs, or commit messages.
- **Automation (CI):** no keys in CI either — workflows authenticate via
  **Workload Identity Federation** (short-lived OIDC tokens; see
  `scripts/setup_wif.sh` and §7).

### Configuration

Copy `.env.example` to `.env` and fill it in. Everything (the app and the
scripts) reads the same variables:

  | Variable | Purpose | Required for |
  |----------|---------|--------------|
  | `FOREST_SENTINEL_DATABASE_URL` | PostgreSQL connection | always (defaults to local docker compose) |
  | `FOREST_SENTINEL_GEE_PROJECT` | EE-registered GCP project id | pipeline |
  | `FOREST_SENTINEL_GCS_STAGING_BUCKET` | transient COG export bucket | pipeline |
  | `FOREST_SENTINEL_COG_ROOT` | local canonical COG dir (default `data/cogs/`) | pipeline |
  | `GOOGLE_APPLICATION_CREDENTIALS` | path to a service-account key — **only** for key-based local dev | optional |

On the VM, `.env` is **generated** by `vm_setup.sh` from `.env.example` plus the
committed [`config/instance.env`](config/instance.env) — make persistent changes
there, not in `.env` (see §6).

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
    --out config/aois/guadalcanal.geojson
```

**Multiple AOIs**: the scheduled run processes the single `AOI_PATH`
(conventionally the committed `config/aoi.geojson`) **plus every
`*.geojson` in the `config/aois/` directory** (`FOREST_SENTINEL_AOIS_DIR`),
sequentially — one CLI invocation per AOI, so one AOI's failure doesn't stop
the others (the run still exits nonzero to alert the scheduler). All AOIs
share a single `PIPELINE_TIMEOUT` budget per firing; checkpoint/resume applies
per AOI. AOIs can also be **uploaded from the dashboard** (sidebar → Add AOI):
the upload is validated, written to `config/aois/`, and registered immediately —
commit the file to the instance repo to make it permanent. Set
`FOREST_SENTINEL_AOI_UPLOADS=0` to disable uploads (`vm_setup.sh` does this
automatically when `OPEN_DASHBOARD=1` exposes the port publicly).

Inspect and retire AOIs with the CLI:

```sh
uv run forest-sentinel aoi list                # ids, names, row counts, last run
uv run forest-sentinel aoi delete "Name"       # dry-run: prints what would go
uv run forest-sentinel aoi delete "Name" --yes # deletes rows + COG directory
```

`aoi delete` removes every dependent row in one transaction plus the AOI's COG
tree and its `config/aois/` file; if the AOI's GeoJSON is still *committed* to the
repo, remove that too or the next run re-creates it.

Keep AOIs small to stay inside the free tiers (the `e2-micro` VM has 1 GB RAM and a
30 GB disk shared with Postgres and the COG store).

### Running the pipeline

```sh
uv run forest-sentinel run \
    --aoi config/aois/guadalcanal.geojson \
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
export ZONE=us-west1-a
./scripts/provision_vm.sh
```

`provision_vm.sh` creates the VM (Debian 12, e2-micro, 30 GB standard disk) **with
the pipeline service account attached** (run `setup_gcp.sh` first), so the VM needs
no credential files. If you set `OPEN_DASHBOARD=1`, it also creates a firewall rule
for the dashboard port. By default the dashboard is **not** exposed publicly — reach
it over an SSH tunnel instead.

Two ways to finish setup:

- **Self-configuring (what the deploy Action uses):** set `REPO_URL` when
  provisioning and the VM runs `scripts/vm_startup.sh` → `vm_setup.sh` on first
  boot, no SSH needed:

  ```sh
  REPO_URL=https://github.com/<owner>/<repo>.git ./scripts/provision_vm.sh
  ```

- **Manually over SSH:**

  ```sh
  gcloud compute ssh forest-sentinel-vm --zone "$ZONE"
  #   on the VM:
  git clone https://github.com/jackrsteiner/open-forest-sentinel.git
  cd open-forest-sentinel && ./scripts/vm_setup.sh
  ```

`vm_setup.sh` installs Docker + uv, starts PostgreSQL + PostGIS, creates `/data/cogs`,
applies migrations, generates `.env` (from `.env.example` + `config/instance.env`),
and installs + enables the systemd units for the dashboard and the scheduled pipeline.

---

## 6. Run on the VM & serve the dashboard

`vm_setup.sh` generates `~/open-forest-sentinel/.env` from `.env.example` plus the
committed `config/instance.env` (project, bucket, AOI, window). To change settings,
edit `config/instance.env` (and/or `config/aoi.geojson`) and re-run
`./scripts/vm_setup.sh` — it regenerates `.env` and restarts the dashboard. Direct
`.env` edits work for quick experiments but are overwritten on the next re-run.
No SSH needed: the **Update instance** workflow
([`.github/workflows/update-instance.yml`](.github/workflows/update-instance.yml))
runs the same pull + `vm_setup.sh` on the VM from the Actions tab, and can merge
template updates first (see `INSTANCE_DEPLOYMENT.md` → "Updating an instance later").

**Detection tuning (`THRESHOLD`, `MIN_AREA`, `BASELINE_WINDOW`) is methodology,
not just configuration.** The methodology version is content-addressed: a run
whose parameter set matches a previous run reuses that methodology (and its
rasters); a changed parameter set automatically mints a new `auto-<hash>`
version, under which **nothing is reusable** — the next run re-exports the whole
window from Earth Engine and logs/records a prominent warning saying so. The
dashboard's run card shows each run's methodology and its full parameter set.
Reverting the knobs re-matches the earlier methodology, so A/B-ing two
parameter sets only pays the full recompute once per set.

```sh
sudo systemctl start   forest-sentinel-pipeline    # trigger one run now
journalctl -u forest-sentinel-pipeline -f          # watch it
```

The dashboard's **Run pipeline now** button does the same thing without a
shell (`POST /api/pipeline/run` → the same systemd unit; disabled via
`FOREST_SENTINEL_PIPELINE_TRIGGER=0`, automatic when `OPEN_DASHBOARD=1`).
Progress streams into the runs panel as the run commits its events.

Reach the dashboard via an SSH tunnel (no public exposure):

```sh
gcloud compute ssh forest-sentinel-vm --zone "$ZONE" -- -L 8000:localhost:8000
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
same systemd run, authenticating via Workload Identity Federation — no keys in CI.
It ships **disabled** (guarded by `if: false` and a commented `schedule`). To enable it:

1. Run `scripts/setup_wif.sh` (once per instance) and add the repo **variables**
   `WIF_PROVIDER` and `PROVISIONER_SA` it prints (see
   [`INSTANCE_DEPLOYMENT.md`](INSTANCE_DEPLOYMENT.md) — instance repos already
   have these after their first deploy).
2. Make sure `PROJECT_ID` (and, if not default, `INSTANCE_NAME`/`ZONE`) are set
   in `config/instance.env`.
3. Remove the `if: ${{ false }}` guard and uncomment the `schedule:` trigger.

> The pipeline currently runs **synchronously** (it submits each Earth Engine export
> and polls it to completion). A submit-and-return mode is future work; until then,
> keep the scheduled window small enough that a run finishes well within the job's
> time budget.

---

## 8. Operations

- **COG retention.** The 30 GB disk is shared by Postgres and `/data/cogs`. It is
  finite, so retention is automated (#80): the `forest-sentinel-prune` systemd timer
  (installed by `vm_setup.sh`, daily at 02:30 UTC) runs `forest-sentinel cogs prune`,
  which deletes catalog COGs whose **acquisition date** (the store's
  `{aoi}/{product}/{date}/` path component, not file mtime) is older than
  `COG_RETENTION_DAYS` (`config/instance.env`, default 90; blank/0 keeps everything).
  Two guardrails, per the design constraints in
  [`docs/architecture.md`](docs/architecture.md) §7: the effective retention is
  floored at `WINDOW_DAYS` + a 14-day margin (pruning inside the active window would
  re-spend Earth Engine quota on re-exports and rewrite non-frozen change rasters'
  recorded baseline provenance — the job warns and applies the floor instead), and
  **database rows are never deleted** — they are the reproduction recipe, and the
  pipeline's missing-file path re-exports a pruned raster on demand. Preview with
  `./scripts/prune_cogs.sh --dry-run`.
- **Database backups.** `pg_dump` the `forest_sentinel` database on a schedule;
  store dumps off-VM.
- **Logs.** `journalctl -u forest-sentinel-pipeline` and
  `journalctl -u forest-sentinel-dashboard`.
- **Cost watch-outs** (from [`docs/architecture.md`](docs/architecture.md) §4b): keep
  the GCS bucket inside its 5 GB-month free tier (the 1-day lifecycle rule helps),
  stay on the Earth Engine noncommercial tier, mind dashboard egress, and don't let
  the disk spill to metered storage.
- **Teardown.** `PROJECT_ID=… ./scripts/teardown_gcp.sh` deletes the created
  resources (VM, firewall rule, bucket, service accounts, WIF pool); add `--nuke`
  to delete the entire project instead.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `could not connect to the database` | Postgres isn't up. `docker compose up -d`; on the VM check `docker compose ps`. |
| `earthengine authenticate ... ee.Authenticate()` | EE isn't initialized. Make sure ADC is available (attached service account on the VM; `gcloud auth application-default login` or a key + `GOOGLE_APPLICATION_CREDENTIALS` locally), set `FOREST_SENTINEL_GEE_PROJECT`, and register the project at code.earthengine.google.com/register. EE auth is checked **before** the storage bucket. |
| `FOREST_SENTINEL_GCS_STAGING_BUCKET is not set` (StorageError) | Export staging bucket missing. Run `scripts/setup_gcp.sh` and set the variable. |
| `an AOI named '…' already exists` | The AOI name is unique. The Slice 0 load rejects duplicates; the pipeline reuses the existing AOI by name. |
| Pipeline reports 0 observations | No HLS scenes for the AOI/window, or all cloud-masked. Widen the window or pick a less cloudy period. |
| VM not free / unexpected charges | The e2-micro free tier only applies in `us-west1`/`us-central1`/`us-east1`; check the zone. |

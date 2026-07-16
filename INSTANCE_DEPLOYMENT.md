# Deploying your own instance (template → repo → one-click deploy)

This is the end-to-end workflow for standing up a **new Open Forest Sentinel
instance**: your own GitHub repo (created from this template), your own GCP
project, and an always-free VM that provisions itself. After the one-time manual
bootstrap, a single GitHub Action does everything else — with **no
service-account keys anywhere**: GitHub Actions authenticates to GCP via
Workload Identity Federation (short-lived OIDC tokens), and the VM authenticates
to Earth Engine / Cloud Storage via its attached service account.

For general operations (tuning, systemd, backups, troubleshooting) see
[`DEPLOYMENT.md`](DEPLOYMENT.md); for a fully manual browser-only path see
[`docs/cloud-shell-setup.md`](docs/cloud-shell-setup.md).

---

## At a glance

1. Base repo, once ever: enable **Template repository** → [§0](#0-one-time-on-the-base-repo-maintainers)
2. **Use this template** → create a *public* instance repo (note exact `owner/name` casing) → [§1](#1-create-the-instance-repo)
3. GCP console: create project, attach billing, register for Earth Engine → [§2](#2-manual-gcp-bootstrap-once-per-instance)
4. Cloud Shell: `PROJECT_ID=… GITHUB_REPO=… ./scripts/setup_wif.sh` → [§2](#2-manual-gcp-bootstrap-once-per-instance)
5. Repo settings: variables `WIF_PROVIDER` + `PROVISIONER_SA`; PAT → secret `OFS_ADMIN_TOKEN` → [§3](#3-wire-up-the-instance-repo)
6. Edit + commit `config/instance.env` (`PROJECT_ID`) and `config/aoi.geojson` → [§4](#4-commit-your-configuration)
7. Actions → run **Deploy instance** (graft → WIF auth → provision GCP + VM → wait) → [§5](#5-run-the-deploy-instance-workflow)
8. SSH tunnel → dashboard at `http://localhost:8000` → [§6](#6-view-the-dashboard)
9. Later: pull template updates with `git pull --no-rebase upstream main` → [Updating an instance later](#updating-an-instance-later)
10. Done with it: `./scripts/teardown_gcp.sh` (or `--nuke`) → [Tear it all down](#tear-it-all-down)

---

## How it fits together

| Piece | Role |
|-------|------|
| `config/instance.env` + `config/aoi.geojson` | your committed, non-secret instance configuration |
| `scripts/setup_wif.sh` | one-time manual bootstrap: Workload Identity Federation + provisioner service account |
| `.github/workflows/deploy.yml` | the "Deploy instance" Action: history graft + `setup_gcp.sh` + `provision_vm.sh` |
| `scripts/vm_startup.sh` → `scripts/vm_setup.sh` | the VM configures itself on first boot and starts the initial pipeline run |
| `scripts/teardown_gcp.sh` | deletes everything again |

## 0. One-time, on the base repo (maintainers)

Enable **Settings → General → Template repository** so the "Use this template"
button appears.

## 1. Create the instance repo

Click **Use this template → Create a new repository**. Make it **public** (the
VM clones it anonymously; private repos work for the initial deploy but not for
later `git pull`s on the VM). Note the repo's `owner/name` **exactly as GitHub
shows it** — casing matters for step 2.

> Template copies start with a single squashed commit, unrelated to this repo's
> history. The deploy workflow fixes that by grafting the template history onto
> your repo (a one-time force-push of `main`), so that later you can pull
> updates with `git pull upstream main`. Don't enable branch protection on
> `main` until after the first deploy, or the graft push will be rejected.

## 2. Manual GCP bootstrap (once per instance)

1. In the [GCP console](https://console.cloud.google.com/): create a project,
   attach billing (usage stays inside the always-free tiers).
2. Register the project for Earth Engine at
   <https://code.earthengine.google.com/register> (choose **noncommercial /
   unpaid**). This cannot be automated.
   Then set the project's Earth Engine **noncommercial tier to Contributor**
   (Cloud console → Earth Engine settings; self-service). It is still free —
   it only requires the billing account you just attached — and raises the
   monthly compute quota from 150 to 1,000 EECU-hours plus the concurrent
   batch-task limit, which the pipeline's batched exports exploit
   (`docs/scaling.md` §2). The default Community tier works but makes the
   first backfill painfully slow (~40 min/export observed).
3. In [Cloud Shell](https://shell.cloud.google.com/) (or any authenticated
   `gcloud`):

   ```sh
   git clone https://github.com/<owner>/<your-instance-repo>.git
   cd <your-instance-repo>
   PROJECT_ID=<your-project-id> GITHUB_REPO=<owner/name-exact-casing> ./scripts/setup_wif.sh
   ```

   The script creates a workload identity pool/provider locked to *your repo
   only*, plus a `forest-sentinel-provisioner` service account, and prints the
   values for step 3.

## 3. Wire up the instance repo

In the repo's **Settings → Secrets and variables → Actions**:

| Where | Name | Value |
|-------|------|-------|
| Variables | `WIF_PROVIDER` | printed by `setup_wif.sh` (`projects/…/providers/github-oidc`) |
| Variables | `PROVISIONER_SA` | printed by `setup_wif.sh` (`forest-sentinel-provisioner@…`) |
| Secrets | `OFS_ADMIN_TOKEN` | a [fine-grained PAT](https://github.com/settings/personal-access-tokens) scoped to this repo with **Contents: read/write** and **Workflows: read/write** |

The two variables are identifiers, not secrets. The PAT exists only because the
history graft pushes commits that touch `.github/workflows/`, which the default
Actions token is not allowed to do; you can delete the PAT after the first
successful deploy.

## 4. Commit your configuration

Edit and commit (both files live outside the base machinery and survive the
history graft):

- **`config/instance.env`** — set `PROJECT_ID` (required); optionally `ZONE`,
  `INSTANCE_NAME`, `WINDOW_DAYS`, thresholds, etc. When `setup_wif.sh` runs
  inside your instance clone it sets and commits `PROJECT_ID` for you (and
  pushes it if the shell has GitHub credentials), so usually only the AOI is
  left to provide.
- **`config/aoi.geojson`** — your Area of Interest. Build one at
  <https://jackrsteiner.github.io/aoi-maker/>, or with
  `uv run python scripts/make_aoi.py --bbox … --name … --out config/aoi.geojson`.
  Keep it small: the e2-micro VM has 1 GB RAM and a 30 GB disk.

## 5. Run the "Deploy instance" workflow

**Actions → Deploy instance → Run workflow** (on `main`). The workflow:

1. grafts the template history onto your repo (skipped if already done),
2. authenticates to GCP via Workload Identity Federation,
3. runs `setup_gcp.sh` — enables APIs, creates the pipeline service account
   (no key) and the staging bucket,
4. runs `provision_vm.sh` — creates the e2-micro VM **with the pipeline service
   account attached** and `vm_startup.sh` as its startup script,
5. waits while the VM configures itself (Docker, PostGIS, migrations, systemd
   units) and starts the initial pipeline run,
6. writes the dashboard tunnel command to the run summary.

## 6. View the dashboard

The dashboard is never exposed publicly by default — reach it over an SSH
tunnel (dashboard port is **8000**):

```sh
gcloud compute ssh forest-sentinel-vm --project <your-project-id> \
    --zone us-west1-a -- -L 8000:localhost:8000
# then open http://localhost:8000
```

The pipeline also runs daily at 03:00 UTC via the VM's systemd timer.

## Updating an instance later

Because your `main` carries the template's history after the graft:

```sh
git remote add upstream https://github.com/jackrsteiner/open-forest-sentinel.git
git pull --no-rebase upstream main   # ordinary merge; your config/ lives outside src/
git push
```

To apply updated machinery on the VM, SSH in and re-run
`./scripts/vm_setup.sh` (idempotent), or tear down and redeploy.

Changing `config/instance.env` / `config/aoi.geojson`: commit, then either
re-run `vm_setup.sh` on the VM (it regenerates `.env` and restarts the
dashboard) — or tear down and redeploy.

## Tear it all down

```sh
PROJECT_ID=<your-project-id> ./scripts/teardown_gcp.sh          # resources only
PROJECT_ID=<your-project-id> ./scripts/teardown_gcp.sh --nuke   # delete the whole project
```

Resource mode removes the VM, firewall rule, staging bucket, both service
accounts, and the WIF provider/pool (revoking GitHub's provisioning access),
but keeps the project, billing link, and Earth Engine registration for a future
redeploy. `--nuke` deletes the entire project (recoverable for ~30 days).

## Caveats & troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Graft step fails with "refusing to allow …workflows" or 403 | `OFS_ADMIN_TOKEN` missing/expired or lacks **Workflows: read/write**. |
| Graft push rejected | Branch protection on `main` — disable it for the first deploy. |
| `auth@v2` step fails with `unauthorized_client` | `WIF_PROVIDER`/`PROVISIONER_SA` variables wrong, or `GITHUB_REPO` was passed to `setup_wif.sh` with different casing than the real repo name. Re-run `setup_wif.sh` with the exact `owner/name` — it repoints the provider in place. |
| Redeploying after a teardown or a wrong-repo setup | Just re-run `setup_wif.sh` — it self-heals: soft-deleted WIF pools/providers are undeleted, a provider locked to another repo is repointed, and stale cross-repo impersonation grants are pruned. |
| Deploy re-run doesn't pick up VM changes | An existing VM's metadata/startup script is not refreshed — tear down and redeploy (or `gcloud compute instances add-metadata`). |
| VM setup fails | The workflow prints the serial-console tail; on the VM see `/var/log/ofs-startup.log` and `journalctl`. |
| Pipeline can't reach Earth Engine | Project not EE-registered (step 2.2), or `PROJECT_ID` not set in `config/instance.env`. |

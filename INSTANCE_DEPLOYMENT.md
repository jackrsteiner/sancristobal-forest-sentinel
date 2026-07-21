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
6. Edit + commit `config/instance.env` (`PROJECT_ID`); AOI now or later via dashboard upload → [§4](#4-commit-your-configuration)
7. Actions → run **Deploy instance** (graft → WIF auth → provision GCP + VM → wait) → [§5](#5-run-the-deploy-instance-workflow)
8. SSH tunnel → dashboard at `http://localhost:8000` → [§6](#6-view-the-dashboard)
9. Later: Actions → run **Update instance** (merges template updates + refreshes the VM) → [Updating an instance later](#updating-an-instance-later)
10. Done with it: `./scripts/teardown_gcp.sh` (or `--nuke`) → [Tear it all down](#tear-it-all-down)

---

## How it fits together

| Piece | Role |
|-------|------|
| `config/instance.env` + `config/aoi.geojson` | your committed, non-secret instance configuration |
| `scripts/setup_wif.sh` | one-time manual bootstrap: Workload Identity Federation + provisioner service account |
| `.github/workflows/deploy.yml` | the "Deploy instance" Action: history graft + `setup_gcp.sh` + `provision_vm.sh` |
| `scripts/vm_startup.sh` → `scripts/vm_setup.sh` | the VM configures itself on first boot and starts the initial pipeline run |
| `.github/workflows/update-instance.yml` | the "Update instance" Action: merge template updates + refresh the VM, no SSH needed |
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
- **Your Area of Interest — optional at this point.** Either commit it now
  (`config/aoi.geojson`, or one file per AOI under `config/aois/*.geojson` — every
  file there is monitored), or skip it and **upload from the dashboard later**
  (§6): the sidebar's *Add AOI* control accepts a GeoJSON and the next
  scheduled run backfills it. Committed files are the durable form — an
  uploaded AOI is written to `config/aois/` on the VM and **committed back to
  the instance repo by the next "Update instance" run** (sync_aois toggle). Build a GeoJSON at
  <https://jackrsteiner.github.io/aoi-maker/>, or with
  `uv run python scripts/make_aoi.py --bbox … --name … --out config/aoi.geojson`.
  Keep AOIs small: the e2-micro VM has 1 GB RAM and a 30 GB disk
  (`docs/scaling.md`).
- **Context layers — optional.** Commit GeoJSON overlays (concessions,
  protected areas, roads, rivers, settlements, mills, ports) under
  `config/context/` named `<kind>--<name>.geojson` (e.g.
  `concession--acme-palm.geojson`; kinds: `concession`, `protected_area`,
  `road`, `river`, `settlement`, `mill`, `port`, `other`). Every pipeline run
  loads them, and re-committing a revised file replaces the layer's features
  on the next run. One-off loads work too:
  `uv run forest-sentinel context load <file> --kind <kind>`.

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

The pipeline also runs daily at 03:00 UTC via the VM's systemd timer — over
**every configured AOI** (committed `config/aoi.geojson` plus each
`config/aois/*.geojson`, sequentially). The sidebar's **Add AOI** control uploads a
new AOI GeoJSON without touching the repo; it is monitored from the next run.
Retire one with `forest-sentinel aoi delete <name> --yes` on the VM
(`aoi list` shows what exists).

## Updating an instance later

**Actions → Update instance → Run workflow.** One button does the whole
rollout, with four toggles (all on by default):

- **sync_upstream** — merges the template's latest `main` into your repo and
  pushes (possible because the deploy graft gave your `main` the template's
  history). On a merge conflict the run fails without pushing anything and the
  run summary lists the conflicting files plus the local-resolution commands.
- **sync_aois** — copies dashboard-uploaded AOI GeoJSONs (`config/aois/` on
  the VM) back into this repo and commits them, making uploads durable — the
  VM itself holds no GitHub credentials for repo contents, so this workflow is
  what turns an upload into a committed file.
- **sync_settings** — same, for dashboard settings edits
  (`config/overrides.env` on the VM). Settings marked "next-run" take effect
  at the start of the next pipeline/prune run directly from that file — no
  update_vm needed; only "update-instance" settings (systemd-rendered values
  like schedules and the pipeline timeout) wait for an update_vm.
- **update_vm** — authenticates via Workload Identity Federation and SSHes to
  the VM (no keys, nothing to install locally) to `git pull` and re-run
  `vm_setup.sh`: migrations applied, `.env` regenerated, systemd units
  re-rendered, dashboard restarted. A running pipeline is left untouched.

Untick `sync_upstream` to only refresh the VM (e.g. after committing a change
to `config/instance.env`); untick `update_vm` to only bring the repo up to
date.

### Optional: near-real-time sync of dashboard changes

By default, settings edits and uploads stay on the VM until you run the
workflow above. To have the dashboard fire the sync automatically (changes
committed to this repo within about a minute of saving):

1. Create a **fine-grained personal access token** at
   github.com → Settings → Developer settings → Fine-grained tokens:
   - **Repository access**: *only this instance repo*.
   - **Permissions**: Actions — *Read and write*. **Nothing else** — in
     particular not Contents. (Actions-write can only trigger/re-run this
     repo's workflows, all of which are version-controlled; it cannot read
     secrets or push code. `repository_dispatch` is deliberately not used —
     it would require a contents-write token on the VM.)
2. Put the token on the VM, outside the repo tree:

   ```
   gcloud compute ssh forest-sentinel-vm -- \
     "sudo install -d -m 700 -o ofs /etc/forest-sentinel &&
      sudo -u ofs tee /etc/forest-sentinel/dispatch-token >/dev/null &&
      sudo chmod 600 /etc/forest-sentinel/dispatch-token" < token.txt
   ```

3. In `config/instance.env`, set `GITHUB_REPO=<owner>/<this-repo>` and
   `FOREST_SENTINEL_SYNC_TOKEN_FILE=/etc/forest-sentinel/dispatch-token`,
   commit, and run **Update instance** once to roll the config out.

Leaving either value blank keeps the manual-sync behavior. A dispatch failure
(revoked token, network) never blocks the edit itself — the change stays on
the VM and the next manual sync picks it up. Bursts of edits are debounced
into one workflow run.

**Optional: run from the published image.** Set `APP_IMAGE` in
`config/instance.env` (e.g. `ghcr.io/jackrsteiner/open-forest-sentinel:latest`,
or a commit-SHA tag to pin) and run **Update instance**: the VM then pulls the
CI-tested container instead of building from source, which makes updates
faster and independent of PyPI weather on the tiny VM. Blank keeps the default
from-source build. See `DEPLOYMENT.md` §8 for details.

The manual equivalent, if you prefer it or need to resolve a merge conflict:

```sh
git remote add upstream https://github.com/jackrsteiner/open-forest-sentinel.git
git pull --no-rebase upstream main   # ordinary merge; your config/ lives outside src/
git push
```

then SSH in and re-run `./scripts/vm_setup.sh` (idempotent), or tear down and
redeploy.

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

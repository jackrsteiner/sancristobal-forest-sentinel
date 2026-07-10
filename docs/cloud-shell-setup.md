# Provisioning from Google Cloud Shell (no local tooling)

This guide stands up a complete Open Forest Sentinel deployment — service account,
staging bucket, always-free VM, database, dashboard, and a daily scheduled pipeline —
**entirely from a web browser**, using [Google Cloud Shell](https://shell.cloud.google.com).
You install nothing on your own machine: no `gcloud`, no Docker, no Python, no git.

It is the browser-only counterpart of [`DEPLOYMENT.md`](../DEPLOYMENT.md), and runs the
same helper scripts from [`scripts/`](../scripts/). Read that document for the *why*
behind each piece (and [`docs/architecture.md`](architecture.md) for the system design);
this one is a linear checklist of the *how*.

**What you'll have at the end:**

- A GCP project with the Earth Engine, Storage, and Compute APIs enabled.
- The `forest-sentinel` service account, its key, and the transient GCS staging bucket.
- An always-free `e2-micro` VM running PostgreSQL + PostGIS, the dashboard, and a
  systemd timer that runs the pipeline daily at 03:00 UTC.
- (Optional) the GitHub Actions scheduled-run workflow wired up.

---

## 1. One-time Google account & project setup (Console UI)

These steps happen in the Google Cloud Console — they cannot be scripted.

1. **Google account** — any Gmail / Google Workspace account works.
2. **Create a project** at <https://console.cloud.google.com/projectcreate>. Note the
   **project ID** (e.g. `my-forest-sentinel`, not the display name) — every later step
   uses it.
3. **Enable billing** for the project at
   <https://console.cloud.google.com/billing> (⋮ on the project → *Change billing*).
   Billing must be attached even though this deployment is designed to stay inside the
   always-free tiers (see `DEPLOYMENT.md` §1 and `docs/architecture.md` §4b). New
   accounts get the standard free trial; the free tiers apply regardless.
4. **Register the project for Earth Engine** at
   <https://code.earthengine.google.com/register>. Choose **noncommercial / unpaid**
   usage and select your project. This is the one step Google requires to be
   interactive; skip it if you've already registered this project.

---

## 2. Open Cloud Shell

Go to <https://shell.cloud.google.com> (or click the `>_` icon in the Cloud Console
toolbar). What you get:

- A Debian terminal in your browser with **`gcloud` pre-installed and already
  authenticated as you** — no `gcloud auth login` needed.
- A **persistent 5 GB `$HOME`** — files (like the repo clone) survive between sessions.
- An ephemeral VM around it — sessions disconnect after ~20 minutes idle and the
  machine is recycled after ~1 hour of inactivity. That's fine here: everything
  long-running happens on *your* VM under systemd, not in the Cloud Shell session, so
  a dropped session never interrupts the pipeline.
- Free of charge (usage-capped at 50 hours/week — far more than this needs).

Point it at your project (replace the ID throughout this guide):

```sh
gcloud config set project my-forest-sentinel
```

> Re-run this if you come back in a later session and it's unset; check with
> `gcloud config get-value project`.

---

## 3. Clone the repo and provision the GCP resources

```sh
git clone https://github.com/jackrsteiner/open-forest-sentinel.git
cd open-forest-sentinel

export PROJECT_ID=my-forest-sentinel
./scripts/setup_gcp.sh
```

The script is idempotent (safe to re-run) and, as described in `DEPLOYMENT.md` §3:

- enables the `earthengine`, `storage`, and `compute` APIs,
- creates the **`forest-sentinel` service account** with `roles/earthengine.writer`
  and `roles/storage.objectAdmin`,
- creates the transient staging bucket `gs://<PROJECT_ID>-ofs-staging` with a 1-day
  auto-delete lifecycle,
- writes the service-account key to `./gcp-service-account.json` (mode `600`).

It finishes by printing the three environment values you'll need in §7 — keep that
output visible or scroll back to it later.

> **Key hygiene.** The key file now sits in Cloud Shell's persistent `$HOME`. Treat it
> like a password: it goes to the VM in §5 (and optionally into a GitHub secret in
> §9), and then you delete it from Cloud Shell. Never commit it — `.gitignore`
> already excludes it.

---

## 4. Create the always-free VM

```sh
export ZONE=us-central1-a
./scripts/provision_vm.sh
```

This creates a VM named `forest-sentinel` (Debian 12, `e2-micro`, 30 GB standard
disk). The always-free tier **only** applies in `us-west1`, `us-central1`, and
`us-east1` — the script warns if you pick a zone outside them.

The dashboard port is *not* opened to the internet (recommended); §8 shows how to view
it through a tunnel instead. If you ever want it public, re-run with `OPEN_DASHBOARD=1`.

---

## 5. Copy the key up and run the on-VM setup

```sh
gcloud compute scp gcp-service-account.json forest-sentinel:~/ --zone "$ZONE"
gcloud compute ssh forest-sentinel --zone "$ZONE"
```

> The first `gcloud compute ssh`/`scp` generates an SSH key for you — accept the
> prompts (an empty passphrase is fine for this use).

You now have a shell **on the VM**. Run:

```sh
git clone https://github.com/jackrsteiner/open-forest-sentinel.git
cd open-forest-sentinel && ./scripts/vm_setup.sh
```

`vm_setup.sh` (idempotent) installs Docker + uv, starts PostgreSQL + PostGIS with a
persistent data volume, creates the canonical COG store at `/data/cogs`, installs the
key you copied up, writes a starter `.env`, applies the database migrations, and
enables two systemd units: the dashboard service and the daily 03:00 UTC pipeline
timer.

Stay on the VM for §6–§7. Once the key is safely on the VM (and, if you plan to do
§9, in a GitHub secret), remove it from Cloud Shell — back in the Cloud Shell tab:

```sh
rm ~/open-forest-sentinel/gcp-service-account.json
```

---

## 6. Create your AOI (on the VM)

The VM already has the repo and its Python environment, so generate the AOI there and
it lands exactly where the scheduler will look for it:

```sh
cd ~/open-forest-sentinel
mkdir -p aois
uv run python scripts/make_aoi.py \
    --bbox 159.0 -9.6 159.3 -9.3 \
    --name "Guadalcanal North Coast" \
    --out aois/my-aoi.geojson
```

`--bbox` is `min_lon min_lat max_lon max_lat` (WGS 84). The file is validated with the
same loader the pipeline uses, so it's guaranteed to be accepted. Keep the AOI small —
the `e2-micro` VM has 1 GB RAM and a 30 GB disk shared with Postgres and the COG store
(`DEPLOYMENT.md` §4).

> Prefer working in Cloud Shell? `uv sync` there (uv:
> `curl -LsSf https://astral.sh/uv/install.sh | sh`), run the same `make_aoi.py`
> command, then `gcloud compute scp aois/my-aoi.geojson forest-sentinel:~/open-forest-sentinel/aois/ --zone "$ZONE"`.

---

## 7. Configure `.env` and run the pipeline once

Still on the VM, edit the environment file (`nano .env` works):

```sh
cd ~/open-forest-sentinel
nano .env
```

Set these (the COG root and key path are already appended by `vm_setup.sh`):

```sh
FOREST_SENTINEL_GEE_PROJECT=my-forest-sentinel
FOREST_SENTINEL_GCS_STAGING_BUCKET=my-forest-sentinel-ofs-staging
AOI_PATH=aois/my-aoi.geojson
WINDOW_DAYS=30
```

Then restart the dashboard (to pick up the env) and trigger one pipeline run now
rather than waiting for the 03:00 UTC timer:

```sh
sudo systemctl restart forest-sentinel-dashboard
sudo systemctl start   forest-sentinel-pipeline
journalctl -u forest-sentinel-pipeline -f        # watch it (Ctrl-C to stop watching)
```

The run is synchronous — it submits each Earth Engine export and polls it to
completion — so expect it to take a while. It ends by printing a per-stage summary.
A count of 0 observations usually means a cloudy AOI/window; widen `WINDOW_DAYS` or
pick a different period (`DEPLOYMENT.md` §9).

---

## 8. View the dashboard from your browser

Cloud Shell's **Web Preview** completes the no-local-machine loop. Back in the Cloud
Shell tab (not the VM), open a tunnel from Cloud Shell port 8080 to the dashboard on
the VM:

```sh
gcloud compute ssh forest-sentinel --zone "$ZONE" -- -N -L 8080:localhost:8000
```

Leave that running, click the **Web Preview** button (the eye/window icon in the Cloud
Shell toolbar) → **Preview on port 8080**. The dashboard opens in a new browser tab,
served through the tunnel — nothing is exposed to the public internet. Ctrl-C the
tunnel when done.

---

## 9. Optional: schedule runs from GitHub Actions

The VM's systemd timer already runs the pipeline daily, so this section is optional —
it wires up the repo's [`scheduled-run.yml`](../.github/workflows/scheduled-run.yml)
workflow (`DEPLOYMENT.md` §7), which SSHes to the VM on a GitHub-side cron and triggers
the same systemd run.

**a. Grant the service account SSH rights** (it only has Earth Engine + Storage roles
so far). From Cloud Shell:

```sh
SA=forest-sentinel@${PROJECT_ID}.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${SA}" --role roles/compute.instanceAdmin.v1 \
    --condition=None
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${SA}" --role roles/iam.serviceAccountUser \
    --condition=None
```

(`instanceAdmin.v1` lets `gcloud compute ssh` manage the instance's SSH-key metadata;
`serviceAccountUser` is required because the VM has a service account attached. A
tighter setup — a dedicated CI service account, or OS Login/IAP-scoped roles — is a
good hardening step later.)

**b. Add the four repository secrets** in GitHub → repo → *Settings → Secrets and
variables → Actions*:

| Secret | Value |
|--------|-------|
| `GCP_PROJECT` | your project ID |
| `GCE_INSTANCE` | `forest-sentinel` |
| `GCE_ZONE` | e.g. `us-central1-a` |
| `GCP_SA_KEY` | the full JSON of the key — `cat gcp-service-account.json` in Cloud Shell (before you delete it there; afterwards, `cat` it on the VM) and paste |

**c. Enable the workflow.** Edit `.github/workflows/scheduled-run.yml` — doable
entirely in the GitHub web editor: delete the `if: ${{ false }}` guard line and
uncomment the `schedule:` block. Test it from the repo's *Actions* tab with
**Run workflow** (the `workflow_dispatch` trigger).

---

## 10. Verify everything

From Cloud Shell (`gcloud compute ssh forest-sentinel --zone "$ZONE"`), on the VM:

| Check | Command | Expect |
|-------|---------|--------|
| Database up | `sudo docker compose ps` (in `~/open-forest-sentinel`) | `db` service healthy |
| Dashboard running | `systemctl status forest-sentinel-dashboard` | `active (running)` |
| Timer scheduled | `systemctl status forest-sentinel-pipeline.timer` | `active (waiting)`, next trigger shown |
| Last run OK | `journalctl -u forest-sentinel-pipeline -n 50` | per-stage summary, exit code 0 |
| Data landed | dashboard via §8 | your AOI, observations, any events |

Ongoing operations (COG pruning, `pg_dump` backups, cost watch-outs) are covered in
`DEPLOYMENT.md` §8, and the troubleshooting table in §9 applies unchanged. Two
Cloud-Shell-specific notes:

- **Session dropped?** Nothing is lost — reconnect at shell.cloud.google.com; your
  `$HOME` (repo clone, gcloud config) persists, and the VM never noticed.
- **`gcloud` acting on the wrong project?** `gcloud config set project <PROJECT_ID>`.

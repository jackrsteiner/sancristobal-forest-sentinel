#!/usr/bin/env bash
# Create the always-free Google Compute Engine VM that hosts the orchestrator,
# PostgreSQL + PostGIS, and the canonical COG store (see docs/architecture.md §4b).
# Idempotent — an existing instance / firewall rule is left in place.
#
# The pipeline service account (created by setup_gcp.sh) is ATTACHED to the VM,
# so the app authenticates to Earth Engine / GCS via the metadata server — no
# service-account key file anywhere.
#
# When REPO_URL is set, scripts/vm_startup.sh is installed as the VM's startup
# script and the VM configures itself on first boot (clone, vm_setup.sh, initial
# pipeline run) — this is what the "Deploy instance" GitHub Action uses. When
# REPO_URL is unset, the VM is created bare and you finish setup manually over
# SSH (instructions are printed).
#
# Prerequisites: the `gcloud` CLI, authenticated, with a billing-enabled project,
# after running setup_gcp.sh (the attached service account must exist).
# The always-free e2-micro tier is only free in us-west1 / us-central1 / us-east1.
#
# Configure via environment variables:
#   PROJECT_ID       GCP project id                          (required)
#   ZONE             always-free zone        (default: us-west1-a)
#   INSTANCE_NAME    VM name             (default: forest-sentinel-vm)
#   SERVICE_ACCOUNT_EMAIL  SA to attach to the VM
#                    (default: forest-sentinel-pipeline@${PROJECT_ID}...;
#                     set to "" to use the Compute Engine default SA)
#   REPO_URL         instance repo to self-deploy on first boot   (optional)
#   REPO_REF         branch / tag / sha            (default: main)
#   CLONE_TOKEN      short-lived token for cloning a private repo (optional)
#   DASHBOARD_PORT   firewall port for the dashboard         (default: 8000)
#   OPEN_DASHBOARD   set to 1 to open DASHBOARD_PORT to 0.0.0.0/0 (default: 0)
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your GCP project id}"
ZONE="${ZONE:-us-west1-a}"
INSTANCE_NAME="${INSTANCE_NAME:-forest-sentinel-vm}"
SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_EMAIL-forest-sentinel-pipeline@${PROJECT_ID}.iam.gserviceaccount.com}"
REPO_URL="${REPO_URL:-}"
REPO_REF="${REPO_REF:-main}"
CLONE_TOKEN="${CLONE_TOKEN:-}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"
OPEN_DASHBOARD="${OPEN_DASHBOARD:-0}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# A freshly created service account can take a few seconds before it is
# attachable to a new instance.
retry() {
    local attempt
    for attempt in 1 2 3 4 5; do
        if "$@"; then
            return 0
        fi
        echo "    (attempt ${attempt} failed; retrying in 10s)" >&2
        sleep 10
    done
    "$@"
}

if ! command -v gcloud >/dev/null 2>&1; then
    echo "error: 'gcloud' is not installed. See https://cloud.google.com/sdk/docs/install" >&2
    exit 1
fi

case "${ZONE}" in
    us-west1-*|us-central1-*|us-east1-*) ;;
    *) echo "warning: ${ZONE} is outside the always-free regions; the VM may incur cost." >&2 ;;
esac

create_args=(
    --project "${PROJECT_ID}"
    --zone "${ZONE}"
    --machine-type e2-micro
    --image-family debian-12
    --image-project debian-cloud
    --boot-disk-size 30GB
    --boot-disk-type pd-standard
)

# Attach the pipeline SA so the app gets keyless Application Default Credentials
# from the metadata server (cloud-platform scope; access is bounded by the SA's
# IAM roles, not the scope).
if [ -n "${SERVICE_ACCOUNT_EMAIL}" ]; then
    create_args+=(--service-account "${SERVICE_ACCOUNT_EMAIL}" --scopes cloud-platform)
fi

if [ -n "${REPO_URL}" ]; then
    metadata="enable-guest-attributes=TRUE,ofs-repo-url=${REPO_URL},ofs-repo-ref=${REPO_REF}"
    if [ -n "${CLONE_TOKEN}" ]; then
        metadata="${metadata},ofs-clone-token=${CLONE_TOKEN}"
    fi
    create_args+=(
        --metadata "${metadata}"
        --metadata-from-file "startup-script=${SCRIPT_DIR}/vm_startup.sh"
    )
fi

echo "==> Ensuring VM ${INSTANCE_NAME} in ${ZONE}"
if ! gcloud compute instances describe "${INSTANCE_NAME}" \
        --project "${PROJECT_ID}" --zone "${ZONE}" >/dev/null 2>&1; then
    retry gcloud compute instances create "${INSTANCE_NAME}" "${create_args[@]}"
else
    echo "    (already exists — metadata/startup script NOT refreshed; to reset,"
    echo "     tear down and redeploy, or use 'gcloud compute instances add-metadata')"
fi

if [ "${OPEN_DASHBOARD}" = "1" ]; then
    echo "==> Opening firewall for the dashboard on tcp:${DASHBOARD_PORT}"
    if ! gcloud compute firewall-rules describe ofs-dashboard \
            --project "${PROJECT_ID}" >/dev/null 2>&1; then
        gcloud compute firewall-rules create ofs-dashboard \
            --project "${PROJECT_ID}" \
            --allow "tcp:${DASHBOARD_PORT}" \
            --description "Open Forest Sentinel dashboard"
    else
        echo "    (rule ofs-dashboard already exists)"
    fi
fi

cat <<EOF

VM ready: ${INSTANCE_NAME} (${ZONE}).
EOF

if [ -n "${REPO_URL}" ]; then
    cat <<EOF

The VM is configuring itself from ${REPO_URL} @ ${REPO_REF} (startup script).
Watch progress:
  gcloud compute instances get-guest-attributes ${INSTANCE_NAME} \\
      --zone ${ZONE} --query-path ofs/setup-status
  gcloud compute instances get-serial-port-output ${INSTANCE_NAME} --zone ${ZONE}
EOF
else
    cat <<EOF

Finish setup on the VM (no key file needed — the attached service account
provides credentials via the metadata server):
  gcloud compute ssh ${INSTANCE_NAME} --zone ${ZONE}
  #   then, on the VM:
  curl -fsSL https://raw.githubusercontent.com/jackrsteiner/open-forest-sentinel/main/scripts/vm_setup.sh | bash
  #   (or clone the repo and run scripts/vm_setup.sh)
EOF
fi

cat <<EOF

The dashboard is NOT exposed publicly by default. To reach it without opening the
firewall, use an SSH tunnel:
  gcloud compute ssh ${INSTANCE_NAME} --zone ${ZONE} -- -L ${DASHBOARD_PORT}:localhost:${DASHBOARD_PORT}
Then open http://localhost:${DASHBOARD_PORT}
EOF

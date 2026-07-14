#!/usr/bin/env bash
# Create the always-free Google Compute Engine VM that hosts the orchestrator,
# PostgreSQL + PostGIS, and the canonical COG store (see docs/architecture.md §4b).
# Idempotent — an existing instance / firewall rule is left in place.
#
# Prerequisites: the `gcloud` CLI, authenticated, with a billing-enabled project.
# The always-free e2-micro tier is only free in us-west1 / us-central1 / us-east1.
#
# Configure via environment variables:
#   PROJECT_ID     GCP project id                          (required)
#   ZONE           always-free zone        (default: us-west1-a)
#   INSTANCE_NAME  VM name             (default: forest-sentinel-vm)
#   DASHBOARD_PORT firewall port for the dashboard         (default: 8000)
#   OPEN_DASHBOARD set to 1 to open DASHBOARD_PORT to 0.0.0.0/0 (default: 0)
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your GCP project id}"
ZONE="${ZONE:-us-west1-a}"
INSTANCE_NAME="${INSTANCE_NAME:-forest-sentinel-vm}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"
OPEN_DASHBOARD="${OPEN_DASHBOARD:-0}"

if ! command -v gcloud >/dev/null 2>&1; then
    echo "error: 'gcloud' is not installed. See https://cloud.google.com/sdk/docs/install" >&2
    exit 1
fi

case "${ZONE}" in
    us-west1-*|us-central1-*|us-east1-*) ;;
    *) echo "warning: ${ZONE} is outside the always-free regions; the VM may incur cost." >&2 ;;
esac

echo "==> Ensuring VM ${INSTANCE_NAME} in ${ZONE}"
if ! gcloud compute instances describe "${INSTANCE_NAME}" \
        --project "${PROJECT_ID}" --zone "${ZONE}" >/dev/null 2>&1; then
    gcloud compute instances create "${INSTANCE_NAME}" \
        --project "${PROJECT_ID}" \
        --zone "${ZONE}" \
        --machine-type e2-micro \
        --image-family debian-12 \
        --image-project debian-cloud \
        --boot-disk-size 30GB \
        --boot-disk-type pd-standard
else
    echo "    (already exists)"
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

Finish setup on the VM:
  # 1. Copy your service-account key up (created by scripts/setup_gcp.sh):
  gcloud compute scp gcp-service-account.json ${INSTANCE_NAME}:~/ --zone ${ZONE}

  # 2. SSH in and run the on-VM setup:
  gcloud compute ssh ${INSTANCE_NAME} --zone ${ZONE}
  #   then, on the VM:
  curl -fsSL https://raw.githubusercontent.com/jackrsteiner/open-forest-sentinel/main/scripts/vm_setup.sh | bash
  #   (or clone the repo and run scripts/vm_setup.sh)

The dashboard is NOT exposed publicly by default. To reach it without opening the
firewall, use an SSH tunnel:
  gcloud compute ssh ${INSTANCE_NAME} --zone ${ZONE} -- -L ${DASHBOARD_PORT}:localhost:${DASHBOARD_PORT}
EOF

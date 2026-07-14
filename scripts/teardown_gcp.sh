#!/usr/bin/env bash
# Tear down the Google Cloud infrastructure created for an Open Forest Sentinel
# instance. Two modes:
#
#   ./scripts/teardown_gcp.sh          delete the created resources (VM, firewall
#                                      rule, staging bucket, service accounts,
#                                      Workload Identity Federation provider/pool)
#                                      but keep the project, its billing link, and
#                                      the Earth Engine registration
#   ./scripts/teardown_gcp.sh --nuke   delete the ENTIRE GCP project instead
#
# Prerequisites: the `gcloud` CLI, authenticated as a user who can administer the
# project. Missing resources are skipped, so a partial deploy tears down cleanly.
#
# Configure via environment variables (defaults match the setup scripts):
#   PROJECT_ID            GCP project id                      (required)
#   ZONE                  VM zone                  (default: us-west1-a)
#   INSTANCE_NAME         VM name              (default: forest-sentinel-vm)
#   STAGING_BUCKET        staging bucket   (default: ${PROJECT_ID}-ofs-staging)
#   SERVICE_ACCOUNT_NAME  pipeline SA id (default: forest-sentinel-pipeline)
#   PROVISIONER_NAME      provisioner SA id
#                         (default: forest-sentinel-provisioner)
#   POOL_ID               workload identity pool id     (default: github)
#   PROVIDER_ID           OIDC provider id              (default: github-oidc)
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your GCP project id}"
ZONE="${ZONE:-us-west1-a}"
INSTANCE_NAME="${INSTANCE_NAME:-forest-sentinel-vm}"
STAGING_BUCKET="${STAGING_BUCKET:-${PROJECT_ID}-ofs-staging}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-forest-sentinel-pipeline}"
PROVISIONER_NAME="${PROVISIONER_NAME:-forest-sentinel-provisioner}"
POOL_ID="${POOL_ID:-github}"
PROVIDER_ID="${PROVIDER_ID:-github-oidc}"

SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
PROVISIONER_SA="${PROVISIONER_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if ! command -v gcloud >/dev/null 2>&1; then
    echo "error: 'gcloud' is not installed. See https://cloud.google.com/sdk/docs/install" >&2
    exit 1
fi

confirm() {
    printf '%s [y/N] ' "$1"
    read -r reply
    case "${reply}" in
        y|Y|yes|YES) ;;
        *) echo "Aborted."; exit 1 ;;
    esac
}

if [ "${1:-}" = "--nuke" ]; then
    confirm "DELETE the entire project '${PROJECT_ID}' (everything in it, including the Earth Engine registration)?"
    gcloud projects delete "${PROJECT_ID}"
    echo "Project ${PROJECT_ID} scheduled for deletion (recoverable for ~30 days)."
    exit 0
fi

confirm "Delete the Open Forest Sentinel resources in project '${PROJECT_ID}' (VM, bucket, service accounts, WIF)?"

echo "==> Deleting VM ${INSTANCE_NAME} (${ZONE})"
if gcloud compute instances describe "${INSTANCE_NAME}" \
        --project "${PROJECT_ID}" --zone "${ZONE}" >/dev/null 2>&1; then
    gcloud compute instances delete "${INSTANCE_NAME}" \
        --project "${PROJECT_ID}" --zone "${ZONE}" --quiet
else
    echo "    (not found)"
fi

echo "==> Deleting firewall rule ofs-dashboard"
if gcloud compute firewall-rules describe ofs-dashboard --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud compute firewall-rules delete ofs-dashboard --project "${PROJECT_ID}" --quiet
else
    echo "    (not found)"
fi

echo "==> Deleting staging bucket gs://${STAGING_BUCKET} (and its objects)"
if gcloud storage buckets describe "gs://${STAGING_BUCKET}" >/dev/null 2>&1; then
    gcloud storage rm -r "gs://${STAGING_BUCKET}" --quiet
else
    echo "    (not found)"
fi

for sa in "${SA_EMAIL}" "${PROVISIONER_SA}"; do
    echo "==> Deleting service account ${sa}"
    if gcloud iam service-accounts describe "${sa}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
        gcloud iam service-accounts delete "${sa}" --project "${PROJECT_ID}" --quiet
    else
        echo "    (not found)"
    fi
done

echo "==> Deleting Workload Identity Federation provider + pool"
if gcloud iam workload-identity-pools providers describe "${PROVIDER_ID}" \
        --project "${PROJECT_ID}" --location global \
        --workload-identity-pool "${POOL_ID}" >/dev/null 2>&1; then
    gcloud iam workload-identity-pools providers delete "${PROVIDER_ID}" \
        --project "${PROJECT_ID}" --location global \
        --workload-identity-pool "${POOL_ID}" --quiet
else
    echo "    (provider not found)"
fi
if gcloud iam workload-identity-pools describe "${POOL_ID}" \
        --project "${PROJECT_ID}" --location global >/dev/null 2>&1; then
    gcloud iam workload-identity-pools delete "${POOL_ID}" \
        --project "${PROJECT_ID}" --location global --quiet
else
    echo "    (pool not found)"
fi

cat <<EOF

Teardown complete. Notes:
  - The project, billing link, and Earth Engine registration were kept.
    Use '$0 --nuke' to delete the whole project instead.
  - Deleted WIF pools/providers are soft-deleted for 30 days. To redeploy into
    this project within that window, either undelete them
    (gcloud iam workload-identity-pools undelete ${POOL_ID} --location global)
    or re-run setup_wif.sh with a different POOL_ID.
EOF

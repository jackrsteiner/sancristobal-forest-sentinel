#!/usr/bin/env bash
# Provision the Google Cloud resources Open Forest Sentinel needs for the
# Earth Engine pipeline: enable APIs, create a service account + key, and create
# the transient GCS staging bucket. Idempotent — existing resources are reused.
#
# Prerequisites: the `gcloud` CLI, authenticated as a user who can administer the
# project (`gcloud auth login`), and a billing-enabled GCP project.
#
# Earth Engine itself must be enabled for the project once, interactively, at
# https://code.earthengine.google.com/register — this script prints the link.
#
# Configure via environment variables (all optional except PROJECT_ID):
#   PROJECT_ID            GCP project id                      (required)
#   REGION                bucket location          (default: us-west1)
#   SERVICE_ACCOUNT_NAME  service account id     (default: forest-sentinel-pipeline)
#   STAGING_BUCKET        GCS staging bucket name  (default: ${PROJECT_ID}-ofs-staging)
#   KEY_FILE              output path for the SA key (default: ./gcp-service-account.json)
#   STAGING_TTL_DAYS      auto-delete staged objects after N days (default: 1)
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your GCP project id}"
REGION="${REGION:-us-west1}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-forest-sentinel-pipeline}"
STAGING_BUCKET="${STAGING_BUCKET:-${PROJECT_ID}-ofs-staging}"
KEY_FILE="${KEY_FILE:-./gcp-service-account.json}"
STAGING_TTL_DAYS="${STAGING_TTL_DAYS:-1}"

SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if ! command -v gcloud >/dev/null 2>&1; then
    echo "error: 'gcloud' is not installed. See https://cloud.google.com/sdk/docs/install" >&2
    exit 1
fi

echo "==> Setting active project to ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> Enabling required APIs"
gcloud services enable \
    earthengine.googleapis.com \
    storage.googleapis.com \
    compute.googleapis.com \
    --project "${PROJECT_ID}"

echo "==> Ensuring service account ${SA_EMAIL}"
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${SERVICE_ACCOUNT_NAME}" \
        --project "${PROJECT_ID}" \
        --display-name "Open Forest Sentinel pipeline"
else
    echo "    (already exists)"
fi

echo "==> Granting roles (Earth Engine + Storage)"
for role in roles/earthengine.writer roles/storage.objectAdmin; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member "serviceAccount:${SA_EMAIL}" \
        --role "${role}" \
        --condition=None >/dev/null
done

echo "==> Ensuring GCS staging bucket gs://${STAGING_BUCKET}"
if ! gcloud storage buckets describe "gs://${STAGING_BUCKET}" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${STAGING_BUCKET}" \
        --project "${PROJECT_ID}" \
        --location "${REGION}" \
        --uniform-bucket-level-access
else
    echo "    (already exists)"
fi

echo "==> Applying a ${STAGING_TTL_DAYS}-day lifecycle rule (staging is transient)"
lifecycle_tmp="$(mktemp)"
trap 'rm -f "${lifecycle_tmp}"' EXIT
cat >"${lifecycle_tmp}" <<EOF
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": ${STAGING_TTL_DAYS}}}]}
EOF
gcloud storage buckets update "gs://${STAGING_BUCKET}" --lifecycle-file="${lifecycle_tmp}"

if [ -f "${KEY_FILE}" ]; then
    echo "==> Service-account key already present at ${KEY_FILE} (not overwriting)"
else
    echo "==> Creating service-account key at ${KEY_FILE}"
    gcloud iam service-accounts keys create "${KEY_FILE}" \
        --iam-account "${SA_EMAIL}"
    chmod 600 "${KEY_FILE}"
fi

cat <<EOF

GCP resources ready.

  Project:          ${PROJECT_ID}
  Service account:  ${SA_EMAIL}
  Key file:         ${KEY_FILE}   (chmod 600 — never commit this)
  Staging bucket:   gs://${STAGING_BUCKET}   (objects auto-deleted after ${STAGING_TTL_DAYS}d)

ONE MORE MANUAL STEP — register the project for Earth Engine (once):
  https://code.earthengine.google.com/register
  Choose the "noncommercial / unpaid" usage when prompted, and select project
  ${PROJECT_ID}. Earth Engine access cannot be enabled non-interactively.

Then set these in your environment / .env (see .env.example):
  FOREST_SENTINEL_GEE_PROJECT=${PROJECT_ID}
  FOREST_SENTINEL_GCS_STAGING_BUCKET=${STAGING_BUCKET}
  GOOGLE_APPLICATION_CREDENTIALS=$(cd "$(dirname "${KEY_FILE}")" && pwd)/$(basename "${KEY_FILE}")
EOF

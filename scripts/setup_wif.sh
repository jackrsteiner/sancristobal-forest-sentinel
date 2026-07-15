#!/usr/bin/env bash
# One-time, per-instance Workload Identity Federation bootstrap. Run this
# MANUALLY (e.g. in Cloud Shell) as a user who can administer the project; it is
# the only step that needs human credentials. Afterwards the instance repo's
# "Deploy instance" GitHub Action can provision everything else with short-lived
# OIDC tokens — no service-account keys anywhere.
#
# Creates: a workload identity pool + GitHub OIDC provider (locked to exactly
# one GitHub repository) and a "provisioner" service account the Action
# impersonates. Idempotent — existing resources are reused.
#
# Configure via environment variables:
#   PROJECT_ID        GCP project id                                (required)
#   GITHUB_REPO       owner/name of the INSTANCE repo, with GitHub's exact
#                     casing, e.g. jane-doe/My-Forest-Instance      (required)
#   POOL_ID           workload identity pool id     (default: github)
#   PROVIDER_ID       OIDC provider id              (default: github-oidc)
#   PROVISIONER_NAME  provisioner service account id
#                     (default: forest-sentinel-provisioner)
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your GCP project id}"
GITHUB_REPO="${GITHUB_REPO:?set GITHUB_REPO to owner/name of the instance repo (exact casing)}"
POOL_ID="${POOL_ID:-github}"
PROVIDER_ID="${PROVIDER_ID:-github-oidc}"
PROVISIONER_NAME="${PROVISIONER_NAME:-forest-sentinel-provisioner}"

PROVISIONER_SA="${PROVISIONER_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if ! command -v gcloud >/dev/null 2>&1; then
    echo "error: 'gcloud' is not installed. See https://cloud.google.com/sdk/docs/install" >&2
    exit 1
fi

case "${GITHUB_REPO}" in
    */*) ;;
    *) echo "error: GITHUB_REPO must be owner/name (got '${GITHUB_REPO}')" >&2; exit 1 ;;
esac

echo "==> Setting active project to ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> Enabling the IAM / STS APIs"
gcloud services enable \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    sts.googleapis.com \
    cloudresourcemanager.googleapis.com \
    --project "${PROJECT_ID}"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"

echo "==> Ensuring workload identity pool '${POOL_ID}'"
if ! gcloud iam workload-identity-pools describe "${POOL_ID}" \
        --project "${PROJECT_ID}" --location global >/dev/null 2>&1; then
    gcloud iam workload-identity-pools create "${POOL_ID}" \
        --project "${PROJECT_ID}" \
        --location global \
        --display-name "GitHub Actions"
else
    echo "    (already exists)"
fi

echo "==> Ensuring OIDC provider '${PROVIDER_ID}' (locked to ${GITHUB_REPO})"
if ! gcloud iam workload-identity-pools providers describe "${PROVIDER_ID}" \
        --project "${PROJECT_ID}" --location global \
        --workload-identity-pool "${POOL_ID}" >/dev/null 2>&1; then
    gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_ID}" \
        --project "${PROJECT_ID}" \
        --location global \
        --workload-identity-pool "${POOL_ID}" \
        --display-name "GitHub OIDC" \
        --issuer-uri "https://token.actions.githubusercontent.com" \
        --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
        --attribute-condition "assertion.repository == '${GITHUB_REPO}'"
else
    echo "    (already exists — if you need to point it at a different repo, delete it first)"
fi

echo "==> Ensuring provisioner service account ${PROVISIONER_SA}"
if ! gcloud iam service-accounts describe "${PROVISIONER_SA}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${PROVISIONER_NAME}" \
        --project "${PROJECT_ID}" \
        --display-name "Open Forest Sentinel provisioner (GitHub Actions)"
else
    echo "    (already exists)"
fi

echo "==> Granting the provisioner the roles setup_gcp.sh / provision_vm.sh need"
for role in \
    roles/serviceusage.serviceUsageAdmin \
    roles/iam.serviceAccountAdmin \
    roles/resourcemanager.projectIamAdmin \
    roles/compute.admin \
    roles/storage.admin \
    roles/iam.serviceAccountUser; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member "serviceAccount:${PROVISIONER_SA}" \
        --role "${role}" \
        --condition=None >/dev/null
done

echo "==> Allowing ${GITHUB_REPO} workflows to impersonate the provisioner"
gcloud iam service-accounts add-iam-policy-binding "${PROVISIONER_SA}" \
    --project "${PROJECT_ID}" \
    --role roles/iam.workloadIdentityUser \
    --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${GITHUB_REPO}" \
    >/dev/null

WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"

cat <<EOF

Workload Identity Federation ready. Finish wiring the instance repo:

1. Repo Settings -> Secrets and variables -> Actions -> Variables. New repository variable to add:
     WIF_PROVIDER    ${WIF_PROVIDER}
     PROVISIONER_SA  ${PROVISIONER_SA}
   (These are identifiers, not secrets — variables are fine.)

2. Generate a new fine-grained personal access token for the history-graft step
   (github.com -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens):
     Repository access:  only ${GITHUB_REPO}
     Permissions:        Contents: Read and write, Workflows: Read and write
   Save it as the repository SECRET:
     OFS_ADMIN_TOKEN

3. Commit your config (config/instance.env with PROJECT_ID=${PROJECT_ID}, and
   config/aoi.geojson), then run the "Deploy instance" workflow from the
   Actions tab.

To revoke GitHub's provisioning access later, run scripts/teardown_gcp.sh or:
  gcloud iam workload-identity-pools providers delete ${PROVIDER_ID} \\
      --location global --workload-identity-pool ${POOL_ID}
EOF

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
# Deleted pools/providers linger in a DELETED state for 30 days, and `describe`
# still finds them — so check the state, not mere existence, and undelete
# instead of treating the soft-deleted husk as usable (teardown + re-setup
# would otherwise dead-end on a NOT_FOUND at provider creation).
pool_state="$(gcloud iam workload-identity-pools describe "${POOL_ID}" \
    --project "${PROJECT_ID}" --location global --format 'value(state)' 2>/dev/null || true)"
if [ "${pool_state}" = "DELETED" ]; then
    echo "    (soft-deleted — undeleting)"
    gcloud iam workload-identity-pools undelete "${POOL_ID}" \
        --project "${PROJECT_ID}" --location global >/dev/null
elif [ -n "${pool_state}" ]; then
    echo "    (already exists)"
else
    gcloud iam workload-identity-pools create "${POOL_ID}" \
        --project "${PROJECT_ID}" \
        --location global \
        --display-name "GitHub Actions"
fi

EXPECTED_CONDITION="assertion.repository == '${GITHUB_REPO}'"

echo "==> Ensuring OIDC provider '${PROVIDER_ID}' (locked to ${GITHUB_REPO})"
provider_state="$(gcloud iam workload-identity-pools providers describe "${PROVIDER_ID}" \
    --project "${PROJECT_ID}" --location global \
    --workload-identity-pool "${POOL_ID}" --format 'value(state)' 2>/dev/null || true)"
if [ "${provider_state}" = "DELETED" ]; then
    echo "    (soft-deleted — undeleting)"
    gcloud iam workload-identity-pools providers undelete "${PROVIDER_ID}" \
        --project "${PROJECT_ID}" --location global \
        --workload-identity-pool "${POOL_ID}" >/dev/null
elif [ -n "${provider_state}" ]; then
    echo "    (already exists)"
else
    gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_ID}" \
        --project "${PROJECT_ID}" \
        --location global \
        --workload-identity-pool "${POOL_ID}" \
        --display-name "GitHub OIDC" \
        --issuer-uri "https://token.actions.githubusercontent.com" \
        --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
        --attribute-condition "${EXPECTED_CONDITION}"
fi

# Self-heal a wrong-repo provider: the repository lock is a mutable attribute
# condition, so an existing (or just-undeleted) provider pointing at a different
# repo is repointed to GITHUB_REPO instead of demanding a delete/re-create.
current_condition="$(gcloud iam workload-identity-pools providers describe "${PROVIDER_ID}" \
    --project "${PROJECT_ID}" --location global \
    --workload-identity-pool "${POOL_ID}" --format 'value(attributeCondition)' 2>/dev/null || true)"
if [ -n "${current_condition}" ] && [ "${current_condition}" != "${EXPECTED_CONDITION}" ]; then
    echo "==> Provider was locked to a different repository — repointing to ${GITHUB_REPO}"
    echo "    (was: ${current_condition})"
    gcloud iam workload-identity-pools providers update-oidc "${PROVIDER_ID}" \
        --project "${PROJECT_ID}" \
        --location global \
        --workload-identity-pool "${POOL_ID}" \
        --attribute-condition "${EXPECTED_CONDITION}"
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

MEMBER_PREFIX="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/"
EXPECTED_MEMBER="${MEMBER_PREFIX}${GITHUB_REPO}"

echo "==> Allowing ${GITHUB_REPO} workflows to impersonate the provisioner"
gcloud iam service-accounts add-iam-policy-binding "${PROVISIONER_SA}" \
    --project "${PROJECT_ID}" \
    --role roles/iam.workloadIdentityUser \
    --member "${EXPECTED_MEMBER}" \
    >/dev/null

# After a repoint, drop grants left behind for other repositories under this
# pool, so the previous repository keeps no path to the provisioner. Scoped to
# this pool's attribute.repository members only.
stale_members="$(gcloud iam service-accounts get-iam-policy "${PROVISIONER_SA}" \
    --project "${PROJECT_ID}" \
    --flatten 'bindings[].members' \
    --filter 'bindings.role:roles/iam.workloadIdentityUser' \
    --format 'value(bindings.members)' 2>/dev/null \
    | grep -F "${MEMBER_PREFIX}" | grep -Fxv "${EXPECTED_MEMBER}" || true)"
while IFS= read -r member; do
    [ -n "${member}" ] || continue
    echo "    removing stale grant for ${member#"${MEMBER_PREFIX}"}"
    gcloud iam service-accounts remove-iam-policy-binding "${PROVISIONER_SA}" \
        --project "${PROJECT_ID}" \
        --role roles/iam.workloadIdentityUser \
        --member "${member}" \
        >/dev/null
done <<EOF
${stale_members}
EOF

# --- Sync PROJECT_ID into the committed instance config (best-effort) -------
# Only when this shell is inside a clone of the instance repo itself (origin
# must match GITHUB_REPO), so a clone of the base repo or an unrelated checkout
# is never touched. The edit and commit always work; the push needs GitHub
# credentials in this shell and degrades to a "run git push" instruction.
INSTANCE_ENV_FILE="config/instance.env"
CONFIG_STATUS="manual" # manual | synced | committed | pushed
AOI_IS_SAMPLE=0
repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
origin_url="$(git remote get-url origin 2>/dev/null || true)"
origin_lc="$(printf '%s' "${origin_url}" | tr '[:upper:]' '[:lower:]')"
repo_lc="$(printf '%s' "${GITHUB_REPO}" | tr '[:upper:]' '[:lower:]')"
case "${origin_lc}" in
    *"/${repo_lc}" | *"/${repo_lc}.git" | *":${repo_lc}" | *":${repo_lc}.git")
        if [ -n "${repo_root}" ] && [ -f "${repo_root}/${INSTANCE_ENV_FILE}" ]; then
            current_project="$(sed -n 's/^PROJECT_ID=//p' "${repo_root}/${INSTANCE_ENV_FILE}" | head -n 1)"
            if [ "${current_project}" = "${PROJECT_ID}" ]; then
                echo "==> ${INSTANCE_ENV_FILE} already sets PROJECT_ID=${PROJECT_ID}"
                CONFIG_STATUS="synced"
            else
                echo "==> Setting PROJECT_ID=${PROJECT_ID} in ${INSTANCE_ENV_FILE}"
                sed -i "s|^PROJECT_ID=.*|PROJECT_ID=${PROJECT_ID}|" "${repo_root}/${INSTANCE_ENV_FILE}"
                if ! git -C "${repo_root}" config user.email >/dev/null 2>&1; then
                    git -C "${repo_root}" config user.name "setup_wif.sh"
                    git -C "${repo_root}" config user.email "setup-wif@noreply.local"
                fi
                git -C "${repo_root}" commit -q \
                    -m "Set PROJECT_ID=${PROJECT_ID} in config/instance.env" \
                    -- "${INSTANCE_ENV_FILE}"
                CONFIG_STATUS="committed"
                branch="$(git -C "${repo_root}" rev-parse --abbrev-ref HEAD)"
                echo "==> Pushing to ${GITHUB_REPO} — git may prompt for GitHub credentials"
                echo "    (username + a fine-grained PAT as the password). To skip the push,"
                echo "    press Enter at the prompts (or Ctrl-C); the commit is kept either way."
                # No-op INT handler (not ''): Ctrl-C stops only the child git
                # push, and the script carries on into the skipped-push branch.
                trap ':' INT
                if git -C "${repo_root}" push origin "${branch}"; then
                    echo "    (committed and pushed to ${GITHUB_REPO})"
                    CONFIG_STATUS="pushed"
                else
                    echo "    (committed locally; push skipped. To push it later, authenticate —"
                    echo "     e.g. gh auth login && gh auth setup-git — then run: git push)"
                fi
                trap - INT
            fi
            if [ -f "${repo_root}/examples/aoi-sample.geojson" ] \
                    && cmp -s "${repo_root}/config/aoi.geojson" "${repo_root}/examples/aoi-sample.geojson"; then
                AOI_IS_SAMPLE=1
            fi
        fi
        ;;
esac

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

EOF

case "${CONFIG_STATUS}" in
    pushed)
        cat <<EOF
3. config/instance.env now sets PROJECT_ID=${PROJECT_ID} (committed and pushed).
   Make sure config/aoi.geojson is your own AOI (committed + pushed).
EOF
        ;;
    committed)
        cat <<EOF
3. config/instance.env now sets PROJECT_ID=${PROJECT_ID}, committed locally but
   NOT pushed (authenticate to GitHub, then: git push). Make sure
   config/aoi.geojson is your own AOI, and push both.
EOF
        ;;
    synced)
        cat <<EOF
3. config/instance.env already sets PROJECT_ID=${PROJECT_ID}. Make sure it and
   your config/aoi.geojson are pushed.
EOF
        ;;
    *)
        cat <<EOF
3. Commit and push your config (config/instance.env with
   PROJECT_ID=${PROJECT_ID}, and config/aoi.geojson).
EOF
        ;;
esac

if [ "${AOI_IS_SAMPLE}" = "1" ]; then
    cat <<EOF

   Note: config/aoi.geojson is still the bundled sample AOI — replace it with
   your own (build one at https://jackrsteiner.github.io/aoi-maker/) before
   deploying.
EOF
fi

# Tunnel details for the closing instructions: honor the committed config when
# this ran inside the instance clone, otherwise fall back to the defaults.
TUNNEL_INSTANCE="forest-sentinel-vm"
TUNNEL_ZONE="us-west1-a"
TUNNEL_PORT="8000"
if [ "${CONFIG_STATUS}" != "manual" ] && [ -f "${repo_root}/${INSTANCE_ENV_FILE}" ]; then
    v="$(sed -n 's/^INSTANCE_NAME=//p' "${repo_root}/${INSTANCE_ENV_FILE}" | head -n 1)"
    [ -n "${v}" ] && TUNNEL_INSTANCE="${v}"
    v="$(sed -n 's/^ZONE=//p' "${repo_root}/${INSTANCE_ENV_FILE}" | head -n 1)"
    [ -n "${v}" ] && TUNNEL_ZONE="${v}"
    v="$(sed -n 's/^DASHBOARD_PORT=//p' "${repo_root}/${INSTANCE_ENV_FILE}" | head -n 1)"
    [ -n "${v}" ] && TUNNEL_PORT="${v}"
fi

cat <<EOF

4. Run the deployment: repo -> Actions tab -> "Deploy instance" -> Run workflow.
   It grafts the template history, provisions GCP and the VM, and waits for the
   dashboard to come up (the first boot takes ~8 minutes).

5. View the dashboard. From Cloud Shell (8080 is the Cloud Shell Web-Preview
   port; the dashboard itself serves on ${TUNNEL_PORT}):
     gcloud compute ssh ${TUNNEL_INSTANCE} --zone ${TUNNEL_ZONE} -- -N -4 -L 8080:localhost:${TUNNEL_PORT}
   then click the "Web Preview" button (the small monitor-with-a-dot icon at
   the top right of the Cloud Shell toolbar) -> "Preview on port 8080".
   (From your own machine instead: ... -L ${TUNNEL_PORT}:localhost:${TUNNEL_PORT} and open
   http://localhost:${TUNNEL_PORT})

To revoke GitHub's provisioning access later, run scripts/teardown_gcp.sh or:
  gcloud iam workload-identity-pools providers delete ${PROVIDER_ID} \\
      --location global --workload-identity-pool ${POOL_ID}
EOF

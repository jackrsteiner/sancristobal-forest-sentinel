#!/usr/bin/env bash
# GCE startup script for Open Forest Sentinel instances. provision_vm.sh attaches
# this as `startup-script` metadata, so it runs AS ROOT on EVERY boot. It creates
# the unprivileged app user, clones the instance repo, and delegates to
# scripts/vm_setup.sh (which is idempotent). The initial pipeline run is
# triggered on the first boot only.
#
# Reads instance metadata attributes (set by provision_vm.sh):
#   ofs-repo-url      git URL of the instance repo (required)
#   ofs-repo-ref      branch / tag / sha            (default: main)
#   ofs-clone-token   optional short-lived token for cloning a private repo
#
# Reports progress via the `ofs/setup-status` guest attribute (`done`/`failed`),
# which the deploy workflow polls. Logs to /var/log/ofs-startup.log.
set -euo pipefail

exec >>/var/log/ofs-startup.log 2>&1
echo "==> ofs startup script: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

OFS_USER="ofs"
APP_DIR="/home/${OFS_USER}/open-forest-sentinel"
SENTINEL="/var/lib/ofs/first-boot-done"
METADATA="http://metadata.google.internal/computeMetadata/v1/instance"

attr() {
    curl -sf -H "Metadata-Flavor: Google" "${METADATA}/attributes/$1" 2>/dev/null || true
}

set_status() {
    curl -sf -X PUT -H "Metadata-Flavor: Google" \
        -H "Content-Type: text/plain" \
        -d "$1" "${METADATA}/guest-attributes/ofs/setup-status" >/dev/null || true
}

trap 'echo "==> ofs startup FAILED (line $LINENO)"; set_status failed' ERR

set_status running

REPO_URL="$(attr ofs-repo-url)"
REPO_REF="$(attr ofs-repo-ref)"
CLONE_TOKEN="$(attr ofs-clone-token)"
REPO_REF="${REPO_REF:-main}"
if [ -z "${REPO_URL}" ]; then
    echo "error: 'ofs-repo-url' instance metadata is not set" >&2
    exit 1
fi

echo "==> Ensuring base packages (git, curl, sudo)"
apt-get update -y
apt-get install -y git curl sudo ca-certificates

echo "==> Ensuring app user '${OFS_USER}'"
if ! id "${OFS_USER}" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "${OFS_USER}"
fi
# vm_setup.sh uses sudo internally (apt, docker, systemd units).
echo "${OFS_USER} ALL=(ALL) NOPASSWD:ALL" >"/etc/sudoers.d/${OFS_USER}"
chmod 440 "/etc/sudoers.d/${OFS_USER}"

echo "==> Ensuring instance repo at ${APP_DIR} (${REPO_URL} @ ${REPO_REF})"
CLONE_URL="${REPO_URL}"
if [ -n "${CLONE_TOKEN}" ]; then
    # Token only for the initial clone of a private repo; the on-disk remote is
    # reset to the tokenless URL below (the token is short-lived job credential).
    CLONE_URL="https://x-access-token:${CLONE_TOKEN}@${REPO_URL#https://}"
fi
if [ ! -d "${APP_DIR}/.git" ]; then
    sudo -u "${OFS_USER}" -H git clone "${CLONE_URL}" "${APP_DIR}"
    sudo -u "${OFS_USER}" -H git -C "${APP_DIR}" remote set-url origin "${REPO_URL}"
fi

RUN_INITIAL=0
if [ ! -f "${SENTINEL}" ]; then
    RUN_INITIAL=1
fi

echo "==> Running vm_setup.sh as ${OFS_USER} (RUN_INITIAL=${RUN_INITIAL})"
sudo -u "${OFS_USER}" -H \
    env REPO_URL="${REPO_URL}" REPO_REF="${REPO_REF}" APP_DIR="${APP_DIR}" \
        RUN_INITIAL="${RUN_INITIAL}" \
    bash "${APP_DIR}/scripts/vm_setup.sh"

mkdir -p "$(dirname "${SENTINEL}")"
touch "${SENTINEL}"

set_status "done"
echo "==> ofs startup script finished OK"

#!/usr/bin/env bash
# On-VM provisioning for Open Forest Sentinel. Run this ON the Compute Engine VM
# (Debian 12). Installs Docker + uv, clones the repo, starts PostgreSQL + PostGIS,
# applies migrations, and installs the systemd units for the dashboard and the
# scheduled pipeline. Idempotent.
#
# Expects a service-account key at ~/gcp-service-account.json (copied up by
# provision_vm.sh) and the AOI/window settings provided via the env file it writes.
#
# Configure via environment variables:
#   REPO_URL   git URL to clone   (default: https://github.com/jackrsteiner/open-forest-sentinel.git)
#   REPO_REF   branch / tag / sha (default: main)
#   APP_DIR    checkout location  (default: $HOME/open-forest-sentinel)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/jackrsteiner/open-forest-sentinel.git}"
REPO_REF="${REPO_REF:-main}"
APP_DIR="${APP_DIR:-$HOME/open-forest-sentinel}"
KEY_SRC="${KEY_SRC:-$HOME/gcp-service-account.json}"

echo "==> Installing system packages (git, Docker, uv)"
sudo apt-get update -y
sudo apt-get install -y git ca-certificates curl
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER" || true
fi
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="${HOME}/.local/bin:${PATH}"

echo "==> Cloning ${REPO_URL} @ ${REPO_REF} into ${APP_DIR}"
if [ ! -d "${APP_DIR}/.git" ]; then
    git clone "${REPO_URL}" "${APP_DIR}"
fi
cd "${APP_DIR}"
git fetch origin "${REPO_REF}"
git checkout "${REPO_REF}"
git pull --ff-only origin "${REPO_REF}" || true

echo "==> Installing the canonical COG store at /data/cogs"
sudo mkdir -p /data/cogs
sudo chown -R "$USER" /data

echo "==> Placing the service-account key"
if [ -f "${KEY_SRC}" ]; then
    install -m 600 "${KEY_SRC}" "${APP_DIR}/gcp-service-account.json"
else
    echo "warning: ${KEY_SRC} not found — copy your key to ${APP_DIR}/gcp-service-account.json" >&2
fi

echo "==> Writing ${APP_DIR}/.env (edit it to set your project, bucket, and AOI)"
if [ ! -f "${APP_DIR}/.env" ]; then
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    {
        echo "FOREST_SENTINEL_COG_ROOT=/data/cogs"
        echo "GOOGLE_APPLICATION_CREDENTIALS=${APP_DIR}/gcp-service-account.json"
    } >>"${APP_DIR}/.env"
fi

echo "==> Starting PostgreSQL + PostGIS and applying migrations"
uv sync
# sudo: the docker-group membership granted above does not apply to the current
# login session, so plain `docker` would fail on the first (fresh-VM) run.
sudo docker compose up -d
for _ in $(seq 1 30); do
    sudo docker compose exec -T db pg_isready -U forest_sentinel >/dev/null 2>&1 && break
    sleep 2
done
set -a; . "${APP_DIR}/.env"; set +a
uv run alembic upgrade head

echo "==> Installing systemd units"
sed "s#@APP_DIR@#${APP_DIR}#g; s#@USER@#${USER}#g" \
    scripts/systemd/forest-sentinel-dashboard.service \
    | sudo tee /etc/systemd/system/forest-sentinel-dashboard.service >/dev/null
sed "s#@APP_DIR@#${APP_DIR}#g; s#@USER@#${USER}#g" \
    scripts/systemd/forest-sentinel-pipeline.service \
    | sudo tee /etc/systemd/system/forest-sentinel-pipeline.service >/dev/null
sudo cp scripts/systemd/forest-sentinel-pipeline.timer \
    /etc/systemd/system/forest-sentinel-pipeline.timer
sudo systemctl daemon-reload
sudo systemctl enable --now forest-sentinel-dashboard.service
sudo systemctl enable --now forest-sentinel-pipeline.timer

cat <<EOF

On-VM setup complete.

  Edit ${APP_DIR}/.env to set FOREST_SENTINEL_GEE_PROJECT, FOREST_SENTINEL_GCS_STAGING_BUCKET,
  and AOI_PATH / WINDOW_DAYS for the scheduled run, then:
    sudo systemctl restart forest-sentinel-dashboard.service

  Dashboard:        systemctl status forest-sentinel-dashboard
  Scheduled run:    systemctl status forest-sentinel-pipeline.timer
  Trigger a run now: sudo systemctl start forest-sentinel-pipeline.service
  Logs:             journalctl -u forest-sentinel-pipeline -f
EOF

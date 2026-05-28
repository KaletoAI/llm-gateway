#!/usr/bin/env bash
set -euo pipefail

HOST="${DEPLOY_HOST:?Set DEPLOY_HOST=user@host (e.g. root@192.168.1.10)}"
DEST="/opt/llm-gateway"
SERVICE="llm-gateway"

cd "$(dirname "$(readlink -f "$0")")"

echo "==> Syncing files to ${HOST}:${DEST}"
rsync -az --delete --human-readable --info=stats1,progress2 \
    --exclude='.git/' \
    --exclude='.claude/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='.env' \
    --exclude='config.yaml' \
    --exclude='config.yaml.bak*' \
    --exclude='stats.db' \
    --exclude='stats.db-wal' \
    --exclude='stats.db-shm' \
    --exclude='deploy.sh' \
    ./ "${HOST}:${DEST}/"

echo "==> Ensuring venv + installing requirements + syncing systemd unit"
ssh "${HOST}" bash -se <<EOF
set -euo pipefail
cd "${DEST}"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt

UNIT_SRC="${DEST}/${SERVICE}.service"
UNIT_DST="/etc/systemd/system/${SERVICE}.service"
if [ ! -f "\${UNIT_DST}" ] || ! cmp -s "\${UNIT_SRC}" "\${UNIT_DST}"; then
    echo "    unit file changed -> installing + daemon-reload"
    install -m 0644 "\${UNIT_SRC}" "\${UNIT_DST}"
    systemctl daemon-reload
    systemctl enable ${SERVICE} >/dev/null
fi
EOF

echo "==> Restarting ${SERVICE}"
ssh "${HOST}" "systemctl restart ${SERVICE}"

echo "==> Service status"
ssh "${HOST}" "systemctl is-active ${SERVICE} && systemctl status ${SERVICE} --no-pager --lines=5"

echo "==> Done."

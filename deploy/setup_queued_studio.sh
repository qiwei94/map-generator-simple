#!/usr/bin/env bash
set -euo pipefail

# Install the web queue and one local compute worker as separate services.
# By default this script does NOT restart anything. Pass --activate only after
# confirming /api/jobs contains no running task.

ROOT_DIR="${MAP_STUDIO_ROOT:-/root/map-generator-simple}"
ENV_DIR="/etc/map-generator"
ENV_FILE="${ENV_DIR}/studio.env"
ACTIVATE="${1:-}"

if [[ "$(id -u)" != "0" ]]; then
  echo "run as root" >&2
  exit 1
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "missing ${ENV_FILE}; copy deploy/studio.env.example and fill secrets" >&2
  exit 1
fi
if grep -q "CHANGE_ME" "${ENV_FILE}"; then
  echo "${ENV_FILE} still contains CHANGE_ME" >&2
  exit 1
fi

chmod 600 "${ENV_FILE}"

install -m 0644 /dev/stdin /etc/systemd/system/studio.service <<EOF
[Unit]
Description=Map Relief Studio API and durable queue
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT_DIR}
EnvironmentFile=${ENV_FILE}
Environment="PATH=/opt/pyshim:/usr/local/python3.9/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=/usr/local/python3.9/bin/python3.9 webapp/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

install -m 0644 /dev/stdin /etc/systemd/system/worker.service <<EOF
[Unit]
Description=Map Relief single compute worker
After=network-online.target studio.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT_DIR}
EnvironmentFile=${ENV_FILE}
Environment="PATH=/opt/pyshim:/usr/local/python3.9/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="OSMIUM_BIN=/opt/osmium-native/bin/osmium"
ExecStart=/usr/local/python3.9/bin/python3.9 tools/cloud_worker.py --server http://127.0.0.1 --token \${WORKER_TOKEN} --worker-id local-primary --poll-interval 3
Restart=always
RestartSec=5
TimeoutStopSec=30
KillMode=control-group

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable studio.service worker.service

if [[ "${ACTIVATE}" == "--activate" ]]; then
  running="$(/usr/local/python3.9/bin/python3.9 -c "import json,pathlib; p=pathlib.Path('${ROOT_DIR}/tmp/webapp_jobs/_jobs.json'); d=json.loads(p.read_text()) if p.exists() else {}; print(sum(j.get('status') in ('starting','running') for j in d.values()))")"
  if [[ "${running}" != "0" ]]; then
    echo "refusing activation: ${running} task(s) are still running" >&2
    exit 2
  fi
  systemctl restart studio.service
  systemctl restart worker.service
  systemctl --no-pager --full status studio.service worker.service
else
  echo "units installed but not restarted"
  echo "after active jobs finish: $0 --activate"
fi

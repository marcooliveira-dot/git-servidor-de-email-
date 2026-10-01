#!/usr/bin/env bash
set -euo pipefail
if [[ "$EUID" -ne 0 ]]; then
  echo 'Execute: sudo bash deploy/install.sh'; exit 1
fi
source /etc/os-release
if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 24.04 && "${VERSION_ID:-}" != 26.04 ]]; then
  echo 'Este instalador foi preparado para Ubuntu 24.04 ou 26.04.'; exit 1
fi
checkout_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$checkout_dir" != /opt/email-archive/app ]]; then
  echo 'Clone o repositório em /opt/email-archive/app antes de executar.'; exit 1
fi
apt-get update
apt-get install -y python3 python3-venv postgresql nginx ca-certificates
if ! id email-archive >/dev/null 2>&1; then
  adduser --system --group --home /var/lib/email-archive email-archive
fi
install -d -o email-archive -g email-archive -m 0750 /var/lib/email-archive
install -d -o root -g email-archive -m 0750 /etc/email-archive
python3 -m venv /opt/email-archive/app/.venv
/opt/email-archive/app/.venv/bin/pip install -r /opt/email-archive/app/requirements-lock.txt
if [[ ! -e /etc/email-archive/archive.env ]]; then
  /opt/email-archive/app/.venv/bin/python -m email_archive.cli generate-env --output /etc/email-archive/archive.env
fi
install -o root -g root -m 0644 deploy/email-archive-web.service /etc/systemd/system/email-archive-web.service
install -o root -g root -m 0644 deploy/email-archive-worker.service /etc/systemd/system/email-archive-worker.service
install -o root -g root -m 0755 deploy/manage /usr/local/sbin/email-archive-manage
systemctl daemon-reload
printf '%s\n' 'Preparação concluída. Serviços ainda não iniciados.' 'Configure banco, buckets e archive.env conforme INSTALL.md.'

#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
expected_root="/opt/portfolio-tracker"
runtime_root="/var/lib/portfolio-tracker"
config_root="/etc/portfolio-tracker"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ${project_root}/scripts/install-vps.sh" >&2
  exit 1
fi

if [[ "${project_root}" != "${expected_root}" ]]; then
  echo "Repository must be installed at ${expected_root}; found ${project_root}" >&2
  exit 1
fi

if ! id -u portfolio >/dev/null 2>&1; then
  useradd \
    --system \
    --home-dir "${runtime_root}" \
    --shell /usr/sbin/nologin \
    portfolio
fi

install -d -m 0700 -o portfolio -g portfolio \
  "${runtime_root}" \
  "${runtime_root}/ledger" \
  "${runtime_root}/snapshots" \
  "${runtime_root}/backups" \
  "${runtime_root}/locks" \
  "${runtime_root}/state" \
  "${runtime_root}/quarantine"
install -d -m 0700 -o root -g root "${config_root}"

if [[ ! -e "${config_root}/portfolio.env" ]]; then
  install -m 0600 -o root -g root \
    "${project_root}/config/portfolio.env.example" \
    "${config_root}/portfolio.env"
fi

for source in "${project_root}"/systemd/*.example; do
  unit_name="$(basename "${source}" .example)"
  install -m 0644 -o root -g root \
    "${source}" \
    "/etc/systemd/system/${unit_name}"
done

systemctl daemon-reload

echo "Portfolio Tracker files installed."
echo "Next:"
echo "  1. Edit ${config_root}/portfolio.env and set PORTFOLIO_GITHUB_TOKEN."
echo "  2. Create paper/live PORTFOLIO_OPEN events with stable IDs."
echo "  3. Run one rebuild and explicit first publish/bootstrap."
echo "  4. Enable the path/timer units only after the checks above pass."

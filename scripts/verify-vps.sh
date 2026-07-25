#!/usr/bin/env bash
set -euo pipefail

project_root="/opt/portfolio-tracker"
runtime_root="/var/lib/portfolio-tracker"
config_file="/etc/portfolio-tracker/portfolio.env"
failed=0

check() {
  local description="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf 'PASS  %s\n' "${description}"
  else
    printf 'FAIL  %s\n' "${description}"
    failed=1
  fi
}

environment_mode_is_private() {
  [[ -f "${config_file}" ]] &&
    [[ "$(stat -c '%a' "${config_file}")" == "600" ]]
}

runtime_owner_is_portfolio() {
  [[ -d "${runtime_root}" ]] &&
    [[ "$(stat -c '%U' "${runtime_root}")" == "portfolio" ]]
}

check "project path" test -d "${project_root}/backend/portfolio_tracker"
check "portfolio service user" id -u portfolio
check "runtime directory" test -d "${runtime_root}/ledger"
check "private environment file" test -f "${config_file}"
check "environment file mode 600" environment_mode_is_private
check "runtime owned by portfolio" runtime_owner_is_portfolio
check "Python syntax" \
  /usr/bin/python3 -m compileall -q \
  "${project_root}/backend/portfolio_tracker" \
  "${project_root}/backend/integrations"
check "systemd unit syntax" \
  systemd-analyze verify \
  /etc/systemd/system/portfolio-rebuild.service \
  /etc/systemd/system/portfolio-publish.service \
  /etc/systemd/system/portfolio-backup.service

if [[ "${failed}" -ne 0 ]]; then
  exit 1
fi

printf 'VPS installation checks passed. No token value was displayed.\n'

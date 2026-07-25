#!/usr/bin/env bash
set -euo pipefail

project_root="/opt/portfolio-tracker"
runtime_root="/var/lib/portfolio-tracker"
config_file="/etc/portfolio-tracker/portfolio.env"
failed=0
check_active=false

if [[ "${1:-}" == "--active" ]]; then
  check_active=true
elif [[ $# -gt 0 ]]; then
  printf 'Usage: %s [--active]\n' "$0" >&2
  exit 2
fi

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

runtime_tree_is_private() {
  local path
  for path in \
    "${runtime_root}" \
    "${runtime_root}/ledger" \
    "${runtime_root}/snapshots" \
    "${runtime_root}/backups" \
    "${runtime_root}/locks" \
    "${runtime_root}/state" \
    "${runtime_root}/quarantine"; do
    [[ -d "${path}" ]] || return 1
    [[ "$(stat -c '%a' "${path}")" == "700" ]] || return 1
    [[ "$(stat -c '%U' "${path}")" == "portfolio" ]] || return 1
  done
}

github_token_is_set() {
  grep -Eq '^PORTFOLIO_GITHUB_TOKEN=.+$' "${config_file}"
}

triggers_are_enabled_and_active() {
  local unit
  for unit in \
    portfolio-rebuild.path \
    portfolio-rebuild.timer \
    portfolio-publish.path \
    portfolio-publish.timer \
    portfolio-backup.timer; do
    systemctl is-enabled --quiet "${unit}" || return 1
    systemctl is-active --quiet "${unit}" || return 1
  done
}

check "project path" test -d "${project_root}/backend/portfolio_tracker"
check "portfolio service user" id -u portfolio
check "runtime directory" test -d "${runtime_root}/ledger"
check "private environment file" test -f "${config_file}"
check "environment file mode 600" environment_mode_is_private
check "GitHub token configured" github_token_is_set
check "runtime owned by portfolio" runtime_owner_is_portfolio
check "runtime tree mode 700 and ownership" runtime_tree_is_private
check "Python syntax" \
  /usr/bin/python3 -m compileall -q \
  "${project_root}/backend/portfolio_tracker" \
  "${project_root}/backend/integrations"
check "systemd unit syntax" \
  systemd-analyze verify \
  /etc/systemd/system/portfolio-rebuild.service \
  /etc/systemd/system/portfolio-rebuild.path \
  /etc/systemd/system/portfolio-rebuild.timer \
  /etc/systemd/system/portfolio-publish.service \
  /etc/systemd/system/portfolio-publish.path \
  /etc/systemd/system/portfolio-publish.timer \
  /etc/systemd/system/portfolio-backup.service \
  /etc/systemd/system/portfolio-backup.timer

if [[ "${check_active}" == true ]]; then
  check "path/timer triggers enabled and active" triggers_are_enabled_and_active
fi

if [[ "${failed}" -ne 0 ]]; then
  exit 1
fi

printf 'VPS installation checks passed. No token value was displayed.\n'

#!/usr/bin/env bash
# Configure Snapper and hibernation; run after booting the installed @ root.
set -euo pipefail
PREBOOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SCRIPT_DIR=$(cd "$PREBOOT_DIR/.." && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

verify_subvolumes() {
  [[ $(findmnt -n -o FSTYPE --mountpoint /) == btrfs ]] || die "Root must be Btrfs."
  [[ $(findmnt -n -o FSROOT --mountpoint /) == /@ ]] || die "Boot with rootflags=subvol=@ before configuring Snapper."
  [[ $(findmnt -n -o FSTYPE --mountpoint /.snapshots) == btrfs &&
     $(findmnt -n -o FSROOT --mountpoint /.snapshots) == /@snapshots &&
     $(findmnt -n -o UUID --mountpoint /.snapshots) == "$(findmnt -n -o UUID --mountpoint /)" ]] || die "Mount @snapshots at /.snapshots on the root filesystem first."
}

configure_snapper() {
  local template
  local -a options=()
  mkdir -p /etc/snapper/configs
  if [[ ! -f /etc/snapper/configs/root ]]; then
    for template in /etc/snapper/config-templates/default /usr/share/snapper/config-templates/default; do
      [[ -f "$template" ]] && break
    done
    [[ -f "$template" ]] || die "Snapper's default configuration template is missing."
    options+=(--initialize --template "$template")
  fi
  python3 "$SCRIPT_DIR/scripts/configure_snapper.py" "${options[@]}"
  # Reload registration/config caches before D-Bus-backed snapper commands.
  systemctl restart snapperd.service
  snapper -c root set-config SYNC_ACL=yes
  chmod 0750 /.snapshots
  # Validate the registration and permissions with an actual snapshot operation.
  local number
  number=$(snapper -c root create -t single -c number -p -d 'Setup validation')
  [[ $number =~ ^[0-9]+$ ]] || die "Invalid validation snapshot number."
  snapper -c root list
  snapper -c root delete "$number"
}

main() {
  local hibernation=1
  if [[ ${1:-} == --skip-hibernation ]]; then hibernation=0; shift; fi
  [[ $# -eq 0 ]] || die "Usage: sudo $0 [--skip-hibernation]"
  if [[ $EUID -ne 0 ]]; then
    if (( hibernation )); then exec sudo -- "$PREBOOT_DIR/${BASH_SOURCE[0]##*/}";
    else exec sudo -- "$PREBOOT_DIR/${BASH_SOURCE[0]##*/}" --skip-hibernation; fi
  fi
  require_commands findmnt python3 pacman flock cmp
  local os_id
  os_id=$(awk -F= '$1 == "ID" {gsub(/"/, "", $2); print $2}' /etc/os-release)
  case "$os_id" in endeavouros|arch) ;; *) die "Snapper setup supports EndeavourOS and Arch Linux." ;; esac
  python3 "$SCRIPT_DIR/scripts/ensure_snapshots_subvol.py" --check
  if (( hibernation )); then python3 "$SCRIPT_DIR/scripts/configure_hibernation.py" --check; fi
  start_workspace snapper
  local -a pkgs=(btrfs-progs snapper snap-pac git acl)
  if pacman -Si btrfs-assistant >/dev/null 2>&1; then
    pkgs+=(btrfs-assistant)
  else
    log_warn "btrfs-assistant is not in configured pacman repositories (available in [endeavouros] or AUR); skipping GUI tool."
  fi
  pacman -Syu --needed --noconfirm "${pkgs[@]}"
  python3 "$SCRIPT_DIR/scripts/ensure_snapshots_subvol.py"
  verify_subvolumes
  local commit
  commit=$(awk -F: '$1 == "snapper_rollback_commit" {gsub(/["[:space:]]/, "", $2); print $2}' "$SCRIPT_DIR/ansible/group_vars/all/00-defaults.yml")
  [[ $commit =~ ^[0-9a-f]{40}$ ]] || die "Invalid snapper_rollback_commit in ansible/group_vars/all/00-defaults.yml."
  git clone https://github.com/jrabinow/snapper-rollback.git "$WORK_DIR/rollback"
  git -C "$WORK_DIR/rollback" checkout --detach "$commit"
  [[ $(git -C "$WORK_DIR/rollback" rev-parse HEAD) == "$commit" ]] || die "Rollback commit verification failed."
  install -o root -g root -m 0755 "$WORK_DIR/rollback/snapper-rollback.py" /usr/local/bin/snapper-rollback
  local config="$WORK_DIR/rollback.conf" backup=""
  printf '[root]\ndev = /dev/disk/by-uuid/%s\nsubvol_main = @\nsubvol_snapshots = @snapshots\nmountpoint = /btrfsroot\n' "$(findmnt -n -o UUID --mountpoint /)" > "$config"
  if [[ -f /etc/snapper-rollback.conf ]] && ! cmp -s "$config" /etc/snapper-rollback.conf; then
    backup=$(mktemp /etc/snapper-rollback.conf.backup.XXXXXX)
    cp -a /etc/snapper-rollback.conf "$backup"
  fi
  install -o root -g root -m 0644 "$config" /etc/snapper-rollback.conf
  if (( hibernation )); then python3 "$SCRIPT_DIR/scripts/configure_hibernation.py"; fi
  configure_snapper
  systemctl daemon-reload
  systemctl enable --now snapper-cleanup.timer
  if systemctl list-unit-files snapper-boot.timer --no-legend | grep -q '^snapper-boot.timer'; then
    systemctl enable --now snapper-boot.timer
  else log_warn "This distribution does not provide snapper-boot.timer; boot snapshots are unavailable."; fi
  log_info "Snapper root snapshots, rollback configuration and snap-pac hooks are configured."
}
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi

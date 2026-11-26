#!/usr/bin/env bash
# Bootstrap v2 workstation tooling (pacman prerequisites, uv, mise, ansible-core).
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

MISE_VERSION="v2026.11.14"
MISE_URL="https://github.com/jdx/mise/releases/download/v2026.11.14/mise-v2026.11.14-linux-x64"
MISE_SHA256="6fa596291e9758106f761fea4f8e7fac6c3e7ff4714d5c0b9a76e16068db14f6"

UV_VERSION="0.13.10"
UV_URL="https://github.com/astral-sh/uv/releases/download/0.13.10/uv-x86_64-unknown-linux-gnu.tar.gz"
UV_SHA256="bf65ac1a0af10f92c522d91ac096c7c0a39657647b155577e7836c2cbbb4526c"

ANSIBLE_CORE_VERSION="2.22.0"
ANSIBLE_LINT_VERSION="26.11.1"

log_info() { printf '[INFO] %s\n' "$*"; }
log_warn() { printf '[WARN] %s\n' "$*" >&2; }
log_error() { printf '[ERROR] %s\n' "$*" >&2; }
die() { log_error "$*"; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./bootstrap.sh

Bootstraps v2 workstation tooling as your desktop user:
  - Pacman prerequisites (curl, git, gnupg, python, age, ...)
  - uv (verified release tarball)
  - mise (verified release binary)
  - ansible-core via uv + required Ansible Galaxy collections
EOF
}

require_user() {
  local uid=${1:-$EUID}
  [[ $uid -ne 0 ]] || {
    log_error "Run as your desktop user, without sudo. Individual system operations use sudo."
    return 1
  }
}

require_supported_system() {
  local os_release=${1:-/etc/os-release}
  local arch=${2:-$(uname -m)}
  local os_id

  [[ -r "$os_release" ]] || { log_error "Missing OS release file: $os_release"; return 1; }
  os_id=$(awk -F= '$1 == "ID" {gsub(/"/, "", $2); print $2}' "$os_release") || return

  case "$os_id" in
    endeavouros|arch) ;;
    *) log_error "Supported systems: EndeavourOS or Arch Linux; detected '$os_id'."; return 1 ;;
  esac

  [[ "$arch" == "x86_64" ]] || { log_error "v2 bootstrap requires x86_64; detected $arch."; return 1; }
}

verify_sha256() {
  local file=$1 expected=$2 actual
  [[ $expected =~ ^[a-fA-F0-9]{64}$ ]] || { log_error "Missing or invalid SHA-256 for $file"; return 1; }
  actual=$(sha256sum -- "$file") || return
  [[ ${actual%% *} == "${expected,,}" ]] || { log_error "Checksum mismatch: $file"; return 1; }
}

download_verified() {
  local url=$1 destination=$2 expected=$3 partial
  [[ $url == https://* ]] || { log_error "Downloads must use HTTPS: $url"; return 1; }
  mkdir -p -- "$(dirname "$destination")" || return
  partial=$(mktemp "${destination}.download.XXXXXX") || return
  if ! curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fSL --retry 3 --connect-timeout 15 --max-time 600 "$url" -o "$partial"; then
    rm -f -- "$partial"
    return 1
  fi
  if ! verify_sha256 "$partial" "$expected"; then
    rm -f -- "$partial"
    return 1
  fi
  mv -T -- "$partial" "$destination"
}

install_pacman_prereqs() {
  log_info "Installing pacman bootstrap prerequisites..."
  sudo -v
  sudo pacman -Syu --needed --noconfirm ca-certificates curl git gnupg tar xz which python python-yaml age
}

install_uv() {
  local local_bin="$HOME/.local/bin"
  local uv_bin="" current_uv="" staged_uv staged_uvx
  if command -v uv >/dev/null; then
    uv_bin=$(command -v uv)
    current_uv=$(uv --version 2>/dev/null | head -n1 || true)
  fi
  if [[ -n $uv_bin && $current_uv =~ (^|[^0-9.])${UV_VERSION//./\\.}([^0-9.]|$) ]]; then
    log_info "uv $UV_VERSION is already installed: $uv_bin"
  elif [[ -n $uv_bin && $uv_bin != "$local_bin/uv" ]]; then
    log_info "uv is already installed outside $local_bin: $uv_bin"
  else
    log_info "Installing verified uv $UV_VERSION..."
    download_verified "$UV_URL" "$WORK_DIR/uv.tar.gz" "$UV_SHA256" || return
    python3 "$SCRIPT_DIR/scripts/st_assets.py" tar "$WORK_DIR/uv.tar.gz" "$WORK_DIR/uv" || return
    install -d -m 0755 "$local_bin" || return
    staged_uv=$(mktemp "$local_bin/.uv.XXXXXX") || return
    staged_uvx=$(mktemp "$local_bin/.uvx.XXXXXX") || { rm -f -- "$staged_uv"; return 1; }
    if ! install -m 0755 "$WORK_DIR/uv/uv-x86_64-unknown-linux-gnu/uv" "$staged_uv" \
      || ! install -m 0755 "$WORK_DIR/uv/uv-x86_64-unknown-linux-gnu/uvx" "$staged_uvx" \
      || ! mv -Tf -- "$staged_uv" "$local_bin/uv" \
      || ! mv -Tf -- "$staged_uvx" "$local_bin/uvx"; then
      rm -f -- "$staged_uv" "$staged_uvx"
      return 1
    fi
  fi
  hash -r
}

install_mise() {
  local local_bin="$HOME/.local/bin"
  local mise_bin="" current_mise="" expected_mise="${MISE_VERSION#v}" staged
  if command -v mise >/dev/null; then
    mise_bin=$(command -v mise)
    current_mise=$(mise --version 2>/dev/null | head -n1 || true)
  fi
  if [[ -n $mise_bin && $current_mise =~ (^|[^0-9.])${expected_mise//./\\.}([^0-9.]|$) ]]; then
    log_info "mise $MISE_VERSION is already installed: $mise_bin"
  elif [[ -n $mise_bin && $mise_bin != "$local_bin/mise" ]]; then
    log_info "mise is already installed outside $local_bin: $mise_bin"
  else
    log_info "Installing verified mise $MISE_VERSION..."
    download_verified "$MISE_URL" "$WORK_DIR/mise" "$MISE_SHA256" || return
    install -d -m 0755 "$local_bin" || return
    staged=$(mktemp "$local_bin/.mise.XXXXXX") || return
    if ! install -m 0755 "$WORK_DIR/mise" "$staged" || ! mv -Tf -- "$staged" "$local_bin/mise"; then
      rm -f -- "$staged"
      return 1
    fi
  fi
  hash -r
}

install_ansible() {
  local current_core="" current_lint=""
  if command -v ansible-playbook >/dev/null; then
    current_core=$(ansible-playbook --version 2>/dev/null | head -n1 || true)
  fi
  if [[ $current_core == *"[core $ANSIBLE_CORE_VERSION]"* ]]; then
    log_info "ansible-core $ANSIBLE_CORE_VERSION is already installed: $(command -v ansible-playbook)"
  else
    log_info "Installing pinned ansible-core $ANSIBLE_CORE_VERSION via uv..."
    uv tool install --force --with-executables-from ansible-core "ansible-core==${ANSIBLE_CORE_VERSION}"
    hash -r
  fi
  if command -v ansible-lint >/dev/null; then
    current_lint=$(ansible-lint --version 2>/dev/null | head -n1 || true)
  fi
  if [[ $current_lint =~ (^|[^0-9.])${ANSIBLE_LINT_VERSION//./\\.}([^0-9.]|$) ]]; then
    log_info "ansible-lint $ANSIBLE_LINT_VERSION is already installed: $(command -v ansible-lint)"
  else
    log_info "Installing pinned ansible-lint $ANSIBLE_LINT_VERSION via uv..."
    uv tool install --force "ansible-lint==${ANSIBLE_LINT_VERSION}"
    hash -r
  fi
  log_info "Installing Ansible Galaxy collections..."
  ansible-galaxy collection install --upgrade -r "$SCRIPT_DIR/ansible/requirements.yml"
}

trust_mise_config() {
  mise trust "$SCRIPT_DIR/mise.toml"
}

main() {
  case ${1:-} in
    -h|--help) usage; return 0 ;;
    '') [[ $# -eq 0 ]] || { usage >&2; die "Unexpected empty argument."; } ;;
    *) usage >&2; die "Unknown argument: $1" ;;
  esac

  require_user "$EUID"
  require_supported_system /etc/os-release "$(uname -m)"

  WORK_DIR=$(mktemp -d -t eos-bootstrap.XXXXXX) || die "Failed to create workspace."
  chmod 0700 "$WORK_DIR"
  trap 'rm -rf -- "$WORK_DIR"' EXIT

  mkdir -p "$HOME/.local/bin"
  export PATH="$HOME/.local/bin:$PATH"

  install_pacman_prereqs
  install_uv
  install_mise
  install_ansible
  trust_mise_config

  log_info "Bootstrap complete."
  log_info "Next steps:"
  log_info "  mise run plan   # Dry-run Ansible playbook (--check --diff)"
  log_info "  mise run apply  # Converge workstation configuration"
  log_info "  mise run auth   # Interactive GitHub & Google Drive sign-in"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi

#!/usr/bin/env bash
# Run inside an archlinux:latest CI container to verify playbook convergence and idempotency.
set -euo pipefail

if [[ ! -f /.dockerenv && ! -f /run/.containerenv ]]; then
  echo "Refusing to run outside a disposable container (neither /.dockerenv nor /run/.containerenv found)." >&2
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "This script must start as root inside the container so it can create the unprivileged tester user." >&2
  exit 1
fi

REPO_SRC=${1:-/workspace}

pacman -Syu --noconfirm sudo

if ! id -u tester >/dev/null 2>&1; then
  useradd -m -s /bin/bash tester
fi
cat > /etc/sudoers.d/tester <<'EOF'
tester ALL=(ALL) NOPASSWD:ALL
EOF
chmod 0440 /etc/sudoers.d/tester
usermod --add-subuids 100000-165535 --add-subgids 100000-165535 tester

rm -rf /home/tester/repo
cp -a "$REPO_SRC" /home/tester/repo
rm -f /home/tester/repo/ansible/group_vars/all/local.yml
chown -R tester:tester /home/tester/repo

su - tester -s /bin/bash <<'EOF'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd /home/tester/repo

echo "=== Bootstrap (Pacman prereqs, uv, mise, pinned ansible-core/ansible-lint) ==="
bash ./bootstrap.sh
mise run lint

export ANSIBLE_CONFIG="$PWD/ansible/ansible.cfg"

echo "=== Ansible Pass 0 (fresh --check --diff) ==="
ansible-playbook --check --diff -i ansible/inventory/hosts.yml ansible/playbook.yml \
  --skip-tags snapper,systemd_live,desktop_gui \
  -e enable_aur=false

echo "=== Ansible Pass 1 (initial convergence) ==="
ansible-playbook -i ansible/inventory/hosts.yml ansible/playbook.yml \
  --skip-tags snapper,systemd_live,desktop_gui \
  -e enable_aur=false

echo "=== Ansible Pass 2 (post-convergence --check --diff idempotency) ==="
check_log=$(mktemp)
pass3_log=$(mktemp)
trap 'rm -f -- "$check_log" "$pass3_log"' EXIT
ansible-playbook --check --diff -i ansible/inventory/hosts.yml ansible/playbook.yml \
  --skip-tags snapper,systemd_live,desktop_gui \
  -e enable_aur=false | tee "$check_log"

grep -Eq 'changed=0[[:space:]].*failed=0' "$check_log" || {
  echo "Expected changed=0 and failed=0 on post-convergence --check pass." >&2
  exit 1
}

echo "=== Ansible Pass 3 (apply idempotency check) ==="
ansible-playbook --diff -i ansible/inventory/hosts.yml ansible/playbook.yml \
  --skip-tags snapper,systemd_live,desktop_gui \
  -e enable_aur=false | tee "$pass3_log"

grep -Eq 'changed=0[[:space:]].*failed=0' "$pass3_log" || {
  echo "Expected changed=0 and failed=0 on second apply pass." >&2
  exit 1
}
EOF

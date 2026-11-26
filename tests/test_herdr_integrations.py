"""Run ansible/tasks/herdr_integrations.yml against a stub herdr and stub agent CLIs."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

REPO = Path(__file__).resolve().parents[1]
ANSIBLE_PLAYBOOK = shutil.which('ansible-playbook')

HERDR_STUB = '''#!/usr/bin/env bash
# Status lines follow `herdr integration status` (0.9.5): "<target>[ (experimental)]: <state> (<path>)".
state=$HERDR_STATE
case "$1 $2" in
  "integration status")
    for target in claude codex copilot; do
      if [[ -f $state/$target ]]; then
        printf '%s: current (v1) (/stub/%s)\\n' "$target" "$target"
      else
        printf '%s: not installed (/stub/%s)\\n' "$target" "$target"
      fi
    done
    printf 'letta (experimental): not installed (/stub/letta)\\n'
    ;;
  "integration install")
    printf '%s\\n' "$3" >> "$state/install.log"
    [[ $3 != "${HERDR_FAIL_TARGET:-}" ]] || { printf 'boom\\n' >&2; exit 3; }
    touch "$state/$3"
    ;;
  *) exit 64 ;;
esac
'''


@unittest.skipUnless(ANSIBLE_PLAYBOOK, 'ansible-playbook is not installed')
class HerdrIntegrationTasksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='eos-herdr-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.herdr = self.bin / 'herdr'
        self.herdr.write_text(HERDR_STUB)
        self.herdr.chmod(0o755)
        # CLI names are test-only so real agent CLIs on the host PATH cannot leak in.
        for cli in ('eostest-claude', 'eostest-codex'):  # eostest-copilot is deliberately absent
            path = self.bin / cli
            path.write_text('#!/bin/sh\n')
            path.chmod(0o755)
        (self.state / 'codex').touch()  # codex integration already current

    def run_tasks(self, *extra, env=None):
        integrations = [{'cli': f'eostest-{target}', 'target': target} for target in ('claude', 'codex', 'copilot')]
        playbook = self.root / 'test.yml'
        playbook.write_text(textwrap.dedent(f'''\
            - hosts: localhost
              gather_facts: false
              vars:
                herdr_bin: {json.dumps(str(self.herdr))}
                workstation_local_bin: {json.dumps(str(self.bin))}
                workstation_home: {json.dumps(str(self.root))}
                herdr_agent_integrations: {json.dumps(integrations)}
              tasks:
                - name: Sync Herdr integrations
                  ansible.builtin.include_tasks: {json.dumps(str(REPO / 'ansible/tasks/herdr_integrations.yml'))}
            '''))
        environment = dict(os.environ, HERDR_STATE=str(self.state), ANSIBLE_LOCALHOST_WARNING='false',
                           ANSIBLE_INVENTORY_UNPARSED_WARNING='false', ANSIBLE_NOCOLOR='1')
        environment.pop('ANSIBLE_CONFIG', None)
        if env:
            environment.update(env)
        result = subprocess.run([ANSIBLE_PLAYBOOK, '-i', 'localhost,', '-c', 'local', str(playbook), *extra],
                                cwd=self.root, env=environment, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def installs(self):
        log = self.state / 'install.log'
        return log.read_text().splitlines() if log.exists() else []

    def test_installs_only_missing_integrations_and_rerun_is_unchanged(self):
        self.assertIn('changed=1', self.run_tasks())
        self.assertEqual(self.installs(), ['claude'])
        self.assertIn('changed=0', self.run_tasks())
        self.assertEqual(self.installs(), ['claude'])

    def test_check_mode_reports_missing_integration_without_installing(self):
        out = self.run_tasks('--check')
        self.assertIn('Would install Herdr integration claude', out)
        self.assertIn('changed=1', out)
        self.assertEqual(self.installs(), [])

    def test_failed_install_warns_and_is_not_reported_as_changed(self):
        out = self.run_tasks(env={'HERDR_FAIL_TARGET': 'claude'})
        self.assertIn('WARNING: herdr integration install claude failed (rc=3): boom', out)
        self.assertIn('changed=0', out)
        self.assertEqual(self.installs(), ['claude'])


if __name__ == '__main__':
    unittest.main()

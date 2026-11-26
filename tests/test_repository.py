"""Repository regressions: filesystem fixtures and stubs, never real installers."""
import gzip
import hashlib
import importlib
import io
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import warnings
import xml.etree.ElementTree as ET
import zipfile
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
assets = importlib.import_module('st_assets')
snapper_config = importlib.import_module('configure_snapper')


def build_ar_deb(path, members):
    """Write a minimal Debian ar archive from `(name, bytes)` pairs."""
    buf = bytearray(b'!<arch>\n')
    for name, payload in members:
        header = (
            f'{name:<16}'
            f'{"0":<12}'
            f'{"0":<6}'
            f'{"0":<6}'
            f'{"100644":<8}'
            f'{len(payload):<10}'
            '`\n'
        ).encode('ascii')
        assert len(header) == 60
        buf.extend(header)
        buf.extend(payload)
        if len(payload) % 2:
            buf.append(ord('\n'))
    Path(path).write_bytes(bytes(buf))


def build_tar_bytes(entries, mode='w:gz'):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as archive:
        for item in entries:
            name = item[0]
            content = item[1]
            file_mode = item[2] if len(item) > 2 else 0o644
            symlink_target = item[3] if len(item) > 3 else None
            info = tarfile.TarInfo(name)
            if symlink_target is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = symlink_target
                archive.addfile(info)
            else:
                data = content.encode('utf-8') if isinstance(content, str) else content
                info.size = len(data)
                info.mode = file_mode
                archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='eos-repo-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def bash(self, code, *arguments, script='common.sh', env=None):
        environment = dict(os.environ, TMPDIR=str(self.root))
        if env:
            environment.update(env)
        return subprocess.run(['bash', '-euo', 'pipefail', '-c',
                               f'source {shlex.quote(str(REPO / script))}\n{code}',
                               'test', *map(str, arguments)], cwd=self.root,
                              env=environment, capture_output=True, text=True)

    def test_mktemp_failure_is_not_success(self):
        result = self.bash('mktemp() { return 42; }; create_secure_tmpdir test')
        self.assertEqual(result.returncode, 42)
        self.assertEqual(result.stdout, '')

    def test_chmod_failure_removes_empty_temporary_directory(self):
        result = self.bash('chmod() { return 42; }; create_secure_tmpdir test')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_logs_do_not_interpret_backslash_sequences(self):
        result = self.bash("log_error 'path\\nname'")
        self.assertEqual(result.stderr, '[ERROR] path\\nname\n')
        self.assertEqual(result.stdout, '')

    def test_missing_explicit_installer_never_falls_back(self):
        (self.root / 'good.zip').write_text('archive')
        result = self.bash('find_archive missing.zip "*.zip"')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_archive_search_rejects_ambiguity(self):
        for name in ('one.zip', 'two.zip'):
            (self.root / name).write_text('archive')
        result = self.bash('find_archive "" "*.zip"', env={'ST_DOWNLOAD_DIR': str(self.root)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Multiple matching', result.stderr)

    def test_archive_patterns_overlap_without_false_ambiguity(self):
        path = self.root / 'one.sh.zip'
        path.write_text('archive')
        result = self.bash('find_archive "" "*.sh.zip" "*.zip"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(path))

    def test_explicit_bare_filename_is_canonicalized(self):
        path = self.root / 'installer.run'
        path.write_text('archive')
        result = self.bash('find_archive installer.run "*.run"')
        self.assertEqual(result.stdout.strip(), str(path))

    def test_st_preflight_skips_sudo_check_when_st_skip_udev_is_set(self):
        path = self.root / 'bin'
        path.mkdir()
        for cmd in ('python3', 'sha256sum', 'realpath', 'find', 'mktemp', 'ldd', 'desktop-file-validate'):
            tool = path / cmd
            tool.write_text('#!/bin/sh\nexit 0\n')
            tool.chmod(0o755)
        skipped = self.bash(
            'require_supported_system() { return 0; }\nPATH="$1"\nhash -r\nst_preflight',
            path,
            env={'ST_SKIP_UDEV': '1'},
        )
        self.assertEqual(skipped.returncode, 0, skipped.stderr)

        required = self.bash(
            'require_supported_system() { return 0; }\nPATH="$1"\nhash -r\nst_preflight',
            path,
        )
        self.assertNotEqual(required.returncode, 0)
        self.assertIn('sudo', required.stderr)

    def test_st_finish_skips_stlink_udev_when_st_skip_udev_is_set(self):
        exe = self.root / 'app'
        exe.write_text('#!/bin/sh\nexit 0\n')
        exe.chmod(0o755)
        result = self.bash('''
SCRIPT_DIR="$3"
install_stlink_rules() { return 99; }
WORK_DIR="$1/work"
INSTALL_DIR="$1/inst"
INSTALL_TARGET="$1/target"
mkdir -p "$WORK_DIR" "$INSTALL_DIR"
HOME="$1/home"
mkdir -p "$HOME"
st_finish TestApp "$HOME/.local/bin/testapp" "$2" "$INSTALL_DIR" icon wmclass "$HOME/.local/share/applications/testapp.desktop"
''', self.root, exe, REPO, env={'ST_SKIP_UDEV': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'home/.local/bin/testapp').is_file())
        self.assertTrue((self.root / 'home/.local/share/applications/testapp.desktop').is_file())

    def test_publish_preserves_old_installation_and_hidden_files(self):
        target = self.root / 'app'
        target.mkdir()
        (target / 'keep').write_text('old')
        result = self.bash('begin_install "$1"; printf new > "$INSTALL_DIR/.hidden"; publish_install', target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target.is_symlink())
        self.assertEqual((target / '.hidden').read_text(), 'new')
        backups = list(self.root.glob('app.backup.*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'keep').read_text(), 'old')

    def test_publish_failure_restores_old_installation(self):
        target = self.root / 'app'
        target.mkdir()
        (target / 'keep').write_text('old')
        result = self.bash('''
begin_install "$1"
mv() { if [[ $1 == -Tf ]]; then return 42; fi; command mv "$@"; }
publish_install
''', target)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.is_symlink())
        self.assertEqual((target / 'keep').read_text(), 'old')

    def test_zip_rejects_traversal_and_external_symlinks(self):
        for name, symlink in (('../escape', False), ('link', True)):
            path = self.root / 'unsafe.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                info = zipfile.ZipInfo(name)
                if symlink:
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, '../escape' if symlink else 'bad')
            with self.subTest(name=name), self.assertRaises(ValueError):
                assets.extract_zip(path, self.root / 'extracted')
            self.assertFalse((self.root.parent / 'escape').exists())

    def test_tar_rejects_external_links(self):
        path = self.root / 'unsafe.tar'
        with tarfile.open(path, 'w') as archive:
            info = tarfile.TarInfo('link')
            info.type = tarfile.SYMTYPE
            info.linkname = '/etc'
            archive.addfile(info)
        with tarfile.open(path) as archive, self.assertRaises(ValueError):
            assets.extract_tar(archive, self.root / 'extracted')

    def test_deb_field_and_extract_work_without_dpkg_deb(self):
        deb = self.root / 'pkg.deb'
        control = build_tar_bytes([('./control', 'Package: stm32cubeide-test\nVersion: 2.2.0\nDescription: IDE\n next line\n')])
        data = build_tar_bytes([
            ('./opt/st/stm32cubeide', '#!/bin/sh\nexit 0\n', 0o755),
            ('./opt/st/libfoo.so.1', b'elf', 0o755),
            ('./opt/st/libfoo.so', b'', 0o777, 'libfoo.so.1'),
        ], mode='w:xz')
        build_ar_deb(deb, [('debian-binary', b'2.0\n'), ('control.tar.gz', control), ('data.tar.xz', data)])

        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            assets.deb_field(str(deb), 'package')
        self.assertEqual(out.getvalue().strip(), 'stm32cubeide-test')

        dest = self.root / 'deb-out'
        assets.deb_extract(str(deb), str(dest))
        self.assertTrue(os.access(dest / 'opt/st/stm32cubeide', os.X_OK))
        self.assertTrue((dest / 'opt/st/libfoo.so').is_symlink())

    def test_deb_extract_rejects_unsafe_paths_and_corrupt_ar(self):
        bad_ar = self.root / 'bad.deb'
        bad_ar.write_bytes(b'not an ar')
        with self.assertRaises(ValueError):
            assets.deb_field(str(bad_ar), 'Package')

        unsafe_deb = self.root / 'unsafe.deb'
        control = build_tar_bytes([('control', 'Package: bad\n')])
        data = build_tar_bytes([('escape', b'', 0o777, '/etc/passwd')])
        build_ar_deb(unsafe_deb, [('debian-binary', b'2.0\n'), ('control.tar.gz', control), ('data.tar.gz', data)])
        with self.assertRaises(ValueError):
            assets.deb_extract(str(unsafe_deb), str(self.root / 'unsafe-out'))

        # Negative size and out-of-range #1/ name length must fail cleanly.
        neg_deb = self.root / 'neg.deb'
        neg_deb.write_bytes(
            b'!<arch>\n'
            + b'control.tar.gz  0           0     0     100644  -5        `\n'
        )
        with self.assertRaises(ValueError):
            assets.deb_field(str(neg_deb), 'Package')

        ext_deb = self.root / 'ext-bad.deb'
        build_ar_deb(ext_deb, [('#1/999', b'short')])
        with self.assertRaises(ValueError):
            assets.deb_field(str(ext_deb), 'Package')

    def test_deb_supports_zstd_payloads_when_zstd_available(self):
        if not shutil.which('zstd'):
            self.skipTest('zstd not installed')
        deb = self.root / 'zst.deb'
        control_tar = build_tar_bytes([('./control', 'Package: zst-pkg\nVersion: 1.0\n')], mode='w:')
        data_tar = build_tar_bytes([('./usr/bin/hello', '#!/bin/sh\necho hi\n', 0o755)], mode='w:')
        control_zst = subprocess.run(['zstd', '-c'], input=control_tar, capture_output=True, check=True).stdout
        data_zst = subprocess.run(['zstd', '-c'], input=data_tar, capture_output=True, check=True).stdout
        build_ar_deb(deb, [('debian-binary', b'2.0\n'), ('control.tar.zst', control_zst), ('data.tar.zst', data_zst)])

        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            assets.deb_field(str(deb), 'Package')
        self.assertEqual(out.getvalue().strip(), 'zst-pkg')

        dest = self.root / 'zst-out'
        assets.deb_extract(str(deb), str(dest))
        self.assertTrue(os.access(dest / 'usr/bin/hello', os.X_OK))

    def test_tar_final_pass_removes_deferred_escaping_symlink(self):
        tar_path = self.root / 'deferred-escape.tar'
        with tarfile.open(tar_path, 'w') as archive:
            escape_info = tarfile.TarInfo('a/b/link')
            escape_info.type = tarfile.SYMTYPE
            escape_info.linkname = 'd/../../..'
            archive.addfile(escape_info)

            pivot_info = tarfile.TarInfo('a/b/d')
            pivot_info.type = tarfile.SYMTYPE
            pivot_info.linkname = '.'
            archive.addfile(pivot_info)

        # On Python 3.12+, filter='data' normalizes linkname to '../..'; with fallback path
        # (Python < 3.12) or any deferred pivot on disk, final sweep removes escaping symlinks.
        extracted_fallback = self.root / 'extracted-tar-deferred-fallback'
        with mock.patch.object(assets.sys, 'version_info', (3, 11, 0)):
            with tarfile.open(tar_path) as archive:
                if hasattr(tarfile, 'fully_trusted_filter'):
                    archive.extraction_filter = tarfile.fully_trusted_filter
                with self.assertRaisesRegex(ValueError, 'Unsafe archive symlink'):
                    assets.extract_tar(archive, extracted_fallback)
        self.assertFalse((extracted_fallback / 'a/b/link').is_symlink())
        self.assertFalse((extracted_fallback / 'a/b/link').exists())

        extracted = self.root / 'extracted-tar-deferred'
        with tarfile.open(tar_path) as archive:
            assets.extract_tar(archive, extracted)
        self.assertTrue((extracted / 'a/b/link').resolve().is_relative_to(extracted.resolve()))

    def test_zip_rejects_chained_symlink_escape_and_absolute_symlink(self):
        chained = self.root / 'chained.zip'
        with zipfile.ZipFile(chained, 'w') as archive:
            m1 = zipfile.ZipInfo('sub/dir')
            m1.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(m1, '..')
            m2 = zipfile.ZipInfo('sub/dir/escape')
            m2.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(m2, '..')
        with self.assertRaises(ValueError):
            assets.extract_zip(chained, self.root / 'extracted-chained')
        self.assertFalse((self.root / 'extracted-chained/escape').exists())

        abs_link = self.root / 'abs.zip'
        with zipfile.ZipFile(abs_link, 'w') as archive:
            m = zipfile.ZipInfo('abs')
            m.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(m, '/etc/passwd')
        with self.assertRaises(ValueError):
            assets.extract_zip(abs_link, self.root / 'extracted-abs')

    def test_zip_duplicate_symlink_member_does_not_write_outside_root(self):
        dup = self.root / 'dup.zip'
        outside = self.root / 'outside.txt'
        outside.write_text('untouched')
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(dup, 'w') as archive:
                m1 = zipfile.ZipInfo('item')
                m1.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(m1, 'safe-target')
                m2 = zipfile.ZipInfo('item')
                m2.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(m2, 'replacement')
        extracted = self.root / 'extracted-dup'
        assets.extract_zip(dup, extracted)
        self.assertFalse((extracted / 'item').is_symlink())
        self.assertEqual((extracted / 'item').read_text(), 'replacement')
        self.assertEqual(outside.read_text(), 'untouched')

    def test_zip_final_pass_removes_deferred_escaping_symlink(self):
        crafted = self.root / 'deferred-escape.zip'
        with zipfile.ZipFile(crafted, 'w') as archive:
            escape_link = zipfile.ZipInfo('a/b/link')
            escape_link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(escape_link, 'd/../../..')
            pivot_link = zipfile.ZipInfo('a/b/d')
            pivot_link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(pivot_link, '.')
        extracted = self.root / 'extracted-deferred'
        with self.assertRaisesRegex(ValueError, 'Unsafe archive symlink'):
            assets.extract_zip(crafted, extracted)
        self.assertFalse((extracted / 'a/b/link').is_symlink())
        self.assertFalse((extracted / 'a/b/link').exists())

    def test_tar_extracts_valid_internal_relative_symlink(self):
        tar_path = self.root / 'valid.tar'
        with tarfile.open(tar_path, 'w') as archive:
            file_info = tarfile.TarInfo('lib/libfoo.so.1')
            payload = b'elf'
            file_info.size = len(payload)
            file_info.mode = 0o755
            archive.addfile(file_info, io.BytesIO(payload))
            link_info = tarfile.TarInfo('lib/libfoo.so')
            link_info.type = tarfile.SYMTYPE
            link_info.linkname = 'libfoo.so.1'
            archive.addfile(link_info)
        extracted = self.root / 'extracted-tar'
        with tarfile.open(tar_path) as archive:
            assets.extract_tar(archive, extracted)
        self.assertTrue((extracted / 'lib/libfoo.so').is_symlink())
        self.assertEqual((extracted / 'lib/libfoo.so').read_bytes(), b'elf')

    def test_zip_restores_executable_permissions(self):
        path = self.root / 'installer.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            info = zipfile.ZipInfo('jre/bin/java')
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            archive.writestr(info, 'stub')
        assets.extract_zip(path, self.root / 'extracted')
        self.assertTrue(os.access(self.root / 'extracted/jre/bin/java', os.X_OK))

    def jar_fixture(self, path, template):
        tree = ET.parse(template)
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('resources/panelsOrder', ' '.join(x.attrib['id'] for x in tree.getroot()))
            for tag in dict.fromkeys(element.tag for element in tree.getroot()):
                archive.writestr(tag.replace('.', '/') + '.class', '')

    def test_response_xml_escapes_installation_path_and_checks_panels(self):
        template = REPO / 'resources/izpack/cubemx.xml'
        jar = self.root / 'installer.jar'
        self.jar_fixture(jar, template)
        response = self.root / 'response.xml'
        target = str(self.root / 'quotes & spaces <target>')
        assets.response(template, target, response, jar)
        self.assertEqual(ET.parse(response).find('.//installpath').text, target)
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('resources/panelsOrder', '')
        with self.assertRaises(ValueError):
            assets.response(template, target, response, jar)

    def test_wrapper_preserves_arguments_with_shell_metacharacters(self):
        directory = self.root / 'space & quotes " $'
        directory.mkdir()
        executable = directory / 'application'
        executable.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
        executable.chmod(0o755)
        wrapper = self.root / 'wrapper'
        assets.wrapper(str(wrapper), str(directory), str(executable), 'GDK_BACKEND=x11')
        result = subprocess.run([wrapper, 'two words', '$(should-not-run)', ';'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['two words', '$(should-not-run)', ';'])
        desktop = self.root / 'application.desktop'
        assets.desktop(str(desktop), 'Application', str(executable), str(directory), 'applications-development', 'application')
        result = subprocess.run(['desktop-file-validate', desktop], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Exec="', desktop.read_text())

    def test_cube_install_failure_preserves_existing_installation(self):
        for script, template_name, payload_name in (
            ('install_stm32_cube_mx.sh', 'cubemx', 'SetupSTM32CubeMX-6.18_1'),
            ('install_stm32_cube_prog.sh', 'cubeprogrammer', 'SetupSTM32CubeProgrammer-2.23.0.exe'),
        ):
            with self.subTest(script=script):
                fixture = self.root / template_name
                fixture.mkdir()
                jar = fixture / payload_name
                self.jar_fixture(jar, REPO / f'resources/izpack/{template_name}.xml')
                archive = fixture / 'archive.zip'
                with zipfile.ZipFile(archive, 'w') as zip_file:
                    zip_file.write(jar, payload_name)
                    java = zipfile.ZipInfo('jre/bin/java')
                    java.external_attr = (stat.S_IFREG | 0o755) << 16
                    zip_file.writestr(java, '#!/usr/bin/env bash\nexit 42\n')
                target = fixture / 'installed'
                target.mkdir()
                (target / 'keep').write_text('working')
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                code = '''
st_preflight() { require_commands python3 sha256sum realpath find mktemp; }
eval "$(declare -f begin_install | sed '1s/begin_install/original_begin_install/')"
begin_install() { original_begin_install "$TEST_TARGET"; }
main "$1"
'''
                result = self.bash(code, archive, script=script,
                                   env={'TEST_TARGET': str(target), 'ST_INSTALLER_SHA256': digest})
                self.assertEqual(result.returncode, 42, result.stderr)
                self.assertEqual((target / 'keep').read_text(), 'working')
                self.assertEqual(list((fixture / '.installed.versions').iterdir()), [])

    def test_cube_install_copies_adjacent_jre_and_rejects_missing_runtime(self):
        for script, template, payload, runtime in (
            ('install_stm32_cube_mx.sh', 'cubemx', 'SetupSTM32CubeMX-test', 'jre'),
            ('install_stm32_cube_prog.sh', 'cubeprogrammer',
             'SetupSTM32CubeProgrammer-test.exe', 'bin/jre'),
        ):
            for copy_runtime in ('yes', 'no'):
                with self.subTest(script=script, copy_runtime=copy_runtime):
                    fixture = self.root / f'{template}-{copy_runtime}'
                    fixture.mkdir()
                    jar = fixture / payload
                    self.jar_fixture(jar, REPO / f'resources/izpack/{template}.xml')
                    archive = fixture / 'archive.zip'
                    java = '''#!/usr/bin/env bash
set -euo pipefail
[[ $1 == -Djava.awt.headless=true && $2 == -jar ]] || exit 41
[[ $PWD == "${3%/*}" && -x jre/bin/java ]] || exit 43
python3 - "$4" <<'PY'
import os
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET
target = Path(ET.parse(sys.argv[1]).find('.//installpath').text)
for name in ('STM32CubeMX', 'bin/STM32_Programmer_CLI', 'bin/STM32CubeProgrammerLauncher'):
    executable = target / name
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text('#!/bin/sh\\nexit 0\\n')
    executable.chmod(0o755)
if os.environ['TEST_COPY_RUNTIME'] == 'yes':
    shutil.copytree('jre', target / os.environ['TEST_RUNTIME'])
PY
'''
                    with zipfile.ZipFile(archive, 'w') as container:
                        container.write(jar, payload)
                        info = zipfile.ZipInfo('jre/bin/java')
                        info.external_attr = (stat.S_IFREG | 0o755) << 16
                        container.writestr(info, java)
                    target = fixture / 'installed'
                    target.mkdir()
                    (target / 'keep').write_text('working')
                    result = self.bash('''
st_preflight() { require_commands python3 sha256sum realpath find mktemp; }
eval "$(declare -f begin_install | sed '1s/begin_install/original_begin_install/')"
begin_install() { original_begin_install "$TEST_TARGET"; }
check_elf_dependencies() { return 0; }
st_finish() { check_executable "$3"; publish_install; }
main "$1"
''', archive, script=script, env={
                        'HOME': str(fixture / 'home'),
                        'TEST_TARGET': str(target), 'TEST_RUNTIME': runtime,
                        'TEST_COPY_RUNTIME': copy_runtime,
                        'ST_INSTALLER_SHA256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                    })
                    if copy_runtime == 'yes':
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertTrue(target.is_symlink())
                        self.assertTrue(os.access(target / runtime / 'bin/java', os.X_OK))
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('Required executable missing', result.stderr)
                        self.assertEqual((target / 'keep').read_text(), 'working')
                        self.assertFalse(target.is_symlink())
                        self.assertEqual(list((fixture / '.installed.versions').iterdir()), [])

    def test_cubeide_selects_main_package_via_python_ar_and_preserves_old_on_failure(self):
        # Companion package appears first in payload; st_assets.py deb-field must
        # choose the IDE package and st_assets.py deb-extract must unpack both.
        driver_deb = self.root / 'driver.deb'
        main_deb = self.root / 'main.deb'
        server_deb = self.root / 'server.deb'
        build_ar_deb(driver_deb, [
            ('debian-binary', b'2.0\n'),
            ('control.tar.gz', build_tar_bytes([('control', 'Package: st-stlink-udev-rules\n')])),
            ('data.tar.gz', build_tar_bytes([('etc/udev/rules.d/49.rules', 'rule\n')])),
        ])
        build_ar_deb(main_deb, [
            ('debian-binary', b'2.0\n'),
            ('control.tar.gz', build_tar_bytes([('control', 'Package: stm32cubeide-test\n')])),
            ('data.tar.gz', build_tar_bytes([
                ('opt/st/stm32cubeide/stm32cubeide', '#!/bin/sh\nexit 0\n', 0o755),
            ])),
        ])
        build_ar_deb(server_deb, [
            ('debian-binary', b'2.0\n'),
            ('control.tar.gz', build_tar_bytes([('control', 'Package: st-stlink-server\n')])),
            ('data.tar.gz', build_tar_bytes([
                ('usr/bin/stlink-server', '#!/bin/sh\nexit 0\n', 0o755),
            ])),
        ])

        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w') as tar:
            for path in (driver_deb, main_deb, server_deb):
                tar.add(path, arcname=path.name)
        installer = b'#!/bin/sh\n# fixture\noffset=`head -n 4 "$0"`\n# end header\n' + payload.getvalue()
        archive = self.root / 'ide.zip'
        with zipfile.ZipFile(archive, 'w') as zip_file:
            zip_file.writestr('stm32cubeide_test.sh', installer)
        home = self.root / 'home'
        target = home / 'STMicroelectronics/STM32Cube/stm32cubeide'
        target.mkdir(parents=True)
        (target / 'keep').write_text('working')
        result = self.bash('''
st_preflight() { require_commands python3 sha256sum realpath find mktemp; }
check_elf_dependencies() { return 44; }
main "$1"
''', archive, script='install_stm32_cube_ide.sh',
                           env={'HOME': str(home), 'ST_INSTALLER_SHA256': hashlib.sha256(archive.read_bytes()).hexdigest()})
        self.assertEqual(result.returncode, 44, result.stderr)
        self.assertEqual((target / 'keep').read_text(), 'working')

    def test_sw_installer_failure_preserves_existing_installation(self):
        jar = self.root / 'installer.jar'
        self.jar_fixture(jar, REPO / 'resources/izpack/sw4stm32.xml')
        jre = io.BytesIO()
        java = b'#!/usr/bin/env bash\nexit 42\n'
        with tarfile.open(fileobj=jre, mode='w') as tar:
            info = tarfile.TarInfo('jre/bin/java')
            info.size = len(java)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(java))
        installer = self.root / 'sw.run'
        installer.write_bytes(b'#!/bin/sh\nJRE_MARKER:\n' + gzip.compress(jre.getvalue()) +
                              b'\nJAR_MARKER:\n' + jar.read_bytes())
        target = self.root / 'installed'
        target.mkdir()
        (target / 'keep').write_text('working')
        result = self.bash('''
st_preflight() { require_commands python3 sha256sum realpath find mktemp; }
eval "$(declare -f begin_install | sed '1s/begin_install/original_begin_install/')"
begin_install() { original_begin_install "$TEST_TARGET"; }
main "$1"
''', installer, script='install_sw4stm32.sh',
                           env={'TEST_TARGET': str(target), 'ST_INSTALLER_SHA256': hashlib.sha256(installer.read_bytes()).hexdigest()})
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual((target / 'keep').read_text(), 'working')
        self.assertEqual(list((self.root / '.installed.versions').iterdir()), [])

    def test_missing_cube_icon_does_not_fail_successful_install(self):
        jar = self.root / 'SetupSTM32CubeMX-test'
        self.jar_fixture(jar, REPO / 'resources/izpack/cubemx.xml')
        archive = self.root / 'mx.zip'
        fake_java = '''#!/usr/bin/env bash
python3 - "$4" <<'PY'
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET
directory = Path(ET.parse(sys.argv[1]).find('.//installpath').text)
binary = directory / 'STM32CubeMX'
binary.write_text('#!/bin/sh\\nexit 0\\n')
binary.chmod(0o755)
shutil.copytree('jre', directory / 'jre')
PY
'''
        with zipfile.ZipFile(archive, 'w') as zip_file:
            zip_file.write(jar, jar.name)
            info = zipfile.ZipInfo('jre/bin/java')
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            zip_file.writestr(info, fake_java)
        target = self.root / 'installed'
        result = self.bash('''
st_preflight() { require_commands python3 sha256sum realpath find mktemp; }
eval "$(declare -f begin_install | sed '1s/begin_install/original_begin_install/')"
begin_install() { original_begin_install "$TEST_TARGET"; }
st_finish() { check_executable "$3"; publish_install; }
main "$1"
''', archive, script='install_stm32_cube_mx.sh',
                           env={'TEST_TARGET': str(target), 'ST_INSTALLER_SHA256': hashlib.sha256(archive.read_bytes()).hexdigest()})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Native CubeMX application icon is unavailable', result.stderr)
        self.assertTrue(target.is_symlink())
        self.assertTrue((target / 'STM32CubeMX').exists())

    def test_snapper_config_preserves_other_configs_and_retention(self):
        root = self.root / 'root-config'
        root.write_text('SUBVOLUME="/"\nALLOW_GROUPS="custom"\nNUMBER_LIMIT="42"\n')
        defaults = self.root / 'snapper-defaults'
        defaults.write_text('# comment\nSNAPPER_CONFIGS="home database"\n')
        snapper_config.configure(root, defaults)
        values = snapper_config.read_assignments(root.read_text())
        self.assertEqual(values['NUMBER_LIMIT'], '42')
        self.assertIn('custom', values['ALLOW_GROUPS'].split())
        self.assertEqual(snapper_config.read_assignments(defaults.read_text())['SNAPPER_CONFIGS'], 'home database root')
        self.assertTrue(root.with_name('root-config.pre-eos-setup').exists())
        self.assertIn('# comment', defaults.read_text())

    def test_snapper_rejects_foreign_root_config(self):
        root = self.root / 'root-config'
        root.write_text('SUBVOLUME="/home"\n')
        with self.assertRaises(ValueError):
            snapper_config.configure(root, self.root / 'defaults')
        self.assertEqual(root.read_text(), 'SUBVOLUME="/home"\n')

    def test_snapper_initializes_empty_template_fields(self):
        root = self.root / 'root-config'
        root.write_text('SUBVOLUME=""\nFSTYPE=""\nNUMBER_LIMIT="50"\n')
        snapper_config.configure(root, self.root / 'defaults', initialize=True)
        values = snapper_config.read_assignments(root.read_text())
        self.assertEqual(values['SUBVOLUME'], '/')
        self.assertEqual(values['FSTYPE'], 'btrfs')
        self.assertEqual(values['NUMBER_LIMIT'], '10')
        self.assertEqual(values['TIMELINE_CREATE'], 'no')

    def test_snapper_backup_path_check_mode_and_template_initialize(self):
        configs_dir = self.root / 'etc/snapper/configs'
        configs_dir.mkdir(parents=True)
        root_cfg = configs_dir / 'root'
        defaults = self.root / 'etc/conf.d/snapper'
        defaults.parent.mkdir(parents=True)
        defaults.write_text('SNAPPER_CONFIGS=""\n')
        template = self.root / 'template-default'
        template.write_text('SUBVOLUME=""\nFSTYPE=""\nNUMBER_LIMIT="50"\n')

        # Check mode initializes from template without creating root config or defaults.
        self.assertTrue(snapper_config.configure(root_cfg, defaults, initialize=True, check_mode=True, template=template))
        self.assertFalse(root_cfg.exists())
        self.assertEqual(defaults.read_text(), 'SNAPPER_CONFIGS=""\n')

        # Real initialize-from-template creates root config atomically in one step.
        self.assertTrue(snapper_config.configure(root_cfg, defaults, initialize=True, template=template))
        self.assertTrue(root_cfg.is_file())
        self.assertEqual(snapper_config.read_assignments(root_cfg.read_text())['NUMBER_LIMIT'], '10')

        # Existing configs/root writes backup outside configs/ so snapper ignores it.
        (configs_dir / 'root').write_text(root_cfg.read_text().replace('SYNC_ACL="yes"', 'SYNC_ACL="no"'))
        self.assertTrue(snapper_config.configure(root_cfg, defaults))
        self.assertFalse((configs_dir / 'root.pre-eos-setup').exists())
        self.assertTrue((self.root / 'etc/snapper/root.pre-eos-setup').is_file())
        self.assertFalse(snapper_config.configure(root_cfg, defaults, check_mode=True))

    def test_snapper_rejects_incorrect_mounted_root(self):
        result = self.bash('findmnt() { if [[ $3 == FSTYPE ]]; then printf btrfs; else printf /@/nested; fi; }; verify_subvolumes',
                           script='preboot/setup_btrfs_snapper.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Boot with rootflags', result.stderr)

    def test_snapper_accepts_only_matching_root_and_snapshot_mounts(self):
        mock_cmd = '''
findmnt() {
  case "$3:${@: -1}" in
    FSTYPE:*) printf btrfs ;;
    FSROOT:/) printf /@ ;;
    FSROOT:/.snapshots) printf /@snapshots ;;
    UUID:/) printf root-uuid ;;
    UUID:/.snapshots) printf '%s' "$SNAPSHOT_UUID" ;;
  esac
}
verify_subvolumes
'''
        result = self.bash(mock_cmd, script='preboot/setup_btrfs_snapper.sh', env={'SNAPSHOT_UUID': 'root-uuid'})
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.bash(mock_cmd, script='preboot/setup_btrfs_snapper.sh', env={'SNAPSHOT_UUID': 'wrong-uuid'})
        self.assertNotEqual(result.returncode, 0)

    def test_failed_install_after_published_stage_cleans_only_new_staging(self):
        result = self.bash('''
start_workspace install-test
begin_install "$1/first"
printf preserved > "$INSTALL_DIR/content"
publish_install
begin_install "$1/second"
printf partial > "$INSTALL_DIR/content"
exit 42
''', self.root)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual((self.root / 'first/content').read_text(), 'preserved')
        self.assertFalse((self.root / 'second').exists())
        self.assertEqual(list((self.root / '.second.versions').iterdir()), [])

    def test_staging_directory_failure_preserves_previous_published_install(self):
        result = self.bash('''
start_workspace install-test
begin_install "$1/first"
printf preserved > "$INSTALL_DIR/content"
publish_install
mkdir() { return 43; }
begin_install "$1/second"
''', self.root)
        self.assertEqual(result.returncode, 43, result.stderr)
        self.assertEqual((self.root / 'first/content').read_text(), 'preserved')

    def test_secondary_btrfs_fstab_rendering_is_idempotent(self):
        fstab = self.root / 'fstab'
        fstab.write_text(
            '# Header comment\n'
            'UUID=root-uuid\t/\tbtrfs\tdefaults,subvol=@\t0\t0\n'
            'UUID=old-uuid\t/mnt/data\tbtrfs\tdefaults,subvol=@data\t0\t0\n'
            'UUID=old-uuid\t/mnt/data/.snapshots\tbtrfs\tdefaults,subvol=@snapshots\t0\t0\n'
        )
        result = self.bash('render_secondary_fstab "$1" new-uuid-1234', fstab,
                           script='preboot/setup_secondary_btrfs_drive.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(sum('/mnt/data\t' in line for line in lines), 1)
        self.assertEqual(sum('/mnt/data/.snapshots\t' in line for line in lines), 1)
        self.assertIn(
            'UUID=new-uuid-1234\t/mnt/data\tbtrfs\t'
            'defaults,noatime,compress=zstd:1,ssd,discard=async,space_cache=v2,nofail,subvol=@data\t0\t0',
            lines,
        )
        self.assertIn(
            'UUID=new-uuid-1234\t/mnt/data/.snapshots\tbtrfs\t'
            'defaults,noatime,compress=zstd:1,ssd,discard=async,space_cache=v2,nofail,subvol=@snapshots\t0\t0',
            lines,
        )

    def test_secondary_btrfs_rejects_system_disk_including_efi(self):
        for mount_expr in ('/dev/nvme0n1p3[/@]', '/dev/nvme0n1p1'):
            with self.subTest(mount_expr=mount_expr):
                result = self.bash('''
lsblk() {
  if [[ "$*" == *"-dnro TYPE"* ]]; then printf 'disk\\n';
  elif [[ "$*" == *"-dnro PKNAME"* ]]; then printf 'nvme0n1\\n'; fi
}
findmnt() {
  if [[ "$*" == *"--mountpoint /efi"* ]]; then printf '%s\\n' "$TEST_SRC"; fi
}
verify_disk_is_secondary /dev/nvme0n1
''', script='preboot/setup_secondary_btrfs_drive.sh', env={'TEST_SRC': mount_expr})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Refusing to operate on system disk', result.stderr)

    def test_secondary_btrfs_rejects_multihop_luks_and_active_swap_on_disk(self):
        swaps_empty = self.root / 'swaps-empty'
        swaps_empty.write_text('Filename\tType\tSize\tUsed\tPriority\n')
        luks_res = self.bash('''
lsblk() {
  if [[ "$*" == *"-dnro TYPE"* ]]; then printf 'disk\\n';
  elif [[ "$*" == *"-dnro PKNAME"* ]]; then
    case "${@: -1}" in
      /dev/mapper/luks-root) printf 'nvme0n1p2\\n' ;;
      /dev/nvme0n1p2) printf 'nvme0n1\\n' ;;
    esac
  fi
}
findmnt() {
  if [[ "$*" == *"--mountpoint /"* ]]; then printf '/dev/mapper/luks-root[/@]\\n'; fi
}
EOS_SWAPS_FILE="$1" verify_disk_is_secondary /dev/nvme0n1
''', swaps_empty, script='preboot/setup_secondary_btrfs_drive.sh')
        self.assertNotEqual(luks_res.returncode, 0)
        self.assertIn('backing /', luks_res.stderr)

        swaps_active = self.root / 'swaps-active'
        swaps_active.write_text(
            'Filename\tType\tSize\tUsed\tPriority\n'
            '/dev/mapper/luks-swap\tpartition\t8388604\t0\t-2\n'
        )
        swap_res = self.bash('''
lsblk() {
  if [[ "$*" == *"-dnro TYPE"* ]]; then printf 'disk\\n';
  elif [[ "$*" == *"-dnro PKNAME"* ]]; then
    case "${@: -1}" in
      /dev/mapper/luks-swap) printf 'nvme0n1p3\\n' ;;
      /dev/nvme0n1p3) printf 'nvme0n1\\n' ;;
    esac
  fi
}
findmnt() { return 0; }
EOS_SWAPS_FILE="$1" verify_disk_is_secondary /dev/nvme0n1
''', swaps_active, script='preboot/setup_secondary_btrfs_drive.sh')
        self.assertNotEqual(swap_res.returncode, 0)
        self.assertIn('backing active swap', swap_res.stderr)

    def test_secondary_btrfs_requires_format_flag_for_non_btrfs_partition(self):
        result = self.bash('''
lsblk() { printf '/dev/nvme1n1p1 part\\n'; }
blkid() { printf 'ext4\\n'; }
locate_existing_btrfs_partition /dev/nvme1n1
''', script='preboot/setup_secondary_btrfs_drive.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pass --format', result.stderr)

    def test_secondary_btrfs_migrates_and_symlinks_downloads_idempotently(self):
        home = self.root / 'home'
        mnt_data = self.root / 'mnt_data'
        (home / 'Downloads').mkdir(parents=True)
        (home / 'Downloads/file.txt').write_text('saved-download')
        mnt_data.mkdir(parents=True)
        result = self.bash('''
MOUNT_DATA="$2"
configure_user_links "$1" "$(id -u)" "$(id -g)"
configure_user_links "$1" "$(id -u)" "$(id -g)"
''', home, mnt_data, script='preboot/setup_secondary_btrfs_drive.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((home / 'data').is_symlink())
        self.assertEqual((home / 'data').resolve(), mnt_data.resolve())
        self.assertTrue((home / 'Downloads').is_symlink())
        self.assertEqual((home / 'Downloads').resolve(), (mnt_data / 'Downloads').resolve())
        self.assertEqual((mnt_data / 'Downloads/file.txt').read_text(), 'saved-download')

    def test_secondary_btrfs_failed_subvolume_or_umount_preserves_mounted_data(self):
        mount_root = self.root / 'mounts'
        mount_root.mkdir()
        for scenario in ('btrfs-create-fail', 'busy-umount-fail'):
            with self.subTest(scenario=scenario):
                case_root = mount_root / scenario
                case_root.mkdir()
                umount_log = self.root / f'{scenario}-umount.log'
                res = self.bash('''
export SECONDARY_MOUNT_ROOT="$1"
export SCENARIO="$2"
export UMOUNT_LOG="$3"
start_workspace secondary-btrfs
trap cleanup_secondary_workspace EXIT
mount() {
  local mnt=${@: -1}
  [[ $mnt != "$WORK_DIR"* ]] || { printf 'mountpoint inside WORK_DIR: %s\n' "$mnt" >&2; return 99; }
  printf 'live-pool-data' > "$mnt/sentinel.txt"
}
btrfs() {
  if [[ $2 == show ]]; then return 1; fi
  if [[ $2 == create && $SCENARIO == btrfs-create-fail ]]; then return 42; fi
  return 0
}
findmnt() {
  local mnt=${@: -1}
  [[ -e "$mnt/sentinel.txt" ]]
}
umount() {
  local mnt=${@: -1}
  printf '%s\n' "$mnt" >> "$UMOUNT_LOG"
  if [[ $SCENARIO == busy-umount-fail ]]; then return 16; fi
  rm -f "$mnt/sentinel.txt"
}
ensure_subvolumes /dev/nvme9n1p1
''', case_root, scenario, umount_log, script='preboot/setup_secondary_btrfs_drive.sh')
                self.assertNotEqual(res.returncode, 0)
                self.assertTrue(umount_log.is_file())
                if scenario == 'btrfs-create-fail':
                    self.assertEqual(res.returncode, 42, res.stderr)
                    self.assertEqual(list(case_root.iterdir()), [])
                else:
                    self.assertEqual(res.returncode, 1, res.stderr)
                    self.assertIn('Unmount failed', res.stderr)
                    dirs = list(case_root.iterdir())
                    self.assertEqual(len(dirs), 1)
                    self.assertEqual((dirs[0] / 'sentinel.txt').read_text(), 'live-pool-data')

    def test_secondary_btrfs_downloads_collision_preserves_existing_file(self):
        home = self.root / 'home'
        mnt_data = self.root / 'mnt_data'
        (home / 'Downloads').mkdir(parents=True)
        (mnt_data / 'Downloads').mkdir(parents=True)
        (home / 'Downloads/file.txt').write_text('new-local-data')
        (mnt_data / 'Downloads/file.txt').write_text('existing-pool-data')
        result = self.bash('''
MOUNT_DATA="$2"
configure_user_links "$1" "$(id -u)" "$(id -g)"
''', home, mnt_data, script='preboot/setup_secondary_btrfs_drive.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((home / 'Downloads').is_symlink())
        self.assertEqual((mnt_data / 'Downloads/file.txt').read_text(), 'new-local-data')
        backups = list((mnt_data / 'Downloads').glob('file.txt.~*~'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'existing-pool-data')

    def test_secondary_btrfs_refuses_mount_root_inside_work_dir_or_symlink(self):
        real_dir = self.root / 'real-dir'
        real_dir.mkdir()
        symlink_root = self.root / 'symlink-mount-root'
        symlink_root.symlink_to(real_dir)

        symlink_res = self.bash('''
export SECONDARY_MOUNT_ROOT="$1"
start_workspace secondary-btrfs
trap cleanup_secondary_workspace EXIT
ensure_subvolumes /dev/nvme9n1p1
''', symlink_root, script='preboot/setup_secondary_btrfs_drive.sh')
        self.assertNotEqual(symlink_res.returncode, 0)
        self.assertIn('Invalid secondary mount root', symlink_res.stderr)

        inside_res = self.bash('''
start_workspace secondary-btrfs
trap cleanup_secondary_workspace EXIT
mkdir -p "$WORK_DIR/sub"
export SECONDARY_MOUNT_ROOT="$WORK_DIR/sub"
ensure_subvolumes /dev/nvme9n1p1
''', script='preboot/setup_secondary_btrfs_drive.sh')
        self.assertNotEqual(inside_res.returncode, 0)
        self.assertIn('must stay outside WORK_DIR', inside_res.stderr)

    def test_secondary_btrfs_downloads_migration_refuses_mounted_disk(self):
        home = self.root / 'home'
        mnt_data = self.root / 'mnt_data'
        usb_mount = home / 'Downloads/usb'
        usb_mount.mkdir(parents=True)
        mnt_data.mkdir(parents=True)
        (home / 'Downloads/local.txt').write_text('local-file')
        (usb_mount / 'keep.txt').write_text('external-disk-file')

        for mount_point in (usb_mount.resolve(), (home / 'Downloads').resolve()):
            with self.subTest(mount_point=mount_point):
                res = self.bash('''
MOUNT_DATA="$2"
FAKE_MOUNT="$3"
findmnt() {
  if [[ "$*" == *"-rn -o TARGET"* ]]; then
    printf '/\\n%s\\n' "$FAKE_MOUNT"
    return 0
  fi
  command findmnt "$@"
}
configure_user_links "$1" "$(id -u)" "$(id -g)"
''', home, mnt_data, mount_point, script='preboot/setup_secondary_btrfs_drive.sh')
                self.assertNotEqual(res.returncode, 0)
                self.assertIn('Refusing to migrate', res.stderr)
                self.assertTrue((home / 'Downloads').is_dir())
                self.assertFalse((home / 'Downloads').is_symlink())
                self.assertEqual((usb_mount / 'keep.txt').read_text(), 'external-disk-file')
                self.assertEqual((home / 'Downloads/local.txt').read_text(), 'local-file')

    def test_secondary_btrfs_partition_and_format_stdout_and_force_guard(self):
        dev_node = self.root / 'nvme9n1p1'
        dev_node.write_text('stub-block')
        # Simulate noisy log/mkfs stdout; captured stdout must hold only the partition path.
        part_res = self.bash('''
PART_NODE="$1"
parted() { printf 'Information: partitioned\\n'; }
partprobe() { :; }
udevadm() { :; }
mkfs.btrfs() { printf 'btrfs-progs v6.11\\nLabel: data\\n'; }
lsblk() { printf '%s part\\n' "$PART_NODE"; }
is_block_device() { [[ $1 == "$PART_NODE" ]]; }
part=$(partition_and_format_disk /dev/nvme9n1)
printf 'CAPTURED=[%s]\\n' "$part"
''', dev_node, script='preboot/setup_secondary_btrfs_drive.sh')
        self.assertEqual(part_res.returncode, 0, part_res.stderr)
        self.assertEqual(part_res.stdout.splitlines()[-1], f'CAPTURED=[{dev_node}]')

        refused = self.bash('''
lsblk() { printf '/dev/nvme9n1\\n/dev/nvme9n1p1\\n'; }
blkid() { if [[ ${5:-} == /dev/nvme9n1p1 ]]; then printf 'btrfs\\n'; fi; }
verify_disk_unformatted_or_forced /dev/nvme9n1 0
''', script='preboot/setup_secondary_btrfs_drive.sh')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('pass --format --force', refused.stderr)

        forced = self.bash('''
lsblk() { printf '/dev/nvme9n1\\n/dev/nvme9n1p1\\n'; }
blkid() { printf 'btrfs\\n'; }
verify_disk_unformatted_or_forced /dev/nvme9n1 1
''', script='preboot/setup_secondary_btrfs_drive.sh')
        self.assertEqual(forced.returncode, 0, forced.stderr)

    def test_secondary_btrfs_refused_format_leaves_existing_mounts_alone(self):
        stubs = '''
unmount_existing_target_mounts() { printf 'UNMOUNTED %s\\n' "$1" >&2; }
partition_and_format_disk() { printf 'FORMATTED %s\\n' "$1" >&2; printf '/dev/nvme9n1p1\\n'; }
locate_existing_btrfs_partition() { printf 'LOCATED %s\\n' "$1" >&2; printf '/dev/nvme9n1p1\\n'; }
lsblk() { printf '/dev/nvme9n1\\n/dev/nvme9n1p1\\n'; }
blkid() { if [[ ${5:-} == /dev/nvme9n1p1 ]]; then printf 'btrfs\\n'; fi; }
'''
        script = 'preboot/setup_secondary_btrfs_drive.sh'
        refused = self.bash(stubs + 'prepare_secondary_partition /dev/nvme9n1 1 0', script=script)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('pass --format --force', refused.stderr)
        self.assertNotIn('UNMOUNTED', refused.stderr)
        self.assertNotIn('FORMATTED', refused.stderr)

        forced = self.bash(stubs + 'prepare_secondary_partition /dev/nvme9n1 1 1', script=script)
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(forced.stdout, '/dev/nvme9n1p1\n')
        self.assertEqual(forced.stderr.splitlines(), ['UNMOUNTED /dev/nvme9n1', 'FORMATTED /dev/nvme9n1'])

        existing = self.bash(stubs + 'prepare_secondary_partition /dev/nvme9n1 0 0', script=script)
        self.assertEqual(existing.returncode, 0, existing.stderr)
        self.assertEqual(existing.stdout, '/dev/nvme9n1p1\n')
        self.assertEqual(existing.stderr.splitlines(), ['UNMOUNTED /dev/nvme9n1', 'LOCATED /dev/nvme9n1'])

    def run_secondary_fstab_update(self, case, env=None):
        """Run update_fstab_and_mount against fixture paths with a findmnt stub that,
        like util-linux, fails --verify when an fstab target directory does not exist."""
        case_root = self.root / case
        etc = case_root / 'etc'
        etc.mkdir(parents=True)
        fstab = etc / 'fstab'
        fstab.write_text('UUID=root-uuid\t/\tbtrfs\tsubvol=@\t0 0\n')
        work = case_root / 'work'
        work.mkdir()
        state = case_root / 'state'
        state.mkdir()
        mnt_data = case_root / 'mnt/data'
        result = self.bash(r'''
FSTAB=$1 WORK_DIR=$2 MOUNT_DATA=$3 MOUNT_SNAPSHOTS=$3/.snapshots STATE=$4
key() { printf '%s/%s' "$STATE" "$(printf '%s' "$1" | tr / _)"; }
blkid() { printf 'data-uuid\n'; }
chown() { :; }
systemctl() { :; }
mount() { printf 'mount %s\n' "$*" >> "$STATE/log"; touch "$(key "${!#}")"; }
umount() { printf 'umount %s\n' "$*" >> "$STATE/log"; rm -f -- "$(key "${!#}")"; }
findmnt() {
  if [[ $1 == --verify ]]; then
    local source target rest status=0
    while read -r source target rest; do
      [[ -n $source && $source != \#* ]] || continue
      [[ -d $target ]] || { printf '%s\n   [E] unreachable on boot required target\n' "$target"; status=1; }
    done < "$3"
    [[ -z ${VERIFY_FAIL:-} ]] || { printf '[E] simulated verify failure\n'; status=1; }
    return "$status"
  fi
  local target=${!#}
  [[ -e $(key "$target") ]] || return 1
  case $3 in
    FSTYPE) printf 'btrfs\n' ;;
    FSROOT) [[ $target == "$MOUNT_DATA" ]] && printf '/@data\n' || printf '/@snapshots\n' ;;
    UUID) printf 'data-uuid\n' ;;
  esac
}
update_fstab_and_mount /dev/fake1 ""
''', fstab, work, mnt_data, state, script='preboot/setup_secondary_btrfs_drive.sh', env=env)
        log = (state / 'log').read_text().splitlines() if (state / 'log').exists() else []
        return result, fstab, mnt_data, log

    def test_secondary_btrfs_fstab_verify_runs_after_mount_points_exist(self):
        result, fstab, mnt_data, log = self.run_secondary_fstab_update('fresh')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = fstab.read_text().splitlines()
        self.assertEqual(lines[0], 'UUID=root-uuid\t/\tbtrfs\tsubvol=@\t0 0')
        self.assertTrue(lines[1].startswith(f'UUID=data-uuid\t{mnt_data}\tbtrfs\t'))
        self.assertTrue(lines[1].endswith(',subvol=@data\t0\t0'))
        self.assertTrue(lines[2].startswith(f'UUID=data-uuid\t{mnt_data}/.snapshots\tbtrfs\t'))
        self.assertTrue(lines[2].endswith(',subvol=@snapshots\t0\t0'))
        self.assertEqual(len(list(fstab.parent.glob('fstab.pre-secondary-btrfs.*'))), 1)
        self.assertEqual(len(log), 2, log)
        self.assertTrue(log[0].startswith('mount -t btrfs -o '))
        self.assertTrue(log[0].endswith(f',subvol=@data UUID=data-uuid {mnt_data}'))
        self.assertEqual(log[1], f'mount {mnt_data}/.snapshots')

    def test_secondary_btrfs_failed_fstab_verify_leaves_fstab_and_unmounts_data(self):
        result, fstab, mnt_data, log = self.run_secondary_fstab_update('verify-fails', env={'VERIFY_FAIL': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('left unchanged', result.stderr)
        self.assertIn('simulated verify failure', result.stderr)
        self.assertEqual(fstab.read_text(), 'UUID=root-uuid\t/\tbtrfs\tsubvol=@\t0 0\n')
        self.assertEqual(list(fstab.parent.iterdir()), [fstab])
        self.assertEqual(log[-1], f'umount {mnt_data}')

    @unittest.skipIf(os.geteuid() == 0, 'requires non-root user to exercise sudo re-exec guard')
    def test_preboot_non_root_sudo_reexec_points_to_existing_script(self):
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        sudo_log = self.root / 'sudo.log'
        sudo_stub = bin_dir / 'sudo'
        sudo_stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$SUDO_LOG"\n')
        sudo_stub.chmod(0o755)
        env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', SUDO_LOG=str(sudo_log))
        for rel_path, args, expected_flag in (
            ('preboot/setup_secondary_btrfs_drive.sh', ['--format', '/dev/nvme9n1'], '--format'),
            ('preboot/setup_btrfs_snapper.sh', ['--skip-hibernation'], '--skip-hibernation'),
        ):
            with self.subTest(script=rel_path):
                sudo_log.unlink(missing_ok=True)
                res = subprocess.run(
                    ['bash', str(REPO / rel_path), *args],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                recorded = sudo_log.read_text().splitlines()
                self.assertEqual(recorded[0], '--')
                self.assertEqual(recorded[1], str(REPO / rel_path))
                self.assertTrue(Path(recorded[1]).is_file())
                self.assertIn(expected_flag, recorded[2:])

    def test_snapper_config_skips_rewrite_when_unchanged(self):
        root = self.root / 'root-config'
        root.write_text('SUBVOLUME="/"\nALLOW_GROUPS=""\nNUMBER_LIMIT="10"\n')
        defaults = self.root / 'snapper-defaults'
        defaults.write_text('SNAPPER_CONFIGS=""\n')
        self.assertTrue(snapper_config.configure(root, defaults))
        root_stat_1 = root.stat()
        defaults_stat_1 = defaults.stat()
        self.assertFalse(snapper_config.configure(root, defaults))
        root_stat_2 = root.stat()
        defaults_stat_2 = defaults.stat()
        self.assertEqual((root_stat_1.st_ino, root_stat_1.st_mtime_ns),
                         (root_stat_2.st_ino, root_stat_2.st_mtime_ns))
        self.assertEqual((defaults_stat_1.st_ino, defaults_stat_1.st_mtime_ns),
                         (defaults_stat_2.st_ino, defaults_stat_2.st_mtime_ns))

    def test_verify_pgp_accepts_exact_single_primary_fingerprint(self):
        key = self.root / 'kicad.asc'
        key.write_text('dummy key')
        fpr = 'FDA854F61C4D0D9572BB95E5245D5502FAD7A805'
        colons = f'pub:-:4096:1:245D5502FAD7A805:1594116023:::-:::scSC::::::23::0:\nfpr:::::::::{fpr}:\n'
        observed_mode = None

        def fake_run(cmd, env=None, **_kwargs):
            nonlocal observed_mode
            self.assertIn('--with-colons', cmd)
            observed_mode = os.stat(env['GNUPGHOME']).st_mode & 0o777
            return subprocess.CompletedProcess(cmd, 0, stdout=colons, stderr='')

        with mock.patch.object(assets.subprocess, 'run', side_effect=fake_run):
            assets.verify_pgp(key, fpr.lower())
        self.assertEqual(observed_mode, 0o700)

    def test_verify_pgp_rejects_mismatch_extra_keys_or_bad_inputs(self):
        key = self.root / 'kicad.asc'
        key.write_text('dummy key')
        fpr = 'FDA854F61C4D0D9572BB95E5245D5502FAD7A805'
        other = '0' * 40

        with self.assertRaises(ValueError):
            assets.verify_pgp(key, 'not-a-fingerprint')
        with self.assertRaises(ValueError):
            assets.verify_pgp(self.root / 'missing.asc', fpr)

        bad_outputs = (
            (1, '', 'gpg error'),
            (0, f'pub:-:4096:\nfpr:::::::::{other}:\n', ''),
            (0, f'pub:-:4096:\nfpr:::::::::{fpr}:\nsub:-:4096:\n', ''),
            (0, f'pub:-:4096:\nfpr:::::::::{fpr}:\npub:-:4096:\nfpr:::::::::{fpr}:\n', ''),
        )
        for rc, stdout, stderr in bad_outputs:
            with self.subTest(rc=rc, stdout=stdout):
                proc = subprocess.CompletedProcess(['gpg'], rc, stdout=stdout, stderr=stderr)
                with mock.patch.object(assets.subprocess, 'run', return_value=proc):
                    with self.assertRaises(ValueError):
                        assets.verify_pgp(key, fpr)

    def test_stm32_install_is_current_and_publish_records_sha256(self):
        target = self.root / 'app'
        wrapper = self.root / 'bin/app'
        desktop = self.root / 'apps/app.desktop'
        wrapper.parent.mkdir(parents=True)
        desktop.parent.mkdir(parents=True)
        wrapper.write_text('wrapper')
        desktop.write_text('desktop')
        digest = 'a' * 64
        result = self.bash('''
VERIFIED_ARCHIVE_SHA256="$1"
begin_install "$2"
printf installed > "$INSTALL_DIR/marker"
publish_install
install_is_current "$2" "$3" "$4"
''', digest, target, wrapper, desktop)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target / '.source.sha256').read_text().strip(), digest)

        mismatch = self.bash(
            'VERIFIED_ARCHIVE_SHA256="$1"; install_is_current "$2" "$3" "$4"',
            'b' * 64, target, wrapper, desktop,
        )
        self.assertNotEqual(mismatch.returncode, 0)

        forced = self.bash(
            'VERIFIED_ARCHIVE_SHA256="$1"; install_is_current "$2" "$3" "$4"',
            digest, target, wrapper, desktop,
            env={'ST_FORCE_REINSTALL': '1'},
        )
        self.assertNotEqual(forced.returncode, 0)

        wrapper.unlink()
        missing_artifact = self.bash(
            'VERIFIED_ARCHIVE_SHA256="$1"; install_is_current "$2" "$3" "$4"',
            digest, target, wrapper, desktop,
        )
        self.assertNotEqual(missing_artifact.returncode, 0)

    def test_stm32_cubemx_rerun_reports_unchanged_without_extra_version_dir(self):
        jar = self.root / 'SetupSTM32CubeMX-test'
        self.jar_fixture(jar, REPO / 'resources/izpack/cubemx.xml')
        archive = self.root / 'mx.zip'
        fake_java = '''#!/usr/bin/env bash
python3 - "$4" <<'PY'
from pathlib import Path
import shutil, sys, xml.etree.ElementTree as ET
directory = Path(ET.parse(sys.argv[1]).find('.//installpath').text)
binary = directory / 'STM32CubeMX'
binary.write_text('#!/bin/sh\\nexit 0\\n')
binary.chmod(0o755)
shutil.copytree('jre', directory / 'jre')
PY
'''
        with zipfile.ZipFile(archive, 'w') as zip_file:
            zip_file.write(jar, jar.name)
            info = zipfile.ZipInfo('jre/bin/java')
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            zip_file.writestr(info, fake_java)

        home = self.root / 'home'
        home.mkdir()
        env = {
            'HOME': str(home),
            'ST_SKIP_UDEV': '1',
            'ST_INSTALLER_SHA256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        }
        code = '''
st_preflight() { require_commands python3 sha256sum realpath find mktemp desktop-file-validate; }
check_elf_dependencies() { return 0; }
refresh_desktop_environment() { return 0; }
main "$1"
'''
        first = self.bash(code, archive, script='install_stm32_cube_mx.sh', env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        versions_dir = home / 'STMicroelectronics/STM32Cube/.STM32CubeMX.versions'
        self.assertEqual(len(list(versions_dir.iterdir())), 1)

        second = self.bash(code, archive, script='install_stm32_cube_mx.sh', env=env)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('(unchanged)', second.stdout)
        self.assertEqual(len(list(versions_dir.iterdir())), 1)

    def test_stm32_role_uses_repo_root_and_idempotent_changed_when(self):
        tasks = yaml.safe_load((REPO / 'ansible/roles/stm32/tasks/main.yml').read_text(encoding='utf-8'))
        probe = next(t for t in tasks if t['name'] == 'Check local availability of requested STMicroelectronics archives')
        self.assertEqual(probe['ansible.builtin.command']['chdir'], '{{ repo_root }}')
        run = next(t for t in tasks if t['name'] == 'Run requested STMicroelectronics user-space installers')
        self.assertIn('(unchanged)', run['changed_when'])

    def test_desktop_udev_handler_reloads_and_triggers_usb_rules(self):
        handlers = yaml.safe_load((REPO / 'ansible/roles/desktop_apps/handlers/main.yml').read_text(encoding='utf-8'))
        reload_handler = next(h for h in handlers if h['name'] == 'Reload desktop udev rules')
        self.assertEqual(
            reload_handler['ansible.builtin.command']['argv'],
            ['udevadm', 'control', '--reload-rules'],
        )
        trigger_handler = next(h for h in handlers if h.get('listen') == 'Reload desktop udev rules')
        self.assertEqual(
            trigger_handler['ansible.builtin.command']['argv'],
            ['udevadm', 'trigger', '--subsystem-match=usb'],
        )
        self.assertTrue(trigger_handler['become'])
        self.assertEqual(trigger_handler['when'], 'eos_systemd_booted | default(false)')

        sys_handlers = yaml.safe_load((REPO / 'ansible/roles/system/handlers/main.yml').read_text(encoding='utf-8'))
        sys_trigger = next(h for h in sys_handlers if h.get('listen') == 'Reload udev rules')
        self.assertEqual(
            sys_trigger['ansible.builtin.command']['argv'],
            ['udevadm', 'trigger', '--subsystem-match=nvme', '--subsystem-match=pci'],
        )
        self.assertTrue(sys_trigger['become'])
        self.assertEqual(sys_trigger['when'], 'eos_systemd_booted | default(false)')

        stm32_handlers = yaml.safe_load((REPO / 'ansible/roles/stm32/handlers/main.yml').read_text(encoding='utf-8'))
        stm32_trigger = next(h for h in stm32_handlers if h.get('listen') == 'Reload stm32 udev rules')
        self.assertEqual(
            stm32_trigger['ansible.builtin.command']['argv'],
            ['udevadm', 'trigger', '--subsystem-match=usb'],
        )
        self.assertTrue(stm32_trigger['become'])
        self.assertEqual(stm32_trigger['when'], 'eos_systemd_booted | default(false)')

    def test_touchpad_udev_handler_reapplies_rules_to_input_devices(self):
        handlers = yaml.safe_load((REPO / 'ansible/roles/desktop_apps/handlers/main.yml').read_text(encoding='utf-8'))
        trigger = next(h for h in handlers if h.get('listen') == 'Reload touchpad udev rules')
        self.assertEqual(
            trigger['ansible.builtin.command']['argv'],
            ['udevadm', 'trigger', '--subsystem-match=input', '--action=change'],
        )
        self.assertTrue(trigger['become'])

    def _cosmic_swipe_tasks(self):
        tasks = yaml.safe_load((REPO / 'ansible/roles/desktop_apps/tasks/cosmic_swipe.yml').read_text(encoding='utf-8'))
        install = next(t for t in tasks if t['name'] == 'Install cosmic-swipe touchpad gestures')
        configure = next(t for t in install['block'] if t['name'] == 'Configure installed cosmic-swipe release')
        return install, configure['block']

    def test_cosmic_swipe_installs_only_on_cosmic_and_cleans_up_when_disabled(self):
        tasks = yaml.safe_load((REPO / 'ansible/roles/desktop_apps/tasks/cosmic_swipe.yml').read_text(encoding='utf-8'))
        install, _ = self._cosmic_swipe_tasks()
        self.assertEqual(
            install['when'],
            ['install_cosmic_swipe | bool', 'desktop_apps_cosmic_comp_stat.stat.exists'],
        )
        remove = next(t for t in tasks if t['name'] == 'Remove cosmic-swipe touchpad gestures when disabled')
        self.assertEqual(remove['when'], 'not (install_cosmic_swipe | bool)')

    def test_cosmic_swipe_udev_rule_sorts_before_logind_acl_rules(self):
        _, configure = self._cosmic_swipe_tasks()
        rule = next(t for t in configure if t['name'] == 'Install touchpad-only udev rule for cosmic-swipe')
        name = Path(rule['ansible.builtin.copy']['dest']).name
        self.assertLess(name, '73-seat-late.rules')

    def test_cosmic_swipe_user_service_is_skipped_in_containers(self):
        _, configure = self._cosmic_swipe_tasks()
        service = next(t for t in configure if t['name'] == 'Enable and start cosmic-swipe user service')
        self.assertIn('systemd_live', service['tags'])

    def test_cosmic_swipe_pin_matches_release_archive_layout(self):
        entry = yaml.safe_load((REPO / 'ansible/group_vars/all/downloads.yml').read_text(encoding='utf-8'))[
            'pinned_downloads']['cosmic_swipe']
        self.assertEqual(
            Path(entry['url']).name,
            f"cosmic-swipe-{entry['version']}-x86_64-linux.tar.gz",
        )


if __name__ == '__main__':
    unittest.main()

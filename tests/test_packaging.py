import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import zipfile

from library_manager import __version__
from scripts.build_package import build


class PackageTests(unittest.TestCase):
    def test_packaged_plugin_can_initialize_database_and_keeps_runtime_resources(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            archive_path = build(root / 'dist', status='stable', tag='v' + __version__)
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                metadata = json.loads(archive.read('metadata.json'))
                version = metadata['versions'][0]
                self.assertEqual(version['status'], 'stable')
                self.assertEqual(version['version'], __version__)
                self.assertEqual((version['kicad_version'], version['kicad_version_max']), ('9.0', '10.99'))
                prefix = 'plugins/kicad_library_manager/'
                manifest = json.loads(archive.read(prefix + 'plugin.json'))
                self.assertEqual(manifest['identifier'], metadata['identifier'])
                for action in manifest['actions']:
                    self.assertIn(prefix + action['entrypoint'], names)
                    for icon in action['icons-light']:
                        self.assertIn(prefix + icon, names)
                self.assertTrue(any('scaffold/db_repo/.github/workflows/' in name for name in names))
                self.assertFalse(any(name.startswith(prefix + 'tests/') for name in names))
                self.assertFalse(any(name.startswith(prefix + '.github/') for name in names))
                self.assertNotIn(prefix + 'scripts/build_package.py', names)
                self.assertFalse(any('__pycache__' in name or name.endswith('.pyc') for name in names))
                archive.extractall(root / 'installed')
            # A fresh process must import the extracted plugin, not this checkout.
            code = (
                'import sys; sys.path.insert(0, sys.argv[1]); '
                'from library_manager.init_db_repo import init_repo_create_missing_only; '
                'result=init_repo_create_missing_only(repo_path=sys.argv[2], '
                'base_branch="main", dbl_filename="library.kicad_dbl"); '
                'assert ".github/workflows/build_db.yml" in result.created; '
                'assert ".github/workflows/assign_ipn.yml" in result.created'
            )
            subprocess.run([
                sys.executable, '-S', '-B', '-c', code,
                str(root / 'installed/plugins/kicad_library_manager'), str(root / 'new-database'),
            ], check=True, capture_output=True, text=True)
            self.assertTrue((root / 'new-database/Database/library.kicad_dbl').is_file())

    def test_release_tag_must_match_source_and_local_build_defaults_to_testing(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(ValueError, 'Tag must match'):
                build(root / 'wrong-tag', tag='v99.0.0')
            self.assertFalse((root / 'wrong-tag').exists())
            with zipfile.ZipFile(build(root / 'trial')) as archive:
                metadata = json.loads(archive.read('metadata.json'))
                self.assertEqual(metadata['versions'][0]['status'], 'testing')

    def test_repeated_builds_have_identical_bytes(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            first = build(root / 'first').read_bytes()
            second = build(root / 'second').read_bytes()
            self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()

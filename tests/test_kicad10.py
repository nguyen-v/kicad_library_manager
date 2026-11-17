from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace
import json
import unittest

from library_manager.library_formats import read_library_table, symbol_names, symbol_metadata
from library_manager.ui import kicad_env as env


class LibraryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.addCleanup(env.configure_runtime, 9)

    def test_nested_tables_keep_real_nicknames_ignore_disabled_and_stop_cycles(self):
        outer, inner = self.root / 'sym-lib-table', self.root / 'nested'
        outer.write_text('(sym_lib_table (version 7) '
                         '(lib (name "Shared") (type "Table") (uri "nested")) '
                         '(lib (name "Off") (type "KiCad") (uri "off.kicad_sym") (disabled)))')
        inner.write_text('(sym_lib_table (version 7) '
                         '(lib (name "Base") (type "KiCad") (uri "${LIBS}/base.kicad_sym") (hidden) '
                         '(descr "A \\"quoted\\" library")) '
                         '(lib (name "Cycle") (type "Table") (uri "sym-lib-table")))')
        rows = read_library_table(str(outer), lambda s: s.replace('${LIBS}', str(self.root)))
        self.assertEqual(set(rows), {'Base'})
        self.assertEqual(rows['Base']['descr'], 'A "quoted" library')

    def test_unpacked_libraries_read_multiple_symbols_and_ignore_unit_nodes(self):
        library = self.root / 'Anything'  # the .kicad_symdir suffix is optional
        library.mkdir()
        (library / 'part.kicad_sym').write_text('(kicad_symbol_lib (version 20241209) '
            '(symbol "Part" (property "Description" "Unicode Ω \\"quoted\\"") '
            '(property "Datasheet" "https://example.test") (symbol "Part_1_1")) '
            '(symbol "Legitimate_1_1"))')
        self.assertEqual(symbol_names(library, 'Nick'), ['Nick:Legitimate_1_1', 'Nick:Part'])
        self.assertEqual(symbol_metadata(library, 'Nick')['Nick:Part'],
                         ('Unicode Ω "quoted"', 'https://example.test'))

    def test_packed_symbols_still_supported(self):
        path = self.root / 'company.kicad_sym'
        path.write_text('(kicad_symbol_lib (symbol "R" (symbol "R_0_1")))')
        self.assertEqual(symbol_names(path, 'company'), ['company:R'])

    def test_config_follows_connected_editor_and_fallback_is_numeric(self):
        for name in ('9.0', '10.0'):
            (self.root / name).mkdir()
        env._KICAD_MAJOR = None
        self.assertEqual(Path(env.kicad_version_dir(str(self.root))).name, '10.0')
        env.configure_runtime(9)
        self.assertEqual(Path(env.kicad_version_dir(str(self.root))).name, '9.0')
        env.configure_runtime(10)
        self.assertEqual(Path(env.kicad_version_dir(str(self.root))).name, '10.0')

    def test_explicit_cli_of_wrong_major_fails_before_rendering(self):
        env.configure_runtime(10)
        with patch.dict('os.environ', {'KICAD_CLI': '/wrong'}), \
             patch('os.path.isfile', return_value=True), patch('os.access', return_value=True), \
             patch.object(env, '_cli_major', return_value=9):
            with self.assertRaisesRegex(RuntimeError, 'matching KiCad 10'):
                env.resolve_kicad_cli()

    def test_future_major_is_not_silently_accepted(self):
        with self.assertRaisesRegex(RuntimeError, 'not supported'):
            env.configure_runtime(11)

    def test_kicad10_state_is_separate_and_kicad9_paths_are_preserved(self):
        from library_manager.paths import config_directory, cache_directory
        with patch.dict('os.environ', {'XDG_CONFIG_HOME': '/config', 'XDG_CACHE_HOME': '/cache'}):
            env.configure_runtime(9)
            self.assertEqual(config_directory(), Path('/config/kicad_library_manager'))
            self.assertEqual(cache_directory(), Path('/cache/kicad_library_manager'))
            env.configure_runtime(10)
            self.assertEqual(config_directory(), Path('/config/kicad_library_manager/10.0'))
            self.assertEqual(cache_directory(), Path('/cache/kicad_library_manager/10.0'))

    def test_project_variables_use_launching_project_not_repo_parent(self):
        env.configure_runtime(10, '/project')
        with patch.object(env, 'prime_kicad_env_vars', return_value={}):
            self.assertEqual(env.expand_kicad_uri('${KIPRJMOD}/lib', '/standalone/library'), '/project/lib')

    def test_older_stock_variables_follow_current_major_unless_explicit(self):
        env.configure_runtime(10)
        with patch.dict('os.environ', {}, clear=True), patch.object(env, 'prime_kicad_env_vars',
                return_value={'KICAD10_FOOTPRINT_DIR': '/ten'}):
            self.assertEqual(env.expand_kicad_uri('${KICAD9_FOOTPRINT_DIR}/X.pretty', ''), '/ten/X.pretty')
        with patch.dict('os.environ', {'KICAD9_FOOTPRINT_DIR': '/nine'}, clear=True), \
             patch.object(env, 'prime_kicad_env_vars', return_value={'KICAD10_FOOTPRINT_DIR': '/ten'}):
            self.assertEqual(env.expand_kicad_uri('${KICAD9_FOOTPRINT_DIR}/X.pretty', ''), '/nine/X.pretty')

    def test_appimage_defaults_and_configured_paths_without_nonexistent_cli_env_command(self):
        config = self.root / 'config'
        (config / '10.0').mkdir(parents=True)
        (config / '10.0/kicad_common.json').write_text(json.dumps({'environment': {'vars': {'COMPANY_LIBS': '/custom'}}}))
        stock = self.root / 'stock'
        (stock / 'symbols').mkdir(parents=True)
        env.configure_runtime(10)
        with patch.dict('os.environ', {'KICAD_CONFIG_HOME': str(config), 'KICAD_STOCK_DATA_HOME': str(stock)}, clear=True), \
             patch.object(env, 'resolve_kicad_cli', return_value='/trial/bin/kicad-cli'):
            values = env.kicad_cli_env_vars()
        self.assertEqual(values['KICAD10_SYMBOL_DIR'], str(stock / 'symbols'))
        self.assertEqual(values['COMPANY_LIBS'], '/custom')
        (config / '10.0/kicad_common.json').write_text('{"environment":{"vars":null}}')
        with patch.dict('os.environ', {'KICAD_CONFIG_HOME': str(config), 'KICAD_STOCK_DATA_HOME': str(stock)}, clear=True), \
             patch.object(env, 'resolve_kicad_cli', return_value='/trial/bin/kicad-cli'):
            self.assertEqual(env.kicad_cli_env_vars()['KICAD10_SYMBOL_DIR'], str(stock / 'symbols'))


if __name__ == '__main__':
    unittest.main()

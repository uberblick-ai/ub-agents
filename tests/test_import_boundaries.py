"""Keep the source dependency directions needed by the future view/log packages."""

import ast
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'ub_agents'


def imported_modules(source):
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = ([alias.name for alias in node.names] if node.module in (None, 'ub_agents')
                     else [node.module])
        else:
            continue
        modules.update(name.removeprefix('ub_agents.').split('.')[0] for name in names)
    return modules


def is_view(module):
    return module == 'view' or module.startswith('view_')


class ImportBoundaryTests(unittest.TestCase):
    def test_core_does_not_import_terminal_view(self):
        for path in sorted(SOURCE.glob('*.py')):
            if is_view(path.stem):
                continue
            with self.subTest(module=path.stem):
                forbidden = sorted(module for module in imported_modules(path.read_text()) if is_view(module))
                self.assertEqual(forbidden, [], f'{path.name} imports terminal view modules: {forbidden}')

    def test_log_format_and_json_do_not_import_each_other(self):
        formats = imported_modules((SOURCE / 'log_format.py').read_text())
        json = imported_modules((SOURCE / 'log_json.py').read_text())
        self.assertFalse('log_json' in formats and 'log_format' in json,
                         'log_format and log_json must have at most one import direction')

    def test_import_scan_includes_local_and_aliased_imports(self):
        for statement in ('from .view_data import ELIGIBLE_LIMIT',
                          'from . import view_data as data',
                          'from ub_agents.view_data import Session',
                          'from ub_agents import view_data as data',
                          'import ub_agents.view_data as data',
                          'import view_data'):
            with self.subTest(statement=statement):
                self.assertIn('view_data', imported_modules('def local():\n    ' + statement))
        self.assertEqual(imported_modules('"from .view_data import Session"\n# import view_data'), set())

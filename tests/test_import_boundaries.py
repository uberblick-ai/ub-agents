"""Keep core imports independent of the terminal view and log import cycles."""

import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


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
    return module == 'view'


class ImportBoundaryTests(unittest.TestCase):
    def test_core_does_not_import_terminal_view(self):
        for path in sorted(SOURCE.rglob('*.py')):
            relative = path.relative_to(SOURCE)
            if relative.parts[0] == 'view':
                continue
            with self.subTest(module=str(relative)):
                forbidden = sorted(module for module in imported_modules(path.read_text()) if is_view(module))
                self.assertEqual(forbidden, [], f'{relative} imports terminal view modules: {forbidden}')

    def test_core_scan_includes_setup_modules_and_excludes_view_package(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for module, statement in (('core.py', 'import json'),
                                      ('init/setup.py', 'from ..view.data import Session'),
                                      ('view/ui.py', 'from . import view')):
                path = source / module
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(statement)
            result = unittest.TestResult()
            with patch(__name__ + '.SOURCE', source):
                ImportBoundaryTests('test_core_does_not_import_terminal_view').run(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.failures), 1)
        self.assertIn('init/setup.py imports terminal view modules', result.failures[0][1])

    def test_log_format_and_json_do_not_import_each_other(self):
        formats = imported_modules((SOURCE / 'log_format.py').read_text())
        json = imported_modules((SOURCE / 'log_json.py').read_text())
        self.assertFalse('log_json' in formats and 'log_format' in json,
                         'log_format and log_json must have at most one import direction')

    def test_import_scan_includes_local_and_aliased_imports(self):
        for statement in ('from .view.data import ELIGIBLE_LIMIT',
                          'from ..view.data import ELIGIBLE_LIMIT',
                          'from .view import data',
                          'from .. import view as data',
                          'from . import view as data',
                          'from ub_agents.view.data import Session',
                          'from ub_agents.view import data',
                          'from ub_agents import view as data',
                          'import ub_agents.view.data',
                          'import ub_agents.view.data as data',
                          'import view'):
            with self.subTest(statement=statement):
                self.assertIn('view', imported_modules('def local():\n    ' + statement))
        self.assertEqual(imported_modules('"from .view.data import Session"\n# import view'), set())

    def test_package_submodules_load_independently_without_textual(self):
        for module in ('init.setup', 'view.request'):
            with self.subTest(module=module):
                script = f'''
import importlib
import sys
importlib.import_module('ub_agents.{module}')
package = 'ub_agents.{module.split('.')[0]}'
assert {{name for name in sys.modules if name.startswith(package + '.')}} == {{'ub_agents.{module}'}}
assert not any(name == 'textual' or name.startswith('textual.') for name in sys.modules)
'''
                result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

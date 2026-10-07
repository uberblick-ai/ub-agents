"""Verify a built wheel's plain launch and terminal view in a clean environment."""

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


def run(*argv, **kwargs):
    subprocess.run(list(map(str, argv)), check=True, **kwargs)


def check(wheel, destination):
    root = destination / 'venv'
    venv.EnvBuilder(with_pip=True, clear=True).create(root)
    python = root / 'bin/python'
    run(python, '-m', 'pip', 'install', '--quiet', wheel)
    run(root / 'bin/ub-agents', '--help', stdout=subprocess.DEVNULL)
    run(python, '-c', 'import ub_agents.cli, sys; assert "textual" not in sys.modules')
    # Exercise argument parsing, launch logging and the loop in the installed
    # wheel, with only the GitHub boundary replaced by a fake.
    script = '''
from pathlib import Path
import sys
from unittest.mock import patch
from ub_agents.cli import main
from ub_agents.config import Config
from ub_agents.loop import Loop
root = Path(sys.argv[1])
cfg = Config(root, 'example/project', (), 1, (), approvals='off')
class GitHub:
    repository = 'example/project'
    def __init__(self, *_): pass
with patch('ub_agents.cli.load_config', return_value=cfg), patch('ub_agents.cli.GitHub', GitHub), \\
     patch('ub_agents.cli.repository_checks', return_value=[]), patch('ub_agents.cli.launch_checks'), \\
     patch.object(Loop, 'launch'), \\
     patch('ub_agents.observations.Publisher', side_effect=OSError('packaging fake')):
    assert main(['--config', str(root / 'ub-agents.yaml'), 'launch', '--no-ui', '--once']) == 0
assert 'textual' not in sys.modules
'''
    # No queue, network or operator checkout participates in this check.
    project = destination / 'project'
    project.mkdir(exist_ok=True)
    # launch looks for the file before loading it; the faked loader ignores its content.
    (project / 'ub-agents.yaml').write_text('')
    run(python, '-P', '-c', script, project)
    run(python, '-P', '-m', 'ub_agents.view', '--probe', '--base-version',
        __import__('tomllib').loads(Path('pyproject.toml').read_text())['project']['version'])
    run(python, '-c', 'from ub_agents.view_ui import View')
    print('Clean-wheel packaging checks passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    if args.destination:
        check(args.wheel.resolve(), args.destination.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix='ub-agents-package-') as directory:
            check(args.wheel.resolve(), Path(directory))

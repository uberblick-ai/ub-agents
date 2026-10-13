from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.support import RecordingRunner, SetupGitHub
from ub_agents.doctor import Doctor
from ub_agents.init_probes import Probes
from ub_agents.init_setup import Applied, Choices, apply_plan, make_plan
from ub_agents.init_ui import line_wizard, run_wizard
from ub_agents.init_wizard import Wizard
from ub_agents.labels import configured_labels


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / 'workflow.yaml'
        self.github = SetupGitHub()
        self.github.metadata['permissions'] = {'push': True}
        self.runner = RecordingRunner(self.root)
        self.plan = make_plan(self.path, 'org/project', Choices('codex:model:high', permissions=True))
        self.github.label_names = [l.name for l in configured_labels(self.plan.config)]
        self.probes = Probes(self.plan, self.github, self.doctor())
        self.wizard = Wizard(self.plan, self.github, self.probes)
        self.wizard.applied = apply_plan(self.plan, self.github)
        self.wizard.current = 'result'
        (self.root / '.agents/ub_agents.md').write_text('Checks: python -m unittest\nMerge: required checks and reviewer approval.\nDecisions: @owner.\nReview: durable ownership and process cleanup.\n')

    def doctor(self):
        return Doctor(github=self.github, runner=self.runner, which=lambda name: '/bin/' + name)

    def test_readiness_is_read_only_retries_external_preparation_and_start_rechecks(self):
        status = ('git', '-C', str(self.root), '--no-optional-locks', 'status', '--porcelain=v1', '--untracked-files=all')
        self.runner.responses[status] = '?? workflow.yaml'
        self.assertFalse(self.wizard.request_start())
        self.assertFalse(self.wizard.start_loop)
        self.assertIn('dirty', self.wizard.readiness_text)
        self.assertFalse((self.root / '.ub-agents').exists())
        self.runner.responses[status] = ''
        self.assertTrue(self.wizard.retry_readiness(), self.wizard.readiness_text)
        # A readiness retry never launches; selecting Start must check afresh.
        self.assertFalse(self.wizard.start_loop)
        self.runner.responses[status] = 'M workflow.yaml'
        self.assertFalse(self.wizard.request_start())
        self.runner.responses[status] = ''
        self.assertTrue(self.wizard.request_start(), self.wizard.readiness_text)
        self.assertEqual(self.github.writes, [])

    def test_default_branch_ahead_divergence_labels_policy_permissions_and_runtime_block(self):
        branch = ('git', '-C', str(self.root), 'rev-parse', '--abbrev-ref', 'HEAD')
        ahead = ('git', '-C', str(self.root), 'rev-list', '--left-right', '--count', 'HEAD...refs/remotes/origin/main')
        auth = ('codex', 'login', 'status')
        for key, value, expected in ((branch, 'setup', 'launch runs only from main'),
                                     (ahead, '1\t0', 'local commits'), (ahead, '1\t1', 'diverged'),
                                     (auth, subprocess.CompletedProcess([], 1, '', ''), 'auth')):
            original = self.runner.responses[key]
            self.runner.responses[key] = value
            self.assertFalse(self.wizard.request_start())
            self.assertIn(expected, self.wizard.readiness_text)
            self.runner.responses[key] = original
        self.github.label_names.remove('needs-preparation')
        self.assertFalse(self.wizard.request_start())
        self.assertIn('Missing GitHub workflow labels', self.wizard.readiness_text)
        self.github.label_names.append('needs-preparation')
        (self.root / '.agents/ub_agents.md').write_text('Decision: @org/maintainers\n')
        self.assertFalse(self.wizard.request_start())
        self.assertIn('Replace project check placeholders', self.wizard.readiness_text)
        (self.root / '.agents/ub_agents.md').write_text('Reviewed project policy\n')
        (self.root / 'ub-agents.local.yaml').write_text('runtime-args:\n  codex: []\n')
        self.assertFalse(self.wizard.request_start())
        self.assertIn('permissions', self.wizard.readiness_text)

    def test_unavailable_required_checks_and_partial_setup_cannot_launch(self):
        self.github.auth_error = OSError('gh unavailable')
        self.assertFalse(self.wizard.request_start())
        self.assertFalse(self.wizard.start_loop)
        self.wizard.applied = Applied(error='file write failed')
        self.assertFalse(self.wizard.request_start())
        self.assertIn('recovery', self.wizard.readiness_text)

    def test_fallback_finish_and_retry_never_launch_and_start_only_after_checks(self):
        with patch('builtins.input', side_effect=['r', '']), redirect_stdout(io.StringIO()):
            self.assertTrue(line_wizard(self.wizard))
        self.assertFalse(self.wizard.start_loop)
        with patch('builtins.input', side_effect=['l']), redirect_stdout(io.StringIO()):
            self.assertTrue(line_wizard(self.wizard))
        self.assertTrue(self.wizard.start_loop)

    def test_launch_dispatch_occurs_after_full_screen_exit_and_only_after_explicit_start(self):
        for start in (False, True):
            self.wizard.start_loop = start
            with patch('ub_agents.init_ui.os.environ', {'TERM': 'dumb'}), \
                    patch('ub_agents.init_ui.line_wizard', return_value=True), \
                    patch('ub_agents.cli.main', return_value=0) as launch, redirect_stdout(io.StringIO()):
                self.assertEqual(run_wizard(self.wizard), 0)
            if start:
                launch.assert_called_once_with(['launch', '--config', str(self.path)])
            else:
                launch.assert_not_called()

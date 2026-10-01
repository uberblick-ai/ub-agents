from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import shlex
import unittest
from unittest.mock import patch

from ub_agents.labels import configured_labels, provision_labels
from tests.support import DoctorGitHub, agent, config


class LabelTests(unittest.TestCase):
    def setUp(self):
        root = Path('/synthetic')
        self.config = replace(config(root, agent(root, triggers=('custom-start',), outcomes={
            'done': {'add': ('custom-destination',), 'remove': ('legacy-state',)},
            'parked': {'add': ('custom-stop',), 'remove': ()}}),
            agent(root, name='reviewer', kind='pr', triggers=('CUSTOM-DESTINATION',))),
            stop_labels=('CUSTOM-STOP',))
        self.github = DoctorGitHub()
        self.github.label_names = ['CUSTOM-START', 'unrelated']

    def test_inventory_retains_all_consumers_and_deduplicates_case_insensitively(self):
        labels = configured_labels(self.config)
        self.assertEqual([label.name for label in labels],
                         ['custom-start', 'custom-destination', 'legacy-state', 'custom-stop'])
        self.assertEqual([use.agent for use in labels[1].uses], ['worker', 'reviewer'])
        self.assertIn('starts reviewer on a PR', labels[1].uses[1].meaning)
        self.assertIn('removed from the assignment by worker outcome done', labels[2].uses[0].meaning)
        self.assertEqual([use.required for use in labels[3].uses], [True, False])

    def test_confirmed_provisioning_follows_the_configuration(self):
        with patch.dict('os.environ', {'CI': ''}), \
                patch('ub_agents.labels.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value='Y'), redirect_stdout(io.StringIO()) as output:
            with patch.object(output, 'isatty', return_value=True):
                provision_labels(self.config, self.github)
        self.assertEqual([write[1] for write in self.github.writes],
                         ['custom-destination', 'legacy-state', 'custom-stop'])
        self.assertIn('outcome done', output.getvalue())
        self.assertIn('parks an issue or PR', output.getvalue())

    def test_eof_declines_creation(self):
        with patch.dict('os.environ', {'CI': ''}), \
                patch('ub_agents.labels.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=EOFError), redirect_stdout(io.StringIO()) as output:
            with patch.object(output, 'isatty', return_value=True):
                provision_labels(self.config, self.github)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(output.getvalue().count('gh label create '), 3)

    def test_creation_commands_quote_label_names_and_descriptions(self):
        for name in ('needs review', "reviewer's input", 'label;$(echo unsafe)'):
            cfg = replace(self.config, agents=(agent(self.config.root, triggers=(name,)),), stop_labels=())
            label = configured_labels(cfg)[0]
            self.assertEqual(shlex.split(label.command(cfg.repository)),
                             ['gh', 'label', 'create', name, '--repo', 'org/project',
                              '--description', label.description, '--color', label.color])

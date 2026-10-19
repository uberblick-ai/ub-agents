from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import shlex
import unittest
from unittest.mock import patch

from ub_agents.labels import Label, LabelUse, configured_labels, provision_labels
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
                         ['custom-start', 'custom-destination', 'legacy-state', 'custom-stop', 'outside-input'])
        self.assertEqual([use.agent for use in labels[1].uses], ['reviewer', 'worker'])
        self.assertEqual(labels[1].explanation, 'starts reviewer on a PR; added when worker reports done')
        self.assertEqual(labels[1].description, 'ub-agents: starts reviewer on a PR; added when worker reports done')
        self.assertEqual(labels[2].explanation, 'removed when worker reports done')
        self.assertEqual([use.required for use in labels[3].uses], [False, True])
        self.assertEqual(labels[3].explanation,
                         'parks an issue or PR until a person decides; added when worker reports parked')

    def test_workflow_colors_match_names_case_insensitively_with_a_fallback(self):
        expected = {'needs-preparation': 'c5def5', 'ready': '0e8a16', 'needs-review': 'fbca04',
                    'needs-changes': 'd93f0b', 'ready-to-merge': '1d76db', 'custom-start': '1d76db',
                    'needs-human': '1d76db', 'outside-input': '1d76db'}
        for uppercase in (False, True):
            with self.subTest(uppercase=uppercase):
                names = tuple(name.upper() if uppercase else name for name in expected)
                cfg = replace(self.config, agents=(agent(self.config.root, triggers=names),), stop_labels=())
                self.assertEqual({label.name.casefold(): label.color for label in configured_labels(cfg)}, expected)

    def test_stop_colors_override_starter_names_and_custom_names(self):
        cfg = replace(self.config, agents=(agent(self.config.root, triggers=('READY', 'custom-stop')),),
                      stop_labels=('ready', 'CUSTOM-STOP', 'needs-human'))
        labels = configured_labels(cfg)
        self.assertEqual({label.name: label.color for label in labels},
                         {'READY': 'b60205', 'custom-stop': 'b60205', 'needs-human': 'b60205',
                          'outside-input': '1d76db'})
        for label in labels:
            command = shlex.split(label.command(cfg.repository))
            self.assertEqual(command[command.index('--color') + 1], label.color)

    def test_outside_input_is_optional_only_when_approvals_are_on(self):
        for configured, resolved, expected in (('on', None, True), ('off', None, False),
                                               (None, 'on', True), (None, 'off', False)):
            with self.subTest(configured=configured, resolved=resolved):
                labels = configured_labels(replace(self.config, approvals=configured), approvals=resolved)
                outside = [label for label in labels if label.name == 'outside-input']
                self.assertEqual(bool(outside), expected)
                if outside:
                    self.assertFalse(any(use.required for use in outside[0].uses))

    def test_description_keeps_whole_clauses_at_the_character_limit(self):
        effect = LabelUse('reviewer', 'starts reviewer on a PR')
        transition = LabelUse('worker', 'added when worker reports ' + 'x' * 38)
        label = Label('review', (effect, transition, LabelUse('worker', 'removed when worker reports done')))
        self.assertEqual(len(label.description), 100)
        self.assertEqual(label.description, f'ub-agents: {effect.meaning}; {transition.meaning}')
        longer = replace(label, uses=(effect, replace(transition, meaning=transition.meaning + 'x')))
        self.assertEqual(longer.description, f'ub-agents: {effect.meaning}')

    def test_overlong_first_clause_shortens_at_a_word_boundary(self):
        label = Label('review', (LabelUse('worker', 'starts ' + 'long-name-' * 20 + ' on an issue or PR'),))
        self.assertEqual(label.description, 'ub-agents: starts...')

    def test_confirmed_provisioning_follows_the_configuration(self):
        with patch.dict('os.environ', {'CI': ''}), \
                patch('ub_agents.labels.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value='Y'), redirect_stdout(io.StringIO()) as output:
            with patch.object(output, 'isatty', return_value=True):
                provision_labels(self.config, self.github)
        self.assertEqual([write[1] for write in self.github.writes],
                         ['custom-destination', 'legacy-state', 'custom-stop', 'outside-input'])
        self.assertIn('reports done', output.getvalue())
        self.assertIn('parks an issue or PR', output.getvalue())

    def test_eof_declines_creation(self):
        with patch.dict('os.environ', {'CI': ''}), \
                patch('ub_agents.labels.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', side_effect=EOFError), redirect_stdout(io.StringIO()) as output:
            with patch.object(output, 'isatty', return_value=True):
                provision_labels(self.config, self.github)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(output.getvalue().count('gh label create '), 4)

    def test_creation_commands_quote_label_names_and_descriptions(self):
        for name in ('needs review', "reviewer's input", 'label;$(echo unsafe)'):
            cfg = replace(self.config, agents=(agent(self.config.root, triggers=(name,)),), stop_labels=())
            label = configured_labels(cfg)[0]
            self.assertEqual(shlex.split(label.command(cfg.repository)),
                             ['gh', 'label', 'create', name, '--repo', 'org/project',
                              '--description', label.description, '--color', label.color])

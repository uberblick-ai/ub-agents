"""Execute the generated Actions script against a recording gh executable."""

import ast
from importlib.resources import files
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from ub_agents.init.setup import Choices, make_plan
from ub_agents.labels import configured_labels


TEMPLATE = files('ub_agents').joinpath('templates/close-parent-issues.yaml').read_text()
WORKFLOW = yaml.safe_load(TEMPLATE)
PREFIX = 'repos/org/project/issues'


def parent(number=10, **changes):
    return {'number': number, 'repository_url': 'https://api.github.com/repos/org/project',
            'state': 'open', 'labels': [], 'sub_issues_summary': {'total': 2}, **changes}


def child(reason='completed', state='closed'):
    return {'state': state, 'state_reason': reason}


class CloseParentIssuesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.responses = {
            f'{PREFIX}/1/parent': parent(),
            f'{PREFIX}/10/sub_issues?per_page=100': [[child(), child('not_planned')]],
            'graphql:10': {'data': {'repository': {'issue': {'closedByPullRequestsReferences': {'totalCount': 0}}}}},
            f'{PREFIX}/10': parent(state='closed'),
            f'{PREFIX}/10/parent': {'error': 404},
        }
        (self.root / 'event.json').write_text(json.dumps({'issue': {'number': 1}}))
        stub = self.root / 'gh'
        stub.write_text(f'#!{sys.executable}\n' + '''
import json
from pathlib import Path
import sys

args = sys.argv[1:]
assert args[:3] == ['api', '--hostname', 'github.com'], args
endpoint = args[3]
with Path('calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
if endpoint == 'graphql':
    assert 'includeClosedPrs: false' in args[args.index('-f') + 1]
    endpoint += ':' + next(value.split('=')[1] for value in args if value.startswith('number='))
if '/sub_issues?' in endpoint:
    assert '--paginate' in args and '--slurp' in args
responses = json.loads(Path('responses.json').read_text())
assert endpoint in responses, endpoint
value = responses[endpoint]
if isinstance(value, dict) and 'error' in value:
    print(f"gh: failed (HTTP {value['error']})", file=sys.stderr)
    raise SystemExit(1)
print(json.dumps(value))
''')
        stub.chmod(0o755)

    def run_workflow(self, *, succeeds=True):
        (self.root / 'responses.json').write_text(json.dumps(self.responses))
        script = WORKFLOW['jobs']['close-parents']['steps'][0]['run']
        result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=self.root,
                                env={**os.environ, 'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
                                     'GITHUB_REPOSITORY': 'org/project',
                                     'GITHUB_EVENT_PATH': str(self.root / 'event.json')},
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        return [call for call in calls if '--method' in call], calls

    def test_workflow_has_only_issue_permission_and_labels_match_starter(self):
        self.assertEqual(WORKFLOW['permissions'], {'issues': 'write'})
        # PyYAML's YAML 1.1 parser treats the Actions key `on` as True.
        self.assertEqual(WORKFLOW[True], {'issues': {'types': ['closed']}})
        job = WORKFLOW['jobs']['close-parents']
        self.assertNotIn('permissions', job)
        step, = job['steps']
        self.assertEqual(step['env'], {'GH_TOKEN': '${{ github.token }}'})
        source = step['run'].split("<<'PY'\n", 1)[1].removesuffix('PY\n')
        labels = next(ast.literal_eval(node.value) for node in ast.parse(source).body
                      if isinstance(node, ast.Assign) and node.targets[0].id == 'WORKFLOW_LABELS')
        plan = make_plan(self.root / 'ub-agents.yaml', 'org/project', Choices('codex:model:high'))
        self.assertEqual(labels, {label.name for label in configured_labels(plan.config, approvals='on')})
        self.assertIn('Keep trigger, transition and stop labels in sync with ub-agents.yaml', TEMPLATE)
        self.assertEqual(plan.targets[self.root / '.github/workflows/close-parent-issues.yml'], TEMPLATE)

    def test_all_closed_uses_completed_if_any_completed_even_on_later_page(self):
        self.responses[f'{PREFIX}/10/sub_issues?per_page=100'] = [[child('not_planned')], [child()]]
        writes, _ = self.run_workflow()
        self.assertEqual(writes, [['api', '--hostname', 'github.com', f'{PREFIX}/10', '--method', 'PATCH',
                                  '-f', 'state=closed', '-f', 'state_reason=completed']])

    def test_all_not_planned_or_duplicate_closes_not_planned(self):
        self.responses[f'{PREFIX}/10/sub_issues?per_page=100'] = [[child('not_planned'), child('duplicate')]]
        writes, _ = self.run_workflow()
        self.assertEqual(len(writes), 1)
        self.assertIn('state_reason=not_planned', writes[0])

    def test_open_sibling_on_later_page_keeps_parent_open(self):
        self.responses[f'{PREFIX}/10/sub_issues?per_page=100'] = [[child()], [child(None, 'open')]]
        self.assertEqual(self.run_workflow()[0], [])

    def test_each_workflow_label_keeps_parent_open(self):
        for label in ('needs-preparation', 'ready', 'needs-review', 'needs-changes',
                      'ready-to-merge', 'needs-human', 'outside-input'):
            with self.subTest(label=label):
                self.responses[f'{PREFIX}/1/parent'] = parent(labels=[{'name': label.upper()}])
                self.assertEqual(self.run_workflow()[0], [])

    def test_ordinary_label_does_not_prevent_closure(self):
        self.responses[f'{PREFIX}/1/parent'] = parent(labels=[{'name': 'enhancement'}])
        self.assertEqual(len(self.run_workflow()[0]), 1)

    def test_open_linked_pr_keeps_parent_open(self):
        self.responses['graphql:10']['data']['repository']['issue']['closedByPullRequestsReferences']['totalCount'] = 1
        self.assertEqual(self.run_workflow()[0], [])

    def test_cross_repository_parent_is_never_changed_or_followed(self):
        self.responses[f'{PREFIX}/1/parent'] = parent(repository_url='https://api.github.com/repos/org/other')
        writes, calls = self.run_workflow()
        self.assertEqual(writes, [])
        self.assertEqual(len(calls), 1)

    def test_no_parent_is_normal(self):
        self.responses[f'{PREFIX}/1/parent'] = {'error': 404}
        self.assertEqual(self.run_workflow()[0], [])

    def test_nested_parents_close_in_one_run(self):
        self.responses.update({
            f'{PREFIX}/10/parent': parent(20, sub_issues_summary={'total': 1}),
            f'{PREFIX}/20/sub_issues?per_page=100': [[child()]],
            'graphql:20': self.responses['graphql:10'],
            f'{PREFIX}/20': parent(20, state='closed'),
            f'{PREFIX}/20/parent': {'error': 404},
        })
        writes, _ = self.run_workflow()
        self.assertEqual([call[3] for call in writes], [f'{PREFIX}/10', f'{PREFIX}/20'])

    def test_closed_parent_is_untouched(self):
        self.responses[f'{PREFIX}/1/parent'] = parent(state='closed')
        self.assertEqual(self.run_workflow()[0], [])

    def test_unreadable_ancestor_does_not_undo_a_confirmed_closure(self):
        self.responses[f'{PREFIX}/10/parent'] = {'error': 403}
        writes, _ = self.run_workflow(succeeds=False)
        self.assertEqual([call[3] for call in writes], [f'{PREFIX}/10'])

    def test_failed_or_partial_reads_leave_parent_open(self):
        for endpoint, value in (
                (f'{PREFIX}/1/parent', {'error': 403}),
                (f'{PREFIX}/1/parent', parent(labels=None)),
                (f'{PREFIX}/10/sub_issues?per_page=100', {'error': 500}),
                (f'{PREFIX}/10/sub_issues?per_page=100', [[child()]]),
                (f'{PREFIX}/10/sub_issues?per_page=100', [[{'state': 'closed'}]]),
                (f'{PREFIX}/10/sub_issues?per_page=100', [[child(None)]]),
                ('graphql:10', {'errors': [{'message': 'not accessible'}],
                                'data': {'repository': {'issue': None}}}),
                ('graphql:10', {'data': {'repository': {'issue': None}}})):
            with self.subTest(endpoint=endpoint, value=value):
                original = self.responses[endpoint]
                self.responses[endpoint] = value
                (self.root / 'calls.jsonl').unlink(missing_ok=True)
                self.assertEqual(self.run_workflow(succeeds=False)[0], [])
                self.responses[endpoint] = original

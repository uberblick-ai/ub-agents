from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.config import load_config
from ub_agents.doctor import diagnose
from ub_agents.init_setup import Choices, apply_plan, make_plan
from ub_agents.loop import Loop
from tests.support import FakeGitHub, RecordingRunner, agent, issue, pr, stub_refresh


class StarterWorkflowTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.path = self.root / 'ub-agents.yaml'
        self.github = FakeGitHub(issue(labels=('needs-preparation', 'unrelated')))
        plan = make_plan(self.path, 'org/project', Choices('codex:model:high'))
        self.assertTrue(apply_plan(plan, self.github).ok)
        starter = load_config(self.path)
        # Exercise the generated roles and outcomes without a real runtime or worktree.
        roles = tuple(replace(role, command=agent(self.root).command, runtimes=(), worktree=False)
                      for role in starter.agents)
        self.loop = Loop(replace(starter, agents=roles), self.github, 'operator', output=lambda *_: None)

    def execute(self, name, outcome, *, number=1, summary='Requirements checked', feedback=None):
        plan = next(plan for plan in self.loop.plans()
                    if plan.item.number == number and plan.agent.name == name)
        self.assertEqual(plan.state, 'ready')

        def supervise(command, cwd, env, *args, **kwargs):
            context = json.loads(Path(env['UB_AGENTS_CONTEXT']).read_text())
            self.assertEqual(context['agent'], name)
            if feedback is not None:
                self.assertEqual(context['feedback'], feedback)
            lease = next(record for record in reversed(self.loop.coordinator.history(number))
                         if record['kind'] == 'lease')
            self.loop.coordinator.report(lease, 'success', summary, outcome=outcome)
            return 0

        with patch('ub_agents.assignment_run.supervise', side_effect=supervise) as executed:
            self.loop.execute(plan)
        executed.assert_called_once()
        report = self.loop.coordinator.history(number)[-1]
        self.assertTrue(report['accepted'])
        return report

    def test_fresh_starter_passes_offline_doctor(self):
        result = diagnose(self.path, offline=True, runner=RecordingRunner(self.root),
                          which=lambda name: '/tools/' + name)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(list((self.root / '.agents').glob('*.md'))), 6)
        label, = [check for check in result['checks'] if check['id'] == 'github-label:needs-review']
        self.assertEqual([use['meaning'] for use in label['uses'][:2]],
                         ['starts issue-reviewer on an issue', 'starts reviewer on a PR'])

    def test_only_pr_reviewer_starts_with_a_round_limit_and_p1_p2_rules(self):
        bounded = [(role.name, name, changes['max-rounds']) for role in self.loop.config.agents
                   for name, changes in role.outcomes.items() if 'max-rounds' in changes]
        self.assertEqual(bounded, [('reviewer', 'changes-requested', 3)])
        text = (self.root / '.agents/reviewer.md').read_text()
        for phrase in ('P1 or P2', 'unmet acceptance', 'failing check', 'outcome_rounds',
                       'only P2 findings remain', 'each open P1 finding'):
            self.assertIn(phrase, text)

    def test_starter_parks_the_fourth_pr_changes_request(self):
        self.github.items[2] = pr(labels=('needs-review', 'unrelated'))
        for used in range(4):
            self.github.change(2, labels=frozenset({'needs-review', 'unrelated'}))
            self.execute('reviewer', 'changes-requested', number=2, summary='P1: acceptance criterion unmet.')
            self.assertEqual(self.github.item(2).labels,
                             {'unrelated', 'needs-changes' if used < 3 else 'needs-human'})
        notice, = [c['body'] for c in self.github.store[2] if '**Action needed**' in c['body']]
        self.assertIn('max-rounds: 3', notice)
        self.assertIn('P1: acceptance criterion unmet.', notice)

    def test_shared_review_label_starts_each_reviewer_only_for_its_kind(self):
        self.github.items = {1: issue(labels=('needs-review',)), 2: pr(labels=('needs-review',)),
                             3: issue(3, labels=()), 4: pr(4, labels=())}
        self.assertEqual({(plan.item.number, plan.agent.name) for plan in self.loop.plans()},
                         {(1, 'issue-reviewer'), (2, 'reviewer')})
        self.execute('issue-reviewer', 'approved')
        self.execute('reviewer', 'approved', number=2)
        self.assertEqual(self.github.item(1).labels, {'ready'})
        self.assertEqual(self.github.item(2).labels, {'ready-to-merge'})

    def test_preparation_review_then_approval_makes_issue_ready(self):
        self.execute('issue-preparer', 'review')
        self.assertEqual(self.github.item(1).labels, {'needs-review', 'unrelated'})
        self.execute('issue-reviewer', 'approved')
        self.assertEqual(self.github.item(1).labels, {'ready', 'unrelated'})

    def test_review_findings_return_as_preparer_feedback(self):
        self.execute('issue-preparer', 'review')
        findings = 'Bound the migration scope and specify observable acceptance criteria.'
        report = self.execute('issue-reviewer', 'changes-requested', summary=findings)
        self.assertEqual(self.github.item(1).labels, {'needs-preparation', 'unrelated'})
        self.execute('issue-preparer', 'prepared', feedback=[{
            'agent': 'issue-reviewer', 'outcome': 'changes-requested', 'summary': findings,
            'candidate_sha': None, 'created': report['created']}])
        self.assertEqual(self.github.item(1).labels, {'ready', 'unrelated'})

    def test_split_removes_trigger_without_adding_a_label_or_closing_parent(self):
        self.execute('issue-preparer', 'split')
        self.assertEqual(self.github.item(1).labels, {'unrelated'})
        self.assertEqual(self.github.item(1).state, 'open')
        self.assertFalse(any(write[0] == 'add-labels' for write in self.github.writes))

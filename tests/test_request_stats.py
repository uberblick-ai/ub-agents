from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.config import Queue, Runtime
from ub_agents.loop import Loop
from ub_agents.polling import DiscoveryBudget
from ub_agents.records import iso
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class RequestStatsTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.github = FakeGitHub(issue())
        self.lines = []
        self.loop = Loop(config(self.root), self.github, 'operator', output=self.lines.append)
        self.loop.coordinator.clock = lambda: 1000

    def test_budget_charges_failed_claim_checks_and_stops_at_successful_boundary(self):
        for claimed in (False, True):
            with self.subTest(claimed=claimed):
                self.loop.discovery_budget = DiscoveryBudget(clock=lambda: 1000)
                with self.loop.discovery_pass():
                    self.github.quota_requests += 100
                    self.loop._claim_boundary()
                    self.github.quota_requests += 50  # Unsuccessful claim's checks remain discovery.
                    if claimed:
                        boundary = self.loop._claim_boundary()
                        self.github.quota_requests += 70  # A successful claim's run traffic.
                        self.loop._claimed_pass(boundary)
                self.assertIn('REST quota=150,', self.lines[-1])
                self.assertEqual(self.loop.discovery_budget.balance, -25)
                self.assertAlmostEqual(self.loop.discovery_budget.wait_seconds(), 374.4)

    def test_idle_pass_coalesces_changed_duration_but_reports_changed_counts_or_candidates(self):
        self.github.items.clear()
        with patch('ub_agents.request_stats.monotonic', side_effect=[1, 2, 10, 50]):
            self.assertFalse(self.loop.tick())
            self.assertFalse(self.loop.tick())
        self.assertEqual(len(self.lines), 1)
        self.assertIn('empty: 1.0s', self.lines[0])
        self.assertIn('candidates reached=0', self.lines[0])
        self.github.gh_calls += 1  # Prior requests are outside the next pass.
        self.assertFalse(self.loop.tick())
        self.assertEqual(len(self.lines), 1)
        observe = self.github.observe

        def read(details=True):
            self.github.gh_calls += 2
            self.github.not_modified_responses += 2
            return observe(details)

        with patch.object(self.github, 'observe', side_effect=read):
            self.assertFalse(self.loop.tick())
            self.assertFalse(self.loop.tick())
        self.assertEqual(len(self.lines), 2)
        self.assertIn('gh calls=2, REST quota=0, HTTP 304=2', self.lines[-1])
        self.github.items[1] = issue(labels=('ready', 'needs-human'))
        self.assertFalse(self.loop.tick())
        self.assertIn('candidates reached=1', self.lines[-1])

    def test_multiple_agent_rows_count_one_candidate(self):
        self.loop.config = config(self.root, agent(self.root, name='first'), agent(self.root, name='second'))
        self.github.change(1, labels=frozenset({'ready', 'needs-human'}))
        self.assertFalse(self.loop.tick())
        self.assertEqual(sum(line.startswith('#1 ') for line in self.lines), 2)
        self.assertIn('candidates reached=1', self.lines[-1])

    def test_reached_candidate_counts_even_when_its_evaluation_fails(self):
        with patch.object(self.loop, '_item_plans', side_effect=ValueError('Cannot evaluate')):
            with self.assertRaisesRegex(ValueError, 'Cannot evaluate'):
                self.loop.tick()
        self.assertIn('candidates reached=1', self.lines[-1])

    def test_consecutive_declines_coalesce_but_missing_pass_and_changed_reason_reset(self):
        self.github = FakeGitHub(pr())
        self.loop = Loop(config(self.root), self.github, 'operator', output=self.lines.append)
        head = 0

        def refresh(*args, **kwargs):
            nonlocal head
            head += 1
            self.github.change(2, head=f'{head:040x}')
            return ''

        with patch('ub_agents.loop.refresh_instructions', side_effect=refresh):
            self.assertFalse(self.loop.tick())
            self.assertFalse(self.loop.tick())
            declines = [line for line in self.lines if 'declined' in line]
            self.assertEqual(declines, ['#2 worker: declined — Head moved before claim'])
            self.github.change(2, labels=frozenset())
            self.assertFalse(self.loop.tick())
            self.github.change(2, labels=frozenset({'needs-changes'}))
            self.assertFalse(self.loop.tick())
        self.assertEqual(sum('Head moved before claim' in line for line in self.lines), 2)
        plan = self.loop.plans()[0]
        with self.loop.discovery_pass():
            self.loop.declined(plan, 'Runtime changed before claim')
            self.loop.declined(plan, 'Runtime changed before claim')
        self.assertEqual(sum('Runtime changed before claim' in line for line in self.lines), 1)

    def test_silent_claim_checks_explain_distinct_declines_without_writes(self):
        for change, expected in (
                ('head', 'Head moved before claim'),
                ('trigger', 'Start gate: No trigger matches'),
                ('stop', 'Fresh plan is no longer ready: parked — Stop label needs-human is present'),
                ('runtime', 'Runtime changed before claim'),
                ('milestone', 'Milestone gate: Waiting for active milestone #2'),
                ('blockers', 'Open blockers: #3')):
            with self.subTest(change=change):
                github = FakeGitHub(pr() if change == 'head' else issue(), issue(3, labels=()))
                co = Loop(config(self.root), github, 'operator', output=self.lines.append).coordinator
                plan = co.plan(github.item(2 if change == 'head' else 1), agent(self.root), ())
                self.lines.clear()
                if change == 'head':
                    github.change(2, head='b' * 40)
                elif change == 'trigger':
                    github.change(1, labels=frozenset())
                elif change == 'stop':
                    github.change(1, labels=frozenset({'ready', 'needs-human'}))
                elif change == 'runtime':
                    co.plan = lambda *args, **kwargs: replace(plan, runtime=Runtime('codex', 'model', 'high'))
                elif change == 'milestone':
                    co.queue = Queue(milestones='gate')
                    github.milestones = [{'number': 2, 'state': 'open', 'open_issues': 1, 'created_at': iso(1)}]
                    github.change(3, milestone=2)
                elif change == 'blockers':
                    github.dependencies[1] = [3]
                self.assertIsNone(co.claim(plan, ('needs-human',)))
                self.assertEqual(self.lines, [f'#{plan.item.number} worker: declined — {expected}'])
                self.assertEqual(github.writes, [])

    def test_successful_claim_and_existing_authorization_decline_add_no_decline_line(self):
        co = self.loop.coordinator
        plan = self.loop.plans()[0]

        def authorize(*args):
            self.lines.append('#1 worker: parked — Approval missing')
            return False

        self.assertIsNone(co.claim(plan, authorize=authorize))
        self.assertEqual(self.lines, ['#1 worker: parked — Approval missing'])
        self.lines.clear()
        self.assertIsNotNone(co.claim(plan))
        self.assertEqual(self.lines, [])

    def test_recovery_declines_disappeared_outcome_in_loop_and_fresh_claim(self):
        co = self.loop.coordinator
        plan = self.loop.plans()[0]
        recovery = replace(plan, state='recover')
        self.assertFalse(self.loop.recover(recovery))
        self.assertEqual(self.lines, ['#1 worker: declined — Recovery outcome no longer matches'])
        self.lines.clear()
        # A separate pass may reach a stale recovery row again.
        with self.loop.discovery_pass():
            self.loop._declines.clear()
            self.assertIsNone(co.claim(recovery, recovery=True))
        self.assertEqual(self.lines[0], '#1 worker: declined — Recovery outcome no longer matches')

    def test_lost_election_explains_decline_and_withdraws_claim(self):
        plan = self.loop.plans()[0]
        with patch('ub_agents.coordination.live_leases', return_value=[]):
            self.assertIsNone(self.loop.coordinator.claim(plan))
        self.assertEqual(self.lines, ['#1 worker: declined — Claim election lost'])
        self.assertEqual(self.loop.coordinator.history(1)[0]['state'], 'withdrawn')

    def test_replan_after_refresh_explains_missing_ready_row_without_claim(self):
        path = self.root / 'ub-agents.yaml'
        self.loop.config_path = path
        changed = replace(self.loop.config, agents=(agent(self.root, triggers=('other',)),))
        with patch('ub_agents.loop.refresh_checkout'), \
                patch('ub_agents.loop.load_config', return_value=changed):
            self.assertFalse(self.loop.tick())
        declines = [line for line in self.lines if 'declined' in line]
        self.assertEqual(declines, ['#1 worker: declined — Re-plan after refresh is no longer ready'])
        self.assertEqual(self.github.writes, [])

    def test_existing_runtime_wait_coalesces_without_second_decline_message(self):
        self.loop.config = config(self.root, agent(self.root, runtimes=(Runtime('codex', 'model', 'high'),),
                                                 command=None))
        def unavailable(*args):
            from ub_agents.errors import AgentError
            raise AgentError('Runtime became unavailable')

        with patch('ub_agents.coordination.shutil.which', return_value='/synthetic/codex'), \
                patch.object(self.loop.coordinator, 'choose_runtime', side_effect=unavailable):
            self.assertFalse(self.loop.tick())
            self.assertFalse(self.loop.tick())
        waits = [line for line in self.lines if line.startswith('#1 ')]
        self.assertEqual(waits, ['#1 worker: waiting — Runtime became unavailable'])
        self.assertEqual(self.github.writes, [])

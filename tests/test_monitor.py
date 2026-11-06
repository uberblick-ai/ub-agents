"""The browser monitor shows every launcher's trusted runs and serves only this machine."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ub_agents.config import Priority, Queue, Runtime
from ub_agents.coordination import Coordinator, Plan
from ub_agents.errors import AgentError
from ub_agents.loop import Loop
from ub_agents.monitor import monitor_background
from ub_agents.monitor.eligible import EligiblePlanning, PLAN_SECONDS
from ub_agents.monitor.local import runtime_pauses
from ub_agents.monitor.server import Snapshots, allowed, create, page
from ub_agents.monitor.snapshot import MonitorSource
from ub_agents.records import body, iso, timestamp
from tests.support import FakeGitHub, PollGitHub, agent, config, issue, pr


def lease(number, run, **changes):
    now = timestamp()
    return {"kind": "lease", "run": run, "agent": "worker", "assignment": number, "assignment_sha": None,
            "runtime": "direct", "triggers": [], "actor": "operator", "created": iso(now - 60),
            "expires": iso(now + 600), "state": "running", "attempt": 1, "started": True,
            "attempt_effect": "pending", "host": "server"} | changes


def snapshot_request(port):
    request = Mock()
    request.makefile.return_value = BytesIO(
        f"GET /snapshot.json HTTP/1.0\r\nHost: localhost:{port}\r\n\r\n".encode())
    return request


class MonitorTests(unittest.TestCase):
    def test_planner_uses_the_local_runtime_pause_and_recovers_after_it_ends(self):
        root, now = Path('/checkout'), [timestamp()]
        worker = agent(root, command=(), runtimes=(Runtime('claude', 'model', 'high'),))
        github = FakeGitHub(issue(1))
        source = MonitorSource(config(root, worker), github, Coordinator(github, None), lambda: now[0])
        with patch('ub_agents.monitor.eligible.runtime_pauses', return_value={'claude': now[0] + 100}):
            row = source.snapshot()['eligible']['rows'][0]
        self.assertEqual((row['glyph'], row['state'], list(row['pauses'])), ('◷', 'waiting', ['claude']))
        now[0] += PLAN_SECONDS
        row = source.snapshot()['eligible']['rows'][0]
        self.assertEqual((row['glyph'], row['state'], row['pauses']), ('●', 'next', {}))

    def test_inheriting_a_default_without_a_priority_label_shows_no_word(self):
        priority = Priority(('priority:high', 'priority:low'), 'priority:high')
        github = FakeGitHub(issue(1), issue(2, labels=('ready', 'priority:low')))
        github.dependencies = {1: [2]}
        source = MonitorSource(config(Path('/checkout'), queue=Queue(priority=priority)),
                               github, Coordinator(github, None), timestamp)
        [row] = source.snapshot()['eligible']['rows']
        self.assertEqual((row['item']['number'], row['priority']), (2, None))

    def test_eligible_uses_claim_order_inheritance_and_excludes_unavailable_items(self):
        priority = Priority(('priority:urgent', 'priority:high', 'priority:medium', 'priority:low'), 'priority:medium')
        cfg = config(Path('/checkout'), queue=Queue(milestones='strict-gate', priority=priority))
        github = FakeGitHub(*(issue(n, milestone=1) for n in range(1, 26)))
        github.milestones = [{'number': n, 'title': str(n), 'state': 'open'} for n in (1, 2)]
        github.change(2, labels=frozenset({'ready', 'needs-human'}))
        github.change(3, labels=frozenset({'ready', 'priority:high'}))
        github.dependencies = {3: [4]}
        github.change(24, labels=frozenset({'ready', 'priority:urgent'}))
        github.change(25, milestone=2)
        github.create_comment(1, body(lease(1, 'owned')))
        writes = list(github.writes)
        source = MonitorSource(cfg, github, Coordinator(github, 'operator'), timestamp)

        eligible = source.snapshot()['eligible']

        self.assertEqual(eligible['count'], 21)
        self.assertEqual([row['item']['number'] for row in eligible['rows']], [24, 4, *range(5, 23)])
        self.assertEqual([row['priority'] for row in eligible['rows'][:3]], ['urgent', 'high', None])
        self.assertEqual([row['state'] for row in eligible['rows'][:2]], ['next', 'ready'])
        self.assertEqual(github.writes, writes)
        with self.assertRaises(AttributeError):
            source.planning.github.create_comment(1, 'forbidden')

    def test_eligible_counts_all_items_beyond_the_terminal_snapshot_limit(self):
        github = FakeGitHub(*(issue(n) for n in range(1, 131)))
        source = MonitorSource(config(Path('/checkout')), github, Coordinator(github, None), timestamp)
        eligible = source.snapshot()['eligible']
        self.assertEqual(eligible['count'], 130)
        self.assertEqual([row['item']['number'] for row in eligible['rows']], list(range(1, 21)))

    def test_eligible_groups_agents_and_puts_ready_and_recovery_before_waits(self):
        root, now = Path('/checkout'), timestamp()
        first, second = agent(root), agent(root, name='reviewer')
        cfg = config(root, first, second, queue=Queue(priority=Priority(('priority:high',), 'priority:high')))
        github = FakeGitHub()
        planning = EligiblePlanning(cfg, github, Coordinator(github, None), lambda: now)
        planning.loop = Loop(cfg, planning.github, None, output=lambda *_: None)
        planning.loop.discovery.items = {9: issue(9, labels=('priority:high',))}
        plans = [Plan(issue(1), first, None, 'backoff', '', 2),
                 Plan(pr(2), first, None, 'recover', '', 1, priority='priority:high', priority_from_issue=9),
                 Plan(pr(2), second, None, 'ready', '', 1, priority='priority:high', priority_from_issue=9),
                 Plan(issue(3, labels=('ready', 'priority:high')), first, None, 'ready', '', 1, priority='priority:high'),
                 Plan(issue(4), first, None, 'waiting', '', 1, paused_clis=('claude',)),
                 Plan(issue(5), first, None, 'waiting', '', 1)]
        with patch.object(planning.loop, 'iter_plans', return_value=plans), \
                patch('ub_agents.monitor.eligible.runtime_pauses', return_value={'claude': now + 100800}):
            rows = planning.snapshot()['rows']
        self.assertEqual([row['item']['number'] for row in rows], [2, 3, 1, 4, 5])
        self.assertEqual([(row['glyph'], row['state']) for row in rows],
                         [('●', 'next'), ('●', 'ready'), ('◷', 'backoff'), ('◷', 'waiting'), ('◷', 'waiting')])
        self.assertEqual([a['name'] for a in rows[0]['agents']], ['worker', 'reviewer'])
        self.assertEqual(rows[0]['item']['url'], 'https://github.com/org/project/pull/2')
        self.assertEqual(rows[2]['agents'][0]['failures'], 1)
        self.assertEqual(rows[3]['pauses'], {'claude': iso(now + 100800)})

    def test_priority_is_shown_only_for_labels_with_priority_configured(self):
        root = Path('/checkout')
        worker = agent(root)
        github = FakeGitHub()
        plans = [Plan(issue(1), worker, None, 'ready', '', 1, priority='priority:medium'),
                 Plan(issue(2, labels=('ready', 'priority:medium')), worker, None, 'ready', '', 1, priority='priority:medium'),
                 Plan(issue(3), worker, None, 'ready', '', 1, priority='priority:high', priority_source=4)]
        for priority, expected in ((Priority(), [None, None, None]),
                                   (Priority(('priority:high', 'priority:medium'), 'priority:medium'), [None, 'medium', 'high'])):
            cfg = config(root, worker, queue=Queue(priority=priority))
            planning = EligiblePlanning(cfg, github, Coordinator(github, None), timestamp)
            planning.loop = Loop(cfg, planning.github, None, output=lambda *_: None)
            planning.loop.discovery.items = {4: issue(4, labels=('priority:high',))}
            with patch.object(planning.loop, 'iter_plans', return_value=plans):
                self.assertEqual([r['priority'] for r in planning.snapshot()['rows']], expected)

    def test_planning_interval_reuses_discovery_and_refreshes_after_changes(self):
        now = [timestamp()]
        github = PollGitHub(issue(1))
        source = MonitorSource(config(Path('/checkout')), github, Coordinator(github, None), lambda: now[0])
        first = source.snapshot()['eligible']
        discovery = source.planning.loop.discovery
        github.reads.clear()
        now[0] += 30
        self.assertIs(source.snapshot()['eligible'], first)
        self.assertFalse(any(name == 'timeline' for name, _ in github.reads))
        now[0] += PLAN_SECONDS
        source.snapshot()
        self.assertIs(source.planning.loop.discovery, discovery)
        self.assertFalse(any(name == 'timeline' for name, _ in github.reads))
        github.change(1, labels=frozenset())
        now[0] += PLAN_SECONDS
        self.assertEqual(source.snapshot()['eligible']['count'], 0)
        self.assertEqual(github.writes, [])

    def test_failed_planning_preserves_the_last_snapshot_until_retry_succeeds(self):
        now = [timestamp()]
        github = FakeGitHub(issue(1))
        source = MonitorSource(config(Path('/checkout')), github, Coordinator(github, None), lambda: now[0])
        snapshots = Snapshots(source, clock=lambda: now[0])
        first = snapshots.get()
        now[0] += PLAN_SECONDS
        with patch.object(source.planning.loop, 'iter_plans', side_effect=AgentError('Cannot plan queue')):
            with self.assertRaisesRegex(AgentError, 'Cannot plan queue'):
                snapshots.get()
        self.assertIs(snapshots.value, first)
        self.assertIs(source.planning.value, first['eligible'])
        self.assertIsNot(snapshots.get(), first)

    def test_local_pauses_come_only_from_the_live_launchers_fresh_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            cfg, now = config(Path(root)), timestamp()
            directory = Path(root) / '.ub-agents' / 'sessions'
            directory.mkdir(parents=True)
            data = {'version': 1, 'repository': cfg.repository, 'pid': 123, 'host': 'test',
                    'published_at': iso(now), 'runtime_pauses': {'claude': iso(now + 100800)}}
            for changes, expected in (({}, {'claude': now + 100800}), ({'ended': True}, {}),
                                       ({'pid': 456}, {}), ({'published_at': iso(now - 31)}, {})):
                (directory / 'session.json').write_text(json.dumps(data | changes))
                with patch('ub_agents.monitor.local.running_launcher', return_value={'pid': 123, 'host': 'test'}):
                    self.assertEqual(runtime_pauses(cfg, now), expected)
            with patch('ub_agents.monitor.local.running_launcher', return_value=None):
                self.assertEqual(runtime_pauses(cfg, now), {})

    def test_page_has_bounded_eligible_scrolling_empty_state_and_failure_banner(self):
        html = page().decode()
        self.assertLess(html.index('runningPanel(data.running)'), html.index('eligiblePanel(data.eligible)'))
        self.assertLess(html.index('eligiblePanel(data.eligible)'), html.index('expiredPanel(data.expired)'))
        self.assertIn('max-height: 280px; overflow-y: auto', html)
        self.assertIn('height: 56px;', html)
        self.assertIn('Nothing eligible', html)
        self.assertIn(' · showing ${eligible.rows.length}', html)
        self.assertIn('Couldn\'t refresh:', html)
        self.assertIn('--priority-urgent: #e0524a', html)
        self.assertIn('--priority-high: #c98a86', html)
        self.assertIn('--priority-low: #86a891', html)
        self.assertIn('.priority-medium { color: var(--muted); }', html)

    def test_snapshot_shows_trusted_runs_and_stopped_items_with_their_ask(self):
        github = FakeGitHub(issue(1), issue(2, labels=("needs-human",)))
        github.create_comment(1, body(lease(1, "a")))
        github.create_comment(1, body(lease(1, "b")), login="outsider")
        released = github.create_comment(2, body(lease(2, "c", state="released", result="blocked",
                                                      attempt_effect="unchanged")))
        github.create_comment(2, body({"kind": "outcome", "agent": "worker", "actor": "operator", "assignment": 2,
                                       "created": iso(timestamp()), "status": "blocked", "summary": "Unclear scope",
                                       "lease_id": released["id"], "accepted": True,
                                       "actions": ["Choose a scope"], "options": ["Small", "Large"]}))
        source = MonitorSource(config(Path("/checkout")), github, Coordinator(github, None), timestamp)

        snapshot = source.snapshot()

        self.assertEqual([run["item"]["number"] for run in snapshot["running"]], [1])
        self.assertEqual(snapshot["running"][0]["host"], "server")
        [stopped] = snapshot["needs_human"]
        self.assertEqual((stopped["item"]["number"], stopped["labels"]), (2, ["needs-human"]))
        self.assertEqual((stopped["ask"]["actions"], stopped["ask"]["options"]), (["Choose a scope"], ["Small", "Large"]))
        self.assertEqual([run["result"] for run in snapshot["recent"]], ["blocked"])

    def test_a_blocked_run_needs_a_person_only_while_its_item_is_open(self):
        github = FakeGitHub(issue(3, labels=()))
        github.create_comment(3, body(lease(3, "d", state="released", result="blocked", attempt_effect="unchanged")))
        source = MonitorSource(config(Path("/checkout")), github, Coordinator(github, None), timestamp)
        waiting = []
        for state in ("open", "closed", "open"):
            github.change(3, state=state)
            waiting.append([entry["item"]["number"] for entry in source.snapshot()["needs_human"]])
        self.assertEqual(waiting, [[3], [], [3]])

    def test_snapshot_offers_each_configured_outcome_that_stops_for_a_person(self):
        integrator = agent(Path("/checkout"), name="integrator", outcomes={
            "merged": {}, "maintainer-merge": {"add": ("needs-human",)}, "changes-requested": {"add": ("needs-changes",)}})
        github = FakeGitHub()
        source = MonitorSource(config(Path("/checkout"), integrator), github, Coordinator(github, None), timestamp)

        self.assertEqual(source.snapshot()["stop_outcomes"], ["maintainer-merge"])

    def test_server_answers_only_this_machine_and_its_own_page(self):
        self.assertTrue(allowed("127.0.0.1:4377", 4377, "/snapshot.json", "same-origin", "cors"))
        self.assertTrue(allowed("localhost:4377", 4377, "/", "cross-site", "navigate"))
        self.assertFalse(allowed("rebound.example:4377", 4377, "/snapshot.json", None, None))
        self.assertFalse(allowed("127.0.0.1:4377", 4377, "/snapshot.json", "cross-site", "cors"))
        self.assertFalse(allowed("127.0.0.1:4377", 4377, "/snapshot.json", "cross-site", "navigate"))

    def test_server_silently_finishes_replies_to_disconnected_viewers(self):
        for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            for written in (0, 1):
                with self.subTest(error=error, written=written):
                    source = Mock()
                    source.snapshot.return_value = {}
                    stdout, stderr = StringIO(), StringIO()
                    with create(source, port=0) as server:
                        request = snapshot_request(server.server_address[1])
                        request.sendall.side_effect = [None] * written + [error("viewer left")]
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            server.process_request_thread(request, ("127.0.0.1", 12345))
                    source.snapshot.assert_called_once_with()
                    self.assertEqual(request.sendall.call_count, written + 1)
                    request.close.assert_called_once_with()
                    self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))

    def test_server_reports_other_request_errors_normally(self):
        for error, stage in ((OSError("unexpected header failure"), "headers"),
                             (RuntimeError("unexpected reply failure"), "body"),
                             (ConnectionResetError("upstream disconnected"), "snapshot")):
            with self.subTest(error=error, stage=stage):
                source = Mock()
                source.snapshot.return_value = {}
                if stage == "snapshot":
                    source.snapshot.side_effect = error
                stdout, stderr = StringIO(), StringIO()
                with create(source, port=0) as server:
                    request = snapshot_request(server.server_address[1])
                    request.sendall.side_effect = [None] * (stage == "body") + [error]
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        server.process_request_thread(request, ("127.0.0.1", 12345))
                request.close.assert_called_once_with()
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("Exception occurred during processing of request", stderr.getvalue())
                self.assertIn("Traceback (most recent call last):", stderr.getvalue())
                self.assertIn(f"{type(error).__name__}: {error}", stderr.getvalue())

    def test_background_monitor_runs_in_its_own_session_until_it_answers(self):
        with tempfile.TemporaryDirectory() as root, \
                patch("ub_agents.monitor.already_serving", side_effect=[False, False, True]), \
                patch("ub_agents.monitor.subprocess.Popen") as popen, patch("ub_agents.monitor.sleep"):
            popen.return_value.poll.return_value = None
            monitor_background(Path(root) / "ub-agents.yaml", Path(root), 4400, open_page=False)
        command, options = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(command[-6:], ["monitor", "--config", f"{root}/ub-agents.yaml", "--port", "4400", "--no-open"])
        self.assertTrue(options["start_new_session"])


if __name__ == "__main__":
    unittest.main()

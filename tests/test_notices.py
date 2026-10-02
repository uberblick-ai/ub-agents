from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.coordination import Coordinator
from ub_agents.errors import GitHubError
from ub_agents.loop import Loop
from ub_agents.notices import ACTION_MARKER
from ub_agents.records import LEGACY_MARKER, MARKER, attempts, body, iso, records
from tests.support import FakeGitHub, agent, config, issue, pr, stub_refresh


class NoticeTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worker = agent(self.root)
        self.github = FakeGitHub(issue(), pr())
        self.output = []
        self.now = 1000
        self.co = Coordinator(self.github, "operator", lambda: self.now, output=self.output.append)

    def start(self, number=1, worker=None):
        plan = self.co.plan(self.github.item(number), worker or self.worker, ("needs-human",))
        lease = self.co.claim(plan, ("needs-human",))
        self.co.update(lease, state="running", started=True)
        return lease

    def notices(self, number=1):
        return [c for c in self.github.comments(number) if c["body"].startswith(ACTION_MARKER)]

    def retry(self, number=1):
        # Exercise the actual CLI reset, including its advisory notice cleanup.
        cfg = config(self.root, self.worker)
        with patch("ub_agents.cli.load_config", return_value=cfg), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["retry", "--number", str(number), "--agent", "worker",
                                   "--reason", "Decision made"]), 0)

    def test_all_record_kinds_have_one_human_line_and_collapsed_json(self):
        lease = self.start(2)
        outcome = self.co.report(lease, "blocked", "Please decide\nbetween options")
        self.co.release(lease, "blocked", outcome["summary"])
        self.retry(2)
        for record in self.co.history(2):
            with self.subTest(kind=record["kind"]):
                comment = next(c for c in self.github.comments(2) if c["id"] == record["id"])
                before, after = comment["body"].split("<details>\n", 1)
                self.assertEqual(len(before.strip().splitlines()), 2)  # hidden marker + human line
                self.assertIn("worker", before)
                self.assertIn(record["runtime"], before)
                self.assertIn("aaaaaaa", before)
                self.assertIn("<summary>Coordination record</summary>", after)
                self.assertNotIn("<details open", comment["body"])
                self.github.minimize_comment(comment)
                minimized = next(c for c in self.github.comments(2) if c["id"] == record["id"])
                self.assertEqual(records([minimized], "operator")[0], record)
        long = deepcopy(outcome)
        long["summary"] = "A" * 2000
        self.assertLess(len(body(long).splitlines()[1]), 350)
        self.assertIn("A" * 2000, body(long))

    def test_legacy_records_never_make_an_item_malformed(self):
        lease = self.start()
        valid = self.github.comments(1)[0]
        # Recreate the previous layout, then mix valid and broken old records.
        valid["body"] = (LEGACY_MARKER + "\nOld human prose\n\n```json\n" +
                         valid["body"].split("```json\n", 1)[1].split("\n```", 1)[0] + "\n```\n")
        broken = valid | {"id": 90, "body": LEGACY_MARKER + "\nunreadable old record"}
        wrong_item = valid | {"id": 91, "issue_url": "not an assignment URL"}
        self.github.store[1] = [valid, broken, wrong_item]
        self.assertEqual(self.co.history(1)[0]["run"], lease["run"])
        self.assertEqual(len(self.co.history(1)), 1)
        history, invalid = self.co.repository_history()
        self.assertEqual((len(history), invalid), (1, set()))
        broken["body"] = MARKER + "\nunreadable new record"
        self.assertEqual(self.co.repository_history()[1], {1})

    def test_release_minimizes_only_superseded_runs_of_the_same_agent(self):
        first = self.start()
        self.co.report(first, "retry", "Transient failure")
        self.co.release(first, "retry", "Transient failure")
        other = self.start(worker=agent(self.root, name="other"))
        self.co.report(other, "retry", "Other run")
        self.co.release(other, "retry", "Other run")
        latest = self.start()
        # Until release, even the earlier records stay expanded.
        self.assertFalse(any(c.get("isMinimized") for c in self.github.comments(1)))
        self.co.report(latest, "retry", "Another transient failure")
        self.co.release(latest, "retry", "Another transient failure")
        history = self.co.history(1)
        for comment, record in zip(self.github.comments(1), history):
            self.assertEqual(bool(comment.get("isMinimized")), record["run"] == first["run"])
        self.assertEqual(len(history), 6)
        self.assertEqual(len(attempts(history, "worker", self.now)), 2)
        self.assertTrue(all(w[2] == "OUTDATED" for w in self.github.writes if w[0] == "minimize"))

    def test_release_without_a_new_outcome_leaves_the_latest_outcome_expanded(self):
        first = self.start()
        self.co.report(first, "retry", "Earlier report")
        self.co.release(first, "retry", "Earlier report")
        self.co.release(self.start(), "retry", "No report")
        comments = self.github.comments(1)
        self.assertTrue(comments[0]["isMinimized"])
        self.assertFalse(comments[1].get("isMinimized", False))
        self.assertFalse(comments[2].get("isMinimized", False))

    def test_blocked_notice_has_reason_evidence_links_and_exact_retry(self):
        lease = self.start(2)
        outcome = self.co.report(lease, "blocked", "Choose a migration strategy")
        with patch.object(self.github, "candidate_evidence", wraps=self.github.candidate_evidence) as read:
            self.co.release(lease, "blocked", outcome["summary"])
        read.assert_called_once_with(2, "a" * 40)
        notices = self.notices(2)
        self.assertEqual(len(notices), 1)
        comment = notices[0]["body"]
        for expected in ("**Action needed**", outcome["summary"], "a" * 40, "APPROVED", "SUCCESS",
                         lease["url"], outcome["url"],
                         'ub-agent retry --number 2 --agent worker --reason "Human resolved the blocker"'):
            self.assertIn(expected, comment)
        self.assertEqual(records(notices, "operator"), [])
        self.assertEqual(len(self.co.history(2)), 2)
        self.co.notices.released(lease, outcome, outcome["summary"])
        self.assertEqual(len(self.notices(2)), 1)
        self.retry(2)
        self.assertTrue(self.notices(2)[0]["isMinimized"])
        self.assertEqual(self.co.plan(self.github.item(2), self.worker, ()).state, "ready")

    def parked_loop(self, handoff=None):
        worker = agent(self.root, kind="issue" if handoff else "pr",
                       outcomes={"human": {"add": ("needs-human",), "remove": ()}})
        github = FakeGitHub(issue(), pr(labels=("ready",)))
        loop = Loop(config(self.root, worker), github, "operator", output=self.output.append)

        def run(*args, **kwargs):
            number = 1 if handoff else 2
            loop.coordinator.report(loop.coordinator.history(number)[0], "success",
                                    "Maintainer must merge because docs changed", handoff=handoff, outcome="human")
            return 0
        with patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(loop.tick())
        return loop, github

    def test_stop_outcome_notice_and_later_claim_resume(self):
        loop, github = self.parked_loop()
        notice = next(c for c in github.comments(2) if c["body"].startswith(ACTION_MARKER))
        for expected in ("**Action needed**", "Maintainer must merge", "a" * 40, "APPROVED", "SUCCESS",
                         "Remove the stop label(s) `needs-human`", "`ready`", "`needs-changes`"):
            self.assertIn(expected, notice["body"])
        self.assertNotIn("ub-agent retry", notice["body"])
        self.assertEqual(loop.plans()[0].state, "parked")  # visible after all triggers were removed
        before = deepcopy(github.writes)
        loop.tick()
        loop.tick()
        self.assertEqual(github.writes, before)
        self.assertEqual(sum(": parked —" in line for line in self.output), 1)
        github.change(2, labels=frozenset({"needs-changes"}))
        # Any later winning agent claim resumes the notice, not just the original agent.
        next_agent = agent(self.root, name="reviewer")
        lease = loop.coordinator.claim(loop.coordinator.plan(github.item(2), next_agent, ("needs-human",)))
        self.assertIsNotNone(lease)
        self.assertTrue(next(c for c in github.comments(2) if c["id"] == notice["id"])["isMinimized"])
        self.assertEqual(len([c for c in github.comments(2) if c["body"].startswith(ACTION_MARKER)]), 1)

    def test_handoff_stop_notice_is_on_the_parked_pr(self):
        loop, github = self.parked_loop(handoff=2)
        self.assertFalse(any(c["body"].startswith(ACTION_MARKER) for c in github.comments(1)))
        notices = [c for c in github.comments(2) if c["body"].startswith(ACTION_MARKER)]
        self.assertEqual(len(notices), 1)
        self.assertEqual([(p.item.number, p.state) for p in loop.plans()], [(2, "parked")])
        source = loop.coordinator.history(1)
        for record in source:
            self.assertIn(record["url"], notices[0]["body"])

    def test_blocked_exit_without_report_names_host_and_log_directory(self):
        loop = Loop(config(self.root, agent(self.root, kind="issue")), self.github, "operator", output=self.output.append)
        with patch("ub_agents.loop.supervise", return_value=7), patch("ub_agents.loop.socket.gethostname", return_value="launcher-host"):
            self.assertTrue(loop.tick())
        lease, outcome = loop.coordinator.history(1)
        notice = self.notices()[0]["body"]
        for expected in ("Execution exited 7", "launcher-host", str(self.root / ".ub-agent" / "runs" / lease["run"]),
                         lease["url"], outcome["url"], "ub-agent retry --number 1 --agent worker"):
            self.assertIn(expected, notice)
        self.assertEqual((lease["result"], outcome["status"]), ("blocked", "blocked"))
        self.assertEqual(len(attempts(loop.coordinator.history(1), "worker", loop.coordinator.clock())), 1)
        for _ in range(3):
            self.assertFalse(loop.tick())
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(sum(": blocked —" in line for line in self.output), 1)

    def test_advisory_failures_do_not_change_release_transition_or_counts(self):
        for operation in ("minimize_comment", "create_comment", "candidate_evidence", "item"):
            with self.subTest(operation=operation):
                self.setUp()
                first = self.start(2)
                self.co.report(first, "retry", "Transient failure")
                self.co.release(first, "retry", "Transient failure")
                lease = self.start(2)
                outcome = self.co.report(lease, "blocked", "Need a decision")
                before_labels = self.github.item(2).labels
                before_outcome = deepcopy(outcome)
                error = GitHubError("POST", "graphql", "Synthetic advisory failure")
                with patch.object(self.github, operation, side_effect=error):
                    self.co.release(lease, "blocked", "Need a decision")
                self.assertEqual(lease["result"], "blocked")
                self.assertEqual(self.github.item(2).labels, before_labels)
                self.assertEqual(self.co.outcome(lease), before_outcome)
                self.assertEqual(len(attempts(self.co.history(2), "worker", self.now)), 1)
                self.assertTrue(any("Advisory" in line and "failed" in line for line in self.output))
                if operation == "candidate_evidence":
                    self.assertIn("Review decision: unavailable. CI for this SHA: unavailable.", self.notices(2)[0]["body"])

    def test_notice_failure_does_not_reject_an_accepted_stop_transition(self):
        original = self.github.create_comment
        worker = agent(self.root, kind="issue", outcomes={"human": {"add": ("needs-human",), "remove": ()}})
        loop = Loop(config(self.root, worker), self.github, "operator", output=self.output.append)

        def create(number, text):
            if text.startswith(ACTION_MARKER):
                raise GitHubError("POST", "comments", "Notice unavailable")
            return original(number, text)

        def run(*args, **kwargs):
            loop.coordinator.report(loop.coordinator.history(1)[0], "success", "Human merge", outcome="human")
            return 0

        with patch.object(self.github, "create_comment", side_effect=create), \
                patch("ub_agents.loop.supervise", side_effect=run):
            loop.tick()
        lease, outcome = loop.coordinator.history(1)
        self.assertEqual((lease["result"], outcome["accepted"], outcome["transition_complete"]), ("success", True, True))
        self.assertEqual(self.github.item(1).labels, frozenset({"needs-human"}))
        self.assertEqual(attempts(loop.coordinator.history(1), "worker", loop.coordinator.clock()), [])
        self.assertTrue(any("Action needed post" in line and "failed" in line for line in self.output))

    def test_unchanged_blocked_poll_is_quiet_but_reason_and_state_changes_print(self):
        lease = self.start()
        self.co.report(lease, "blocked", "Need a decision")
        self.co.release(lease, "blocked", "Need a decision")
        loop = Loop(config(self.root, agent(self.root, kind="issue")), self.github, "operator", output=self.output.append)
        for _ in range(3):
            loop.tick()
        self.assertEqual(len(self.output), 1)
        self.co.update(lease, summary="Need a different decision")
        loop.tick()
        loop.tick()
        self.assertEqual(len(self.output), 2)
        self.github.change(1, labels=frozenset({"ready", "needs-human"}))
        loop.tick()
        loop.tick()
        self.assertEqual(len(self.output), 3)
        self.assertIn("parked", self.output[-1])


if __name__ == "__main__":
    unittest.main()

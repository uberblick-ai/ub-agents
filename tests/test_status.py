from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.loop import Loop
from tests.support import FakeGitHub, agent, config, issue


class StatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.agent = agent(self.root, kind="issue")
        self.github = FakeGitHub(issue())
        self.loop = Loop(config(self.root, self.agent), self.github, "operator", output=lambda *_: None)
        self.now = 1000
        self.loop.coordinator.clock = lambda: self.now

    def claim(self):
        lease = self.loop.coordinator.claim(self.loop.plans()[0])
        self.loop.coordinator.update(lease, state="running", started=True)
        return lease

    def status(self):
        writes = list(self.github.writes)
        with patch("ub_agents.cli.load_config", return_value=self.loop.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.Loop", return_value=self.loop), \
                patch("ub_agents.cli.timestamp", return_value=self.now):
            structured, plain = io.StringIO(), io.StringIO()
            with redirect_stdout(structured):
                self.assertEqual(main(["status", "--json"]), 0)
            with redirect_stdout(plain):
                self.assertEqual(main(["status"]), 0)
        self.assertEqual(self.github.writes, writes)
        return json.loads(structured.getvalue()), plain.getvalue()

    def test_recovered_success_is_not_a_later_live_runs_report(self):
        source = self.claim()
        reported = self.loop.coordinator.report(source, "success", "Run A completed", outcome="done")
        self.now += 61
        self.assertTrue(self.loop.recover(self.loop.plans()[0]))
        history = self.loop.coordinator.history(1)
        self.assertTrue(history[1]["accepted"])
        self.assertEqual(history[2]["recovered_run"], source["run"])
        self.assertEqual(history[2]["result"], "success")
        self.assertEqual(history[1]["id"], reported["id"])
        self.github.change(1, labels=frozenset({"ready"}))

        later = self.claim()
        rows, plain = self.status()
        self.assertEqual((rows[0]["state"], rows[0]["lease"]["run"]), ("owned", later["run"]))
        self.assertIsNone(rows[0]["outcome"])
        self.assertNotIn("reported:", plain)

        # Expiry and release still select B's lease rather than A's report.
        self.now += 61
        rows, plain = self.status()
        self.assertIsNone(rows[0]["lease"])
        self.assertIsNone(rows[0]["outcome"])
        self.assertNotIn("reported:", plain)
        self.loop.coordinator.update(later, state="released", result="retry", attempt_effect="failure")
        rows, plain = self.status()
        self.assertEqual(rows[0]["result"], "retry")
        self.assertIsNone(rows[0]["outcome"])
        self.assertIn("last result: retry", plain)
        self.assertNotIn("reported:", plain)

    def test_owning_runs_report_shows_its_acceptance_then_latest_result(self):
        lease = self.claim()
        reported = self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
        rows, plain = self.status()
        self.assertEqual(rows[0]["outcome"]["id"], reported["id"])
        self.assertFalse(rows[0]["outcome"]["accepted"])
        self.assertIn("reported: success (unaccepted)", plain)

        self.loop.coordinator.accept(lease, reported)
        rows, plain = self.status()
        self.assertTrue(rows[0]["outcome"]["accepted"])
        self.assertIn("reported: success", plain)
        self.assertNotIn("(unaccepted)", plain)

        self.loop.coordinator.release(lease, "success", "Completed")
        rows, plain = self.status()
        self.assertIsNone(rows[0]["lease"])
        self.assertEqual(rows[0]["outcome"]["run"], lease["run"])
        self.assertEqual(rows[0]["result"], "success")
        self.assertIn("last result: success", plain)
        self.assertIn("reported: success", plain)

    def test_live_and_released_recovery_show_the_original_runs_report(self):
        source = self.claim()
        reported = self.loop.coordinator.report(source, "success", "Completed", outcome="done")
        self.now += 61
        recovery = self.loop.coordinator.claim(self.loop.plans()[0], recovery=True)
        rows, plain = self.status()
        self.assertEqual(rows[0]["lease"]["run"], recovery["run"])
        self.assertEqual(rows[0]["outcome"]["id"], reported["id"])
        self.assertIn("reported: success (unaccepted)", plain)

        self.loop.coordinator.accept(recovery, reported)
        self.loop.coordinator.report(recovery, "success", "Recovered completion")
        self.loop.coordinator.release(recovery, "success", "Recovered completion")
        rows, plain = self.status()
        self.assertIsNone(rows[0]["lease"])
        self.assertEqual(rows[0]["outcome"]["id"], reported["id"])
        self.assertTrue(rows[0]["outcome"]["accepted"])
        self.assertNotIn("(unaccepted)", plain)

    def test_outcome_must_match_lease_id_and_run_provenance(self):
        lease = self.claim()
        reported = self.loop.coordinator.report(lease, "success", "Completed", outcome="done")
        for changes in ({"lease_id": lease["id"] + 100}, {"lease_id": lease["id"], "run": "other-run"}):
            with self.subTest(changes=changes):
                self.loop.coordinator.update_outcome(lease, reported, **changes)
                rows, plain = self.status()
                self.assertIsNone(rows[0]["outcome"])
                self.assertNotIn("reported:", plain)

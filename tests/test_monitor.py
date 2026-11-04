"""The browser monitor shows every launcher's trusted runs and serves only this machine."""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.coordination import Coordinator
from ub_agents.monitor import monitor_background
from ub_agents.monitor.server import allowed, handler
from ub_agents.monitor.snapshot import MonitorSource
from ub_agents.records import body, iso, timestamp
from tests.support import FakeGitHub, config, issue


def lease(number, run, **changes):
    now = timestamp()
    return {"kind": "lease", "run": run, "agent": "worker", "assignment": number, "assignment_sha": None,
            "runtime": "direct", "triggers": [], "actor": "operator", "created": iso(now - 60),
            "expires": iso(now + 600), "state": "running", "attempt": 1, "started": True,
            "attempt_effect": "pending", "host": "server"} | changes


class MonitorTests(unittest.TestCase):
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

    def test_server_answers_only_this_machine_and_its_own_page(self):
        self.assertTrue(allowed("127.0.0.1:4377", 4377, "/snapshot.json", "same-origin", "cors"))
        self.assertTrue(allowed("localhost:4377", 4377, "/", "cross-site", "navigate"))
        self.assertFalse(allowed("rebound.example:4377", 4377, "/snapshot.json", None, None))
        self.assertFalse(allowed("127.0.0.1:4377", 4377, "/snapshot.json", "cross-site", "cors"))

    def test_server_ignores_a_viewer_that_left_before_the_reply(self):
        class ClosedTab:
            def makefile(self, *args):
                return BytesIO(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:4377\r\n\r\n")

            def sendall(self, data):
                raise BrokenPipeError

        handler(None, b"page")(ClosedTab(), ("127.0.0.1", 50000), SimpleNamespace(server_address=("127.0.0.1", 4377)))
        self.assertFalse(allowed("127.0.0.1:4377", 4377, "/snapshot.json", "cross-site", "navigate"))

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

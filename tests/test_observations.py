from copy import deepcopy
from contextlib import chdir
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ub_agents.coordination import Plan
from ub_agents.errors import GitHubError
from ub_agents.execution import group_members
from ub_agents.loop import Loop, _GracefulStop
from ub_agents.observations import (MAX_BYTES, MAX_OUTCOMES, MAX_PLANS,
                                   MAX_TEXT, RETAINED_SESSIONS, STALE_SECONDS,
                                   Observations, Publisher)
from ub_agents.observation_worker import prune, stale, write_snapshot
from ub_agents.records import iso, records, timestamp
from tests.support import MemoryPublisher, PollGitHub, agent, config, issue, observation_writer_command, stub_refresh


class ObservationTests(unittest.TestCase):
    def setUp(self):
        stub_refresh(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = config(self.root, agent(self.root, kind="issue"))
        self.memory = MemoryPublisher()
        self.observer = Observations(self.cfg, "operator", self.root / "ub-agents.yaml", self.memory)

    def loop(self, github, observer=True):
        return Loop(self.cfg, github, "operator", output=lambda *_: None,
                    observer=self.observer if observer else None)

    def test_observation_preserves_request_order_writes_outcomes_and_lazy_evaluation(self):
        results = []
        for observed in (False, True):
            github = PollGitHub(issue(1), issue(2), issue(3), issue(4))
            # The first reached plan parks, the second claims; later plans remain unread.
            github.timelines[1] = []
            loop = self.loop(github, observed)
            loop.coordinator.clock = lambda: 1800000000
            with patch("ub_agents.coordination.uuid.uuid4") as uuid:
                uuid.return_value.hex = "same-run"
                def finish(*args, **kwargs):
                    lease = next(r for r in loop.coordinator.history(2) if r["kind"] == "lease")
                    loop.coordinator.report(lease, "success", "Finished", outcome="done")
                    return 0
                with patch("ub_agents.loop.supervise", side_effect=finish):
                    loop.launch(once=True)
            results.append((github.reads[:], github.writes[:], deepcopy(github.items),
                            records([c for comments in github.store.values() for c in comments])))
            self.assertFalse(any(name in {"timeline", "issue_content", "comments"} and args[0] in {3, 4}
                                 for name, args in github.reads))
        self.assertEqual(results[0], results[1])
        state = self.memory.snapshots[-1]
        self.assertEqual([r["item"] for r in state["latest_pass"]["rows"]], [1, 2])
        self.assertEqual(state["latest_pass"]["state"], "partial")
        self.assertEqual(state["outcomes"][0]["acceptance"], "finalized")
        self.assertEqual(state["outcomes"][0]["runtime"], "direct")
        self.assertTrue(state["ended"])

    def test_complete_pass_includes_only_evaluated_plans_and_foreign_owner_without_paths(self):
        github = PollGitHub(issue())
        other = self.loop(github, False)
        lease = other.coordinator.claim(other.plans()[0])
        other.coordinator.update(lease, state="running", host=socket.gethostname(),
                                 log_dir="/private/other/logs", process_group=os.getpid())
        loop = self.loop(github)
        loop.launch(once=True)
        row = self.memory.snapshots[-1]["latest_pass"]["rows"][0]
        self.assertEqual(row["owner"], {"actor": "operator", "host": socket.gethostname(),
                                       "run": lease["run"], "host_reason": None})
        self.assertNotIn("/private/other/logs", json.dumps(self.memory.snapshots))
        self.assertEqual(self.memory.snapshots[-1]["latest_pass"]["state"], "complete")
        self.assertIsNone(self.memory.snapshots[-1]["assignment"])

    def test_targeted_launch_preserves_request_order_writes_and_outcomes(self):
        results = []
        for observed in (False, True):
            github = PollGitHub(issue(1), issue(2), issue(3))
            loop = self.loop(github, observed)
            loop.coordinator.clock = lambda: 1800000000
            with patch("ub_agents.coordination.uuid.uuid4") as uuid:
                uuid.return_value.hex = "same-run"
                def finish(*args, **kwargs):
                    lease = next(r for r in loop.coordinator.history(2) if r["kind"] == "lease")
                    loop.coordinator.report(lease, "success", "Finished", outcome="done")
                    return 0
                with patch("ub_agents.loop.supervise", side_effect=finish):
                    self.assertEqual(loop.launch(number=2, agent_name="worker"), 0)
            results.append((github.reads[:], github.writes[:], deepcopy(github.items),
                            records([c for comments in github.store.values() for c in comments])))
        self.assertEqual(results[0], results[1])
        state = self.memory.snapshots[-1]
        self.assertEqual([r["item"] for r in state["latest_pass"]["rows"]], [2])
        self.assertEqual(state["latest_pass"]["state"], "partial")
        self.assertEqual(state["outcomes"][0]["acceptance"], "finalized")
        self.assertTrue(state["ended"])

    def test_targeted_refusal_completes_observed_pass_without_an_assignment(self):
        github = PollGitHub(issue(labels=("ready", "needs-human")))
        loop = self.loop(github)
        self.assertEqual(loop.launch(number=1), 1)
        state = self.memory.snapshots[-1]
        self.assertEqual(state["latest_pass"]["state"], "complete")
        self.assertEqual([r["item"] for r in state["latest_pass"]["rows"]], [1])
        self.assertIsNone(state["assignment"])
        self.assertTrue(state["ended"])

    def test_interrupt_during_observer_close_still_clears_launch_selection(self):
        for error in (KeyboardInterrupt, _GracefulStop):
            with self.subTest(error=error):
                loop = self.loop(PollGitHub())
                with patch.object(loop, "_launch"), \
                        patch.object(self.observer, "close", side_effect=error), \
                        self.assertRaises(error):
                    loop.launch(number=1, agent_name="worker")
                self.assertIsNone(loop._launch_number)
                self.assertIsNone(loop._launch_agent)

    def test_unfinalized_reports_do_not_gain_blockers_from_later_plans(self):
        plan = Plan(issue(labels=("ready", "needs-human")), self.cfg.agents[0], None,
                    "parked", "Stop label", 1)
        self.observer.begin_pass()
        self.observer.assignment(plan)
        self.observer.record({"kind": "lease", "assignment": 1, "agent": "worker", "run": "run",
                              "runtime": "direct", "state": "running", "expires": iso(timestamp())})
        for fields, acceptance in (({}, "unaccepted"), ({"accepted": True}, "accepted"),
                                   ({"rejected": "Paused"}, "rejected")):
            with self.subTest(acceptance=acceptance):
                self.observer.record({"kind": "outcome", "assignment": 1, "agent": "worker", "run": "run",
                                      "created": iso(timestamp()), "status": "success", "summary": "Done",
                                      **fields})
                self.observer.plan(plan)
                row = self.memory.snapshots[-1]["outcomes"][0]
                self.assertEqual(row["acceptance"], acceptance)
                self.assertIsNone(row["human_blocker"])
                self.assertIsNone(row["blocker_observed_at"])
                self.assertEqual(row["blocker_reason"], "Transition is not finalized")

    def test_claim_start_process_exit_report_and_human_blocker_are_distinct(self):
        self.cfg = replace(self.cfg, agents=(replace(self.cfg.agents[0], outcomes={
            "done": {"add": ("needs-human",), "remove": ()}}),))
        github = PollGitHub(issue())
        loop = self.loop(github)
        def execute(*args, **kwargs):
            current = self.memory.snapshots[-1]["assignment"]
            self.assertEqual(current["process"], "starting")
            self.assertEqual(current["lease_state"], "running")
            kwargs["process_started"](12345)
            self.assertEqual(self.memory.snapshots[-1]["assignment"]["process"], "running")
            lease = loop.coordinator.history(1)[0]
            loop.coordinator.report(lease, "success", "Step done", outcome="done")
            self.assertEqual(self.memory.snapshots[-1]["outcomes"][0]["acceptance"], "unaccepted")
            return 0
        with patch("ub_agents.loop.supervise", side_effect=execute):
            loop.launch(once=True)
        assignments = [s["assignment"] for s in self.memory.snapshots if s["assignment"]]
        self.assertTrue(any(a["process"] == "claiming" and a["run"] for a in assignments))
        self.assertTrue(any(a["process"] == "exited" for a in assignments))
        row = self.memory.snapshots[-1]["outcomes"][0]
        self.assertEqual(row["acceptance"], "finalized")
        self.assertTrue(row["completed"])
        self.assertEqual(row["human_blocker"], ["needs-human"])
        self.assertTrue(any(s["outcomes"] and s["outcomes"][0]["transition_complete"]
                            and not s["outcomes"][0]["completed"] for s in self.memory.snapshots))
        loop.tick()
        github.change(1, labels=frozenset({"ready"}))
        # A fresh observed plan can clear the currently observed blocker.
        self.observer.plan(next(loop.iter_plans()))
        self.assertEqual(self.memory.snapshots[-1]["outcomes"][0]["human_blocker"], [])

    def test_rejected_report_remains_unfinalized_with_supervisor_result(self):
        github = PollGitHub(issue())
        loop = self.loop(github)
        def execute(*args, **kwargs):
            lease = loop.coordinator.history(1)[0]
            loop.coordinator.report(lease, "success", "Step done", outcome="done")
            github.change(1, labels=frozenset({"ready", "needs-human"}))
            return 0
        with patch("ub_agents.loop.supervise", side_effect=execute):
            loop.launch(once=True)
        row = self.memory.snapshots[-1]["outcomes"][0]
        self.assertEqual((row["acceptance"], row["result"], row["report_result"]),
                         ("rejected", "blocked", "success"))
        self.assertFalse(row["completed"])
        self.assertIn("paused", row["rejection"])

    def test_recovery_records_this_sessions_recovered_outcome_only(self):
        github = PollGitHub(issue(), issue(3))
        other = self.loop(github, False)
        now = timestamp()
        other.coordinator.clock = lambda: now
        lease = other.coordinator.claim(other.plans()[0])
        other.coordinator.update(lease, state="running", started=True)
        other.coordinator.report(lease, "success", "Recovered step", outcome="done")
        loop = self.loop(github)
        loop.coordinator.clock = lambda: now + 61
        with patch("ub_agents.loop.supervise") as execution:
            loop.launch(once=True)
        execution.assert_not_called()
        rows = self.memory.snapshots[-1]["outcomes"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run"], lease["run"])
        self.assertTrue(rows[0]["recovered"])
        self.assertEqual(rows[0]["acceptance"], "finalized")
        self.assertTrue(any(s["assignment"] and s["assignment"]["process"] == "recovery"
                            for s in self.memory.snapshots))

    def test_row_text_outcome_and_total_byte_limits_with_omission_counts(self):
        self.observer.begin_pass()
        for number in range(MAX_PLANS + 7):
            item = replace(issue(number + 1), title="T" * (MAX_TEXT + 3),
                           body="😀" * (MAX_TEXT + 100))
            self.observer.plan(Plan(item, self.cfg.agents[0], None, "parked", "reason", 1))
        # In-memory row counts and strings are bounded too.
        self.assertEqual(len(self.observer.state["latest_pass"]["rows"]), MAX_PLANS)
        state = self.memory.snapshots[-1]
        self.assertGreaterEqual(state["omitted"]["plans"], 7)
        self.assertGreater(state["shortened"]["characters"], 0)
        self.assertLessEqual(len(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()), MAX_BYTES)
        for row in state["latest_pass"]["rows"]:
            self.assertEqual(len(row["title"]), MAX_TEXT)
            self.assertEqual(row["description"]["omitted_characters"], 100)
        self.observer.begin_pass()
        plan = Plan(replace(issue(), body=None), self.cfg.agents[0], None, "parked", "reason", 1)
        self.observer.plan(plan)
        self.assertFalse(self.memory.snapshots[-1]["latest_pass"]["rows"][0]["description"]["available"])
        for number in range(MAX_OUTCOMES + 3):
            self.observer.assignment(plan)
            self.observer.record({"kind": "lease", "assignment": 1, "agent": "worker", "run": str(number),
                                  "runtime": "direct", "state": "claiming", "expires": iso(timestamp())})
            self.observer.record({"kind": "outcome", "assignment": 1, "agent": "worker", "run": str(number),
                                  "created": iso(timestamp()), "status": "retry", "summary": "S" * (MAX_TEXT + 4)})
        state = self.memory.snapshots[-1]
        self.assertEqual(len(state["outcomes"]), MAX_OUTCOMES)
        self.assertEqual(state["omitted"]["outcomes"], 3)
        self.assertGreater(state["shortened"]["characters"], 0)

    def test_wait_deadlines_and_rate_limit_use_existing_clock_only(self):
        loop = self.loop(PollGitHub())
        with patch.object(loop.stop_event, "wait"):
            loop._wait(loop.stop_event, 15, "runtime pause")
            loop.wait_rate_limit(GitHubError("GET", "user", "limit", rate_limited=True,
                                            reset_at=loop.coordinator.clock() + 60))
        waits = [s["activity"] for s in self.memory.snapshots if s["activity"]["state"] == "waiting"]
        self.assertEqual([r["reason"] for r in waits], ["runtime pause", "rate-limit reset"])
        self.assertTrue(all(r["until"] for r in waits))


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lines = []

    def publisher(self, command=None):
        publisher = Publisher(self.root, self.lines.append, command=command)
        def finish():
            publisher.close()
            if publisher.process:
                publisher.process.wait(timeout=5)
                publisher.diagnostics.join(timeout=5)
                self.assertFalse(publisher.diagnostics.is_alive())
        self.addCleanup(finish)
        return publisher

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.01)
        self.fail("Publisher did not finish the expected operation")

    def read(self, session):
        try:
            return json.loads((self.root / ".ub-agents" / "sessions" / f"{session}.json").read_text())
        except FileNotFoundError:
            return None

    def test_atomic_private_snapshots_without_consumer_and_two_launchers(self):
        observers = [Observations(config(self.root), "operator", self.root / "ub-agents.yaml", self.publisher())
                     for _ in range(2)]
        for observer in observers:
            state = self.wait_for(lambda: self.read(observer.state["session"]))
            self.assertEqual(state["version"], 1)
            path = self.root / ".ub-agents" / "sessions" / f"{state['session']}.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.parent.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotEqual(observers[0].state["session"], observers[1].state["session"])
        # Saturate the mailbox; it still converges on the final coalesced state.
        for number in range(500):
            observers[0].activity("waiting", iso(timestamp() + number), "x" * MAX_TEXT)
        expected = observers[0].state["activity"]
        self.wait_for(lambda: self.read(observers[0].state["session"])["activity"] == expected)
        observers[0].close()
        self.wait_for(lambda: self.read(observers[0].state["session"])["ended"])
        self.assertFalse(self.read(observers[1].state["session"])["ended"])
        observers[1].close()
        self.wait_for(lambda: self.read(observers[1].state["session"])["ended"])
        self.assertEqual(list((self.root / ".ub-agents" / "sessions").glob("*.tmp")), [])
        self.assertEqual(self.lines, [])

    def test_worker_ignores_checkout_modules_that_shadow_standard_library(self):
        (self.root / "json.py").write_text("raise RuntimeError('checkout module imported')\n")
        with chdir(self.root):
            observer = Observations(config(self.root), "operator", None, self.publisher())
        self.wait_for(lambda: self.read(observer.state["session"]))
        observer.close()
        self.wait_for(lambda: self.read(observer.state["session"])["ended"])
        self.assertEqual(self.lines, [])

    def test_non_object_session_json_does_not_stop_publication_or_pruning(self):
        directory = self.root / ".ub-agents" / "sessions"
        directory.mkdir(parents=True)
        now = timestamp()
        values = ([], "text", None, 7, True)
        for number in range(RETAINED_SESSIONS + 10):
            path = directory / f"malformed-{number}.json"
            path.write_text(json.dumps(values[number % len(values)]))
            os.utime(path, (now - STALE_SECONDS - 1, now - STALE_SECONDS - 1))
        fresh = directory / "fresh.json"
        fresh.write_text("[]")
        state = {"session": "own-session", "host": socket.gethostname(), "pid": os.getpid(),
                 "ended": False, "version": 1}
        write_snapshot(directory, state)
        self.assertEqual(len(list(directory.glob("malformed-*.json"))), RETAINED_SESSIONS)
        self.assertTrue(fresh.exists())
        write_snapshot(directory, state | {"ended": True})
        self.assertTrue(self.read(state["session"])["ended"])

    def test_publishes_near_size_limit_and_worker_start_failure_is_nonfatal(self):
        observer = Observations(config(self.root), "operator", None, self.publisher())
        observer.begin_pass()
        for number in range(MAX_PLANS + 1):
            plan = Plan(replace(issue(number + 1), body="😀" * (MAX_TEXT + 10)),
                        config(self.root).agents[0], None, "parked", "reason", 1)
            observer.plan(plan)
        observer.complete_pass()
        state = self.wait_for(lambda: self.read(observer.state["session"]))
        self.wait_for(lambda: self.read(observer.state["session"])["latest_pass"]["state"] == "complete")
        state = self.read(observer.state["session"])
        path = self.root / ".ub-agents" / "sessions" / f"{state['session']}.json"
        self.assertLessEqual(path.stat().st_size, MAX_BYTES)
        self.assertGreater(path.stat().st_size, MAX_BYTES // 2)
        self.assertGreater(state["omitted"]["plans"], 1)
        self.assertGreater(state["shortened"]["characters"], 0)
        observer.close()
        with patch("ub_agents.observations.subprocess.Popen", side_effect=OSError("Cannot start writer")):
            failed = self.publisher()
        for _ in range(10):
            failed.submit(b"{}")
        self.assertEqual(len(self.lines), 1)
        self.assertIn("Cannot start writer", self.lines[0])

    def test_clean_exit_during_an_in_progress_write_drains_final_snapshot(self):
        body = ("    from ub_agents.observation_worker import write_snapshot\n"
                "    write_snapshot(directory,state)\n"
                "    while not (Path(root)/'finish-write').exists():\n"
                "        time.sleep(0.01)\n"
                "    time.sleep(0.1)")
        publisher = self.publisher(observation_writer_command(body))
        observer = Observations(config(self.root), "operator", None, publisher)
        self.wait_for(lambda: self.read(observer.state["session"]))
        observer.close()
        (self.root / "finish-write").touch()
        self.wait_for(lambda: self.read(observer.state["session"])["ended"])
        self.assertEqual(self.lines, [])

    def test_interrupt_during_listener_start_preserves_descriptor_ownership(self):
        publisher = Publisher.__new__(Publisher)
        start = threading.Thread.start
        def interrupt(thread):
            start(thread)
            raise KeyboardInterrupt
        with patch("ub_agents.observations.threading.Thread.start", side_effect=interrupt, autospec=True), \
                self.assertRaises(KeyboardInterrupt):
            publisher.__init__(self.root, self.lines.append)
        publisher.process.wait(timeout=5)
        publisher.diagnostics.join(timeout=5)
        self.assertFalse(publisher.diagnostics.is_alive())
        self.assertEqual(self.lines, [])

    def test_heartbeat_keeps_idle_session_fresh(self):
        # The real helper with a short heartbeat, so the test need not wait 5 seconds.
        command = [sys.executable, "-c", "import ub_agents.observation_worker as worker\n"
                   "worker.HEARTBEAT_SECONDS = 0.2\nworker.main()"]
        observer = Observations(config(self.root), "operator", None, self.publisher(command))
        observer.activity("waiting", iso(timestamp() + 60), "next poll")
        state = self.wait_for(lambda: self.read(observer.state["session"]))
        self.wait_for(lambda: self.read(observer.state["session"])["published_at"] != state["published_at"])
        self.assertFalse(stale(self.read(observer.state["session"]), timestamp(), socket.gethostname()))
        observer.close()

    def test_unwritable_session_directory_does_not_change_launch(self):
        stub_refresh(self)
        local = self.root / ".ub-agents"
        local.mkdir()
        (local / "sessions").write_text("not a writable directory")
        observer = Observations(config(self.root), "operator", None, self.publisher())
        loop = Loop(config(self.root), PollGitHub(issue()), "operator", observer=observer, output=lambda *_: None)
        with patch("ub_agents.loop.supervise", return_value=0):
            loop.launch(once=True)
        self.wait_for(lambda: self.lines)
        self.assertEqual(len(self.lines), 1)
        self.assertEqual(loop.coordinator.history(1)[0]["state"], "released")
        for _ in range(20):
            observer.emit()
        self.assertEqual(len(self.lines), 1)

    def test_write_error_warns_once_and_hanging_writer_never_delays_execution_or_exit(self):
        stub_refresh(self)
        for body in ("    raise OSError('write failed')", "    time.sleep(100000)"):
            with self.subTest(writer=body):
                self.lines.clear()
                (self.root / "writer-entered").unlink(missing_ok=True)
                publisher = self.publisher(observation_writer_command(body))
                observer = Observations(config(self.root), "operator", None, publisher)
                self.wait_for(lambda: (self.root / "writer-entered").exists())
                loop = Loop(config(self.root), PollGitHub(issue()), "operator", observer=observer,
                            output=lambda *_: None)
                loop.config_path = self.root / "ub-agents.yaml"
                reloaded = replace(loop.config, poll_seconds=41)
                def finish(*args, **kwargs):
                    lease = loop.coordinator.history(1)[0]
                    loop.coordinator.report(lease, "success", "Accepted despite writer", outcome="done")
                    return 0
                started = time.monotonic()
                with patch("ub_agents.loop.supervise", side_effect=finish), \
                        patch("ub_agents.loop.refresh_checkout"), \
                        patch("ub_agents.loop.load_config", return_value=reloaded):
                    loop.launch(once=True)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(loop.coordinator.history(1)[0]["result"], "success")
                self.assertEqual(loop.config.poll_seconds, 41)
                self.assertEqual(observer.state["outcomes"][0]["acceptance"], "finalized")
                publisher.process.wait(timeout=5)
                publisher.diagnostics.join(timeout=5)
                self.assertLessEqual(len(self.lines), 1)
                if "raise" in body:
                    self.assertIn("write failed", self.lines[0])

    def test_hanging_writer_does_not_delay_recovery(self):
        stub_refresh(self)
        publisher = self.publisher(observation_writer_command("    time.sleep(100000)"))
        observer = Observations(config(self.root), "operator", None, publisher)
        self.wait_for(lambda: (self.root / "writer-entered").exists())
        github = PollGitHub(issue())
        loop = Loop(config(self.root), github, "operator", output=lambda *_: None)
        now = timestamp()
        loop.coordinator.clock = lambda: now
        lease = loop.coordinator.claim(loop.plans()[0])
        loop.coordinator.update(lease, state="running", started=True)
        loop.coordinator.report(lease, "success", "Recovered with stalled writer", outcome="done")
        loop.coordinator.clock = lambda: now + 61
        loop.observer = observer
        started = time.monotonic()
        with patch("ub_agents.loop.supervise") as execution:
            loop.launch(once=True)
        self.assertLess(time.monotonic() - started, 0.5)
        execution.assert_not_called()
        self.assertEqual(observer.state["outcomes"][0]["acceptance"], "finalized")
        self.assertTrue(observer.state["outcomes"][0]["recovered"])

    def test_killed_launcher_leaves_detectably_stale_session_and_helper_exits(self):
        script = ("import sys,time\nfrom pathlib import Path\n"
                  "from ub_agents.observations import Publisher,Observations\n"
                  "from tests.support import config\n"
                  "root=Path(sys.argv[1])\n"
                  "observer=Observations(config(root),'operator',None,Publisher(root))\n"
                  "(root/'session').write_text(observer.state['session'])\n"
                  "(root/'helper-pid').write_text(str(observer.publisher.process.pid))\n"
                  "time.sleep(60)\n")
        launcher = subprocess.Popen([sys.executable, "-c", script, str(self.root)])
        try:
            session = self.wait_for(lambda: (self.root / "session").read_text()
                                    if (self.root / "session").exists() else None)
            self.wait_for(lambda: self.read(session))
            launcher.kill()
            launcher.wait(timeout=5)
            self.assertTrue(stale(self.read(session), timestamp(), socket.gethostname()))
            self.assertFalse(self.read(session)["ended"])
        finally:
            if launcher.poll() is None:
                launcher.kill()
            launcher.wait(timeout=5)
        helper_pid = int((self.root / "helper-pid").read_text())
        self.wait_for(lambda: group_members(helper_pid) == [])

    def test_crashed_sessions_are_pruned_to_bound_and_live_sessions_survive(self):
        directory = self.root / ".ub-agents" / "sessions"
        directory.mkdir(parents=True)
        now = timestamp()
        for number in range(RETAINED_SESSIONS + 10):
            (directory / f"old-{number}.json").write_text(json.dumps({
                "ended": False, "pid": 99999999, "host": socket.gethostname(),
                "published_at": iso(now)}))
        (directory / "live.json").write_text(json.dumps({
            "ended": False, "pid": os.getpid(), "host": socket.gethostname(),
            "published_at": iso(now)}))
        (directory / "stale.json").write_text(json.dumps({
            "ended": False, "pid": os.getpid(), "host": "another-host",
            "published_at": iso(now - STALE_SECONDS - 1)}))
        prune(directory, "own-session", now, socket.gethostname())
        self.assertTrue((directory / "live.json").exists())
        self.assertEqual(len(list(directory.glob("*.json"))), RETAINED_SESSIONS + 1)

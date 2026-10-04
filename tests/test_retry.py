from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.coordination import Coordinator
from ub_agents.records import iso, records, seconds
from tests.support import PollGitHub, agent, config, edit_lease, issue, pr


class RetryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.worker = agent(self.root)
        other = agent(self.root, name="other", triggers=("other-ready",))
        self.config = replace(config(self.root, other, self.worker),
                              stop_labels=("needs-human", "paused"))
        self.github = PollGitHub(issue(97))

    def retry(self, number=97, name="worker", reason="Cause resolved", now=1000):
        output, errors = io.StringIO(), io.StringIO()
        with patch("ub_agents.cli.load_config", return_value=self.config), \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.timestamp", return_value=now), \
                patch("ub_agents.cli.Loop", side_effect=AssertionError("retry must not plan work")), \
                redirect_stdout(output), redirect_stderr(errors):
            result = main(["retry", str(number), "--reason", reason]
                          + (["--agent", name] if name is not None else []))
        return result, output.getvalue(), errors.getvalue()

    def assert_next_step(self, labels, expected, state="open"):
        for factory in (issue, pr):
            with self.subTest(kind=factory.__name__, state=state, labels=labels):
                item = replace(factory(97, labels=labels), state=state)
                self.github = PollGitHub(item)
                result, output, errors = self.retry()
                self.assertEqual(result, 0)
                self.assertEqual(errors, "")
                self.assertEqual(output.splitlines(), [
                    "Reset worker attempts on #97: https://github.com/org/project/issues/97#issuecomment-1",
                    expected])
                # Reset notice cleanup checks the new record author's role.
                self.assertEqual(self.github.reads, [
                    ("comments", (97,)), ("item", (97, None)), ("comments", (97,)), ("role", ("operator",))])
                self.assertEqual(self.github.writes, [("create", 1)])
                reset = records(self.github.store[97], "operator")[0]
                self.assertEqual((reset["kind"], reset["agent"], reset["assignment"],
                                  reset["assignment_sha"], reset["summary"]),
                                 ("reset", "worker", 97, item.head, "Cause resolved"))
                self.assertEqual(self.github.items[97], item)

    def test_closed_takes_precedence_over_labels(self):
        for labels in ((), ("ready", "needs-human", "paused"), ("needs-human",)):
            self.assert_next_step(labels, "#97 is closed.", state="closed")

    def test_stop_labels_take_precedence_over_present_triggers(self):
        self.assert_next_step(("needs-human", "ready"),
                              "#97 has stop label needs-human; it stays parked until the label is removed.")
        self.assert_next_step(("paused", "needs-human", "needs-changes"),
                              "#97 has stop labels needs-human, paused; "
                              "it stays parked until the labels are removed.")

    def test_stop_labels_and_missing_triggers_both_need_attention(self):
        self.assert_next_step(("needs-human", "other-ready"),
                              "#97 has stop label needs-human; it stays parked until the label is removed. "
                              "One of worker's trigger labels must also be added: ready, needs-changes.")
        self.assert_next_step(("paused", "needs-human"),
                              "#97 has stop labels needs-human, paused; "
                              "it stays parked until the labels are removed. "
                              "One of worker's trigger labels must also be added: ready, needs-changes.")

    def test_missing_triggers_list_only_selected_agents_labels(self):
        for labels in ((), ("other-ready", "unrelated")):
            self.assert_next_step(labels,
                                  "#97 won't run until one of worker's trigger labels is added: "
                                  "ready, needs-changes.")

    def test_present_triggers_name_each_match_and_describe_next_poll(self):
        for labels, description in ((("ready",), "label ready"),
                                    (("needs-changes", "unrelated"), "label needs-changes"),
                                    (("needs-changes", "ready"), "labels ready, needs-changes")):
            self.assert_next_step(labels, f"#97 has trigger {description}; "
                                  "a running launcher picks it up on its next poll. "
                                  "`ub-agents status` shows its progress.")

    def test_unknown_agent_and_invalid_input_refuse_without_writes(self):
        for kwargs, message in (({"name": "missing"}, "Unknown configured agent"),
                                ({"reason": " \t"}, "retry requires a positive item number and a reason")):
            with self.subTest(kwargs=kwargs):
                result, output, errors = self.retry(**kwargs)
                self.assertEqual((result, output, errors), (1, "", f"ub-agents: {message}\n"))
                self.assertEqual(self.github.reads, [])
                self.assertEqual(self.github.writes, [])

    def test_default_agent_matches_kind_and_uses_configuration_order(self):
        for factory in (issue, pr):
            kind = factory(97).kind
            other_kind = "pr" if kind == "issue" else "issue"
            wrong = agent(self.root, name="wrong", kind=other_kind)
            first = agent(self.root, name="first", kind=kind, triggers=("absent",))
            either = agent(self.root, name="either", kind="either")
            last = agent(self.root, name="last", kind=kind)
            for agents, chosen in (((wrong, first), "first"),
                                   ((wrong, first, either, last), "first"),
                                   ((wrong, either, first), "either")):
                with self.subTest(kind=kind, agents=[a.name for a in agents]):
                    self.config = config(self.root, *agents)
                    self.github = PollGitHub(factory(97))
                    create = self.github.create_comment

                    def checked_create(number, text):
                        # The selection must reach the operator before the reset write.
                        self.assertTrue(sys.stdout.getvalue().startswith(
                            f"Using agent {chosen} for {kind} #97.\n"))
                        return create(number, text)

                    with patch.object(self.github, "create_comment", side_effect=checked_create):
                        result, output, errors = self.retry(name=None)
                    self.assertEqual((result, errors), (0, ""))
                    self.assertIn(f"Reset {chosen} attempts on #97:", output)
                    reset, = records(self.github.store[97], "operator")
                    self.assertEqual((reset["agent"], reset["assignment_sha"]),
                                     (chosen, self.github.items[97].head))

    def test_default_agent_refuses_without_writes_when_no_kind_applies(self):
        for factory, configured_kind in ((issue, "pr"), (pr, "issue")):
            with self.subTest(kind=factory.__name__):
                self.config = config(self.root, agent(self.root, kind=configured_kind))
                self.github = PollGitHub(factory(97))
                result, output, errors = self.retry(name=None)
                self.assertEqual((result, output), (1, ""))
                self.assertIn(f"No configured agent applies to {factory(97).kind} #97", errors)
                self.assertEqual(self.github.writes, [])

    def test_live_lease_refuses_without_reset(self):
        coordinator = Coordinator(self.github, "operator", clock=lambda: 1000)
        lease = coordinator.claim(coordinator.plan(self.github.item(97), self.worker, ()))
        self.assertIsNotNone(lease)
        self.github.reads.clear()
        writes = list(self.github.writes)
        result, output, errors = self.retry()
        self.assertEqual((result, output, errors),
                         (1, "", "ub-agents: Cannot reset attempts while an assignment is owned\n"))
        self.assertEqual(self.github.reads, [("comments", (97,)), ("role", ("operator",))])
        self.assertEqual(self.github.writes, writes)

    def test_renewed_lease_refuses_reset_with_explicit_or_default_agent(self):
        self.config = config(self.root, self.worker)
        coordinator = Coordinator(self.github, "operator", clock=lambda: 1000)
        lease = coordinator.claim(coordinator.plan(self.github.item(97), self.worker, ()))
        original_expiry = seconds(lease["expires"])
        edit_lease(self.github, lease, expires=iso(original_expiry + 1800))
        writes = list(self.github.writes)
        for name in ("worker", None):
            with self.subTest(agent=name):
                result, output, errors = self.retry(name=name, now=original_expiry + 1)
                self.assertEqual(result, 1)
                self.assertIn("Cannot reset attempts while an assignment is owned", errors)
                self.assertNotIn("Reset", output)
                self.assertEqual(self.github.writes, writes)

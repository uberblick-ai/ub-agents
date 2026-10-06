from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.coordination import Coordinator
from ub_agents.errors import GitHubError, LostOwnership
from ub_agents.loop import Loop
from ub_agents.notices import ACTION_MARKER, action_body, notice_reason
from ub_agents.records import attempts, body, iso, payload, records, seconds
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
        outcome = self.co.report(lease, "blocked", "Please decide\nbetween options", action="Maintainer: choose A or B; recommend A.")
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

    def test_release_minimizes_only_superseded_runs_of_the_same_agent(self):
        first = self.start()
        self.co.report(first, "retry", "Transient failure")
        self.co.release(first, "retry", "Transient failure")
        other = self.start(worker=agent(self.root, name="other"))
        self.co.report(other, "retry", "Other run")
        self.co.release(other, "retry", "Other run")
        latest = self.start()
        # Until release, even the earlier records stay expanded.
        self.assertFalse(self.github.minimized_ids)
        self.co.report(latest, "retry", "Another transient failure")
        self.co.release(latest, "retry", "Another transient failure")
        history = self.co.history(1)
        for comment, record in zip(self.github.comments(1), history):
            self.assertEqual(comment["id"] in self.github.minimized_ids, record["run"] == first["run"])
        self.assertEqual(len(history), 6)
        self.assertEqual(len(attempts(history, "worker", self.now)), 2)
        self.assertTrue(all(w[2] == "OUTDATED" for w in self.github.writes if w[0] == "minimize"))

    def test_release_without_a_new_outcome_leaves_the_latest_outcome_expanded(self):
        first = self.start()
        self.co.report(first, "retry", "Earlier report")
        self.co.release(first, "retry", "Earlier report")
        self.co.release(self.start(), "retry", "No report")
        comments = self.github.comments(1)
        self.assertIn(comments[0]["id"], self.github.minimized_ids)
        self.assertNotIn(comments[1]["id"], self.github.minimized_ids)
        self.assertNotIn(comments[2]["id"], self.github.minimized_ids)

    def test_repeated_releases_skip_already_minimized_rest_comments_after_restart(self):
        for _ in range(4):
            # Each run uses a fresh coordinator, so this cannot rely on a session cache.
            self.co = Coordinator(self.github, "operator", lambda: self.now, output=self.output.append)
            lease = self.start()
            self.co.report(lease, "success", "Done", outcome="done")
            self.co.accept(lease, self.co.outcome(lease))
            self.co.release(lease, "success", "Done")
        comments = self.github.comments(1)
        self.assertTrue(all("isMinimized" not in comment for comment in comments))
        expected = [comment["id"] for comment in comments[:-2]]
        minimized = [write[1] for write in self.github.writes if write[0] == "minimize"]
        self.assertEqual(minimized, expected)
        self.assertEqual(self.github.minimized_ids, set(expected))
        self.assertEqual(len(self.co.history(1)), 8)

    def test_retries_and_later_claims_minimize_a_notice_only_once(self):
        lease = self.start()
        self.co.report(lease, "blocked", "Decision needed", action="Maintainer: choose A or B; recommend A.")
        self.co.release(lease, "blocked", "Decision needed")
        notice = self.notices()[0]
        self.retry()
        self.retry()
        self.co = Coordinator(self.github, "operator", lambda: self.now, output=self.output.append)
        self.start()
        self.co.notices.resumed(1)
        self.assertNotIn("isMinimized", self.notices()[0])
        self.assertEqual([write[1] for write in self.github.writes if write[0] == "minimize"],
                         [notice["id"]])

    def test_null_body_does_not_prevent_later_notice_minimization(self):
        self.github.create_comment(1, "Unrelated comment")
        self.github.store[1][0]["body"] = None
        lease = self.start()
        self.co.report(lease, "blocked", "Decision needed", action="Maintainer: choose A or B; recommend A.")
        self.co.release(lease, "blocked", "Decision needed")
        notice = self.github.comments(1)[-1]
        self.retry()
        self.assertIn(notice["id"], self.github.minimized_ids)
        self.assertEqual(self.output, [])

    def test_release_never_minimizes_its_own_claim_after_a_later_contender_withdraws(self):
        plan = self.co.plan(self.github.item(1), self.worker, ())
        self.github.claim_barrier = threading.Barrier(2)
        self.github.claim_read_barrier = threading.Barrier(2)
        other = Coordinator(self.github, "operator", lambda: self.now, output=self.output.append)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda co: co.claim(plan), (self.co, other)))
        lease = next(c for c in claims if c is not None)
        self.co.report(lease, "retry", "Transient failure")
        self.co.release(lease, "retry", "Transient failure")
        comments = self.github.comments(1)
        self.assertEqual([r["state"] for r in self.co.history(1) if r["kind"] == "lease"],
                         ["released", "withdrawn"])
        loser = next(r for r in self.co.history(1) if r.get("state") == "withdrawn")
        self.assertEqual(self.github.minimized_ids, {loser["id"]})

    def test_handoff_minimizes_other_agents_old_candidates_but_keeps_current_records(self):
        older = []
        for name in ("reviewer", "integrator"):
            lease = self.start(2, agent(self.root, name=name))
            outcome = self.co.report(lease, "retry", "Earlier candidate")
            self.co.release(lease, "retry", outcome["summary"])
            older.extend((lease["id"], outcome["id"]))
        # Trusted records from another launcher account are folded too.
        self.github.store[2][0]["user"]["login"] = "maintainer"
        self.github.store[2][1]["user"]["login"] = "maintainer"
        self.github.change(2, head="b" * 40)
        current = self.start(2, agent(self.root, name="current-reviewer"))
        source = self.start()
        outcome = self.co.report(source, "success", "New candidate", handoff=2, outcome="done")
        # The handoff item's copied outcome, rather than its source comment on
        # the issue, sets the ordering boundary for candidate cleanup.
        between = self.github.create_comment(2, body(payload(lease) | {"run": "before-copy"}))
        older.append(between["id"])
        self.co.accept(source, outcome)
        history = self.co.history(2)
        counts = {name: attempts(history, name, self.now) for name in ("reviewer", "integrator")}
        self.assertFalse(self.github.minimized_ids)
        self.co.release(source, "success", outcome["summary"])
        self.assertEqual(self.github.minimized_ids, set(older))
        self.assertNotIn(current["id"], self.github.minimized_ids)
        self.assertEqual(self.co.history(2), history)
        self.assertEqual({name: attempts(self.co.history(2), name, self.now) for name in counts}, counts)

    def test_handoff_preserves_its_run_and_records_written_after_its_outcome(self):
        first = self.start(2, agent(self.root, name="reviewer"))
        first_outcome = self.co.report(first, "retry", "Earlier candidate")
        self.co.release(first, "retry", "Earlier candidate")
        source = self.start(2)
        self.github.change(2, head="b" * 40)
        outcome = self.co.report(source, "success", "New candidate", handoff=2, outcome="done")
        self.co.accept(source, outcome)
        late = self.github.create_comment(2, body(payload(first) | {"run": "late-record"}))
        self.co.release(source, "success", outcome["summary"])
        self.assertEqual(self.github.minimized_ids, {first["id"], first_outcome["id"]})
        self.assertNotIn(source["id"], self.github.minimized_ids)
        self.assertNotIn(outcome["id"], self.github.minimized_ids)
        self.assertNotIn(late["id"], self.github.minimized_ids)

    def test_candidate_cleanup_preserves_parking_evidence_and_ignores_untrusted_comments(self):
        parked = self.start(2, agent(self.root, name="integrator"))
        parked_outcome = self.co.report(parked, "blocked", "Human decision needed", action="Maintainer: choose A or B; recommend A.")
        self.co.release(parked, "blocked", parked_outcome["summary"])
        latest_notice = self.notices(2)[-1]
        no_sha = self.github.create_comment(2, body(payload(parked) | {
            "run": "no-sha", "agent": "no-sha-agent", "assignment_sha": None}))
        approval = self.github.create_comment(2, body(payload(parked) | {
            "run": "approval-withdrawal", "state": "withdrawn", "started": False,
            "attempt_effect": "unchanged", "summary": "Outside feedback needs approval"}))
        untrusted = self.github.create_comment(2, body(payload(parked) | {"run": "untrusted"}), login="outsider")
        current = self.github.create_comment(2, body(payload(parked) | {
            "run": "displayed-current", "candidate_sha": "b" * 40}))
        old = self.github.create_comment(2, body(payload(parked) | {"run": "outdated"}))
        source = self.start()
        self.github.change(2, head="b" * 40)
        outcome = self.co.report(source, "success", "New candidate", handoff=2, outcome="done")
        self.co.accept(source, outcome)
        self.co.release(source, "success", outcome["summary"])
        self.assertEqual(self.github.minimized_ids, {old["id"]})
        for comment_id in (parked["id"], parked_outcome["id"], latest_notice["id"],
                           no_sha["id"], approval["id"], untrusted["id"], current["id"]):
            self.assertNotIn(comment_id, self.github.minimized_ids)

    def test_unaccepted_or_unsuccessful_handoff_does_not_supersede_other_agents(self):
        for accepted, result in ((False, "success"), (True, "blocked")):
            with self.subTest(accepted=accepted, result=result):
                self.setUp()
                first = self.start(2, agent(self.root, name="reviewer"))
                self.co.report(first, "retry", "Earlier candidate")
                self.co.release(first, "retry", "Earlier candidate")
                self.github.change(2, head="b" * 40)
                source = self.start()
                outcome = self.co.report(source, "success", "New candidate", handoff=2, outcome="done")
                if accepted:
                    self.co.accept(source, outcome)
                self.co.release(source, result, outcome["summary"])
                self.assertFalse(self.github.minimized_ids)

    def test_candidate_minimization_failure_is_advisory(self):
        first = self.start(2, agent(self.root, name="reviewer"))
        self.co.report(first, "retry", "Earlier candidate")
        self.co.release(first, "retry", "Earlier candidate")
        self.github.change(2, head="b" * 40)
        source = self.start()
        outcome = self.co.report(source, "success", "New candidate", handoff=2, outcome="done")
        self.co.accept(source, outcome)
        history = self.co.history(2)
        with patch.object(self.github, "minimize_comment", side_effect=GitHubError("POST", "graphql", "Unavailable")):
            self.co.release(source, "success", outcome["summary"])
        self.assertEqual(source["result"], "success")
        self.assertEqual(self.co.history(2), history)
        self.assertFalse(self.github.minimized_ids)
        self.assertTrue(any("Advisory minimize comment" in line for line in self.output))

    def test_parking_handoff_preserves_new_notice_links_and_folds_previous_notice_evidence(self):
        old = self.start(2, agent(self.root, name="integrator"))
        old_outcome = self.co.report(old, "blocked", "Previous decision", action="Maintainer: choose A or B; recommend A.")
        self.co.release(old, "blocked", old_outcome["summary"])
        worker = agent(self.root, outcomes={"human": {"add": ("needs-human",), "remove": ()}})
        source = self.start(worker=worker)
        self.github.change(2, head="b" * 40)
        outcome = self.co.report(source, "success", "Human must merge", handoff=2, outcome="human", action="Maintainer: choose A or B; recommend A.")
        self.co.update_outcome(source, outcome, transition_complete=True)
        self.co.accept(source, outcome)
        self.co.release(source, "success", outcome["summary"])
        self.assertEqual(self.github.minimized_ids, {old["id"], old_outcome["id"]})
        latest = self.notices(2)[-1]
        self.assertIn(source["url"], latest["body"])
        self.assertIn(outcome["url"], latest["body"])
        self.assertNotIn(latest["id"], self.github.minimized_ids)
        self.assertNotIn(self.co.history(2)[-1]["id"], self.github.minimized_ids)

    def test_recovered_handoff_minimizes_old_candidates(self):
        old = self.start(2, agent(self.root, name="reviewer"))
        old_outcome = self.co.report(old, "retry", "Earlier candidate")
        self.co.release(old, "retry", old_outcome["summary"])
        worker = agent(self.root, kind="issue")
        source = self.start(worker=worker)
        self.github.change(2, head="b" * 40)
        outcome = self.co.report(source, "success", "Recovered candidate", handoff=2, outcome="done")
        self.now += 61
        loop = Loop(config(self.root, worker), self.github, "operator", output=self.output.append)
        loop.coordinator.clock = lambda: self.now
        with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
            self.assertTrue(loop.recover(loop.coordinator.plan(self.github.item(1), worker, ("needs-human",))))
        self.assertEqual({c["id"] for c in self.github.comments(2) if c["id"] in self.github.minimized_ids},
                         {old["id"], old_outcome["id"]})
        copied = next(r for r in loop.coordinator.history(2) if r["run"] == outcome["run"])
        self.assertTrue(copied["accepted"])
        self.assertNotIn(copied["id"], self.github.minimized_ids)

    def test_election_minimization_failure_does_not_change_the_winner(self):
        plan = self.co.plan(self.github.item(1), self.worker, ())
        self.github.claim_barrier = threading.Barrier(2)
        self.github.claim_read_barrier = threading.Barrier(2)
        other = Coordinator(self.github, "operator", lambda: self.now, output=self.output.append)
        with patch.object(self.github, "minimize_comment", side_effect=GitHubError("POST", "graphql", "Unavailable")), \
                ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda co: co.claim(plan), (self.co, other)))
        winner = next(c for c in claims if c is not None)
        self.assertEqual(sum(c is not None for c in claims), 1)
        self.co.assert_owned(winner)
        self.assertEqual([r["state"] for r in self.co.history(1)], ["claiming", "withdrawn"])
        self.assertFalse(self.github.minimized_ids)
        self.assertTrue(any("Advisory minimize comment" in line for line in self.output))

    def test_superseded_handoff_copies_are_minimized_but_remain_readable(self):
        for summary in ("First candidate", "Revised candidate"):
            lease = self.start()
            outcome = self.co.report(lease, "success", summary, handoff=2, outcome="done")
            self.co.accept(lease, outcome)
            self.co.release(lease, "success", summary)
        copies = self.github.comments(2)
        self.assertEqual(len(copies), 2)
        self.assertIn(copies[0]["id"], self.github.minimized_ids)
        self.assertNotIn(copies[1]["id"], self.github.minimized_ids)
        self.assertEqual([r["summary"] for r in self.co.history(2)], ["First candidate", "Revised candidate"])

    def test_blocked_notice_has_reason_evidence_links_and_exact_retry(self):
        lease = self.start(2)
        outcome = self.co.report(lease, "blocked", "Choose a migration strategy", action="Maintainer: choose A or B; recommend A.")
        with patch.object(self.github, "candidate_evidence", wraps=self.github.candidate_evidence) as read:
            self.co.release(lease, "blocked", outcome["summary"])
        read.assert_called_once_with(2, "a" * 40)
        notices = self.notices(2)
        self.assertEqual(len(notices), 1)
        comment = notices[0]["body"]
        visible, details = comment.split('<details>', 1)
        self.assertIn(f'**Action needed**\n\n**{outcome["action"]}**\n\n', visible)
        self.assertNotIn(outcome['summary'], visible)
        self.assertIn(f'{outcome["summary"]}\n\nCandidate:', details)
        for expected in ("**Action needed**", outcome["summary"], "a" * 40, "APPROVED", "SUCCESS",
                         lease["url"], outcome["url"],
                         'ub-agents retry 2 --agent worker --reason "Human resolved the blocker"'):
            self.assertIn(expected, comment)
        self.assertEqual(records(notices, "operator"), [])
        self.assertEqual(len(self.co.history(2)), 2)
        self.co.notices.released(lease, outcome, outcome["summary"])
        self.assertEqual(len(self.notices(2)), 1)
        self.retry(2)
        self.assertIn(self.notices(2)[0]["id"], self.github.minimized_ids)
        self.assertEqual(self.co.plan(self.github.item(2), self.worker, ()).state, "ready")

    def test_full_markdown_reasoning_is_preserved_and_all_evidence_is_collapsed(self):
        summary = ('## Storage reasoning\n\nLocal avoids uploads; cloud enables sharing.\n\n'
                   '## Rollout reasoning\n\nStaging limits migration risk.\n\n'
                   '- [Review](https://example.com/review)\n- CI diagnostics\n\n'
                   '<details><summary>More diagnostics</summary>\n\nNested details\n\n</details>\n\n'
                   + 'Supporting evidence. ' * 100)
        asks = ['Owner: choose local or cloud storage; recommend local for privacy.',
                'Owner: choose immediate or staged rollout; recommend staged to limit migration risk.']
        lease = self.start(2)
        outcome = self.co.report(lease, 'blocked', summary, action=asks)
        self.co.release(lease, 'blocked', summary)
        comment = self.notices(2)[0]['body']
        visible, details = comment.split('<details>', 1)
        self.assertIn('\n'.join(f'- **{ask}**' for ask in asks), visible)
        self.assertIn(summary.strip(), details)
        for evidence in ('Candidate:', 'Review decision:', 'CI for this SHA:', lease['url'], outcome['url']):
            self.assertNotIn(evidence, visible)
            self.assertIn(evidence, details)
        for resume in ('Then resume worker:', 'ub-agents retry', 'same role (`worker`)', 'different role'):
            self.assertIn(resume, visible)
            self.assertNotIn(resume, details)
        self.assertNotIn('<details open', comment)

    def test_options_notice_orders_reason_asks_choices_resume_and_folded_evidence(self):
        lease = self.start(2)
        summary = 'Local CI is red on `abcdef`. One test reads the launcher environment.\n\nFull diagnostics.'
        options = ['Maintainer: run CI outside a supervised session: `mise run ci abcdef`',
                   'Maintainer: merge a fix that clears `UB_AGENTS_READ_CONFIG`.']
        ask = 'Owner: authorize the *storage* change & review [the PR].'
        outcome = self.co.report(lease, 'blocked', summary, action=ask, option=options)
        self.co.release(lease, 'blocked', summary)
        notice = self.notices(2)[0]['body']
        visible, details = notice.split('<details>', 1)
        ordered = ['**Action needed**', 'Local CI is red on `abcdef`.',
                   '**Owner: authorize the \\*storage\\* change \\& review \\[the PR\\].**',
                   'To unblock, do one of:',
                   '1. Maintainer: run CI outside a supervised session (recommended)',
                   '   ```sh\n   mise run ci abcdef\n   ```',
                   '2. Maintainer: merge a fix that clears `UB_AGENTS_READ_CONFIG`.',
                   'Then resume worker:', '```sh\nub-agents retry 2', 'Restore a matching trigger']
        positions = [visible.index(text) for text in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(notice.count('(recommended)'), 1)
        self.assertNotIn('One test reads', visible)
        self.assertIn('<summary>Reasoning and evidence</summary>', details)
        for evidence in (summary, 'Candidate:', 'Review decision:', 'CI for this SHA:', lease['url'], outcome['url']):
            self.assertIn(evidence, details)
        self.assertNotIn('ub-agents retry', details)

    def test_single_backtick_code_survives_and_other_markdown_is_escaped(self):
        ask = 'Maintainer: run `check --flag` and review **bold** [link](https://x) <b>HTML</b> ~~strike~~ ``double``.'
        notice = action_body('marker', [ask], 'Evidence')
        visible = notice.split('<details>', 1)[0]
        self.assertIn('`check --flag`', visible)
        for escaped in ('\\*\\*bold\\*\\*', '\\[link\\]\\(https://x\\)', '\\<b\\>', '\\~\\~strike\\~\\~', '\\`\\`double\\`\\`'):
            self.assertIn(escaped, visible)
        self.assertEqual(notice_reason('CI is red.\nEvidence follows.'), 'CI is red.')
        reason = notice_reason('A long blocker ' * 50)
        self.assertEqual(len(reason), 300)
        self.assertTrue(reason.endswith('…'))
        self.assertNotIn('\n', reason)

    def test_legacy_record_preserves_summary_without_inventing_decisions(self):
        lease = self.start()
        self.co.update(lease, action_required=None)
        summary = '## First choice\n\nA or B?\n\n## Second choice\n\nC or D?'
        outcome = self.co.report(lease, 'blocked', summary, agent_report=False)
        self.co.release(lease, 'blocked', summary)
        visible, details = self.notices()[0]['body'].split('<details>', 1)
        self.assertIn('Maintainer: review the blocker details and decide the next step.', visible)
        self.assertNotIn('First choice', visible)
        self.assertIn(summary, details)
        self.assertNotIn('action', records(self.github.comments(1))[1])

    def parked_loop(self, handoff=None, action="Maintainer: choose A or B; recommend A.", option=None):
        worker = agent(self.root, kind="issue" if handoff else "pr",
                       outcomes={"human": {"add": ("needs-human",), "remove": ()}})
        github = FakeGitHub(issue(), pr(labels=("ready",)))
        loop = Loop(config(self.root, worker), github, "operator", output=self.output.append)

        def run(*args, **kwargs):
            number = 1 if handoff else 2
            loop.coordinator.report(loop.coordinator.history(number)[0], "success",
                                    "Maintainer must merge because docs changed", handoff=handoff, outcome="human", action=action, option=option)
            return 0
        with patch("ub_agents.loop.supervise", side_effect=run):
            self.assertTrue(loop.tick())
        return loop, github

    def test_stop_outcome_notice_and_later_claim_resume(self):
        loop, github = self.parked_loop()
        notice = next(c for c in github.comments(2) if c["body"].startswith(ACTION_MARKER))
        visible, details = notice['body'].split('<details>', 1)
        self.assertIn('**Action needed**\n\n**Maintainer: choose A or B; recommend A.**\n\n', visible)
        self.assertIn('Maintainer must merge because docs changed\n\nCandidate:', details)
        for expected in ("**Action needed**", "Maintainer must merge", "a" * 40, "APPROVED", "SUCCESS",
                         "Remove the stop label(s) `needs-human`", "`ready`", "`needs-changes`"):
            self.assertIn(expected, notice["body"])
        self.assertNotIn("ub-agents retry", notice["body"])
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
        self.assertIn(notice["id"], github.minimized_ids)
        self.assertEqual(len([c for c in github.comments(2) if c["body"].startswith(ACTION_MARKER)]), 1)

    def test_handoff_stop_notice_is_on_the_parked_pr(self):
        loop, github = self.parked_loop(handoff=2)
        self.assertFalse(any(c["body"].startswith(ACTION_MARKER) for c in github.comments(1)))
        notices = [c for c in github.comments(2) if c["body"].startswith(ACTION_MARKER)]
        self.assertEqual(len(notices), 1)
        outcome = next(r for r in loop.coordinator.history(1) if r['kind'] == 'outcome')
        self.assertIn(f'**Action needed**\n\n**{outcome["action"]}**\n\n', notices[0]['body'])
        copied = next(r for r in loop.coordinator.history(2) if r['kind'] == 'outcome')
        self.assertEqual(copied['action'], outcome['action'])
        self.assertEqual([(p.item.number, p.state) for p in loop.plans()], [(2, "parked")])
        source = loop.coordinator.history(1)
        for record in source:
            self.assertIn(record["url"], notices[0]["body"])

    def test_handoff_keeps_every_ask_in_the_record_copy_and_visible_notice(self):
        asks = ['Owner: choose A or B; recommend A.', 'Maintainer: merge after the owner chooses A.']
        loop, github = self.parked_loop(handoff=2, action=asks)
        copied = loop.coordinator.history(2)[0]
        self.assertEqual(copied['action'], asks[0])
        self.assertEqual(copied['actions'], asks)
        notice = next(c['body'] for c in github.comments(2) if c['body'].startswith(ACTION_MARKER))
        visible, details = notice.split('<details>', 1)
        for ask in asks:
            self.assertIn(f'- **{ask}**', visible)
        self.assertIn('same role (`worker`)', visible)
        self.assertIn('different role', visible)

    def test_options_handoff_keeps_choices_and_stop_label_resume_visible(self):
        options = ['Maintainer: merge after CI: `mise run ci SHA`', 'Maintainer: request a fix.']
        loop, github = self.parked_loop(handoff=2, action=None, option=options)
        copied = loop.coordinator.history(2)[0]
        self.assertEqual(copied['options'], options)
        self.assertEqual(copied['action'], options[0])
        notice = next(c['body'] for c in github.comments(2) if c['body'].startswith(ACTION_MARKER))
        visible, details = notice.split('<details>', 1)
        for text in ('To unblock, do one of:', '1. Maintainer: merge after CI (recommended)',
                     '   mise run ci SHA', '2. Maintainer: request a fix.', 'Then resume worker:',
                     'Remove the stop label(s) `needs-human`', '`ready`', '`needs-changes`'):
            self.assertIn(text, visible)
            self.assertNotIn(text, details)
        self.assertNotIn('ub-agents retry', notice)

    def test_stop_notice_reads_propagate_lost_ownership(self):
        loop, github = self.parked_loop(handoff=2)
        source = loop.coordinator.history(1)
        lease = next(r for r in source if r["kind"] == "lease")
        outcome = next(r for r in source if r["kind"] == "outcome")
        writes = list(github.writes)
        with patch.object(github, "comments", side_effect=LostOwnership("Lease expired while waiting")):
            with self.assertRaisesRegex(LostOwnership, "Lease expired"):
                loop.coordinator.notices.advisory("released run comments", lambda:
                    loop.coordinator.notices.released(lease, outcome, outcome["summary"]))
        self.assertEqual(github.writes, writes)
        self.assertFalse(any("Advisory" in line for line in self.output))

    def test_expiry_recovery_posts_one_notice_for_the_original_stop_outcome(self):
        worker = agent(self.root, kind="pr", outcomes={"human": {"add": ("needs-human",), "remove": ()}})
        loop = Loop(config(self.root, worker), self.github, "operator", output=self.output.append)
        loop.coordinator.clock = lambda: self.now
        plan = loop.plans()[0]
        lease = loop.coordinator.claim(plan, loop.config.stop_labels)
        summary = '## Human must merge\n\n- Migration reasoning\n- Review evidence'
        loop.coordinator.report(lease, "success", summary, outcome="human", action="Maintainer: choose A or B; recommend A.")
        self.now += 61
        changed = agent(self.root, kind="pr", triggers=("different",),
                        outcomes={"other": {"add": ("wrong",), "remove": ()}})
        loop = Loop(config(self.root, changed), self.github, "operator", output=self.output.append)
        loop.coordinator.clock = lambda: self.now
        with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
            self.assertTrue(loop.tick())
        notice = self.notices(2)[0]["body"]
        self.assertIn(f"Recovered {lease['run']}:\n\n{summary}", notice)
        self.assertIn("Human must merge", notice)
        self.assertIn("Remove the stop label(s) `needs-human`", notice)
        self.assertIn("`ready`, `needs-changes`", notice)
        self.assertNotIn("`different`", notice)
        self.assertIn("a" * 40, notice)
        loop.tick()
        self.assertEqual(len(self.notices(2)), 1)

    def test_recovered_handoff_copy_resolves_resume_context_from_the_issue(self):
        worker = agent(self.root, kind="issue", outcomes={"human": {"add": ("needs-human",), "remove": ()}})
        loop = Loop(config(self.root, worker), self.github, "operator", output=self.output.append)
        loop.coordinator.clock = lambda: self.now
        lease = loop.coordinator.claim(loop.plans()[0], loop.config.stop_labels)
        loop.coordinator.report(lease, "success", "Human must merge", handoff=2, outcome="human", action="Maintainer: choose A or B; recommend A.")
        self.now += 61
        changed = agent(self.root, kind="issue", triggers=("different",),
                        outcomes={"other": {"add": ("wrong",), "remove": ()}})
        restarted = Loop(config(self.root, changed), self.github, "operator", output=self.output.append)
        restarted.coordinator.clock = lambda: self.now
        with patch("ub_agents.loop.supervise", side_effect=AssertionError("must not execute")):
            self.assertTrue(restarted.tick())
        copied, = restarted.coordinator.history(2)
        self.assertTrue(copied["accepted"])
        self.assertTrue(copied["transition_complete"])
        self.assertEqual(copied["assignment"], 1)
        self.assertEqual(restarted.validate_report(copied)["triggers"], ["ready", "needs-changes"])
        recovery = next(r for r in restarted.coordinator.history(1)
                        if r["kind"] == "lease" and r.get("mode") == "recovery")
        self.assertNotIn("outcomes", recovery)
        # A later notice retry can receive the copy on the PR and a recovery
        # lease with no declarations; the original issue still owns the context.
        restarted.coordinator.notices.released(recovery, None, copied["summary"], parking_outcome=copied)
        notices = self.notices(2)
        self.assertEqual(len(notices), 1)
        self.assertIn("Remove the stop label(s) `needs-human`", notices[0]["body"])
        self.assertIn("`ready`, `needs-changes`", notices[0]["body"])
        self.assertNotIn("`different`", notices[0]["body"])
        self.assertFalse(self.notices(1))
        self.assertFalse(any("Advisory" in line for line in self.output))

    def test_resume_minimize_failure_cannot_change_a_claim_or_retry_reset(self):
        lease = self.start()
        self.co.report(lease, "blocked", "Decision needed", action="Maintainer: choose A or B; recommend A.")
        self.co.release(lease, "blocked", "Decision needed")
        error = GitHubError("POST", "graphql", "No minimization permission")
        with patch.object(self.github, "minimize_comment", side_effect=error):
            self.retry()
        self.assertEqual(self.co.history(1)[-1]["kind"], "reset")
        self.assertEqual(attempts(self.co.history(1), "worker", self.now), [])
        with patch.object(self.github, "minimize_comment", side_effect=error):
            next_lease = self.start()
        self.assertEqual(next_lease["state"], "running")
        self.assertTrue(any("minimize comment" in line and "failed" in line for line in self.output))
        self.assertNotIn(self.notices()[0]["id"], self.github.minimized_ids)

    def test_delayed_notice_does_not_park_an_item_again_after_a_reset(self):
        lease = self.start()
        outcome = self.co.report(lease, "blocked", "Decision needed", action="Maintainer: choose A or B; recommend A.")
        with patch.object(self.github, "create_comment", side_effect=GitHubError("POST", "comments", "Notice failed")):
            self.co.release(lease, "blocked", "Decision needed")
        self.assertEqual(self.notices(), [])
        self.retry()
        self.co.notices.released(lease, outcome, "Decision needed")
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.co.plan(self.github.item(1), self.worker, ()).state, "ready")

    def test_exhausted_exit_without_report_names_host_and_log_directory(self):
        for code in (0, 7):
            with self.subTest(code=code):
                github = FakeGitHub(issue())
                worker = agent(self.root, kind="issue", backoff_seconds=10, max_backoff_seconds=100)
                cfg = config(self.root, worker)
                for attempt in range(1, worker.max_attempts + 1):
                    # Restarting between attempts must retain the count and backoff.
                    loop = Loop(cfg, github, "operator", output=self.output.append)
                    loop.coordinator.clock = lambda: self.now
                    with patch("ub_agents.loop.supervise", return_value=code), \
                            patch("ub_agents.loop.socket.gethostname", return_value="launcher-host"):
                        self.assertTrue(loop.tick())
                    history = loop.coordinator.history(1)
                    lease, outcome = history[-2:]
                    notices = [c for c in github.comments(1) if c["body"].startswith(ACTION_MARKER)]
                    self.assertEqual((lease["result"], outcome["status"]), ("retry", "retry"))
                    self.assertEqual(len(attempts(history, "worker", self.now)), attempt)
                    self.assertEqual(github.item(1).labels, frozenset({"ready"}))
                    if attempt < worker.max_attempts:
                        self.assertEqual(notices, [])
                        self.assertEqual(loop.plans()[0].state, "backoff")
                        self.assertFalse(loop.tick())
                    else:
                        self.assertEqual(len(notices), 1)
                        self.assertEqual(loop.plans()[0].state, "blocked")
                    self.now = seconds(lease["retry_after"])
                notice = notices[0]["body"]
                visible, details = notice.split('<details>', 1)
                self.assertIn('review the blocker details and decide the next step', visible)
                self.assertNotIn('Execution exited', visible)
                self.assertIn('Attempt limit exhausted', details)
                for expected in (f"Execution exited {code}", "Attempt limit exhausted", "max-attempts: 3",
                                 "launcher-host", str(self.root / ".ub-agents" / "runs" / lease["run"]),
                                 lease["url"], outcome["url"], "ub-agents retry 1 --agent worker"):
                    self.assertIn(expected, notice)
                before = deepcopy(github.writes)
                self.output.clear()
                for _ in range(3):
                    self.assertFalse(loop.tick())
                self.assertEqual(github.writes, before)
                self.assertEqual(sum(": blocked —" in line for line in self.output), 1)

    def test_exhausted_notice_failure_leaves_retry_verdict_and_count_unchanged(self):
        worker = agent(self.root, kind="issue", max_attempts=1)
        loop = Loop(config(self.root, worker), self.github, "operator", output=self.output.append)
        original = self.github.create_comment

        def create(number, text):
            if text.startswith(ACTION_MARKER):
                raise GitHubError("POST", "comments", "Notice unavailable")
            return original(number, text)

        with patch.object(self.github, "create_comment", side_effect=create), \
                patch("ub_agents.loop.supervise", return_value=7):
            self.assertTrue(loop.tick())
        lease, outcome = loop.coordinator.history(1)
        self.assertEqual((lease["result"], lease["attempt_effect"], outcome["status"]),
                         ("retry", "failure", "retry"))
        self.assertEqual(len(attempts(loop.coordinator.history(1), "worker", loop.coordinator.clock())), 1)
        self.assertEqual(loop.plans()[0].state, "blocked")
        self.assertEqual(self.github.item(1).labels, frozenset({"ready"}))
        self.assertEqual(self.notices(), [])
        self.assertTrue(any("Action needed post" in line and "failed" in line for line in self.output))

    def test_advisory_failures_do_not_change_release_transition_or_counts(self):
        for operation in ("unminimized_comments", "minimize_comment", "create_comment", "candidate_evidence", "item"):
            with self.subTest(operation=operation):
                self.setUp()
                first = self.start(2)
                self.co.report(first, "retry", "Transient failure")
                self.co.release(first, "retry", "Transient failure")
                lease = self.start(2)
                outcome = self.co.report(lease, "blocked", "Need a decision", action="Maintainer: choose A or B; recommend A.")
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
            loop.coordinator.report(loop.coordinator.history(1)[0], "success", "Human merge", outcome="human", action="Maintainer: choose A or B; recommend A.")
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
        self.co.report(lease, "blocked", "Need a decision", action="Maintainer: choose A or B; recommend A.")
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

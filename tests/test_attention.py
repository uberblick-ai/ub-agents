from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console

from tests.support import FakeGitHub, MemoryPublisher, agent, config, issue
from ub_agents.approvals import ApprovalCheck
from ub_agents.attention import attention_state, waiting_time
from ub_agents.coordination import Plan
from ub_agents.loop import Loop
from ub_agents.notices import ACTION_MARKER, action_body
from ub_agents.observations import Observations
from ub_agents.records import iso
from ub_agents.view_data import Session, work_rows
from ub_agents.view_unblock import ActionComment, unblock_metadata
from ub_agents.view_work import work_lines


class AttentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = config(self.root, agent(self.root, kind='issue'))
        self.publisher = MemoryPublisher()
        self.observer = Observations(self.cfg, 'operator', None, self.publisher)
        self.observer.begin_pass()
        self.plan = Plan(replace(issue(), labels={'needs-human'}), self.cfg.agents[0], None,
                         'parked', 'Stop label needs-human is present', 1)

    def row(self, plan=None, **kwargs):
        self.observer.plan(plan or self.plan, **kwargs)
        session = Session(self.root / 'session.json', self.publisher.snapshots[-1])
        return work_rows(session, self.root)[0], session

    def outcome(self, **kwargs):
        return {'kind': 'outcome', 'id': 2, 'assignment': 1, 'agent': 'worker', 'run': 'r',
                'lease_id': 1, 'created': iso(1000), 'summary': 'Maintainer merge', 'status': 'success',
                'accepted': True, 'transition_complete': True, 'transition': {'add': ['needs-human']}, **kwargs}

    def finished(self, **kwargs):
        return {'kind': 'lease', 'id': 1, 'assignment': 1, 'agent': 'worker', 'run': 'r',
                'created': iso(900), 'expires': iso(1200), 'state': 'released', 'result': 'blocked',
                'summary': 'Choose direction', **kwargs}

    def notice(self, id=3, author='other', created=1300, body='Maintainer decision'):
        return {'id': id, 'body': ACTION_MARKER + f'r{id} -->\n**Action needed**\n\n{body}\n\n'
                'After resolving the blocker, run:\n\n```sh\nub-agents retry 1\n```',
                'user': {'login': author}, 'created_at': iso(created)}

    def test_newest_trusted_notice_from_any_launcher_takes_precedence(self):
        plan = replace(self.plan, history=(self.finished(), self.outcome()))
        row, session = self.row(plan, comments=[self.notice(), self.notice(4, created=1400),
                                              self.notice(5, author='reader', created=1500)],
                                authors={'other': True, 'reader': False})
        self.assertEqual(row.data['waiting_since'], iso(1400))
        self.assertEqual(row.data['attention_reason'], 'Maintainer decision')
        self.assertNotIn('retry', row.data['attention_reason'])
        self.assertEqual(session.data['action_needed']['1']['author'], 'other')
        self.assertIn('Maintainer decision', row.reason)

    def test_finalized_stop_outcome_and_handoff_copy(self):
        for outcome in (self.outcome(), self.outcome(assignment=10, handoff=1, agent='integrator')):
            row, _ = self.row(replace(self.plan, history=(outcome,)))
            self.assertEqual(row.data['waiting_since'], iso(1000))
            self.assertEqual(row.data['attention_reason'], 'Maintainer merge')
        for changes in ({'accepted': False}, {'transition_complete': False}, {'rejected': 'bad'},
                        {'handoff': 2}, {'transition': {'add': ['ready']}}):
            row, _ = self.row(replace(self.plan, history=(self.outcome(**changes),)))
            self.assertIsNone(row.data['waiting_since'])

    def test_notice_action_precedes_reason_and_has_no_bold_markers_in_work_view(self):
        action = 'Maintainer: choose A or B; recommend A.'
        notice = self.notice(body=f'**{action}**\n\nLong gate details')
        row, _ = self.row(replace(self.plan, history=(self.outcome(action='Older action'),)),
                          comments=[notice], authors={'other': True})
        self.assertEqual(row.data['attention_reason'], action)
        self.assertIn(action, work_lines(row, 100)[1].plain)
        self.assertNotIn('**', work_lines(row, 100)[1].plain)

    def test_parking_outcome_action_is_shown_without_a_notice(self):
        action = 'Maintainer: merge this PR under project policy.'
        for outcome in (self.outcome(action=action), self.outcome(action=action, assignment=10, handoff=1)):
            row, _ = self.row(replace(self.plan, history=(outcome,)))
            self.assertEqual(row.data['attention_reason'], action)
        outcome = self.outcome(action=action, accepted=False, status='blocked', transition_complete=False)
        row, _ = self.row(replace(self.plan, state='blocked', history=(self.finished(), outcome)))
        self.assertEqual(row.data['attention_reason'], action)
        recovered = self.finished(id=10, mode='recovery', recovered_lease_id=1)
        row, _ = self.row(replace(self.plan, state='blocked', history=(outcome, recovered)))
        self.assertEqual(row.data['attention_reason'], action)

    def test_multiple_asks_are_shown_without_markdown_or_collapsed_reasoning(self):
        asks = ['Owner: choose A or B; recommend A.', 'Maintainer: use *staging* & review [the PR] when load < capacity -> staged with ~2h downtime.']
        notice = self.notice()
        notice['body'] = action_body(ACTION_MARKER + 'r -->', asks, 'Private reasoning\n\nCI evidence')
        row, _ = self.row(comments=[notice], authors={'other': True})
        self.assertEqual(row.data['attention_reason'], '; '.join(asks))
        self.assertNotIn('reasoning', row.data['attention_reason'])
        row, _ = self.row(replace(self.plan, history=(self.outcome(action=asks[0], actions=asks),)))
        self.assertEqual(row.data['attention_reason'], '; '.join(asks))

    def test_launcher_fallback_notice_shows_the_review_request_without_diagnostics(self):
        fallback = 'Maintainer: review the blocker details and decide the next step.'
        notice = self.notice()
        notice['body'] = action_body(ACTION_MARKER + 'r -->', [fallback], 'Execution exited 1; inspect the run log.')
        row, _ = self.row(comments=[notice], authors={'other': True})
        self.assertEqual(row.data['attention_reason'], fallback)
        self.assertNotIn('Execution exited', row.reason)

    def test_options_notice_uses_the_reason_without_choices_or_resume_text(self):
        notice = self.notice()
        notice['body'] = action_body(ACTION_MARKER + 'r -->', ['Owner: authorize the change.'],
            'CI is red. Full diagnostics.', options=['Maintainer: run CI.', 'Maintainer: merge a fix.'],
            reason='CI is red. Full diagnostics.', resume='Then resume worker:\n\n```sh\nub-agents retry 1\n```')
        row, _ = self.row(comments=[notice], authors={'other': True})
        self.assertEqual(row.data['attention_reason'], 'CI is red.')

    def test_blocked_and_exhausted_fall_back_to_latest_finished_run(self):
        history = (self.finished(), self.finished(id=3, run='new', expires=iso(1500), agent='integrator'),
                   self.finished(id=4, run='active', state='running', expires=iso(5000)))
        for state, reason, attempt in (('blocked', 'Last run blocked', 1),
                                       ('blocked', 'Attempt limit exhausted', 4), ('failed', '', 4)):
            row, _ = self.row(replace(self.plan, state=state, reason=reason, attempt=attempt, history=history))
            self.assertEqual(row.data['waiting_since'], iso(1500))
            self.assertEqual(row.data['attention_reason'], 'Choose direction')
        row, _ = self.row(replace(self.plan, history=(self.finished(),)))
        self.assertIsNone(row.data['waiting_since'])

    def test_unknown_start_and_summary_omitted_and_approval_gate_labels(self):
        row, session = self.row()
        self.assertIsNone(row.data['waiting_since'])
        first, second = work_lines(row, 50)
        self.assertTrue(first.plain.rstrip().endswith(row.data['title']))
        self.assertEqual(second.plain, '  worker · needs-human')
        self.assertEqual(unblock_metadata(row, ActionComment(created_at=iso(1000), available=True), session, 2000),
                         ('worker · needs-human', ''))
        gate = ApprovalCheck(False, 'Approve outside input', gate='start', gate_key='input')
        row, _ = self.row(replace(self.plan, item=issue(), approval_gate=gate,
                                  history=(self.finished(), self.outcome())))
        self.assertEqual(row.data['stop_labels'], list(self.cfg.stop_labels))
        self.assertEqual(row.data['attention_reason'], 'Approve outside input')
        self.assertIsNone(row.data['waiting_since'])

    def test_resumed_notice_is_ignored_and_new_post_updates_published_row(self):
        row, _ = self.row(replace(self.plan, history=(self.finished(id=5),)),
                          comments=[self.notice()], authors={'other': True})
        self.assertIsNone(row.data['waiting_since'])
        self.observer.action_needed(1, self.notice(6, created=1600))
        row = self.publisher.snapshots[-1]['latest_pass']['rows'][0]
        self.assertEqual(row['waiting_since'], iso(1600))
        self.assertEqual(row['attention_reason'], 'Maintainer decision')

    def test_wait_format_boundaries_floor_and_invalid_times(self):
        for seconds, expected in ((-1, '0m'), (0, '0m'), (59, '0m'), (60, '1m'), (3599, '59m'),
                                  (3600, '1h'), (172799, '47h'), (172800, '2d'), (345600, '4d')):
            self.assertEqual(waiting_time(iso(1000), 1000 + seconds), expected)
        for invalid in (None, '', 'invalid', '2026-10-05T12:12:00', 1000):
            self.assertEqual(waiting_time(invalid, 2000), '')

    def test_each_glyph_reason_clipping_red_time_and_matching_unblock(self):
        now = datetime.fromtimestamp(1000 + 3599, timezone.utc)
        for state, reason, attempt, glyph, label in (
                ('parked', 'Stop label needs-human is present', 1, '?', 'needs-human'),
                ('blocked', 'Last run blocked', 1, '!', 'blocked'),
                ('blocked', 'Attempt limit exhausted', 4, '✗', 'failed 3/3'),
                ('failed', '', 4, '✗', 'failed 3/3')):
            row, session = self.row(replace(self.plan, state=state, reason=reason, attempt=attempt),
                                    comments=[self.notice(created=1000, body='Stop label needs-human is present: ' + '界' * 80)],
                                    authors={'other': True})
            self.assertEqual(attention_state(row), (glyph, label))
            first, detail = work_lines(row, 50, now=now)
            self.assertEqual(first.plain[0], glyph)
            self.assertTrue(first.plain.endswith('59m'))
            self.assertTrue(detail.plain.startswith(f'  worker · {label} · '))
            self.assertTrue(detail.plain.endswith('…'))
            self.assertNotIn('Stop label', detail.plain)
            self.assertEqual(first.get_style_at_offset(Console(), 49).color.get_truecolor().hex, '#ff8b7f')
            self.assertIn('waiting 59m', unblock_metadata(row, ActionComment(), session, now.timestamp())[0])
            self.assertTrue(work_lines(row, 50, now=datetime.fromtimestamp(4600, timezone.utc))[0].plain.endswith('1h'))
            for width in (0, 1, 4, 15, 60):
                lines = work_lines(row, width, now=now)
                self.assertEqual(lines[0].cell_len, width)
                self.assertLessEqual(lines[1].cell_len, width)

    def test_multiple_stop_labels(self):
        self.observer.stop_labels = ('needs-human', 'hold')
        row, _ = self.row(replace(self.plan, item=replace(issue(), labels={'needs-human', 'hold'})))
        self.assertEqual(attention_state(row), ('?', 'hold, needs-human'))

    def test_launcher_display_adds_no_github_reads(self):
        github = FakeGitHub(issue())
        github.login = 'other'
        github.roles['other'] = 'write'
        foreign = Loop(self.cfg, github, 'other', output=lambda *_: None)
        lease = foreign.coordinator.claim(foreign.coordinator.plan(issue(), self.cfg.agents[0], self.cfg.stop_labels),
                                          self.cfg.stop_labels)
        foreign.coordinator.update(lease, state='running', started=True)
        foreign.coordinator.report(lease, 'blocked', 'Choose direction', action="Maintainer: choose A or B; recommend A.")
        foreign.coordinator.release(lease, 'blocked', 'Choose direction')
        other = github.create_comment(1, self.notice(author='operator')['body'])
        github.login = 'operator'
        observer = Observations(self.cfg, 'operator', None, self.publisher)
        loop = Loop(self.cfg, github, 'operator', output=lambda *_: None, observer=observer)
        reads = []
        with patch.object(github, 'comments', wraps=github.comments) as comments, \
                patch.object(github, 'role', wraps=github.role) as roles:
            for enabled in (False, True):
                loop.observer = observer if enabled else None
                observer.begin_pass()
                list(loop.iter_plans(cached=False))
                reads.append((comments.call_count, roles.call_count))
                comments.reset_mock()
                roles.reset_mock()
        self.assertEqual(reads[0], reads[1])
        row = self.publisher.snapshots[-1]['latest_pass']['rows'][0]
        self.assertEqual(row['waiting_since'], other['created_at'])
        self.assertEqual(self.publisher.snapshots[-1]['action_needed']['1']['author'], 'other')

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.support import SetupGitHub
from ub_agents.config import load_config
from ub_agents.errors import AgentError
from ub_agents.init_boards import Boards
from ub_agents.init_setup import Choices, apply_plan, make_plan


class BoardSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / 'setup' / 'workflow.yaml'
        self.github = SetupGitHub()
        self.boards = Boards(self.github, 'org/project')
        self.choices = Choices('codex:model:high')

    def plan(self, roles=('implementer',)):
        repo = self.boards.setup()
        self.choices.board_requests = {role: self.boards.request(f'{role} lessons', 'General', repo) for role in roles}
        return make_plan(self.path, 'org/project', self.choices)

    def test_discovery_and_requests_are_read_only_and_only_real_numbers_enter_config(self):
        plan = self.plan()
        request = self.choices.board_requests['implementer']
        self.assertEqual((request.title, request.category), ('implementer lessons', 'General'))
        self.assertEqual(self.github.writes, [])
        self.assertFalse(self.path.parent.exists())
        self.assertNotIn('retrospectives:', plan.targets[self.path])
        result = apply_plan(plan, self.github)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.board_numbers, {'implementer': 204})
        roles = {role.name: role for role in load_config(self.path).agents}
        self.assertEqual(roles['implementer'].retrospectives, 204)
        self.assertEqual([write[0] for write in self.github.writes], ['create-board'])
        mutation = self.github.writes[0][2]
        self.assertEqual(mutation['categoryId'], 'general-id')
        self.assertIn('launcher account', mutation['body'])

    def test_protection_runs_before_public_mutations(self):
        plan = self.plan()
        self.path.parent.mkdir()
        self.path.write_text('existing')
        result = apply_plan(plan, self.github)
        self.assertFalse(result.ok)
        self.assertEqual(self.path.read_text(), 'existing')
        self.assertEqual(self.github.writes, [])

    def test_exact_title_is_reused_when_created_between_review_and_apply(self):
        plan = self.plan()
        self.github.boards[204] = self.github.board(204, 'implementer lessons')
        result = apply_plan(plan, self.github)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.github.writes, [])
        self.assertIn('Reused public board', result.completed[0])
        roles = {role.name: role for role in load_config(self.path).agents}
        self.assertEqual(roles['implementer'].retrospectives, 204)

    def test_duplicate_titles_require_an_explicit_number(self):
        for number in (204, 205):
            self.github.boards[number] = self.github.board(number, 'same title')
        with self.assertRaisesRegex(AgentError, 'set the board numbers'):
            self.boards.find('same title')
        self.assertEqual(self.github.writes, [])

    def test_discovery_searches_past_first_page_before_creating(self):
        original = self.github.graphql
        def paged(query, variables):
            if 'discussions(first:' in query:
                page = {'nodes': [], 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}} if variables['cursor'] is None else {
                    'nodes': [{'number': 203, 'title': 'Implementer retrospectives'}],
                    'pageInfo': {'hasNextPage': False, 'endCursor': None}}
                return {'repository': {'discussions': page}}
            return original(query, variables)
        with patch.object(self.github, 'graphql', side_effect=paged):
            self.assertEqual(self.boards.find('Implementer retrospectives')['number'], 203)
        self.assertEqual(self.github.writes, [])

    def test_partial_creation_failure_keeps_public_effects_without_any_config_or_directory(self):
        plan = self.plan(('implementer', 'reviewer'))
        def stop_after_first(board):
            self.github.create_board_error = AgentError('token failure ghp-secret')
        self.github.after_create = stop_after_first
        result = apply_plan(plan, self.github)
        self.assertFalse(result.ok)
        self.assertEqual(result.board_numbers, {'implementer': 204})
        self.assertIn('Created public board https://github.com/org/project/discussions/204', result.completed[0])
        self.assertFalse(self.path.parent.exists())
        self.assertIn('reviewer lessons', result.remaining[0])
        self.assertNotIn('ghp-secret', result.error)
        # After external repair, an untouched local starter can be retried and
        # the first successful board is discovered rather than duplicated.
        self.github.after_create = None
        self.github.create_board_error = None
        retry = apply_plan(plan, self.github)
        self.assertTrue(retry.ok, retry.error)
        self.assertEqual(len(self.github.writes), 2)
        roles = {role.name: role for role in load_config(self.path).agents}
        self.assertEqual(roles['reviewer'].retrospectives, 205)

    def test_created_but_unverifiable_board_never_enables_retrospectives(self):
        plan = self.plan()
        self.github.after_create = lambda board: board.update(viewerCanUpdate=False)
        result = apply_plan(plan, self.github)
        self.assertFalse(result.ok)
        self.assertEqual(len(self.github.writes), 1)
        self.assertIn('Created public board', result.completed[0])
        self.assertEqual(result.board_numbers, {})
        self.assertFalse(self.path.parent.exists())

    def test_interrupted_or_unreadable_creation_response_reports_possible_public_effect(self):
        plan = self.plan()
        for error in (KeyboardInterrupt(), AgentError('response unavailable')):
            self.github.create_board_error = error
            result = apply_plan(plan, self.github)
            self.assertFalse(result.ok)
            self.assertIn('may have completed', result.incomplete[0])
            self.assertFalse(self.path.parent.exists())
        self.github.create_board_error = None
        self.github.after_create = lambda board: board.update(number='node-id')
        result = apply_plan(plan, self.github)
        self.assertFalse(result.ok)
        self.assertIn('may have completed', result.incomplete[0])
        self.assertFalse(self.path.parent.exists())

    def test_unavailable_discussions_account_creation_permission_and_category(self):
        self.github.discussion_metadata['hasDiscussionsEnabled'] = False
        with self.assertRaisesRegex(AgentError, 'Enable GitHub Discussions'):
            self.boards.setup()
        self.github.discussion_metadata['hasDiscussionsEnabled'] = True
        self.github.discussion_metadata['viewerPermission'] = 'READ'
        with self.assertRaisesRegex(AgentError, 'cannot create requested boards'):
            self.boards.request('chosen board', 'General', self.boards.setup())
        self.github.discussion_metadata['viewerPermission'] = 'WRITE'
        with self.assertRaisesRegex(AgentError, 'category'):
            self.boards.request('chosen board', 'not a category', self.boards.setup())
        self.assertEqual(self.github.writes, [])

    def test_repository_mismatch_and_missing_or_noninteger_destinations_refuse(self):
        self.github.discussion_metadata['nameWithOwner'] = 'other/project'
        with self.assertRaises(AgentError):
            self.boards.setup()
        original = deepcopy(self.github.boards[203])
        for change in ({'number': True}, {'number': '203'}, {'number': 0}, {'id': ''},
                       {'url': 'https://github.com/other/project/discussions/203'}):
            self.github.boards[203] = original | change
            with self.assertRaises(AgentError):
                self.boards.verify(203)
        self.assertEqual(self.github.writes, [])

import subprocess
import unittest
from unittest.mock import patch

from ub_agents.errors import AgentError
from ub_agents.github import GitHub


class GitHubTests(unittest.TestCase):
    def test_reads_all_pages_without_indexed_search(self):
        with patch("ub_agents.github.subprocess.run", return_value=subprocess.CompletedProcess([], 0, '[[{"id":1}],[{"id":2}]]', "")) as run:
            self.assertEqual(GitHub("org/project").comments(1), [{"id": 1}, {"id": 2}])
        argv = run.call_args.args[0]
        self.assertIn("--paginate", argv)
        self.assertIn("--slurp", argv)
        self.assertNotIn("search", argv)

    def test_bad_response_and_failed_auth_are_failures(self):
        for result in [subprocess.CompletedProcess([], 0, "not json", ""),
                       subprocess.CompletedProcess([], 0, "{}", ""),
                       subprocess.CompletedProcess([], 1, "", "not authenticated")]:
            with patch("ub_agents.github.subprocess.run", return_value=result), self.assertRaises(AgentError):
                GitHub("org/project").comments(1)

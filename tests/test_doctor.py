from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main
from ub_agents.config import load_config
from ub_agents.coordination import Coordinator
from ub_agents.doctor import diagnose, render
from ub_agents.errors import AgentError
from ub_agents.execution import repository_checks
from ub_agents.github import GitHub
from tests.support import DoctorGitHub, RecordingRunner, issue, isolate_observations


class DoctorTests(unittest.TestCase):
    def setUp(self):
        isolate_observations(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / "ub-agents.yaml"
        self.path.write_text('''repository: org/project
agents:
  worker:
    runtime: codex:model-a:high
    runtime-args: [--sandbox, danger-full-access]
    trigger: ready
    outcomes: {done: {}}
    instructions: instructions.md
''')
        (self.root / "instructions.md").write_text("Synthetic instructions")
        self.runner = RecordingRunner(self.root)
        self.github = DoctorGitHub()
        self.missing = set()
        self.which = lambda name: (shutil.which(name) if "/" in name else
                                   None if name in self.missing else f"/tools/{name}")

    def diagnose(self, **overrides):
        return diagnose(self.path, **(dict(runner=self.runner, which=self.which,
                                          github=self.github) | overrides))

    def checks(self, result, id):
        return [c for c in result["checks"] if c["id"].split(":")[0] == id]

    def one(self, result, id):
        values = self.checks(result, id)
        self.assertEqual(len(values), 1, values)
        return values[0]

    def capture(self, result, json_output=False):
        with redirect_stdout(io.StringIO()) as output:
            render(result, json_output)
        return output.getvalue()

    def cli(self, json_output=False):
        with patch("ub_agents.doctor.subprocess.run", self.runner), \
                patch("ub_agents.doctor.shutil.which", self.which), \
                patch("ub_agents.doctor.GitHub", return_value=self.github), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            code = main(["--config", str(self.path), "doctor"] + (["--json"] if json_output else []))
        self.assertEqual(stderr.getvalue(), "")
        return code, stdout.getvalue()

    def test_success_json_schema_and_stable_human_order(self):
        result = self.diagnose()
        self.assertTrue(result["ok"])
        self.assertEqual(result["version"], 1)
        self.assertEqual([c["id"] for c in result["checks"]][:4], ["python", "platform", "git", "gh"])
        self.assertEqual(len({c["id"] for c in result["checks"]}), len(result["checks"]))
        for check in result["checks"]:
            self.assertEqual(set(check), {"id", "status", "required", "agent", "runtime", "message", "remedy"})
            self.assertIn(check["status"], {"ok", "warn", "fail", "skip"})
            self.assertIsInstance(check["required"], bool)
        self.assertIn("0 required failures, 0 warnings", self.capture(result))
        self.assertEqual(self.cli()[0], 0)
        code, output = self.cli(True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), result)

    def test_approvals_effective_policy_and_source(self):
        for visibility, value in (("public", "on"), ("private", "off"), ("internal", "off")):
            with self.subTest(visibility=visibility):
                self.github.metadata["visibility"] = visibility
                check = self.one(self.diagnose(), "approvals")
                self.assertEqual(check["status"], "ok")
                self.assertEqual(check["message"], f"Approvals: {value} (visibility ({visibility}))")
        for value in ("on", "off"):
            self.path.write_text(self.path.read_text() + f"approvals: {value}\n")
            self.github.metadata.pop("visibility", None)
            check = self.one(self.diagnose(), "approvals")
            self.assertEqual((check["status"], check["message"]), ("ok", f"Approvals: {value} (config)"))
            self.path.write_text(self.path.read_text().replace(f"approvals: {value}\n", ""))
        self.assertEqual(self.one(self.diagnose(), "approvals")["status"], "fail")

    def test_default_approvals_skip_when_repository_metadata_is_unavailable(self):
        for missing_gh in (True, False):
            with self.subTest(missing_gh=missing_gh):
                self.missing = {"gh"} if missing_gh else set()
                self.github.repository_error = AgentError("repository unavailable")
                result = self.diagnose()
                self.assertFalse(result["ok"])
                check = self.one(result, "approvals")
                self.assertEqual(check["status"], "skip")
                self.assertEqual(check["message"], "gh unavailable" if missing_gh else
                                 "repository response unavailable")
                self.assertIsNone(check["remedy"])

    def test_explicit_approvals_report_without_repository_metadata(self):
        for missing_gh in (True, False):
            for value in ("on", "off"):
                with self.subTest(missing_gh=missing_gh, value=value):
                    self.missing = {"gh"} if missing_gh else set()
                    self.github.repository_error = AgentError("repository unavailable")
                    self.path.write_text(self.path.read_text() + f"approvals: {value}\n")
                    check = self.one(self.diagnose(), "approvals")
                    self.assertEqual((check["status"], check["message"]),
                                     ("ok", f"Approvals: {value} (config)"))
                    self.path.write_text(self.path.read_text().replace(f"approvals: {value}\n", ""))

    def test_runtime_pause_is_visible_read_only_and_does_not_fail_doctor(self):
        from ub_agents.records import iso, timestamp
        from ub_agents.runtime_usage import RuntimeUsage
        now = timestamp()
        usage = RuntimeUsage(self.root, lambda: now, output=lambda *_: None)
        usage.record("codex", "primary", 90, now + 300, 18000)
        before = usage.path.read_bytes()
        result = self.diagnose()
        check = self.one(result, "runtime-pause")
        self.assertEqual((check["status"], check["required"]), ("ok", False))
        self.assertIn(f"codex paused: primary usage 90%; pause ends {iso(now + 360)}", check["message"])
        self.assertTrue(result["ok"])
        self.assertIn("codex paused", self.capture(result))
        self.assertIn("codex paused", self.capture(result, True))
        self.assertEqual(usage.path.read_bytes(), before)

    def test_quota_comes_from_real_request_headers_and_warns_below_ten_percent(self):
        github = GitHub('org/project', self.runner)
        prefix = ('gh', 'api', '--hostname', 'github.com', '--method', 'GET', '-H',
                  'Accept: application/vnd.github+json', '--include')
        self.runner.responses[prefix + ('repos/org/project',)] = json.dumps(self.github.metadata)
        self.runner.responses[prefix + ('repos/org/project/labels?per_page=100&page=1',)] = json.dumps(
            [{'name': name} for name in self.github.label_names])
        self.runner.responses[prefix + ('repos/org/project/collaborators/operator/permission',)] = (
            '{"role_name":"write"}')
        for remaining, status in ((500, 'ok'), (499, 'warn')):
            with self.subTest(remaining=remaining):
                self.runner.responses[prefix + ('user',)] = (
                    f'HTTP/2.0 200 OK\nX-RateLimit-Remaining: {remaining}\n'
                    'X-RateLimit-Limit: 5000\nX-RateLimit-Reset: 1000\n\n{"login":"operator"}')
                result = self.diagnose(github=github)
                check = self.one(result, 'github-rate-limit')
                self.assertEqual(check['status'], status)
                self.assertIn(f'{remaining} of 5000 requests remaining', check['message'])
                self.assertIn('1970-01-01T00:16:40Z (UTC)', check['message'])
                self.assertTrue(result['ok'])
        self.assertFalse(any('rate_limit' in command for command, _ in self.runner.calls))

    def test_doctor_warns_when_a_real_request_is_rate_limited(self):
        github = GitHub('org/project', self.runner)
        prefix = ('gh', 'api', '--hostname', 'github.com', '--method', 'GET', '-H',
                  'Accept: application/vnd.github+json', '--include')
        response = subprocess.CompletedProcess([], 1,
            'HTTP/2.0 403 Error\nX-RateLimit-Remaining: 0\nX-RateLimit-Limit: 5000\n'
            'X-RateLimit-Reset: 1000\n\n{}', 'private stderr')
        for endpoint in ('user', 'repos/org/project', 'repos/org/project/labels?per_page=100&page=1'):
            self.runner.responses[prefix + (endpoint,)] = response
        check = self.one(self.diagnose(github=github), 'github-rate-limit')
        self.assertEqual(check['status'], 'warn')
        self.assertIn('0 of 5000 requests remaining', check['message'])
        self.assertIn('doctor was rate limited', check['message'])
        self.assertNotIn('private', check['message'])

    def test_later_rate_limit_headers_replace_earlier_successful_quota(self):
        github = GitHub('org/project', self.runner)
        prefix = ('gh', 'api', '--hostname', 'github.com', '--method', 'GET', '-H',
                  'Accept: application/vnd.github+json', '--include')
        self.runner.responses[prefix + ('user',)] = (
            'HTTP/2.0 200 OK\nX-RateLimit-Remaining: 5000\nX-RateLimit-Limit: 5000\n'
            'X-RateLimit-Reset: 1000\n\n{"login":"operator"}')
        self.runner.responses[prefix + ('repos/org/project',)] = json.dumps(self.github.metadata)
        self.runner.responses[prefix + ('repos/org/project/labels?per_page=100&page=1',)] = json.dumps(
            [{'name': name} for name in self.github.label_names])
        self.runner.responses[prefix + ('repos/org/project/collaborators/operator/permission',)] = (
            subprocess.CompletedProcess([], 1,
                'HTTP/2.0 403 Error\nX-RateLimit-Remaining: 0\nX-RateLimit-Limit: 5000\n'
                'X-RateLimit-Reset: 4600\n\n{}', 'API rate limit exceeded'))
        result = self.diagnose(github=github)
        check = self.one(result, 'github-rate-limit')
        self.assertEqual(check['status'], 'warn')
        self.assertIn('0 of 5000 requests remaining', check['message'])
        self.assertIn('1970-01-01T01:16:40Z (UTC)', check['message'])
        self.assertIn('doctor was rate limited', check['message'])
        self.assertTrue(result['ok'])

    def test_missing_git_and_gh_continue_and_exit_one(self):
        for name in ("git", "gh"):
            with self.subTest(name=name):
                self.missing = {name}
                result = self.diagnose()
                self.assertFalse(result["ok"])
                check = self.one(result, name)
                self.assertEqual((check["status"], check["required"]), ("fail", True))
                self.assertIn(name, check["remedy"])
                self.assertEqual(self.one(result, "process-inspection")["status"], "ok")
                self.assertEqual(self.one(result, "runtimes")["status"], "ok")
                self.assertEqual(self.cli(True)[0], 1)

    def test_missing_trigger_and_add_remove_transition_labels_are_required(self):
        self.path.write_text(self.path.read_text().replace('    outcomes: {done: {}}\n', '''    outcomes:
      done: {add: [review-next], remove: [old-state]}
'''))
        self.github.label_names = ['needs-human']
        result = self.diagnose()
        labels = self.checks(result, 'github-label')
        self.assertFalse(result['ok'])
        failures = [check for check in labels if check['status'] == 'fail']
        self.assertEqual(len(failures), 3)
        for check, name in zip(failures, ['ready', 'review-next', 'old-state']):
            self.assertTrue(check['required'])
            self.assertEqual(check['agent'], 'worker')
            self.assertIn(name, check['message'])
            self.assertIn('worker', check['message'])
            self.assertIn(f'gh label create {name} --repo org/project', check['remedy'])
        self.assertEqual(self.github.writes, [])

    def test_missing_stop_label_warns_and_all_present_match_case_insensitively(self):
        self.github.label_names = ['READY']
        result = self.diagnose()
        self.assertTrue(result['ok'])
        labels = self.checks(result, 'github-label')
        self.assertEqual([check['status'] for check in labels], ['ok', 'warn'])
        self.assertEqual(labels[1]['required'], False)
        self.assertIn('needs-human', labels[1]['remedy'])
        self.github.label_names.append('NEEDS-HUMAN')
        self.assertTrue(all(check['status'] == 'ok' for check in self.checks(self.diagnose(), 'github-label')))
        self.assertEqual(self.github.writes, [])

    def test_stop_label_used_by_a_transition_has_both_required_and_stop_checks(self):
        self.path.write_text(self.path.read_text().replace('    outcomes: {done: {}}\n', '''    outcomes:
      needs-human: {add: [needs-human]}
'''))
        self.github.label_names = ['ready']
        result = self.diagnose()
        self.assertFalse(result['ok'])
        missing = [check for check in self.checks(result, 'github-label') if check['status'] != 'ok']
        self.assertEqual([(check['status'], check['agent']) for check in missing],
                         [('fail', 'worker'), ('warn', None)])

    def test_unreadable_labels_fail_and_hide_private_errors(self):
        for error in (AgentError('ghp_private sk-private'), subprocess.TimeoutExpired('gh', 20)):
            self.github.label_error = error
            result = self.diagnose()
            self.assertFalse(result['ok'])
            check = self.one(result, 'github-labels')
            self.assertEqual((check['status'], check['required']), ('fail', True))
            self.assertNotIn('private', self.capture(result))
            self.assertEqual(self.github.writes, [])

    def test_runtime_permissions_warns_for_each_agent_without_args_only(self):
        self.path.write_text(self.path.read_text().replace('    runtime-args: [--sandbox, danger-full-access]\n', '') + '''  reviewer:
    runtime: [codex:model-a:high, claude:model-b:high]
    trigger: needs-review
    outcomes: {done: {}}
    instructions: instructions.md
  command:
    command: [git, --version]
    trigger: ready
    outcomes: {done: {}}
''')
        result = self.diagnose()
        self.assertTrue(result['ok'])
        warnings = self.checks(result, 'runtime-permissions')
        self.assertEqual([check['agent'] for check in warnings], ['worker', 'reviewer'])
        for check in warnings:
            self.assertEqual((check['status'], check['required']), ('warn', False))
            self.assertIn('runtime-args', check['message'])
            self.assertIn('https://github.com/uberblick-ai/ub-agents/blob/main/docs/configuration.md#runtime-permissions',
                          check['remedy'])
        self.assertEqual(self.github.writes, [])

    def test_missing_only_runtime_is_required_and_names_agent(self):
        self.missing.add("codex")
        result = self.diagnose()
        check = self.one(result, "runtime-executable")
        self.assertEqual((check["status"], check["required"], check["agent"]), ("fail", True, "worker"))
        self.assertEqual(self.one(result, "runtime-auth")["status"], "skip")
        self.assertFalse(result["ok"])
        self.assertFalse(any(c[0][0] == "codex" for c in self.runner.calls))

    def test_eligible_alternative_warns_and_exits_zero(self):
        self.path.write_text(self.path.read_text().replace("runtime: codex:model-a:high",
                                                         "runtime: [codex:model-a:high, claude:model-b:high]"))
        self.missing.add("codex")
        result = self.diagnose()
        missing = self.checks(result, "runtime-executable")[0]
        self.assertEqual((missing["status"], missing["required"], missing["agent"], missing["runtime"]),
                         ("warn", False, "worker", "codex:model-a:high"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.cli()[0], 0)
        self.assertIn("  remedy: Install codex", self.capture(result))

    def test_unauthenticated_github_still_reads_repository(self):
        self.github.auth_error = AgentError("ghp-auth-secret")
        result = self.diagnose()
        self.assertFalse(result["ok"])
        self.assertEqual(self.one(result, "github-auth")["remedy"], "gh auth login")
        self.assertEqual(self.github.reads, ["user", "repos/org/project", "repos/org/project/labels"])

    def test_elevated_launcher_role_warns_without_failing_or_writing(self):
        for role in ("maintain", "admin", "write", "read", "triage", "none", None):
            with self.subTest(role=role):
                self.github.roles["operator"] = role
                result = self.diagnose()
                check = self.one(result, "github-launcher-role")
                self.assertEqual(check["status"], "ok" if role == "write" else "warn")
                self.assertFalse(check["required"])
                self.assertTrue(result["ok"])
                if role in {"maintain", "admin"}:
                    self.assertIn("approve their own work", check["message"])
                    self.assertIn("write", check["remedy"])
                self.assertEqual(self.github.writes, [])

    def test_listed_launcher_roles_and_unlisted_authenticated_account_warn(self):
        self.path.write_text(self.path.read_text() + "launchers: [alice, bob, reader, outside, unknown]\n")
        self.github.roles.update(alice="write", bob="admin", reader="read", outside="none", unknown=None)
        with patch.object(self.github, "role", wraps=self.github.role) as roles:
            result = self.diagnose()
        self.assertTrue(result["ok"])
        self.assertEqual(self.one(result, "github-launcher-listed")["status"], "warn")
        checks = self.checks(result, "github-launcher-account")
        self.assertEqual([c["status"] for c in checks], ["ok", "ok", "warn", "warn", "warn"])
        self.assertIn("could not be read", checks[-1]["message"])
        self.assertEqual(roles.call_count, 6)
        self.assertEqual(self.github.writes, [])
        self.path.write_text(self.path.read_text().replace("alice, bob", "OPERATOR, bob"))
        with patch.object(self.github, "role", wraps=self.github.role) as roles:
            result = self.diagnose()
        self.assertFalse(self.checks(result, "github-launcher-listed"))
        self.assertEqual(sum(c.args[0].casefold() == "operator" for c in roles.call_args_list), 1)

    def test_listed_launcher_role_read_failure_is_a_warning(self):
        self.path.write_text(self.path.read_text() + "launchers: [operator, alice]\n")
        original = self.github.role
        def role(login):
            if login == "alice":
                raise AgentError("Cannot read permission")
            return original(login)
        with patch.object(self.github, "role", side_effect=role):
            result = self.diagnose()
        self.assertTrue(result["ok"])
        checks = self.checks(result, "github-launcher-account")
        self.assertEqual(checks[-1]["status"], "warn")
        self.assertIn("alice", checks[-1]["message"])

    def test_actual_github_actor_uses_read_only_api_and_hides_failure_output(self):
        for response in ('{"login":"operator"}', subprocess.CompletedProcess([], 4, 'sk-private', 'ghp-private'),
                         subprocess.TimeoutExpired("gh", 20), '{"login": null}'):
            with self.subTest(response=response):
                command = ("gh", "api", "--hostname", "github.com", "--method", "GET", "-H",
                           "Accept: application/vnd.github+json", "--include", "user")
                repo_command = command[:-1] + ("repos/org/project",)
                self.runner.responses[command] = response
                self.runner.responses[repo_command] = json.dumps(self.github.metadata)
                self.runner.responses[command[:-1] + ("repos/org/project/collaborators/operator/permission",)] = '{"role_name":"write"}'
                self.runner.responses[command[:-1] + ("repos/org/project/labels?per_page=100&page=1",)] = json.dumps(
                    [{"name": name} for name in self.github.label_names])
                result = self.diagnose(github=GitHub("org/project", runner=self.runner))
                output = self.capture(result) + self.capture(result, True)
                self.assertNotIn("ghp-private", output)
                self.assertNotIn("sk-private", output)
                self.assertEqual(self.one(result, "github-auth")["status"], "ok" if response == '{"login":"operator"}' else "fail")
                if isinstance(response, subprocess.CompletedProcess):
                    self.assertIn("exit 4", self.one(result, "github-auth")["message"])

    def test_malformed_missing_config_skips_dependencies(self):
        for content in ('agents: [\n', self.path.read_text() + 'unknown: true\n'):
            with self.subTest(content=content):
                self.path.write_text(content)
                result = self.diagnose()
                self.assertFalse(result["ok"])
                self.assertEqual(self.one(result, "config")["status"], "fail")
                for id in ("instructions", "repository-root", "repository-remote", "runtimes", "local-state"):
                    self.assertEqual(self.one(result, id)["status"], "skip")
                for id in ("python", "platform", "git", "gh", "github-auth", "process-inspection"):
                    self.assertEqual(self.one(result, id)["status"], "ok")
        self.path.unlink()
        self.assertIn("ub-agents init", self.one(self.diagnose(), "config")["remedy"])

    def test_instruction_read_failure(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path.name == "instructions.md":
                raise PermissionError("unreadable")
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            self.assertEqual(self.one(self.diagnose(), "instructions")["status"], "fail")

    def test_repository_root_remote_and_github_mismatches(self):
        config = load_config(self.path)
        for remote in ('https://github.com/org/project', 'git@github.com:org/project.git',
                       'ssh://git@github.com/org/project.git'):
            self.assertTrue(all(error is None for _, error in repository_checks(
                config, lambda root, *args: str(root) if args[0] == 'rev-parse' else remote)))
        self.runner.responses[("git", "-C", str(self.root), "rev-parse", "--show-toplevel")] = str(self.root.parent)
        self.runner.responses[("git", "-C", str(self.root), "remote", "get-url", "origin")] = "https://github.com/wrong/repo"
        self.github.metadata["full_name"] = "new-owner/new-project"
        result = self.diagnose()
        for id in ("repository-root", "repository-remote", "github-repository"):
            self.assertEqual(self.one(result, id)["status"], "fail")
        self.assertIn("org/project", self.one(result, "github-repository")["message"])
        self.assertIn("new-owner/new-project", self.one(result, "github-repository")["message"])
        self.github.metadata["full_name"] = "ORG/Project"
        self.assertEqual(self.one(self.diagnose(), "github-repository")["status"], "ok")
        self.assertEqual(self.one(self.diagnose(), "github-permissions")["status"], "ok")
        self.github.metadata["permissions"] = {"pull": True}
        result = self.diagnose()
        self.assertEqual((self.one(result, "github-permissions")["status"], result["ok"]), ("fail", False))
        self.assertIn("triage", self.one(result, "github-permissions")["remedy"])
        self.github.repository_error = AgentError("sk-repository-secret")
        self.assertEqual(self.one(self.diagnose(), "github-repository")["status"], "fail")
        self.assertEqual(self.one(self.diagnose(), "github-permissions")["status"], "skip")

    def test_local_state_symlink_and_ignore(self):
        local = self.root / ".ub-agents"
        self.assertIn("created at launch", self.one(self.diagnose(), "local-state")["message"])
        self.assertFalse(local.exists())
        self.assertEqual(self.one(self.diagnose(access=lambda *_: False), "local-state")["status"], "fail")
        local.mkdir(mode=0o700)
        self.assertEqual(self.one(self.diagnose(), "local-state")["status"], "ok")
        self.assertEqual(self.one(self.diagnose(access=lambda *_: False), "local-state")["status"], "fail")
        local.rmdir()
        local.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.one(self.diagnose(), "local-state")["status"], "fail")
        local.unlink()
        local.write_text("not a directory")
        self.assertEqual(self.one(self.diagnose(), "local-state")["status"], "fail")
        self.runner.responses[("git", "-C", str(self.root), "check-ignore", "-q", ".ub-agents/")] = subprocess.CompletedProcess([], 1, '', '')
        self.assertEqual(self.one(self.diagnose(), "local-state-ignored")["status"], "fail")

    def test_different_runtime_no_alternative_and_partial_coverage(self):
        self.path.write_text(self.path.read_text() + '''  reviewer:
    runtime: codex:model-a:low
    trigger: needs-review
    outcomes: {done: {}}
    instructions: instructions.md
    different-runtime-from: worker
''')
        result = self.diagnose()
        self.assertFalse(result["ok"])
        independence = self.checks(result, "different-runtime-from")
        self.assertEqual([c["status"] for c in independence], ["warn", "fail"])
        self.assertIn("worker", independence[0]["message"])
        self.assertEqual(independence[0]["agent"], "reviewer")
        self.path.write_text(self.path.read_text().replace("runtime: codex:model-a:low", "runtime: claude:model-b:low")
                             .replace("runtime: codex:model-a:high", "runtime: [codex:model-a:high, claude:model-b:high]"))
        result = self.diagnose()
        self.assertTrue(result["ok"])
        self.assertEqual([c["status"] for c in self.checks(result, "different-runtime-from")], ["warn", "ok"])
        self.missing.add("claude")
        self.assertFalse(self.diagnose()["ok"])

    def test_ps_unavailable_failure_bad_output_and_missing_self(self):
        for response in (FileNotFoundError("ps"), subprocess.CompletedProcess([], 9, "", "sk-private"),
                         "bad output", "bad 1 S\n", "123456789 1 S\n", subprocess.TimeoutExpired("ps", 20)):
            with self.subTest(response=response):
                self.runner.responses[("ps", "-axo", "pid=,pgid=,stat=")] = response
                check = self.one(self.diagnose(), "process-inspection")
                self.assertEqual((check["status"], check["required"]), ("fail", True))
                self.assertIn("sandbox", check["remedy"])

    def test_auth_failure_can_use_second_runtime(self):
        self.path.write_text(self.path.read_text().replace("runtime: codex:model-a:high", "runtime: [codex:model-a:high, claude:model-b:high]"))
        self.runner.responses[("codex", "login", "status")] = subprocess.CompletedProcess([], 2, 'sk-secret', 'ghp-secret')
        result = self.diagnose()
        self.assertTrue(result["ok"])
        self.assertEqual(self.checks(result, "runtime-auth")[0]["status"], "warn")

    def test_no_secrets_writes_new_files_or_mutating_commands(self):
        self.github.auth_error = AgentError("ghp-auth-private sk-github-private")
        self.runner.responses[("codex", "login", "status")] = subprocess.CompletedProcess([], 6, 'sk-custom-private', 'ghp-custom-private')
        before = sorted(p.relative_to(self.root) for p in self.root.rglob("*"))
        result = self.diagnose()
        output = self.capture(result) + self.capture(result, True)
        self.assertNotIn("ghp-", output)
        self.assertNotIn("ghp_", output)
        self.assertNotIn("sk-", output)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(before, sorted(p.relative_to(self.root) for p in self.root.rglob("*")))
        self.assertFalse((self.root / ".ub-agents").exists())
        allowed = {("rev-parse", "--show-toplevel"), ("remote", "get-url", "origin"),
                   ("check-ignore", "-q", ".ub-agents/")}
        for command, kwargs in self.runner.calls:
            self.assertLessEqual(kwargs["timeout"], 20)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            if command[0] == "git":
                self.assertIn(command[3:], allowed)

    def test_direct_command_and_shared_executable_selection(self):
        self.path.write_text('''repository: org/project
agents:
  worker:
    command: [./tools/worker-tool]
    trigger: ready
    outcomes: {done: {}}
''')
        (self.root / "tools").mkdir()
        executable = self.root / "tools" / "worker-tool"
        self.assertEqual(self.one(self.diagnose(), "command")["status"], "fail")
        executable.write_text("synthetic executable")
        executable.chmod(0o700)
        agent = load_config(self.path).agents[0]
        self.assertEqual(agent.command, (str(executable),))
        self.assertEqual(self.one(self.diagnose(), "command")["status"], "ok")
        self.assertIsNone(Coordinator(self.github, "operator").choose_runtime(issue(), agent, []))
        executable.chmod(0o600)
        with self.assertRaises(AgentError):
            Coordinator(self.github, "operator").choose_runtime(issue(), agent, [])
        self.assertEqual(self.one(self.diagnose(), "command")["status"], "fail")

    def test_launch_shares_repository_validation_and_never_runs_check(self):
        def read_git(root, *args):
            self.assertEqual(root, self.root)
            return str(root) if args[0] == "rev-parse" else "git@github.com:org/project.git"
        with patch("ub_agents.execution.git", side_effect=read_git) as git, \
                patch("ub_agents.cli.GitHub", return_value=self.github), \
                patch("ub_agents.cli.Loop") as loop, \
                patch("ub_agents.doctor.Doctor.probe", side_effect=AssertionError("launch must not probe")):
            self.assertEqual(main(["--config", str(self.path), "launch", "--once"]), 0)
            loop.return_value.launch.assert_called_once_with(once=True)
            self.assertEqual([call.args[1:] for call in git.call_args_list],
                             [("rev-parse", "--show-toplevel"), ("remote", "get-url", "origin")])
        for result, expected_calls in ((str(self.root.parent), 1), ("https://github.com/wrong/repo", 2)):
            def mismatch(root, *args):
                if expected_calls == 1 or args[0] == "remote":
                    return result
                return str(root)
            with patch("ub_agents.execution.git", side_effect=mismatch) as git, \
                    patch("ub_agents.cli.GitHub", return_value=self.github), \
                    patch("ub_agents.cli.Loop") as loop, redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--config", str(self.path), "launch", "--once"]), 1)
                loop.return_value.launch.assert_not_called()
                self.assertEqual(git.call_count, expected_calls)

    def test_config_credential_forms_are_redacted(self):
        self.path.write_text("agents: [ghp_yaml-secret, sk-yaml-secret\n")
        output = self.capture(self.diagnose())
        self.assertNotIn("ghp_yaml-secret", output)
        self.assertNotIn("sk-yaml-secret", output)

    def test_environment_failures_and_optional_platform(self):
        from types import SimpleNamespace
        class Version(tuple):
            major, minor, micro = 3, 10, 0
        with patch("ub_agents.doctor.sys.version_info", Version((3, 10, 0))):
            self.assertEqual(self.one(self.diagnose(), "python")["status"], "fail")
        with patch("ub_agents.doctor.sys.platform", "freebsd"):
            self.assertEqual(self.one(self.diagnose(), "platform")["status"], "warn")
        platform_os = SimpleNamespace(name="nt", getpid=os.getpid, access=os.access,
                                      W_OK=os.W_OK, X_OK=os.X_OK)
        with patch("ub_agents.doctor.os", platform_os):
            self.assertEqual(self.one(self.diagnose(), "platform")["status"], "fail")

from contextlib import redirect_stdout, redirect_stderr
from importlib.resources import files
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
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
from ub_agents.launcher_lock import launcher_lock
from tests.support import DoctorGitHub, RecordingDiscussionRunner, RecordingRunner, issue, isolate_observations


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
        self.github.label_names.append('outside-input')
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

    def capture(self, result, json_output=False, verbose=False):
        with redirect_stdout(io.StringIO()) as output:
            render(result, json_output, verbose)
        return output.getvalue()

    def cli(self, json_output=False, verbose=False, *, terminal=False, stdout_terminal=None, ci='', answer=''):
        def respond(question):
            if isinstance(answer, Exception):
                raise answer
            return answer(question) if callable(answer) else answer

        with patch("ub_agents.doctor.subprocess.run", self.runner), \
                patch("ub_agents.doctor.shutil.which", self.which), \
                patch("ub_agents.doctor.GitHub", return_value=self.github), \
                patch("ub_agents.labels.sys.stdin.isatty", return_value=terminal), \
                patch.dict("os.environ", {"CI": ci}), \
                patch("builtins.input", side_effect=respond) as prompt, \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            with patch.object(stdout, "isatty", return_value=terminal if stdout_terminal is None else stdout_terminal):
                code = main(["--config", str(self.path), "doctor"]
                            + (["--json"] if json_output else []) + (["--verbose"] if verbose else []))
        self.assertEqual(stderr.getvalue(), "")
        self.prompt = prompt
        return code, stdout.getvalue()

    def test_success_json_schema_and_stable_human_order(self):
        result = self.diagnose()
        self.assertTrue(result["ok"])
        self.assertEqual(result["version"], 2)
        self.assertEqual([c["id"] for c in result["checks"]][:4], ["python", "platform", "git", "gh"])
        self.assertEqual(len({c["id"] for c in result["checks"]}), len(result["checks"]))
        for check in result["checks"]:
            expected = {"id", "status", "required", "agent", "runtime", "message", "remedy"}
            if check["id"].startswith("github-label:"):
                expected.add("uses")
            self.assertEqual(set(check), expected)
            self.assertIn(check["status"], {"ok", "warn", "fail", "skip"})
            self.assertIsInstance(check["required"], bool)
        self.assertIn("0 required failures, 0 warnings", self.capture(result))
        self.assertEqual(self.cli()[0], 0)
        code, output = self.cli(True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), result)

    def test_launcher_is_informational_online_offline_and_in_every_output_mode(self):
        (self.root / ".ub-agents").mkdir()
        for offline in (False, True):
            baseline = self.diagnose(offline=offline)
            self.assertIsNone(baseline["launcher"])
            self.assertIn("No launcher running on this checkout", self.capture(baseline))
            with launcher_lock(self.root) as owner:
                result = self.diagnose(offline=offline)
                self.assertEqual(result["launcher"], owner)
                self.assertEqual(result["checks"], baseline["checks"])
                self.assertEqual(result["ok"], baseline["ok"])
                for verbose in (False, True):
                    output = self.capture(result, verbose=verbose)
                    for value in (str(owner["pid"]), owner["host"], owner["started_at"]):
                        self.assertIn(value, output)
                self.assertEqual(json.loads(self.capture(result, json_output=True))["launcher"], owner)
            self.assertIsNone(self.diagnose(offline=offline)["launcher"])

    def test_offline_cli_never_reaches_github_network_runtimes_commands_or_prompts(self):
        self.path.write_text(self.path.read_text() + '''    retrospectives: 203
  reviewer:
    runtime: claude:model-b:high
    different-runtime-from: worker
    instructions: instructions.md
    trigger: needs-review
    outcomes: {done: {}}
  command:
    command: [missing-command]
    trigger: ready
    outcomes: {done: {}}
launchers: [operator]
''')
        def only_git(name):
            self.assertEqual(name, "git", "offline must not look for GitHub or agent tools")
            return "/tools/git"

        with patch("ub_agents.doctor.subprocess.run", self.runner), \
                patch("ub_agents.doctor.shutil.which", side_effect=only_git), \
                patch("ub_agents.doctor.GitHub", side_effect=AssertionError("GitHub accessed")), \
                patch("socket.create_connection", side_effect=AssertionError("network accessed")), \
                patch("ub_agents.doctor.Doctor.create_missing_labels", side_effect=AssertionError("label setup")), \
                patch("builtins.input", side_effect=AssertionError("offline prompted")):
            for options in (["--json"], ["--json", "--verbose"], ["--verbose"], []):
                with self.subTest(options=options), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(["doctor", "--offline", "--config", str(self.path), *options]), 0)
                if "--json" in options:
                    result = json.loads(output.getvalue())
                    self.assertTrue(result["ok"])
                    for check in result["checks"]:
                        if check["id"] in {"gh", "control-checkout", "runtime-permissions"} or \
                                check["id"].startswith(("github-", "runtime-", "runtimes:",
                                                        "command:", "different-runtime-from:")):
                            self.assertEqual(check["status"], "skip", check)
                            self.assertIn("offline", check["message"], check)
                    for id in ("python", "platform", "git", "config", "repository-root", "repository-remote",
                               "approvals", "local-state", "local-state-ignored", "process-inspection"):
                        self.assertEqual(self.one(result, id)["status"], "ok")
                    self.assertEqual(self.one(result, "github-retrospectives")["status"], "skip")
                    self.assertEqual(self.one(result, "command")["status"], "skip")
                    self.assertEqual(self.one(result, "different-runtime-from")["status"], "skip")
                else:
                    self.assertIn("0 required failures, 0 warnings", output.getvalue())
                    if options:
                        self.assertIn("Approvals: from repository visibility", output.getvalue())
        self.assertEqual({command for command, _ in self.runner.calls}, {
            ("git", "-C", str(self.root), "rev-parse", "--show-toplevel"),
            ("git", "-C", str(self.root), "remote", "get-url", "origin"),
            ("git", "-C", str(self.root), "check-ignore", "-q", ".ub-agents/"),
            ("ps", "-axo", "pid=,pgid=,stat="),
        })

    def test_offline_retains_required_local_failures_and_exit_status(self):
        for id, command in (
                ("repository-root", ("git", "-C", str(self.root), "rev-parse", "--show-toplevel")),
                ("repository-remote", ("git", "-C", str(self.root), "remote", "get-url", "origin")),
                ("local-state-ignored", ("git", "-C", str(self.root), "check-ignore", "-q", ".ub-agents/")),
                ("process-inspection", ("ps", "-axo", "pid=,pgid=,stat="))):
            with self.subTest(id=id), patch.dict(self.runner.responses, {
                    command: subprocess.CompletedProcess(command, 1, "", "")}):
                result = self.diagnose(offline=True)
                self.assertFalse(result["ok"])
                self.assertEqual(self.one(result, id)["status"], "fail")
                with patch("ub_agents.doctor.subprocess.run", self.runner), \
                        patch("ub_agents.doctor.shutil.which", self.which), \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(["--config", str(self.path), "doctor", "--offline", "--json"]), 1)
                self.assertFalse(json.loads(output.getvalue())["ok"])
        (self.root / "instructions.md").write_bytes(b"\xff")
        self.assertEqual(self.one(self.diagnose(offline=True), "instructions")["status"], "fail")
        self.path.write_text("invalid: configuration\n")
        self.assertEqual(self.one(self.diagnose(offline=True), "config")["status"], "fail")

    def test_offline_passes_in_a_real_git_checkout_without_credentials_or_agent_clis(self):
        from ub_agents.execution import git
        git(self.root, "init")
        git(self.root, "remote", "add", "origin", "https://github.com/org/project.git")
        (self.root / ".gitignore").write_text(".ub-agents/\n")
        which = shutil.which
        with patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}), \
                patch("ub_agents.doctor.shutil.which", side_effect=lambda name: which(name) if name == "git" else None), \
                patch("ub_agents.doctor.GitHub", side_effect=AssertionError("GitHub accessed")), \
                patch("socket.create_connection", side_effect=AssertionError("network accessed")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["doctor", "--offline", "--config", str(self.path)]), 0)
        self.assertIn("0 required failures, 0 warnings", output.getvalue())

    def test_doctor_uses_local_runtime_replacement_and_names_invalid_local_file(self):
        local = self.root / 'ub-agents.local.yaml'
        local.write_text('agents: {worker: {runtime: claude:machine:low}}\n')
        result = self.diagnose()
        self.assertTrue(result['ok'])
        self.assertEqual([check['runtime'] for check in self.checks(result, 'runtime-executable')],
                         ['claude:machine:low'])
        self.assertIn(('claude', 'auth', 'status'), [command for command, _ in self.runner.calls])
        local.write_text('agents: {worker: {runtime: null}}\n')
        failure = self.one(self.diagnose(), 'config')
        self.assertEqual(failure['status'], 'fail')
        self.assertIn(str(local), failure['message'])
        self.assertIn('worker runtime', failure['message'])

    def test_retrospective_board_is_checked_per_agent_without_writes(self):
        self.path.write_text(self.path.read_text() + "    retrospectives: 203\n")
        runner = RecordingDiscussionRunner()
        self.github.discussion = GitHub("org/project", runner).discussion
        result = self.diagnose()
        self.assertTrue(result["ok"])
        self.assertEqual(self.one(result, "github-retrospectives")["status"], "ok")
        for discussion in (None, {"id": "foreign", "url": "https://github.com/stranger/repo/discussions/203"}):
            with self.subTest(discussion=discussion):
                runner.discussion = discussion
                result = self.diagnose()
                self.assertFalse(result["ok"])
                check = self.one(result, "github-retrospectives")
                self.assertEqual((check["status"], check["required"], check["agent"]), ("fail", True, "worker"))
                self.assertIn("worker: discussion #203 in org/project", check["message"])
                self.assertEqual(self.cli()[0], 1)
        runner.lookup_error = "Lookup unavailable"
        self.assertEqual(self.one(self.diagnose(), "github-retrospectives")["status"], "fail")
        self.missing.add("gh")
        calls = len(runner.calls)
        self.assertEqual(self.one(self.diagnose(), "github-retrospectives")["status"], "skip")
        self.assertEqual(len(runner.calls), calls)
        self.assertEqual(runner.writes, [])

    def test_doctor_checks_each_configured_agent_board(self):
        self.path.write_text(self.path.read_text() + '''    retrospectives: 203
  reviewer:
    command: [git, --version]
    trigger: ready
    outcomes: {done: {}}
    retrospectives: 204
''')
        runner = RecordingDiscussionRunner()
        self.github.discussion = GitHub("org/project", runner).discussion
        result = self.diagnose()
        checks = self.checks(result, "github-retrospectives")
        self.assertEqual([(check["agent"], check["status"]) for check in checks],
                         [("worker", "ok"), ("reviewer", "fail")])
        self.assertIn("reviewer: discussion #204", checks[1]["message"])
        self.assertEqual([json.loads(kwargs["input"])["variables"]["number"] for _, kwargs in runner.calls],
                         [203, 204])
        self.assertEqual(runner.writes, [])

    def test_healthy_default_has_one_summary_per_area_and_deduplicates_shared_labels(self):
        worker = self.path.read_text().split("  worker:\n", 1)[1].replace(
            "runtime: codex:model-a:high", "runtime: [codex:model-a:high, claude:model-b:high]")
        worker = worker.replace("trigger: ready", "trigger: [ready, workflow:next]")
        self.path.write_text("repository: org/project\nagents:\n"
                             + "".join(f"  {name}:\n{worker}" for name in ("worker", "reviewer", "preparer", "integrator")))
        self.github.label_names.append("workflow:next")
        result = self.diagnose()
        self.assertTrue(all(c["status"] == "ok" for c in result["checks"]))
        self.assertEqual(len(self.checks(result, "github-label")), 4)
        expected = ("No launcher running on this checkout\n"
                    "ok machine: 7 checks passed\n"
                    "ok configuration: 11 checks passed\n"
                    "ok GitHub: 5 checks passed, 4 labels present\n"
                    "ok runtimes: 20 checks passed\n"
                    "0 required failures, 0 warnings\n")
        self.assertEqual(self.capture(result), expected)
        self.assertEqual(self.cli(), (0, expected))

    def test_default_keeps_every_warning_failure_and_remedy_from_verbose(self):
        self.github.roles["operator"] = "admin"
        self.github.label_names = []
        self.missing.add("codex")
        self.path.write_text(self.path.read_text() + '''  reviewer:
    command: [missing-command]
    trigger: ready
    outcomes: {done: {}}
''')
        self.missing.add("missing-command")
        result = self.diagnose()
        default, verbose = self.capture(result), self.capture(result, verbose=True)
        def attention(output):
            return [line for line in output.splitlines()
                    if line.startswith(("warn ", "fail ", "  remedy:"))]
        # Area grouping may reorder checks, but must retain every label and remedy.
        self.assertCountEqual(attention(default), attention(verbose))
        self.assertEqual(default.splitlines()[-1], verbose.splitlines()[-1])
        label_line = next(line for line in default.splitlines() if line.startswith("fail github-label:ready "))
        self.assertIn("starts worker on an issue", label_line)
        self.assertIn("starts reviewer on an issue", label_line)
        self.assertEqual(default.count("fail github-label:ready "), 1)
        self.assertIn("warn github-launcher-role - Launcher account operator has admin", default)
        self.assertIn("ok GitHub: 4 checks passed\n", default)
        self.assertIn("skip runtimes: 1 skipped\n", default)
        self.assertEqual(self.cli()[0], 1)
        self.assertEqual(self.cli(verbose=True)[0], 1)

    def test_missing_gh_shows_failure_and_github_skip_summary(self):
        self.missing.add("gh")
        result = self.diagnose()
        output = self.capture(result)
        self.assertIn("fail gh - gh is not on PATH\n  remedy: Install GitHub CLI", output)
        self.assertIn("skip GitHub: 5 skipped\n", output)
        self.assertIn("ok configuration: 4 checks passed, 2 skipped\n", output)
        self.assertNotIn("skip github-auth", output)
        self.assertEqual(self.cli()[0], self.cli(verbose=True)[0])
        self.assertEqual(output.splitlines()[-1], self.capture(result, verbose=True).splitlines()[-1])

    def test_mixed_passes_and_skips_are_counted_without_individual_skip_lines(self):
        self.path.write_text(self.path.read_text().replace(
            "runtime: codex:model-a:high", "runtime: [codex:model-a:high, claude:model-b:high]") + '''  command:
    command: [git, --version]
    trigger: ready
    outcomes: {done: {}}
''')
        self.missing.add("codex")
        output = self.capture(self.diagnose())
        self.assertIn("ok configuration: 8 checks passed, 1 skipped\n", output)
        self.assertIn("ok runtimes: 4 checks passed, 1 skipped\n", output)
        self.assertNotIn("skip instructions", output)
        self.assertNotIn("skip runtime-auth", output)

    def test_invalid_configuration_retains_existing_commands_skip_id(self):
        self.path.write_text("agents: [\n")
        result = self.diagnose()
        self.assertEqual(self.one(result, "commands")["status"], "skip")
        self.assertIn("skip runtimes: 3 skipped\n", self.capture(result))
        self.assertIn("skip commands - configuration unavailable\n", self.capture(result, verbose=True))

    def test_json_is_byte_identical_to_existing_rendering_with_or_without_verbose(self):
        for unhealthy in (False, True):
            with self.subTest(unhealthy=unhealthy):
                if unhealthy:
                    self.github.label_names = []
                    self.missing.add("gh")
                result = self.diagnose()
                expected = json.dumps(result, indent=2) + "\n"
                self.assertEqual(self.capture(result, True), expected)
                self.assertEqual(self.capture(result, True, verbose=True), expected)
                self.assertEqual(self.cli(True), self.cli(True, verbose=True))

    def test_verbose_preserves_legacy_line_format_and_original_check_order(self):
        checks = [
            dict(id="runtime-auth:worker:codex:model-a:high", status="skip", required=True,
                 agent="worker", runtime="codex:model-a:high", message="executable unavailable", remedy=None),
            dict(id="github-label:ready", status="fail", required=True,
                 agent=None, runtime=None, message="Label ready is missing", remedy="Create ready", uses=[]),
            dict(id="github-auth", status="ok", required=True,
                 agent=None, runtime=None, message="authenticated as operator", remedy=None),
            dict(id="github-launcher-role", status="warn", required=False,
                 agent=None, runtime=None, message="elevated account", remedy="Use write"),
        ]
        result = {"version": 2, "ok": False, "checks": checks}
        self.assertEqual(self.capture(result, verbose=True),
                         "No launcher running on this checkout\n"
                         "skip runtime-auth:worker:codex:model-a:high worker/codex:model-a:high executable unavailable\n"
                         "fail github-label:ready - Label ready is missing\n"
                         "  remedy: Create ready\n"
                         "ok github-auth - authenticated as operator\n"
                         "warn github-launcher-role - elevated account\n"
                         "  remedy: Use write\n"
                         "1 required failure, 1 warning\n")
        # Areas consisting entirely of failures/warnings have no summary line.
        checks[:] = [c for c in checks if c["status"] in {"warn", "fail"}]
        self.assertEqual(self.capture(result), self.capture(result, verbose=True))

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

    def test_runtime_pauses_are_absent_from_doctor(self):
        from ub_agents.records import timestamp
        from ub_agents.runtime_usage import RuntimeUsage
        now = timestamp()
        usage = RuntimeUsage(lambda: now, output=lambda *_: None)
        usage.limit("codex", now + 300)
        result = self.diagnose()
        self.assertEqual(self.checks(result, "runtime-pause"), [])
        self.assertTrue(result["ok"])
        for json_output in (False, True):
            self.assertNotIn("codex paused", self.capture(result, json_output))
            self.assertNotIn("runtime-pause", self.capture(result, json_output))

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
            self.assertIsNone(check['agent'])
            self.assertEqual([use['agent'] for use in check['uses']], ['worker'])
            self.assertIn(name, check['message'])
            self.assertIn('worker', check['message'])
            self.assertIn(f'gh label create {name} --repo org/project', check['remedy'])
        self.assertEqual(self.github.writes, [])

    def test_missing_stop_label_warns_and_all_present_match_case_insensitively(self):
        self.github.label_names = ['READY']
        result = self.diagnose()
        self.assertTrue(result['ok'])
        labels = self.checks(result, 'github-label')
        self.assertEqual([check['status'] for check in labels], ['ok', 'warn', 'warn'])
        self.assertEqual(labels[1]['required'], False)
        self.assertIn('needs-human', labels[1]['remedy'])
        self.github.label_names.extend(['NEEDS-HUMAN', 'OUTSIDE-INPUT'])
        self.assertTrue(all(check['status'] == 'ok' for check in self.checks(self.diagnose(), 'github-label')))
        self.assertEqual(self.github.writes, [])

    def test_stop_label_used_by_a_transition_is_one_required_check_with_both_uses(self):
        self.path.write_text(self.path.read_text().replace('    outcomes: {done: {}}\n', '''    outcomes:
      needs-human: {add: [needs-human]}
'''))
        self.github.label_names = ['ready', 'outside-input']
        result = self.diagnose()
        self.assertFalse(result['ok'])
        missing = [check for check in self.checks(result, 'github-label') if check['status'] != 'ok']
        self.assertEqual([(check['status'], check['agent']) for check in missing],
                         [('fail', None)])
        self.assertEqual(missing[0]['uses'], [
            {'agent': None, 'meaning': 'parks an issue or PR until a person decides', 'required': False},
            {'agent': 'worker', 'meaning': 'added when worker reports needs-human', 'required': True},
        ])
        self.assertIn('parks an issue or PR until a person decides; added when worker reports needs-human',
                      missing[0]['message'])
        self.assertIn('1 required failure, 0 warnings', self.capture(result))

    def test_shared_missing_label_has_one_line_remedy_and_json_result_for_all_uses(self):
        self.path.write_text(self.path.read_text().replace('outcomes: {done: {}}',
                                                         'outcomes: {done: {remove: [READY]}}') + '''  reviewer:
    command: [git, --version]
    trigger: READY
    outcomes: {done: {}}
''')
        self.github.label_names = []
        result = self.diagnose()
        labels = self.checks(result, 'github-label')
        self.assertEqual([check['id'] for check in labels],
                         ['github-label:ready', 'github-label:needs-human', 'github-label:outside-input'])
        self.assertIsNone(labels[0]['agent'])
        self.assertEqual(labels[0]['uses'], [
            {'agent': 'worker', 'meaning': 'starts worker on an issue or PR', 'required': True},
            {'agent': 'reviewer', 'meaning': 'starts reviewer on an issue or PR', 'required': True},
            {'agent': 'worker', 'meaning': 'removed when worker reports done', 'required': True},
        ])
        self.assertEqual(labels[1]['uses'], [
            {'agent': None, 'meaning': 'parks an issue or PR until a person decides', 'required': False},
        ])
        for verbose in (False, True):
            output = self.capture(result, verbose=verbose)
            lines = [line for line in output.splitlines() if line.startswith('fail github-label:ready ')]
            self.assertEqual(len(lines), 1)
            for use in labels[0]['uses']:
                self.assertIn(use['meaning'], lines[0])
            self.assertEqual(output.count('gh label create ready '), 1)
            self.assertEqual(output.count('gh label create needs-human '), 1)
            self.assertEqual(output.splitlines()[-1], '1 required failure, 2 warnings')
        code, output = self.cli(json_output=True)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), result)

    def test_confirmed_creation_follows_report_and_rereads_labels_before_final_counts(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                self.github.label_names = ['unrelated']
                self.github.reads.clear()
                self.github.writes.clear()

                def answer(question):
                    report = sys.stdout.getvalue()
                    self.assertIn('1 required failure, 2 warnings', report)
                    self.assertIn('starts worker on an issue', report)
                    self.assertIn('parks an issue or PR until a person decides', report)
                    self.assertEqual(self.github.writes, [])
                    return ' YeS '

                code, output = self.cli(terminal=True, answer=answer, verbose=verbose)
                self.prompt.assert_called_once_with('Create these 3 labels on org/project? [y/N] ')
                self.assertEqual(code, 0)
                self.assertEqual([write[1] for write in self.github.writes], ['ready', 'needs-human', 'outside-input'])
                self.assertEqual(self.github.reads.count('repos/org/project/labels'), 2)
                self.assertEqual(self.github.reads.count('user'), 1)
                self.assertEqual(output.count('fail github-label:ready '), 1)
                self.assertIn('Created label ready on org/project.', output)
                self.assertEqual(output.splitlines()[-1], '0 required failures, 0 warnings')

    def test_confirmed_creation_never_changes_existing_labels(self):
        self.github.label_names = ['READY', 'unrelated']
        code, output = self.cli(terminal=True, answer='y')
        self.assertEqual(code, 0)
        self.assertEqual([write[1] for write in self.github.writes], ['needs-human', 'outside-input'])
        self.assertEqual(self.github.label_names, ['READY', 'unrelated', 'needs-human', 'outside-input'])
        self.assertEqual(self.github.reads.count('repos/org/project/labels'), 2)
        self.assertEqual(output.splitlines()[-1], '0 required failures, 0 warnings')

    def test_color_remedies_and_confirmed_creation_exclude_priority_labels(self):
        self.path.write_text(self.path.read_text().replace('outcomes: {done: {}}', '''outcomes:
      done:
        add: [needs-preparation, NEEDS-REVIEW, needs-changes, ready-to-merge, custom-destination]''') + '''queue:
  priority:
    labels: [prio:urgent, prio:high, prio:medium, prio:low]
''')
        self.github.label_names = []
        expected = {'ready': '0e8a16', 'needs-preparation': 'c5def5', 'NEEDS-REVIEW': 'fbca04',
                    'needs-changes': 'd93f0b', 'ready-to-merge': '1d76db', 'custom-destination': '1d76db',
                    'needs-human': 'b60205', 'outside-input': '1d76db'}
        checks = self.checks(self.diagnose(), 'github-label')
        self.assertEqual({check['id'].removeprefix('github-label:') for check in checks}, set(expected))
        for check in checks:
            color = expected[check['id'].removeprefix('github-label:')]
            self.assertTrue(check['remedy'].endswith(f'--color {color}'), check['remedy'])
        code, _ = self.cli(terminal=True, answer='yes')
        self.assertEqual(code, 0)
        self.assertEqual({write[1]: write[3] for write in self.github.writes}, expected)

    def test_second_label_read_controls_exit_status_even_after_successful_writes(self):
        self.github.label_names = []
        with patch.object(self.github, 'labels', side_effect=[[], ['needs-human']]) as labels:
            code, output = self.cli(terminal=True, answer='yes')
        self.assertEqual(code, 1)
        self.assertEqual(labels.call_count, 2)
        self.assertEqual(len(self.github.writes), 3)
        self.assertEqual(output.splitlines()[-1], '1 required failure, 1 warning')

    def test_unreadable_second_label_read_fails_without_exposing_private_errors(self):
        self.github.label_names = []
        with patch.object(self.github, 'labels', side_effect=[[], AgentError('ghp_private')]):
            code, output = self.cli(terminal=True, answer='yes')
        self.assertEqual(code, 1)
        self.assertIn('fail github-labels - GitHub labels failed', output)
        self.assertNotIn('ghp_private', output)
        self.assertEqual(output.splitlines()[-1], '1 required failure, 0 warnings')

    def test_second_read_reports_new_missing_labels_and_quota_warnings(self):
        self.github.label_names = ['needs-human']
        responses = iter([['needs-human'], []])

        def labels():
            names = next(responses)
            if not names:
                self.github.quota_headers['x-ratelimit-remaining'] = '0'
            return names

        with patch.object(self.github, 'labels', side_effect=labels):
            code, output = self.cli(terminal=True, answer='yes')
        self.assertEqual(code, 1)
        self.assertEqual(output.count('fail github-label:ready '), 1)
        self.assertEqual(output.count('warn github-label:needs-human '), 1)
        self.assertIn('warn github-rate-limit - 0 of 5000 requests remaining', output)
        self.assertEqual(output.splitlines()[-1], '1 required failure, 3 warnings')

    def test_decline_default_unrecognized_answer_and_eof_keep_commands_without_writes(self):
        for answer in ('no', '', 'maybe', EOFError()):
            with self.subTest(answer=answer):
                self.github.label_names = []
                self.github.reads.clear()
                code, output = self.cli(terminal=True, answer=answer)
                self.prompt.assert_called_once_with('Create these 3 labels on org/project? [y/N] ')
                self.assertEqual(code, 1)
                self.assertEqual(self.github.writes, [])
                self.assertEqual(self.github.reads.count('repos/org/project/labels'), 1)
                self.assertEqual(output.count('gh label create '), 3)
                self.assertEqual(output.splitlines()[-1], '1 required failure, 2 warnings')

    def test_pipes_ci_and_json_never_prompt_or_create_labels(self):
        for terminal, stdout_terminal, ci, json_output in (
                (False, True, '', False), (True, False, '', False),
                (True, True, 'true', False), (True, True, '0', False), (True, True, '', True)):
            with self.subTest(terminal=terminal, stdout_terminal=stdout_terminal, ci=ci, json_output=json_output):
                self.github.label_names = []
                self.github.reads.clear()
                code, output = self.cli(terminal=terminal, stdout_terminal=stdout_terminal, ci=ci,
                                        json_output=json_output, answer='yes')
                self.assertEqual(code, 1)
                self.prompt.assert_not_called()
                self.assertEqual(self.github.writes, [])
                self.assertEqual(self.github.reads.count('repos/org/project/labels'), 1)
                self.assertEqual(output.count('gh label create '), 3)

    def test_present_or_unreadable_labels_do_not_prompt(self):
        for error in (None, AgentError('unavailable')):
            with self.subTest(error=error):
                self.github.label_error = error
                self.cli(terminal=True, answer='yes')
                self.prompt.assert_not_called()
                self.assertEqual(self.github.writes, [])

    def test_unreadable_labels_fail_and_hide_private_errors(self):
        for error in (AgentError('ghp_private sk-private'), subprocess.TimeoutExpired('gh', 20)):
            self.github.label_error = error
            result = self.diagnose()
            self.assertFalse(result['ok'])
            check = self.one(result, 'github-labels')
            self.assertEqual((check['status'], check['required']), ('fail', True))
            self.assertNotIn('private', self.capture(result))
            self.assertEqual(self.github.writes, [])

    def test_runtime_permissions_groups_agents_without_args_into_one_warning(self):
        self.path.write_text(self.path.read_text().replace('    runtime-args: [--sandbox, danger-full-access]\n', '') + '''  reviewer:
    runtime: [codex:model-a:high, claude:model-b:high]
    trigger: needs-review
    outcomes: {done: {}}
    instructions: instructions.md
  command:
    command: [git, --version]
    trigger: ready
    outcomes: {done: {}}
  configured:
    runtime: claude:model-c:high
    runtime-args: [--permission-mode, acceptEdits]
    trigger: ready
    outcomes: {done: {}}
    instructions: instructions.md
''')
        result = self.diagnose()
        self.assertTrue(result['ok'])
        check = self.one(result, 'runtime-permissions')
        self.assertEqual((check['id'], check['status'], check['required'], check['agent'], check['runtime']),
                         ('runtime-permissions', 'warn', False, None, None))
        self.assertIn('Agents without runtime-args: worker, reviewer;', check['message'])
        self.assertNotIn('command', check['message'])
        self.assertNotIn('configured', check['message'])
        url = 'https://github.com/uberblick-ai/ub-agents/blob/main/docs/configuration.md#runtime-permissions'
        self.assertIn(url, check['remedy'])
        code, output = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(output.count('warn runtime-permissions '), 1)
        self.assertEqual(output.count(url), 1)
        self.assertIn('0 required failures, 1 warning', output)
        self.assertEqual(self.capture(result, verbose=True).count('warn runtime-permissions '), 1)
        self.assertEqual(self.github.writes, [])

    def test_missing_only_runtime_is_required_and_names_agent(self):
        self.missing.add("codex")
        result = self.diagnose()
        check = self.one(result, "runtime-executable")
        self.assertEqual((check["status"], check["required"], check["agent"]), ("fail", True, "worker"))
        self.assertEqual(self.one(result, "runtime-auth")["status"], "skip")
        self.assertFalse(result["ok"])
        self.assertFalse(any(c[0][0] == "codex" for c in self.runner.calls))

    def test_runtime_permissions_warns_for_uncovered_or_empty_cli_mapping_entries(self):
        base = self.path.read_text().replace("runtime: codex:model-a:high",
                                             "runtime: [codex:model-a:high, claude:model-b:high, claude:model-c:low]")
        for arguments, warns in (
                ("{}", True), ("{codex: [--sandbox, danger-full-access]}", True),
                ("{claude: [--permission-mode, acceptEdits]}", True),
                ("{codex: [--sandbox, danger-full-access], claude: []}", True),
                ("{codex: [], claude: [--permission-mode, acceptEdits]}", True),
                ("{codex: [--sandbox, danger-full-access], claude: [--permission-mode, acceptEdits]}", False),
                ("[--add-dir, '{scratch}']", False)):
            with self.subTest(arguments=arguments):
                self.path.write_text(base.replace("[--sandbox, danger-full-access]", arguments))
                result = self.diagnose()
                self.assertTrue(result["ok"])
                checks = self.checks(result, "runtime-permissions")
                self.assertEqual(len(checks), int(warns))
                if warns:
                    check = checks[0]
                    self.assertEqual((check["status"], check["required"]), ("warn", False))
                    self.assertIn("Agents without runtime-args: worker;", check["message"])

    def test_runtime_permissions_use_top_level_defaults_and_agent_replacements(self):
        base = self.path.read_text().replace("    runtime-args: [--sandbox, danger-full-access]\n", "").replace(
            "runtime: codex:model-a:high", "runtime: [codex:model-a:high, claude:model-b:high]")
        full = "{codex: [--sandbox, danger-full-access], claude: [--permission-mode, acceptEdits]}"
        for defaults, override, warns in (
                (full, None, False),
                ("{codex: [--sandbox, danger-full-access]}", None, True),
                ("{codex: [--sandbox, danger-full-access], claude: []}", None, True),
                (full, "[]", True), (full, "{}", True),
                (full, "{codex: [--sandbox, read-only]}", True),
                ("{}", full, False), (full, "[--add-dir, '{scratch}']", False)):
            with self.subTest(defaults=defaults, override=override):
                content = base.replace("agents:\n", f"runtime-args: {defaults}\nagents:\n")
                if override is not None:
                    content += f"    runtime-args: {override}\n"
                self.path.write_text(content)
                result = self.diagnose()
                self.assertTrue(result["ok"])
                checks = self.checks(result, "runtime-permissions")
                self.assertEqual(len(checks), int(warns))
                if warns:
                    self.assertIn("Agents without runtime-args: worker;", checks[0]["message"])
                    self.assertIn("top-level runtime-args or agent overrides", checks[0]["remedy"])

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

    def test_before_init_names_selected_configuration_and_uses_singular_failure(self):
        self.path.unlink()
        for name in ('ub-agents.yaml', 'workflow.yaml'):
            with self.subTest(name=name):
                self.path = self.root / name
                result = self.diagnose()
                self.assertEqual(self.one(result, 'config')['message'], f'no {name} here; run ub-agents init')
                code, output = self.cli(terminal=True, answer='yes')
                self.assertEqual(code, 1)
                self.assertEqual(output.splitlines()[-1], '1 required failure, 0 warnings')
                self.prompt.assert_not_called()

    def test_final_counts_pluralize_failures_and_warnings_independently(self):
        self.github.label_names = []
        self.missing.add('git')
        self.assertEqual(self.capture(self.diagnose()).splitlines()[-1], '2 required failures, 2 warnings')

    def test_outside_input_is_optional_and_only_listed_with_effective_approvals_on(self):
        for visibility, override, listed in (('public', None, True), ('private', None, False),
                                             ('internal', None, False), ('private', 'on', True),
                                             ('public', 'off', False)):
            with self.subTest(visibility=visibility, override=override):
                self.setUp()
                self.github.metadata['visibility'] = visibility
                if override:
                    self.path.write_text(self.path.read_text() + f'approvals: {override}\n')
                self.github.label_names.remove('outside-input')
                result = self.diagnose()
                checks = [check for check in self.checks(result, 'github-label')
                          if check['id'] == 'github-label:outside-input']
                self.assertEqual(bool(checks), listed)
                self.assertTrue(result['ok'])
                if listed:
                    self.assertEqual((checks[0]['status'], checks[0]['required']), ('warn', False))
                    self.assertIn('gh label create outside-input', checks[0]['remedy'])

    def test_instruction_read_failure(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path.name == "instructions.md":
                raise PermissionError("unreadable")
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            self.assertEqual(self.one(self.diagnose(), "instructions")["status"], "fail")

    def test_shared_instruction_failures_are_required_and_readable_file_passes(self):
        base = self.path.read_text()
        policy = self.root / 'policy.md'
        policy.write_text('Project policy')
        for value, content in (('missing.md', b'policy'), ('../outside.md', b'policy'),
                               ('policy.md', b'\xff'), ('policy.md', b'policy')):
            with self.subTest(value=value, content=content):
                self.path.write_text(base + f'shared-instructions: {value}\n')
                policy.write_bytes(content)
                result = self.diagnose()
                valid = value == 'policy.md' and content == b'policy'
                self.assertEqual(result['ok'], valid)
                if valid:
                    self.assertEqual(len(self.checks(result, 'instructions')), 2)
                else:
                    self.assertTrue(any(c['required'] and c['status'] == 'fail' for c in result['checks']))
        original = Path.read_text
        def denied(path, *args, **kwargs):
            if path == policy:
                raise PermissionError('Synthetic unreadable policy')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', denied):
            result = self.diagnose()
        self.assertFalse(result['ok'])
        self.assertTrue(any(c['required'] and c['status'] == 'fail' and
                            'shared-instructions is unreadable' in c['message'] for c in result['checks']))

    def test_shared_policy_warns_once_per_placeholder_online_and_offline_without_failing(self):
        policy = self.root / 'policy.md'
        self.path.write_text(self.path.read_text() + 'shared-instructions: policy.md\n')
        placeholders = ('@org/maintainers', '<project build command>', '<project test command>',
                        '<project lint or validation command>')
        policy.write_text('\n'.join(placeholders * 2))
        for offline in (False, True):
            with self.subTest(offline=offline):
                result = self.diagnose(offline=offline)
                self.assertTrue(result['ok'])
                checks = self.checks(result, 'policy-placeholder')
                self.assertEqual(len(checks), 4)
                self.assertEqual(len({c['id'] for c in checks}), 4)
                for placeholder in placeholders:
                    check, = [c for c in checks if placeholder in c['message']]
                    self.assertEqual((check['status'], check['required']), ('warn', False))
                    self.assertIn('policy.md', check['message'])
                output = self.capture(result)
                self.assertIn('configuration', output.lower())
                self.assertIn('4 warnings', output)

    def test_starter_merging_warns_with_any_init_owner_and_ignores_line_wrapping(self):
        policy = self.root / 'policy.md'
        self.path.write_text(self.path.read_text() + 'shared-instructions: policy.md\n')
        starter = files('ub_agents').joinpath('templates/ub_agents.md').read_text()
        for owner in ('@org/maintainers', '@org/core.team', '@operator'):
            for offline in (False, True):
                with self.subTest(owner=owner, offline=offline):
                    content = starter.replace('@org/maintainers', owner)
                    # Reflow the section while preserving its Markdown heading boundaries.
                    content = content.replace('by squash, once the checks above and any required CI\nchecks',
                                              'by\n squash, once the checks above\n and any required CI checks')
                    policy.write_text(content)
                    result = self.diagnose(offline=offline)
                    self.assertTrue(result['ok'])
                    check = self.one(result, 'policy-merging')
                    self.assertEqual((check['status'], check['required']), ('warn', False))
                    self.assertIn('starter Merging section', check['message'])
                    self.assertEqual(len(self.checks(result, 'policy-placeholder')), int(owner == '@org/maintainers'))

    def test_public_starter_merging_rule_still_warns_after_reflow(self):
        policy = self.root / 'policy.md'
        self.path.write_text(self.path.read_text() + 'shared-instructions: policy.md\n')
        starter = files('ub_agents').joinpath('templates/ub_agents.md').read_text()
        policy.write_text(starter.replace('- changes setup, CI or release tooling;',
            '- changes setup, CI or release tooling;\n'
            '- is labelled `outside-input`\n  (built from outside text);'))
        self.assertEqual(self.one(self.diagnose(offline=True), 'policy-merging')['status'], 'warn')

    def test_customized_or_absent_merging_section_has_no_starter_warning(self):
        policy = self.root / 'policy.md'
        self.path.write_text(self.path.read_text() + 'shared-instructions: policy.md\n')
        starter = files('ub_agents').joinpath('templates/ub_agents.md').read_text().replace('@org/maintainers', '@owner')
        project_policy = Path(__file__).resolve().parents[1] / '.agents/ub_agents.md'
        for text in (starter.replace('any required CI\nchecks', 'the required Test result\ncheck'),
                     project_policy.read_text(), 'Custom policy without a Merging section'):
            with self.subTest(text=text):
                policy.write_text(text)
                result = self.diagnose(offline=True)
                self.assertTrue(result['ok'])
                self.assertEqual(self.checks(result, 'policy-merging'), [])
                self.assertEqual(self.checks(result, 'policy-placeholder'), [])

    def test_starter_checks_apply_only_to_the_configured_shared_file(self):
        (self.root / 'instructions.md').write_text('@org/maintainers <project build command>')
        result = self.diagnose(offline=True)
        self.assertTrue(result['ok'])
        self.assertEqual(self.checks(result, 'policy-placeholder'), [])
        self.assertEqual(self.checks(result, 'policy-merging'), [])

    def test_repository_root_remote_and_github_mismatches(self):
        config = load_config(self.path)
        for remote in ('https://github.com/org/project', 'git@github.com:org/project.git',
                       'ssh://git@github.com/org/project.git'):
            for suffix in ('', '/', '.git', '.git/'):
                with self.subTest(remote=remote, suffix=suffix):
                    self.assertTrue(all(error is None for _, error in repository_checks(
                        config, lambda root, *args: str(root) if args[0] == 'rev-parse'
                        else remote.removesuffix('.git') + suffix, self.runner)))
        self.assertEqual(self.runner.calls, [])
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
        self.assertIn("write access", self.one(result, "github-permissions")["remedy"])
        self.github.repository_error = AgentError("sk-repository-secret")
        self.assertEqual(self.one(self.diagnose(), "github-repository")["status"], "fail")
        self.assertEqual(self.one(self.diagnose(), "github-permissions")["status"], "skip")

    def test_ssh_alias_matches_repository_in_doctor_online_and_offline(self):
        remote_command = ("git", "-C", str(self.root), "remote", "get-url", "origin")
        ssh_command = ("ssh", "-G", "--", "github.com-kihaki")
        self.path.write_text(self.path.read_text().replace('org/project', 'back-channel/campfire-android'))
        self.github.metadata['full_name'] = 'back-channel/campfire-android'
        self.runner.responses[ssh_command] = 'host github.com-kihaki\nuser git\nhostname GitHub.COM\nport 22\n'
        for remote in ('git@github.com-kihaki:back-channel/campfire-android',
                       'ssh://git@github.com-kihaki/back-channel/campfire-android'):
            for suffix in ('', '.git', '/', '.git/'):
                for offline in (False, True):
                    with self.subTest(remote=remote, suffix=suffix, offline=offline):
                        self.runner.calls.clear()
                        self.runner.responses[remote_command] = remote + suffix
                        result = self.diagnose(offline=offline)
                        self.assertTrue(result['ok'])
                        self.assertEqual(self.one(result, 'repository-remote')['status'], 'ok')
                        calls = [kwargs for command, kwargs in self.runner.calls if command == ssh_command]
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(calls[0]['stdin'], subprocess.DEVNULL)
                        self.assertEqual(calls[0]['timeout'], 20)

    def test_ssh_alias_resolution_failures_preserve_remote_error_and_offer_ssh_remedy(self):
        remote_command = ("git", "-C", str(self.root), "remote", "get-url", "origin")
        ssh_command = ("ssh", "-G", "--", "github-work")
        self.runner.responses[remote_command] = 'git@github-work:org/project.git'
        for response in ('hostname other.example\n', 'host github-work\n', 'hostname github.com.evil.example\n',
                         FileNotFoundError('ssh'), subprocess.TimeoutExpired('ssh', 20),
                         UnicodeError('private output'),
                         subprocess.CompletedProcess([], 1, 'hostname github.com\n', 'private error')):
            for offline in (False, True):
                with self.subTest(response=response, offline=offline):
                    self.runner.responses[ssh_command] = response
                    check = self.one(self.diagnose(offline=offline), 'repository-remote')
                    self.assertEqual((check['status'], check['required']), ('fail', True))
                    self.assertEqual(check['message'], 'origin must point to the configured GitHub repository')
                    self.assertIn('git@github.com:org/project.git', check['remedy'])
                    self.assertIn('SSH host alias for github.com', check['remedy'])

    def test_wrong_repository_and_unsupported_remote_forms_never_consult_ssh(self):
        remote_command = ("git", "-C", str(self.root), "remote", "get-url", "origin")
        for remote in ('git@github-work:wrong/repo.git', 'ssh://git@github-work/wrong/repo.git',
                       'https://github-work/org/project.git', 'user@github-work:org/project.git',
                       'ssh://user@github-work/org/project.git', 'github-work:org/project.git',
                       'ssh://git@github-work:22/org/project.git'):
            with self.subTest(remote=remote):
                self.runner.calls.clear()
                self.runner.responses[remote_command] = remote
                check = self.one(self.diagnose(offline=True), 'repository-remote')
                self.assertEqual(check['status'], 'fail')
                self.assertEqual(check['message'], 'origin must point to the configured GitHub repository')
                self.assertFalse(any(command[0] == 'ssh' for command, _ in self.runner.calls))

    def test_launch_accepts_ssh_alias_using_local_resolution(self):
        def read_git(root, *args):
            return str(root) if args[0] == 'rev-parse' else 'ssh://git@github-work/org/project.git'
        self.runner.responses[("ssh", "-G", "--", "github-work")] = 'hostname github.com\n'
        with patch('ub_agents.execution.git', side_effect=read_git), \
                patch('ub_agents.execution.subprocess.run', self.runner), \
                patch('ub_agents.refresh.git', side_effect=lambda root, *args:
                      self.runner(['git', '-C', str(root), *args]).stdout.strip()), \
                patch('ub_agents.cli.GitHub', return_value=self.github), \
                patch('ub_agents.cli.Loop') as loop:
            self.assertEqual(main(['--config', str(self.path), 'launch', '--once']), 0)
            loop.return_value.launch.assert_called_once_with(once=True)
        self.assertTrue(any(command == ('ssh', '-G', '--', 'github-work')
                            for command, _ in self.runner.calls))

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

    def test_issue_runtime_independence_checks_every_named_source(self):
        self.github.label_names.append("prepare")
        self.path.write_text(self.path.read_text() + '''  preparer:
    runtime: codex:model-c:high
    trigger: prepare
    outcomes: {done: {}}
    instructions: instructions.md
    kind: issue
  reviewer:
    runtime: claude:model-b:high
    trigger: needs-review
    outcomes: {done: {}}
    instructions: instructions.md
    kind: issue
    different-runtime-from: [worker, preparer]
''')
        result = self.diagnose()
        self.assertTrue(result["ok"])
        checks = self.checks(result, "different-runtime-from")
        self.assertEqual([(c["status"], c["message"]) for c in checks], [
            ("ok", "independent execution available after worker"),
            ("ok", "independent execution available after preparer")])
        self.assertEqual(len({c["id"] for c in checks}), len(checks))

        configured = load_config(self.path)
        co = Coordinator(self.github, "operator", lambda: 1000, runtime_available=self.which)
        item = issue(labels=("ready", "prepare"))
        self.github.items[1] = item
        for source in configured.agents[:2]:
            lease = co.claim(co.plan(item, source, ()))
            co.update(lease, state="running", started=True)
            outcome = co.report(lease, "success", "Prepared", outcome="done")
            co.accept(lease, outcome)
            co.release(lease, "success", "Prepared")
        plan = co.plan(issue(labels=("needs-review",)), configured.agents[-1], ())
        self.assertEqual((plan.state, plan.runtime.cli), ("ready", "claude"))

        self.path.write_text(self.path.read_text().replace("runtime: codex:model-c:high", "runtime: claude:model-c:high"))
        result = self.diagnose()
        self.assertFalse(result["ok"])
        checks = self.checks(result, "different-runtime-from")
        self.assertEqual([c["status"] for c in checks], ["ok", "warn", "fail"])
        self.assertIn("preparer", checks[-1]["message"])

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
                   ("rev-parse", "--abbrev-ref", "HEAD"),
                   ("--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"),
                   ("rev-list", "--left-right", "--count", "HEAD...refs/remotes/origin/main"),
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
                patch("ub_agents.refresh.git", side_effect=lambda root, *args:
                      self.runner(["git", "-C", str(root), *args]).stdout.strip()), \
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

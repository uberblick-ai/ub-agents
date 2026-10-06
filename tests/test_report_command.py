from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ub_agents
from ub_agents.cli import main
from ub_agents.config import Runtime, load_config
from ub_agents.coordination import Coordinator
from ub_agents.loop import Loop
from ub_agents.records import MARKER, record_version
from ub_agents.report_command import launcher_report_command
from tests.support import FakeGitHub, agent, config, issue, run_environment, stub_refresh


class ReportCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_console_and_module_launchers_pin_their_package_despite_login_path_and_imports(self):
        installation = self.root / "launcher installation"
        package = installation / "ub_agents"
        shutil.copytree(Path(ub_agents.__file__).parent, package)
        (package / "__init__.py").write_text('__version__ = "launcher-fixture"\n')
        # Expose the command produced by the fixture's actual launcher process.
        # All other arguments still run the real CLI, including report.
        with (package / "cli.py").open("a") as stream:
            stream.write('''
original_main = main
def main():
    if sys.argv[1:] == ["--test-report-command"]:
        from .report_command import launcher_report_command
        print(launcher_report_command())
        return 0
    return original_main()
''')
        console = installation / "bin" / "ub-agents"
        console.parent.mkdir()
        console.write_text(f"#!{sys.executable}\nfrom ub_agents.cli import main\nraise SystemExit(main())\n")
        console.chmod(0o700)
        other = self.root / "other"
        other.mkdir()
        for name in ("ub-agents", "python", "python3"):
            executable = other / name
            executable.write_text("#!/bin/sh\necho wrong-installation\nexit 99\n")
            executable.chmod(0o700)
        shadow = other / "ub_agents"
        shadow.mkdir()
        (shadow / "__init__.py").write_text('raise RuntimeError("wrong package")\n')
        shell = "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/bash"
        env = {k: v for k, v in os.environ.items() if not k.startswith("UB_AGENTS_")}
        for launcher in ([str(console)], [sys.executable, "-m", "ub_agents"]):
            with self.subTest(launcher=launcher):
                result = subprocess.run(launcher + ["--test-report-command"], cwd=self.root,
                                        env=env | {"PYTHONPATH": str(installation)},
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = result.stdout.strip()
                arguments = shlex.split(report)
                self.assertTrue(Path(arguments[0]).is_absolute())
                self.assertEqual(Path(arguments[-1]).parent, package)
                # Change PATH after the login profile has run, simulating its
                # preference for an unrelated installation without editing it.
                prefix = f"PATH={shlex.quote(str(other))}:/usr/bin:/bin; "
                reporting_env = env | {"PYTHONPATH": str(other)}
                result = subprocess.run([shell, "-lc", prefix + report + " --version"], cwd=other,
                                        env=reporting_env, capture_output=True, text=True, timeout=20)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, "ub-agents launcher-fixture"),
                                 result.stderr)
                result = subprocess.run([shell, "-lc", prefix + report + " report --status retry --summary fixture"],
                                        cwd=other, env=reporting_env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 1)
                self.assertIn("requires a supervised ub-agents assignment", result.stderr)
                self.assertNotIn("wrong", result.stdout + result.stderr)

    def test_each_supervised_execution_receives_the_same_literal_report_command(self):
        stub_refresh(self)
        for cli in (None, "codex", "claude"):
            with self.subTest(cli=cli):
                configured = agent(self.root, kind="issue")
                if cli:
                    configured = replace(configured, command=(), runtimes=(Runtime(cli, "model", "high"),),
                                         runtime_args=("--allowedTools", "Bash({report_command} report *)"))
                github = FakeGitHub(issue())
                loop = Loop(config(self.root, configured), github, "operator", output=lambda *_: None)

                def execute(command, cwd, env, *args, **kwargs):
                    context = json.loads(Path(env["UB_AGENTS_CONTEXT"]).read_text())
                    path = Path(env["UB_AGENTS_RUN_CONFIG"])
                    self.assertEqual(path, args[0] / "run.json")
                    self.assertTrue(path.is_absolute())
                    pinned = json.loads(path.read_text())
                    self.assertEqual({k: pinned[k] for k in ("repository", "run", "assignment", "agent")},
                                     {k: context[k] for k in ("repository", "run", "assignment", "agent")})
                    self.assertEqual(str(pinned["lease_id"]), env["UB_AGENTS_LEASE_ID"])
                    self.assertEqual(str(pinned["assignment"]), env["UB_AGENTS_ASSIGNMENT"])
                    self.assertEqual({k: pinned[k] for k in ("approvals", "trusted-bots", "triggers", "retrospectives")},
                                     {"approvals": "on", "trusted-bots": [], "retrospectives": None,
                                      "triggers": {"issue": ["needs-changes", "ready"], "pr": []}})
                    for removed in ("UB_AGENTS_READ_CONFIG", "UB_AGENTS_RETROSPECTIVE_CONFIG"):
                        self.assertNotIn(removed, env)
                    for removed in ("read-config.json", "retrospective-config.json"):
                        self.assertFalse((path.parent / removed).exists())
                    report = env["UB_AGENTS_REPORT"]
                    self.assertEqual(report, context["report_command"])
                    self.assertEqual(report, launcher_report_command())
                    self.assertTrue(Path(shlex.split(report)[0]).is_absolute())
                    if cli:
                        self.assertIn(f"Bash({report} report *)", command)
                        prompt = args[3]
                        self.assertIn(f"End the run with {report} report", prompt)
                        self.assertNotIn("$UB_AGENTS_REPORT", prompt)
                    else:
                        self.assertIsNone(args[3])
                    loop.coordinator.report(loop.coordinator.history(1)[0], "blocked", "Verified", action="Maintainer: choose A or B; recommend A.")
                    return 0

                with patch("ub_agents.coordination.shutil.which", return_value="installed"), \
                        patch.dict(os.environ, {"UB_AGENTS_REPORT": "/stale/ub-agents",
                                                "UB_AGENTS_READ_CONFIG": "/stale/read.json",
                                                "UB_AGENTS_RETROSPECTIVE_CONFIG": "/stale/retro.json"}), \
                        patch("ub_agents.loop.supervise", side_effect=execute) as executed:
                    self.assertTrue(loop.tick())
                executed.assert_called_once()

    def test_shipped_claude_rules_allow_only_the_specific_report_invocation(self):
        project = Path(__file__).resolve().parent.parent
        configured = load_config(project / "ub-agents.yaml")
        for role in configured.agents:
            if any(runtime.cli == "claude" for runtime in role.runtimes):
                self.assertIn("Bash({report_command} report *)", role.runtime_args)
                self.assertNotIn("Bash(ub-agents *)", role.runtime_args)
                self.assertNotIn("Bash(*ub-agents *)", role.runtime_args)


class ReportVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.github = FakeGitHub(issue())
        self.coordinator = Coordinator(self.github, "operator")
        self.role = agent(self.root)
        self.config = config(self.root, self.role)
        self.lease = self.coordinator.claim(self.coordinator.plan(issue(), self.role, ()))
        self.coordinator.update(self.lease, state="running", started=True)

    def report(self, *, report_command="/launcher/ub-agents", env_overrides=None):
        env = run_environment(self.config, self.role, self.lease)
        env.update(env_overrides or {})
        if report_command:
            env["UB_AGENTS_REPORT"] = report_command
        with patch.dict(os.environ, env, clear=True), patch("ub_agents.cli.GitHub", return_value=self.github), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            code = main(["report", "--status", "blocked", "--summary", "Need a decision",
                         "--action", "Maintainer: choose A or B; recommend A."])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_newer_supervised_format_fails_clearly_without_parsing_or_writing(self):
        version = record_version(MARKER) + 1
        self.github.store[1][0]["body"] = f"<!-- ub-agents:v{version} -->\nunknown future payload"
        writes = list(self.github.writes)
        for report_command in ("/launcher/ub-agents", None):
            with self.subTest(report_command=report_command):
                code, stdout, stderr = self.report(report_command=report_command)
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn(f"record format v{version}, newer", stderr)
                self.assertIn("UB_AGENTS_REPORT", stderr)
                if report_command:
                    self.assertIn("/launcher/ub-agents report", stderr)
                self.assertNotIn("lease was not found", stderr)
                self.assertEqual(self.github.writes, writes)

    def test_other_versions_comments_and_authors_do_not_trigger_the_diagnostic(self):
        future = record_version(MARKER) + 1
        for version, author, targeted in ((2, "operator", True), (future, "outsider", True),
                                          (future, "operator", False)):
            with self.subTest(version=version, author=author, targeted=targeted):
                comment = self.github.store[1][0]
                original = dict(comment)
                comment["body"] = f"<!-- ub-agents:v{version} -->\nunknown payload"
                comment["user"] = {"login": author}
                if not targeted:
                    comment["id"] += 100
                writes = list(self.github.writes)
                code, _, stderr = self.report()
                self.assertEqual(code, 1)
                self.assertIn("lease was not found", stderr)
                self.assertNotIn("newer", stderr)
                self.assertEqual(self.github.writes, writes)
                comment.clear()
                comment.update(original)

    def test_current_format_keeps_normal_reporting(self):
        code, stdout, stderr = self.report()
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "blocked")

    def test_report_takes_assignment_and_lease_id_from_run_json(self):
        code, stdout, stderr = self.report(env_overrides={"UB_AGENTS_ASSIGNMENT": "wrong",
                                                        "UB_AGENTS_LEASE_ID": "wrong"})
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["run"], self.lease["run"])

    def test_report_always_applies_pinned_bots_to_coordination_reads(self):
        self.config = replace(self.config, trusted_bots=("copilot",))
        self.github.roles["copilot"] = "admin"
        self.github.store[1].append({"id": 999, "body": MARKER + "\nmalformed bot record",
                                     "user": {"login": "copilot", "type": "Bot"}})
        code, _, stderr = self.report()
        self.assertEqual((code, stderr), (0, ""))

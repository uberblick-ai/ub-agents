from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.config import load_config
from ub_agents.doctor import Doctor
from ub_agents.doctor_mcp import MCPChecks
from ub_agents.mcp_probe import probe_stdio
from tests.support import RecordingMCPProbe


class MCPChecksTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.project.mkdir()
        self.home.mkdir()
        environment = patch.dict(os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
                                             "CLAUDE_CONFIG_DIR": str(self.home / ".claude")})
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.project / "ub-agents.yaml"
        self.path.write_text('''repository: org/project
shared-instructions: shared.md
agents:
  worker:
    runtime: [claude:model:high, codex:model:high]
    runtime-args:
      claude: [--allowedTools, 'mcp__x__*']
      codex: []
    instructions: role.md
    trigger: ready
    outcomes: {done: {}}
  command:
    command: ['true']
    trigger: ready
    outcomes: {done: {}}
''')
        (self.project / "shared.md").write_text("Shared instructions")
        (self.project / "role.md").write_text("Role instructions")
        self.probe = RecordingMCPProbe()

    def write(self, relative, value):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))

    def run_checks(self, *, offline=False, config=None):
        doctor = Doctor()
        MCPChecks(config or load_config(self.path), probe=self.probe).run(doctor, offline=offline)
        result = doctor.result()
        self.assertTrue(result["ok"])
        self.assertTrue(all(not check["required"] for check in result["checks"]))
        self.assertEqual(len({check["id"] for check in result["checks"]}), len(result["checks"]))
        self.assertTrue(all(check["agent"] != "command" for check in result["checks"]))
        return result

    def checks(self, **kwargs):
        return self.run_checks(**kwargs)["checks"]

    def test_missing_per_agent_runtime_server_and_remedies(self):
        checks = self.checks()
        self.assertEqual([(c["agent"], c["runtime"], c["server"], c["status"]) for c in checks],
                         [("worker", "claude:model:high", "x", "warn"), ("worker", "codex:model:high", "x", "warn")])
        self.assertTrue(all("missing" in check["message"] for check in checks))
        self.assertIn("ub mcp install claude", checks[0]["remedy"])
        self.assertIn("enabledMcpjsonServers", checks[0]["remedy"])
        self.assertIn("committed .claude/settings.json", checks[0]["remedy"])
        self.assertIn("runtime-args with -c mcp_servers.x.command", checks[1]["remedy"])
        self.assertEqual(self.probe.calls, [])

    def test_unapproved_project_claude_and_untrusted_codex_never_start(self):
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "secret-command"}}})
        self.write(".codex/config.toml", '[mcp_servers.x]\ncommand="secret-command"\n')
        checks = self.checks()
        self.assertIn("pending approval", checks[0]["message"])
        self.assertIn("enabledMcpjsonServers", checks[0]["remedy"])
        self.assertIn("unread", checks[1]["message"])
        self.assertIn("-c mcp_servers.x.command", checks[1]["remedy"])
        self.assertEqual(self.probe.calls, [])

    def test_claude_project_or_user_approval_loads_and_lists_tools(self):
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "server", "args": ["serve"]}}})
        for relative in (".claude/settings.json", "../home/.claude/settings.json"):
            with self.subTest(relative=relative):
                self.write(relative, {"enabledMcpjsonServers": ["x"]})
                checks = self.checks()
                self.assertEqual(checks[0]["status"], "ok")
                self.assertIn("loaded; 3 tools", checks[0]["message"])
                (self.project / relative).unlink()
        self.assertEqual(len(self.probe.calls), 2)

    def test_allow_all_approval_and_explicit_denial(self):
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "server"}}})
        self.write(".claude/settings.json", {"enableAllProjectMcpServers": True})
        self.assertEqual(self.checks()[0]["status"], "ok")
        self.probe.calls.clear()
        self.write(".claude/settings.json", {"enableAllProjectMcpServers": True, "disabledMcpjsonServers": ["x"]})
        self.assertIn("pending approval", self.checks()[0]["message"])
        self.assertEqual(self.probe.calls, [])

    def test_codex_cli_overrides_define_server_without_project_trust(self):
        config = load_config(self.path)
        worker = replace(config.agents[0], runtime_args={"codex": (
            "-c", 'mcp_servers.x.command="server"', "--config=mcp_servers.x.args=[\"serve\"]",
            "-c", 'mcp_servers.x.env={TOKEN="private-value"}', "-c", 'mcp_servers.x.cwd="subdir"')})
        self.write(".codex/config.toml", '[mcp_servers.x]\ncommand="untrusted-server"\n')
        result = self.run_checks(config=replace(config, agents=(worker,)))
        check = result["checks"][1]
        self.assertEqual(check["status"], "ok")
        command, cwd, env = self.probe.calls[0]
        self.assertEqual(command, ["server", "serve"])
        self.assertEqual(cwd, str(self.project / "subdir"))
        self.assertEqual(env["TOKEN"], "private-value")
        self.assertNotIn("private-value", json.dumps(result))
        self.assertNotIn("untrusted-server", json.dumps(result))

    def test_approval_mode_alone_does_not_define_codex_server(self):
        config = load_config(self.path)
        worker = replace(config.agents[0], runtime_args=("-c", 'mcp_servers.x.default_tools_approval_mode="approve"'))
        checks = self.checks(config=replace(config, agents=(worker,)))
        self.assertEqual(checks[0]["status"], "warn")
        self.assertIn("missing", checks[0]["message"])
        self.assertIn("-c mcp_servers.x.command", checks[0]["remedy"])
        self.assertEqual(self.probe.calls, [])

    def test_user_codex_config_and_overrides_merge_but_project_does_not(self):
        self.write("../home/.codex/config.toml", '[mcp_servers.x]\ncommand="user-server"\nargs=["user"]\n')
        self.write(".codex/config.toml", '[mcp_servers.x]\ncommand="untrusted-server"\n')
        config = load_config(self.path)
        worker = replace(config.agents[0], runtime_args=("-c", 'mcp_servers.x.args=["override"]'))
        self.assertEqual(self.checks(config=replace(config, agents=(worker,)))[1]["status"], "ok")
        self.assertEqual(self.probe.calls[0][0], ["user-server", "override"])

    def test_claude_explicit_config_strict_and_settings_flags(self):
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "unapproved"}}})
        config = load_config(self.path)
        worker = replace(config.agents[0], runtime_args=("--strict-mcp-config", "--mcp-config",
                         '{"mcpServers":{"x":{"command":"explicit"}}}'))
        self.assertEqual(self.checks(config=replace(config, agents=(worker,)))[0]["status"], "ok")
        self.assertEqual(self.probe.calls[0][0], ["explicit"])
        worker = replace(worker, runtime_args=("--settings", '{"enabledMcpjsonServers":["x"]}'))
        self.assertEqual(self.checks(config=replace(config, agents=(worker,)))[0]["status"], "ok")
        worker = replace(worker, runtime_args=("--setting-sources=user",))
        self.probe.calls.clear()
        self.assertIn("unread", self.checks(config=replace(config, agents=(worker,)))[0]["message"])
        self.assertEqual(self.probe.calls, [])

    def test_parent_and_checkout_local_configuration_are_unread(self):
        self.write("../.codex/config.toml", '[mcp_servers.x]\ncommand="parent"\n')
        self.write("../.mcp.json", {"mcpServers": {"x": {"command": "parent"}}})
        self.write("../home/.claude/.claude.json", {"projects": {str(self.project): {
            "mcpServers": {"x": {"command": "local"}}, "enabledMcpjsonServers": ["x"]}}})
        checks = self.checks()
        self.assertTrue(all("unread" in check["message"] for check in checks))
        self.assertEqual(self.probe.calls, [])

    def test_control_checkout_approval_and_settings_local_are_not_reused(self):
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "server"}}})
        self.write(".claude/settings.local.json", {"enabledMcpjsonServers": ["x"]})
        self.write("../home/.claude/.claude.json", {"projects": {str(self.project): {
            "enabledMcpjsonServers": ["x"], "hasTrustDialogAccepted": True}}})
        self.assertIn("pending approval", self.checks()[0]["message"])
        self.assertEqual(self.probe.calls, [])

    def test_tool_references_and_project_registrations_expand_scope(self):
        self.write("role.md", "Use mcp__role-server__read and mcp__role-server__write. Ignore prose-server.")
        self.write("shared.md", "Use mcp__shared_server__*. mcp__shared_server__read")
        self.write(".mcp.json", {"mcpServers": {"registered": {"command": "server"}}})
        self.write(".codex/config.toml", '[mcp_servers.codex_registered]\ncommand="server"\n')
        self.write("../home/.codex/config.toml", '[mcp_servers.unrelated]\ncommand="server"\n')
        checks = self.checks()
        self.assertEqual(len(checks), 10)
        self.assertEqual({c["server"] for c in checks},
                         {"x", "role-server", "shared_server", "registered", "codex_registered"})
        self.assertEqual(self.probe.calls, [])

    def test_shared_effective_configuration_is_probed_once_per_diagnosis(self):
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "server"}}})
        self.write(".claude/settings.json", {"enabledMcpjsonServers": ["x"]})
        config = load_config(self.path)
        worker = config.agents[0]
        reviewer = replace(worker, name="reviewer")
        checks = self.checks(config=replace(config, agents=(worker, reviewer)))
        self.assertEqual(len(checks), 4)
        self.assertEqual(len(self.probe.calls), 1)

    def test_transport_skip_and_offline_never_start_servers(self):
        self.write(".mcp.json", {"mcpServers": {"x": {"type": "http", "url": "https://private/?token=secret"}}})
        self.write(".claude/settings.json", {"enabledMcpjsonServers": ["x"]})
        result = self.run_checks()
        self.assertEqual(result["checks"][0]["status"], "skip")
        self.assertIn("non-stdio", result["checks"][0]["message"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertTrue(all(c["status"] == "skip" and "offline" in c["message"] for c in self.checks(offline=True)))
        self.assertEqual(self.probe.calls, [])

    def test_failures_are_optional_and_name_server_and_reason(self):
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "server"}}})
        self.write(".claude/settings.json", {"enabledMcpjsonServers": ["x"]})
        for reason in ("not found", "exited", "timed out", "handshake failed"):
            with self.subTest(reason=reason):
                self.probe.result = None, reason
                check = self.checks()[0]
                self.assertEqual(check["status"], "warn")
                self.assertIn("x", check["remedy"])
                self.assertIn(reason, check["remedy"])

    def test_environment_expansion_and_codex_forwarding(self):
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "${SERVER:-fallback}", "args": ["${ARG}"],
                                                       "env": {"KEY": "${TOKEN}"}}}})
        self.write(".claude/settings.json", {"enabledMcpjsonServers": ["x"]})
        self.write("../home/.codex/config.toml", '[mcp_servers.x]\ncommand="server"\nenv_vars=["TOKEN"]\n')
        with patch.dict(os.environ, {"ARG": "serve", "TOKEN": "private", "NOT_FOR_CODEX": "private"}):
            checks = self.checks()
        self.assertTrue(all(c["status"] == "ok" for c in checks))
        self.assertEqual(self.probe.calls[0][0], ["fallback", "serve"])
        self.assertEqual(self.probe.calls[0][2]["KEY"], "private")
        self.assertEqual(self.probe.calls[1][2]["TOKEN"], "private")
        self.assertNotIn("NOT_FOR_CODEX", self.probe.calls[1][2])
        self.assertNotIn("private", json.dumps(checks))

    def test_invalid_config_never_echoes_parser_input_or_starts_servers(self):
        self.write(".mcp.json", '{"mcpServers": secret-token}')
        self.write(".codex/config.toml", 'command=secret-token\n')
        result = self.run_checks()
        self.assertTrue(all(c["status"] == "warn" for c in result["checks"]))
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertEqual(self.probe.calls, [])

    def test_malformed_claude_sources_do_not_block_codex_probe(self):
        config = load_config(self.path)
        for path, arguments in (
                (".mcp.json", ()),
                ("../home/.claude/.claude.json", ()),
                ("../home/.claude/settings.json", ()),
                (".claude/settings.json", ()),
                ("extra-mcp.json", ("--mcp-config", "extra-mcp.json")),
                ("extra-settings.json", ("--settings", "extra-settings.json"))):
            with self.subTest(path=path):
                self.write(".mcp.json", {"mcpServers": {"x": {"command": "claude-server"}}})
                self.write(".claude/settings.json", {"enabledMcpjsonServers": ["x"]})
                self.write(path, '{"private": secret-token}')
                worker = replace(config.agents[0], runtime_args={
                    "claude": ("--allowedTools", "mcp__x__*", *arguments),
                    "codex": ("-c", 'mcp_servers.x.command="codex-server"')})
                result = self.run_checks(config=replace(config, agents=(worker,)))
                checks = result["checks"]
                calls = list(self.probe.calls)
                self.probe.calls.clear()
                (self.project / path).unlink()
                self.assertEqual([c["runtime"] for c in checks if c["id"].startswith("runtime-mcp-config:")],
                                 ["claude:model:high"])
                codex = [c for c in checks if c.get("server") == "x" and c["runtime"].startswith("codex:")]
                self.assertEqual([(c["status"], c["message"]) for c in codex], [("ok", "x: loaded; 3 tools")])
                self.assertEqual([call[0] for call in calls], [["codex-server"]])
                self.assertNotIn("secret-token", json.dumps(result))

    def test_malformed_codex_user_config_does_not_block_claude_probe(self):
        self.write("../home/.codex/config.toml", 'command=secret-token\n')
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "claude-server"}}})
        self.write(".claude/settings.json", {"enabledMcpjsonServers": ["x"]})
        config = load_config(self.path)
        worker = replace(config.agents[0], runtimes=tuple(reversed(config.agents[0].runtimes)),
                         runtime_args={"codex": ("-c", 'mcp_servers.x.command="codex-server"')})
        checks = self.checks(config=replace(config, agents=(worker,)))
        self.assertEqual([c["runtime"] for c in checks if c["id"].startswith("runtime-mcp-config:")],
                         ["codex:model:high"])
        self.assertEqual(checks[-1]["message"], "x: loaded; 3 tools")
        self.assertEqual([call[0] for call in self.probe.calls], [["claude-server"]])

    def test_ignored_config_errors_preserve_loaded_and_pending_verdicts(self):
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "claude-server"},
                                              "pending": {"command": "never-start"}}})
        self.write(".claude/settings.json", {"enabledMcpjsonServers": ["x"]})
        config = load_config(self.path)
        worker = replace(config.agents[0], runtime_args={
            "codex": ("-c", 'mcp_servers.x.command="codex-server"')})
        for path in (".codex/config.toml", "../.codex/config.toml", "../.mcp.json"):
            with self.subTest(path=path):
                self.write(path, "private invalid configuration")
                checks = self.checks(config=replace(config, agents=(worker,)))
                calls = list(self.probe.calls)
                self.probe.calls.clear()
                (self.project / path).unlink()
                self.assertFalse(any(c["id"].startswith("runtime-mcp-config:") for c in checks))
                self.assertEqual([c["status"] for c in checks if c.get("server") == "x"], ["ok", "ok"])
                self.assertEqual(checks[0]["message"], "pending: pending approval")
                self.assertEqual([call[0] for call in calls], [["claude-server"], ["codex-server"]])

    def test_excluded_claude_sources_do_not_block_explicit_probe(self):
        self.write(".mcp.json", "private invalid configuration")
        self.write("../home/.claude/.claude.json", "private invalid configuration")
        config = load_config(self.path)
        for arguments in (("--strict-mcp-config",), ("--setting-sources=",)):
            with self.subTest(arguments=arguments):
                worker = replace(config.agents[0], runtime_args={"claude": (
                    *arguments, "--mcp-config", '{"mcpServers":{"x":{"command":"explicit"}}}')})
                checks = self.checks(config=replace(config, agents=(worker,)))
                calls = list(self.probe.calls)
                self.probe.calls.clear()
                self.assertFalse(any(c["id"].startswith("runtime-mcp-config:") for c in checks))
                self.assertEqual(checks[0]["message"], "x: loaded; 3 tools")
                self.assertEqual([call[0] for call in calls], [["explicit"]])

    def test_inline_errors_do_not_block_later_agents_using_the_same_runtime(self):
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "claude-server"}}})
        self.write(".claude/settings.json", {"enabledMcpjsonServers": ["x"]})
        config = load_config(self.path)
        worker = config.agents[0]
        reviewer = replace(worker, name="reviewer", runtimes=worker.runtimes[:1])
        for flag in ("--mcp-config", "--settings"):
            with self.subTest(flag=flag):
                broken = replace(worker, runtime_args={"claude": (flag, '{"private": secret-token}')})
                checks = self.checks(config=replace(config, agents=(broken, reviewer)))
                calls = list(self.probe.calls)
                self.probe.calls.clear()
                self.assertEqual([(c["agent"], c["runtime"]) for c in checks
                                  if c["id"].startswith("runtime-mcp-config:")], [("worker", "claude:model:high")])
                self.assertEqual(checks[-1]["message"], "x: loaded; 3 tools")
                self.assertEqual([call[0] for call in calls], [["claude-server"]])

    def test_project_denial_does_not_block_user_or_explicit_servers(self):
        self.write(".claude/settings.json", {"disabledMcpjsonServers": ["x"]})
        self.write("../home/.claude/.claude.json", {"mcpServers": {"x": {"command": "user-server"}}})
        self.assertEqual(self.checks()[0]["message"], "x: loaded; 3 tools")
        self.write(".mcp.json", {"mcpServers": {"x": {"command": "denied-project-server"}}})
        config = load_config(self.path)
        worker = replace(config.agents[0], runtime_args={"claude": (
            "--mcp-config", '{"mcpServers":{"x":{"command":"explicit"}}}')})
        self.assertEqual(self.checks(config=replace(config, agents=(worker,)))[0]["message"], "x: loaded; 3 tools")
        self.assertEqual([call[0] for call in self.probe.calls], [["user-server"], ["explicit"]])


class StdioProbeTests(unittest.TestCase):
    def probe(self, script, *, timeout=2):
        return probe_stdio([sys.executable, "-c", script], cwd=Path.cwd(), env=dict(os.environ), timeout=timeout)

    def test_handshake_notification_and_paginated_tools(self):
        result = self.probe('''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get('method') == 'initialize':
        print(json.dumps({'jsonrpc':'2.0','method':'notifications/message','params':{'secret':'never shown'}}), flush=True)
        result = {'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fake','version':'1'}}
    elif request.get('method') == 'tools/list':
        result = {'tools':[{'name':'one'}]}
        if not request.get('params', {}).get('cursor'):
            result['nextCursor'] = 'next'
    else:
        continue
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)
''')
        self.assertEqual(result, (2, None))

    def test_not_found_exit_timeout_and_private_handshake_errors(self):
        self.assertEqual(probe_stdio(["/missing/doctor-mcp-fixture"], cwd=Path.cwd(), env={}), (None, "not found"))
        for script, expected in (
                ("import sys; print('private', file=sys.stderr); sys.exit(7)", "exited"),
                ("import time; time.sleep(5)", "timed out"),
                ("print('private output', flush=True)", "handshake failed"),
                ("print('{\"jsonrpc\":\"2.0\",\"id\":1,\"error\":{\"message\":\"secret\"}}', flush=True)", "handshake failed")):
            with self.subTest(expected=expected):
                self.assertEqual(self.probe(script, timeout=0.2), (None, expected))

    def test_cleanup_includes_child_holding_stdout_after_parent_exit(self):
        # The child keeps the pipe open; the deadline and owned group cleanup
        # must still finish without relying on the parent to close stdout.
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "pid"
            self.assertEqual(self.probe(f'''import subprocess, sys
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
open({str(pidfile)!r}, 'w').write(str(p.pid))
''', timeout=0.5), (None, "timed out"))
            # A killed orphan may be a zombie until the host reaps it.
            process_id = int(pidfile.read_text())
            output = subprocess.run(["ps", "-o", "stat=", "-p", str(process_id)], capture_output=True, text=True)
            self.assertTrue(not output.stdout.strip() or output.stdout.strip().startswith("Z"))

"""Checkout convenience commands validate input before handing off to SSH."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("remote_loop", ROOT / "bin/remote-loop.py")
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)


class RemoteLoopTests(unittest.TestCase):
    def test_attach_requests_a_terminal_and_targets_the_named_loop(self):
        self.assertEqual(remote.ssh_command("uberblick-2", "attach", "operator@box"),
                         ["ssh", "-t", "--", "operator@box", "ub-agents-tmux attach uberblick-2"])

    def test_control_commands_use_the_correct_unit_or_drain_wrapper(self):
        for action in ("start", "stop"):
            with self.subTest(action=action):
                self.assertEqual(remote.ssh_command("ablauf", action, "box")[-1],
                                 f"systemctl --user {action} ub-agents@ablauf.service")
        self.assertEqual(remote.ssh_command("ablauf", "drain", "box")[-1],
                         "ub-agents-tmux drain ablauf")
        self.assertNotIn("-t", remote.ssh_command("ablauf", "logs", "box"))

    def test_shell_syntax_and_ssh_options_are_refused_before_connecting(self):
        for loop in ("-bad", "loop;touch /tmp/side-effect", "loop\nstop", "$(id)"):
            with self.subTest(loop=loop), self.assertRaises(ValueError):
                remote.ssh_command(loop, "start", "box")
        for host in ("", "-oProxyCommand=command", "box\ncommand", "user@box extra"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                remote.ssh_command("loop", "start", host)

    def test_ssh_exit_status_is_preserved_by_the_real_entry_point(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "argv.json"
            ssh = root / "ssh"
            ssh.write_text(f"#!{sys.executable}\nimport json,sys\n"
                           f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                           "sys.exit(17)\n")
            ssh.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}{os.pathsep}{os.environ['PATH']}",
                       UB_AGENTS_REMOTE_HOST="operator@box")
            result = subprocess.run([sys.executable, ROOT / "bin/remote-loop.py", "ablauf", "status"],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 17, result.stderr)
            self.assertEqual(json.loads(log.read_text()),
                             ["--", "operator@box", "systemctl --user --no-pager --full status ub-agents@ablauf.service"])

"""Credential-free checks for the issue-10 prototype's boundaries and overlay."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ub_agents.config import load_config
from ub_agents.execution import command_for, repository_checks
from ub_agents.labels import configured_labels
from ub_agents.refresh import refresh_instructions


SPIKE = Path(__file__).resolve().parents[1] / "spikes/docker-runner"
spec = importlib.util.spec_from_file_location("docker_spike_common", SPIKE / "common.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


class DockerSpikeTests(unittest.TestCase):
    def test_runtime_and_labels_are_isolated(self):
        prototype = load_config(SPIKE / "ub-agent.yaml")
        normal = load_config(SPIKE.parents[1] / "ub-agent.yaml")
        labels = lambda config: {label.name.casefold() for label in configured_labels(config)}
        self.assertFalse(labels(prototype) & labels(normal))
        agent = prototype.agents[0]
        implementer = next(agent for agent in normal.agents if agent.name == "implementer")
        self.assertEqual(agent.runtimes, implementer.runtimes)
        self.assertEqual(agent.runtime_args, implementer.runtime_args)
        self.assertIn("danger-full-access", command_for(agent, agent.runtimes[0]))
        self.assertEqual(agent.kind, "issue")
        self.assertEqual(agent.outcomes["completed"]["add"], ("docker-spike-10-done",))

    def test_separate_workers_get_separate_volumes_without_host_access(self):
        commands = [common.container_command("sha256:example", worker, number, "Test", "test@example.invalid")
                    for worker, number in (("ub-spike10-a", 101), ("ub-spike10-b", 102))]
        for command in commands:
            self.assertEqual(command[command.index("--user") + 1], "1000:1000")
            self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
            self.assertIn("no-new-privileges:true", command)
            self.assertIn("--read-only", command)
            self.assertEqual(command[command.index("--network") + 1], "bridge")
            mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "--mount"]
            self.assertEqual(len(mounts), 1)
            self.assertTrue(mounts[0].startswith("type=volume,source=ub-spike10-"))
            for forbidden in ("--privileged", "--cap-add", "--pid", "--device", "--volume", "--env", "--rm"):
                self.assertNotIn(forbidden, command)
            self.assertNotIn("docker.sock", " ".join(command))
        self.assertNotEqual(commands[0][commands[0].index("--mount") + 1],
                            commands[1][commands[1].index("--mount") + 1])

    def test_credentials_reject_extra_access_and_ambiguous_provider(self):
        for value in ({}, {"GH_TOKEN": "synthetic"},
                      {"GH_TOKEN": "synthetic", "OPENAI_API_KEY": "synthetic", "AWS_SECRET_ACCESS_KEY": "synthetic"},
                      {"GH_TOKEN": "synthetic", "OPENAI_API_KEY": "synthetic", "CODEX_AUTH_JSON": {}},
                      {"GH_TOKEN": "synthetic\n", "OPENAI_API_KEY": "synthetic"}):
            with self.subTest(keys=list(value)), self.assertRaises(ValueError):
                common.credentials(json.dumps(value).encode())
        for provider in ({"OPENAI_API_KEY": "synthetic"}, {"CODEX_AUTH_JSON": {"synthetic": True}}):
            value = {"GH_TOKEN": "synthetic", **provider}
            self.assertEqual(common.credentials(json.dumps(value).encode()), value)

    def test_target_issue_validation(self):
        template = (SPIKE / "ub-agent.yaml").read_text()
        self.assertIn("docker-spike-10-101-ready", common.render_config(template, 101))
        for number in (10, 0, -1, True, "101"):
            with self.subTest(number=number), self.assertRaises(ValueError):
                common.render_config(template, number)

    def test_overlay_survives_real_control_refresh_and_new_worktree(self):
        # A real fast-forward catches the common trap: checking out the spike
        # branch as the launch control checkout would fail before any claim.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, control, producer = (root / name for name in ("remote.git", "control", "producer"))

            def git(path, *args):
                return subprocess.check_output(["git", "-C", str(path), *args],
                                               text=True, stderr=subprocess.DEVNULL).strip()

            subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                           check=True, capture_output=True)
            subprocess.run(["git", "clone", str(remote), str(producer)], check=True, capture_output=True)
            git(producer, "config", "user.name", "Synthetic")
            git(producer, "config", "user.email", "synthetic@example.invalid")
            (producer / "AGENTS.md").write_text("Synthetic refresh fixture\n")
            (producer / ".gitignore").write_text(".ub-agent/\n")
            git(producer, "add", ".")
            git(producer, "-c", "commit.gpgsign=false", "commit", "-m", "Synthetic baseline")
            git(producer, "push", "origin", "main")
            subprocess.run(["git", "clone", str(remote), str(control)], check=True, capture_output=True)
            role = control / ".ub-agent/docker-spike/implementer.md"
            role.parent.mkdir(parents=True)
            role.write_text("Synthetic policy\n")
            (control / ".git/info/exclude").write_text("/.docker-spike.yaml\n")
            config_path = control / ".docker-spike.yaml"
            config_path.write_text(common.render_config((SPIKE / "ub-agent.yaml").read_text(), 101)
                                   .replace("instructions: role-suffix.md",
                                            "instructions: .ub-agent/docker-spike/implementer.md"))
            config = load_config(config_path)
            (producer / "AGENTS.md").write_text("Synthetic updated fixture\n")
            git(producer, "add", ".")
            git(producer, "-c", "commit.gpgsign=false", "commit", "-m", "Synthetic update")
            git(producer, "push", "origin", "main")

            class GitHub:
                def default_branch(self):
                    return "main"

            self.assertEqual(refresh_instructions(config, config.agents[0], GitHub()), "Synthetic policy\n")
            self.assertEqual(git(control, "rev-parse", "HEAD"), git(producer, "rev-parse", "HEAD"))
            self.assertEqual(git(control, "status", "--porcelain"), "")
            self.assertTrue(config_path.exists())
            git(control, "worktree", "add", "--detach", str(control / ".ub-agent/worktrees/test"), "HEAD")
            # Validate the repository contract against a GitHub origin while
            # retaining a local-only remote for this credential-free fixture.
            git(control, "remote", "set-url", "origin", "https://github.com/uberblick-ai/ub-agents.git")
            self.assertTrue(all(error is None for _, error in repository_checks(config)))


if __name__ == "__main__":
    unittest.main()

import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ub_agents import __version__
from ub_agents.cli import main, parser


OVERVIEW = """ub-agents — project-owned engineering loops on GitHub

usage: ub-agents <command> [options]

commands:
  init                   set up this repository: starter configuration, agent
                         instructions and workflow labels
  check                  validate the configuration and instruction files
  doctor [--json]        check the machine, GitHub access, labels and agent
                         runtimes
  launch [NUMBER]        run the queue in the foreground, or handle one item
  status [NUMBER] [--json]
                         matching work, owners, attempts and why items wait
  cleanup [--apply]      preview or remove stale worktrees and branches
  retry NUMBER           let stopped work run again, with a recorded reason
  approve NUMBER         record approval of an issue's or PR's current input

inside a run, through the launcher's report_command:
  report                 record the run's outcome
  retrospective          post to the agent's retrospective board
  read NUMBER            read an issue or PR as filtered JSON

options:
  -h, --help             show this help; after a command, that command's help
  -v, --version          print the version
  --config PATH          project configuration (default: ub-agents.yaml)
"""

LAUNCH_HELP = """usage: ub-agents launch [NUMBER] [options]

Run the queue in the foreground under the configured gates. Without a number,
watch the queue; with a number, handle only that issue or PR, then exit. Queue
priority and milestone policy do not apply to an explicit item; all other gates
do. An agent still needs a matching trigger label, including with --agent.

options:
  --agent NAME           evaluate only this configured agent (needs NUMBER)
  --once                 observe once, run at most one assignment, then exit
  --no-ui                plain lines instead of the terminal view
  --config PATH          project configuration (default: ub-agents.yaml)
  -h, --help             show this help

examples:
  ub-agents launch
  ub-agents launch --once
  ub-agents launch 143 --agent implementer
"""


class HelpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.command_line = parser()

    def invoke(self, argv):
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            previous = Path.cwd()
            os.chdir(self.root)
            stack.callback(os.chdir, previous)
            # Help must not read project secrets, reach GitHub/network, create
            # artifacts or execute operations, even with an explicit config path.
            for target in ("ub_agents.cli.resolve_config_path", "ub_agents.cli.load_config",
                           "ub_agents.cli.launch_output", "ub_agents.cli.GitHub",
                           "ub_agents.cli.run", "subprocess.run", "subprocess.Popen",
                           "socket.create_connection", "pathlib.Path.open", "builtins.open"):
                stack.enter_context(patch(target, side_effect=AssertionError(f"Help touched {target}")))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            try:
                code = main(argv)
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(list(self.root.iterdir()), [])
        return code, output.getvalue(), errors.getvalue()

    def test_overview_forms_are_identical_on_stdout(self):
        expected = self.invoke([])
        self.assertEqual(expected, (0, OVERVIEW, ""))
        for argv in (["help"], ["-h"], ["--help"], ["--config", "missing.yaml"],
                     ["--config", "missing.yaml", "help"], ["--config", "missing.yaml", "--help"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(argv), expected)

    def test_launch_help_matches_requested_layout(self):
        for argv in (["launch", "-h"], ["launch", "--help"], ["help", "launch"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(argv), (0, LAUNCH_HELP, ""))

    def test_every_registered_command_has_one_aligned_overview_row(self):
        overview = self.invoke([])[1]
        rows = [line for line in overview.splitlines() if re.match(r"^  [a-z]", line)]
        expected = [name for run_command in (False, True)
                    for name, command in self.command_line.commands().items()
                    if command.run_command == run_command and not command.overview_hidden]
        self.assertEqual([line.split()[0] for line in rows], expected)
        lines = overview.splitlines()
        for line in rows:
            if len(line[2:25].strip()) > 21:
                continuation = lines[lines.index(line) + 1]
                self.assertTrue(continuation.startswith(" " * 25))
                self.assertTrue(continuation[25:].strip())
            else:
                self.assertTrue(line[25:].strip())
        self.assertNotIn(" # ", overview)

    def test_overview_fits_within_80_columns(self):
        for line in self.invoke([])[1].splitlines():
            with self.subTest(line=line):
                self.assertLessEqual(len(line), 80)

    def test_doctor_help_describes_summary_verbose_and_json(self):
        code, output, errors = self.invoke(["doctor", "--help"])
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("summary per area by default", output)
        self.assertIn("--verbose", output)
        self.assertIn("show the full per-check list", output)
        self.assertIn("does not change --json", output)
        self.assertIn("confirmed interactive prompt", output)

    def test_status_help_describes_optional_item_and_has_an_example(self):
        code, output, errors = self.invoke(["status", "--help"])
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("usage: ub-agents status [NUMBER] [options]", output)
        self.assertIn("even without a matching trigger", " ".join(output.split()))
        self.assertIn("ub-agents status 143\n", output)
        self.assertIn("ub-agents status 143 --json\n", output)

    def test_overview_stays_aligned_with_forced_color(self):
        env = os.environ.copy()
        for variable in ("FORCE_COLOR", "PYTHON_COLORS", "NO_COLOR"):
            env.pop(variable, None)
        for variable in ("FORCE_COLOR", "PYTHON_COLORS"):
            for argv in ([], ["help"], ["--help"]):
                with self.subTest(variable=variable, argv=argv):
                    result = subprocess.run([sys.executable, "-m", "ub_agents", *argv], cwd=self.root,
                                            env={**env, variable: "1"}, capture_output=True,
                                            text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(result.stdout, OVERVIEW)

    def test_compact_report_row_preserves_detailed_choices_and_parsing(self):
        rows = {line.split()[0]: line[2:25].strip()
                for line in self.command_line.format_help().splitlines() if re.match(r"^  [a-z]", line)}
        self.assertEqual(rows["report"], "report")
        details = self.command_line.commands()["report"].format_help()
        self.assertIn("--status {retry,blocked}", details)
        self.assertIn("--outcome OUTCOME", details)
        self.assertIn("--option OPTION", details)
        self.assertIn('require --action or --option', ' '.join(details.split()))
        self.assertIn('--option "Maintainer: use A." --option "Maintainer: use B."', details)
        for option, value in (("--status", "retry"), ("--status", "blocked"),
                              ("--outcome", "handed-off")):
            with self.subTest(option=option, value=value):
                args = self.command_line.parse_args(["report", option, value, "--summary", "Result"])
                self.assertEqual(getattr(args, option.removeprefix("--")), value)

    def test_number_is_optional_for_launch_and_status(self):
        rows = {line.split()[0]: line[2:].split("  ", 1)[0].strip()
                for line in self.invoke([])[1].splitlines() if re.match(r"^  [a-z]", line)}
        self.assertEqual(rows["launch"], "launch [NUMBER]")
        self.assertEqual(rows["status"], "status [NUMBER] [--json]")
        self.assertEqual(rows["retry"], "retry NUMBER")
        self.assertEqual(rows["approve"], "approve NUMBER")
        for name in ("retry", "approve"):
            self.assertNotIn("[", rows[name])
            details = self.invoke(["help", name])[1]
            self.assertNotIn("[NUMBER]", details)
            self.assertNotIn("--number", details)
            self.assertIn(f"usage: ub-agents {name} NUMBER", details)
        self.assertNotIn("--agent", rows["retry"])
        self.assertIn("--agent NAME", self.invoke(["help", "retry"])[1])
        self.assertIn("default: first matching item kind", self.invoke(["help", "retry"])[1])

    def test_rendering_required_number_preserves_positional_and_legacy_parsing(self):
        for name in ("retry", "approve"):
            with self.subTest(command=name):
                command = self.command_line.commands()[name]
                number_action = next(action for action in command._actions if action.dest == "number")
                options = ["--reason", "Resolved"] if name == "retry" else []
                command.format_help()
                self.command_line.format_help()
                self.assertEqual(number_action.nargs, "?")
                self.assertFalse(number_action.required)
                positional = self.command_line.parse_args([name, "143", *options])
                legacy = self.command_line.parse_args([name, "--number", "143", *options])
                self.assertEqual((positional.number, positional.legacy_number), (143, None))
                self.assertEqual((legacy.number, legacy.legacy_number), (None, 143))
                missing = self.invoke([name, *options])
                self.assertEqual(missing[0], 2)
                self.assertIn("requires a positive item number", missing[2])

    def test_every_command_has_equivalent_detailed_help_and_valid_examples(self):
        for name, command in self.command_line.commands().items():
            with self.subTest(command=name):
                detailed = self.invoke([name, "--help"])
                self.assertEqual((detailed[0], detailed[2]), (0, ""))
                self.assertEqual(self.invoke([name, "-h"]), detailed)
                self.assertEqual(self.invoke(["help", name]), detailed)
                self.assertEqual(self.invoke(["--config", "missing.yaml", "help", name]), detailed)
                self.assertEqual(self.invoke(["--config", "missing.yaml", name, "--help"]), detailed)
                if any(action.dest == "command_config" for action in command._actions):
                    self.assertEqual(self.invoke([name, "--config", "missing.yaml", "--help"]), detailed)
                self.assertIn(f"usage: ub-agents {name}", detailed[1])
                self.assertIn("options:", detailed[1])
                self.assertIn("examples:", detailed[1])
                self.assertTrue(command.description)
                self.assertIn("".join(command.description.split()), "".join(detailed[1].split()))
                self.assertIn(len(command.examples), (2, 3))
                for example in command.examples:
                    self.assertIn(example, detailed[1])
                    words = shlex.split(example)
                    self.assertEqual(words[0], "ub-agents")
                    self.assertEqual(self.command_line.parse_args(words[1:]).command, name)

    def test_command_usage_has_positionals_required_options_and_compact_optional_options(self):
        expected = {"init": "[options]", "check": "[options]", "doctor": "[options]",
                    "launch": "[NUMBER] [options]", "status": "[NUMBER] [options]", "cleanup": "[options]",
                    "report": "(--outcome OUTCOME | --status {retry,blocked}) --summary SUMMARY [options]",
                    "retrospective": "--body-file PATH [options]",
                    "retry": "NUMBER --reason REASON [options]", "approve": "NUMBER [options]",
                    "read": "NUMBER [options]", "help": "[COMMAND] [options]"}
        for name, command in self.command_line.commands().items():
            with self.subTest(command=name):
                details = self.invoke(["help", name])[1]
                self.assertEqual(details.splitlines()[0], f"usage: ub-agents {name} {expected[name]}")
                for action in command._actions:
                    if action.option_strings and action.required and action.help != argparse.SUPPRESS:
                        self.assertIn(f"{action.help} (required)", " ".join(details.split()))
                self.assertIn("  -h, --help             show this help\n", details)
                self.assertNotIn("show this help message and exit", details)
                # Descriptions and option rows wrap within 80 columns; existing
                # example commands remain literal, including long report forms.
                for line in details.split("\n\n", 1)[1].split("\nexamples:")[0].splitlines():
                    self.assertLessEqual(len(line), 80, line)
        self.assertIn("choose --status or --outcome (required)",
                      " ".join(self.invoke(["help", "report"])[1].split()))

    def test_new_parser_registration_appears_without_an_overview_list(self):
        subparsers = next(action for action in self.command_line._actions
                          if isinstance(action, argparse._SubParsersAction))
        extra = subparsers.add_parser("extra", help="inspect an extra item",
                                      description="Inspect an extra item when diagnosing it.",
                                      examples=("ub-agents extra 1", "ub-agents extra 2"))
        extra.add_argument("number", metavar="NUMBER", type=int, help="item number")
        self.assertIn("  extra NUMBER           inspect an extra item", self.command_line.format_help())
        self.assertIn("examples:", extra.format_help())
        self.assertIn("usage: ub-agents extra NUMBER [options]", extra.format_help())

    def test_unknown_commands_print_the_error_and_overview_on_stderr(self):
        for name in ("nope", "recover"):
            for argv in ([name], ["help", name], ["--config", "missing.yaml", name],
                         ["--config", "missing.yaml", "help", name]):
                with self.subTest(argv=argv):
                    self.assertEqual(self.invoke(argv), (2, "", f'ub-agents: unknown command "{name}"\n\n{OVERVIEW}'))

    def test_usage_errors_print_the_usage_of_the_command_that_ran(self):
        for argv, name in ((["retry"], "retry"), (["retry", "143"], "retry"),
                           (["retry", "--agent", "implementer", "--reason", "Resolved"], "retry"),
                           (["approve"], "approve"), (["approve", "0"], "approve"),
                           (["approve", "143", "--number", "143"], "approve"),
                           (["--config", "x.yaml", "check", "--config", "x.yaml"], "check"),
                           (["report"], "report"), (["read"], "read"),
                           (["launch", "--agent", "implementer"], "launch"),
                           (["launch", "--bogus"], "launch"),
                           (["launch", "not-a-number"], "launch"),
                           (["launch", "--agent"], "launch"),
                           (["launch", "--config", "missing.yaml", "--bogus"], "launch")):
            with self.subTest(argv=argv):
                code, output, errors = self.invoke(argv)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertTrue(errors.startswith(self.command_line.commands()[name].format_usage()))
                self.assertIn(f"ub-agents {name}: error:", errors)
                self.assertNotIn("usage: ub-agents <command>", errors)
                self.assertNotIn("[-h]", errors)

    def test_top_level_usage_errors_keep_the_overview_usage_line(self):
        for argv in (["--bogus"], ["--config"]):
            with self.subTest(argv=argv):
                code, output, errors = self.invoke(argv)
                self.assertEqual((code, output), (2, ""))
                self.assertTrue(errors.startswith("usage: ub-agents <command> [options]\n"))
                self.assertIn("ub-agents: error:", errors)

    def test_recover_has_no_help_or_examples(self):
        self.assertNotIn("recover", self.command_line.commands())
        self.assertNotIn("recover", self.invoke([])[1])
        self.assertTrue(all("recover" not in example for command in self.command_line.commands().values()
                            for example in command.examples))

    def test_version_is_unchanged_and_isolated(self):
        for flag in ("-v", "--version"):
            with self.subTest(flag=flag):
                self.assertEqual(self.invoke([flag]), (0, f"ub-agents {__version__}\n", ""))

    def test_cli_help_works_outside_a_repository_without_gh(self):
        # Exercise the real process entry point with no executables on PATH and
        # no gh authentication directory, rather than relying only on mocks.
        env = {key: value for key, value in os.environ.items()
               if key not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}}
        env.update(PATH=str(self.root), GH_CONFIG_DIR=str(self.root / "missing-gh"))
        forms = [[], ["help"], ["-h"], ["--help"], ["-v"], ["--version"]]
        forms.extend(argv for name in self.command_line.commands()
                     for argv in (["help", name], [name, "--help"]))
        for argv in forms:
            with self.subTest(argv=argv):
                result = subprocess.run([sys.executable, "-m", "ub_agents", *argv], cwd=self.root,
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertTrue(result.stdout)
                self.assertEqual(list(self.root.iterdir()), [])

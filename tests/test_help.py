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
        self.assertEqual((expected[0], expected[2]), (0, ""))
        for argv in (["help"], ["--help"], ["--config", "missing.yaml"],
                     ["--config", "missing.yaml", "help"], ["--config", "missing.yaml", "--help"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.invoke(argv), expected)
        self.assertIn("--config", expected[1])
        self.assertIn("--version", expected[1])
        self.assertIn("ub-agents help COMMAND", expected[1])

    def test_every_registered_command_has_one_aligned_overview_row(self):
        overview = self.invoke([])[1]
        rows = [line for line in overview.splitlines() if " # " in line]
        names = [line.split()[1] for line in rows]
        self.assertEqual(names, list(self.command_line.commands()))
        self.assertEqual(len({line.index("#") for line in rows}), 1)
        self.assertTrue(all(line.split(" # ")[1].strip() for line in rows))

    def test_overview_fits_within_100_columns(self):
        for line in self.invoke([])[1].splitlines():
            with self.subTest(line=line):
                self.assertLessEqual(len(line), 100)

    def test_overview_stays_aligned_with_forced_color(self):
        env = os.environ.copy()
        for variable in ("FORCE_COLOR", "PYTHON_COLORS", "NO_COLOR"):
            env.pop(variable, None)
        for variable in ("FORCE_COLOR", "PYTHON_COLORS"):
            expected = None
            for argv in ([], ["help"], ["--help"]):
                with self.subTest(variable=variable, argv=argv):
                    result = subprocess.run([sys.executable, "-m", "ub_agents", *argv], cwd=self.root,
                                            env={**env, variable: "1"}, capture_output=True,
                                            text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    rows = [line for line in result.stdout.splitlines() if " # " in line]
                    self.assertTrue(all("\x1b" not in line for line in rows), rows)
                    self.assertEqual([line.split()[1] for line in rows], list(self.command_line.commands()))
                    self.assertEqual(len({line.index("#") for line in rows}), 1)
                    visible = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
                    self.assertTrue(all(len(line) <= 100 for line in visible.splitlines()), visible)
                    if expected is None:
                        expected = result.stdout
                    self.assertEqual(result.stdout, expected)

    def test_compact_report_row_preserves_detailed_choices_and_parsing(self):
        rows = {line.split()[1]: line.split(" # ")[0].strip()
                for line in self.command_line.format_help().splitlines() if " # " in line}
        self.assertEqual(rows["report"], "ub-agents report --status STATUS --summary SUMMARY")
        details = self.command_line.commands()["report"].format_help()
        self.assertIn("--status {retry,blocked}", details)
        self.assertIn("--outcome OUTCOME", details)
        for option, value in (("--status", "retry"), ("--status", "blocked"),
                              ("--outcome", "handed-off")):
            with self.subTest(option=option, value=value):
                args = self.command_line.parse_args(["report", option, value, "--summary", "Result"])
                self.assertEqual(getattr(args, option.removeprefix("--")), value)

    def test_number_is_optional_only_for_launch(self):
        rows = {line.split()[1]: line.split(" # ")[0].strip()
                for line in self.invoke([])[1].splitlines() if " # " in line}
        self.assertEqual(rows["launch"], "ub-agents launch [NUMBER]")
        for name in ("retry", "recover", "approve"):
            self.assertIn("--number NUMBER", rows[name])
            self.assertNotIn("[", rows[name])
        for name in ("retry", "recover"):
            self.assertIn("--agent AGENT --reason REASON", rows[name])

    def test_every_command_has_equivalent_detailed_help_and_valid_examples(self):
        for name, command in self.command_line.commands().items():
            with self.subTest(command=name):
                detailed = self.invoke([name, "--help"])
                self.assertEqual((detailed[0], detailed[2]), (0, ""))
                self.assertEqual(self.invoke(["help", name]), detailed)
                self.assertEqual(self.invoke(["--config", "missing.yaml", "help", name]), detailed)
                self.assertEqual(self.invoke(["--config", "missing.yaml", name, "--help"]), detailed)
                self.assertIn(f"usage: ub-agents {name}", detailed[1])
                self.assertIn("options:", detailed[1])
                self.assertIn("Examples:", detailed[1])
                self.assertTrue(command.description)
                self.assertIn("".join(command.description.split()), "".join(detailed[1].split()))
                self.assertIn(len(command.examples), (2, 3))
                for example in command.examples:
                    self.assertIn(example, detailed[1])
                    words = shlex.split(example)
                    self.assertEqual(words[0], "ub-agents")
                    self.assertEqual(self.command_line.parse_args(words[1:]).command, name)

    def test_required_arguments_and_options_are_marked_in_detailed_help(self):
        for name, command in self.command_line.commands().items():
            with self.subTest(command=name):
                details = self.invoke(["help", name])[1]
                for action in command._actions:
                    if action.required and action.help != argparse.SUPPRESS:
                        self.assertIn(f"{action.help} (required)", " ".join(details.split()))
        self.assertIn("NUMBER", self.invoke(["help", "launch"])[1])
        self.assertIn("(optional)", self.invoke(["help", "launch"])[1])
        self.assertIn("choose --status or --outcome (required)",
                      " ".join(self.invoke(["help", "report"])[1].split()))

    def test_new_parser_registration_appears_without_an_overview_list(self):
        subparsers = next(action for action in self.command_line._actions
                          if isinstance(action, argparse._SubParsersAction))
        extra = subparsers.add_parser("extra", help="Inspect an extra item",
                                      description="Inspect an extra item when diagnosing it.",
                                      examples=("ub-agents extra 1", "ub-agents extra 2"))
        extra.add_argument("number", metavar="NUMBER", type=int, help="Item number")
        self.assertIn("ub-agents extra NUMBER", self.command_line.format_help())
        self.assertIn("Examples:", extra.format_help())
        self.assertIn("Item number (required)", extra.format_help())

    def test_unknown_commands_and_missing_required_arguments_are_errors(self):
        for argv, hint in ((["nope"], "ub-agents help"), (["help", "nope"], "ub-agents help"),
                           (["retry"], "ub-agents help retry"),
                           (["retry", "--agent", "implementer", "--reason", "Resolved"], "ub-agents help retry"),
                           (["recover"], "ub-agents help recover"), (["approve"], "ub-agents help approve"),
                           (["report"], "ub-agents help report"),
                           (["launch", "--agent", "implementer"], "ub-agents help launch")):
            with self.subTest(argv=argv):
                code, output, errors = self.invoke(argv)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertIn("error:", errors)
                self.assertIn(f"Run {hint} for usage and examples.", errors)

    def test_version_is_unchanged_and_isolated(self):
        self.assertEqual(self.invoke(["--version"]), (0, f"ub-agents {__version__}\n", ""))

    def test_cli_help_works_outside_a_repository_without_gh(self):
        # Exercise the real process entry point with no executables on PATH and
        # no gh authentication directory, rather than relying only on mocks.
        env = {key: value for key, value in os.environ.items()
               if key not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}}
        env.update(PATH=str(self.root), GH_CONFIG_DIR=str(self.root / "missing-gh"))
        forms = [[], ["help"], ["--help"]]
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

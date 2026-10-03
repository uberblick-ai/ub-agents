from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main


class ArgumentTests(unittest.TestCase):
    commands = (("init",), ("check",), ("doctor",), ("launch",), ("status",),
                ("cleanup",), ("retry", "42", "--reason", "Fixed"),
                ("recover", "42", "--reason", "Stopped"), ("approve", "42"))

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / "nested" / "project.yaml"
        self.path.parent.mkdir()
        self.load = self.enterContext(patch("ub_agents.cli.load_config"))
        self.run = self.enterContext(patch("ub_agents.cli.run"))
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def usage_error(self, argv, message):
        self.run.reset_mock()
        self.stderr.seek(0)
        self.stderr.truncate()
        with self.assertRaises(SystemExit) as caught:
            main(argv)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn(message, self.stderr.getvalue())
        self.run.assert_not_called()
        self.load.assert_not_called()

    def test_config_before_or_after_every_configuration_command(self):
        self.run.return_value = 0
        for command in self.commands:
            for argv in (["--config", str(self.path), *command],
                         [*command, "--config", str(self.path)]):
                with self.subTest(argv=argv):
                    self.assertEqual(main(argv), 0)
                    args = self.run.call_args.args[0]
                    self.assertEqual(args.config, self.path)
                    self.assertFalse(args.default_config)

    def test_config_in_both_positions_is_a_usage_error(self):
        for command in self.commands:
            with self.subTest(command=command):
                self.usage_error(["--config", str(self.path), *command, "--config", str(self.path)],
                                 "--config may be given before or after the command, not both")
        self.assertFalse((self.path.parent / ".ub-agents").exists())

    def test_launch_logs_beside_the_selected_config_in_either_position(self):
        def run(args):
            print("Launching")

        self.run.side_effect = run
        for argv in (["--config", str(self.path), "launch"],
                     ["launch", "--config", str(self.path)]):
            self.assertEqual(main(argv), 0)
        log = self.path.parent / ".ub-agents" / "launch.log"
        self.assertEqual([line.split(" ", 1)[1] for line in log.read_text().splitlines()],
                         ["Launching", "Launching"])

    def test_positional_number_and_hidden_alias(self):
        self.run.return_value = 0
        for name in ("approve", "retry", "recover"):
            options = [] if name == "approve" else ["--reason", "Fixed"]
            for number in (["42"], ["--number", "42"]):
                with self.subTest(command=name, number=number):
                    self.assertEqual(main([name, *number, *options]), 0)
                    self.assertEqual(self.run.call_args.args[0].number, 42)
            self.stdout.seek(0)
            self.stdout.truncate()
            with self.assertRaises(SystemExit) as caught:
                main([name, "--help"])
            self.assertEqual(caught.exception.code, 0)
            self.assertNotIn("--number", self.stdout.getvalue())
            self.assertIn("number", self.stdout.getvalue())

    def test_conflicting_missing_and_invalid_numbers_are_usage_errors(self):
        for name in ("approve", "retry", "recover"):
            options = [] if name == "approve" else ["--reason", "Fixed"]
            for number in (["42", "--number", "42"], ["42", "--number", "43"]):
                with self.subTest(command=name, number=number):
                    self.usage_error([name, *number, *options], "accepts either N or --number N, not both")
            for number in ([], ["0"], ["-1"], ["--number", "0"], ["--number", "-1"]):
                with self.subTest(command=name, number=number):
                    self.usage_error([name, *number, *options], "requires a positive item number")

    def test_report_keeps_global_config_compatibility_but_no_command_config(self):
        self.run.return_value = 0
        report = ["report", "--status", "retry", "--summary", "Transient failure"]
        self.assertEqual(main(report), 0)
        self.assertEqual(main(["--config", str(self.path), *report]), 0)
        self.usage_error([*report, "--config", str(self.path)], "unrecognized arguments: --config")

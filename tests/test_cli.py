from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.cli import main


class ArgumentTests(unittest.TestCase):
    commands = (("init",), ("check",), ("doctor",), ("launch",), ("status",), ("status", "42"),
                ("cleanup",), ("retry", "42", "--reason", "Fixed"),
                ("approve", "42"), ("read", "42"))

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / "nested" / "project.yaml"
        self.path.parent.mkdir()
        self.path.touch()
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

    def test_config_help_uses_the_same_metavar_in_both_positions(self):
        for command in ((), *self.commands):
            with self.subTest(command=command):
                self.stdout.seek(0)
                self.stdout.truncate()
                with self.assertRaises(SystemExit) as caught:
                    main([*command, "--help"])
                self.assertEqual(caught.exception.code, 0)
                self.assertIn("--config PATH", self.stdout.getvalue())
                self.assertNotIn("COMMAND_CONFIG", self.stdout.getvalue())
        self.run.assert_not_called()
        self.load.assert_not_called()

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

    def test_config_resolution_failure_reports_error_without_running(self):
        commands = (("launch", "--no-ui"), ("launch", "--once", "--no-ui"),
                    ("launch", "42", "--no-ui"), ("status",), ("cleanup",), ("doctor",))
        for command in commands:
            for argv in ([*command], ["--config", str(self.path), *command],
                         [*command, "--config", str(self.path)]):
                failure = FileNotFoundError(2, "No such file or directory")
                with self.subTest(argv=argv), \
                        patch("ub_agents.cli.resolve_config_path", side_effect=failure):
                    self.stderr.seek(0)
                    self.stderr.truncate()
                    self.assertEqual(main(argv), 1)
                    self.assertEqual(self.stderr.getvalue(), f"ub-agents: {failure}\n")
        self.run.assert_not_called()
        self.load.assert_not_called()
        self.assertFalse((self.path.parent / ".ub-agents").exists())

    def test_positional_number_and_hidden_alias(self):
        self.run.return_value = 0
        for name in ("approve", "retry"):
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
            self.assertIn("NUMBER", self.stdout.getvalue())

    def test_conflicting_missing_and_invalid_numbers_are_usage_errors(self):
        for name in ("approve", "retry"):
            options = [] if name == "approve" else ["--reason", "Fixed"]
            for number in (["42", "--number", "42"], ["42", "--number", "43"]):
                with self.subTest(command=name, number=number):
                    self.usage_error([name, *number, *options], "accepts either N or --number N, not both")
            for number in ([], ["0"], ["-1"], ["--number", "0"], ["--number", "-1"]):
                with self.subTest(command=name, number=number):
                    self.usage_error([name, *number, *options], "requires a positive item number")

    def test_launch_item_stays_optional_and_accepts_a_positive_number(self):
        self.run.return_value = 0
        for number, expected in (([], None), (["42"], 42)):
            with self.subTest(number=number):
                self.assertEqual(main(["launch", *number, "--config", str(self.path)]), 0)
                self.assertEqual(self.run.call_args.args[0].number, expected)
        for options in ([], ["--once"]):
            self.assertEqual(main(["launch", "--agent", "worker", *options, "--config", str(self.path)]), 0)
            self.assertIsNone(self.run.call_args.args[0].number)
            self.assertEqual(self.run.call_args.args[0].agent, "worker")
        for number in ("0", "-1"):
            self.usage_error(["launch", number], "launch requires a positive item number")

    def test_status_item_is_optional_and_requires_a_positive_number(self):
        self.run.return_value = 0
        for number, expected in (([], None), (["42"], 42)):
            with self.subTest(number=number):
                self.assertEqual(main(["status", *number, "--config", str(self.path)]), 0)
                self.assertEqual(self.run.call_args.args[0].number, expected)
        for number in ("0", "-1"):
            self.usage_error(["status", number], "status requires a positive item number")
        self.usage_error(["status", "invalid"], "invalid int value")
        self.usage_error(["status", "42", "43"], "unrecognized arguments: 43")

    def test_recover_remains_removed(self):
        with self.assertRaises(SystemExit) as caught:
            main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertNotIn("recover", self.stdout.getvalue())
        self.usage_error(["recover", "42", "--reason", "Stopped"], 'unknown command "recover"')

    def test_report_keeps_global_config_compatibility_but_no_command_config(self):
        self.run.return_value = 0
        report = ["report", "--status", "retry", "--summary", "Transient failure"]
        self.assertEqual(main(report), 0)
        self.assertEqual(main(["--config", str(self.path), *report]), 0)
        self.usage_error([*report, "--config", str(self.path)], "unrecognized arguments: --config")


class MissingConfigTests(unittest.TestCase):
    commands = (("check",), ("status",), ("status", "42"), ("launch",), ("launch", "--once"),
                ("cleanup",), ("retry", "42", "--reason", "Fixed"), ("approve", "42"), ("read", "42"))

    def test_missing_config_names_init_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            # Resolve like config paths are: macOS temporary paths live under /private/var.
            root = Path(directory).resolve()
            selected = root / "custom.yaml"
            for command in self.commands:
                for selection in ([], ["--config", str(selected)]):
                    positions = ([*selection, *command], [*command, *selection]) if selection else (list(command),)
                    for argv in positions:
                        with self.subTest(argv=argv), patch("ub_agents.config.Path.cwd", return_value=root), \
                                patch("ub_agents.cli.os.environ", {}), \
                                patch("ub_agents.cli.run") as run, \
                                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                            self.assertEqual(main(argv), 1)
                        name = str(selected) if selection else "ub-agents.yaml"
                        self.assertEqual(stderr.getvalue(),
                                         f"ub-agents: No {name} here; run ub-agents init to set up a project\n")
                        self.assertEqual(stdout.getvalue(), "")
                        run.assert_not_called()
                        self.assertEqual(list(root.iterdir()), [])

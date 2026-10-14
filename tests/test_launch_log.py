from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_agents.launch_log import LOG_CAP_BYTES, launch_output


class LaunchLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.local = self.root / ".ub-agents"
        self.local.mkdir()
        self.log = self.local / "launch.log"
        self.archive = self.local / "launch.log.1"

    def test_over_cap_rotates_replacing_old_archive_and_records_one_line(self):
        content = "x" * LOG_CAP_BYTES + "\n"
        self.log.write_text(content)
        self.archive.write_text("old archive")
        with redirect_stdout(io.StringIO()) as terminal, launch_output(self.root):
            print("new launch")
        self.assertEqual(self.archive.read_text(), content)
        lines = [line.split(" ", 1)[1] for line in self.log.read_text().splitlines()]
        self.assertEqual(lines, ["rotated launch.log to launch.log.1 (larger than 5 MB)", "new launch"])
        self.assertEqual(terminal.getvalue().splitlines(), lines)

    def test_at_or_below_cap_appends_without_rotation(self):
        for size in (0, LOG_CAP_BYTES):
            with self.subTest(size=size):
                self.log.write_text("x" * size)
                with redirect_stdout(io.StringIO()), launch_output(self.root):
                    print("new")
                self.assertTrue(self.log.read_text().startswith("x" * size))
                self.assertFalse(self.archive.exists())

    def test_existing_writer_keeps_appending_to_archive_while_new_writer_uses_fresh_log(self):
        self.log.write_text("x" * LOG_CAP_BYTES + "\n")
        with self.log.open("a") as earlier:
            with redirect_stdout(io.StringIO()), launch_output(self.root):
                earlier.write("earlier launcher continued\n")
                earlier.flush()
                print("current launcher")
        self.assertTrue(self.archive.read_text().endswith("earlier launcher continued\n"))
        self.assertIn("current launcher", self.log.read_text())
        self.assertNotIn("earlier launcher", self.log.read_text())

    def test_rotation_failure_is_logged_once_and_launch_keeps_appending(self):
        self.log.write_text("x" * LOG_CAP_BYTES + "\n")
        with patch.object(Path, "replace", side_effect=PermissionError("rename denied")), \
                redirect_stdout(io.StringIO()) as terminal, launch_output(self.root):
            print("launch continued")
        self.assertEqual(terminal.getvalue(),
                         "Cannot rotate launch.log: rename denied; continuing to launch\nlaunch continued\n")
        self.assertTrue(self.log.read_text().endswith("launch continued\n"))

    def test_archive_symlink_is_replaced_without_touching_target(self):
        outside = self.root / "outside"
        outside.write_text("external")
        self.archive.symlink_to(outside)
        self.log.write_text("x" * LOG_CAP_BYTES + "\n")
        with redirect_stdout(io.StringIO()), launch_output(self.root):
            pass
        self.assertFalse(self.archive.is_symlink())
        self.assertEqual(outside.read_text(), "external")

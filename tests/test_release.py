"""Maintainer release tests never contact GitHub, install packages or push."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.support import RecordingRunner


spec = importlib.util.spec_from_file_location("release_tool", Path(__file__).resolve().parents[1] / "bin/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
VERSION = "0.2.0"
SHA = "a" * 40
TAG = f"v{VERSION}"
URL = f"https://github.com/{release.REPOSITORY}/archive/refs/tags/{TAG}.tar.gz"
DIGEST = hashlib.sha256(b"archive fixture").hexdigest()
NOTES = "\n\n**Upgrading:** restart launchers.\n\n### Added\n\n- A change (#398).\n\n"


def package(name="pyyaml", version="6.0.3", digest=DIGEST):
    return {"name": name, "version": version, "sha256": digest,
            "url": f"https://files.pythonhosted.org/packages/fixture/{name.replace('-', '_')}-{version}.tar.gz"}


def formula(*packages):
    return ('class UbAgents < Formula\n'
            '  include Language::Python::Virtualenv\n\n'
            '  desc "Run coding agents"\n'
            '  homepage "https://github.com/uberblick-ai/ub-agents"\n'
            '  url "https://github.com/uberblick-ai/ub-agents/archive/refs/tags/v0.1.16.tar.gz"\n'
            f'  sha256 "{"0" * 64}"\n'
            '  license "MIT"\n'
            '  depends_on "gh"\n'
            '  depends_on "python@3.14"\n\n' +
            '\n'.join(release.resource_block(item) for item in packages) + '\n'
            '  def install\n'
            '    virtualenv_install_with_resources\n'
            '  end\n\n'
            '  test do\n'
            '    system libexec/"bin/python", "-c", "from ub_agents.view_ui import View"\n'
            '  end\n'
            'end\n')


def report(*packages):
    return {"version": "1", "environment": {"python_version": "3.14"}, "install": [
        {"metadata": {"name": "ub-agents", "version": VERSION},
         "download_info": {"url": URL, "archive_info": {"hashes": {"sha256": DIGEST}}}},
        *[{"metadata": {"name": item["name"], "version": item["version"]},
           "download_info": {"url": item["url"], "archive_info": {"hashes": {"sha256": item["sha256"]}}}}
          for item in packages]]}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.runner = RecordingRunner(self.root)
        self.runner.responses.clear()
        (self.root / "pyproject.toml").write_text(f'[project]\nversion = "{VERSION}"\n')
        (self.root / "CHANGELOG.md").write_text(f"# Changelog\n\n## {VERSION} — 2026-10-09" + NOTES +
                                               "## 0.1.16 — 2026-10-08\n\nOld notes.\n")
        self.git("remote", "get-url", "origin", response=f"git@github.com:{release.REPOSITORY}.git")
        self.git("fetch", "--quiet", "origin", "main")
        self.git("rev-parse", "HEAD", response=SHA)
        self.git("rev-parse", "refs/remotes/origin/main", response=SHA)
        self.git("status", "--porcelain")
        self.git("show-ref", "--verify", "--quiet", f"refs/tags/{TAG}", response=self.result(1))
        self.git("ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{TAG}", response=self.result(2))
        self.respond(["gh", "api", f"repos/{release.REPOSITORY}/commits/{SHA}/status"],
                     json.dumps({"statuses": [{"context": "signoff", "state": "success"}]}))

    def result(self, code, stdout="", stderr=""):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def respond(self, args, response=""):
        self.runner.responses[tuple(str(arg) for arg in args)] = response

    def git(self, *args, response="", root=None):
        self.respond(["git", "-C", root or self.root, *args], response)

    def check(self):
        return release.preflight(self.root, VERSION, self.runner, environ={})

    def assert_no_publication(self):
        for args, _ in self.runner.calls:
            self.assertNotIn("push", args)
            self.assertNotIn("tag", args)
            self.assertNotIn("create", args)

    def test_preflight_returns_signed_off_sha_and_verbatim_notes(self):
        self.assertEqual(self.check(), (SHA, NOTES))
        self.assert_no_publication()

    def test_invalid_version_and_loop_roles_refuse_before_commands(self):
        for version, environ, check in (("v0.2.0", {}, "version check"),
                                        ("0.02.0", {}, "version check"),
                                        (VERSION, {"UB_AGENTS_RUN": "run"}, "maintainer check"),
                                        (VERSION, {"UB_AGENTS_RUN_CONFIG": "config"}, "maintainer check")):
            with self.subTest(version=version, environ=environ), self.assertRaisesRegex(release.ReleaseError, check):
                release.preflight(self.root, version, self.runner, environ=environ)
        self.assertEqual(self.runner.calls, [])

    def test_wrong_origin_refuses(self):
        self.git("remote", "get-url", "origin", response="git@github.com:someone/else.git")
        with self.assertRaisesRegex(release.ReleaseError, "origin check"):
            self.check()
        self.assert_no_publication()

    def test_stale_main_refuses_after_fetch(self):
        self.git("rev-parse", "refs/remotes/origin/main", response="b" * 40)
        with self.assertRaisesRegex(release.ReleaseError, "origin/main check"):
            self.check()
        self.assertIn(("git", "-C", str(self.root), "fetch", "--quiet", "origin", "main"),
                      [args for args, _ in self.runner.calls])
        self.assert_no_publication()

    def test_dirty_checkout_refuses(self):
        self.git("status", "--porcelain", response=" M CHANGELOG.md\n")
        with self.assertRaisesRegex(release.ReleaseError, "clean checkout check"):
            self.check()
        self.assert_no_publication()

    def test_local_and_remote_tag_refusals(self):
        for args in (("show-ref", "--verify", "--quiet", f"refs/tags/{TAG}"),
                     ("ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{TAG}")):
            key = ("git", "-C", str(self.root), *args)
            previous = self.runner.responses[key]
            self.runner.responses[key] = self.result(0)
            with self.subTest(args=args), self.assertRaisesRegex(release.ReleaseError, "tag check.*already exists"):
                self.check()
            self.runner.responses[key] = previous
            self.assert_no_publication()

    def test_remote_tag_lookup_failure_is_not_absence(self):
        self.git("ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{TAG}",
                 response=self.result(128, stderr="connection failed"))
        with self.assertRaisesRegex(release.ReleaseError, "ls-remote.*connection failed"):
            self.check()
        self.assert_no_publication()

    def test_mismatched_and_malformed_pyproject_refuse(self):
        for content in ('[project]\nversion = "0.1.16"\n', 'broken = [', '[other]\n'):
            (self.root / "pyproject.toml").write_text(content)
            with self.subTest(content=content), self.assertRaisesRegex(release.ReleaseError, "pyproject version check"):
                self.check()
            self.assert_no_publication()

    def test_missing_undated_invalid_and_empty_changelog_refuse(self):
        for content in ("# Changelog\n", f"## {VERSION}\n{NOTES}",
                        f"## {VERSION} — 2026-02-30\n{NOTES}", f"## {VERSION} — 2026-10-09\n\n"):
            (self.root / "CHANGELOG.md").write_text(content)
            with self.subTest(content=content), self.assertRaisesRegex(release.ReleaseError, "CHANGELOG check"):
                self.check()
            self.assert_no_publication()

    def test_missing_pending_and_failed_signoff_refuse(self):
        for statuses in ([], [{"context": "other", "state": "success"}],
                         [{"context": "signoff", "state": "pending"}],
                         [{"context": "signoff", "state": "failure"}],
                         [{"context": "signoff", "state": "failure"}, {"context": "signoff", "state": "success"}]):
            self.respond(["gh", "api", f"repos/{release.REPOSITORY}/commits/{SHA}/status"],
                         json.dumps({"statuses": statuses}))
            with self.subTest(statuses=statuses), self.assertRaisesRegex(release.ReleaseError, f"signoff check.*{SHA}"):
                self.check()
            self.assert_no_publication()

    def test_unreadable_signoff_names_check_and_refuses(self):
        for response in ("not JSON", "{}", '{"statuses": null}', '{"statuses": [null]}',
                         self.result(1, stderr="GitHub unavailable")):
            self.respond(["gh", "api", f"repos/{release.REPOSITORY}/commits/{SHA}/status"], response)
            with self.subTest(response=response), self.assertRaisesRegex(release.ReleaseError, "signoff check"):
                self.check()
            self.assert_no_publication()

    def test_check_mode_prints_notes_and_refusal_returns_nonzero(self):
        tool = release.Release(self.root, VERSION, self.runner)
        with patch.object(release, "Release", return_value=tool), \
                patch.object(release, "preflight", return_value=(SHA, NOTES)), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release.main([VERSION, "--check"]), 0)
        self.assertIn(SHA, output.getvalue())
        self.assertIn(NOTES, output.getvalue())
        with patch.object(release, "Release", return_value=tool), \
                patch.object(release, "preflight", side_effect=release.ReleaseError("local tag check: already exists")), \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release.main([VERSION]), 1)
        self.assertIn("local tag check", error.getvalue())
        self.assertIn("Completed: none", error.getvalue())
        self.assertEqual(self.runner.calls, [])

    def test_notes_extraction_first_middle_and_last_sections(self):
        for prefix, suffix in (("# Changelog\n\n", "\n## 0.1.16 — 2026-10-08\nold"),
                               ("## Unreleased\nnext\n\n", "\n## 0.1.16 — 2026-10-08\nold"),
                               ("## Unreleased\nnext\n\n", "")):
            content = prefix + f"## {VERSION} — 2026-10-09" + NOTES + suffix
            expected = NOTES + ('\n' if suffix else '')
            self.assertEqual(release.release_notes(content, VERSION), expected)

    def test_duplicate_section_refuses(self):
        section = f"## {VERSION} — 2026-10-09" + NOTES
        with self.assertRaisesRegex(release.ReleaseError, "expected one dated"):
            release.release_notes(section + section, VERSION)

    def test_formula_url_hash_and_unchanged_resources(self):
        packages = [package(), package("typing-extensions", "4.16.0")]
        original = formula(*packages)
        # An existing archive URL is preserved even when the resolver found a
        # different source URL for the same version.
        resolved = {item["name"]: item | {"url": item["url"].replace("fixture", "another")}
                    for item in packages}
        updated = release.update_formula(original, URL, DIGEST, resolved)
        expected = original.replace('v0.1.16.tar.gz', f'{TAG}.tar.gz').replace('"' + "0" * 64 + '"', f'"{DIGEST}"')
        self.assertEqual(updated, expected)
        self.assertEqual(release.resources(updated), release.resources(original))

    def test_formula_changed_added_and_removed_resources(self):
        unchanged, changed, removed = package(), package("textual", "8.2.7"), package("old", "1.0")
        newer, added = package("textual", "8.2.8"), package("rich", "15.0.0")
        resolved = {item["name"]: item for item in (unchanged, newer, added)}
        original = formula(unchanged, changed, removed)
        updated = release.update_formula(original, URL, DIGEST, resolved)
        actual = release.resources(updated)
        self.assertEqual({name: item["version"] for name, item in actual.items()},
                         {name: item["version"] for name, item in resolved.items()})
        self.assertIn(release.resource_block(unchanged), updated)
        self.assertIn(release.resource_block(newer), updated)
        self.assertIn(release.resource_block(added), updated)
        self.assertNotIn('resource "old"', updated)
        self.assertEqual(updated[updated.index('  def install'):], original[original.index('  def install'):])
        self.assertNotIn('\n\n\n', updated)

    def test_formula_can_add_first_and_remove_last_resource(self):
        item = package()
        added = release.update_formula(formula(), URL, DIGEST, {"pyyaml": item})
        self.assertEqual(set(release.resources(added)), {"pyyaml"})
        removed = release.update_formula(formula(item), URL, DIGEST, {})
        self.assertEqual(release.resources(removed), {})

    def test_formula_rejects_unsupported_python_and_layout(self):
        original = formula(package())
        for content in (original.replace('python@3.14', 'python@3.13'),
                        original.replace('  url ', '  source ', 1),
                        original.replace('  resource "pyyaml" do', '  resource "pyyaml" do # unexpected'),
                        original.replace('  end\n', '  end # unexpected\n', 1)):
            with self.subTest(content=content), self.assertRaisesRegex(release.ReleaseError, "formula .*check"):
                release.update_formula(content, URL, DIGEST, {"pyyaml": package()})

    def test_resolution_uses_full_dependency_closure(self):
        packages = [package(), package("textual", "8.2.8"), package("markdown-it-py", "4.2.0")]
        self.assertEqual(release.resolved_resources(report(*packages), VERSION),
                         {item["name"]: item for item in packages})

    def test_resolution_rejects_wrong_python_version_wheels_and_missing_hash(self):
        mutations = [lambda data: data.update(version="2"),
                     lambda data: data["environment"].update(python_version="3.13"),
                     lambda data: data["install"][0]["metadata"].update(version="0.1.16"),
                     lambda data: data["install"][1]["download_info"].update(url="https://example.com/package.whl"),
                     lambda data: data["install"][1]["download_info"]["archive_info"].update(hashes={})]
        for mutate in mutations:
            data = report(package())
            mutate(data)
            with self.subTest(data=data), self.assertRaisesRegex(release.ReleaseError, "check"):
                release.resolved_resources(data, VERSION)

    def publication(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.workspace = workspace
        packages = [package(), package("textual", "8.2.8")]
        self.packages = packages
        python = "/fixture/python3.14"
        self.python = python
        resolver = workspace / "resolve-venv"
        archive = workspace / f"ub-agents-{VERSION}.tar.gz"
        self.archive = archive
        tap = workspace / "tap"
        self.git("tag", "-a", TAG, SHA, "-m", f"ub-agents {VERSION}")
        self.git("push", "origin", f"refs/tags/{TAG}:refs/tags/{TAG}")
        self.notes_command = ["gh", "release", "create", TAG, "--repo", release.REPOSITORY, "--verify-tag",
                              "--title", f"ub-agents {VERSION}", "--notes-file", workspace / "release-notes.md"]
        self.respond(self.notes_command)
        self.respond([python, "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"], "3.14\n")
        self.respond([python, "-m", "venv", resolver])

        def resolve(args, **kwargs):
            Path(args[args.index("--report") + 1]).write_text(json.dumps(report(*packages)))
            return ""

        self.respond([resolver / "bin/python", "-m", "pip", "--isolated", "install", "--dry-run",
                      "--ignore-installed", "--no-cache-dir", "--no-binary", ":all:", "--report",
                      workspace / "dependencies.json", archive], resolve)

        def clone(args, **kwargs):
            (tap / "Formula").mkdir(parents=True)
            (tap / release.FORMULA).write_text(formula(*packages))
            return ""

        self.respond(["gh", "repo", "clone", release.TAP, tap, "--", "--quiet"], clone)
        branch = f"ub-agents-{VERSION}"
        self.git("switch", "-c", branch, root=tap)
        self.git("add", "--", release.FORMULA, root=tap)
        self.git("commit", "-m", f"ub-agents {VERSION}", root=tap)
        self.git("push", "origin", f"HEAD:refs/heads/{branch}", root=tap)
        self.pr_url = f"https://github.com/{release.TAP}/pull/20"
        self.pr_body = None

        def create(args, **kwargs):
            self.pr_body = Path(args[args.index("--body-file") + 1]).read_text()
            return self.pr_url

        self.respond(["gh", "pr", "create", "--repo", release.TAP, "--head", branch,
                      "--title", f"ub-agents {VERSION}", "--body-file", workspace / "tap-pr.md"], create)
        self.comment_body = None

        def comment(args, **kwargs):
            self.comment_body = Path(args[args.index("--body-file") + 1]).read_text()
            return ""

        self.comment_command = ["gh", "pr", "comment", self.pr_url, "--repo", release.TAP,
                                "--body-file", workspace / "tap-pr.md"]
        self.respond(self.comment_command, comment)
        venv = workspace / "test-venv"
        self.respond([python, "-m", "venv", venv])
        interpreter = venv / "bin/python"
        resource_paths = [workspace / f"resource-{index}" / item["url"].rsplit('/', 1)[-1]
                          for index, item in enumerate(packages)]
        self.install_command = [interpreter, "-m", "pip", "--isolated", "install", "--no-deps", archive, *resource_paths]
        self.respond(self.install_command)
        self.list_command = [interpreter, "-m", "pip", "--isolated", "list", "--format=json"]
        self.respond(self.list_command, json.dumps([{"name": item["name"], "version": item["version"]}
                                                    for item in packages] +
                                                   [{"name": "ub-agents", "version": VERSION},
                                                    {"name": "pip", "version": "26.2"}]))
        self.respond([interpreter, "-m", "pip", "--isolated", "check"])
        cli = venv / "bin/ub-agents"
        self.respond([cli, "--version"], f"ub-agents {VERSION}\n")
        self.respond([cli, "init", "--repository", "example/project"], "Created configuration.\n")
        self.respond([cli, "check"], "Valid configuration: example/project\n")
        self.respond([interpreter, "-c", "from ub_agents.view_ui import View"])
        self.downloads = []

        def fetch(url, path):
            self.downloads.append((url, path))
            path.write_bytes(b"archive fixture")
            return DIGEST

        self.tool = release.Release(self.root, VERSION, self.runner, fetch)
        self.addCleanup(patch.stopall)
        patch.object(release.shutil, "which", return_value=python).start()
        return self.tool

    def publish(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.tool.publish(self.workspace, SHA, NOTES)
        return output.getvalue()

    def test_publication_order_notes_links_and_exact_formula_tests(self):
        tool = self.publication()
        output = self.publish()
        self.assertEqual(tool.completed, list(release.STEPS))
        self.assertEqual((self.workspace / "release-notes.md").read_text(), NOTES)
        self.assertIn(f"https://github.com/{release.REPOSITORY}/releases/tag/{TAG}", self.pr_body)
        self.assertIn("Formula tests: PASS", self.comment_body)
        self.assertIn(f"ub-agents {VERSION}", output)
        self.assertIn(self.pr_url, output)
        self.assertEqual([url for url, _ in self.downloads], [URL, *[item["url"] for item in self.packages]])
        args = [args for args, _ in self.runner.calls]
        positions = [next(index for index, call in enumerate(args) if word in call)
                     for word in ("tag", "push", "release", "clone", "create", "--version", "comment")]
        # The first 'create' is release creation; the tap PR must precede tests.
        pr_index = next(index for index, call in enumerate(args) if call[:3] == ("gh", "pr", "create"))
        self.assertLess(pr_index, positions[-2])
        self.assertLess(positions[0], positions[1])
        self.assertLess(positions[1], positions[2])
        self.assertLess(positions[2], positions[3])
        self.assertLess(positions[-2], positions[-1])
        self.assertIn(tuple(str(arg) for arg in self.install_command), args)
        test_calls = [(call, kwargs) for call, kwargs in self.runner.calls
                      if call[0].endswith("ub-agents") or "from ub_agents.view_ui import View" in call]
        self.assertEqual(len(test_calls), 4)
        self.assertTrue(all(kwargs["cwd"] == self.workspace / "test-project" for _, kwargs in test_calls))
        self.assertFalse(any("merge" in call or "--force" in call or "-d" in call for call in args))

    def test_release_creation_failure_reports_completed_and_remaining_steps(self):
        tool = self.publication()
        self.respond(self.notes_command, self.result(1, stderr="GitHub unavailable"))
        with self.assertRaisesRegex(release.ReleaseError, "GitHub unavailable") as raised:
            self.publish()
        message = tool.failure(raised.exception)
        self.assertIn("Completed: annotated tag created, tag pushed", message)
        self.assertIn("Still to do: GitHub release created, tap PR opened, formula tests passed", message)
        self.assertIn(TAG, message)
        self.assertFalse(any("clone" in args for args, _ in self.runner.calls))

    def test_formula_failure_prints_and_posts_failure_without_merging(self):
        tool = self.publication()
        self.respond([self.workspace / "test-venv/bin/ub-agents", "check"], "wrong configuration\n")
        with self.assertRaisesRegex(release.ReleaseError, "formula test check") as raised:
            self.publish()
        self.assertIn("Formula tests: FAIL", self.comment_body)
        self.assertIn("wrong configuration", self.comment_body)
        self.assertEqual(tool.completed, [*release.STEPS[:4], release.STEPS[5]])
        self.assertIn(self.pr_url, tool.failure(raised.exception))
        self.assertIn("Still to do: formula tests passed\n", tool.failure(raised.exception))

    def test_exact_version_check_rejects_unlisted_or_wrong_dependencies(self):
        self.publication()
        self.respond(self.list_command, json.dumps([{"name": "unexpected", "version": "1.0"}]))
        with self.assertRaisesRegex(release.ReleaseError, "installed versions check"):
            self.publish()
        self.assertIn("Formula tests: FAIL", self.comment_body)

    def test_resource_checksum_failure_is_reported(self):
        self.publication()
        original = self.tool.fetch
        self.tool.fetch = lambda url, path: original(url, path) if url == URL else "0" * 64
        with self.assertRaisesRegex(release.ReleaseError, "resource checksum check"):
            self.publish()
        self.assertIn("Formula tests: FAIL", self.comment_body)
        self.assertFalse(any("--version" in args for args, _ in self.runner.calls))

    def test_comment_failure_keeps_test_success_and_returns_failure(self):
        tool = self.publication()
        self.respond(self.comment_command, self.result(1, stderr="comment unavailable"))
        with self.assertRaisesRegex(release.ReleaseError, "comment unavailable") as raised:
            self.publish()
        self.assertEqual(tool.completed, list(release.STEPS[:5]))
        self.assertIn("Still to do: formula test result posted", tool.failure(raised.exception))


if __name__ == "__main__":
    unittest.main()

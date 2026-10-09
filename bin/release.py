#!/usr/bin/env python3
"""Maintainer release tooling; deliberately outside the installed package."""

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen


REPOSITORY = "uberblick-ai/ub-agents"
TAP = "uberblick-ai/homebrew-tap"
FORMULA = "Formula/ub-agents.rb"
STEPS = ("annotated tag created", "tag pushed", "GitHub release created",
         "tap PR opened", "formula tests passed", "formula test result posted")
RESOURCE = re.compile(r'^  resource "([^"\n]+)" do\n(?:    [^\n]*\n|\n)*  end\n', re.M)


class ReleaseError(Exception):
    pass


def command(run, args, *, cwd=None, ok=(0,)):
    result = run([str(arg) for arg in args], cwd=cwd, capture_output=True, text=True)
    if result.returncode not in ok:
        detail = (result.stderr or result.stdout or "no output").strip()
        raise ReleaseError(f"{' '.join(str(arg) for arg in args)} failed ({result.returncode}): {detail}")
    return result


def release_notes(changelog, version):
    """Keep the section verbatim, including upgrade notes, without its heading."""
    headings = list(re.finditer(r'^## .+$', changelog, re.M))
    matches = [i for i, heading in enumerate(headings)
               if re.fullmatch(rf'## {re.escape(version)} — \d{{4}}-\d{{2}}-\d{{2}}', heading.group())]
    if len(matches) != 1:
        raise ReleaseError(f"CHANGELOG check: expected one dated ## {version} — YYYY-MM-DD section")
    index = matches[0]
    heading = headings[index]
    try:
        date.fromisoformat(heading.group().rsplit(" ", 1)[1])
    except ValueError as exc:
        raise ReleaseError("CHANGELOG check: invalid section date") from exc
    end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
    notes = changelog[heading.end():end]
    if not notes.strip():
        raise ReleaseError("CHANGELOG check: release notes are empty")
    return notes


def preflight(root, version, run=subprocess.run, environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("UB_AGENTS_RUN") or environ.get("UB_AGENTS_RUN_CONFIG"):
        raise ReleaseError("maintainer check: loop roles cannot cut releases")
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version):
        raise ReleaseError("version check: use X.Y.Z")

    def git(*args, ok=(0,)):
        return command(run, ["git", "-C", root, *args], ok=ok)

    origin = git("remote", "get-url", "origin").stdout.strip().removesuffix(".git")
    if origin not in (f"git@github.com:{REPOSITORY}", f"https://github.com/{REPOSITORY}",
                      f"ssh://git@github.com/{REPOSITORY}"):
        raise ReleaseError(f"origin check: expected {REPOSITORY}")
    git("fetch", "--quiet", "origin", "main")
    sha = git("rev-parse", "HEAD").stdout.strip()
    if sha != git("rev-parse", "refs/remotes/origin/main").stdout.strip():
        raise ReleaseError("origin/main check: run from a checkout at current origin/main")
    if git("status", "--porcelain").stdout.strip():
        raise ReleaseError("clean checkout check: commit or remove local changes before releasing")
    tag = f"refs/tags/v{version}"
    if git("show-ref", "--verify", "--quiet", tag, ok=(0, 1)).returncode == 0:
        raise ReleaseError(f"local tag check: v{version} already exists")
    if git("ls-remote", "--exit-code", "--tags", "origin", tag, ok=(0, 2)).returncode == 0:
        raise ReleaseError(f"origin tag check: v{version} already exists")
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    except (OSError, ValueError, KeyError) as exc:
        raise ReleaseError(f"pyproject version check: {exc}") from exc
    if project.get("version") != version:
        raise ReleaseError(f"pyproject version check: expected version = \"{version}\"")
    try:
        notes = release_notes((root / "CHANGELOG.md").read_text(), version)
    except OSError as exc:
        raise ReleaseError(f"CHANGELOG check: {exc}") from exc
    try:
        status = json.loads(command(run, ["gh", "api", f"repos/{REPOSITORY}/commits/{sha}/status"]).stdout)
        if not isinstance(status["statuses"], list) or any(not isinstance(item, dict) for item in status["statuses"]):
            raise ValueError("expected a list of commit statuses")
        signoff = next((item for item in status["statuses"] if item.get("context") == "signoff"), {})
    except (ReleaseError, ValueError, KeyError, TypeError) as exc:
        raise ReleaseError(f"signoff check: {exc}") from exc
    if signoff.get("state") != "success":
        state = signoff.get("state", "missing")
        raise ReleaseError(f"signoff check: {sha} signoff is {state}; run mise run ci {sha} on origin/main")
    return sha, notes


def canonical(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def resources(formula):
    found = {}
    for block in RESOURCE.finditer(formula):
        name = canonical(block[1])
        url = re.search(r'^    url "([^"\n]+)"$', block[0], re.M)
        digest = re.search(r'^    sha256 "([a-f0-9]{64})"$', block[0], re.M)
        if not url or not digest or name in found:
            raise ReleaseError(f"formula resource check: unsupported or duplicate resource {name}")
        filename = unquote(urlsplit(url[1]).path.rsplit('/', 1)[-1])
        stem = re.sub(r'\.(tar\.gz|tar\.bz2|tar\.xz|zip)$', '', filename)
        versions = [stem[index + 1:] for index, char in enumerate(stem)
                    if char == '-' and canonical(stem[:index]) == name]
        if stem == filename or len(versions) != 1:
            raise ReleaseError(f"formula resource check: cannot read source version for {name}")
        version = versions[0]
        found[name] = {"name": name, "version": version, "url": url[1], "sha256": digest[1],
                       "block": block[0]}
    if len(found) != len(re.findall(r'^  resource ', formula, re.M)):
        raise ReleaseError("formula resource check: unsupported resource block")
    return found


def resolved_resources(report, version):
    if report.get("version") != "1" or report.get("environment", {}).get("python_version") != "3.14":
        raise ReleaseError("dependency resolution check: expected pip report version 1 from Python 3.14")
    found = {}
    project = None
    for item in report["install"]:
        metadata = item["metadata"]
        name = canonical(metadata["name"])
        if name == "ub-agents":
            project = metadata["version"]
            continue
        url = item["download_info"]["url"]
        digest = item["download_info"].get("archive_info", {}).get("hashes", {}).get("sha256", "")
        if not url.startswith("https://") or not re.search(r'\.(tar\.gz|tar\.bz2|tar\.xz|zip)$', urlsplit(url).path):
            raise ReleaseError(f"dependency resolution check: expected source archive for {name}")
        if not re.fullmatch(r'[a-f0-9]{64}', digest) or name in found:
            raise ReleaseError(f"dependency resolution check: invalid checksum or duplicate dependency {name}")
        found[name] = {"name": name, "version": metadata["version"], "url": url, "sha256": digest}
    if project != version:
        raise ReleaseError(f"tag archive version check: expected ub-agents {version}, got {project}")
    return found


def resource_block(resource):
    return (f'  resource "{resource["name"]}" do\n'
            f'    url "{resource["url"]}"\n'
            f'    sha256 "{resource["sha256"]}"\n'
            '  end\n')


def update_formula(formula, url, digest, resolved):
    if '  depends_on "python@3.14"' not in formula:
        raise ReleaseError("formula Python check: expected python@3.14")
    old = resources(formula)
    # Only the two top-level fields change; resource fields have four spaces.
    for field, value in (("url", url), ("sha256", digest)):
        formula, count = re.subn(rf'^  {field} "[^"\n]+"$', lambda _: f'  {field} "{value}"', formula, flags=re.M)
        if count != 1:
            raise ReleaseError(f"formula {field} check: expected one top-level {field}")

    def replace(match):
        name = canonical(match[1])
        if name not in resolved:
            return ""
        if old[name]["version"] == resolved[name]["version"]:
            return match[0]
        return resource_block(resolved[name]) + ('\n' if match[0].endswith('\n\n') else '')

    # Include one trailing blank line so removals leave no extra spacing.
    formula = re.sub(RESOURCE.pattern + r'\n?', replace, formula, flags=re.M | re.S)
    added = [resource_block(resolved[name]) for name in sorted(resolved.keys() - old.keys())]
    if added:
        if formula.count("  def install\n") != 1:
            raise ReleaseError("formula install check: expected one install method")
        formula = formula.replace("  def install\n", '\n'.join(added) + '\n  def install\n')
    return formula


def download(url, destination):
    digest = hashlib.sha256()
    with urlopen(url, timeout=60) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def formula_python(run):
    python = shutil.which("python3.14")
    if python is None:
        prefix = command(run, ["brew", "--prefix", "python@3.14"]).stdout.strip()
        python = str(Path(prefix) / "bin/python3.14")
    actual = command(run, [python, "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"]).stdout.strip()
    if actual != "3.14":
        raise ReleaseError(f"formula Python check: expected Python 3.14, got {actual}")
    return python


def test_formula(run, fetch, python, workspace, archive, formula, version):
    """Install only the tag and formula sources, then replay its four test steps."""
    venv = workspace / "test-venv"
    command(run, [python, "-m", "venv", venv])
    interpreter = venv / "bin/python"
    packages = resources(formula)
    archives = []
    for index, package in enumerate(packages.values()):
        directory = workspace / f"resource-{index}"
        directory.mkdir()
        path = directory / unquote(urlsplit(package["url"]).path.rsplit('/', 1)[-1])
        if fetch(package["url"], path) != package["sha256"]:
            raise ReleaseError(f"formula resource checksum check: {package['name']}")
        archives.append(path)
    command(run, [interpreter, "-m", "pip", "--isolated", "install", "--no-deps", archive, *archives])
    installed = json.loads(command(run, [interpreter, "-m", "pip", "--isolated", "list", "--format=json"]).stdout)
    actual = {canonical(item["name"]): item["version"] for item in installed if canonical(item["name"]) != "pip"}
    expected = {name: item["version"] for name, item in packages.items()} | {"ub-agents": version}
    if actual != expected:
        raise ReleaseError(f"formula installed versions check: expected {expected}, got {actual}")
    command(run, [interpreter, "-m", "pip", "--isolated", "check"])
    project = workspace / "test-project"
    project.mkdir()
    cli = venv / "bin/ub-agents"
    lines = []
    for args, expected_output in (([cli, "--version"], f"ub-agents {version}"),
                                 ([cli, "init", "--repository", "example/project"], None),
                                 ([cli, "check"], "Valid configuration: example/project"),
                                 ([interpreter, "-c", "from ub_agents.view_ui import View"], None)):
        label = ' '.join(str(arg) for arg in args[1:])
        output = command(run, args, cwd=project).stdout.strip()
        if expected_output is not None and expected_output not in output:
            raise ReleaseError(f"formula test {label}: expected output to contain {expected_output!r}, got {output!r}")
        lines.append(f"- `{label}`: PASS" + (f" — `{output}`" if expected_output else ""))
    return f"Formula tests: PASS (Python 3.14, tag v{version}, exact formula resource versions).\n\n" + '\n'.join(lines) + '\n'


class Release:
    def __init__(self, root, version, run=subprocess.run, fetch=download):
        self.root, self.version, self.run, self.fetch = root, version, run, fetch
        self.completed = []
        self.links = []
        self.step = "preflight"

    def call(self, *args, cwd=None):
        return command(self.run, args, cwd=cwd).stdout.strip()

    def done(self):
        self.completed.append(self.step)
        print(f"release: {self.step}", flush=True)

    def publish(self, workspace, sha, notes):
        version, tag = self.version, f"v{self.version}"
        self.step = STEPS[0]
        self.call("git", "-C", self.root, "tag", "-a", tag, sha, "-m", f"ub-agents {version}")
        self.done()
        self.step = STEPS[1]
        self.call("git", "-C", self.root, "push", "origin", f"refs/tags/{tag}:refs/tags/{tag}")
        self.links.append(f"Tag: https://github.com/{REPOSITORY}/tree/{tag} ({sha})")
        self.done()
        self.step = STEPS[2]
        notes_file = workspace / "release-notes.md"
        notes_file.write_text(notes)
        self.call("gh", "release", "create", tag, "--repo", REPOSITORY, "--verify-tag",
                  "--title", f"ub-agents {version}", "--notes-file", notes_file)
        release_url = f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
        self.links.append(f"Release: {release_url}")
        self.done()
        self.step = STEPS[3]
        url = f"https://github.com/{REPOSITORY}/archive/refs/tags/{tag}.tar.gz"
        archive = workspace / f"ub-agents-{version}.tar.gz"
        digest = self.fetch(url, archive)
        python = formula_python(self.run)
        resolver = workspace / "resolve-venv"
        self.call(python, "-m", "venv", resolver)
        report = workspace / "dependencies.json"
        self.call(resolver / "bin/python", "-m", "pip", "--isolated", "install", "--dry-run",
                  "--ignore-installed", "--no-cache-dir", "--no-binary", ":all:", "--report", report, archive)
        resolved = resolved_resources(json.loads(report.read_text()), version)
        tap = workspace / "tap"
        self.call("gh", "repo", "clone", TAP, tap, "--", "--quiet")
        branch = f"ub-agents-{version}"
        self.call("git", "-C", tap, "switch", "-c", branch)
        formula_path = tap / FORMULA
        formula = update_formula(formula_path.read_text(), url, digest, resolved)
        formula_path.write_text(formula)
        self.call("git", "-C", tap, "add", "--", FORMULA)
        self.call("git", "-C", tap, "commit", "-m", f"ub-agents {version}")
        self.call("git", "-C", tap, "push", "origin", f"HEAD:refs/heads/{branch}")
        self.links.append(f"Tap branch: https://github.com/{TAP}/tree/{branch}")
        body = workspace / "tap-pr.md"
        body.write_text(f"Updates ub-agents to [{version}]({release_url}).\n\n"
                        "The release task tests the formula with Python 3.14 and the exact resource "
                        "versions, then posts the result below.\n")
        pr = self.call("gh", "pr", "create", "--repo", TAP, "--head", branch,
                       "--title", f"ub-agents {version}", "--body-file", body)
        self.links.append(f"Tap PR: {pr}")
        self.done()
        self.step = STEPS[4]
        try:
            result = test_formula(self.run, self.fetch, python, workspace, archive, formula, version)
        except (ReleaseError, OSError, ValueError, KeyError, TypeError) as exc:
            result = f"Formula tests: FAIL (Python 3.14, tag {tag}).\n\n{exc}\n"
            print(result, flush=True)
            body.write_text(result)
            try:
                self.call("gh", "pr", "comment", pr, "--repo", TAP, "--body-file", body)
                self.completed.append(STEPS[5])
            except (ReleaseError, OSError) as posting:
                print(f"release: could not post formula failure: {posting}", file=sys.stderr)
            raise
        print(result, flush=True)
        self.done()
        self.step = STEPS[5]
        body.write_text(result)
        self.call("gh", "pr", "comment", pr, "--repo", TAP, "--body-file", body)
        self.done()
        print('\n'.join(self.links) + '\nMaintainer: review and merge the tap PR.', flush=True)

    def failure(self, exc):
        remaining = [step for step in STEPS if step not in self.completed]
        return (f"release: failed during {self.step}: {exc}\n"
                f"Completed: {', '.join(self.completed) or 'none'}\n"
                f"Still to do: {', '.join(remaining)}\n" + '\n'.join(self.links) +
                "\nInspect remote state if a publication command failed; finish remaining steps manually. "
                "Never move or delete a pushed tag.\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="X.Y.Z already prepared on origin/main")
    parser.add_argument("--check", action="store_true", help="only run preflight and print release notes")
    args = parser.parse_args(argv)
    release = Release(Path(__file__).resolve().parents[1], args.version)
    try:
        sha, notes = preflight(release.root, args.version)
        if args.check:
            print(f"Release checks passed: {sha}\n{notes}")
            return 0
        with tempfile.TemporaryDirectory(prefix="ub-agents-release-") as directory:
            release.publish(Path(directory), sha, notes)
        return 0
    except (ReleaseError, OSError, ValueError, KeyError, TypeError, KeyboardInterrupt) as exc:
        print(release.failure(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Thin argv adapters and attributable POSIX process supervision."""

import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time

from .errors import AgentError, CleanupError


def resolve_executable(executable, cwd, which=None):
    """Match runtime selection: slash paths are relative to the agent cwd."""
    if "/" in executable:
        path = Path(executable) if Path(executable).is_absolute() else Path(cwd) / executable
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return (which or shutil.which)(executable)


def runtime_argv(runtime, argv):
    # Only these two literal substitutions are supported; never use a shell.
    return [arg.replace("{model}", runtime.model).replace("{effort}", runtime.effort)
            for arg in argv]


def command_for(agent, runtime):
    if agent.command:
        return list(agent.command)
    if runtime.command:
        command = runtime_argv(runtime, runtime.command)
    elif runtime.cli == "codex":
        command = ["codex", "exec", "--model", runtime.model,
                   "--config", f"model_reasoning_effort={json.dumps(runtime.effort)}"]
    else:
        command = ["claude", "--print", "--model", runtime.model, "--effort", runtime.effort]
    # No permission flags, auth stores, resume ids, or hidden provider fallback.
    return command + list(agent.runtime_args)


def git(root, *arguments):
    try:
        result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True,
                                text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentError(f"Git operation failed: {exc}") from exc
    if result.returncode:
        raise AgentError(f"Git operation failed: {result.stderr.strip()}")
    return result.stdout.strip()


def repository_checks(config, read_git=None):
    """Shared root/remote rules; consume all results to diagnose both failures."""
    read_git = read_git or git
    try:
        if Path(read_git(config.root, "rev-parse", "--show-toplevel")).resolve() != config.root:
            raise AgentError("Configuration must be at the consuming repository root")
        yield "repository-root", None
    except AgentError as exc:
        yield "repository-root", exc
    try:
        remote = read_git(config.root, "remote", "get-url", "origin")
        normalized = remote.removesuffix(".git").rstrip("/")
        if normalized not in {f"https://github.com/{config.repository}",
                              f"git@github.com:{config.repository}",
                              f"ssh://git@github.com/{config.repository}"}:
            raise AgentError("origin must point to the configured GitHub repository")
        yield "repository-remote", None
    except AgentError as exc:
        yield "repository-remote", exc


class Workspace:
    def __init__(self, config, agent, item, lease, github):
        self.root = config.root.resolve()
        self.agent = agent
        self.item = item
        self.lease = lease
        self.github = github
        self.private = self.root / ".ub-agent" / "worktrees" / lease["run"]
        self.created = False

    def prepare(self):
        if not self.agent.worktree:
            return self.agent.cwd
        self.private.parent.mkdir(parents=True, exist_ok=True)
        self.check_private_boundary()
        if self.item.kind == "pr":
            git(self.root, "fetch", "origin", f"refs/pull/{self.item.number}/head")
            head = git(self.root, "rev-parse", "FETCH_HEAD")
            if head != self.item.head:
                raise AgentError("Candidate changed while preparing its private worktree")
            git(self.root, "worktree", "add", "--detach", str(self.private), head)
        else:
            base = self.github.default_branch()
            git(self.root, "fetch", "origin", base)
            branch = f"ub-agent/{self.agent.name}/{self.item.number}/{self.lease['run']}"
            self.lease["branch"] = branch
            git(self.root, "worktree", "add", "-b", branch, str(self.private), "FETCH_HEAD")
        self.created = True
        cwd = self.private / self.agent.cwd.resolve().relative_to(self.root)
        if not cwd.is_dir() or not cwd.resolve().is_relative_to(self.private):
            raise AgentError(f"Configured cwd does not exist at the candidate: {cwd}")
        return cwd

    def cleanup(self):
        if not self.created:
            return
        # Shared project directories are never passed to private cleanup.
        self.check_private_boundary()
        try:
            git(self.root, "worktree", "remove", "--force", str(self.private))
        except AgentError as exc:
            raise CleanupError(f"Cannot remove owned private worktree; preserve artifacts: {exc}") from exc
        self.created = False
        # Retain branches: an interrupted commit can be recovered by a human.

    def check_private_boundary(self):
        if self.private.resolve() != self.private:
            raise CleanupError("Private worktree path redirects outside its owned directory; preserve artifacts")


def group_members(group):
    try:
        result = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="], text=True,
                                capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CleanupError(f"Cannot inspect owned process group: {exc}") from exc
    if result.returncode:
        raise CleanupError(f"Cannot inspect owned process group: {result.stderr.strip()}")
    return [pid for pid, pgid, state in parse_process_table(result.stdout)
            if pgid == group and not state.startswith("Z")]


def parse_process_table(output):
    """Parse the process table used by supervision and the read-only preflight."""
    rows = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 3:
            raise CleanupError("Unreadable process table")
        try:
            rows.append((int(fields[0]), int(fields[1]), fields[2]))
        except ValueError as exc:
            raise CleanupError("Unreadable process ids") from exc
    return rows


def stop_group(process, grace=2):
    try:
        _stop_group(process, grace)
    except CleanupError:
        # Inspection failure must not prevent signalling the group we created.
        # Still report uncertainty; this fallback is not proof all helpers ended.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise


def _stop_group(process, grace):
    # The group belongs to the child started with start_new_session=True.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_members(process.pid):
            process.wait(timeout=5)
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise CleanupError(f"Cannot signal owned process group {process.pid}") from exc
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()
            if not group_members(process.pid):
                process.wait(timeout=5)
                return
            time.sleep(0.05)
    raise CleanupError(f"Process group {process.pid} survived termination; preserve artifacts")


def supervise(command, cwd, env, run_dir, timeout, stop_event, prompt=None):
    if os.name != "posix":
        raise AgentError("Process supervision requires Linux or macOS")
    run_dir.mkdir(parents=True, exist_ok=True)
    input_path = run_dir / "prompt.txt"
    input_path.write_text(prompt or "")
    with input_path.open("rb") as stdin, (run_dir / "process.log").open("wb") as log:
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env,
                stdin=stdin if prompt is not None else subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise AgentError(f"Cannot start configured execution: {exc}") from exc
        (run_dir / "pid").write_text(str(process.pid))
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if stop_event.is_set():
                    raise KeyboardInterrupt
                if time.monotonic() >= deadline:
                    raise AgentError(f"Execution timed out after {timeout:g} seconds")
                stop_event.wait(min(0.2, max(0, deadline - time.monotonic())))
            return process.returncode
        finally:
            stop_group(process)

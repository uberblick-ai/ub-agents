"""Thin argv adapters and attributable POSIX process supervision."""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

from .errors import AgentError, CleanupError, LostOwnership, RetryableExecutionError
from .report_command import launcher_report_command
from .state import user_state_directory


def command_for(agent, runtime, scratch, report_command=None):
    if agent.command:
        return list(agent.command)
    if runtime.cli == "codex":
        command = ["codex", "exec", "--json", "--model", runtime.model,
                   "--config", f"model_reasoning_effort={json.dumps(runtime.effort)}"]
    else:
        command = ["claude", "--print", "--output-format", "stream-json", "--verbose",
                   "--model", runtime.model, "--effort", runtime.effort]
    # No permission flags, auth stores, or hidden provider fallback; never a shell.
    values = {"{scratch}": str(scratch), "{report_command}": report_command or launcher_report_command()}
    return command + [re.sub(r"\{(?:scratch|report_command)\}", lambda match: values[match[0]], arg)
                      for arg in agent.runtime_args]


def git(root, *arguments, strip=True):
    try:
        result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True,
                                text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentError(f"Git operation failed: {exc}") from exc
    if result.returncode:
        raise AgentError(f"Git operation failed: {result.stderr.strip()}")
    return result.stdout.strip() if strip else result.stdout


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


class ScratchDirectory:
    def __init__(self, checkout, repository, run):
        self.checkout = checkout
        self.path = user_state_directory() / repository / "runs" / run / "scratch"
        self.run_created = False
        self.created = False

    def prepare(self):
        try:
            self.path = self.path.parent.parent.resolve() / self.path.parent.name / "scratch"
            if self.path.resolve().is_relative_to(self.checkout.resolve()):
                raise AgentError(f"Cannot create run scratch directory {self.path}: path is inside the target checkout")
            for parent in reversed(self.path.parent.parents):
                try:
                    parent.mkdir(mode=0o700)
                except FileExistsError:
                    continue
                parent.chmod(0o700)
            # Own only this run's directory; never adopt an existing run's files.
            self.path.parent.mkdir(mode=0o700)
            self.run_created = True
            self.path.parent.chmod(0o700)
            self.path.mkdir(mode=0o700)
            self.created = True
            # Set the exact mode even when the launcher's umask is more restrictive.
            self.path.chmod(0o700)
        except (OSError, RuntimeError) as exc:
            raise AgentError(f"Cannot create run scratch directory {self.path}: {exc}") from exc
        return self.path

    def cleanup(self):
        if not self.run_created:
            return
        try:
            if self.path.resolve() != self.path:
                raise AgentError("Scratch path redirects outside its owned directory; preserve artifacts")
            if self.created:
                try:
                    shutil.rmtree(self.path)
                except FileNotFoundError:
                    pass  # The agent may already have removed its scratch directory.
            self.path.parent.rmdir()
        except FileNotFoundError:
            pass  # The agent may already have removed its per-run directory.
        except (OSError, RuntimeError) as exc:
            raise AgentError(f"Cannot remove run scratch directory {self.path}; preserve artifacts: {exc}") from exc
        self.created = False
        self.run_created = False


class Workspace:
    def __init__(self, config, agent, item, lease, github):
        self.root = config.root.resolve()
        self.agent = agent
        self.item = item
        self.lease = lease
        self.github = github
        self.private = self.root / ".ub-agents" / "worktrees" / lease["run"]
        self.created = False

    def prepare(self):
        if not self.agent.worktree:
            return self.root
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
            branch = f"ub-agents/{self.agent.name}/{self.item.number}/{self.lease['run']}"
            self.lease["branch"] = branch
            git(self.root, "worktree", "add", "-b", branch, str(self.private), "FETCH_HEAD")
        self.created = True
        return self.private

    def cleanup(self, before_remove=None):
        if not self.created:
            return
        # Shared project directories are never passed to private cleanup.
        self.check_private_boundary()
        if before_remove is not None and not before_remove():
            return
        self.check_private_boundary()
        try:
            git(self.root, "worktree", "remove", "--force", str(self.private))
        except AgentError as exc:
            raise CleanupError(f"Cannot remove owned private worktree {self.private}: {exc}",
                               next_step="resolve the worktree removal error") from exc
        self.created = False
        # Retain branches: an interrupted commit can be recovered by a human.

    def check_private_boundary(self):
        if self.private.resolve() != self.private:
            raise CleanupError(f"Private worktree path {self.private} redirects outside its owned directory",
                               next_step="restore the owned worktree path without redirects")


def group_members(group):
    try:
        result = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="], text=True,
                                capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CleanupError(f"Cannot inspect owned process group {group}: {exc}",
                           next_step="make ps usable") from exc
    if result.returncode:
        raise CleanupError(f"Cannot inspect owned process group {group}: {result.stderr.strip()}",
                           next_step="make ps usable")
    return [pid for pid, pgid, state in parse_process_table(result.stdout)
            if pgid == group and not state.startswith("Z")]


def parse_process_table(output):
    """Parse the process table used by supervision and the read-only preflight."""
    rows = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 3:
            raise CleanupError("Unreadable process table", next_step="make ps output readable")
        try:
            rows.append((int(fields[0]), int(fields[1]), fields[2]))
        except ValueError as exc:
            raise CleanupError("Unreadable process ids", next_step="make ps output readable") from exc
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
            raise CleanupError(f"Cannot signal owned process group {process.pid}",
                               next_step=f"confirm process group {process.pid} has exited") from exc
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()
            if not group_members(process.pid):
                process.wait(timeout=5)
                return
            time.sleep(0.05)
    raise CleanupError(f"Process group {process.pid} survived termination",
                       next_step=f"confirm process group {process.pid} has exited")


def supervise(command, cwd, env, run_dir, timeout, stop_event, prompt=None, expires=None,
              process_started=None, pass_fds=(), observe_output=None):
    if os.name != "posix":
        raise AgentError("Process supervision requires Linux or macOS")
    run_dir.mkdir(parents=True, exist_ok=True)
    input_path = run_dir / "prompt.txt"
    input_path.write_text(prompt or "")
    with input_path.open("rb") as stdin, (run_dir / "process.log").open("wb") as log:
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env,
                stdin=stdin if prompt is not None else subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=pass_fds)
        except OSError as exc:
            raise RetryableExecutionError(f"Cannot start configured execution: {exc}") from exc
        deadline = time.monotonic() + timeout
        try:
            (run_dir / "pid").write_text(str(process.pid))
            if process_started is not None:
                process_started(process.pid)
            while process.poll() is None:
                if observe_output is not None:
                    observe_output()
                if stop_event.is_set():
                    raise KeyboardInterrupt
                if time.monotonic() >= deadline:
                    raise RetryableExecutionError(f"Execution timed out after {timeout:g} seconds")
                # The lease expires by wall clock; monotonic time pauses while a machine sleeps.
                expiry = expires() if callable(expires) else expires
                if expiry is not None and time.time() >= expiry:
                    raise LostOwnership("Local lease deadline expired")
                stop_event.wait(min(0.2, max(0, deadline - time.monotonic())))
            return process.returncode
        finally:
            stop_group(process)
            if observe_output is not None:
                observe_output(final=True)

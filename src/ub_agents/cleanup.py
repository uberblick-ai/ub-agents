"""Remove private artifacts that no local run or open PR still needs."""

from dataclasses import dataclass
from contextlib import nullcontext
from pathlib import Path
import json
import re
import shutil
import socket

from .coordination import Coordinator
from .errors import AgentError, CleanupError, RecordError
from .execution import fetched_head, git, group_members
from .hooks import confirm_hook_groups_stopped, diagnostic, run_hook
from .records import records, seconds
from .run_config import run_directory
from .worktree_setup import confirm_worktree_setup_stopped

BRANCH = re.compile(r"ub-agents?/([a-z][a-z0-9_-]*)/([1-9][0-9]*)/([A-Za-z0-9_-]+)\Z")


@dataclass(frozen=True)
class Artifact:
    kind: str
    name: str
    run: str


def worktrees(root):
    result = []
    for block in git(root, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        if not block:
            continue
        fields = {}
        for entry in block.split("\0"):
            if entry:
                key, _, value = entry.partition(" ")
                fields[key] = value
        if "worktree" not in fields:
            raise AgentError("Unreadable Git worktree inventory")
        result.append(fields)
    return result


class Cleaner:
    def __init__(self, config, github, actor, output=print, *, control=None):
        self.config = config
        self.root = config.root
        self.github = github
        self.coordinator = Coordinator(github, actor, launchers=config.launchers, trusted_bots=config.trusted_bots)
        self.output = output
        self.control = control
        self.blocked_runs = set()
        self.kept_runs = set()
        self.preview_worktrees = set()
        self.branch_tips = {}

    def inventory(self):
        parent = self.root / ".ub-agents" / "worktrees"
        artifacts = []
        registered = set()
        for tree in worktrees(self.root):
            path = Path(tree["worktree"])
            if path.parent == parent:
                artifacts.append(Artifact("worktree", str(path), path.name))
                registered.add(str(path))
        # Entries no longer registered by Git are still private run artifacts.
        if parent.resolve() == parent and parent.is_dir():
            for path in sorted(parent.iterdir()):
                if str(path) not in registered:
                    artifacts.append(Artifact("worktree", str(path), path.name))
        for branch in git(self.root, "for-each-ref", "--format=%(refname:short)",
                          "refs/heads/ub-agents/", "refs/heads/ub-agent/").splitlines():
            match = BRANCH.fullmatch(branch)
            if match:
                artifacts.append(Artifact("branch", branch, match[3]))
        return artifacts

    def history(self, number):
        # An unreadable record is not a keep reason. Parse independently so it
        # cannot hide a valid live lease in the same assignment. Read failures
        # and failed author-trust checks still propagate and preserve artifacts.
        history = []
        trusted = self.coordinator.trust.observation()
        for comment in self.github.comments(number):
            try:
                history.extend(records([comment], trusted=trusted))
            except RecordError:
                continue
        return history

    def local_processes(self, run):
        run_dir = run_directory(self.root, run)
        if Path(run).name != run or run in {".", ".."} or run_dir.resolve() != run_dir:
            raise AgentError("Run diagnostics path is uncertain")
        events = run_dir / "events.jsonl"
        if events.exists() or events.is_symlink():
            try:
                if events.resolve() != events:
                    raise ValueError("redirected diagnostics")
                if any(json.loads(line).get("event") == "cleanup-unconfirmed"
                       for line in events.read_text().splitlines()):
                    raise AgentError("Run cleanup is unconfirmed in local diagnostics")
            except (OSError, ValueError, AttributeError) as exc:
                raise AgentError("Run diagnostics are unreadable") from exc
        try:
            confirm_worktree_setup_stopped(self.config, run)
            confirm_hook_groups_stopped(self.config, run)
        except CleanupError as exc:
            raise AgentError(str(exc)) from exc

    def eligible_lease(self, lease):
        if lease.get("cleanup") == "unconfirmed":
            raise AgentError("Run cleanup is unconfirmed")
        if lease.get("host") != socket.gethostname() or lease["state"] not in {"running", "claiming"}:
            return
        if seconds(lease["expires"]) > self.coordinator.clock():
            raise AgentError("Run lease is live")
        group = lease.get("process_group")
        if type(group) is not int or group < 1:
            return
        try:
            members = group_members(group)
        except CleanupError as exc:
            raise AgentError(f"Expired run's process check cannot be confirmed: {exc}") from exc
        if members:
            raise AgentError("Expired run's process group is still present")

    def run_state(self, artifact, branch=None, *, check_diagnostics=True):
        if self.control is not None:
            self.control.check(artifact)
        index, invalid = self.coordinator.repository_history()
        # Discovery is not authority. Reread referring assignments, including
        # malformed threads that may also contain a valid, protective lease.
        numbers = invalid | {r["assignment"] for r in index if r["kind"] == "lease"
                             and (r["run"] == artifact.run or
                                  (artifact.kind == "branch" and r.get("branch") == artifact.name))}
        history = [r for number in sorted(numbers) for r in self.history(number)]
        own = [r for r in history if r["kind"] == "lease" and r["run"] == artifact.run]
        leases = [r for r in history if r["kind"] == "lease"
                  and (r["run"] == artifact.run or
                       (artifact.kind == "branch" and r.get("branch") == artifact.name))]
        runs = {artifact.run} | {r["run"] for r in leases}
        if runs & self.blocked_runs:
            raise AgentError("Cleanup hook failed; artifacts preserved")
        for lease in leases:
            self.eligible_lease(lease)
        if check_diagnostics:
            for run in sorted(runs):
                self.local_processes(run)
        if artifact.kind == "branch":
            if any(pr.state == "open" for pr in self.github.prs_for_branch(artifact.name, state="all")):
                raise AgentError("Branch is the head of an open PR")
        lease = own[0] | {"branch": branch or own[0].get("branch")} if len(own) == 1 else {
            "run": artifact.run, "agent": None, "assignment": None, "state": None,
            "branch": branch,
        }
        outcomes = [r for r in history if r["kind"] == "outcome" and r["lease_id"] == lease.get("id")]
        return lease, outcomes[0] if len(outcomes) == 1 else None

    def check(self, artifact):
        if artifact.run in self.blocked_runs:
            raise AgentError("Cleanup hook failed; artifacts preserved")
        if artifact.kind == "worktree":
            path = Path(artifact.name)
            if path.parent.resolve() != path.parent:
                raise AgentError("Worktree parent path redirects")
            tree = next((t for t in worktrees(self.root) if t["worktree"] == artifact.name), None)
            branch = tree.get("branch", "").removeprefix("refs/heads/") if tree else None
            return self.run_state(artifact, branch or None)
        lease, outcome = self.run_state(artifact, artifact.name)
        if any(t.get("branch") == f"refs/heads/{artifact.name}"
               and t["worktree"] not in self.preview_worktrees for t in worktrees(self.root)):
            raise AgentError("Branch is still checked out in a kept worktree")
        return lease, outcome

    def known_tip(self, branch, tip):
        # Remote reachability affects the restoration hint, never eligibility.
        try:
            git(self.root, "fetch", "--no-write-fetch-head", "--prune", "origin",
                "+refs/heads/*:refs/remotes/origin/*")
            if git(self.root, "for-each-ref", f"--contains={tip}", "--format=%(refname)", "refs/remotes/origin/"):
                return True
            for pr in self.github.prs_for_branch(branch, state="all"):
                if not pr.merged:
                    continue
                with fetched_head(self.root, f"refs/pull/{pr.number}/head") as head:
                    if head != pr.head:
                        continue
                    try:
                        git(self.root, "merge-base", "--is-ancestor", tip, head)
                    except AgentError:
                        continue
                    return True
        except (AgentError, OSError) as exc:
            if self.control is not None:
                self.output(f"Cannot check remote reachability for {branch}: {exc}; unpushed tip {tip}")
        return False

    def remove_worktree(self, artifact):
        path = Path(artifact.name)
        registered = any(t["worktree"] == artifact.name for t in worktrees(self.root))
        if path.is_symlink():
            path.unlink()
            if not registered:
                return
        if registered:
            git(self.root, "worktree", "remove", "--force", "--force", artifact.name)
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    def mutation(self, artifact):
        if self.control is None:
            return nullcontext()
        return self.control.mutation(artifact)

    def intent(self, artifact, reason):
        if self.control is not None:
            # Flush recovery SHAs before deleting refs, rather than relying on
            # the result line surviving a crash after deletion.
            self.output(f"removing {artifact.kind} {artifact.name}: {' '.join(reason.split())}")
            self.control.checkpoint()

    def clean(self, apply=False):
        self.preview_worktrees.clear()
        self.blocked_runs.clear()
        self.kept_runs.clear()
        self.branch_tips.clear()
        rows = []
        for artifact in self.inventory():
            if self.control is not None:
                self.control.checkpoint()
            try:
                lease, outcome = self.check(artifact)
                reason = "No live run or open PR"
                action = "would remove"
                if artifact.kind == "worktree" and lease.get("cleanup_hook_error"):
                    reason += f"; retry hook: {lease['cleanup_hook_error']}"
                if artifact.kind == "branch":
                    tip = git(self.root, "rev-parse", f"refs/heads/{artifact.name}")
                    if not self.known_tip(artifact.name, tip):
                        reason += f"; unpushed tip {tip}"
                    self.branch_tips[artifact.name] = tip
                if apply:
                    # Inventory/preview conclusions never authorize a deletion.
                    lease, outcome = self.check(artifact)
                    if artifact.kind == "worktree":
                        with self.mutation(artifact):
                            self.intent(artifact, reason)
                            failure = run_hook(self.config, lease, Path(artifact.name), outcome)
                            if failure:
                                self.blocked_runs.add(artifact.run)
                                raise AgentError(f"Cleanup hook failed; retry later: {failure}")
                        # Reads after a hook must not hold up quitting or a new
                        # claim. Reacquire the guard before the actual removal.
                        self.check(artifact)
                        with self.mutation(artifact):
                            self.intent(artifact, reason)
                            self.remove_worktree(artifact)
                    else:
                        with self.mutation(artifact):
                            self.intent(artifact, reason)
                            # Compare-and-delete refuses to discard a newly moved tip.
                            if any(t.get("branch") == f"refs/heads/{artifact.name}"
                                   for t in worktrees(self.root)):
                                raise AgentError("Branch became checked out before deletion")
                            if self.control is not None:
                                self.control.checkpoint()
                            git(self.root, "update-ref", "-d", f"refs/heads/{artifact.name}",
                                self.branch_tips[artifact.name])
                    action = "removed"
                elif artifact.kind == "worktree":
                    self.preview_worktrees.add(artifact.name)
            except CleanupError as exc:
                self.blocked_runs.add(artifact.run)
                action, reason = "kept", str(exc)
                try:
                    diagnostic(self.config, artifact.run, "cleanup-unconfirmed", error=str(exc))
                except (AgentError, OSError) as error:
                    reason += f"; cannot record cleanup uncertainty: {error}"
            except (AgentError, OSError) as exc:
                action, reason = "kept", str(exc)
            reason = " ".join(reason.split())
            if action == "kept":
                self.kept_runs.add(artifact.run)
            row = {"kind": artifact.kind, "name": artifact.name, "action": action, "reason": reason}
            rows.append(row)
            name = str(Path(artifact.name).relative_to(self.root)) if artifact.kind == "worktree" else artifact.name
            self.output(f"{action} {artifact.kind} {name}: {reason}")
        return rows

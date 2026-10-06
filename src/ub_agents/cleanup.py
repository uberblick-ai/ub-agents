"""Conservative, lease-backed maintenance of this checkout's private artifacts."""

from dataclasses import dataclass
from pathlib import Path
import json
import re
import socket

from .coordination import Coordinator
from .errors import AgentError, CleanupError
from .execution import git, group_members
from .hooks import confirm_hook_groups_stopped, diagnostic, run_hook
from .records import seconds
from .run_config import run_directory

BRANCH = re.compile(r"ub-agents/([a-z][a-z0-9_-]*)/([1-9][0-9]*)/([A-Za-z0-9_-]+)\Z")
RUN = re.compile(r"[A-Za-z0-9_-]+\Z")


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
    def __init__(self, config, github, actor, output=print):
        self.config = config
        self.root = config.root
        self.github = github
        self.coordinator = Coordinator(github, actor, launchers=config.launchers, trusted_bots=config.trusted_bots)
        self.output = output
        self.blocked_runs = set()
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
        # Report unregistered entries but never remove or inspect their contents.
        if parent.resolve() == parent and parent.is_dir():
            for path in sorted(parent.iterdir()):
                if str(path) not in registered:
                    artifacts.append(Artifact("worktree", str(path), path.name))
        for branch in git(self.root, "for-each-ref", "--format=%(refname:short)",
                          "refs/heads/ub-agents/").splitlines():
            match = BRANCH.fullmatch(branch)
            if match:
                artifacts.append(Artifact("branch", branch, match[3]))
        return artifacts

    def run_state(self, artifact):
        if not RUN.fullmatch(artifact.run):
            raise AgentError("Uncertain run name")
        index, invalid = self.coordinator.repository_history()
        # Discovery is not authority. Reread the run's assignment comments.
        numbers = {r["assignment"] for r in index if r["kind"] == "lease" and r["run"] == artifact.run}
        if len(numbers) != 1:
            raise AgentError("Run lease cannot be found unambiguously")
        number = next(iter(numbers))
        if number in invalid:
            raise AgentError("Run lease is unreadable")
        history = self.coordinator.history(number)
        leases = [r for r in history if r["kind"] == "lease" and r["run"] == artifact.run]
        if len(leases) != 1:
            raise AgentError("Run lease cannot be found unambiguously")
        lease = leases[0]
        if artifact.kind == "branch":
            match = BRANCH.fullmatch(artifact.name)
            if (lease["agent"] != match[1] or lease["assignment"] != int(match[2])
                    or lease.get("branch") not in (None, artifact.name)):
                raise AgentError("Branch does not match its run lease")
            # A later run may have continued this branch in another worktree.
            # Read every discovered referring assignment.
            related = {r["assignment"] for r in index if r["kind"] == "lease"
                       and r.get("branch") == artifact.name}
            for assignment in related:
                for other in self.coordinator.history(assignment):
                    if (other["kind"] == "lease" and other.get("branch") == artifact.name
                            and other["id"] != lease["id"]):
                        self.eligible_lease(other, self.coordinator.history(assignment))
        self.eligible_lease(lease, history)
        outcomes = [r for r in history if r["kind"] == "outcome" and r["lease_id"] == lease["id"]]
        if len(outcomes) > 1:
            raise AgentError("Run has conflicting outcomes")
        return lease, outcomes[0] if outcomes else None

    def eligible_lease(self, lease, history):
        if lease.get("host") != socket.gethostname():
            raise AgentError("Run lease belongs to another host or has no recorded host")
        if lease.get("cleanup") == "unconfirmed":
            raise AgentError("Run cleanup is unconfirmed")
        run_dir = run_directory(self.root, lease["run"])
        if not RUN.fullmatch(lease["run"]) or run_dir.resolve() != run_dir:
            raise AgentError("Run diagnostics path is uncertain")
        events = run_dir / "events.jsonl"
        if events.exists():
            try:
                if events.resolve() != events:
                    raise ValueError("redirected diagnostics")
                if any(json.loads(line).get("event") == "cleanup-unconfirmed"
                       for line in events.read_text().splitlines()):
                    raise AgentError("Run cleanup is unconfirmed in local diagnostics")
            except (OSError, ValueError, AttributeError) as exc:
                raise AgentError("Run diagnostics are unreadable") from exc
        try:
            confirm_hook_groups_stopped(self.config, lease["run"])
        except CleanupError as exc:
            raise AgentError(str(exc)) from exc
        if lease["state"] == "released":
            return
        if lease["state"] not in {"running", "claiming"}:
            raise AgentError("Run lease is not released or expired")
        if seconds(lease["expires"]) > self.coordinator.clock():
            raise AgentError("Run lease is live")
        recovered = any(r["kind"] == "lease" and r["state"] == "released"
                        and r.get("recovered_lease_id") == lease["id"] for r in history)
        if not recovered and any(r["kind"] == "outcome" and r["lease_id"] == lease["id"] for r in history):
            raise AgentError("Run has an outcome awaiting recovery")
        group = lease.get("process_group")
        if type(group) is not int or group < 1:
            raise AgentError("Expired run has no recorded process group")
        try:
            members = group_members(group)
        except CleanupError as exc:
            raise AgentError(f"Expired run's process check cannot be confirmed: {exc}") from exc
        if members:
            raise AgentError("Expired run's process group is still present")

    def check(self, artifact):
        if artifact.run in self.blocked_runs:
            raise AgentError("Cleanup hook failed; artifacts preserved")
        if artifact.kind == "worktree":
            path = Path(artifact.name)
            if path.resolve() != path:
                raise AgentError("Worktree path redirects")
            tree = next((t for t in worktrees(self.root) if t["worktree"] == artifact.name), None)
            if tree is None:
                raise AgentError("Unregistered directory; ownership is uncertain")
            if "locked" in tree:
                raise AgentError("Worktree is locked")
            if git(path, "status", "--porcelain", "--untracked-files=all"):
                raise AgentError("Worktree is dirty")
            return self.run_state(artifact)
        lease, outcome = self.run_state(artifact)
        if any(t.get("branch") == f"refs/heads/{artifact.name}"
               and t["worktree"] not in self.preview_worktrees for t in worktrees(self.root)):
            raise AgentError("Branch is still checked out in a kept worktree")
        # Hook failure must retain the branch even for a detached worktree.
        for run in {artifact.run} | self.related_runs(artifact.name):
            path = self.root / ".ub-agents" / "worktrees" / run
            if path.exists() and str(path) not in self.preview_worktrees:
                raise AgentError("Run still has a kept worktree")
        prs = self.github.prs_for_branch(artifact.name, state="all")
        if any(pr.state == "open" for pr in prs):
            raise AgentError("Branch is the head of an open PR")
        tip = git(self.root, "rev-parse", f"refs/heads/{artifact.name}")
        git(self.root, "fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*")
        known = bool(git(self.root, "for-each-ref", f"--contains={tip}", "--format=%(refname)", "refs/remotes/origin/"))
        for pr in prs if not known else ():
            git(self.root, "fetch", "origin", f"refs/pull/{pr.number}/head")
            head = git(self.root, "rev-parse", "FETCH_HEAD")
            if head != pr.head:
                raise AgentError("PR head changed while checking remote history")
            try:
                git(self.root, "merge-base", "--is-ancestor", tip, head)
            except AgentError:
                continue
            known = True
            break
        if not known:
            raise AgentError("Branch tip is not known remotely")
        if git(self.root, "rev-parse", f"refs/heads/{artifact.name}") != tip:
            raise AgentError("Branch tip changed while checking remote history")
        # Network fetches may take time: reestablish lease and checkout safety.
        lease, outcome = self.run_state(artifact)
        if any(t.get("branch") == f"refs/heads/{artifact.name}"
               and t["worktree"] not in self.preview_worktrees for t in worktrees(self.root)):
            raise AgentError("Branch became checked out while checking remote history")
        if any((self.root / ".ub-agents" / "worktrees" / run).exists()
               and str(self.root / ".ub-agents" / "worktrees" / run) not in self.preview_worktrees
               for run in self.related_runs(artifact.name) | {artifact.run}):
            raise AgentError("Run acquired a kept worktree while checking remote history")
        if any(pr.state == "open" for pr in self.github.prs_for_branch(artifact.name)):
            raise AgentError("Branch acquired an open PR while checking remote history")
        if git(self.root, "rev-parse", f"refs/heads/{artifact.name}") != tip:
            raise AgentError("Branch tip changed while rechecking ownership")
        self.branch_tips[artifact.name] = tip
        return lease, outcome

    def related_runs(self, branch):
        index, invalid = self.coordinator.repository_history()
        if invalid:
            raise AgentError("Repository coordination state is unreadable")
        runs = set()
        for number in {r["assignment"] for r in index if r["kind"] == "lease" and r.get("branch") == branch}:
            runs.update(r["run"] for r in self.coordinator.history(number)
                        if r["kind"] == "lease" and r.get("branch") == branch)
        return runs

    def clean(self, apply=False):
        self.preview_worktrees.clear()
        self.blocked_runs.clear()
        self.branch_tips.clear()
        rows = []
        for artifact in self.inventory():
            try:
                lease, outcome = self.check(artifact)
                reason = "Eligible released or confirmed expired run"
                action = "would remove"
                if artifact.kind == "worktree" and lease.get("cleanup_hook_error"):
                    reason += f"; retry hook: {lease['cleanup_hook_error']}"
                if apply:
                    # Inventory/preview conclusions never authorize a deletion.
                    lease, outcome = self.check(artifact)
                    if artifact.kind == "worktree":
                        failure = run_hook(self.config, lease, Path(artifact.name), outcome)
                        if failure:
                            self.blocked_runs.add(artifact.run)
                            raise AgentError(f"Cleanup hook failed; retry later: {failure}")
                        self.check(artifact)  # hooks may alter the tree or lease
                        git(self.root, "worktree", "remove", artifact.name)
                    else:
                        # Compare-and-delete refuses to discard a newly moved tip.
                        if any(t.get("branch") == f"refs/heads/{artifact.name}"
                               for t in worktrees(self.root)):
                            raise AgentError("Branch became checked out before deletion")
                        git(self.root, "update-ref", "-d", f"refs/heads/{artifact.name}",
                            self.branch_tips[artifact.name])
                    action = "removed"
                elif artifact.kind == "worktree":
                    self.preview_worktrees.add(artifact.name)
            except CleanupError as exc:
                diagnostic(self.config, artifact.run, "cleanup-unconfirmed", error=str(exc))
                self.output(f"kept {artifact.kind} {artifact.name}: {exc}")
                raise
            except (AgentError, OSError) as exc:
                action, reason = "kept", str(exc)
            row = {"kind": artifact.kind, "name": artifact.name, "action": action, "reason": reason}
            rows.append(row)
            self.output(f"{action} {artifact.kind} {artifact.name}: {reason}")
        return rows

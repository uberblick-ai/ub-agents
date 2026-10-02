"""Refresh operator policy only at the serial loop's execution boundary."""

from collections import deque
from pathlib import Path

from .config import instruction_text
from .errors import AgentError, GitHubError
from .execution import git


def instruction_blob(root, oid, where):
    try:
        return git(root, "cat-file", "blob", oid, strip=False)
    except (AgentError, UnicodeError) as exc:
        raise AgentError(f"{where} is unreadable in the refreshed checkout: {exc}") from exc


def validate_incoming(root, head, path, where, links=0):
    """Validate a Git tree's instruction path before changing the checkout.

    Follow file and directory symlinks in that tree, not in the current checkout.
    No files are extracted and no candidate policy is executed.
    """
    if path is None:
        return
    if links > 40:
        raise AgentError(f"{where} has a symlink loop: {path}")
    try:
        pending = deque(path.relative_to(root).parts)
    except ValueError as exc:
        raise AgentError(f"{where} must remain inside the project") from exc
    resolved = []
    while pending:
        part = pending.popleft()
        if part == ".":
            continue
        if part == "..":
            if not resolved:
                raise AgentError(f"{where} must remain inside the project")
            resolved.pop()
            continue
        name = "/".join([*resolved, part])
        entry = git(root, "ls-tree", "-z", head, "--", f":(literal){name}")
        if not entry:
            # A clean checkout can contain ignored, local instruction files.
            # They survive the merge unless a tracked path removes/replaces them.
            local = root.joinpath(*resolved, part, *pending)
            old = git(root, "ls-tree", "-z", "HEAD", "--",
                      f":(literal){local.relative_to(root)}")
            parents = [root.joinpath(*resolved[:i]) for i in range(1, len(resolved) + 1)]
            if not old and not any(parent.is_symlink() for parent in parents):
                instruction_text(root, local, where)
                actual = local.resolve()
                if actual != local:
                    validate_incoming(root, head, actual, where, links + 1)
                return
            raise AgentError(f"{where} does not exist in the refreshed checkout: {path}")
        mode, kind, oid = entry.split("\t", 1)[0].split()
        if mode == "120000":
            links += 1
            if links > 40:
                raise AgentError(f"{where} has a symlink loop: {path}")
            target = Path(instruction_blob(root, oid, where))
            if target.is_absolute():
                try:
                    target = target.relative_to(root)
                except ValueError as exc:
                    raise AgentError(f"{where} must remain inside the project") from exc
                resolved = []
            pending.extendleft(reversed(target.parts))
        elif pending and kind == "tree":
            resolved.append(part)
        elif not pending and mode in {"100644", "100755"}:
            # Decode now too: invalid text must not advance the operator's HEAD.
            instruction_blob(root, oid, where)
            return
        else:
            raise AgentError(f"{where} is not an instruction file: {path}")
    raise AgentError(f"{where} is not an instruction file: {path}")


def refresh_checkout(config, github, agent=None):
    root = config.root
    where = f"{agent.name} instructions" if agent else None
    try:
        default = github.default_branch()
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            raise AgentError(f"checkout has detached HEAD; switch to the default branch {default}")
        if branch != default:
            raise AgentError(f"checkout is on {branch}; switch to the default branch {default}")
        if git(root, "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"):
            raise AgentError("checkout is dirty (staged, modified or untracked files); "
                             "commit or remove those changes before restarting")
        remote = f"refs/remotes/origin/{default}"
        try:
            git(root, "fetch", "origin", f"+refs/heads/{default}:{remote}")
        except AgentError as exc:
            raise AgentError(f"fetch of origin/{default} failed; fix origin access and retry: {exc}") from exc
        head = git(root, "rev-parse", remote)
        ahead, behind = map(int, git(root, "rev-list", "--left-right", "--count",
                                    f"HEAD...{head}").split())
        if ahead:
            condition = "checkout has diverged" if behind else "checkout has local commits not on origin"
            raise AgentError(f"{condition}; reconcile {default} with origin/{default} before restarting")
        if behind:
            # Check filesystem readability before a merge too. Git's object
            # database alone cannot diagnose permissions on the control checkout.
            if agent is not None:
                instruction_text(root, agent.instructions, where)
                validate_incoming(root, head, agent.instructions, where)
            try:
                # Never inherit autostash or execute checkout-mutating merge hooks.
                git(root, "-c", "merge.autostash=false", "-c", "core.hooksPath=/dev/null",
                    "merge", "--ff-only", "--no-edit", "--no-stat", "--no-overwrite-ignore", head)
            except AgentError as exc:
                raise AgentError(f"fast-forward to origin/{default} failed; fix the checkout "
                                 f"before restarting: {exc}") from exc
        if agent is not None:
            return instruction_text(root, agent.instructions, where)
    except GitHubError:
        # This pre-claim read is discovery: preserve its request and retry metadata.
        raise
    except (AgentError, OSError, UnicodeError) as exc:
        raise AgentError(f"Control checkout refresh stopped at {root}: {exc}. "
                         "Fix the operator checkout or instruction file and restart ub-agent launch; "
                         "no assignment attempt was charged") from exc


def refresh_instructions(config, agent, github):
    # Callers without a configuration file still validate their supplied role.
    return refresh_checkout(config, github, agent)

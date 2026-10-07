"""Refresh operator policy only at the serial loop's execution boundary."""

from collections import deque
from pathlib import Path

from .config import instruction_text
from .errors import AgentError, CheckoutRefreshError, GitHubError
from .execution import git


def control_checkout_checks(config, default, read_git=None):
    """Read-only launch prerequisites, also reported as warnings by doctor.

    Compare with the local origin ref; fetching and fast-forwarding remain at
    the execution boundary. Consume all results to diagnose every condition.
    """
    read_git = read_git or git
    root = config.root
    try:
        branch = read_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            raise AgentError(f"control checkout has detached HEAD; launch runs only from {default}; "
                             f"switch to {default}")
        if branch != default:
            raise AgentError(f"control checkout is on {branch}; launch runs only from {default}; "
                             f"switch to {default}")
        yield "control-checkout-branch", None
    except AgentError as exc:
        yield "control-checkout-branch", exc
    try:
        if read_git(root, "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"):
            raise AgentError("control checkout is dirty (staged, modified or untracked files); "
                             "commit or remove those changes before launch")
        yield "control-checkout-dirty", None
    except AgentError as exc:
        yield "control-checkout-dirty", exc
    try:
        ahead, behind = map(int, read_git(root, "rev-list", "--left-right", "--count",
                                         f"HEAD...refs/remotes/origin/{default}").split())
    except AgentError:
        yield "control-checkout-origin", AgentError(
            f"cannot compare control checkout with origin/{default}; run git fetch origin and retry")
    else:
        error = None
        if ahead:
            condition = ("control checkout has diverged" if behind else
                         "control checkout has local commits not on origin")
            error = AgentError(f"{condition}; reconcile {default} with origin/{default} before launch")
        yield "control-checkout-origin", error


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


def refresh_checkout(config, github, agent=None, *, on_fetch=None):
    root = config.root
    where = f"{agent.name} instructions" if agent else None
    next_step = "repair the checkout's Git metadata and launch again"
    try:
        default = github.default_branch()
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            next_step = f"switch to the default branch {default} and launch again"
            raise AgentError("checkout has detached HEAD")
        if branch != default:
            next_step = f"switch to the default branch {default} and launch again"
            raise AgentError(f"checkout is on {branch}")
        if git(root, "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all"):
            next_step = "commit or remove local changes and launch again"
            raise AgentError("checkout has local changes (staged, modified or untracked files)")
        remote = f"refs/remotes/origin/{default}"
        try:
            git(root, "fetch", "origin", f"+refs/heads/{default}:{remote}")
        except AgentError as exc:
            next_step = "fix origin access and launch again"
            raise AgentError(f"fetch of origin/{default} failed: {exc}") from exc
        head, previous = git(root, "rev-parse", remote, "HEAD").splitlines()
        if on_fetch is not None:
            on_fetch(default, previous, head)
        ahead, behind = map(int, git(root, "rev-list", "--left-right", "--count",
                                    f"HEAD...{head}").split())
        if ahead:
            condition = "checkout has diverged" if behind else "checkout has local commits not on origin"
            next_step = f"reconcile {default} with origin/{default} and launch again"
            raise AgentError(condition)
        if behind:
            # Check filesystem readability before a merge too. Git's object
            # database alone cannot diagnose permissions on the control checkout.
            if agent is not None:
                next_step = "restore readable, valid instruction files and launch again"
                instruction_text(root, agent.instructions, where)
                validate_incoming(root, head, agent.instructions, where)
                instruction_text(root, config.shared_instructions, "shared-instructions")
                validate_incoming(root, head, config.shared_instructions, "shared-instructions")
            next_step = "resolve the checkout's merge error and launch again"
            try:
                # Never inherit autostash or execute checkout-mutating merge hooks.
                git(root, "-c", "merge.autostash=false", "-c", "core.hooksPath=/dev/null",
                    "merge", "--ff-only", "--no-edit", "--no-stat", "--no-overwrite-ignore", head)
            except AgentError as exc:
                raise AgentError(f"fast-forward to origin/{default} failed: {exc}") from exc
        if agent is not None:
            next_step = "restore readable, valid instruction files and launch again"
            instruction_text(root, config.shared_instructions, "shared-instructions")
            return instruction_text(root, agent.instructions, where)
        return previous
    except GitHubError:
        # This pre-claim read is discovery: preserve its request and retry metadata.
        raise
    except (AgentError, OSError, UnicodeError) as exc:
        # Git may append several paragraphs of advice; keep the actual error.
        detail = " ".join(str(exc).splitlines()[0].split())
        raise CheckoutRefreshError(f"{root}: {detail}; {next_step}") from exc


def refresh_instructions(config, agent, github, *, on_fetch=None):
    # Callers without a configuration file still validate their supplied role.
    return refresh_checkout(config, github, agent, on_fetch=on_fetch)

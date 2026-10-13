"""Reviewed, setup-only publication with conservative ownership and recovery."""

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode

from .errors import AgentError

COMMIT = "Set up ub-agents"
STATE = ".ub-agents/init-setup.json"


def command(args, cwd, runner=None):
    runner = runner or subprocess.run
    result = runner(args, cwd=cwd, capture_output=True, text=True,
                    stdin=subprocess.DEVNULL, timeout=60, check=False)
    if result.returncode:
        # Tool diagnostics can include credentials or arbitrary remote text.
        raise AgentError(f"Command failed: {' '.join(args[:3])} (exit {result.returncode}); repair access and retry init")
    return result.stdout


def saved_attempt(path):
    state = Path(path).absolute().parent / STATE
    if not state.exists():
        return None
    if state.is_symlink() or state.parent.is_symlink():
        raise AgentError("Init recovery state must be a real file and directory")
    try:
        value = json.loads(state.read_text())
        if (not isinstance(value, dict) or value["version"] != 1 or not isinstance(value["files"], dict) or
                any(not isinstance(k, str) or not isinstance(v, str) for k, v in value["files"].items()) or
                any(not isinstance(value.get(k), str) or not value[k] for k in
                    ("repository", "branch", "actor", "config", "default")) or
                not re.fullmatch(r"[0-9a-f]{40,64}", value.get("base", "")) or
                value.get("commit") is not None and not re.fullmatch(r"[0-9a-f]{40,64}", value["commit"]) or
                "ignore_before" not in value or value["ignore_before"] is not None and not isinstance(value["ignore_before"], str)):
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError, OSError):
        raise AgentError("Invalid init recovery state; inspect .ub-agents/init-setup.json before proceeding") from None


@dataclass
class SetupPR:
    plan: object
    github: object
    runner: object = field(default_factory=lambda: subprocess.run)
    branch: str = "setup-ub-agents"
    effects: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)

    def git(self, *args):
        value = command(["git", "-C", str(self.plan.path.parent), *args], self.plan.path.parent, self.runner)
        return value if "status" in args or args[0] == "show" and "-s" not in args else value.strip()

    def files(self):
        root = self.plan.path.parent
        result = {str(p.relative_to(root)): text for p, text in self.plan.targets.items()}
        if (self.plan.ignore_after != (self.plan.ignore_before or b"") or
                getattr(self.plan, "resume_state", None) and ".gitignore" in self.plan.resume_state["files"]):
            result[".gitignore"] = self.plan.ignore_after.decode()
        return result

    def pulls(self):
        owner = self.plan.repository.split("/")[0]
        return self.github.request("repos/" + self.plan.repository + "/pulls?" +
                                   urlencode({"state": "all", "head": f"{owner}:{self.branch}"}),
                                   paginate=True, array=True)

    def inspect(self):
        root = self.plan.path.parent
        if self.plan.config.repository != self.plan.repository:
            raise AgentError("Local repository override differs from the target; review it externally or choose local files only")
        if self.git("rev-parse", "--show-toplevel") != str(root):
            raise AgentError("Setup PR requires the config beside the Git repository root; choose local files only or move --config")
        remote = self.git("remote", "get-url", "origin")
        normalized = remote.removesuffix(".git").rstrip("/")
        if normalized not in {f"https://github.com/{self.plan.repository}",
                              f"git@github.com:{self.plan.repository}",
                              f"ssh://git@github.com/{self.plan.repository}"}:
            raise AgentError("origin must point to the target repository; repair it outside init or choose local files only")
        metadata = self.github.request("repos/" + self.plan.repository)
        if metadata.get("full_name", "").casefold() != self.plan.repository.casefold():
            raise AgentError("GitHub returned a different repository")
        self.default = metadata.get("default_branch")
        if not isinstance(self.default, str) or not self.default:
            raise AgentError("Default branch unavailable; repair GitHub access")
        if self.branch == self.default or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", self.branch):
            raise AgentError("Choose a valid setup branch different from the default branch")
        self.git("check-ref-format", "--branch", self.branch)
        self.actor = self.github.actor()
        self.checkout = self.git("rev-parse", "--abbrev-ref", "HEAD")
        self.head = self.git("rev-parse", "HEAD")
        base = self.git("ls-remote", "--exit-code", "origin", "refs/heads/" + self.default).split()
        if len(base) != 2 or base[1] != "refs/heads/" + self.default:
            raise AgentError("Cannot verify the default branch on origin")
        self.base = base[0]
        self.attempt = saved_attempt(self.plan.path)
        if self.attempt:
            a = self.attempt
            if (a.get("repository") != self.plan.repository or a.get("branch") != self.branch or
                    a.get("actor") != self.actor or a.get("config") != self.plan.path.name or
                    a.get("default") != self.default or a.get("files") != self.files()):
                raise AgentError("A different setup attempt exists; finish its reviewed recovery before starting another")
            self.base = a["base"]
            if self.checkout == self.branch and not a.get("commit") and self.head != self.base:
                self.verify_commit(self.head)
                if self.git("show", "-s", "--format=%s", self.head) != COMMIT:
                    raise AgentError("Unrecorded commit is not the reviewed setup commit")
                a["commit"] = self.head
            if self.checkout != self.branch or self.head not in {a["base"], a.get("commit")}:
                raise AgentError("Setup checkout changed; return to the recorded setup branch without resetting or overwriting it")
            self.base = a["base"]
            if a.get("commit"):
                self.verify_commit(a["commit"])
        elif self.head != self.base or self.checkout == "HEAD":
            raise AgentError("Setup PR needs a checkout at origin's default-branch tip; prepare a clean checkout externally or choose local files only")
        dirty = self.git("--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all")
        # NUL-delimited paths avoid ambiguity from spaces, quotes and renames.
        changed = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        if dirty:
            allowed = set(self.files()) | {STATE} if self.attempt else set()
            for row in changed.split("\0"):
                if row and (row[:2] not in {"??", " M", "A ", "M "} or row[3:] not in allowed):
                    raise AgentError("Checkout has unrelated changes or staged files; commit them externally or choose local files only")
        for name, content in self.files().items():
            p = root / name
            if self.attempt and p.exists() and (p.is_symlink() or p.read_text() != content):
                raise AgentError(f"Setup file changed: {name}; inspect and finish manually without overwriting it")
        local = self.git("for-each-ref", "--format=%(objectname)", "refs/heads/" + self.branch)
        remote = self.git("ls-remote", "origin", "refs/heads/" + self.branch).split()
        if local and not (self.attempt and local == self.head):
            raise AgentError("Setup branch already exists and is not this verified attempt; rename it in Review")
        if remote and not (self.attempt and remote[0] == self.attempt.get("commit")):
            raise AgentError("Remote setup branch collision; inspect it externally or choose another reviewed branch")
        self.pr = None
        pulls = self.pulls()
        if len(pulls) > 1:
            raise AgentError("Multiple setup PRs found; resolve the collision externally")
        for pr in pulls:
            self.verify_pr(pr)
            self.pr = pr
        return self

    def marker(self):
        content = json.dumps(self.files(), sort_keys=True).encode()
        return "<!-- ub-agents setup: " + hashlib.sha256(content).hexdigest() + " -->"

    def verify_pr(self, pr):
        a = self.attempt
        number = pr.get("number")
        if type(number) is not int or number <= 0 or pr.get("html_url") != f"https://github.com/{self.plan.repository}/pull/{number}":
            raise AgentError("Setup PR identity or URL cannot be verified")
        if (not a or not a.get("commit") or pr.get("head", {}).get("sha") != a["commit"] or
                pr.get("head", {}).get("ref") != self.branch or
                pr.get("head", {}).get("repo", {}).get("full_name") != self.plan.repository or
                pr.get("base", {}).get("ref") != self.default or
                pr.get("user", {}).get("login") != self.actor or self.marker() not in pr.get("body", "")):
            raise AgentError("Existing PR is not a verified matching setup attempt; inspect it externally")
        if pr.get("state") == "closed" and not pr.get("merged_at"):
            raise AgentError("The matching setup PR was closed without merging; reopen it externally before retrying")

    def verify_commit(self, sha):
        if self.git("show", "-s", "--format=%P", sha) != self.base:
            raise AgentError("Setup commit has an unexpected parent; inspect it externally")
        names = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha).split("\0")
        if {p for p in names if p} != set(self.files()):
            raise AgentError("Setup commit contains unexpected files; inspect it externally")
        for name, content in self.files().items():
            if self.git("show", f"{sha}:{name}") != content:
                raise AgentError("Setup commit content differs from the reviewed setup")

    def preview(self):
        return (f"Repository: {self.plan.repository}\nPosting identity: {self.actor} (authenticated gh account)\n"
                f"Default branch: {self.default}\nBase ref: origin/{self.default} ({self.base})\n"
                f"Current checkout: {self.checkout} ({self.head})\nSetup branch: {self.branch}\n"
                f"Create or switch to {self.branch}; commit only: {', '.join(self.files())}\n"
                f"Exact setup-only commit message: {COMMIT}\nPush: origin HEAD:refs/heads/{self.branch}\n"
                "A new remote branch must still be absent at push time; an existing verified push is reused.\n"
                f"Create or reuse setup PR against {self.default}; review and merge under project policy.\n"
                "Init never merges, enables auto-merge or pushes to the default branch.")

    def save(self):
        state = self.plan.path.parent / STATE
        if state.is_symlink() or state.parent.is_symlink():
            raise AgentError("Unsafe init recovery state path")
        state.parent.mkdir(exist_ok=True)
        state.write_text(json.dumps(self.attempt, indent=2) + "\n")

    def record_files(self):
        self.attempt["files"] = self.files()
        self.attempt["choices"] = asdict(self.plan.choices)
        self.attempt["choices"]["features"] = sorted(self.plan.choices.features)
        self.save()

    def begin(self):
        # Repeat every read-only safety check at the authorization boundary.
        expected = (self.head, self.base, self.actor)
        self.inspect()
        if (self.head, self.base, self.actor) != expected:
            raise AgentError("Checkout, base or account changed since Review; review setup again")
        if not self.attempt:
            self.uncertain.append(f"Setup branch creation may have completed: {self.branch}; inspect the checkout before recovery")
            self.git("switch", "-c", self.branch, self.base)
            self.uncertain.pop()
            self.effects.append(f"Created and switched to setup branch {self.branch} (base {self.base})")
            self.attempt = {"version": 1, "repository": self.plan.repository, "config": self.plan.path.name,
                            "branch": self.branch, "actor": self.actor, "default": self.default,
                            "base": self.base, "files": self.files(), "ignore_before": self.plan.ignore_before.decode() if self.plan.ignore_before is not None else None,
                            "commit": None}
            self.record_files()
        return f"Setup branch: {self.branch} (base {self.base})"

    def publish(self, result):
        try:
            if self.git("rev-parse", "--abbrev-ref", "HEAD") != self.branch:
                raise AgentError("Checkout changed during Apply; finish setup manually")
            for name, content in self.files().items():
                path = self.plan.path.parent / name
                if path.is_symlink() or path.read_text() != content:
                    raise AgentError(f"Setup file changed during Apply: {name}; inspect it before retrying")
            # --only preserves unrelated staged files even if another process stages
            # them after the cleanliness check. No broad add or commit is used.
            if not self.attempt.get("commit"):
                if self.git("rev-parse", "HEAD") != self.base:
                    raise AgentError("Setup branch advanced unexpectedly; inspect it externally")
                self.git("add", "--", *self.files())
                result.incomplete.append("Setup commit may have completed; inspect git log and the setup paths before retrying")
                self.git("commit", "--only", "-m", COMMIT, "--", *self.files())
                self.attempt["commit"] = self.git("rev-parse", "HEAD")
                result.incomplete.pop()
                result.completed.append(f"Setup-only commit: {self.attempt['commit']}")
                self.save()
            else:
                result.completed.append(f"Reused setup-only commit: {self.attempt['commit']}")
            sha = self.attempt["commit"]
            self.verify_commit(sha)
            if self.pr and self.pr.get("merged_at"):
                result.pr_number, result.pr_url = self.pr["number"], self.pr["html_url"]
                result.completed.append(f"Reused merged setup PR: {result.pr_url}")
                return result
            remote = self.git("ls-remote", "origin", "refs/heads/" + self.branch).split()
            if remote and remote[0] != sha:
                raise AgentError("Remote branch changed; do not overwrite it; resolve externally")
            if not remote:
                # The empty expected value requires absence on the server. This
                # cannot replace an existing ref, including a branch created
                # between ls-remote and push. Never use a broad force push.
                self.git("push", "origin", f"HEAD:refs/heads/{self.branch}",
                         f"--force-with-lease=refs/heads/{self.branch}:")
                result.completed.append(f"Pushed {sha} to origin/{self.branch}")
            else:
                result.completed.append(f"Reused verified push: {sha} on origin/{self.branch}")
            self.attempt["pushed"] = True
            self.save()
            pulls = self.pulls()
            if len(pulls) > 1:
                raise AgentError("Multiple matching PRs; resolve externally")
            if pulls:
                self.verify_pr(pulls[0])
                self.pr = pulls[0]
            else:
                result.incomplete.append("Setup PR creation may have completed; inspect the recorded branch on GitHub before retrying")
                self.pr = self.github.request("repos/" + self.plan.repository + "/pulls", "POST",
                    {"title": COMMIT, "head": self.branch, "base": self.default,
                     "body": self.marker() + "\n\nReview the setup files, project checks, permissions and authority, then merge under the project's normal policy. Init never merges or starts the loop automatically."})
                self.verify_pr(self.pr)
                result.incomplete.pop()
            result.pr_number = self.pr["number"]
            result.pr_url = self.pr["html_url"]
            result.completed.append(f"Setup PR: {result.pr_url}; review and merge under project policy")
            self.attempt["pr"] = result.pr_number
            self.save()
        except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            result.error = f"Setup PR publication stopped: {exc if isinstance(exc, AgentError) else type(exc).__name__}"
            result.remaining.append("Retry the same init command from the recorded setup branch; verified files, commit and PR are reused. Inspect GitHub after an unavailable response before retrying.")
        return result


def apply_setup(publication, github, labels=(), *, verify_boards=False):
    from .init_setup import Applied, apply_plan
    try:
        branch = publication.begin()
    except (AgentError, OSError, UnicodeError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        return Applied(completed=publication.effects.copy(),
                       incomplete=publication.uncertain.copy(),
                       error=f"Setup branch preparation stopped: {exc if isinstance(exc, AgentError) else type(exc).__name__}",
                       remaining=["Inspect the current branch and .ub-agents/init-setup.json before retrying; never reset or overwrite an existing branch."])
    result = apply_plan(publication.plan, github, labels, verify_boards=verify_boards,
                        before_files=publication.record_files)
    result.completed.insert(0, branch)
    if result.ok:
        publication.publish(result)
    return result

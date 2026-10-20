# Codex sandbox options

Design spike for [#617](https://github.com/uberblick-ai/ub-agents/issues/617),
tested on 2026-10-20. Internal design notes only; no behavior, configuration or
default changes.

All Codex results below use real, fresh `codex exec` sessions on the launcher's
**Ubuntu 26.04.1 LTS, Linux 7.0.0-30-generic, x86_64**, with **codex-cli 0.159.3**
and `gpt-6.1-sol` at low effort. Git was 2.53.0, GitHub CLI 2.102.0 and Docker
Engine 29.8.2. No macOS or Windows testing was available in this run.

## Options

“Tested” means the small experiment described below, rather than a complete
ub-agents role run. Size is an implementation estimate: **S** means argument and
guidance changes with focused tests; **M** includes checkout lifecycle or
deployment work; **L** changes publication ownership and recovery.

| Option | Works? (tested, Codex version, OS) | What it protects | Change in ub-agents | Size |
| --- | --- | --- | --- | --- |
| 1. Current worktrees, default `workspace-write`, or add the main checkout as a root | **No commits**, tested 0.159.3 / Ubuntu Linux: `index.lock` is read-only. | Working files are writable; Git metadata and unrelated filesystem writes remain blocked. | Insufficient permission change on its own. | S, insufficient |
| 1. Add only the run's resolved Git directory | **No commits**, tested 0.159.3 / Ubuntu Linux: indexing fails to write the common object database. | Other repository metadata and unrelated filesystem writes remain blocked. | Insufficient root list; Git also needs common metadata. | S, insufficient |
| 1. Explicitly add both common `.git` and the run's resolved Git directory | **Yes**, tested 0.159.3 / Ubuntu Linux: add and commit succeed; outside write fails. Adding common `.git` alone still fails. | Unrelated filesystem writes remain blocked, but shared objects, refs, hooks, config and other worktrees' metadata are writable. | Resolve both Git directories per run, pass explicit roots and scratch access, revise role permissions/guidance. | S |
| 2. Per-run clone, or separate Git directory, with explicit local metadata access | **Yes**, tested 0.159.3 / Ubuntu Linux: shared clone, independent clone and separate Git directory commit successfully. A clone with default permissions fails. | Writes stay in the run's checkout and metadata; source Git metadata need not be writable. Local hooks/config are writable. | Replace worktree setup, recovery, branch discovery and cleanup; configure local metadata and scratch roots. | M |
| 3. `workspace-write` with `network_access=true` | **Yes**, tested 0.159.3 / Ubuntu Linux: disposable TCP Git push and read-only `gh` call succeed; both fail with network off. | Filesystem restrictions remain; this setting alone imposes no destination allowlist. | Opt selected roles into command networking, alongside a working Git permission/layout choice. | S |
| 4. Agent edits; launcher commits, pushes and creates the PR | **Partly tested**, 0.159.3 / Ubuntu Linux: sandboxed edit, then host commit and push to a disposable remote succeed. Launcher integration and PR creation untested. | Can keep Git metadata and publication credentials outside the agent's authority, if reads, environment and tools are also separated. | Add an artifact/handoff contract, trusted publisher, validation, ownership checks, retries and crash recovery; move role publication duties. | L |
| 5. `danger-full-access` inside an isolated whole launcher | **Partly tested**, 0.159.3 / Ubuntu Linux: unrestricted Codex can overwrite an outside sentinel; a Codex-driven Docker smoke test commits internally and blocks a mounted sentinel write. Full launcher and VM untested. | A correctly configured container/VM limits host access; everything and every credential inside remains available to the agent. | Document/provision the isolated launcher, storage, identity, egress, recovery and upgrades. No role protocol change required. | M, operational |
| 6. Named permission profile extending `:workspace` | **Yes**, tested 0.159.3 / Ubuntu Linux with explicit common and resolved Git grants: commit succeeds and outside write fails. | Same Git exposure as the selected grants; profiles can also express read denials and network proxy rules. | Generate/select a profile, retain baseline protections and validate supported CLI versions and managed policy. | S–M |
| 7. `--approve-for-me` / automatic approval review | **Yes in one trial**, 0.159.3 / Ubuntu Linux: initial sandboxed add fails, then escalated add and commit succeed. Approval quality and repeatability untested. | Normal commands remain sandboxed; approved escalations execute beyond that boundary. | Configure automatic approval review and project policy; evaluate which publication commands may leave the sandbox. | S–M |

**Recommendation:** Prefer a follow-up implementation using per-run independent
clones with `workspace-write`, explicitly writable local Git metadata, and command
networking only for roles that need it. This avoids granting writes to the
launcher's shared Git metadata. Keeping worktrees with both explicit Git roots
is a smaller transition when shared repository trust is acceptable. Use an
isolated whole launcher when protecting host reads and credentials matters;
`workspace-write` alone does not provide that protection. Defer launcher-owned
publication until its ownership and recovery contract is designed. This spike
changes no behavior or defaults.

## Findings and limits

### The existing worktree claim needs qualification

The comment in `ub-agents.yaml` is correct about default `workspace-write` and
about adding just `.git/worktrees/<run>`. It is too broad if read as saying that
explicit metadata roots cannot work at all: **both the resolved Git directory and
the common Git directory explicitly writable allowed a real commit** in this CLI.

OpenAI documents recursive protection for a root's `.git` directory or pointer
file and the directory that pointer resolves to. Adding a parent working
directory does not remove that protection.
[Protected paths in writable roots](https://learn.chatgpt.com/docs/agent-approvals-security#protected-paths-in-writable-roots).

The experiments distinguish three failures: the default and parent-root cases
cannot create the worktree index lock; granting only that worktree Git directory
gets past the lock but cannot insert objects; granting common `.git` alone still
leaves the resolved worktree Git directory protected. Explicitly listing both
works. This is observed behavior of 0.159.3, not a portability promise for future
CLI versions or a way around administrator-enforced restrictions.

Giving write access to the whole common Git directory also gives access to
metadata shared with the launcher and other runs. It permits changing refs,
configuration or hooks and corrupting shared objects. A per-run clone avoids that
particular shared write grant. Both a `--shared` clone and a
`--no-hardlinks` clone worked; prefer independently owned objects for a production
design so source object retention does not become part of run recovery.

A separate Git directory is also workable **with an explicit grant**. Moving
metadata alone does not imply permission: a `.git` pointer's target is normally
protected. There is no need to disguise metadata paths or bypass Git discovery.

### Networking and credential boundaries

Network-on preserved the outside filesystem write denial. It allowed a real
push over TCP to a temporary bare repository, without writable access to that
remote's filesystem. Git's server performed the remote writes. The `gh` test
only read GitHub API rate-limit information; no experiment created GitHub issues,
PRs or branches in a real repository.

The tested networking flag grants command network access, not a GitHub-only
policy. Current profiles additionally support a network proxy and domain rules;
domain rules need an active proxy. Those proxy rules were not tested here.
[Network permissions](https://learn.chatgpt.com/docs/permissions#network-permissions),
[configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

All filesystem probes could read a harmless sibling sentinel. Default
`workspace-write` is therefore not a host-secret read boundary. Network-on plus
readable credentials still permits credential use and data export. Configured
MCP servers, apps and web tools have their own authority; command sandbox settings
do not isolate those integrations. Profiles can express read restrictions, but
a complete credential/read policy was outside this experiment.
[Scope and enforcement](https://learn.chatgpt.com/docs/permissions#scope-and-enforcement).

The current argv adapter expands only `{scratch}` and `{report_command}`, not
per-run Git directory placeholders. A worktree-based implementation would need
to resolve the common and worktree metadata and add them without broadening
writes to the operator checkout. Every layout also needs the private scratch
directory writable and the installed report command readable; reporting uses
GitHub and so needs networking. None of these trials invoked the real run's
report command from a nested agent.

### Launcher-owned publication

The trial proved the file-edit/host-publication split, not a finished publisher.
A production contract must specify the expected base/head, changed files, commit
message, issue link, PR metadata and the ownership lease. The launcher must
validate those inputs, handle untracked files and retries, preserve unpublished
work after crashes, and make repeated publication idempotent.

Keeping credentials outside the agent requires more than changing who invokes
`git push`: isolate reads/environment and remove agent publication-capable tools.
Trusted publication must also account for hooks and generated code; source edited
by the agent must not become an unrestricted credential-bearing publisher.
PR creation was intentionally untested to avoid a second experimental GitHub
deliverable. No prototype launcher code was added.

### Isolating the whole launcher

The Docker test exercised a disposable process boundary; **Codex itself stayed
outside that container and invoked Docker**. It did not establish that the
whole launcher or authenticated Codex works inside that image. The image was
already available and no credentials or Docker socket were mounted into it.
A full isolated launcher needs its own authentication and persistent run state;
that deployment was not provisioned during this spike. No VM was provisioned,
so VM behavior is a proposed setup rather than a measured result.

Document a dedicated VM, or a dedicated unprivileged container, containing the
launcher, operator clone, all private workspaces, Git metadata and run state.
Install pinned ub-agents, Codex, Git, `gh`, Python and required check tools there.
Keep durable storage inside that boundary for crash recovery, with explicit
backup, upgrade and cleanup procedures. Provision a dedicated identity with
repository-scoped publication access and its own Codex authentication. Assume
agents can read every credential provided inside it.

For a container, use a non-root user, drop capabilities, enable
`no-new-privileges`, keep the image filesystem read-only, provide only required
writable state/temp volumes, set process/memory limits and restrict outbound
traffic to the required model, GitHub and dependency endpoints. Do not expose
the host's home, repositories, SSH agent or container-engine socket. The smoke
test used `--network none`; a working launcher needs a separate egress design.
Container isolation shares a host kernel; use a dedicated VM when that boundary
is insufficient or arbitrary build tools require broader privileges.
These are proposed deployment requirements, not a tested production recipe.

### Profiles and approval review

The named-profile trial used explicit grants for both metadata paths, retaining
`:workspace` as its parent. Granting common metadata without the more specific
resolved directory still failed. The documented profile model can override
baseline read-only rules and combine filesystem and network policy. Select
either profiles or the older sandbox settings for a session, rather than mixing
them. [Permission profiles](https://learn.chatgpt.com/docs/permissions).

Automatic review is another option for unattended runs without a global full
access flag. The trial showed successful local Git escalations, not that
automatic review safely constrains every agent-generated command. A reviewed
command executes outside the sandbox when granted that escalation.
`--approve-for-me` cannot be combined with `--sandbox` in 0.159.3; it selects the
workspace sandbox itself. Manual approval would need an interactive operator
and was not tested in this non-interactive session.
[Automatic approval review](https://learn.chatgpt.com/docs/sandboxing/auto-review).

## Commands run and their output

Paths below are normalized to `<LAB>` (a private scratch directory), `<NAME>`
(the throwaway worktree name) and `<PORT>`. Disposable commit IDs are replaced
by `<COMMIT>`. No hostnames, local usernames, credentials or original local
paths are included. The only author identity below is synthetic.

Each Codex invocation was fresh and also included these common arguments:

```sh
--ignore-user-config --ignore-rules --ephemeral --json -m gpt-6.1-sol \
  -c 'model_reasoning_effort="low"' -c 'approval_policy="never"'
```

The commands below omit those common arguments for readability. The prompt
directed Codex to execute the fixed driver once, report actual errors, and avoid
approval/escalation except in the explicit automatic-review trial. The driver
captured subprocess exit codes and continued after expected failures. Every
successful experiment's `codex exec` process exited 0; Git failures below are
the child command results, not an inference from the model's final message.

### Environment and disposable setup

```text
codex --version
codex-cli 0.159.3
uname -srm
Linux 7.0.0-30-generic x86_64
/etc/os-release
Ubuntu 26.04.1 LTS
git --version
git version 2.53.0
gh --version
gh version 2.102.0 (2026-09-30)
docker version --format '{{.Server.Version}}'
29.8.2
```

The host created the fixture; each listed worktree used a separate branch:

```sh
git init -b main <LAB>/seed
git -C <LAB>/seed config user.name 'Sandbox Probe'
git -C <LAB>/seed config user.email sandbox@example.invalid
# Write tracked.txt with "seed", then:
git -C <LAB>/seed add tracked.txt
git -C <LAB>/seed commit -m 'Seed disposable sandbox probe'
git -C <LAB>/seed worktree add -b <NAME> <LAB>/<NAME>
git clone --shared <LAB>/seed <LAB>/shared-clone
git clone --shared <LAB>/seed <LAB>/clone-explicit
git clone --no-hardlinks <LAB>/seed <LAB>/clone-owned
git clone --separate-git-dir <LAB>/separate-metadata <LAB>/seed <LAB>/separate-checkout
git init --bare <LAB>/remote.git
```

All exited 0. `outside.txt` was a harmless sibling of these working directories,
outside every granted root. The Git driver, placed in each working directory,
was equivalent to:

```python
from pathlib import Path
import subprocess

Path("commit-probe.txt").write_text("fixed driver sandbox probe\n")
print("workspace edit: success")
for args in (
    ["git", "add", "commit-probe.txt"],
    ["git", "-c", "user.name=Sandbox Probe",
     "-c", "user.email=sandbox@example.invalid",
     "commit", "-m", "Sandbox probe"],
    ["git", "status", "--short"],
):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    print(args, "EXIT:", result.returncode)
    print(result.stdout)
outside = Path("<LAB>/outside.txt")
print("outside read:", outside.read_text().strip())
try:
    outside.write_text("changed\n")
    print("outside write: success")
except OSError as error:
    print("outside write:", type(error).__name__, error.errno, error.strerror)
```

### 1. Worktree metadata roots

```sh
codex exec -C <LAB>/baseline --sandbox workspace-write 'Run python3 probe.py once.'
codex exec -C <LAB>/parent-root --sandbox workspace-write \
  -c 'sandbox_workspace_write.writable_roots=["<LAB>/seed"]' 'Run python3 probe.py once.'
codex exec -C <LAB>/leaf-only --sandbox workspace-write \
  -c 'sandbox_workspace_write.writable_roots=["<LAB>/seed/.git/worktrees/leaf-only"]' \
  'Run python3 probe.py once.'
codex exec -C <LAB>/common-only --sandbox workspace-write \
  -c 'sandbox_workspace_write.writable_roots=["<LAB>/seed/.git"]' 'Run python3 probe.py once.'
codex exec -C <LAB>/exact-root --sandbox workspace-write \
  -c 'sandbox_workspace_write.writable_roots=["<LAB>/seed/.git/worktrees/exact-root","<LAB>/seed/.git"]' \
  'Run python3 probe.py once.'
```

Trimmed results:

```text
baseline / parent-root / common-only:
  workspace edit: success
  git add: exit 128
  fatal: Unable to create '<LAB>/seed/.git/worktrees/<NAME>/index.lock': Read-only file system
  git commit: exit 128, same index.lock error

leaf-only:
  git add: exit 128
  error: unable to create temporary file: Read-only file system
  error: commit-probe.txt: failed to insert into database
  fatal: adding files failed
  git commit: exit 1, nothing added to commit

exact-root (both paths):
  git add: exit 0
  git commit: exit 0
  [exact-root <COMMIT>] Sandbox probe
   1 file changed, 1 insertion(+)

all:
  outside read: succeeded
  outside write: OSError 30 Read-only file system
```

### 2. Checkout layouts

```sh
codex exec -C <LAB>/shared-clone --sandbox workspace-write 'Run python3 probe.py once.'
codex exec -C <LAB>/clone-explicit --sandbox workspace-write \
  --add-dir <LAB>/clone-explicit/.git 'Run python3 probe.py once.'
codex exec -C <LAB>/clone-owned --sandbox workspace-write \
  --add-dir <LAB>/clone-owned/.git 'Run python3 probe.py once.'
codex exec -C <LAB>/separate-checkout --sandbox workspace-write \
  -c 'sandbox_workspace_write.writable_roots=["<LAB>/separate-metadata"]' \
  'Run python3 probe.py once.'
```

```text
shared-clone, no explicit metadata grant:
  git add and commit: exit 128
  fatal: Unable to create '<LAB>/shared-clone/.git/index.lock': Read-only file system

clone-explicit / clone-owned / separate-checkout, explicit metadata grant:
  git add: exit 0
  git commit: exit 0
  [main <COMMIT>] Sandbox probe
   1 file changed, 1 insertion(+)
  outside read: succeeded
  outside write: OSError 30 Read-only file system
```

### 3. Command networking

The host started a temporary `git daemon`, bound to loopback on an unused port,
with receive-pack enabled only for the disposable repositories. It seeded
`remote.git` over TCP and confirmed the read-only GitHub request worked outside
the sandbox. The daemon was stopped and waited for before the experiment ended.

```sh
git daemon --listen=127.0.0.1 --port=<PORT> --base-path=<LAB> --export-all --enable=receive-pack
git -C <LAB>/seed push git://127.0.0.1:<PORT>/remote.git main:refs/heads/seed
gh api --hostname github.com rate_limit --jq '.resources.core.limit > 0'
codex exec -C <LAB>/network --sandbox workspace-write \
  -c sandbox_workspace_write.network_access=false 'Run python3 network_probe.py once.'
codex exec -C <LAB>/network --sandbox workspace-write \
  -c sandbox_workspace_write.network_access=true 'Run python3 network_probe.py once.'
```

The fixed network driver ran these commands inside the sandbox. The actual push
was included only in the network-on trial:

```sh
git push --dry-run git://127.0.0.1:<PORT>/remote.git HEAD:refs/heads/network-probe
gh api --hostname github.com rate_limit --jq '.resources.core.limit > 0'
git push git://127.0.0.1:<PORT>/remote.git HEAD:refs/heads/network-probe
```

```text
host control:
  git push: exit 0
  gh: exit 0, true

network_access=false:
  git push --dry-run: exit 128
  fatal: unable to connect to 127.0.0.1:
  127.0.0.1[0: 127.0.0.1]: errno=Operation not permitted
  gh: exit 1, error connecting to api.github.com

network_access=true:
  git push --dry-run: exit 0, [new branch] HEAD -> network-probe
  gh: exit 0, true
  git push: exit 0, [new branch] HEAD -> network-probe

both:
  outside write: OSError 30 Read-only file system

host verification, git --git-dir <LAB>/remote.git show-ref:
  <COMMIT> refs/heads/network-probe
  <COMMIT> refs/heads/seed
```

### 4. Agent edit, host publication

```sh
codex exec -C <LAB>/edit-only --sandbox workspace-write \
  'Create deliverable.txt containing launcher handoff. Run git diff --stat. Do not stage, commit, push, or contact GitHub.'
# After Codex exited, from the host in <LAB>/edit-only:
git add deliverable.txt
git commit -m 'Launcher publishes edit-only result'
git push <LAB>/remote.git HEAD:refs/heads/launcher-publish
```

```text
Codex file write: exit 0
git diff --stat: exit 0, empty (the new file was untracked)
host git add: exit 0
host git commit: exit 0
[edit-only <COMMIT>] Launcher publishes edit-only result
 1 file changed, 1 insertion(+)
host git push: exit 0, [new branch] HEAD -> launcher-publish
```

### 5. Full access and Docker boundary smoke test

```sh
codex exec -C <LAB>/full-access --sandbox danger-full-access 'Run python3 probe.py once.'
codex exec -C <LAB>/full-access --sandbox danger-full-access 'Run python3 container_probe.py once.'
```

The container driver invoked the following Docker command with a Python program
that initialized `/tmp/repo`, wrote/staged/committed `result.txt`, attempted to
write the read-only sentinel mount and checked whether the original host path
existed inside the container:

```sh
docker run --rm --pull=never --network none --user 1000:1000 \
  --cap-drop ALL --security-opt no-new-privileges --read-only \
  --pids-limit 64 --memory 512m --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  --mount type=bind,src=<LAB>/outside.txt,dst=/evidence/outside.txt,readonly \
  mcr.microsoft.com/playwright:v1.62.1-noble python3 -c '<PROBE_PROGRAM>'
```

Image digest:
`sha256:dcc5531e97840b9b5e794f2814476b21571c5124a3fca2267d73041f56e7580e`.

```text
danger-full-access host probe:
  git add: exit 0
  git commit: exit 0
  outside read: succeeded
  outside write: success

Docker smoke test:
  docker exit: 0
  container commit exit: 0
  [main (root-commit) <COMMIT>] Container sandbox probe
   1 file changed, 1 insertion(+)
  mounted sentinel read: succeeded
  mounted sentinel write: OSError 30 Read-only file system
  host sentinel path visible: False
```

### 6. Named permissions profile

The tested profile was passed as one inline TOML `permissions={...}` value,
avoiding dotted CLI override keys containing literal dots in filesystem paths.
Its equivalent config was:

```toml
default_permissions = "git-test"

[permissions.git-test]
extends = ":workspace"

[permissions.git-test.filesystem.":workspace_roots"]
".git" = "write"

[permissions.git-test.filesystem]
"<LAB>/seed/.git" = "write"
"<LAB>/seed/.git/worktrees/profile" = "write"
```

```sh
codex exec -C <LAB>/profile -c 'default_permissions="git-test"' \
  -c 'permissions={git-test={extends=":workspace",filesystem={":workspace_roots"={".git"="write"},"<LAB>/seed/.git"="write","<LAB>/seed/.git/worktrees/profile"="write"}}}' \
  'Run python3 probe.py once.'
```

```text
profile with common grant but no explicit resolved-directory grant:
  git add and commit: exit 128, index.lock: Read-only file system
profile with both explicit grants:
  git add: exit 0
  git commit: exit 0
  [profile <COMMIT>] Sandbox probe
   1 file changed, 1 insertion(+)
  outside read: succeeded
  outside write: OSError 30 Read-only file system
```

### 7. Automatic approval review

```sh
codex exec -C <LAB>/auto-review --approve-for-me \
  -c 'approval_policy="on-request"' \
  'Run python3 prepare.py inside the sandbox first. If git add fails, request approval for git add approval-probe.txt and then git commit in this disposable repository.'
```

The preparation driver only wrote `approval-probe.txt` and attempted the initial
`git add`. The later `approval_policy` override replaced the common `never`
setting. No extra writable metadata roots were supplied.

```text
sandboxed git add: exit 128
fatal: Unable to create '<LAB>/seed/.git/worktrees/auto-review/index.lock': Read-only file system
escalated git add approval-probe.txt: exit 0
escalated git commit -m 'Approved disposable commit': exit 0
[auto-review <COMMIT>] Approved disposable commit
 1 file changed, 1 insertion(+)
```

## Deliverable checks

The worktree-local standard checks passed: virtualenv creation, editable install,
`python -m tests` (2,149 tests), `ub-agents doctor --offline` (zero required
failures or warnings) and `git diff --check`. Only this design note belongs in
the handoff PR; all experiment scripts and fixtures remained in private scratch
storage. No production permissions, templates, docs pages or defaults changed.

# Issue #10: owner-run Docker feasibility spike

This branch is experiment setup, not a supported runner. Nothing on it is to be
merged. Keep its PR draft, without workflow labels. The owner runs all commands
involving credentials, test issues or live workers, and commits the findings on
this same branch. The implementer builds the image and runs credential-free
checks only. See the [owner routing decision](https://github.com/uberblick-ai/ub-agents/issues/10#issuecomment-5948022298)
and the current [issue body and comments](https://github.com/uberblick-ai/ub-agents/issues/10).

## What is being tested

The image installs a committed ub-agents snapshot and Codex CLI 0.159.3. It uses
the current `codex:gpt-6.1-sol:high` implementer role and
`--sandbox danger-full-access`. Each worker clones the repository into its own
Docker-managed volume; the host checkout is never a container mount or clone
source. Its control clone stays on the remote default branch because the current
launcher requires it and fast-forwards before claiming. The experimental config
and combined role are ignored local overlays; no supported files change.

Each worker has one issue-specific trigger, `docker-spike-10-N-ready`, and agent
identity, `docker-spike-implementer-N`. Two contenders for N have the same identity;
different test issues have different identities so outcome recovery from an older
experiment cannot execute through another issue's config. The image rejects
normal-label overlap and requires the trigger to match exactly the chosen test
issue at preflight. These are cooperative guards, not protection against an agent
with the shared GitHub token changing labels or configuration later.

The completion deviation is deliberate: tests publish draft PRs and report
`completed` on their issue without a handoff. Only `docker-spike-10-done` is added.
The normal reviewer and integrator must never receive these PRs. The rest of the
implementer workflow, branch continuation and supervision are unchanged.

## Owner preparation

Use an owner-controlled **separate clone** of the spike branch. Do not run from
the operator's control checkout, whose launch refresh requires the default branch.
Substitute the draft PR's actual branch below:

```sh
git clone https://github.com/uberblick-ai/ub-agents.git ub-agents-docker-spike
cd ub-agents-docker-spike
git switch --track origin/ub-agent/implementer/10/744c880bb5bc42b9a1612451f7534187
python3 -m venv .venv
.venv/bin/pip install -q -e .
export IMAGE=ub-agents-spike10:owner-test
export SPIKE=spikes/docker-runner/spike.py
export GH_REPO=uberblick-ai/ub-agents
python3 "$SPIKE" build --image "$IMAGE"
git rev-parse HEAD
docker version
docker context show
docker image inspect "$IMAGE" --format '{{.Id}} {{json .RepoDigests}} {{.Os}}/{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
```

`build` uses only an allowlisted **HEAD** archive, never uncommitted files, `.git`,
home, local artifacts or a credential file. Commit any setup corrections before
rebuilding. `.ub-agent/docker-spike/build.json` records the framework commit,
platform, image ID and repository digests. A locally built image normally has no
registry digest: record its immutable `sha256:` image ID as the local content
digest, and record `RepoDigests: []` explicitly. Do not invent a registry digest
or publish an image to obtain one. The base image and apt packages can change
between builds, so results are tied to the **image ID**, not just the Dockerfile.
Start commands resolve a tag to its ID once for both workers.

The implementer and owner can run this credential-free prerequisite check:

```sh
python3 spikes/docker-runner/smoke.py --image "$IMAGE"
```

It runs with networking disabled and starts no agent. It checks UID, dropped
capabilities, no-new-privileges, configuration, installed tools, `ps`, volume write
and stopped-container artifact recovery. It removes only its own uniquely named
smoke container/volume and saves `.ub-agent/docker-spike/smoke.json`. This supplies
setup evidence; it does not replace the owner's live isolation/recovery tests.

### Credentials (owner only)

Provision GitHub credentials for the **same account as the normal launcher** and
for both workers. The repository's trusted-comment contract requires that account.
Choose a target-repository fine-grained token, the existing token, or separate
runs with both; record the choice. Needed operations are repository clone/fetch/
push, issue and PR reads, creation/editing of comments and draft PRs, and issue
label transitions. Fine-grained tokens generally need Contents, Issues and Pull
requests read/write plus Metadata read. Verify effective access; do not infer it
from requested scopes or the doctor's repository-level `permissions` response.

The prototype accepts either an OpenAI API key or an owner-provisioned Codex
`auth.json` object. Select one per run, with access to the exact configured model.
API keys use API billing. The owner, not the implementer, may supply a login cache;
never mount the host `.codex` directory. For two OAuth workers, token refresh may
conflict or invalidate the copied cache; record this. Refreshed auth is intentionally
temporary and is not exported on stop; the owner must provision fresh auth again.

Create a mode-0600 JSON file **outside any checkout**, using a secret manager or
these prompted commands. No token appears in argv or shell history:

```sh
export SPIKE_CREDS=/absolute/owner/private/docker-spike-credentials.json
python3 - <<'PY'
import getpass, json, os
from pathlib import Path
os.umask(0o077)
p = Path(os.environ['SPIKE_CREDS'])
with p.open('x') as f:
    json.dump({'GH_TOKEN': getpass.getpass('GitHub token: '),
               'OPENAI_API_KEY': getpass.getpass('OpenAI API key: ')}, f)
PY
```

For a login cache, instead use the same prompted GitHub token with
`"CODEX_AUTH_JSON": json.loads(Path(input('Owner auth.json path: ')).read_text())`.
Do not also include `OPENAI_API_KEY`. Extra credential keys are rejected.
Supply the committing identity you intend to test:

```sh
export SPIKE_GIT_NAME='Owner-selected Git name'
export SPIKE_GIT_EMAIL='owner-selected-address@example.invalid'
```

The scripts send only this JSON over `docker exec -i` stdin after creation, into
a tmpfs with UID 1000 and mode 0700. They do not pass secrets as Docker environment
values, build args, command args or host mounts. The bootstrap deletes the input
file, keeps the GitHub token in tmpfs and the launcher's inherited `GH_TOKEN`, and
stores provider auth in the tmpfs home's `.codex/auth.json`. The worker can read
all of these. The Docker operator can observe input/processes. Temporary storage
does not hide secrets from either party or prevent deliberate copying/exfiltration.
No host signing keys/config are imported. If repository policy requires signed
worker commits, record that obstacle; do not disable signing to bypass it.

### Dedicated test issues and labels (owner only)

Create five small issues: single-worker S, separate-worker A/B, interruption R,
and contention C. Keep them free of milestones, blockers and **all normal workflow
labels** (`ready`, `needs-changes`, `needs-review`, `ready-to-merge`,
`needs-preparation`, `needs-human`). Use only spike labels. Never target #10.

This body exercises ordinary checks/commit/push/draft PR/report. It changes only a
synthetic experiment file, which will never be merged:

```sh
mkdir -p .ub-agent/docker-spike
cat > .ub-agent/docker-spike/test-issue.txt <<'EOF'
Owner-authorized issue #10 Docker test. Add a synthetic text file at
spikes/docker-experiments/issue-N.txt, where N is this issue's number, containing
only "Docker spike test N". Follow the configured implementer instructions and
run all repository checks. Publish the first checked checkpoint as a draft PR
whose body starts Closes #N. Keep the PR draft, never merge, never mark ready and
never apply the normal workflow labels. At completion, use the spike-only
completed outcome without --handoff. Include the starting base SHA and final SHA.
EOF
gh issue create --repo "$GH_REPO" --title 'Docker spike: single worker' --body-file .ub-agent/docker-spike/test-issue.txt
gh issue create --repo "$GH_REPO" --title 'Docker spike: separate A' --body-file .ub-agent/docker-spike/test-issue.txt
gh issue create --repo "$GH_REPO" --title 'Docker spike: separate B' --body-file .ub-agent/docker-spike/test-issue.txt
gh issue create --repo "$GH_REPO" --title 'Docker spike: interruption' --body-file .ub-agent/docker-spike/test-issue.txt
gh issue create --repo "$GH_REPO" --title 'Docker spike: contention' --body-file .ub-agent/docker-spike/test-issue.txt
export S=101 A=102 B=103 R=104 C=105
```

Replace the example numbers with the five returned issue numbers. For R, edit
its body **before** starting, appending: "After the first checked commit and draft
PR push, wait until an owner comment contains SPIKE-CONTINUE, polling every ten
seconds. Do not report completion while waiting." This makes a draft checkpoint
available before interruption. Do not put that waiting instruction on S/A/B/C.

```sh
gh issue view "$R" --repo "$GH_REPO" --json body --jq .body > .ub-agent/docker-spike/interruption.txt
cat >> .ub-agent/docker-spike/interruption.txt <<'EOF'

After the first checked commit and draft PR push, wait until an owner comment
contains SPIKE-CONTINUE, polling every ten seconds. Do not report completion while waiting.
EOF
gh issue edit "$R" --repo "$GH_REPO" --body-file .ub-agent/docker-spike/interruption.txt
gh label create docker-spike-10-done --repo "$GH_REPO" --color 888888 --description 'Issue 10 experiment complete; no merge'
gh label create docker-spike-10-stop --repo "$GH_REPO" --color 888888 --description 'Issue 10 experiment pause'
for n in "$S" "$A" "$B" "$R" "$C"; do
  gh label create "docker-spike-10-$n-ready" --repo "$GH_REPO" --color 888888 --description 'Issue 10 dedicated test trigger'
  gh issue edit "$n" --repo "$GH_REPO" --add-label "docker-spike-10-$n-ready"
done
```

Skip label creation only if it already exists with the exact intended name. The
implementer does not execute any commands in this preparation section.

## Experiment 1: one worker, isolation and credentials

Start held, allowing read-only probes before spending model budget or claiming:

```sh
python3 "$SPIKE" start --image "$IMAGE" --name ub-spike10-single --issue "$S" --credentials "$SPIKE_CREDS" --git-name "$SPIKE_GIT_NAME" --git-email "$SPIKE_GIT_EMAIL" --hold
docker exec ub-spike10-single id
docker exec ub-spike10-single cat /proc/self/status
docker exec ub-spike10-single ps -axo pid,pgid,stat,comm
docker exec ub-spike10-single sh -c 'test ! -S /var/run/docker.sock && test ! -S /run/docker.sock'
docker exec ub-spike10-single git -C /work/repo rev-parse --show-toplevel
docker exec ub-spike10-single git -C /work/repo remote -v
docker exec ub-spike10-single python3 /opt/spike/with-auth.py gh api user --jq .login
docker exec ub-spike10-single python3 /opt/spike/with-auth.py gh api repos/uberblick-ai/ub-agents --jq .permissions
docker exec ub-spike10-single python3 /opt/spike/with-auth.py gh api rate_limit
docker exec ub-spike10-single codex login status
python3 "$SPIKE" collect --name ub-spike10-single
python3 "$SPIKE" release --name ub-spike10-single
docker exec ub-spike10-single tail -f /work/launcher.log
```

Release within 30 minutes; otherwise bootstrap times out without a claim. End
`tail -f` with Ctrl-C; it is only an observer. Wait for the worker container to
exit. Collect again; inspect its exit code and **GitHub outcomes**, since loop
exit zero alone does not prove a successful assignment.

Record the inspection's mounts, user, privilege/capability flags, read-only root,
PID/network namespaces and security options. `CapEff` must be zero; no capabilities
have been shown necessary by this setup. Compare host `ps` with container `ps`:
host processes should not appear in the container namespace. Use only harmless
owner-selected processes for any signal probe; an unrelated numeric PID could
refer to a container process. Never kill the normal launcher to test isolation.

On the host, create a random marker in an owner-selected temporary directory
**outside** the spike clone, and record its path/hash. While held, run
`docker exec ub-spike10-single test -r /absolute/host/marker` and
`docker exec ub-spike10-single test -e /absolute/operator/checkout/.git` with the
actual paths: expected nonzero. Record output/exit status and inspect that the
marker and operator checkout are unchanged. Mount/namespace evidence plus these
probes supports ordinary-access isolation; it is not a container-escape proof.

Network is ordinary bridge egress with **no egress allowlist**. Record
`docker exec ub-spike10-single ip route` and
`docker exec ub-spike10-single curl -I --max-time 10 https://api.github.com`.
Have the owner start a harmless HTTP server in a temporary marker directory on
the host, then probe its approved port via `host.docker.internal` on Desktop,
or the bridge gateway on Linux. Record reachable host/LAN services and DNS.
Check Docker daemon reachability separately from socket absence: probe only
owner-approved daemon TCP endpoints, if any. An exposed network daemon or host
service can defeat an assumed boundary even without a socket mount. Do not scan
unrelated hosts. Do not claim network isolation from filesystem isolation.

Record GitHub token **type, account, expiry, repository selections and actual
permissions**, including access to another owner-approved repository if it has
broader scopes. `gh api -i user` can expose classic token scope headers; redact
identifiers as needed. Fine-grained token settings and safe reads on approved
resources provide additional evidence; repository-level `.permissions` alone
is insufficient. Use a benign owner-authorized comment on S if testing comment
write access. Do not forge coordination records: the normal lease/outcome comments
already demonstrate that the worker writes as the trusted account. Explain that
it can also write false trusted comments, including beyond S if its token permits.

Identify the provider auth type, account/project scope and spending/model access
from owner settings and the real worker result. Record every credential-readable
location listed above. Do not cat secrets or attach auth files. Framework
`process.log`, prompts, sessions, cloned history and evidence can contain private
data or credentials if an agent prints/copies them; keep raw evidence private and
review/redact before committing excerpts.

## Experiment 2: interrupt and recover

Run R after the single-worker experiment. Use a new container name for each
independent variant (and new issues if repeating after success):

```sh
python3 "$SPIKE" start --image "$IMAGE" --name ub-spike10-recovery --issue "$R" --credentials "$SPIKE_CREDS" --git-name "$SPIKE_GIT_NAME" --git-email "$SPIKE_GIT_EMAIL"
gh issue view "$R" --repo "$GH_REPO" --comments
gh pr list --repo "$GH_REPO" --state open --search "Closes #$R" --json number,url,headRefName,isDraft
python3 "$SPIKE" collect --name ub-spike10-recovery
python3 "$SPIKE" stop --name ub-spike10-recovery
python3 "$SPIKE" collect --name ub-spike10-recovery
```

Stop **after** its draft checkpoint is pushed and while it is waiting. Record
the branch, draft number/head, local commits, worktree contents, process logs,
events, lease ID/state/expiry, retry_after, attempt count and stop exit code.
`stop` gives TERM 30 seconds, then Docker may KILL: record which occurred.
For a separate hard-crash variant replace `stop` with
`python3 "$SPIKE" crash --name ub-spike10-recovery` (SIGKILL). A killed container
retains its volume but may leave an unreleased lease; tmpfs auth disappears.

Inspect the copied `work/repo/.git` and `work/repo/.ub-agent/runs/` after stop.
Committed branches and pushed PRs should remain; graceful framework cleanup can
remove uncommitted worktree contents. A crash can retain them in the volume.
Nothing is automatically pushed on termination. Do not run cleanup or remove
the container/volume before recording recovery evidence.

Restart the same container to re-inject credentials and inspect status while held:

```sh
python3 "$SPIKE" restart --name ub-spike10-recovery --credentials "$SPIKE_CREDS" --hold
docker exec ub-spike10-recovery python3 /opt/spike/with-auth.py ub-agent --config /work/repo/.docker-spike.yaml status --json
```

While an unexpired lease exists, other launches must show owned and not start a
new session. Observe this with a second fresh `start --hold` on R and the same
status command, then release it once to record exclusion (use a distinct name).
The runtime deadline is 180 minutes and lease includes 15 minutes of grace;
a hard crash can require waiting until its recorded expiry. Do not shorten it
in the report or reset a live lease. A released retry has a 60-second initial
backoff, doubled on later attempts; read `retry_after` instead of guessing.
Held preflight lasts only 30 minutes, so stop/restart held later if needed.

After confirmed termination and expiry/backoff, an unfinished crash with its
trigger can normally start a fresh run. If status says blocked or cleanup was
unconfirmed, the owner must first establish termination and perform an explicit
reset, recording why:

```sh
docker exec ub-spike10-recovery python3 /opt/spike/with-auth.py ub-agent --config /work/repo/.docker-spike.yaml retry --number "$R" --agent "docker-spike-implementer-$R" --reason 'Owner confirmed stopped container; inspected retained branch and draft before retry'
```

Run that reset **only if indicated**, never before expiry. The worker needs its
spike trigger retained; a stop label prevents execution until the owner removes
that spike label. Restoring a trigger alone does not clear a blocked result.
Once eligible, let the fresh session finish:

```sh
gh issue comment "$R" --repo "$GH_REPO" --body 'SPIKE-CONTINUE: owner has recorded the interrupted checkpoint and authorizes completion of this test.'
python3 "$SPIKE" release --name ub-spike10-recovery
```

Collect after completion. Record whether `earlier_branches` led to the same draft,
whether another draft appeared, the new run/attempt, labels and accepted outcome.
A result reported before a crash may receive outcome-only recovery; record it
separately from reexecution. Lease expiry is not proof that a process on another
machine died. Docker's stopped-container state proves only this container stopped.

## Experiment 3: two workers on separate issues

```sh
python3 "$SPIKE" start-two --image "$IMAGE" --names ub-spike10-separate-a ub-spike10-separate-b --issues "$A" "$B" --credentials "$SPIKE_CREDS" --git-name "$SPIKE_GIT_NAME" --git-email "$SPIKE_GIT_EMAIL"
python3 "$SPIKE" collect --name ub-spike10-separate-a
python3 "$SPIKE" collect --name ub-spike10-separate-b
gh issue view "$A" --repo "$GH_REPO" --comments
gh issue view "$B" --repo "$GH_REPO" --comments
```

Wait for both containers to exit, collect again and record timing, separate mount
sources/clones, assignments, branches, draft PRs, checks and outcomes. Record
shared-account GitHub API usage/rate-limit headers and provider throttling/token
refresh failures. Correlate API failures with lease/retry behavior. Shared account
authentication is required for cooperation and shares its security/rate budget.

## Experiment 4: two contenders for one issue

```sh
python3 "$SPIKE" start-two --image "$IMAGE" --names ub-spike10-contend-a ub-spike10-contend-b --issues "$C" "$C" --credentials "$SPIKE_CREDS" --git-name "$SPIKE_GIT_NAME" --git-email "$SPIKE_GIT_EMAIL"
gh issue view "$C" --repo "$GH_REPO" --comments
python3 "$SPIKE" collect --name ub-spike10-contend-a
python3 "$SPIKE" collect --name ub-spike10-contend-b
```

The start barrier releases both launchers after preflight; network timing still
does not guarantee simultaneous tentative claims. Record comment IDs and states,
winner/withdrawal, actual Codex starts from both process logs, number of branches/
drafts, duplicate work, and exit/outcome evidence. If one sees the other's live
lease before claiming, record exclusion rather than calling it an election test.
Repeat with a new test issue and new names if no overlapping claims occurred.
Docker does not make the GitHub election atomic or supply write fencing.

To test contender recovery, give a fresh C the R waiting instruction, interrupt
the observed winner with `stop` or `crash`, and follow experiment 2 using C and its
recorded container name. The losing `--once` worker may already have exited;
restart it with fresh auth after eligibility and record whether it continues the
winner's draft. Independent volumes mean only pushed commits are shared; local
uncommitted changes remain recoverable only from the interrupted worker's volume.

## Evidence, findings and cleanup

For **each** result retain the exact expanded commands, UTC start/stop times,
host OS/architecture, Docker version/context, image ID/RepoDigests, framework
commit from the image revision label, default-branch base SHA from the worker's
PR/log and final candidate SHA. `startups.jsonl` records the control SHA before
refresh; it can differ from the worktree base after refresh. Treat these as
different fields. Capture all failures too; do not assign results to a later SHA.

`collect` works with running or stopped containers and prints an ignored evidence
directory. Live copies may be inconsistent; collect after stop as well. It retains
Docker startup logs/exit state, settings, `/work` (clone, branches, runtime logs,
doctor and metadata), and Codex session history. Authentication tmpfs is excluded
from exports. Runtime logs/sessions can contain sensitive data: owner reviews the
raw artifacts before publishing a sanitized report. Never commit the evidence
directory, GitHub token, provider auth, a raw clone or model reasoning transcripts.

Use `findings-template.md` as a structure, without filling unrun results. Commit
the sanitized report as `spikes/docker-runner/FINDINGS.md` on this same spike
branch and explicitly push to its branch. Keep the PR draft, with no normal
workflow labels. Do not merge, mark ready or close #10. After review the owner
decides whether a supported runner is warranted.

Evaluate a supported profile's possible contents against the observations:
versioned image/toolchain, per-worker volumes, runtime secret injection, queue
scoping, stop/recovery observability and retention policy, network controls and
resource limits. Record remaining credential reach, trusted-comment authority,
cooperative election, uncommitted-work loss, detached helpers, host services and
container/kernel vulnerabilities. Do not present the setup flags as proven escape
resistance. Docker Desktop on macOS uses a Linux VM for these Linux containers;
this cannot run Xcode/native macOS build jobs or replace native macOS workers.

Only after evidence/report retention, the owner may remove each explicitly named
spike container and its `NAME-data` volume using `docker rm NAME` and
`docker volume rm NAME-data`. No script prunes containers, volumes or processes.
Owner cleanup of dedicated test issues/PRs/labels is manual and must not affect
the normal queue. The spike setup itself remains unmerged.

Reference sources: [Codex authentication and cache storage](https://developers.openai.com/codex/auth/),
[CLI login options](https://developers.openai.com/codex/cli/reference/),
[Docker container privileges and capabilities](https://docs.docker.com/engine/containers/run/),
[Docker Desktop VM boundary](https://docs.docker.com/desktop/features/vmm/),
and [this repository's coordination contract](../../docs/coordination.md).

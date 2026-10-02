# Issue #10 Docker feasibility findings

Status: **paused after experiment 1** (2026-10-02). The owner chose to stabilize the
normal loop before adding runner infrastructure. Experiments 2–4 were not run, and
nothing below makes claims about them.

## Environment

| Field | Value |
| --- | --- |
| Host | macOS 27.0.1, arm64 |
| Docker | 29.8.1 client and server, context `desktop-linux`, linux/arm64 |
| Image | `ub-agents-spike10:owner-test`, content ID `sha256:953a2ad6ad12d35a4328598255d8001669eb9141176f9cdf4b2453a05f1b36aa`, RepoDigests `[]` (local build, not published) |
| Image revision | `70639b02e918393ab3f846b0ef50b31e2fe04f9f` (this branch); ub-agent 0.1.3, codex-cli 0.159.3, gh 2.23.0 |
| Worktree base | `b1120aa5e21f47e3001343a93aa5d9471e386bae` (control SHA before refresh, the same SHA) |
| Result commit | `2f58f554bd587ddd12860c432f78665dee9c8500` (draft PR #51) |

An owner rebuild from a separate clone produced the same image ID the implementer
recorded, and `smoke.py` passed with networking disabled and no credentials.

| Experiment | Test issues / containers | Version evidence | Observation / artifacts | Result or failure |
| --- | --- | --- | --- | --- |
| One worker / isolation | #50, `ub-spike10-single` | As above | Started held 09:16:59Z, released 09:17:54Z, lease 09:18:04Z, Codex started 09:18:10Z, outcome 09:22:33Z, run released `success` 09:22:54Z, container exit 0 (not OOM) | **Passed**: draft PR #51 with only `spikes/docker-experiments/issue-50.txt`; `docker-spike-10-50-ready` replaced by `docker-spike-10-done`; 282 tests in-container; CI 8/8 green |
| Credential reach | #50 | As above | See [Credentials](#credentials) | Observed; broad, see below |
| Graceful stop / recovery | — | — | — | Not run |
| Hard crash / recovery | — | — | — | Not run |
| Two separate issues | — | — | — | Not run |
| Two contenders / recovery | — | — | — | Not run |

Commands, expanded (from a separate clone of this branch):

```sh
python3 spikes/docker-runner/spike.py build --image ub-agents-spike10:owner-test
python3 spikes/docker-runner/smoke.py --image ub-agents-spike10:owner-test
python3 spikes/docker-runner/spike.py start --image ub-agents-spike10:owner-test --name ub-spike10-single \
  --issue 50 --credentials ~/.config/ub-spike10/single-credentials.json \
  --git-name 'Ben Kubota' --git-email 'bk-one@users.noreply.github.com' --hold
# probes listed below, then:
python3 spikes/docker-runner/spike.py collect --name ub-spike10-single
python3 spikes/docker-runner/spike.py release --name ub-spike10-single
python3 spikes/docker-runner/spike.py collect --name ub-spike10-single   # after exit
```

## Isolation

Observed ordinary-access isolation, not escape resistance:

- **User and privileges:** UID/GID 1000. `CapInh`, `CapPrm`, `CapEff`, `CapBnd`
  and `CapAmb` are all zero. `NoNewPrivs: 1`, `Seccomp: 2`, not privileged, read-only
  root, `cap-drop ALL`, `no-new-privileges:true`, private IPC.
- **Limits:** 4 GiB memory, 2 CPUs, 256 PIDs.
- **Mounts:** only the Docker-managed volume `ub-spike10-single-data` at `/work`,
  plus tmpfs for `/run/spike-auth` (0700) and `/tmp`. Neither `/var/run/docker.sock`
  nor `/run/docker.sock` exists. The clone's toplevel is `/work/repo`, cloned from
  GitHub, not the host.
- **Processes:** the container PID namespace shows only `docker-init`, the bootstrap
  and its own children. No host processes are visible.
- **Host files:** `test -e` returns 1 for a fresh host marker outside the clone,
  for the operator checkout's `.git`, for `~/.codex/auth.json` and for the home
  directory. The marker's hash and the operator checkout were unchanged afterwards.
- **Network: not isolated.** Default bridge (`172.17.0.0/16`, gateway
  `172.17.0.1`), Docker Desktop DNS `192.168.65.7`, no egress allowlist.
  `https://api.github.com` answers 200.
  - An owner HTTP server bound to host **127.0.0.1** answered 200 through
    `host.docker.internal`, as did one bound to all interfaces. On Docker Desktop
    every loopback-only service on the Mac is reachable from the worker.
  - The owner's tailnet is reachable through the host: a `*.ts.net` service (the
    Uberblick hub) resolved to its `100.x` address and answered 200. The worker
    can reach whatever the Mac can reach on the tailnet.
- **Docker daemon over TCP:** not probed; no owner-approved endpoint exists.

The filesystem boundary holds for ordinary access. The network boundary does not
exist on Docker Desktop with the default bridge.

## Credentials

| Credential | Type | Supplied by | Readable by the worker at | Reach |
| --- | --- | --- | --- | --- |
| GitHub | Classic OAuth token (`gho_`), account bk-one, scopes `gist, read:org, repo, workflow, write:packages`, no expiry header | `gh auth token`, written to a 0600 host file outside any checkout, sent over `docker exec -i` stdin | tmpfs `/run/spike-auth`, the launcher's inherited `GH_TOKEN` | Admin on this repository and every repository the account can push to. It writes trusted coordination comments as the launcher account; the lease and outcome on #50 prove this, so it could also write false trusted records |
| Codex | ChatGPT login (Pro plan), a **separate session** created with `CODEX_HOME=<private dir> codex login` | the same stdin JSON as `CODEX_AUTH_JSON` | tmpfs home `.codex/auth.json` | The owner's ChatGPT plan usage for the configured model |

- **Codex on ChatGPT Pro has no long-lived non-interactive token.** Codex access
  tokens exist only for ChatGPT Business and Enterprise workspaces and must expire
  (admin-chosen, typically 7–90 days). The alternatives are an API key (API billing)
  or an `auth.json` cache, which Codex refreshes when it is about 8 days old.
  OpenAI advises against sharing one cache across machines or concurrent jobs,
  because a refresh rotates the token.
- **Copying the host's `~/.codex/auth.json` would risk the host loops.** A refresh
  inside a container could invalidate the host session. A separate login per
  worker avoids that. After the run the host login was unchanged (same session,
  same `last_refresh`).
- **Commits are unsigned.** Worker commits (`2f58f55`) are unsigned because no
  signing keys are imported. This repository does not require signatures; one that
  does would block workers.
- The credentials file was deleted from the host after the experiment. The worker's
  Codex session remains in the owner's private directory until the owner logs it out.

## Recovery and concurrency

Not exercised. Experiments 2–4 (stop/crash recovery, two workers, two contenders)
remain as described in `RUNBOOK.md`. One related observation from the normal loop
on the same day: Codex can exit 1 on `Selected model is at capacity` after
`ub-agent report` has succeeded. The launcher then releases the run as blocked
(see #42). A containerized worker would inherit this.

## Other observations

- **`/tmp` is mounted `noexec`.** Two tests failed and one errored until the agent
  re-ran the suite with `TMPDIR` set to a directory inside its worktree. It changed
  no code or tests. A supported image needs an executable temporary directory.
- **Cost of one worker:** about 5 minutes from release to exit for a trivial
  change, including clone, `pip install`, the full test suite and CI polling.

## Recommendation (preliminary)

Observed facts:
- One containerized loop worked end to end with the unmodified launcher: own
  clone and volume, runtime-injected credentials, the GitHub label contract and
  draft-PR checkpoints.
- Filesystem and process isolation from the host held for ordinary access.

Inferences, untested:
- Per-container clones and volumes would let several loops run without sharing the
  operator checkout or its worktrees. Coordination would still be GitHub-comment
  election, unchanged by Docker.
- Before a supported runner is worth it, it needs:
  - network controls: no host or tailnet route, plus an egress allowlist for GitHub,
    the model provider and any approved service such as the Uberblick hub;
  - scoped GitHub credentials (fine-grained, single repository);
  - one provider session per worker, or API keys;
  - an executable temp directory;
  - documented recovery behaviour, which experiments 2–4 would establish.

Limits:
- Docker Desktop runs Linux containers in a VM, so this cannot replace native
  macOS workers or Xcode builds.
- Containers do not make the GitHub election atomic.
- The shared account's trusted-comment authority remains.

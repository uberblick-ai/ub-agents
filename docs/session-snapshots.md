# Session snapshot contract

Local observation format for the [terminal view](terminal-view.md) and other
readers. These files never authorize claims, coordination or recovery.

Every `launch` session, including `--once` and `launch N [--agent NAME]`, also
publishes a local JSON snapshot at `.ub-agents/sessions/<session-id>.json` in the
control checkout. Publication is always on and has no configuration key.
Writing the snapshot makes no GitHub requests; continuous launch also runs
read-only observation passes during assignments to refresh the planned queue.
`status` and embedded loops without an observer keep their existing
behavior. Snapshots contain issue titles and run summaries already read by the
launcher, so treat them as private project data. The `.ub-agents/` and `sessions/`
directories have mode `0700`; snapshot and temporary files have mode `0600`.

## Publication and liveness

Each launcher has a separate random session ID. Files are replaced atomically and
hold the latest coalesced observations, with **format `version: 1`**, rather than
an event journal. A consumer can enumerate `sessions/*.json` without GitHub access.
The worker updates `published_at` every five seconds during idle waits and running
assignments. A session is stale if its launcher PID is gone on the recorded host,
or publication has stopped for more than 30 seconds. An idle session instead has
a fresh heartbeat and `activity.state: waiting`, with the wait's `until` time.
A clean exit publishes `ended: true` and `stopping`. If writing fails or hangs,
the last snapshot may remain stale; execution and shutdown do not wait for the
writer. The isolated helper exits within one second of launcher exit if a write
hangs. Successful publications prune old ended or stale files, retaining at most
20 other inactive sessions, including abandoned temporary files. Live sessions
are retained. Consumers should check liveness as well as timestamps because PID
reuse is possible.

Every launch's background startup sweep also prunes ended or stale snapshots whose file
modification time is more than a fixed 7 days old, together with their
`<session-id>.cleanup.log` files. Leftover `.tmp` and `.cleanup.log` files older
than 7 days are pruned when no live snapshot protects them. Live sessions and
the current launcher's snapshot, temporary file and cleanup log are never
pruned. Unreadable snapshots preserve their companion files, and pruning never
follows symlinks. The 20-inactive-session cap remains in effect, so busy launchers
can remove inactive snapshots sooner. There is no retention configuration key.
Pruning reports one summary line with run-directory and session-file counts and
total space freed only in `.ub-agents/sessions/<session-id>.cleanup.log`;
companion logs and temporary files count individually. No pruning summary is
printed when nothing was removed; removal failures are logged and launch continues.

Every sweep creates a cleanup log and flushes each line, including recovery SHAs
before branch deletion. If publication provides no session ID, cleanup uses a
unique `*.cleanup.log` in the same directory, retained as an orphan. Cleanup is
silent in the terminal, including with `--no-ui`, and cannot remove anything
without a writable log. The view and polling start without waiting for it.

## Snapshot fields

The version 1 envelope contains:

| Field | Contents |
|---|---|
| `session`, `pid`, `host`, `actor`, `repository`, `config_path` | Launcher identity and configuration; `started_at` and `published_at` use UTC ISO 8601 times. |
| `activity` | `polling`, `waiting` (with `until` and a reason), `running assignment`, or `stopping`. |
| `queue_agent`, `queue_idle` | Optional filtered-queue context: the selected configured agent and its last complete idle explanation, naming trigger labels or counting waiting states. Unfiltered launches omit these fields. |
| `poll_now` | Optional poll control: `cooldown_until` and `rate_limit_until` are UTC ISO 8601 times or `null`; `waiting: true` confirms a poll waiter is installed, allowing immediate feedback on `r` during an assignment unless cooldown or rate limits apply. Missing or false `waiting` gives no such confirmation. `refreshing: true` means an `r`-forced read-only queue refresh is running during an assignment. Missing or false `refreshing` means no forced refresh; scheduled refreshes do not set it. |
| `assignment` | Current item, kind, title, agent, effective priority word, run, runtime, attempt, lease state and expiry, process state and reason, and this run's `process_log` and `context_path`. Recovery includes `recovered_run` and has no agent log or context. |
| `latest_pass` | Start time, `partial` or `complete`, and the plans reached, plus rows carried during a partial pass. Discovery removes closed or merged items and Eligible rows without that agent's trigger; open Needs attention rows remain until replanned or completion. A supervised run's completed outcome refreshes its item's rows for every evaluated agent before the next claim (only the selected agent in a filtered queue), without completing the pass or discovering the repository. Rows include item, kind, title, agent, effective `priority` word (or `null`), chosen runtime when available, consecutive `failures`, `max_attempts`, state, reason and observation time, without description text. `eligible_count` counts merged Eligible items before byte trimming, excluding the running agent; older snapshots may omit it. Another launcher's owner includes only actor, host and run, with no log paths. |
| `outcomes` | This session's recent reports and recovered outcomes: item, kind, title, agent, run, runtime, handoff when reported, supervisor result, report result, summary, time, acceptance, transition completion, and currently observed human blockers. Older snapshots may omit optional header context. |
| `limits`, `omitted`, `shortened` | Format limits and counts of dropped rows and shortened fields/characters. Individual rows also carry text shortening counts. Work's `omitted N` counts dropped plan rows; Eligible's heading uses `eligible_count` even when hidden plans were dropped. |

## State interpretation

Process states use `claiming`, `starting`, `running`, `exited`, `recovery` and
`unknown`. Only a process recorded by supervision is shown as running; a lease's
`running` state alone is insufficient. Report acceptance is `unaccepted`,
`rejected`, `accepted` (transition not yet complete), or `finalized` (accepted and
transition complete). `completed` can be true while `human_blocker` lists stop
labels such as `needs-human`. Finalized outcomes' blockers carry their last
observation time and are updated when a later pass reaches that target;
unfinalized reports have no applicable blocker yet. Missing values are `null` with
reasons where known. The Issue tab loads missing descriptions from GitHub when
opened; a view from the previous build can still read these snapshots and offers
`g` to load the description.

## Limits and isolation

Snapshots hold at most 100 latest-pass rows and 20 recent outcomes. Each text
value is limited to 2,048 characters and the entire UTF-8 JSON file to 64 KiB;
the size bound trims surplus history runs, older outcomes and plans outside Work's
visible sections before Running, Needs attention and the first ten Eligible items
in the view's claim order. Each retained item's newest run is kept. Long visible
display text is shortened further if necessary; only an envelope whose remaining
metadata cannot fit loses visible plans. Publication uses
a bounded mailbox and an isolated worker. A slow or failed writer cannot delay
claims, outcome acceptance, recovery, configuration reload, signal handling or
launcher exit; failures produce at most one publication diagnostic per session.
Snapshots are never read for claims, coordination or recovery.

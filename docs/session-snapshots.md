# Session snapshot contract

Local observation format for the [terminal view](https://agents.uberblick.ai/docs/guides/terminal-view.html) and other
readers. Snapshots never authorize claims, coordination or recovery.

The view's real-terminal tests are in `tests/test_view_terminal.py` and
`tests/test_view_unblock_terminal.py`.

Every `launch` session, including `--once` and `launch N`, publishes a JSON
snapshot at `.ub-agents/sessions/<session-id>.json` in the control checkout.
Publication is always on, has no setting and makes no GitHub requests. Snapshots
hold issue titles and run summaries, so treat them as private project data. The
`.ub-agents/` and `sessions/` directories have mode `0700`; snapshot and temporary
files have mode `0600`.

Complete item histories live beside the snapshot, under
`.ub-agents/sessions/<session-id>.histories/`. Each JSON file contains the item's
metadata and every known run, oldest first, including failed, rejected, blocked
and abandoned runs. The filename is the SHA-256 hash of the item's history key
(normally its number). Carried rows can have a separate key for their prior
observation. History directories have mode `0700`; files and temporary files have
mode `0600` and are replaced atomically before the snapshot is published. History
files remain available until the session snapshot is pruned; both publisher
retention and startup cleanup remove them with it.

## Publication and liveness

Each launcher has its own random session ID. The file is replaced atomically and
holds the latest coalesced state with **`version: 1`**, not an event journal.
Readers can list `sessions/*.json` without GitHub access.

The launcher updates `published_at` every five seconds, during idle waits and
running assignments alike. A session is stale when its launcher PID is gone on the
recorded host or publication has stopped for more than 30 seconds. Check both,
because PIDs can be reused. An idle session has a fresh heartbeat and
`activity.state: waiting` with an `until` time. A clean exit publishes `ended: true`
and `stopping`.

An isolated worker writes the files from a coalescing mailbox, so a slow or failed
writer never holds up coordination or supervision; it reports at most one
diagnostic per session, and the last snapshot may stay stale. A sender thread streams one complete observation
at a time and keeps only the newest pending observation. At process exit, the
sender gets a bounded grace period to flush the final observation. Each successful
write keeps at most 20 other inactive sessions and never removes live ones. The startup sweep prunes older files;
see [stale artifact cleanup](coordination.md#stale-artifact-cleanup).

## Snapshot fields

| Field | Contents |
|---|---|
| `version`, `base_version` | Format version `1`, and the launcher's ub-agents version, which the view shows and checks when it attaches. |
| `session`, `pid`, `host`, `actor`, `repository`, `config_path` | Launcher identity and configuration. `actor_reason` and `config_path_reason` explain a `null`. |
| `started_at`, `published_at`, `ended` | UTC ISO 8601 times, and `true` after a clean exit. |
| `activity` | `state` is `polling`, `waiting` (with `until` and `reason`), `running assignment` or `stopping`. |
| `queue_agent`, `queue_idle` | Only with `launch --agent`: the selected agent and its latest idle explanation. |
| `poll_now` | `cooldown_until` and `rate_limit_until` (UTC time or `null`); `waiting: true` when a poll waiter is ready for `r`; `refreshing: true` while an `r`-forced refresh runs during an assignment. |
| `runtime_pauses` | CLI name (`claude`, `codex`) to its usage-pause end time. Missing or empty means no pause. |
| `assignment` | Current item, kind, title, agent, priority word, run, runtime, attempt, `claimed_at`, lease state and expiry, process state and reason, `process_log` and `context_path`. Recovery adds `recovered_run` and has no log or context. |
| `latest_pass` | `started_at`, `state` (`partial` or `complete`), `rows` and `eligible_count`, the Eligible count before trimming. Each row is one plan: item, kind, title, agent, `priority` word or `null`, runtime, `failures`, `max_attempts`, state, reason, `paused_clis`, attention details, observation time and, for another launcher, an `owner` with actor, host and run. A partial pass keeps earlier rows until they are replanned. |
| `outcomes` | This session's recent reports and recovered outcomes: item, kind, title, agent, run, runtime, handoff, supervisor and report results, summary, time, acceptance, transition completion and `human_blocker`. |
| `histories`, `history_storage` | History key to item metadata: title, kind, closing issue, filing author and time. `history_storage: files` tells the view to read full histories from the companion directory. A carried row can point to its own copy with `history_key`. Runs are absent from the snapshot. |
| `action_needed` | Item number to the latest trusted **Action needed** comment after the item's last claim or reset: `id`, `text` (up to 2,048 characters), `author`, `created_at` and `omitted_characters`, or `{"omitted": true}` when trimmed for size. |
| `coordination_authors` | Lowercase login to `{trusted, reason}` for notice authors the launcher has checked. The view does not load notices from untrusted authors and shows the reason. |
| `update` | The update banner, `{text, released_at}`, or `null`. |
| `health_notice` | The latest project health-check message, or `null`. |
| `configuration_pause` | While claims are paused for invalid refreshed files: `error`, full checkout SHA `commit` and `next_step`. Missing or `null` means no configuration pause. |
| `limits`, `omitted`, `shortened` | Format limits, counts of dropped plans and outcomes, and counts of shortened fields and characters. Rows and histories carry their own `shortened` counts. |

## State interpretation

`paused_clis` lists the CLIs whose usage pause holds a plan. Readers show that cause
only while every named CLI is still paused in `runtime_pauses`, and ignore expired or
unreadable times.

Process states are `claiming`, `starting`, `running`, `exited`, `recovery` and
`unknown`. Only a process recorded by supervision shows as running; a lease's
`running` state alone is not enough. Report acceptance is `unaccepted`, `rejected`,
`accepted` (transition not yet complete) or `finalized` (accepted and transition
complete). An outcome can be `completed` while `human_blocker` lists stop labels
such as `needs-human`; a finalized outcome's blockers carry their last observation
time. Missing values are `null`, with reasons where known.

## Limits and trimming

A snapshot holds at most 100 plan rows, 20 recent outcomes and
128 notices and notice authors. Each text value is cut at 2,048 characters and the
whole UTF-8 file at 64 KiB. When the file is too large, the launcher trims in this
order until it fits:

1. Notices for items outside Needs attention.
2. Older outcomes.
3. Plans outside Running, Needs attention and the first ten Eligible items in claim
   order, with history metadata nothing else refers to.
4. Notices for Needs attention items.
5. Visible titles, reasons and summaries, shortened in steps.
6. Visible plans, starting with the last. The current assignment always stays.

History files are outside the snapshot's 64 KiB budget. Runs have no count or
size-based trimming; their text fields retain the 2,048-character limit.

Trimming changes only the published file. The launcher keeps its full state for the
next snapshot.

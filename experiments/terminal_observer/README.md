# Issue #97: launcher-local terminal experiment

Draft research artifact for human assessment. This experiment is not installed
by ub-agents and does not change production commands, flags, dependencies or docs.
[REPORT.md](REPORT.md) gives the decision and evidence boundaries. Run from this
checkout's root on Linux/macOS:

```sh
python3 -m venv .venv
.venv/bin/pip install -q -e .
.venv/bin/pip install -q -r experiments/terminal_observer/requirements-lock.txt
.venv/bin/python -m experiments.terminal_observer.demo
```

The default seeds six synthetic observations from the existing fixtures and grows
one fixture lease's `process.log`. It demonstrates layout and formatting; it does
not attach to the operator's launcher. Current work contains at most one item.
Other claims are **Observed ownership**, without execution or log-access claims.
The list is the last observations, with age; it need not cover the full queue.

- Arrows/Enter select an observed item. `1`, `2`, `3` switch Log/Issue/Runs.
- Page Up pauses follow and scrolls back; Page Down scrolls forward; `f` resumes
  follow at the end. `u` toggles a bounded raw projection.
- `r` reads the **local** snapshot. It performs no discovery or GitHub refresh.
- `o` explicitly opens missing/incomplete details. Already collected or cached
  details cause no request. Without the optional detail transport it shows an
  error. Selection and changing panes alone never fetch details.
- `q` or Ctrl-C closes the separate view. There are no workflow/stop controls.

Display bounds: 200 decoded records, 2,048 characters per record, 400 rendered
lines, 128 parsed records and 32 rendered records per 100ms tick, 32KiB per read,
128KiB per structured record. Head/tail shortening and record eviction are
visible. Above the record cap, labeled raw fragments replace structured display.
Backlog can accumulate in the raw file; follow means following the **consumed**
output. Paused scrollback may evict at the bounds. The displayed raw-file path
preserves every byte that the worker wrote; `u` is not the full raw file.

Replay the current production-format synthetic recordings:

```sh
.venv/bin/python -m experiments.terminal_observer.demo --log experiments/terminal_observer/evidence/claude-production/process.log
.venv/bin/python -m experiments.terminal_observer.demo --log experiments/terminal_observer/evidence/codex-production/process.log
```

Claude uses whole-message `stream-json --verbose`, without partial-message flags;
its fixture includes multiline tools/results, a 49,439-byte result, errors and
unknown records. Codex uses compact human text from `codex exec`, without `--json`.
Historical bytes are untimed. Live capture timestamps label **observer read time**,
not producer time. JSON events and runtime success prose cannot accept outcomes.

For recorded real output, `claude-sanitized.jsonl` and `codex-sanitized.jsonl` are
curated subsets of the already-used first-run probes. Copy either to a private
file named `process.log` and use `--log` as above. They are not complete recordings
of today's production invocations: both probes used different flags (see
[probes.json](evidence/probes.json)). No more live runtime probes are authorized;
`probes.py` is archival and must not be run again for this spike.

## Owned dummy launcher and separate view

Run these in two terminals in this checkout. Use a fresh directory name each time;
this harness will refuse an existing directory. Both commands remain foreground.
Wait for the harness to finish before ending the session.

```sh
# Terminal A: actual Loop.tick/claim/supervise/finalize/release, fixture GitHub,
# and an owned Python subprocess writing synthetic runtime output. No CLI probe.
.venv/bin/python -m experiments.terminal_observer.harness --directory .ub-agent/spike97-demo --runtime claude --seconds 30

# Terminal B: separate Textual process consuming only this launcher's projection.
.venv/bin/python -m experiments.terminal_observer.demo --snapshot .ub-agent/spike97-demo/snapshot.json
```

Use `--runtime codex` in a new directory for the human-text replay. This harness
stubs checkout refresh and simulates the dummy agent's explicit report. Everything
else delegates to the existing Loop with recording transport. It evaluates only
the first encountered executable plan, leaving 29 other triggered fixture plans
unevaluated. A successful report is briefly unaccepted until normal finalization;
a released accepted step stays in Local recent activity for the bounded session.
The raw log and final projection remain inside the fresh harness directory.

`TapLoop` wraps yielded plans and existing coordinator return values. It publishes
through a one-slot coalescing queue to an atomic local file. Disk/UI failures cannot
veto worker operations; teardown awaits the publisher. **This is harness integration,
not an installed integration with a real launcher or runtime.** No independent
status scan, log aggregation, prefetch, remote transport, or `launch.log` polling
is used. The old `Observer`/network audit remain only for prior evidence/regressions;
the demo no longer exposes `--config`.

A real local view would need to run as the loop user on the recorded launcher
host, normally `uberblick`, with that launcher's snapshot and `log_dir` visible.
Existing permissions are respected. No operator launchers, logs, worktrees,
permissions or credentials were accessed/changed for the new harness.

For a deliberately incomplete **own** snapshot, optional real description reads
can be enabled explicitly with `--detail-repository owner/repo`, which must match
the snapshot. `o` then makes one GET of that item's issue endpoint (also sufficient
for a PR description), timeout 20s, no pagination, retry or hydration of other
items. Success and failure are cached for the session; at most 20 missing-detail
opens are allowed, with bounded display text. Rate-limit responses stop subsequent
opens until their indicated reset; they do not pause the worker. Offline validation
uses a recording transport for this path; no live missing-detail read was made.

## Validation and simpler presentation

```sh
.venv/bin/python -m experiments.terminal_observer.local_validate
.venv/bin/python -m experiments.terminal_observer.validate
.venv/bin/python -m experiments.terminal_observer.regressions
.venv/bin/python -m unittest discover -v
.venv/bin/ub-agent check
git diff --check
```

The nine launcher-local checks write `evidence/local-validation.json` and
`local-*.svg`. They compare UI off/on transport traces and detect an injected
navigation-read defect. They also test detail loading/errors/caching, floods,
projection failure isolation and owned-worker drain/interrupt/closure. The nine
older checks and both bug mutations remain. Checks use no network or runtime auth;
every owned process, thread and task is awaited. CI runs the package suite only,
without Textual or these experiment checks.

The same adapters also have a standard-library-only pretty-printer, needing no
Textual install or ub-agents import:

```sh
python3 -m experiments.terminal_observer.pretty experiments/terminal_observer/evidence/claude-production/process.log
python3 -m experiments.terminal_observer.pretty experiments/terminal_observer/evidence/codex-production/process.log --follow
```

`ub-agent status`, the UTC `launch.log`, and raw `process.log` tailing remain useful
existing alternatives. Status performs its ordinary GitHub scan; the pretty-printer
and local log tailing add no requests. Previously measured quota and Homebrew
research is retained in [the earlier scope report](REPORT.previous-scope.md) and
its original evidence, without rerunning those audits.

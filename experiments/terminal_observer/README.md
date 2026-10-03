# Issue #97: terminal observer experiment

This is a draft research artifact, not a merge candidate. [REPORT.md](REPORT.md)
records the revised verdicts, effort and limits. Run from this checkout's root on
Linux/macOS; this code is not installed by the distributed package.

```sh
python3 -m venv .venv
.venv/bin/pip install -q -e .
.venv/bin/pip install -q -r experiments/terminal_observer/requirements-lock.txt
.venv/bin/python -m experiments.terminal_observer.demo
```

The default creates a temporary `process.log` and grows it with **synthetic**
production-style Claude whole messages/tool results and Codex human text. Six
fixture work items go through the existing planner, coordinator record parser
and `cli.status_rows`. A fixture lease's `host` and `log_dir` locate its file;
there is no hard-coded issue-number log selection. Nothing is written to GitHub.

- Arrows/Enter select work; Recent activity starts collapsed (empty for this
  fixture because the status API is not complete history).
- `1`, `2`, `3` select Log, Issue and Runs. #3 names a human decision; #5 waits
  for dependency #4. #6 keeps its current human blocker visible while Runs shows
  accepted success. #2 has no local log or stop control.
- Page Up pauses follow and scrolls back; Page Down scrolls forward. `f` toggles
  follow and returns to the end. `u` shows a bounded raw projection. The displayed
  file path contains full bytes, including shortened/evicted output.
- `r` manually refreshes. Selection, tabs, redraw and tailing use the snapshot.
  `w` simulates a quota wait. `q` or Ctrl-C closes only the view. There are no
  execution, approval, retry or merge controls.

Replay the **synthetic current production formats**, including a >16KiB Claude
tool result, without runtime access. Historical bytes stay untimed:

```sh
.venv/bin/python -m experiments.terminal_observer.demo --log experiments/terminal_observer/evidence/claude-production/process.log
.venv/bin/python -m experiments.terminal_observer.demo --log experiments/terminal_observer/evidence/codex-production/process.log
.venv/bin/python -m experiments.terminal_observer.validate
.venv/bin/python -m experiments.terminal_observer.regressions
```

Offline validation needs no runtime credentials or network. It writes JSON/SVG
under `evidence/`, exercises the real GitHub transport with a recording runner,
and verifies that a navigation-refresh mutation is detected. It also drives a
separate observer subprocess in its own PTY next to an owned dummy Python worker;
every process is awaited and cleaned up. Standard repository checks remain those
in AGENTS.md; ordinary CI does not run these optional experiment checks.

The same adapters have a **standard-library-only** presentation. It requires no
Textual, package install or runtime credentials (run these from the repo root):

```sh
python3 -m experiments.terminal_observer.pretty experiments/terminal_observer/evidence/claude-production/process.log
python3 -m experiments.terminal_observer.pretty experiments/terminal_observer/evidence/codex-production/process.log --follow
```

This pretty-printer deliberately uses the same bounded projection; skipped records
are announced and full bytes remain in the file. Existing installed
`ub-agent status` / `status --json`, UTC `.ub-agent/launch.log`, and `tail -f` on a
selected run's `process.log` form the comparison option in the report.

Optional network modes are **read-only**, separately from offline validation:

```sh
.venv/bin/python -m experiments.terminal_observer.network_audit
.venv/bin/python -m experiments.terminal_observer.demo --config ub-agent.yaml
```

The audit performs four bounded passes and records real HTTP/ETag/request data;
it starts no runtime and reads no run log. The UI manually refreshes existing
status and has its own transport ETag cache and charged-REST cooldown, not shared
launcher accounting. Real issue-detail hydration and complete history are absent.
Live claim-to-log integration is **unverified**. For actual local logs the observer
must run on the recorded host, with those paths visible and permissions to read
them, normally as the loop user on `uberblick`. It never follows a remote lease's
path or exposes stop controls. Respect the assignment's prohibition on touching
operator workers; this revision validated log attachment using fixtures only.

`probes.json`, `codex-sanitized.jsonl` and `claude-sanitized.jsonl` are archival
first-run evidence. Those one-time probes requested extra format flags and used
unclaimed tasks; their curated subsets do not prove current production readability
or real run integration. `probes.py` preserves their commands and refuses direct
execution: **the live-probe allowance is used; do not rerun them for #97**.

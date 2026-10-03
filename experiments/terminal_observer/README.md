# Issue #97: terminal observer experiment

This branch is a research artifact, not a merge candidate. Nothing here is installed
by the distributed package. See [REPORT.md](REPORT.md) for the decision evidence,
budget and limitations. Run all commands from the repository root on Linux/macOS.

```sh
python3 -m venv .venv
.venv/bin/pip install -q -e .
.venv/bin/pip install -q -r experiments/terminal_observer/requirements-lock.txt
.venv/bin/python -m experiments.terminal_observer.demo
```

The default demo creates a temporary raw file, replays **synthetic** Claude/Codex
records into it, and tails it every 100ms. Its six work items and GitHub store are
**fixtures** evaluated through the real planner, coordination record parser and
`cli.status_rows`. Fixture setup writes only to the recording fake, never GitHub.
The real probe is attached to fixture row #1 solely as a visual test; that fixture
lease is not a claim for the probe or an operator worker.

- Use arrows and Enter in the left tree to select work. Recent activity starts
  collapsed; use Space or click its arrow to expand it.
- `1`, `2`, `3` select Log, Issue and Runs. Runs shows accepted coordination
  outcomes independently of runtime/tool text. #3 explains the human decision;
  #5 identifies dependency #4. #2 has no local log or stop control.
- Page Up pauses following and scrolls backward; Page Down scrolls forward.
  `f` toggles follow and returns to the end. `u` toggles a truncated raw projection.
  The displayed raw-file path holds full bytes; open it separately for complete
  output. The default temporary file disappears when the demo exits.
- `r` explicitly refreshes the snapshot. Navigation and tailing never refresh it.
  `w` simulates a 60-second quota wait, retaining the snapshot and live local log.
  `q` closes only the view. There are no execution controls.

Replay sanitized **real** probe output (historical records remain untimed):

```sh
.venv/bin/python -m experiments.terminal_observer.demo --log experiments/terminal_observer/evidence/codex-sanitized.jsonl
.venv/bin/python -m experiments.terminal_observer.demo --log experiments/terminal_observer/evidence/claude-sanitized.jsonl
.venv/bin/python -m experiments.terminal_observer.validate
```

Validation needs no runtime credentials or network. It writes JSON evidence and SVG
screenshots to `evidence/`. It exercises fake transport costs, both parsers,
bounded buffers, keyboard selection/tabs while output grows, scrolling, resizing,
quota waiting and an owned test worker that outlives view closure. Open the SVGs
in a browser. Standard checks remain those in the repository AGENTS.md.

The optional `--config PATH` experiment calls the existing read-only status API in
a worker thread and counts gh calls per explicit refresh. It applies the existing
idle REST budget as a cooldown; it does not change the launcher. This path has
**not** been network-validated. Live issue details, comprehensive history and
selected-run log resolution are unfinished: log attachment is limited to an
explicit file for fixture row #1. Do not mistake this mode for production support.

`probes.py` documents the one live task per runtime already performed. **Do not
rerun it as part of validation for #97**: the issue limits probe count and effort.
For a separately authorized experiment, it runs the same production `supervise`
function with a five-minute cap, existing auth, a read-only prompt and a local
READ_ME.txt. Raw files and live screenshots remain under this checkout's ignored
`.ub-agent/spike97-probes/`; published JSONL is sanitized. It never observes or
signals the operator launcher, its worktrees or workers. Each worker is awaited
and its owned process group is checked empty before return.

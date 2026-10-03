# #111: complete runtime log comparison

Research evidence for owner assessment. [REPORT.md](REPORT.md) contains the
findings and **hold** recommendation. Production code, invocation flags,
dependencies, README, public docs and changelog are unchanged.

`../terminal_observer/` reuses commit
`f9bdba7796e80c977e398dedc139d0717ba55db8`, including its original report and
archival evidence. Its original effort/verdicts belong to #97/#99, not this run.
We reuse its `logs.py`, `pretty.py`, `local.py`, `demo.py` and fixture projection
without fixes. All source/report bytes are unchanged; archival SVGs only remove
trailing whitespace to satisfy the repository check. No GitHub-cost, architecture
or platform audit was repeated.
Do not run its archival `probes.py`.

Run from the repository root with Python 3.11+:

```sh
python3 -m venv .venv
.venv/bin/pip install -q -e .
.venv/bin/pip install -q -r experiments/terminal_observer/requirements-lock.txt
.venv/bin/python -m experiments.runtime_logs_111.audit
.venv/bin/python -m experiments.runtime_logs_111.compare
.venv/bin/python -m unittest discover -v
.venv/bin/ub-agent check
git diff --check
```

`audit` verifies the pinned sources (SVG whitespace normalized), recording hashes, complete successful owned
runs, ordered arrivals, fixture coverage and common publication-sensitive
patterns. This pattern scan accompanies manual inspection; it is not a general
secret detector. `compare` replays the committed sanitized inputs offline. It
checks pretty.py's actual owned PTY output against the entire sequential adapter
projection at 110×32 and 72×24, drains each view, captures headless screenshots,
compares paused rows and returns to follow. It awaits all owned processes and
closes each pilot. Neither command starts an LLM or contacts GitHub.

The comparison writes `evidence/comparison.json`, SVGs and text transcripts.
SVG/text exports strip end-of-line whitespace for publication; PTY assertions and
paused-row measurements use the exact in-memory output. Input logs are not
whitespace-normalized. Replay timestamps, temporary paths and latency can vary. Input hashes
and content findings should remain reproducible. SVGs labeled `combined` are
Textual headless captures. SVGs labeled `pretty` model the final screen of actual
owned PTY output with plain character autowrap; they are not terminal-emulator
screenshots. No image is an actual operator-terminal observation.

Inspect the same inputs manually, using only the existing experiment:

```sh
# Substitute codex for claude to compare the human-text recording.
python3 -m experiments.terminal_observer.pretty experiments/runtime_logs_111/evidence/claude/process.log
.venv/bin/python -m experiments.terminal_observer.demo --log experiments/runtime_logs_111/evidence/claude/process.log
```

Resize to each tested size; compare Page Up, `f`, `2` then `1`, and `u`.
The formatter has no pause key; terminal scrollback is external to it.
`--follow` tails newly appended bytes. Adapters shorten to 2,048 characters per
record; original sanitized bytes stay in the committed `process.log`.

Each runtime directory holds the complete sanitized recording, `capture.json`
(version, exact argv, completeness, timeout, cleanup, hashes and limitations), and
`arrivals.json` (sanitized byte ends and line-completion observer read times).
`coverage.json` proves messages, tools/commands, exit-1 failure, 240 output rows
and the 6,000-character payload are present. Real process/auth/transport failure
is unverified; the failure exercised is an intentional missing-file read.

Capture provenance is reproducible from `capture.py`, `fixture_output.py`, the
prompt constant and exact argv in the metadata. For this assignment we used
**one** new probe per runtime, bounded by the existing launcher's 120-second
supervisor. The prompt and guidance prohibit work outside the owned fixture.
Claude gets only Read/Bash and two explicit read-only commands in plan mode;
Codex gets a read-only sandbox. Hooks/MCP/user settings are excluded, existing
authentication is inherited, and stdout/stderr are merged to a file exactly as
the launcher normally does. Safety arguments differ from operator worker args;
output flags and configured runtime/model/effort come from `command_for`.

No suitable complete pre-existing recording was present in this worktree or the
pinned evidence (those real recordings are curated subsets with different flags).
Other runs' logs, worktrees, authentication stores and operator workers were not
inspected. The only private files belong to this attempt under ignored
`.ub-agent/spike111-private`; no raw private output is committed. Sanitization
keeps all records and lines, replacing paths, IDs, email/credential-like values,
socket paths, opaque signatures and account quota metadata; JSON is reserialized. The committed stream
therefore preserves run completeness and record shapes, not exact private bytes.

`capture --run-once claude` and `capture --run-once codex` are the original capture
entry points, **not validation commands**. Both attempts are already spent for
#111; do not rerun them or reset their directory guards. `sanitize` only rebuilds
sanitized evidence from this attempt's ignored private files and needs those
files; owner replay uses the public inputs and does not need it.

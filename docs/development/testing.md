# Testing

## Changed behavior

Change tests with the code. Use `unittest` with the recording fakes in
`tests/support.py`. Test the observable behavior through the closest useful
boundary, including a failure case when the change introduces one. Keep GitHub,
model calls and release publication behind fakes.

Aim for about one short test per changed behavior. Combine closely related
cases when that keeps the test easy to read. Add another test when it proves a
distinct outcome, rather than mirroring each implementation branch.

Good: a small test that supplies invalid configuration and checks the reported
error. Bad: separate tests for every helper call used to build that same error.

The monitor page behavior test runs its shipped JavaScript with Node.js and fake
browser APIs. It checks read failures, partial results and recovery through the
rendered page. Install Node.js to run it locally. The suite skips it when
`node --version` cannot start, exits unsuccessfully or takes more than five
seconds, including when a version-manager shim is present but cannot run Node.js.
GitHub's hosted runners include Node.js and run the test.

Run the [required checks](../../AGENTS.md#checks) before handoff; their scope and
CI equivalents are in [Development checks](checks.md).

## Suite runtime

Keep the full suite near a minute on a launcher. When you add or expand an
integration test, run `.venv/bin/python -m tests --durations 40` before and after
on the same machine with the same `-j` value. Record wall time, test count, OS,
available cores and worker count in the PR. Investigate new slow tests and
increases in full-run time before handoff; a timing assertion in a test is not
a substitute for this measurement.

Wait for the state or rendered frame a test needs. For interaction-heavy Textual
checks, `tests.view_pilot.RefreshPilot` waits for queued messages and a refresh
instead of inferring readiness from CPU idleness. Keep explicit delays when
elapsed time is the behavior under test. Split independent layout or theme
cases into separately discoverable tests when one long test holds up the workers.
Keep transition and resize sequences together when their order is the behavior.

The runner starts measured slow tests first, using the rough seconds in
`tests/timings.py`. Update those estimates from `--durations 40` when a slow test
is split or substantially changed. Unknown tests still run. Shards select their
members in sorted name order before scheduling, so every test runs once across
the shards regardless of its estimate.

Do not skip cases, weaken assertions or raise timeouts to improve these numbers.
If you merge or delete a test, name the test that still checks each behavior in
the PR and account for the change in test count.

## Process supervision and sandboxes

The process-supervision tests in `tests/test_execution.py` call `ps`. A sandbox
that blocks `ps` makes them fail with `Operation not permitted: 'ps'`. That failure
is environmental: state it with your results and do not change code or tests to
avoid it.

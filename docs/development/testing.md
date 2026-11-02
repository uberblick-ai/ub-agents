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

Run the [required checks](../../AGENTS.md#checks) before handoff; their scope and
CI equivalents are in [Development checks](checks.md).

## Process supervision and sandboxes

The process-supervision tests in `tests/test_execution.py` call `ps`. A sandbox
that blocks `ps` makes them fail with `Operation not permitted: 'ps'`. That failure
is environmental: state it with your results and do not change code or tests to
avoid it.

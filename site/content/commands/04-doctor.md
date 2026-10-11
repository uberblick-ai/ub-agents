# ub-agents doctor

Run `ub-agents doctor` during setup or when a launch fails. It checks your machine, the configuration, GitHub access, labels and agent CLIs, and shows a fix for every warning.

## Results and label setup

Default output groups warnings, failures and remedies by machine, configuration,
GitHub and runtimes, with passed/skipped summaries. `--verbose` lists every check.
Required failures exit 1; warnings and skips alone exit 0. Missing configuration
is a diagnostic failure. Runtime alternatives need at least one usable option;
missing unattended permission arguments produce a warning.

Missing trigger/outcome labels are required failures; stop-only labels are warnings.
Labels match case-insensitively and count once. Interactive doctor offers to create
missing labels (default no), then rereads them and updates its final result.
Declining, EOF, CI, noninteractive runs and `--json` create nothing. Other checks
are diagnostic only. It warns about elevated, unreadable or unlisted launcher
accounts; see [launcher accounts](/docs/configuration/launchers.html).

`--json` emits version 2 with `ok` and `checks`; `--verbose` does not alter it.
Checks contain `id`, `status`, `required`, `agent`, `runtime`, `message`, `remedy`.
Label checks use `github-label:NAME`, null agent/runtime and `uses` entries with
`agent`, `meaning`, `required`; any required use makes the label required.

```text
{{help doctor}}
```

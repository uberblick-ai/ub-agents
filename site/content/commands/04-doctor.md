# ub-agents doctor

Run `ub-agents doctor` when a launch fails or you're unsure your setup is complete. It checks your machine, the configuration, GitHub access, labels and agent CLIs, and shows a fix for every problem.

```sh
ub-agents doctor             # all checks
ub-agents doctor --offline   # only what's on this machine
ub-agents doctor --json      # the same results, machine readable
```

## Results

Doctor groups problems by machine, configuration, GitHub and agent CLIs, each with a fix. `--verbose` lists every check. It exits with 1 when a required check fails and 0 otherwise, so warnings alone don't fail it.

Doctor also shows the launcher running on this control checkout, with pid, host
and start time, or says none is running. This is information, with no warning or
failure for a live launcher, and is included with `--offline` too. A second
[`launch`](/docs/commands/launch.html) on this checkout refuses to start; stop the
current launcher first, or use [`retry`](/docs/commands/retry.html) to hand it an item.

In `--json`, the top-level `launcher` is an object with `pid`, `host` and
`started_at` (UTC), or `null` when none is running. Only the held lock counts;
leftover owner records do not show a running launcher.

When workflow labels are missing, doctor offers to create them, except with `--json`, `--offline` or outside a terminal. A missing stop label or [`outside-input`](/docs/configuration/approvals.html#outside-input-label) label is only a warning.

Doctor also warns about starter placeholders left in your shared policy, such as `@org/maintainers` or `<project test command>`, and a `## Merging` section still identical to the starter.

## Offline

`--offline` skips GitHub and the agent CLIs and checks the configuration, instruction files and repository. Use it after editing `ub-agents.yaml`, or in CI without GitHub credentials.

```text
{{help doctor}}
```

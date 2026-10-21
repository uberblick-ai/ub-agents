# ub-agents doctor

Run `ub-agents doctor` when a launch fails or you're unsure your setup is complete. It checks your machine, the configuration, GitHub access, labels and agent CLIs, and shows a fix for every problem.

```sh
ub-agents doctor             # all checks
ub-agents doctor --offline   # only what's on this machine
ub-agents doctor --json      # the same results, machine readable
```

## Results

Doctor groups problems by machine, configuration, GitHub and agent CLIs, each with a fix. `--verbose` lists every check. It exits with 1 when a required check fails and 0 otherwise, so warnings alone don't fail it.

When workflow labels are missing, doctor offers to create them, except with `--json`, `--offline` or outside a terminal. A missing stop label or [`outside-input`](/docs/configuration/approvals.html#outside-input-label) label is only a warning.

Doctor also warns about starter placeholders left in your shared policy, such as `@org/maintainers` or `<project test command>`, and a `## Merging` section still identical to the starter.

## Offline

`--offline` skips GitHub and the agent CLIs and checks the configuration, instruction files and repository. Use it after editing `ub-agents.yaml`, or in CI without GitHub credentials.

```text
{{help doctor}}
```

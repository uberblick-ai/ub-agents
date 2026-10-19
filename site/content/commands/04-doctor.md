# ub-agents doctor

Run `ub-agents doctor` when a launch fails or you're unsure your setup is complete. It checks your machine, the configuration, GitHub access, labels and agent CLIs, and shows a fix for every problem.

```sh
ub-agents doctor             # all checks
ub-agents doctor --offline   # only what's on this machine
ub-agents doctor --json      # the same results, machine readable
```

## Results

Doctor groups problems by machine, configuration, GitHub and agent CLIs, each with a fix. `--verbose` lists every check. It exits with 1 when a required check fails and 0 otherwise, so warnings alone don't fail it.

When workflow labels are missing, doctor offers to create them. With `--json`, `--offline` or outside a terminal it creates nothing.
With [approvals on](/docs/configuration/approvals.html#outside-input-label), a
missing `outside-input` label produces a warning, like a missing stop label;
neither prevents launch.

When `shared-instructions` is configured, doctor warns once for each remaining starter placeholder: `@org/maintainers`, `<project build command>`, `<project test command>` and `<project lint or validation command>`. It also warns when the file's `## Merging` section still matches the starter word for word, ignoring line wrapping and any owner handle init substituted. Both checks run offline too and never make doctor fail.

Fresh [`init`](/docs/commands/init.html) selects a tailoring issue by default to draft repository-specific rules from file evidence. It files the issue labeled `ready` after opening the setup PR and prints its URL in **Next**. Without a setup PR, in unattended setup or after a failed filing, init prints the equivalent `gh issue create` command; a failure also shows its reason. Deselecting the extra skips the issue and command.

## Offline

`--offline` skips GitHub and the agent CLIs and checks the rest: configuration, instruction files and the checkout. Use it after editing `ub-agents.yaml`, or in CI without GitHub credentials.

```text
{{help doctor}}
```

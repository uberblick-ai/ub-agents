# Treat instructions as code

[Role files](/docs/configuration/instructions.html#role-files) live in your repository. Change them in pull requests and review them like any other code.

## Short and specific

Say what the role does, which checks it runs, what a finished handoff looks like and when to stop. Leave out what the agent already knows.

## Checks in one place

List the loop's checks in [`.agents/ub_agents.md`](/docs/configuration/instructions.html#shared-policy), so every role runs the same ones. Keep build commands in the guidance your agent CLI already loads, such as `AGENTS.md`.

## Say when to stop

Tell each role which decisions belong to a person. It [reports blocked](/docs/configuration/outcomes.html#stop-reports) with one `--action` per decision instead of guessing.

```sh
ub-agents report --status blocked --summary "Scope unclear" \
  --action "Owner: keep the v1 API or drop it; recommend keep."
```

## Fix the instructions, not the item

When the same mistake repeats, change the role file. The next run reads it.

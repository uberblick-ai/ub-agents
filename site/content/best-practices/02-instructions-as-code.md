# Treat instructions as code

[Role files](/docs/configuration/instructions.html#role-files) live in your repository. Change them in pull requests and review them like any other code.

## Short and specific

Say what the role does, which checks it runs, what a finished handoff looks like and when to stop. Leave out what the agent already knows.

## What goes in the shared policy

[`.agents/ub_agents.md`](/docs/configuration/instructions.html#shared-policy) is the loop policy every role reads. Keep it to what the loop itself needs:

- which checks must pass before work is handed on
- when a pull request may merge, and which changes wait for a maintainer
- who answers scope and policy questions
- what reviewers look at first

Leave out:

- build commands, coding style and project conventions; they belong in `AGENTS.md` or `CLAUDE.md`, which your agents already read, so point there instead of copying
- anything only one role needs; put it in that role's file
- how to report, which input to trust and who sets workflow labels; the launcher adds those rules itself

## Say when to stop

Tell each role which decisions belong to a person. It [reports blocked](/docs/configuration/outcomes.html#stop-reports) with one `--action` per decision instead of guessing.

```sh
ub-agents report --status blocked --summary "Scope unclear" \
  --action "Owner: keep the v1 API or drop it; recommend keep."
```

## Fix the instructions, not the item

When the same mistake repeats, change the role file. The next run reads it.

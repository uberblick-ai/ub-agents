# Instructions

Each agent's job is a Markdown file in your repository. `init` writes starter files for five roles; rewrite them in your own words.

```text
your-project/
├── ub-agents.yaml
└── .agents/
    ├── ub_agents.md
    ├── issue-preparer.md
    ├── issue-reviewer.md
    ├── implementer.md
    ├── reviewer.md
    └── integrator.md
```

## Role files

Point each agent at its file. Say what the role does, which checks to run, what a good handoff looks like and [when to stop for a person](/docs/configuration/stop-labels.html#say-when-a-person-decides).

```yaml
instructions: .agents/implementer.md
```

[How the loop works](/docs/how-the-loop-works.html#issues-from-idea-to-ready) describes what the starter roles report. For issue review, pick a different runtime yourself: `different-runtime-from` only works for pull request agents.

## Shared policy

`shared-instructions` names a file every role reads before its own: checks, merge policy and who decides. Build commands and conventions stay where your agent CLI already loads them, such as `AGENTS.md` or `CLAUDE.md`.

```yaml
shared-instructions: .agents/ub_agents.md
```

## What the agent receives

The launcher's own run rules, then the shared policy, then the role file, with the assignment context: the item's title, body, trusted comments and the command to report with. The run rules already cover how to report, which input to trust and who owns workflow labels, so your files need no copies.

## Always current

Before each run, the launcher pulls your default branch and rereads `ub-agents.yaml` and the instruction files, so the next run uses whatever you merged. Keep the launcher's checkout clean.

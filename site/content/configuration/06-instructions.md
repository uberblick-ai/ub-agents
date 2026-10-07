# Instructions

Each agent's job is a Markdown file in your repository. `init` writes starter files for four roles; rewrite them in your own words. The launcher puts its own short run rules in front, such as how to report and which input to trust, and adds the issue or pull request details.

```text
your-project/
├── ub-agents.yaml
└── .agents/
    ├── ub_agents.md
    ├── issue-preparer.md
    ├── implementer.md
    ├── reviewer.md
    └── integrator.md
```

## Role files

Point each agent at its file. Say what the role does, which checks to run, what a good handoff looks like and when to stop for a person.

```yaml
instructions: .agents/implementer.md
```

## Shared policy

`shared-instructions` names a file every role reads before its own: checks, merge policy and who decides. `init` writes `.agents/ub_agents.md` for it. Build commands and conventions stay in the guidance your agent CLI already loads, such as `AGENTS.md` or `CLAUDE.md`.

```yaml
shared-instructions: .agents/ub_agents.md
```

## What the agent receives

The launcher's own rules first, then the shared policy, then the role file, on stdin, with the assignment context. The context has the issue or pull request title, body and trusted comments, plus the command to report with. How to report, which input to trust and who owns workflow labels come from the launcher, so your files need no copies of them.

## Always current

Before each new run, the launcher pulls your default branch and rereads `ub-agents.yaml` and the instruction files. Merge a change and the next run uses it. Keep the launcher's checkout clean.

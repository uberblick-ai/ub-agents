# ub-agents init

Run `ub-agents init` in your project's checkout to write a starter `ub-agents.yaml`, the loop policy `.agents/ub_agents.md` and four role files in `.agents/`. It leaves your `AGENTS.md` or `CLAUDE.md` alone. It never overwrites existing starter files. In a terminal it offers to create missing labels and to turn on starter permissions.

Starter files go beside the selected configuration. Init reports which existing
project guidance the runtime loads and warns if none is found. The shared policy
references existing checks, otherwise supplies placeholders; customize it before
launching. All starter roles use private worktrees.

Label creation defaults to no. Declining prints commands for missing labels; CI,
noninteractive runs or failed label reads print commands for all configured labels.
Existing labels are unchanged. Noninteractive setup makes no GitHub calls except
repository inference when needed. Local starter files remain regardless of the
label answer. A separate permissions prompt defaults to no; declining, EOF and
noninteractive setup leave permission arguments commented out.

```text
{{help init}}
```

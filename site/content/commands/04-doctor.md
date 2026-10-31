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

## MCP servers

Online doctor includes MCP checks in the runtimes area, scoped to each agent,
runtime alternative and server. It finds server names in `mcp__<server>__...`
tool references in runtime arguments, role instructions and shared instructions,
and in the project's `.mcp.json` and `.codex/config.toml`. Command agents are
excluded; ordinary prose mentioning a server does not count.

Doctor evaluates configuration for a fresh agent worktree, without reusing the
control checkout's trust or approval choices. A server is **loaded**, **missing**,
**pending approval** (Claude's `.mcp.json` needs approval), or **unread** (defined
only in configuration that the runtime won't read at the new path). In particular,
a project `.codex/config.toml` is unread; define Codex servers through `runtime-args`
with `-c mcp_servers.<name>.command=...` and the other server settings instead.
For Claude, commit `.mcp.json` and approve the server with `enabledMcpjsonServers`
in `.claude/settings.json`.

For each loaded stdio configuration, doctor starts the server, initializes MCP and
lists its tools, reporting the tool count. Identical command, arguments,
environment and working directory share one probe across agents and alternatives.
Probes use the project's directory unless the server specifies a working directory,
and have the same 20-second timeout as other doctor probes. Other transports and
remote stdio execution are skipped. Unapproved or unread servers are never started.

MCP problems are optional warnings and do not change doctor's exit code. Messages
and remedies contain no server output, environment values or credentials. In
`--json`, each `runtime-mcp` check includes `agent`, `runtime` and `server`;
errors in configuration a runtime reads appear as `runtime-mcp-config` warnings
for that alternative. Malformed files it ignores do not prevent valid servers
from being probed. To make the launcher wait for a required service, configure a
[health check](/docs/configuration/agents.html#health-check).

## Offline

`--offline` skips GitHub, the agent CLIs and MCP probes, reporting MCP checks as
`skip`. It checks the configuration, instruction files and repository. Use it
after editing `ub-agents.yaml`, or in CI without GitHub credentials.

```text
{{help doctor}}
```

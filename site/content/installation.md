# Installation

Install ub-agents with Homebrew, on macOS or Linux:

```sh
brew install uberblick-ai/tap/ub-agents
```

You also need `git`, `gh` logged in to an account with write access to the repository, and the agent CLI you want to run, `claude` or `codex`.

Then, inside your project's checkout, run `ub-agents init`.
It checks your machine, asks a few questions and writes after one confirmation.
Use `ub-agents init --use-defaults` for unattended setup, including in a terminal;
CI/non-TTY also uses defaults. See the [init command guide](/docs/commands/init.html)
for the questions and extras.

```sh
ub-agents init --runtime claude:claude-opus-5-5:high
```

Now edit the new file `ub-agents.yaml`. It could look as simple as this:

```yaml
repository: your-org/your-project
shared-instructions: .agents/ub_agents.md

agents:
  implementer:
    runtime: "claude:claude-opus-5-5:high"
    trigger: ready
    instructions: .agents/implementer.md
    worktree: true
    runtime-args: [--permission-mode, acceptEdits, --permission-prompts, none,
                   --allowedTools, "Bash(git *)", "Bash(gh *)", "Bash(npm test)",
                   "Bash({report_command} report *)", --add-dir, "{scratch}"]
    outcomes:
      handed-off: {add: [needs-review]}
```

Claude agents run only the commands in `--allowedTools`, so add your check commands there.

Fill in `.agents/ub_agents.md`, the policy every agent reads: your checks, who may merge and who answers questions. Build and test commands stay in the `AGENTS.md` or `CLAUDE.md` your agents already read.

```markdown
## Checks

Project checks are documented in `AGENTS.md`.

## Merging

The integrator merges only when every required review and check passes on the current head.
```

Setup success does not establish launch readiness. Review build/test commands,
merge authority and decision-makers; finding an existing instruction file is not
proof that the policy is ready. On the default branch, init opens a setup PR with
the files: review the configuration there and merge it. Off the default branch or
unattended, the files stay local; commit them through your project's PR workflow.
Then start from a clean default-branch launcher checkout, without local commits
ahead of origin:

```sh
ub-agents doctor --offline
ub-agents doctor
ub-agents launch
```

Use a separate launcher checkout if you also code here. Launch fetches and
fast-forwards origin's default branch before each run; agents use private worktrees
and PRs. Init offers to switch the checkout to your default branch and never resets, stashes or clones.

Then add the `ready` label to an issue. The launcher will:

1. Find the issue among open items whose labels match an agent's trigger, ranked by effective priority, then existing work before new issue starts.
2. Fetch your default branch and reread `ub-agents.yaml` and the instruction files.
3. Claim the issue with a comment on GitHub, so no other launcher takes it.
4. Create a private worktree on a fresh branch.
5. Run the agent CLI with the issue and the agent's instructions.
6. Renew the claim every 10 minutes while the agent works.
7. Check the outcome the agent reports, such as `handed-off` with its pull request.
8. Remove `ready` from the issue and add `needs-review` to the pull request.
9. Remove the worktree and release the claim.

In a public repository, a label added by someone with maintain or admin access starts work. See [Approvals](/docs/configuration/approvals.html).

That's it. Add a reviewer for `needs-review` and an integrator for `ready-to-merge`, or keep the four starter roles `init` wrote. For more machines, see [Add a launcher](/docs/guides/add-a-launcher.html).

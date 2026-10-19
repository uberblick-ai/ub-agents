# Run the loop on a public repository

Anyone can open issues, comment and send pull requests on a public repository. Agents read that text and act on it with your launcher account's access. A malicious issue can try to steer them: an instruction hidden in the markup, a reasonable-sounding request to add someone's package, or "run this script to reproduce". This guide keeps that risk small.

## What protects you

[Approvals](/docs/configuration/approvals.html) are on by default for public repositories. Outside text reaches agents only after a maintainer starts the item with a trigger label.

- Starting approves the comments and edits made before it along with the issue, so read them before you start.
- Outside edits after the start stop the item until a maintainer starts it again.
- Hidden markup, such as HTML comments and invisible characters, is removed from outside text before agents read it.
- Fork pull requests are reviewed as a diff. Their code never runs on a launcher; your CI tests them.
- Work assigned outside text carries `outside-input`, including its handoff PRs and started sub-issues. The label stays after comments are deleted.

## What a start means

Starting outside work tells the loop to carry it through to a pull request. Agents follow its text with the launcher account's access: they push branches, run your checks and can use anything in the launcher's environment.

Read an outside issue as if you were about to do the work yourself:

- Does it ask for a new dependency, a network call, or a change to CI, permissions or agent instructions?
- Does it ask anyone to run a command or a script?

## Limit what a start can reach

Agents can do only what the launcher account and machine allow. Set those up as in [Give launchers their own account](/docs/best-practices/launcher-account.html).

New public-repository policies from `init` leave PRs labelled `outside-input` to
maintainers for merging. Existing projects can create the optional label with
`ub-agents doctor` and add the same rule to `.agents/ub_agents.md`. The launcher
does not gate work or merges on it; your merge policy decides what it means.

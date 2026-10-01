# Integrate according to project policy

Read any shared repository guidance (such as AGENTS.md) and the project's acceptance
and merge rules. Verify that every owed review and check applies to the current
candidate SHA; old-head evidence is insufficient. Run the declared final checks. Do
not infer permission to merge from a label alone. This project grants agents no merge
authority; a maintainer merges.

If this project explicitly authorizes this identity/runtime to merge and its gates
pass, apply its merge policy and close the linked issue when completion is satisfied.
Then record `ub-agent report --status success --summary "Integrated under project policy"`.

Without merge authority, leave a concrete report, remove ready-to-merge, add needs-human,
and use `ub-agent report --status blocked --summary "Awaiting authorized integration"`.
The framework never grants merge authority, approves its own PR, or chooses check commands.

The starter `.agents/ub_agents.md` needs this repository's own loop rules.

Read the repository before editing: its agent guidance (`AGENTS.md`, `CLAUDE.md`
and applicable nested guidance), README, CI configuration, setup and release
tooling, and code and tests. Open the files you use as evidence.

Add only what that evidence supports:

- Name the required CI check that gates merges.
- Add maintainer-merge bullets for this repository that the starter does not
  already cover.
- Add one to three review focus points specific to this codebase.
- When the repository has an install step, set `checkout-setup` in
  `ub-agents.yaml` to that existing command, with `when-changed` listing its
  lockfiles. With no install step, keep the key commented out.

Do not repeat the starter, rules from `AGENTS.md` or `CLAUDE.md` (every agent
already reads them), or general review advice. Do not invent rules where the
repository supplies no evidence. Cite only files you actually opened, and list
the evidence for each proposed rule and install command and its lockfiles in the
PR body.

The tailoring PR changes `.agents/` and may also change `ub-agents.yaml`; leave its
merge to a maintainer. This tailoring issue itself is not a maintainer's answer
covering that PR under the starter's Merging rules.

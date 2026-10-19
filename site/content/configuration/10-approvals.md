# Approvals

Decides whose input reaches the agents. On by default for public repositories, off for private and internal ones.

Interactive [init](/docs/commands/init.html) shows the effective value and its source
on the "Who answers the agents' questions?" step. Without an explicit policy,
unreadable visibility leaves the value unresolved; run `ub-agents doctor` before launching.

```yaml
approvals: on
```

## Off

Any trigger label starts work. The current title and body are input. Comments and reviews count only from people with write access or higher.
Text is unchanged; the launcher reads no edit history and does no hidden-content cleanup.

```yaml
approvals: off
```

## On

A maintainer starts an issue by adding a trigger label. Re-applying a trigger label clears earlier edits and comments from outside the team for the agents.

Sub-issues can inherit their parent's start through an accepted [`sub-issues` outcome](/docs/configuration/outcomes.html). Later outside title or body edits suspend work; later outside comments wait for a maintainer to re-apply a trigger. See [inherited starts](https://github.com/uberblick-ai/ub-agents/blob/main/docs/approvals.md#start-suspension-and-comments) and [approval reads](https://github.com/uberblick-ai/ub-agents/blob/main/docs/approvals.md#approval-reads) for the detailed rules.

Before agents receive assignment context or `read` output, each HTML comment
(`<!-- ... -->`) and each consecutive run of Unicode format characters (category
`Cf`, such as zero-width characters, bidi controls and tag characters) becomes
`[hidden content removed]`. Comments are removed even inside code; an unterminated
comment runs to the end of the text. Other text is unchanged.

Cleanup applies to outside authors' titles and bodies, a title with any outside
rename or a body with any outside edit, and cleared outside comments, reviews and
review comments. Trusted authors' fields without outside edits and trusted or
listed-bot feedback stay unchanged byte for byte. Withheld fields and
`withheld_counts` are unchanged. Approval checks, parking, gates and gate keys
still use the raw GitHub text.

This cleanup does not guarantee that agents see only what a maintainer saw.
Link targets, collapsed sections and diffs can still carry unseen text.

## Outside-input label

With approvals on, runs that receive outside authors' titles or bodies, outside
title or body edits, or cleared outside comments, reviews or review comments get
`outside-input` before the agent starts. A failed label write prevents the run
and spends no attempt; the next poll retries. Trusted-bot feedback, withheld text
and reading other items with `read N` do not count.

A labelled assignment passes the label to its handoff PR and the sub-issues its
outcome starts, including during recovery. It survives comment deletion.
Maintainers may remove it; a later run receiving outside text adds it again.
Agents leave it alone. The launcher gates nothing on the label, and applying it
does not start work. With approvals off, it is not added, copied or listed and
causes no additional reads.

The label is optional: `init` offers to create it, `doctor` warns when it is
missing, and `launch` still works. On public repositories, new init policies leave
PRs labelled `outside-input` to maintainers for merging.

**Upgrading:** Run `ub-agents doctor` to create the label, and add this line to
your policy's "Leave the merge to" list in `.agents/ub_agents.md`. Init keeps
existing files.

```markdown
- is labelled `outside-input` (built from outside text);
```

## Outside pull requests

A pull request from outside the team needs a maintainer's trigger label and an approving review of its exact head commit. Accepted agent revisions from an eligible head inherit eligibility in the base repository. Agents can review fork pull requests but never push to them.

For what a maintainer's start allows and how to limit its reach, see [Run the loop on a public repository](/docs/guides/public-repos.html).

A PR counts as a fork when its head repository differs from the base repository
(case-insensitively), or GitHub reports no head repository, including deleted forks.
Its private worktree uses a detached checkout of the base branch tip from `origin`,
with the base's files, guidance and runtime settings. The head is fetched into
`refs/ub-agents/runs/RUN/head`, verified against the assigned SHA and removed at
cleanup. Agents review it only as a diff: they must never check out or execute
its code, install scripts or checks. `checkout-setup` is skipped; repository CI
on the exact head SHA supplies check evidence.

With `worktree: false`, the control checkout stays in place and the prompt gives
the same diff-only and CI instructions. Same-repository PR behavior is unchanged,
including agent revisions built from outside text.

## When something needs approval

The launcher adds the stop label and posts one **Action needed** comment with the steps. Follow them and remove the label.

## Trusted bots

Bot accounts whose comments and reviews count as feedback. They get no other authority.

```yaml
trusted-bots: [copilot-pull-request-reviewer, Copilot]
```

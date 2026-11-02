# Approvals

Decides whose input reaches the agents. On by default for public repositories, off for private and internal ones.

If the repository's visibility can't be read and the key is unset, run `ub-agents doctor` before launching.

```yaml
approvals: on
```

## Off

Any trigger label starts work. Agents read the current title and body, and comments and reviews only from people with write access or higher. Text reaches them unchanged.

```yaml
approvals: off
```

## On

A maintainer starts an issue by adding a trigger label. That start approves the outside comments, reviews and edits made before it, so read them first. Later outside title or body edits suspend work, and later outside comments wait until a maintainer applies a trigger again. Sub-issues can inherit their parent's start through a [`sub-issues` outcome](/docs/configuration/outcomes.html). The [approval rules](https://github.com/uberblick-ai/ub-agents/blob/main/docs/approvals.md#start-suspension-and-comments) have the details.

In outside text, HTML comments (`<!-- ... -->`) and invisible format characters, such as zero-width and bidi controls, become `[hidden content removed]` before agents read it. Trusted authors' text stays unchanged. This doesn't guarantee agents see only what a maintainer saw: link targets, collapsed sections and diffs can still carry unseen text.

## Outside-input label

With approvals on, a run that receives outside text gets `outside-input` before the agent starts. The label passes to its handoff PR and the sub-issues it starts, and stays after comments are deleted. A maintainer may remove it; the next run with outside text adds it again. The launcher gates nothing on it; your merge policy decides what it means. On public repositories, init's policy leaves those PRs to maintainers:

```markdown
- is labelled `outside-input` (built from outside text);
```

The label is optional: `init` offers to create it, and `doctor` warns when it's missing.

## Outside pull requests

A pull request from outside the team needs a maintainer's trigger label and an approving review of its exact head commit. Agent revisions on top of an approved head stay eligible.

Fork pull requests, including deleted forks, are reviewed only as a diff. The worktree is the base branch tip, the head is fetched to a run-owned ref, and agents never check out or run its code. `checkout-setup` is skipped, and your CI on the exact head supplies the check results. Agents never push to forks. See [Run the loop on a public repository](/docs/guides/public-repos.html).

## When something needs approval

The launcher adds the stop label and posts one **Action needed** comment with the steps. Follow them and remove the label.

## Trusted bots

Bot accounts whose comments and reviews count as feedback. They get no other authority.

```yaml
trusted-bots: [copilot-pull-request-reviewer, Copilot]
```

## Trusted PR authors

With approvals on, listed bots' same-repository PRs need no maintainer start or approving head review, even after new pushes. Any account can apply the trigger label. The launcher adds `outside-input` before execution.

```yaml
trusted-pr-authors: [dependabot]
```

The list defaults to empty. Matching ignores case and an optional `[bot]` suffix, and GitHub must report the account as `Bot`; listing a human adds no trust. Non-list values and duplicate logins ignoring case fail configuration validation and `doctor --offline`.

The PR body is always withheld from assignment context and `read N`, because it may contain release notes written by upstream publishers. Agents work from the diff. Titles get hidden content cleanup. The author's own title and body edits do not suspend work; other outside edits still need a maintainer's trigger label, and the body stays withheld afterward.

Outside comments and reviews need clearance to become input but do not suspend the PR. The author bot's feedback needs `trusted-bots` to count without clearance. Listing a PR author gives it no authority to start other work, approve heads, clear input or act as a launcher.

Fork PRs and issues keep their normal rules. With approvals off, the key has no effect and bodies remain input. Supervised runs pin the list for `read N`. See the [full approval rules](https://github.com/uberblick-ai/ub-agents/blob/main/docs/approvals.md#trusted-pr-authors).

## Key reference

| Key | Type and default | Rules |
|---|---|---|
| `approvals` | `on` or `off`, quoted or unquoted | When unset, resolved each discovery pass from visibility: `on` for public, `off` for private and internal. |
| `trusted-bots` | List, `[]` | GitHub logins, unique ignoring case, without `@` or surrounding whitespace. |
| `trusted-pr-authors` | List, `[]` | Same login validation as `trusted-bots`. |

Bot login matching ignores an optional `[bot]` suffix and requires GitHub's account
type to be `Bot`. Copilot posts inline feedback as `Copilot`; list both
`copilot-pull-request-reviewer` and `Copilot` to trust reviews and inline feedback.

The [approval contract](https://github.com/uberblick-ai/ub-agents/blob/main/docs/approvals.md) defines authority, clearance and pinned run policy.

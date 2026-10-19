# Public repositories

Anyone can comment on a public repository. Keep [approvals](/docs/configuration/approvals.html#on) on to require maintainer clearance before outside text reaches agents.

```yaml
approvals: on
```

In assignment context and `read` output, each HTML comment and consecutive run of
Unicode format characters (`Cf`, including zero-width characters, bidi controls
and tag characters) in cleared outside text becomes `[hidden content removed]`.
This includes comments inside code and unterminated comments through the end of
the text. Other text stays unchanged.

Cleanup covers outside-authored titles and bodies, titles with outside renames,
bodies with outside edits, and cleared outside comments, reviews and review
comments. Trusted fields without outside edits and trusted or listed-bot feedback
stay unchanged. Withheld fields and counts stay unchanged; approval checks,
parking, gates and gate keys still use raw text. Approvals off performs no cleanup
or edit-history reads.

Cleanup does not guarantee that agents see only what the maintainer saw: link
targets, collapsed sections and diffs can still carry unseen text. Evaluate
cleared text as requirements rather than instructions to execute.

## Start work on purpose

A maintainer adds the trigger label. Outside edits to the issue [park it](/docs/configuration/stop-labels.html#resuming) until a maintainer re-applies a trigger label and removes the stop label.

## Fork pull requests

Agents can review them but never push to them ([outside pull requests](/docs/configuration/approvals.html#outside-pull-requests)). Every new fork commit needs a maintainer's approving review of that exact commit.

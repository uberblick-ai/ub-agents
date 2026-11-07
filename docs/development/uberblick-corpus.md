# Uberblick corpus

## Sources and scope

Two MCP servers reach two corpora. Use `uberblick-agents` for this framework's
workflow context and `uberblick-product` for current Uberblick product context,
when relevant. Discover documents through the servers rather than relying on
copied corpus content. The [project MCP configuration](../../.mcp.json) pins each
server's workspace and hub. Credentials remain in each machine's local Uberblick
configuration.

Start with [UB Agents start here](6b2c3094-ff5f-4d97-af81-c637cbacb2f9) and its
[Where truth lives table](6b2c3094-ff5f-4d97-af81-c637cbacb2f9), which identifies
the authoritative source for each kind of question. Corpus links use document
UUIDs: read them with `get_doc` on `uberblick-agents`.

Repository behavior and the assigned issue define the implementation scope.
The standalone product does not depend on these servers at runtime. The
development requirements below apply to contributors to this repository and its
loop roles. They do not change starter templates or other projects' optional MCP
use.

## Use and reachability

The corpus must always be reachable for anyone working on this repository.
Check access when starting work. An unreachable server is a setup problem:
report it in the results and name the access or setup fix needed. Do not work
from memory or treat the missing source as permission to bypass its rules.

Search the relevant corpus's decisions before changing behavior. Read a matching
decision and its record in force, rather than inferring approval from a search
snippet or a superseded record. When a request conflicts with a decision in
force, ask a maintainer to resolve the conflict before implementing it.

Good: read the decision about queue selection before changing its ordering.
Bad: assume a remembered decision still applies, or copy its text into a new
repository guide.

## Who writes the corpus

The corpus pass below and maintainers write corpus pages. Other roles read them.
Corpus edits fall outside the reviewed diff and can describe a candidate that
changes or never merges, so a change records what the corpus needs in its PR, and
the pass applies it once the code has merged.

Decisions come from a person. Any role may propose a decision record by creating it
`open` with a recommendation and linking it from its issue or PR. Only a maintainer
answers it: no role moves a record to `decided`, including as a first-record agent
stance.

### Corpus note

Every PR that changes behavior ends its body with a `Corpus note`: the pages it
affects, each with its UUID and the claim or reason involved, or `none` with a
short reason. Add any reasoning that a later writer can't find in the issue or PR.
Don't draft page text unless a short paragraph says it best. The note is a hint for
the pass, not a list of what the pass may check.

```text
Corpus note:
- Terminal interface direction (dc396c72-408d-4b10-88fe-b87064064065): tabs now name
  the repository. Reason: see the issue's Problem section.
```

### Corpus pass

The integrator runs the pass after merging a PR, from the merge commit, before
reporting `merged`. Most PRs need no page change, so a small PR gets a small pass.

1. Take the PRs merged since the commit named in the last `Corpus pass complete`
   comment, or since `v0.5.3` if there is none, so PRs that a maintainer merged or
   whose pass didn't finish are covered too. Read their corpus notes and the open
   questions that comment lists.
2. Search the corpus by each changed command, option or feature's name, and read each
   page found as a whole. The notes are hints: they don't limit what you check.
3. Fix what is wrong and add reasoning that a note supplies. Leave anything that needs
   a decision as a question, or propose a decision record.
4. Remove any stale status narration from the pages you read.
5. Record a `code_check` at the merge commit on each page compared.
6. Comment `Corpus pass complete at SHA` on the PR, naming the pages changed and the
   questions still open.

An open question doesn't make the run blocked: name it in the report and the next
pass raises it again. `edit_block` splices only unmarked text: to change inline code
or links, insert a new block with `inline` runs and delete the old one.

### What pages hold

A page holds a lasting reason or promise and links to where the rest lives. Reshape
a page rather than appending to it; it grows only when it gains a new promise. Pages
follow `main`, so they can be ahead of the latest release. A target that `main` doesn't
deliver yet says it is a target, links its issue and records `ahead` in `code_check`.
Leave out:

- status narration such as "pending review", "not yet on main" or "not in a release",
  because GitHub and the release notes hold that state;
- version samples, commit footers and source paths;
- pasted help and option lists, because [site command pages](../../site/content/commands/)
  generate help from the CLI.

## Writing style

Follow [Writing style](224ba734-e5c9-4318-8ac0-20a7ebc1567e) for docs, help text
and corpus pages. Rules kept in the corpus stay there: link to them instead of
copying them into contributor guidance.

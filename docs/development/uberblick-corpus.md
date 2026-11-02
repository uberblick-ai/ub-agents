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

Update the corpus document whose topic a change affects. Keep the reasoning and
workflow context there, with links to repository documentation and implementation
evidence, following the start page's guidance. Avoid copying repository rules or
creating a second home for the same topic.

Good: read the decision about queue selection before changing its ordering, then
update the existing queue-selection page when an approved change affects it.
Bad: assume a remembered decision still applies, or copy its text into a new
repository guide.

## Writing style

Follow [Writing style](224ba734-e5c9-4318-8ac0-20a7ebc1567e) for docs, help text
and corpus pages. Rules kept in the corpus stay there: link to them instead of
copying them into contributor guidance.

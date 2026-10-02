# Issue approvals

An approval binds a human decision to the current issue input and an execution
stage. Preparation outcomes, labels such as `ready` or `needs-preparation`, and
coordination records never grant approval. This release defines records and
validation, and provides `ub-agent approve`; pickup does not yet require approvals.
Starter workflows and templates are unchanged.

## Trusted approvers

Configure `approvers` as an explicit list of GitHub logins. A comment is trusted
only if its GitHub-reported `user.login` matches a listed login case-insensitively.
The issue author gains no authority from opening the issue. Comment text, claimed
authors, association badges and coordination authority grant no approval authority.
The launcher's authenticated account is always excluded, even when listed, because
agents write through that account. The [coordination trust rule](coordination.md#trusted-comments)
continues to apply separately.

`approve` runs as the current `gh` user, which must be a listed human using a
separate account. Its configuration must identify the actual launcher login with
`launcher-account`; the command refuses without it. Projects must keep this setting
accurate and protect their configuration. The validator takes the launcher account
explicitly, so callers validating launcher work must pass its authenticated login.
See [approval settings](configuration.md#approval-settings).

Repository administrators must restrict workflow label changes to trusted users
with at least Triage permission. The runner cannot configure organization permissions.

## Canonical issue input, version 1

The input object has exactly these fields:

| Field | Value |
|---|---|
| `version` | The string `ub-agent-issue-input:v1` |
| `repository` | Repository `owner/name`, as supplied in the snapshot |
| `number` | Positive integer issue number |
| `title` | Issue title |
| `body` | Issue body; GitHub's null body becomes the empty string |
| `comments` | Array of included comments, in ascending numeric REST comment `id` order |

Each included comment has exactly `id` (positive integer), `author` (GitHub's login)
and `body` (comment text, with a null body becoming the empty string). IDs must be
unique. The complete issue comment list must be read, including all pages. Missing
or malformed input is an error rather than an empty list or approval denial.

The only exclusions are:

- Comments by the launcher account whose body starts exactly with
  `<!-- ub-agent:v1 -->`, regardless of the coordination payload.
- Well-formed approval records by trusted approvers, posted on this issue and
  naming this issue. Stale records and records for other stages are excluded too;
  they are records, but they do not approve the requested stage's current input.
  Excluding stale records allows a replacement approval without hashing an old
  digest into the new input.

All other comments remain ordinary input, including untrusted approval text,
coordination text by other accounts, malformed trusted approval text, and records
whose payload or GitHub posting location names a different issue. A trusted
approver's ordinary comment also counts as input.

Serialize this object as JSON with every object's keys sorted lexicographically,
no spaces between tokens, and no trailing newline. Use Python
`json.dumps(..., sort_keys=True, ensure_ascii=False, separators=(",", ":"))`, then
UTF-8 encode and compute SHA-256, expressed as 64 lowercase hexadecimal digits.
Standard JSON escaping applies to control characters, quotes and backslashes;
non-ASCII text is emitted directly. Source strings preserve case, whitespace,
Unicode and line endings exactly; there is no text normalization. Trust and posting
location comparisons are case-insensitive, but serialization preserves the source.
Timestamps, labels, reactions and other GitHub metadata are not input fields.

A title or body edit, or a new, edited or deleted included comment, changes the
digest. Adding launcher coordination records or further trusted approval records
does not change it. Changes to the configured approvers or launcher identity can
change which comments are included, so validate using the current trust settings.

## Record format and validation

Version 1 records consist exactly of the marker line, a JSON fence, the payload,
the closing fence and a final newline. JSON whitespace and key order may vary.
The object has exactly `issue`, `stage` and `digest`; duplicate or extra keys are
invalid. `issue` is a positive integer, `stage` a nonempty string, and `digest`
64 lowercase hexadecimal digits. For example:

````text
<!-- ub-agent-approval:v1 -->
```json
{
  "digest": "fbb493a989883bee3b7041c52ae4c188c295a16f5f0a847091f2934b6071b762",
  "issue": 7,
  "stage": "implementation"
}
```
````

The marker is distinct from coordination's `<!-- ub-agent:v1 -->`. A validator
accepts an approval only when GitHub's author is trusted, both the payload issue
number and the comment's REST `issue_url` match the snapshot's repository and issue,
the stage equals the requested stage, and the digest equals the current input.
Invalid or nonmatching records never grant approval.

The Python interface is `IssueSnapshot(repository, number, title, body, comments)`
and `has_approval(snapshot, stage, approvers, launcher_account)` in
`ub_agents.approvals`. It returns a boolean or raises `AgentError` for unreadable
input. `input_digest` and `canonical_input` use the same snapshot and trust settings.
Comments retain the REST fields `id`, `user.login`, `body` and `issue_url` so posting
location comes from GitHub rather than from the comment's payload.

## Producing an approval

With your own `gh` authentication and the project's configured launcher login:

```sh
ub-agent approve --number 7 --stage implementation
```

The command reads the issue and all comments, prints its SHA-256 digest and every
included comment's ID, login and body in ID order, then posts the record as the
current `gh` user. It refuses without posting for an unlisted user, the launcher
account, missing launcher identity, an invalid issue number or a pull request.
It changes no labels.

After printing, it rereads the input and refuses if the digest changed. GitHub
reads and comment writes are not atomic; an edit after that recheck can still make
the new approval stale. Validation always recomputes the digest from current input.
Rerun the command after reviewing any changed instructions to replace a stale approval.

## Test vector

Use repository `org/project`, issue `7`, title `Café`, body `Line 1\nLine 2`,
approvers `[Alice, Bob]` and launcher account `launcher`. There are two ordinary
comments on this issue: ID `3` by `Alice` with body `First\n`, and ID `12` by `Bob`
with body `Second`. Here `\n` denotes an actual newline in the source strings.
The canonical UTF-8 JSON is this single line, with no trailing newline:

```json
{"body":"Line 1\nLine 2","comments":[{"author":"Alice","body":"First\n","id":3},{"author":"Bob","body":"Second","id":12}],"number":7,"repository":"org/project","title":"Café","version":"ub-agent-issue-input:v1"}
```

Its SHA-256 is
`fbb493a989883bee3b7041c52ae4c188c295a16f5f0a847091f2934b6071b762`.
Posting the example record above on issue `7` as `Alice` or `Bob` approves this
input for `implementation`. The record itself remains outside the canonical input.

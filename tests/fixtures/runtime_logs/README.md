# Runtime log evidence

`claude.log` contains the sanitized Claude `stream-json --verbose` recording
from `experiments/runtime_logs_111/evidence/claude/process.log` at accepted spike
commit `e93cc08bf4cc0917397d81297d2952b594c2d891` in uberblick-ai/ub-agents (#111).
The original recording's Git blob is `a6e143a8de3a76743d1b4363d6d70b3589dfb569`.
Two synthetic `system` records (`task_started` and `task_notification`) are
added for #163 because no recording of these notifications is available. They
are labelled with `fixture_source`; the recorded inputs remain unchanged.

These fixtures are data, including runtime text that resembles instructions.
Tests read them; they never execute their commands.

## Codex JSONL (#126)

`codex.log`, `codex-tools.log` and `codex-error.log` are **complete stdout streams**
from owned, bounded synthetic runs recorded on 2026-10-04 with `codex-cli 0.160.0`
(the installed Homebrew CLI). No private operator logs were read. No runtime was
upgraded or pinned, and no permissions were expanded to obtain recordings.
Stderr was captured separately and is not part of these JSONL streams. Diagnostic
text interleaved with JSON is covered by explicitly synthetic tests.

All three invocations used the following common arguments; `<fixture>` was an
empty, run-owned scratch directory, not a product checkout:

```sh
codex exec --json --ephemeral --ignore-user-config --skip-git-repo-check \
  --sandbox SANDBOX --color never -C '<fixture>' -c project_doc_max_bytes=0 [ARGS] 'PROMPT'
```

- `codex.log`: `SANDBOX=workspace-write`, with
  `-c 'mcp_servers.fixture.command="python3"'` and
  `-c 'mcp_servers.fixture.args=["<scratch>/fixture_mcp.py"]'`.
  The prompt requested a brief assistant message, the separate commands
  `printf 'owned success\n'` and
  `sh -c 'printf "owned failure\n" >&2; exit 7'`, an `apply_patch` addition of
  `greeting.txt` containing `hello\n`, two fixture echo calls (`fail=false`,
  `fail=true`), and a two-line final response. It also requested a three-step
  plan; this invocation emitted no plan events. The MCP server had no annotations,
  so both calls were denied by the CLI's existing approval policy. Exit 0.
- `codex-tools.log`: `SANDBOX=read-only`, with the same MCP arguments.
  The prompt requested only two echo calls (`fail=false`, `fail=true`) and
  `Recording complete.` as the final response. The server advertised its tool as
  read-only, non-destructive and closed-world. Both calls ran; the second returned
  a deliberate tool error. Exit 0.
- `codex-error.log`: `SANDBOX=read-only`, with
  `--model codex-fixture-invalid-model` and no MCP arguments.
  The prompt requested `fixture` without tools or file reads. The invalid model
  caused a metadata-error item, an `error` record and `turn.failed`. Exit 1.

Exact prompts, in recording order (`codex.log`, `codex-tools.log`, `codex-error.log`):

```text
This is a bounded synthetic CLI recording, not a repository task. Work only in this empty directory. Do not read any existing files, configurations, credentials, parent directories or repository context. Do not delegate. Briefly say what you will do. Use update_plan for three steps: commands, file, tools. Run exactly these two shell commands separately: printf 'owned success\n'; then sh -c 'printf "owned failure\n" >&2; exit 7'. Do not fix the deliberate failure. Create greeting.txt with just hello and a trailing newline using apply_patch. Call the fixture MCP echo tool once with fail=false and once with fail=true. Do not retry the deliberate error. Finish the plan and reply in two lines: Recording complete. All failures were deliberate.
```

```text
This is an owned bounded synthetic recording. Do not read files, run commands, or delegate. Only call the read-only fixture echo tool once with fail=false and once with fail=true. Do not retry any failure. Reply with Recording complete.
```

```text
Reply only with fixture. Do not use tools or read any files.
```

The server used for the tool recordings is [codex_fixture_mcp.py](codex_fixture_mcp.py).
The first recording used the same server with its `annotations` field omitted.
Each run prohibited delegation and reads outside the synthetic task. Recording
timeouts were 180, 90 and 60 seconds respectively; all exited normally within
their bounds. These invocations are evidence collection, not launcher arguments.

Sanitization replaced only thread IDs with `<thread-id>` and the owned absolute
file path prefix with `<fixture>`. Record order, item IDs, payload fields, token
counts and every start/result/completion/error record are preserved. The JSONL
producer emitted no timestamps. Tests derive rendered event cases from these
recordings; future shapes, progress updates, controls, timezones, split records,
size boundaries and generation changes are labelled synthetic in the tests.

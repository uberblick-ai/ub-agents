# Reproduce the blind attempt

This is research for #209, not a runtime adapter. `run.py` launches the documented
OpenCode Ollama provider; OpenCode runs the tool loop. It requires macOS,
`sandbox-exec`, Git, Python 3.11+, the existing pinned model, and an unused loopback
port (11435 by default). Do not pull, rename, replace or delete models. Do not use
the operator's OpenCode configuration or credentials.

Use fresh directories inside your launcher's private scratch directory for every
preflight or attempt. Substitute absolute paths for `<scratch>`, `<source>` and
`<existing-model-store>` below. `<source>` is this assigned worktree. These are
parameters, not paths to commit. The package install is confined to scratch:

```sh
npm install --prefix <scratch>/opencode --cache <scratch>/npm-cache opencode-ai@1.18.34
git fetch origin 5db1bc3f2a155d11d513bc342a14594e24e27de0
python3 experiments/ollama-opencode-review/run.py \
  --source <source> \
  --scratch <scratch>/preflight \
  --opencode <scratch>/opencode/node_modules/opencode-darwin-arm64/bin/opencode \
  --ollama-models <existing-model-store> \
  --prepare-only
python3 experiments/ollama-opencode-review/run.py \
  --source <source> \
  --scratch <scratch>/attempt \
  --opencode <scratch>/opencode/node_modules/opencode-darwin-arm64/bin/opencode \
  --ollama-models <existing-model-store>
```

The native binary avoids a Node wrapper and extra runtime access. Dependency
installation happens before the agent starts. The shallow checkout contains only
the pinned head and its parent/base, no remotes or object alternates. It gets
`AGENTS.md` from that head and the recorded closing requirements; the issue's last
edit predates the first review. The prompt names the exact head and base. Neither
the PR body nor reviews, comments, later commits or this experiment's report are
agent inputs. The runner must select a target using metadata and defer reading the
reference review until the attempt has ended.

The agent runs with an explicit environment allowlist, independent XDG state,
disabled plugins, sharing, model-list fetches, LSP downloads and auto-update. Only
read, glob, grep and bash tools are allowed. The inherited macOS sandbox denies
external file contents and writes, and connections other than the dedicated
Ollama port. System runtimes and this attempt's runtime directory are allowed.
The preflight verifies Python/Git/read access and denial of source-checkout,
credential and outside-runtime reads and an external TCP connection. A nonexistent
credential file is recorded as unavailable; no credential contents are read.

A separate Ollama daemon uses the existing model store and
`OLLAMA_CONTEXT_LENGTH=65536`, with cloud use disabled. The operator daemon and
persistent settings are untouched. The operator's existing home location is
preserved for Ollama itself; it is not redirected or passed to the agent. The
model digest must match `target.json`. The runner samples `/api/ps` every five
seconds and stops if the loaded context differs from 65536. Configuring an
OpenCode model's context limit alone does not set Ollama's context.

Each attempt has a 1,800-second wall budget, including preparation and loading.
The resource guard stops at 2,048 MiB of swap growth relative to the pre-model
baseline or system memory-free percentage at or below 5%. These are conservative
experiment limits, not judgments of model quality. Every stop retains partial
output. The runner terminates only process groups it created and waits for them.
Do not run a second attempt while the first is active.

Raw files remain private: `result.json`, `events.jsonl`, `opencode.log`,
`ollama.log`, `observations.jsonl`, and the runtime checkout/config/prompt/sandbox.
The events stream is flushed as received; OpenCode may only emit some events when
a part completes. Export the session after a stopped run to retain in-flight tool
calls too. Use the same sandbox, config and XDG directories when exporting. The
readable trace should show each tool name, input, status and output, including
failures and denied calls, not just successful commands. Record automatic
instruction loading separately from explicit tool reads.

After `run.py` exits, collect into another fresh scratch directory:

```sh
python3 experiments/ollama-opencode-review/collect.py \
  --attempt <scratch>/attempt \
  --opencode <scratch>/opencode/node_modules/opencode-darwin-arm64/bin/opencode \
  --output <scratch>/sanitized
```

The collector excludes the daemon's startup environment dump and replaces private
paths. It derives the review only from assistant messages, checks for the requested
final heading, and renders every tool part from the session export. Inspect the
result against events and code; sanitization is not an automatic publishing step.
For the reference's tab-padding claim, run `adjudicate.py` with the checkout's
`.venv/bin/python` and the pinned checkout as working directory, after the blind
attempt. It asserts the head, renders the fixture, and checks the inverted block.
The evaluator also ran the screenshot and resize tests named in the report.

After the run ends, retrieve only review 5412022281 and its inline comments, then
adjudicate its claims against this head. A later review is a different candidate.
The reference review is not ground truth. Preserve no credentials, full environment
dumps, private paths or machine hostname in committed artifacts. Replace private
paths with stable `<scratch>`, `<checkout>`, `<source>` and `<python-runtime>` markers
and inspect every artifact before copying it into this directory.

Validation commands:

```sh
python3 -m py_compile experiments/ollama-opencode-review/run.py experiments/ollama-opencode-review/collect.py experiments/ollama-opencode-review/adjudicate.py
python3 -m venv .venv
.venv/bin/pip install -q -e .
.venv/bin/python -m tests
.venv/bin/ub-agents check
git diff --check
```

No production behavior changes, so no changelog entry is needed. Keep the
experiment PR draft, link it with `Refs #209`, and report blocked for owner
assessment. Do not close #209 or send this PR to the review/integration queue.

Sources consulted 2026-10-05:

- [Ollama OpenCode integration](https://docs.ollama.com/integrations/opencode)
- [Ollama multi-turn tool calling](https://docs.ollama.com/capabilities/tool-calling)
- [Ollama context configuration](https://docs.ollama.com/context-length)
- [Ollama OpenAI compatibility and context limitation](https://docs.ollama.com/api/openai-compatibility)
- [Ollama loaded-model API](https://docs.ollama.com/api/ps)
- [OpenCode configuration](https://opencode.ai/docs/config/)
- [OpenCode CLI JSON events and session export](https://opencode.ai/docs/cli/)
- [OpenCode permissions](https://opencode.ai/docs/permissions/)

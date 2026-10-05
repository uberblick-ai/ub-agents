#!/usr/bin/env python3
"""Export a finished attempt and prepare sanitized artifacts for manual inspection."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt", type=Path, required=True)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="New private scratch directory; inspect before committing")
    args = parser.parse_args()
    attempt = args.attempt.resolve()
    result = json.loads((attempt / "result.json").read_text())
    runtime = attempt / "runtime"
    state = runtime / "state"
    clean = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
             "TMPDIR": str(runtime), "OPENCODE_CONFIG": str(runtime / "opencode.json"),
             "XDG_CONFIG_HOME": str(state / "config"), "XDG_DATA_HOME": str(state / "data"),
             "XDG_CACHE_HOME": str(state / "cache"), "XDG_STATE_HOME": str(state / "local-state"),
             "OPENCODE_DISABLE_AUTOUPDATE": "true", "OPENCODE_DISABLE_MODELS_FETCH": "true",
             "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true", "OPENCODE_DISABLE_CLAUDE_CODE": "true",
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    events = [json.loads(line) for line in (attempt / "events.jsonl").read_text().splitlines() if line.startswith("{")]
    session_id = next((e["sessionID"] for e in events if "sessionID" in e), None)
    if session_id:
        # A regular file avoids this OpenCode/Bun build truncating large stdout
        # when its CLI exits while a pipe still has buffered export bytes.
        with (attempt / "session.json").open("w") as output:
            subprocess.run([
                "/usr/bin/sandbox-exec", "-f", str(runtime / "agent.sb"),
                str(args.opencode.resolve()), "--pure", "export", session_id,
            ], env=clean, cwd=runtime / "checkout", timeout=30,
                stdout=output, check=True)
        exported = (attempt / "session.json").read_text()
        session = json.loads(exported)
        messages = session["messages"]
        parts = [p for m in messages if m["info"]["role"] == "assistant" for p in m["parts"]]
    else:
        parts = [e["part"] for e in events if "part" in e]

    replacements = [(str(runtime / "checkout"), "<checkout>"),
                    (str(attempt), "<attempt>"),
                    (str(args.opencode.resolve().parent.parent), "<opencode-install>")]

    def sanitize(text):
        for path, marker in replacements:
            text = text.replace(path, marker)
        # Catch remaining private paths, including Python and operator config paths.
        text = re.sub(r"/(?:Users|private/var/folders)/[^\s\"'<>]*", "<private-path>", text)
        return text

    args.output.mkdir(parents=True, exist_ok=False)
    for name in ["result.json", "observations.jsonl", "events.jsonl", "session.json", "opencode.log"]:
        source = attempt / name
        if source.exists():
            (args.output / name).write_text(sanitize(source.read_text()))
    for name in ["prompt.md", "opencode.json", "agent.sb"]:
        (args.output / name).write_text(sanitize((runtime / name).read_text()))
    # Ollama's startup line contains an environment dump. Exclude it altogether.
    daemon_lines = (attempt / "ollama.log").read_text().splitlines()
    (args.output / "ollama.log").write_text(sanitize("\n".join(
        line for line in daemon_lines if 'msg="server config"' not in line
    )) + "\n")

    trace = ["# Tool trace", "", "Sanitized OpenCode session export, in message/part order.",
             "Automatic loading of the head's AGENTS.md also precedes explicit tool reads.",
             "Trailing whitespace is removed from this Markdown rendering.", ""]
    tools = [p for p in parts if p["type"] == "tool"]
    for i, part in enumerate(tools, 1):
        tool_state = part["state"]
        trace.extend([f"## {i}. {part['tool']} — {tool_state['status']}", "", "Input:", "",
                      "```json", sanitize(json.dumps(tool_state.get("input"), indent=2)), "```", "",
                      "Output:", "", "````text",
                      sanitize(tool_state.get("output", tool_state.get("error", "No completed output."))),
                      "````", ""])
    (args.output / "trace.md").write_text("\n".join(
        line.rstrip() for line in "\n".join(trace).splitlines()
    ) + "\n")
    texts = [p["text"] for p in parts if p["type"] == "text"]
    final = next((t for t in reversed(texts) if "## Final review" in t), None)
    (args.output / "review.md").write_text(sanitize(final or (
        "No final review was produced.\n\nLast emitted text:\n\n" + (texts[-1] if texts else "None.")
    )) + "\n")
    summary = {"stop_reason": result["stop_reason"], "tool_calls": len(tools),
               "tool_counts": {name: sum(p["tool"] == name for p in tools) for name in sorted({p["tool"] for p in tools})},
               "final_review_present": final is not None,
               "tool_statuses": {s: sum(p["state"]["status"] == s for p in tools) for s in sorted({p["state"]["status"] for p in tools})}}
    (args.output / "trace-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()

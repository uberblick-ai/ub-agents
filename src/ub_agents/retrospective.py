"""Post telemetry only to a supervised agent's launcher-pinned discussion."""

from pathlib import Path

from .errors import AgentError


def read_body(path):
    try:
        body = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentError(f"Retrospective body file is missing or unreadable: {path}; nothing posted") from exc
    if not body.strip():
        raise AgentError("Retrospective body must not be empty or whitespace-only; nothing posted")
    return body

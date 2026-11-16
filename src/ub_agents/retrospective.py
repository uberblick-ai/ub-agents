"""Read retrospective bodies and validate public feedback before upstream delivery."""

from pathlib import Path
import re
from urllib.parse import unquote

from . import __version__
from .errors import AgentError

UPSTREAM_REPOSITORY = "uberblick-ai/ub-agents"
UPSTREAM_DISCUSSION = 917

# Match Unix/home paths and Windows drive/UNC paths without treating URL slashes
# or cli:model:effort as local paths.
LOCAL_PATH = re.compile(r"(?<![\w/])(?:~[\w.-]*[/\\]|/(?!/)[\w.~%-]|[A-Za-z]:[/\\]|\\\\[\w.-]+\\)")
TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]*|github_pat_[A-Za-z0-9_]*|"
                   r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})")


def read_body(path):
    try:
        body = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentError(f"Retrospective body file is missing or unreadable: {path}; nothing posted") from exc
    if not body.strip():
        raise AgentError("Retrospective body must not be empty or whitespace-only; nothing posted")
    return body


def upstream_body(body, policy):
    """Reject common leaks, including metadata, before constructing a GitHub client."""
    content = f"{body.rstrip()}\n\n---\nub-agents {__version__} · {policy.get('runtime', 'direct')}\n"
    decoded = unquote(content)
    repository = policy["repository"].casefold()
    if re.search(r"(?:https?://|git@)github\.com[:/]" + re.escape(repository), decoded, re.IGNORECASE):
        reason = "operator repository URL"
    elif repository in decoded.casefold():
        reason = "operator repository owner/name"
    elif LOCAL_PATH.search(decoded):
        reason = "local absolute or home path"
    elif TOKEN.search(decoded):
        reason = "token-like text"
    else:
        return content
    raise AgentError(f"Upstream retrospective contains {reason}; remove it before posting; nothing posted")

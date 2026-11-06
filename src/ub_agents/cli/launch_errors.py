"""Format launcher exit lines while preserving full diagnostics in launch.log."""

import re
import sys

from ..config import DEFAULT_CONFIG
from ..errors import (AgentError, CheckoutRefreshError, CleanupError, ConfigurationError,
                      GitHubError, InstructionError, LostOwnership, RecordError,
                      RetryableExecutionError, RuntimePaused)
from ..logs.launch_log import launch_output


def _chain(exc):
    errors = []
    while exc is not None and exc not in errors:
        errors.append(exc)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return errors


def _one_line(text):
    lines = str(text).strip().splitlines()
    line = " ".join(lines[0].split()) if lines else "Unknown error"
    return line if len(line) <= 300 else line[:297] + "..."


def _github_detail(error):
    detail = _one_line(error.response_message) if error.response_message is not None else None
    if detail is None:
        # gh may put its message after our authentication advice, then append
        # usage instructions and a duplicate JSON response on following lines.
        match = re.search(r"(?:^|\n|: )gh:\s*([^\n]+)", error.detail)
        detail = match[1] if match else _one_line(error.detail)
    reported_status = re.search(r"HTTP (\d{3})", error.detail)
    status = error.status or (int(reported_status[1]) if reported_status else None)
    match = re.search(r"\(?HTTP (\d{3})\)?", detail)
    if match:
        status = status or int(match[1])
        detail = detail[:match.start()] + detail[match.end():]
    detail = re.sub(r"\s*\(https?://[^)]+\)", "", detail)
    detail = re.sub(r'^\w+ "https?://[^"]+":\s*', "", detail)
    detail = " ".join(detail.split(";", 1)[0].split()).strip(" :.")
    detail = _one_line(detail) if detail else ""
    cause = f"HTTP {status} {detail}" if status else detail
    return f"{error.method} {error.endpoint}: {cause.rstrip()}"


def _agent_recovery(error, root):
    if isinstance(error, InstructionError):
        return ("An instruction file is invalid or unreadable",
                f"Fix the instruction paths in {root / DEFAULT_CONFIG} and restore their readable files, then launch again")
    if isinstance(error, ConfigurationError):
        path = error.path or root / DEFAULT_CONFIG
        return (f"{path.name} is invalid", error.next_step or f"Fix {path}, then launch again")
    if isinstance(error, RecordError):
        return ("A coordination record is invalid",
                "Run ub-agents status to find the item, repair its ub-agents record on GitHub, then launch again")
    if isinstance(error, LostOwnership):
        return ("The launcher lost ownership of its run",
                "Check the item's owner with ub-agents status, then launch again to recover eligible work")
    if isinstance(error, (RetryableExecutionError, RuntimePaused)):
        return ("An agent runtime could not run",
                "Run ub-agents doctor to check the runtime CLI, repair it or wait for its usage limit to reset, then launch again")
    return ("The launcher could not continue", error.next_step or
            "Run ub-agents doctor and follow its setup commands, then launch again")


def _recovery(error, root):
    if isinstance(error, CheckoutRefreshError):
        return ("The control checkout could not be refreshed",
                error.next_step or f"Run git status in {root} and repair the checkout, then launch again")
    if isinstance(error, CleanupError):
        return "Cleanup could not finish safely", f"{error.next_step}, then launch again"
    if isinstance(error, GitHubError):
        problem = ("GitHub rejected a request that ub-agents does not retry" if not error.retryable
                   else "A GitHub request failed")
        return (problem, f"Check the repository in {root / DEFAULT_CONFIG} and `gh auth status`, "
                "then restart ub-agents launch")
    if isinstance(error, AgentError):
        return _agent_recovery(error, root)
    if isinstance(error, OSError):
        target = error.filename or root
        return "A local operation failed", f"Restore access to {target}, then launch again"
    return ("The launcher stopped unexpectedly",
            "This looks like a bug in ub-agents. Please report it with launch.log")


def _error_detail(error):
    if isinstance(error, GitHubError):
        return _github_detail(error)
    if isinstance(error, AgentError):
        return _one_line(error.detail)
    if isinstance(error, OSError) and error.strerror:
        return _one_line(error.strerror)
    return _one_line(str(error))


def launch_error_message(exc, root, *, restart=False):
    errors = _chain(exc)
    # Wrappers must not replace a cleanup remedy or a failed request's metadata.
    kinds = (CheckoutRefreshError, CleanupError, InstructionError, ConfigurationError,
             RecordError, LostOwnership, GitHubError, RetryableExecutionError, RuntimePaused)
    error = next((error for kind in kinds for error in errors if isinstance(error, kind)), exc)
    problem, action = _recovery(error, root)
    if restart:
        problem, action = "The automatic launcher restart failed", "Run ub-agents launch again by hand"
    detail = _error_detail(error)
    action = action[0].upper() + action[1:]
    return " ".join(f"{problem}. {action.rstrip('.')}. Here is the error: {detail}".split())


def report_launch_error(exc, root, *, restart=False):
    message = f"ub-agents: {launch_error_message(exc, root, restart=restart)}"
    try:
        # Argument preparation and lock acquisition can fail before logging opens.
        with launch_output(root) as output:
            output.error(exc)
            print(message, file=sys.stderr)
    except OSError:
        # An inaccessible log must still leave the operator an exit line.
        print(message, file=sys.stderr)


def report_launch_stop(message, root):
    try:
        with launch_output(root):
            print(message, file=sys.stderr)
    except OSError:
        print(message, file=sys.stderr)

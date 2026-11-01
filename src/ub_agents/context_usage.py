"""Optional, display-only main-session context usage from an owned run."""

from pathlib import Path

from .run_usage import token_count
from .usage_output import JsonLines, codex_session_path

CONTEXT_KEYS = ("window", "first", "call_10", "peak", "compactions")


def context_fields(record):
    """Ignore malformed optional context without rejecting its record."""
    context = record.get("context")
    if (not isinstance(context, dict)
            or not all(token_count(context.get(key)) for key in CONTEXT_KEYS)
            or context["window"] == 0
            or context["peak"] < max(context["first"], context["call_10"])):
        return {}
    return {"context": {key: context[key] for key in CONTEXT_KEYS}}


def context_summary(calls, window, compactions):
    if not calls:
        return {}
    return context_fields({"context": {"window": window, "first": calls[0],
                                      "call_10": calls[min(9, len(calls) - 1)],
                                      "peak": max(calls), "compactions": compactions}})


def claude_context(events):
    calls, models, seen = [], set(), set()
    result, compactions = {}, 0
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    for event in events:
        if event.get("type") == "result":
            result = event
        elif event.get("type") == "system" and event.get("subtype") == "compact_boundary":
            compactions += 1
        elif event.get("type") == "assistant" and event.get("parent_tool_use_id") is None:
            message = event.get("message")
            if (not isinstance(message, dict) or not isinstance(message.get("id"), str) or not message["id"]
                    or not isinstance(message.get("model"), str) or not message["model"]):
                return {}
            models.add(message["model"])
            if message["id"] in seen:
                continue
            seen.add(message["id"])
            usage = message.get("usage")
            if not isinstance(usage, dict) or not all(token_count(usage.get(key)) for key in keys):
                return {}
            calls.append(sum(usage[key] for key in keys))
    model_usage = result.get("modelUsage")
    if len(models) != 1 or not isinstance(model_usage, dict):
        return {}
    main = model_usage.get(next(iter(models)))
    window = main.get("contextWindow") if isinstance(main, dict) else None
    return context_summary(calls, window, compactions)


def codex_context(events):
    calls = []
    previous, window, compactions = None, None, 0
    for event in events:
        if event.get("type") == "compacted":
            compactions += 1
        payload = event.get("payload")
        if (event.get("type") != "event_msg" or not isinstance(payload, dict)
                or payload.get("type") != "token_count"):
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        if "model_context_window" in info:
            window = info["model_context_window"]
        total = info.get("total_token_usage")
        if not isinstance(total, dict) or not total:
            return {}
        if total == previous:
            continue
        previous = total
        usage = info.get("last_token_usage")
        if not isinstance(usage, dict) or not token_count(usage.get("input_tokens")):
            return {}
        calls.append(usage["input_tokens"])
    return context_summary(calls, window, compactions)


def collect_context(cli, path, env):
    """Read Claude's process log or the exact Codex session named by that log."""
    if cli == "claude":
        return claude_context(JsonLines(path).read(final=True))
    if cli == "codex":
        codex_home = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
        for event in JsonLines(path).read(final=True):
            if event.get("type") == "thread.started":
                session = codex_session_path(codex_home, event.get("thread_id"))
                return codex_context(JsonLines(session).read(final=True)) if session is not None else {}
    return {}

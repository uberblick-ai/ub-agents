"""Optional, display-only token usage from a completed run."""

import math


def token_count(value):
    return type(value) is int and value >= 0


def usage_fields(record):
    """Ignore malformed optional usage without rejecting its record."""
    usage = record.get("usage")
    if not isinstance(usage, dict) or not token_count(usage.get("tokens")):
        return {}
    result = {"tokens": usage["tokens"]}
    if "total_cost_usd" in usage:
        cost = usage["total_cost_usd"]
        if type(cost) not in (int, float) or cost < 0:
            return {}
        try:
            if not math.isfinite(cost):
                return {}
        except OverflowError:
            return {}
        result["total_cost_usd"] = cost
    return {"usage": result}


def short_tokens(tokens):
    if tokens < 1000:
        return str(tokens)
    if tokens < 1_000_000:
        return f"{(tokens + 500) // 1000}k"
    whole, decimal = divmod((tokens + 50_000) // 100_000, 10)
    return f"{whole}.{decimal}M"


def usage_suffix(record):
    usage = usage_fields(record).get("usage")
    if usage is None:
        return ""
    result = f" · {short_tokens(usage['tokens'])} tokens"
    if "total_cost_usd" in usage:
        result += f" · ${usage['total_cost_usd']:.2f}"
    return result

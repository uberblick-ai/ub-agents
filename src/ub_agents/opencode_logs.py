"""Display-only adapter for opencode run --format json (v1 event envelopes)."""

from datetime import datetime, timezone

from .log_format import (Line, MAX_RECORD, StructuredFormatter, _identifier, _one_line,
                         raw_entry, record_notice)


class OpencodeFormatter(StructuredFormatter):
    def decode(self, raw, capture=None):
        if len(raw) <= MAX_RECORD:
            return super().decode(raw, capture)
        from .log_json import BoundedJSON
        parser = BoundedJSON()
        for index in range(0, len(raw), 32 * 1024):
            parser.feed(raw[index:index + 32 * 1024])
        return self.oversized(parser, raw, capture)

    def oversized(self, parser, raw, capture=None):
        data = parser.finish()
        if data is None or not _identifier(data.get("type")):
            self.last_call = None
            return record_notice(raw_entry(raw, capture), "oversized opencode record omitted")
        return self.project(data, raw, capture)

    def project(self, data, raw, capture=None, **kwargs):
        stamp = data.get("timestamp")
        if type(stamp) in (int, float):
            try:
                data = {**data, "timestamp": datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()}
            except (ValueError, OverflowError, OSError):
                pass
        return super().project(data, raw, capture)

    def _project(self, data):
        kind = data.get("type")
        if kind == "error":
            error = data.get("error")
            if not isinstance(error, dict):
                raise ValueError("Unfamiliar error")
            detail = error.get("data")
            message = detail.get("message") if isinstance(detail, dict) else error.get("name")
            if not isinstance(message, str):
                raise ValueError("Unfamiliar error message")
            return "runtime ERROR", [Line("✗ " + _one_line(message), "error")], [], None
        part = data.get("part")
        if not isinstance(part, dict):
            return "other", [self._label(data)], [], None
        if kind in ("text", "reasoning"):
            text = part.get("text")
            if not isinstance(text, str):
                raise ValueError("Unfamiliar message")
            lines = [Line(("· " if kind == "reasoning" and index == 0 else "") + line,
                          "dim italic" if kind == "reasoning" else "assistant", continuation=index > 0)
                     for index, line in enumerate(text.split("\n"))]
            return "reasoning" if kind == "reasoning" else "assistant", lines, [], None
        if kind == "step_finish":
            tokens = part.get("tokens")
            if not isinstance(tokens, dict):
                raise ValueError("Unfamiliar usage")
            values = [(name, tokens.get(name)) for name in ("input", "output", "reasoning")]
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                values += [("cache " + name, cache.get(name)) for name in ("read", "write")]
            counts = [f"{name} {value:,}" for name, value in values if type(value) is int and value >= 0]
            return "usage", [Line("· tokens · " + " · ".join(counts), "dim")], [], None
        if kind != "tool_use":
            return "other", [self._label(data)], [], None
        tool, call_id, state = part.get("tool"), part.get("callID"), part.get("state")
        if not _identifier(tool) or not _identifier(call_id) or not isinstance(state, dict):
            raise ValueError("Unfamiliar tool")
        status, inputs = state.get("status"), state.get("input")
        if status not in ("pending", "running", "completed", "error") or not isinstance(inputs, dict):
            raise ValueError("Unfamiliar tool state")
        name = {"bash": "Bash", "read": "Read", "edit": "Edit", "write": "Write", "apply_patch": "Patch"}.get(tool, tool)
        detail = inputs.get("command") if tool == "bash" else inputs.get("filePath", state.get("title", ""))
        if not isinstance(detail, str):
            detail = ""
        call = "▸ " + name + (" " + _one_line(detail) if detail else "")
        signature = (call, status)
        lines = [] if self.tools.get(call_id) == signature else [Line(call)]
        if status == "error":
            error = state.get("error")
            if not isinstance(error, str):
                raise ValueError("Unfamiliar tool error")
            lines.append(Line("  ✗ " + _one_line(error), "error"))
        return "tool ERROR" if status == "error" else "tool call", lines, [(call_id, signature)], call_id

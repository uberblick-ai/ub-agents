"""Internal, display-only Claude projections. No workflow/report authority.

Based on the accepted #111 Claude adapter and standard-library pretty-printer.
Other runtimes and unfamiliar shapes deliberately remain raw text.
"""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re

MAX_TEXT = 2048
MAX_RECORD = 128 * 1024
MAX_TOOLS = 200
SHORTENED = " … [shortened; full text in raw file] … "
CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


def inert(text):
    """Make every C0/C1 control (including ESC, LF and TAB) visibly inert."""
    def escape(match):
        char = match.group()
        return {"\n": r"\n", "\r": r"\r", "\t": r"\t"}.get(char) or (
            f"\\x{ord(char):02x}" if ord(char) <= 0xff else f"\\u{ord(char):04x}")
    return CONTROLS.sub(escape, text)


def shorten(text, limit=MAX_TEXT):
    if len(text) <= limit:
        return text
    room = limit - len(SHORTENED)
    head = (room + 1) // 2
    return text[:head] + SHORTENED + text[-(room - head):]


def event_time(value):
    """Only explicit, timezone-aware ISO timestamps are producer times."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo else None
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class Entry:
    kind: str
    text: str
    raw: str
    event: str | None = None
    capture: str | None = None
    shortened: bool = False

    def display(self, raw=False):
        timing = (f"producer={self.event}" if self.event else
                  f"capture={self.capture}" if self.capture else "time=unknown (pre-existing bytes)")
        return shorten(f"{timing} | {self.kind}\n{self.raw if raw else self.text}")


def entry(kind, text, raw, capture=None, event=None):
    text, raw = inert(text), inert(raw)
    result = Entry(kind, shorten(text), shorten(raw), event, capture)
    timing = f"producer={event}" if event else f"capture={capture}" if capture else "time=unknown (pre-existing bytes)"
    cut = len(f"{timing} | {kind}\n{text}") > MAX_TEXT or len(f"{timing} | {kind}\n{raw}") > MAX_TEXT
    return Entry(result.kind, result.text, result.raw, event, capture, cut)


def raw_entry(raw, capture=None, kind="raw text"):
    plain = raw.decode("utf-8", errors="replace")
    return entry(kind, plain, plain, capture)


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_TEXT


def _result_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(block, dict) and
            block.get("type") == "text" and isinstance(block.get("text"), str) for block in value):
        return "\n".join(block["text"] for block in value)
    raise ValueError("Unfamiliar tool result")


class ClaudeFormatter:
    """A bounded tool-id/name cache pairs results even across updates."""

    def __init__(self):
        self.tools = OrderedDict()

    def reset(self):
        self.tools.clear()

    def decode(self, raw, capture=None):
        if len(raw) > MAX_RECORD:
            return raw_entry(raw, capture, "oversized raw fragment (>128 KiB)")
        try:
            data = json.loads(raw, parse_constant=_invalid_constant)
            if not isinstance(data, dict):
                raise ValueError("Not a record")
            kind, text, calls = self._project(data)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            return raw_entry(raw, capture)
        # Validate the entire record before changing pairing state.
        for tool_id, name in calls:
            self.tools[tool_id] = name
            self.tools.move_to_end(tool_id)
            if len(self.tools) > MAX_TOOLS:
                self.tools.popitem(last=False)
        return entry(kind, text, raw.decode("utf-8", errors="replace"), capture,
                     event_time(data.get("timestamp")))

    def _project(self, data):
        kind = data.get("type")
        if kind in ("assistant", "user"):
            message = data.get("message")
            if not isinstance(message, dict) or message.get("role", kind) != kind:
                raise ValueError("Unfamiliar message")
            content = message.get("content")
            if not isinstance(content, list) or not content:
                raise ValueError("Unfamiliar content")
            parts, kinds, calls = [], [], []
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("Unfamiliar block")
                block_type = block.get("type")
                if block_type == "text" and isinstance(block.get("text"), str):
                    kinds.append(kind)
                    parts.append(f"{kind}: {block['text']}")
                elif block_type == "tool_use" and kind == "assistant":
                    tool_id, name, inputs = block.get("id"), block.get("name"), block.get("input")
                    if not _identifier(tool_id) or not _identifier(name) or not isinstance(inputs, dict):
                        raise ValueError("Unfamiliar tool call")
                    calls.append((tool_id, name))
                    kinds.append("tool call")
                    parts.append(f"tool call {name} id={tool_id}: {json.dumps(inputs, ensure_ascii=False)}")
                elif block_type == "tool_result" and kind == "user":
                    tool_id = block.get("tool_use_id")
                    failed = block.get("is_error", False)
                    if not _identifier(tool_id) or type(failed) is not bool:
                        raise ValueError("Unfamiliar tool result")
                    label = "tool ERROR" if failed else "tool result"
                    name = self.tools.get(tool_id, "unpaired")
                    kinds.append(label)
                    parts.append(f"{label} {name} id={tool_id}: {_result_text(block.get('content'))}")
                else:
                    raise ValueError("Unfamiliar block type")
            return ", ".join(dict.fromkeys(kinds)), "\n".join(parts), calls
        if kind == "result":
            subtype = data.get("subtype")
            failed = data.get("is_error", False)
            if not _identifier(subtype) or type(failed) is not bool:
                raise ValueError("Unfamiliar runtime result")
            text = _result_text(data.get("result", ""))
            errors = data.get("errors", [])
            if not isinstance(errors, list) or not all(isinstance(error, str) for error in errors):
                raise ValueError("Unfamiliar runtime errors")
            label = "runtime ERROR" if failed or subtype.startswith("error") else "runtime result"
            return label, f"{label} ({subtype}): {text}" + ("\n" + "\n".join(errors) if errors else ""), []
        if kind == "error":
            value = data.get("error", data.get("message"))
            if isinstance(value, dict):
                value = value.get("message")
            if not isinstance(value, str):
                raise ValueError("Unfamiliar runtime error")
            return "runtime ERROR", f"runtime ERROR: {value}", []
        raise ValueError("Unknown record type")


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")

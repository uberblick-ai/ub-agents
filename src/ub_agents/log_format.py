"""Internal, display-only Claude projections. No workflow/report authority.

Based on the accepted #111 Claude adapter and standard-library pretty-printer.
Other runtimes and incomplete/non-JSON fragments deliberately remain raw text,
apart from the compact marker for a cut-off first record.
"""

from collections import OrderedDict
from dataclasses import dataclass, replace
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
class Call:
    tool_id: str
    start: int
    end: int
    elapsed: str = ""


@dataclass(frozen=True)
class Entry:
    kind: str
    text: str
    raw: str
    event: str | None = None
    capture: str | None = None
    shortened: bool = False
    compact: bool = False
    styles: tuple = ()
    calls: tuple[Call, ...] = ()
    progress: tuple = ()

    def display(self, raw=False):
        if self.compact and not raw:
            return self.text
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


def skipped_entry(raw, capture, kind):
    original = raw_entry(raw, capture, kind)
    text = " " * 10 + "· earlier output skipped · h older"
    return replace(original, text=text, compact=True, styles=((10, len(text), "dim"),))


def _identifier(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_TEXT


def _result_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(block, dict) and
            block.get("type") == "text" and isinstance(block.get("text"), str) for block in value):
        return "\n".join(block["text"] for block in value)
    raise ValueError("Unfamiliar tool result")


@dataclass(frozen=True)
class Line:
    text: str
    style: str = ""
    spans: tuple = ()
    continuation: bool = False
    tool_id: str | None = None


def _one_line(value, limit=160):
    """Escape before cutting; only LF is an intentional line boundary."""
    first = inert(value.split("\n", 1)[0])
    if "\n" in value or len(first) > limit:
        return first[:limit - 1] + "…"
    return first


def _line_count(value):
    return value.count("\n") + (1 if value and not value.endswith("\n") else 0)


def _time_column(event, capture):
    stamp = event or event_time(capture)
    if stamp:
        try:
            local = datetime.fromisoformat(stamp).astimezone().strftime("%H:%M:%S")
            return (" " if event else "~") + local + " "
        except (ValueError, OverflowError):
            pass
    return " " * 10


def _compact_entry(kind, lines, raw, capture, event):
    prefix = _time_column(event, capture)
    parts, styles, calls, offset = [], [], [], 0
    for line in lines:
        # Assistant LF survives; every other runtime control stays inert.
        safe = inert(line.text)
        column = " " * len(prefix) if line.continuation else prefix
        value = column + safe
        parts.append(value)
        if line.style:
            styles.append((offset + len(prefix), offset + len(value), line.style))
        for start, end, style in line.spans:
            styles.append((offset + len(prefix) + start, offset + len(prefix) + end, style))
        if line.tool_id:
            calls.append(Call(line.tool_id, offset + len(prefix), offset + len(value)))
        offset += len(value) + 1
    full = "\n".join(parts)
    text, styles, calls, cut = _bound_compact(full, styles, calls)
    original = entry(kind, "", raw, capture, event)
    return Entry(kind, text, original.raw, event, capture,
                 cut or original.shortened, True, styles, calls)


def _bound_compact(full, styles, calls):
    cut = len(full) > MAX_TEXT
    # Keep styles in the retained head and tail of the bounded projection.
    if cut:
        room = MAX_TEXT - len(SHORTENED)
        head = (room + 1) // 2
        tail = len(full) - (room - head)
        retained = []
        for start, end, style in styles:
            if start < head:
                retained.append((start, min(end, head), style))
            if end > tail:
                retained.append((max(start, tail) - tail + head + len(SHORTENED),
                                 end - tail + head + len(SHORTENED), style))
        styles = retained
        calls = [call if call.end <= head else replace(
                     call, start=call.start - tail + head + len(SHORTENED),
                     end=call.end - tail + head + len(SHORTENED))
                 for call in calls if call.end <= head or call.start >= tail]
    return shorten(full), tuple(styles), tuple(calls), cut


def with_elapsed(value, tool_id, elapsed):
    """Replace a retained call's suffix without mutating an older snapshot/page."""
    for call in reversed(value.calls):
        if call.tool_id != tool_id:
            continue
        start = call.end - len(call.elapsed)
        suffix = " · " + elapsed
        full = value.text[:start] + suffix + value.text[call.end:]
        shift = len(suffix) - len(call.elapsed)
        styles = [(a, b, style) if b <= start else (a + shift, b + shift, style)
                  for a, b, style in value.styles if b <= start or a >= call.end]
        styles.append((start, start + len(suffix), "dim"))
        calls = [replace(other, end=other.end + shift, elapsed=suffix) if other is call else
                 replace(other, start=other.start + shift, end=other.end + shift) if other.start >= call.end else other
                 for other in value.calls]
        text, styles, calls, cut = _bound_compact(full, styles, calls)
        value = replace(value, text=text, styles=styles, calls=calls, shortened=value.shortened or cut)
    return value


def _progress(data):
    if data.get("type") != "tool_progress" or not _identifier(data.get("tool_use_id")):
        return ()
    seconds = data.get("elapsed_time_seconds")
    if type(seconds) not in (int, float) or seconds < 0:
        return ()
    try:
        seconds = int(seconds)
        elapsed = f"{seconds}s" if seconds < 60 else f"{seconds // 60}m"
    except (ValueError, OverflowError):
        return ()
    return (data["tool_use_id"], elapsed) if len(elapsed) <= 32 else ()


class ClaudeFormatter:
    """A bounded tool-id/name cache pairs results even across updates."""

    def __init__(self):
        self.tools = OrderedDict()
        self.last_call = None

    def reset(self):
        self.tools.clear()
        self.last_call = None

    def decode(self, raw, capture=None):
        if len(raw) > MAX_RECORD:
            self.last_call = None
            return raw_entry(raw, capture, "oversized raw fragment (>128 KiB)")
        try:
            data = json.loads(raw, parse_constant=_invalid_constant)
            if not isinstance(data, dict):
                raise ValueError("Not a record")
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            self.last_call = None
            return raw_entry(raw, capture)
        try:
            kind, lines, calls, last_call = self._project(data)
        except (ValueError, TypeError, RecursionError, OverflowError):
            # A complete JSON object always has a compact fallback, even when a
            # known record has an unfamiliar shape. Never print its JSON here.
            kind, lines, calls, last_call = "other", [self._label(data)], [], None
        # Validate the entire record before changing pairing state.
        for tool_id, name in calls:
            self.tools[tool_id] = name
            self.tools.move_to_end(tool_id)
            if len(self.tools) > MAX_TOOLS:
                self.tools.popitem(last=False)
        self.last_call = last_call
        value = _compact_entry(kind, lines, raw.decode("utf-8", errors="replace"), capture,
                               event_time(data.get("timestamp")))
        return replace(value, progress=_progress(data))

    @staticmethod
    def _label(data):
        kind = data.get("type")
        label = _one_line(kind) if isinstance(kind, str) else "unknown record"
        subtype = data.get("subtype")
        if isinstance(subtype, str):
            label += " · " + _one_line(subtype)
        return Line("· " + label, "dim")

    def _project(self, data):
        kind = data.get("type")
        if kind == "tool_progress":
            # Preserve the previous raw projection's generic record label.
            return "other", [], [], self.last_call
        if kind in ("system", "rate_limit_event"):
            return kind, [], [], self.last_call
        if kind in ("assistant", "user"):
            message = data.get("message")
            if not isinstance(message, dict) or message.get("role", kind) != kind:
                raise ValueError("Unfamiliar message")
            content = message.get("content")
            if not isinstance(content, list) or not content:
                raise ValueError("Unfamiliar content")
            parts, kinds, calls = [], [], []
            names = dict(self.tools)
            last_call = self.last_call
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("Unfamiliar block")
                block_type = block.get("type")
                if block_type == "text" and isinstance(block.get("text"), str):
                    kinds.append(kind)
                    parts.extend(Line(value, "dim italic" if kind == "assistant" else "italic",
                                      continuation=index > 0)
                                 for index, value in enumerate(block['text'].split("\n")))
                    last_call = None
                elif block_type == "tool_use" and kind == "assistant":
                    tool_id, name, inputs = block.get("id"), block.get("name"), block.get("input")
                    if not _identifier(tool_id) or not _identifier(name) or not isinstance(inputs, dict):
                        raise ValueError("Unfamiliar tool call")
                    calls.append((tool_id, name))
                    names[tool_id] = name
                    kinds.append("tool call")
                    key = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
                           "Bash": "command", "Grep": "pattern", "Glob": "pattern"}.get(name)
                    argument = inputs.get(key) if key else next(
                        (value for value in inputs.values() if isinstance(value, str)), None)
                    text = "▸ " + _one_line(name)
                    if isinstance(argument, str):
                        text += " " + _one_line(argument)
                    spans = []
                    fields = (("new_string", "+", "diff-add"), ("old_string", "-", "diff-remove")) if name == "Edit" else (
                        (("content", "+", "diff-add"),) if name == "Write" else ())
                    for field, sign, style in fields:
                        if isinstance(inputs.get(field), str):
                            start = len(text) + 1
                            text += f" {sign}{_line_count(inputs[field])}"
                            spans.append((start, len(text), style))
                    parts.append(Line(text, spans=tuple(spans), tool_id=tool_id))
                    last_call = tool_id
                elif block_type == "tool_result" and kind == "user":
                    tool_id = block.get("tool_use_id")
                    failed = block.get("is_error", False)
                    if not _identifier(tool_id) or type(failed) is not bool:
                        raise ValueError("Unfamiliar tool result")
                    label = "tool ERROR" if failed else "tool result"
                    name = names.get(tool_id, "tool")
                    kinds.append(label)
                    if failed:
                        error = _result_text(block.get('content'))
                        name = _one_line(name) + ": " if last_call != tool_id else ""
                        parts.append(Line("  ✗ " + name + _one_line(error.split("\n", 1)[0]), "error"))
                        last_call = None
                elif block_type == "thinking":
                    kinds.append("thinking")
                    parts.append(Line("· thinking", "dim"))
                    last_call = None
                else:
                    kinds.append("other block")
                    label = _one_line(block_type) if isinstance(block_type, str) else "unknown block"
                    parts.append(Line("· " + label, "dim"))
                    last_call = None
            return ", ".join(dict.fromkeys(kinds)), parts, calls, last_call
        if kind == "result":
            subtype = data.get("subtype")
            failed = data.get("is_error", False)
            if not _identifier(subtype) or type(failed) is not bool:
                raise ValueError("Unfamiliar runtime result")
            errors = data.get("errors", [])
            if not isinstance(errors, list) or not all(isinstance(error, str) for error in errors):
                raise ValueError("Unfamiliar runtime errors")
            label = "runtime ERROR" if failed or subtype.startswith("error") else "runtime result"
            if label == "runtime result":
                return label, [Line("✓ run finished")], [], None
            detail = _one_line(errors[0].split("\n", 1)[0]) if errors else ""
            return label, [Line("✗ " + _one_line(subtype) + (": " + detail if detail else ""), "error")], [], None
        if kind == "error":
            value = data.get("error", data.get("message"))
            if isinstance(value, dict):
                value = value.get("message")
            if not isinstance(value, str):
                raise ValueError("Unfamiliar runtime error")
            return "runtime ERROR", [Line("✗ " + _one_line(value.split("\n", 1)[0]), "error")], [], None
        return "other", [self._label(data)], [], None


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")

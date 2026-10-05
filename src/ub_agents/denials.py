"""Optional, display-only permission denials from completed Claude output."""

import json

MAX_DENIALS = 10
MAX_COMMAND = 200


def collect_denials(cli, path):
    if cli != "claude":
        return {}
    result = {}
    try:
        with path.open("rb") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "result":
                    continue
                denials = event.get("permission_denials", [])
                entries = []
                if isinstance(denials, list):
                    for denial in denials:
                        if (not isinstance(denial, dict) or not isinstance(denial.get("tool_name"), str)
                                or not denial["tool_name"]):
                            continue
                        tool, tool_input = denial["tool_name"], denial.get("tool_input", {})
                        command = tool_input.get("command") if tool == "Bash" and isinstance(tool_input, dict) else None
                        if not isinstance(command, str) and isinstance(tool_input, dict):
                            command = tool_input.get("file_path")
                        if not isinstance(command, str):
                            command = json.dumps(tool_input, separators=(",", ":"), ensure_ascii=False)
                        entries.append({"tool": tool, "command": command[:MAX_COMMAND]})
                result = {"denials": entries[:MAX_DENIALS]}
                if len(entries) > MAX_DENIALS:
                    result["denials_omitted"] = len(entries) - MAX_DENIALS
    except OSError:
        pass  # Missing diagnostics cannot change the supervised verdict.
    return result


def denial_fields(record):
    """Ignore malformed optional values without rejecting their outcome."""
    denials = record.get("denials")
    if (not isinstance(denials, list) or any(
            not isinstance(entry, dict) or not isinstance(entry.get("tool"), str)
            or not isinstance(entry.get("command"), str) for entry in denials)):
        return {}
    result = {"denials": denials}
    omitted = record.get("denials_omitted")
    if type(omitted) is int and omitted > 0:
        result["denials_omitted"] = omitted
    return result


def denial_count(record):
    fields = denial_fields(record)
    return len(fields.get("denials", [])) + fields.get("denials_omitted", 0)

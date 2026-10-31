"""Insert optional setup entries without serializing an existing YAML file."""

from copy import deepcopy
import json

import yaml
from yaml.nodes import MappingNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken

RULE = "Bash({report_command} retrospective *)"


class UnsafeEdit(Exception):
    """The original text cannot be extended without risking unrelated settings."""


def permission_paths(data, agents):
    """Find the actual Claude argument lists, including shared defaults."""
    paths = []
    for name in agents:
        role = data["agents"][name]
        path = ("agents", name, "runtime-args") if "runtime-args" in role else ("runtime-args",)
        value = data
        for key in path:
            value = value.get(key, {})
        if isinstance(value, dict):
            path += ("claude",)
            value = value.get("claude", [])
        if "--allowedTools" in value and path not in paths:
            paths.append(path)
    return paths


def node_at(node, path):
    for key in path:
        if not isinstance(node, MappingNode):
            raise UnsafeEdit("a required entry is not a YAML mapping")
        node = next((value for name, value in node.value if name.value == key), None)
        if node is None:
            raise UnsafeEdit("a required YAML entry cannot be located")
    return node


def permission_insertion(content, node, index, newline):
    flag = node.value[index]
    if node.flow_style:
        return flag.end_mark.index, ", " + json.dumps(RULE)
    mark = flag.start_mark
    prefix = content[content.rfind("\n", 0, mark.index) + 1:mark.index]
    if prefix.strip() != "-" or mark.line != flag.end_mark.line:
        raise UnsafeEdit("the Claude argument list needs a manual edit")
    end_line = content.find("\n", flag.end_mark.index)
    if end_line < 0:
        return len(content), newline + " " * (mark.column - 2) + "- " + json.dumps(RULE)
    return end_line + 1, " " * (mark.column - 2) + "- " + json.dumps(RULE) + newline


def permission_additions(content, tree, expected, claude_agents, newline, edits):
    for path in permission_paths(expected, claude_agents):
        node = node_at(tree, path)
        if not isinstance(node, SequenceNode):
            raise UnsafeEdit("Claude arguments are not a YAML sequence")
        value = expected
        for key in path:
            value = value[key]
        # Each --allowedTools consumes arguments until the next option.
        for index in reversed(range(len(value))):
            if value[index] != "--allowedTools":
                continue
            end = next((n for n in range(index + 1, len(value)) if value[n].startswith("--")), len(value))
            if RULE in value[index + 1:end]:
                continue
            edits.append(permission_insertion(content, node, index, newline))
            value.insert(index + 1, RULE)


def additions(content, boards, priorities, claude_agents):
    """Return text containing only insertions, or refuse the entire edit."""
    if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(content)):
        raise UnsafeEdit("YAML anchors or aliases require a manual edit")
    tree = yaml.compose(content)
    expected = deepcopy(yaml.safe_load(content))
    edits = []
    newline = "\r\n" if "\r\n" in content else "\n"

    def add_mapping(path, lines):
        node = node_at(tree, path)
        if not isinstance(node, MappingNode) or node.flow_style or not node.value:
            raise UnsafeEdit("flow or empty mappings require a manual edit")
        mark = node.start_mark
        prefix = content[content.rfind("\n", 0, mark.index) + 1:mark.index]
        if prefix.strip():
            raise UnsafeEdit("a mapping starts on the same line as its parent")
        text = (newline + prefix).join(lines) + newline + prefix
        edits.append((mark.index, text))

    for name, number in boards.items():
        add_mapping(("agents", name), [f"retrospectives: {number}"])
        expected["agents"][name]["retrospectives"] = number
    if priorities:
        labels = "labels: " + json.dumps(list(priorities), ensure_ascii=False)
        if "queue" in expected:
            add_mapping(("queue",), ["priority:", "  " + labels])
            expected["queue"]["priority"] = {"labels": list(priorities)}
        else:
            add_mapping((), ["queue:", "  priority:", "    " + labels])
            expected["queue"] = {"priority": {"labels": list(priorities)}}
    permission_additions(content, tree, expected, claude_agents, newline, edits)
    result = content
    for index, text in sorted(edits, key=lambda edit: edit[0], reverse=True):
        result = result[:index] + text + result[index:]
    if yaml.safe_load(result) != expected:
        raise UnsafeEdit("the proposed insertions would change other YAML settings")
    return result

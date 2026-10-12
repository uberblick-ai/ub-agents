"""Shared inert Markdown parsing for Issue rendering and section extraction."""

import re

from markdown_it import MarkdownIt


def description_parser():
    # Show links, images and HTML as source text, without interactive targets.
    # Entities stay literal so parsing cannot introduce escaped control characters.
    parser = MarkdownIt('commonmark', {'html': False}).disable(
        ['link', 'autolink', 'image', 'reference', 'entity']).enable(['table', 'strikethrough'])

    def line_breaks(state):
        # Textual otherwise renders Markdown soft breaks as spaces.
        for token in state.tokens:
            for child in token.children or ():
                if child.type == 'softbreak':
                    child.type = 'hardbreak'

    parser.core.ruler.after('inline', 'description_line_breaks', line_breaks)
    return parser


def pointer_sections(body):
    """Remove the first Pointers heading and its section, preserving source text."""
    tokens = description_parser().parse(body)
    # Match markdown-it's LF line numbers without splitting Unicode separators.
    lines = re.split(r'(?<=\n)', body)
    start = level = None
    end = len(lines)
    for index, token in enumerate(tokens):
        if token.type != 'heading_open':
            continue
        heading_level = int(token.tag[1:])
        if start is not None:
            if heading_level <= level:
                end = token.map[0]
                break
        else:
            heading = tokens[index + 1]
            label = ''.join(child.content for child in heading.children or ()
                            if child.type in {'text', 'code_inline'})
            if label.strip().casefold() == 'pointers':
                start, level = token.map[0], heading_level
    if start is None:
        return body, ''
    return ''.join(lines[:start] + lines[end:]), ''.join(lines[start:end])

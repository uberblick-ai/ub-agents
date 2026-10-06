"""Bounded JSON validation across fragments, without interpreting string contents.

Long string values keep a head and tail for display. Structure, keys and scalars
must fit the normal record budget; invalid or deeper input yields no projection.
"""

from collections import deque
import codecs
import json

from .log_format import MAX_RECORD, MAX_TEXT, SHORTENED


class ShortenedText(str):
    """A validated string whose middle was omitted before projection."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class BoundedJSON:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder('utf-8')()
        self.buffer = bytearray()
        self.head = []
        self.tail = deque(maxlen=MAX_TEXT)
        self.string = False
        self.escape = False
        self.unicode = None
        self.surrogate = None
        self.length = self.depth = self.strings = 0
        self.cuts = set()
        self.invalid = False

    def _append(self, value):
        if len(self.buffer) + len(value) > MAX_RECORD:
            raise ValueError("JSON structure exceeds record budget")
        self.buffer.extend(value)

    def _character(self, char):
        # Match json.loads' decoding of escaped UTF-16 surrogate pairs.
        if self.surrogate is not None:
            high, self.surrogate = self.surrogate, None
            if 0xdc00 <= ord(char) <= 0xdfff:
                self._retain(chr(0x10000 + ((ord(high) - 0xd800) << 10) + ord(char) - 0xdc00))
                return
            self._retain(high)
        if 0xd800 <= ord(char) <= 0xdbff:
            self.surrogate = char
        else:
            self._retain(char)

    def _retain(self, char):
        self.length += 1
        if len(self.head) < MAX_TEXT:
            self.head.append(char)
        else:
            self.tail.append(char)

    def feed(self, raw):
        if self.invalid:
            return
        try:
            for char in self.decoder.decode(raw):
                if not self.string:
                    if char == '"':
                        self.string = True
                    else:
                        if char in '{[':
                            self.depth += 1
                            if self.depth > 64:
                                raise ValueError("JSON nesting exceeds bound")
                        elif char in '}]':
                            self.depth -= 1
                        self._append(char.encode('utf-8'))
                    continue
                if self.unicode is not None:
                    if char not in '0123456789abcdefABCDEF':
                        raise ValueError("Invalid Unicode escape")
                    self.unicode += char
                    if len(self.unicode) == 4:
                        self._character(chr(int(self.unicode, 16)))
                        self.unicode = None
                elif self.escape:
                    self.escape = False
                    if char == 'u':
                        self.unicode = ''
                    else:
                        escapes = {'"': '"', '\\': '\\', '/': '/', 'b': '\b',
                                   'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}
                        self._character(escapes[char])
                elif char == '\\':
                    self.escape = True
                elif char == '"':
                    if self.surrogate is not None:
                        self._retain(self.surrogate)
                        self.surrogate = None
                    cut = self.length > MAX_TEXT * 2
                    value = ''.join(self.head) + (SHORTENED if cut else '') + ''.join(self.tail)
                    self._append(json.dumps(value, ensure_ascii=True).encode())
                    if cut:
                        self.cuts.add(self.strings)
                    self.strings += 1
                    self.head.clear()
                    self.tail.clear()
                    self.length = 0
                    self.string = False
                elif ord(char) < 32:
                    raise ValueError("Unescaped JSON control")
                else:
                    self._character(char)
        except (ValueError, UnicodeError, KeyError):
            self.invalid = True
            self.buffer.clear()
            self.head.clear()
            self.tail.clear()
            self.cuts.clear()

    def finish(self):
        if self.invalid or self.string:
            return None
        try:
            self.decoder.decode(b'', final=True)
            data = json.loads(self.buffer, object_pairs_hook=_object,
                              parse_constant=lambda value: _reject())
            if not isinstance(data, dict):
                return None
            index = 0

            def restore(value):
                nonlocal index
                if isinstance(value, str):
                    cut = index in self.cuts
                    index += 1
                    return ShortenedText(value) if cut else value
                if isinstance(value, dict):
                    result = {}
                    for key, child in value.items():
                        key = restore(key)
                        if isinstance(key, ShortenedText):
                            raise ValueError("Oversized JSON key")
                        result[key] = restore(child)
                    return result
                if isinstance(value, list):
                    return [restore(child) for child in value]
                return value

            return restore(data)
        except (ValueError, UnicodeError, RecursionError, OverflowError):
            return None


def _reject():
    raise ValueError("Invalid JSON constant")

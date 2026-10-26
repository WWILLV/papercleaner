"""Encoding validation and LaTeX comment cleaning."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from papercleaner.models import (
    CommentOptions,
    CommentResult,
    DecodedSource,
    InputError,
)

_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_BOMS = (b"\xfe\xff", b"\xff\xfe")
_UTF32_BOMS = (b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")

_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")

_RAW_ENVIRONMENTS = frozenset(
    {
        "verbatim",
        "verbatim*",
        "Verbatim",
        "Verbatim*",
        "BVerbatim",
        "BVerbatim*",
        "LVerbatim",
        "LVerbatim*",
        "lstlisting",
        "minted",
        "filecontents",
        "filecontents*",
        "alltt",
        "comment",
    }
)
_BEGIN_ENV_RE = re.compile(r"\\begin[ \t]*\{([^{}\r\n]+)\}")
_INLINE_COMMAND_RE = re.compile(
    r"\\(verb|Verb|lstinline|mintinline|url|path|nolinkurl)(\*)?(?![A-Za-z])"
)


@dataclass(frozen=True)
class RawConstruct:
    start: int
    end: int
    closed: bool
    label: str


def is_control_sequence_start(text: str, index: int) -> bool:
    """Return True when this backslash starts a control sequence."""
    if index < 0 or index >= len(text) or text[index] != "\\":
        return False
    preceding = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        preceding += 1
        cursor -= 1
    return preceding % 2 == 0


def line_number(text: str, index: int) -> int:
    return sum(1 for _ in _LINE_BREAK_RE.finditer(text, 0, index)) + 1


class LineIndex:
    """Precomputed line starts for O(log n) line lookups in one text."""

    __slots__ = ("_starts",)

    def __init__(self, text: str) -> None:
        self._starts = [0, *(match.end() for match in _LINE_BREAK_RE.finditer(text))]

    def line_number(self, index: int) -> int:
        """Return the 1-based line for ``index``.

        Equals ``line_number(text, index)`` for every index except one inside
        a ``\\r\\n`` pair. Callers only look up the start of a command, which
        is always past the preceding line break, so that offset is never used.
        """
        return bisect.bisect_right(self._starts, index)


def find_matching_group(
    text: str, start: int, open_char: str, close_char: str
) -> int | None:
    """Return the index just after a balanced group, or None when unclosed."""
    if start >= len(text) or text[start] != open_char:
        return None
    depth = 1
    cursor = start + 1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return None


def _line_end_index(text: str, start: int) -> int:
    candidates = [
        pos
        for pos in (text.find("\r", start), text.find("\n", start))
        if pos != -1
    ]
    return min(candidates) if candidates else len(text)


def _skip_horizontal_space(text: str, position: int) -> int:
    while position < len(text) and text[position] in " \t":
        position += 1
    return position


def _skip_optional_groups(text: str, position: int) -> int | None:
    position = _skip_horizontal_space(text, position)
    while position < len(text) and text[position] == "[":
        end = find_matching_group(text, position, "[", "]")
        if end is None:
            return None
        position = _skip_horizontal_space(text, end)
    return position


def _unclosed_inline(text: str, command_start: int, label: str) -> RawConstruct:
    return RawConstruct(
        command_start, _line_end_index(text, command_start), False, label
    )


def _delimited_inline(
    text: str, command_start: int, content_start: int, delimiter: str, label: str
) -> RawConstruct:
    if not delimiter or delimiter in " \t\r\n":
        return _unclosed_inline(text, command_start, label)
    if delimiter == "{":
        end = find_matching_group(text, content_start - 1, "{", "}")
        if end is None:
            return _unclosed_inline(text, command_start, label)
        return RawConstruct(command_start, end, True, label)
    line_end = _line_end_index(text, command_start)
    closing = text.find(delimiter, content_start, line_end)
    if closing == -1:
        return _unclosed_inline(text, command_start, label)
    return RawConstruct(command_start, closing + 1, True, label)


def _find_environment_end(text: str, name: str, start: int) -> int | None:
    pattern = re.compile(r"\\end[ \t]*\{" + re.escape(name) + r"\}")
    cursor = start
    while True:
        match = pattern.search(text, cursor)
        if match is None:
            return None
        if is_control_sequence_start(text, match.start()):
            return match.end()
        cursor = match.start() + 1


def find_raw_construct(text: str, index: int) -> RawConstruct | None:
    """Recognize a raw environment or inline raw command starting at index."""
    if not is_control_sequence_start(text, index):
        return None

    environment = _BEGIN_ENV_RE.match(text, index)
    if environment is not None:
        name = environment.group(1)
        if name not in _RAW_ENVIRONMENTS:
            return None
        end = _find_environment_end(text, name, environment.end())
        label = f"raw environment {name!r}"
        if end is None:
            return RawConstruct(index, len(text), False, label)
        return RawConstruct(index, end, True, label)

    command = _INLINE_COMMAND_RE.match(text, index)
    if command is None:
        return None
    name = command.group(1)
    position = command.end()

    if name in {"url", "path", "nolinkurl"}:
        position = _skip_horizontal_space(text, position)
        end = find_matching_group(text, position, "{", "}")
        label = f"\\{name} argument"
        if end is None:
            return _unclosed_inline(text, index, label)
        return RawConstruct(index, end, True, label)

    if name == "verb":
        if position >= len(text):
            return _unclosed_inline(text, index, "\\verb raw command")
        return _delimited_inline(
            text, index, position + 1, text[position], "\\verb raw command"
        )

    if name in {"Verb", "lstinline"}:
        position = _skip_optional_groups(text, position)
        label = f"\\{name} raw command"
        if position is None:
            return _unclosed_inline(text, index, label)
        position = _skip_horizontal_space(text, position)
        if position >= len(text):
            return _unclosed_inline(text, index, label)
        return _delimited_inline(text, index, position + 1, text[position], label)

    if name == "mintinline":
        label = "\\mintinline raw command"
        position = _skip_optional_groups(text, position)
        if position is None:
            return _unclosed_inline(text, index, label)
        position = _skip_horizontal_space(text, position)
        language_end = find_matching_group(text, position, "{", "}")
        if language_end is None:
            return _unclosed_inline(text, index, label)
        position = _skip_horizontal_space(text, language_end)
        if position >= len(text):
            return _unclosed_inline(text, index, label)
        return _delimited_inline(text, index, position + 1, text[position], label)

    raise AssertionError(f"unhandled raw command: {name}")


def decode_source(raw: bytes) -> DecodedSource:
    """Decode UTF-8/UTF-8-BOM/GB18030 and prove an exact byte round-trip."""
    if raw.startswith(_UTF32_BOMS):
        raise InputError("unsupported source encoding: UTF-32 byte order mark")
    if raw.startswith(_UTF16_BOMS):
        raise InputError("unsupported source encoding: UTF-16 byte order mark")

    if raw.startswith(_UTF8_BOM):
        body = raw[len(_UTF8_BOM) :]
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InputError(
                "file has a UTF-8 byte order mark but is not valid UTF-8"
            ) from exc
        return DecodedSource(text=text, encoding="utf-8", bom=True)

    for encoding in ("utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if text.encode(encoding) == raw:
            return DecodedSource(text=text, encoding=encoding, bom=False)

    raise InputError(
        "unsupported source encoding; expected UTF-8 (optionally with BOM) "
        "or GB18030"
    )


def encode_source(source: DecodedSource, text: str) -> bytes:
    """Encode cleaned text with the exact source encoding and BOM state."""
    data = text.encode(source.encoding)
    return _UTF8_BOM + data if source.bom else data


_MAGIC_COMMENT_PREFIX_RE = re.compile(
    r"[ \t]*%[ \t]*(?:!tex\b|!bib\b|!lw\b|arara:)", re.IGNORECASE
)


def _preceded_by_odd_backslashes(text: str, index: int) -> bool:
    count = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        count += 1
        cursor -= 1
    return count % 2 == 1


def _line_start(text: str, index: int) -> int:
    return max(text.rfind("\n", 0, index), text.rfind("\r", 0, index)) + 1


def _line_bounds(text: str, index: int) -> tuple[int, int]:
    cursor = index
    while cursor < len(text) and text[cursor] not in "\r\n":
        cursor += 1
    content_end = cursor
    if cursor >= len(text):
        return content_end, content_end
    if text[cursor] == "\r" and cursor + 1 < len(text) and text[cursor + 1] == "\n":
        return content_end, cursor + 2
    return content_end, cursor + 1


def _is_magic_comment(line: str) -> bool:
    return _MAGIC_COMMENT_PREFIX_RE.match(line) is not None


def _apply_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def clean_comments(
    text: str, options: CommentOptions | None = None
) -> CommentResult:
    """Remove LaTeX comments while preserving protected raw constructs."""
    opts = options or CommentOptions()
    spans: list[tuple[int, int]] = []
    magic_lines: list[tuple[int, str]] = []
    warnings: list[str] = []
    comments_removed = 0
    magic_kept = 0
    cursor = 0
    lines = LineIndex(text)

    while cursor < len(text):
        char = text[cursor]

        if char == "%" and not _preceded_by_odd_backslashes(text, cursor):
            line_start = _line_start(text, cursor)
            content_end, eol_end = _line_bounds(text, cursor)
            prefix = text[line_start:cursor]
            whole_line = prefix.strip(" \t") == ""

            if whole_line and _is_magic_comment(text[line_start:content_end]):
                magic_lines.append(
                    (lines.line_number(line_start), text[line_start:content_end])
                )
                if not opts.remove_all:
                    magic_kept += 1
                    cursor = eol_end
                    continue

            if whole_line:
                spans.append((line_start, eol_end))
                comments_removed += 1
                cursor = eol_end
                continue

            if opts.keep_eol:
                cursor = content_end
                continue

            start = cursor
            if not opts.keep_ws:
                while start > line_start and text[start - 1] in " \t":
                    start -= 1
            spans.append((start, content_end))
            comments_removed += 1
            cursor = content_end
            continue

        if char == "\\" and is_control_sequence_start(text, cursor):
            raw = find_raw_construct(text, cursor)
            if raw is not None:
                if not raw.closed:
                    warnings.append(
                        f"unclosed {raw.label} at line "
                        f"{lines.line_number(raw.start)}"
                    )
                cursor = max(raw.end, cursor + 1)
                continue

        cursor += 1

    return CommentResult(
        text=_apply_spans(text, spans),
        comments_removed=comments_removed,
        magic_comments_kept=magic_kept,
        magic_comments=tuple(magic_lines),
        warnings=tuple(dict.fromkeys(warnings)),
    )

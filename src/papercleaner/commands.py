"""Conservative static parser for supported LaTeX file references."""

from __future__ import annotations

import re

from papercleaner.comments import (
    LineIndex,
    find_matching_group,
    find_raw_construct,
    is_control_sequence_start,
)
from papercleaner.models import (
    FileReference,
    ParseIssue,
    ParsedSource,
    RefKind,
)

_COMMAND_RE = re.compile(r"\\([A-Za-z]+)(\*)?")
_SUBFIX_RE = re.compile(r"\\subfix(?![A-Za-z])")
_REMOTE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_MAX_SUBFIX_DEPTH = 100

_SINGLE_PATH: dict[str, tuple[RefKind, bool]] = {
    "input": (RefKind.TEX, True),
    "include": (RefKind.TEX, True),
    "subfile": (RefKind.TEX, True),
    "includegraphics": (RefKind.GRAPHIC, True),
    "includesvg": (RefKind.SVG, True),
    "includepdf": (RefKind.PDF, True),
    "bibliographystyle": (RefKind.BIBSTYLE, False),
    "lstinputlisting": (RefKind.EXACT, True),
    "verbatiminput": (RefKind.EXACT, True),
    "VerbatimInput": (RefKind.EXACT, True),
    "BVerbatimInput": (RefKind.EXACT, True),
    "LVerbatimInput": (RefKind.EXACT, True),
}
_LIST_PATH: dict[str, tuple[RefKind, bool]] = {
    "bibliography": (RefKind.BIB, True),
    "usepackage": (RefKind.STY, False),
    "RequirePackage": (RefKind.STY, False),
}
_IMPORT_COMMANDS = frozenset(
    {
        "import",
        "subimport",
        "includefrom",
        "subincludefrom",
        "inputfrom",
        "subinputfrom",
    }
)
_SPECIAL_COMMANDS = frozenset(
    {
        "graphicspath",
        "DeclareGraphicsExtensions",
        "documentclass",
        "addbibresource",
        "inputminted",
    }
)
_HANDLED = frozenset(_SINGLE_PATH) | frozenset(_LIST_PATH) | _IMPORT_COMMANDS | _SPECIAL_COMMANDS


class _ParseFailure(Exception):
    """Raised when a recognized command cannot be parsed statically."""


class _UnclosedGroupFailure(_ParseFailure):
    """Raised when an optional or required argument group is unclosed."""


def _skip_ws(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _read_group(
    text: str, position: int, unclosed: list[int] | None = None
) -> tuple[str, int]:
    position = _skip_ws(text, position)
    if position >= len(text) or text[position] != "{":
        raise _ParseFailure("missing required argument")
    end = find_matching_group(text, position, "{", "}")
    if end is None:
        if unclosed is not None:
            unclosed.append(position)
        raise _UnclosedGroupFailure("unclosed required argument")
    return text[position + 1 : end - 1], end


def _read_optionals(
    text: str, position: int, unclosed: list[int] | None = None
) -> tuple[tuple[str, ...], int]:
    values: list[str] = []
    position = _skip_ws(text, position)
    while position < len(text) and text[position] == "[":
        end = find_matching_group(text, position, "[", "]")
        if end is None:
            if unclosed is not None:
                unclosed.append(position)
            raise _UnclosedGroupFailure("unclosed optional argument")
        values.append(text[position + 1 : end - 1])
        position = _skip_ws(text, end)
    return tuple(values), position


def _split_list(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _static_path(raw: str, depth: int = 0) -> str | None:
    value = raw.strip()
    if not value or "\n" in value or "\r" in value:
        return None

    subfix = _SUBFIX_RE.match(value)
    if subfix is not None:
        # A runaway nesting depth is reported as a dynamic path so the parser
        # fails closed instead of raising RecursionError.
        if depth >= _MAX_SUBFIX_DEPTH:
            return None
        position = _skip_ws(value, subfix.end())
        if position >= len(value) or value[position] != "{":
            return None
        end = find_matching_group(value, position, "{", "}")
        if end is None or _skip_ws(value, end) != len(value):
            return None
        return _static_path(value[position + 1 : end - 1], depth + 1)

    if any(char in value for char in "\\#{}$%"):
        return None
    return value


def _parse_graphicspath(raw: str) -> tuple[str, ...]:
    entries: list[str] = []
    position = 0
    while position < len(raw):
        position = _skip_ws(raw, position)
        if position >= len(raw):
            break
        if raw[position] != "{":
            raise _ParseFailure("dynamic \\graphicspath entry")
        end = find_matching_group(raw, position, "{", "}")
        if end is None:
            raise _ParseFailure("unclosed \\graphicspath entry")
        entry = _static_path(raw[position + 1 : end - 1])
        if entry is None:
            raise _ParseFailure("dynamic \\graphicspath entry")
        entries.append(entry)
        position = end
    if not entries:
        raise _ParseFailure("empty \\graphicspath")
    return tuple(entries)


def _parse_graphics_extensions(raw: str) -> tuple[str, ...]:
    entries: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if _static_path(token) is None:
            raise _ParseFailure("dynamic \\DeclareGraphicsExtensions entry")
        if (
            not token.startswith(".")
            or "/" in token
            or "\\" in token
            or any(char.isspace() for char in token)
        ):
            raise _ParseFailure("invalid graphics extension")
        entries.append(token)
    if not entries:
        raise _ParseFailure("empty \\DeclareGraphicsExtensions")
    return tuple(entries)


def _add_reference(
    references: list[FileReference],
    command: str,
    raw: str,
    kind: RefKind,
    required: bool,
    line: int,
) -> None:
    value = _static_path(raw)
    if value is None:
        raise _ParseFailure(f"dynamic path in \\{command}")
    references.append(
        FileReference(
            command=command,
            argument=value,
            kind=kind,
            required=required,
            line=line,
        )
    )


def _handle_command(
    name: str,
    text: str,
    position: int,
    line: int,
    references: list[FileReference],
    graphics_dirs: list[str],
    graphics_extensions: list[str],
    unclosed: list[int],
) -> int:
    if name == "graphicspath":
        _, position = _read_optionals(text, position, unclosed)
        raw, position = _read_group(text, position, unclosed)
        graphics_dirs.extend(_parse_graphicspath(raw))
        return position

    if name == "DeclareGraphicsExtensions":
        _, position = _read_optionals(text, position, unclosed)
        raw, position = _read_group(text, position, unclosed)
        graphics_extensions.extend(_parse_graphics_extensions(raw))
        return position

    if name in _IMPORT_COMMANDS:
        directory, position = _read_group(text, position, unclosed)
        filename, position = _read_group(text, position, unclosed)
        directory_value = _static_path(directory)
        filename_value = _static_path(filename)
        if directory_value is None or filename_value is None:
            raise _ParseFailure(f"dynamic path in \\{name}")
        argument = (
            f"{directory_value.rstrip('/')}/{filename_value}"
            if directory_value
            else filename_value
        )
        references.append(
            FileReference(name, argument, RefKind.TEX, True, line)
        )
        return position

    if name == "inputminted":
        _, position = _read_optionals(text, position, unclosed)
        _, position = _read_group(text, position, unclosed)
        raw, position = _read_group(text, position, unclosed)
        _add_reference(references, name, raw, RefKind.EXACT, True, line)
        return position

    if name == "documentclass":
        _, position = _read_optionals(text, position, unclosed)
        raw, position = _read_group(text, position, unclosed)
        _add_reference(references, name, raw, RefKind.CLS, False, line)
        return position

    if name == "addbibresource":
        _, position = _read_optionals(text, position, unclosed)
        raw, position = _read_group(text, position, unclosed)
        value = raw.strip()
        if _REMOTE_RE.match(value):
            return position
        _add_reference(references, name, value, RefKind.BIB, True, line)
        return position

    if name in _LIST_PATH:
        _, position = _read_optionals(text, position, unclosed)
        raw, position = _read_group(text, position, unclosed)
        kind, required = _LIST_PATH[name]
        entries = _split_list(raw)
        if not entries:
            raise _ParseFailure(f"empty path list in \\{name}")
        for entry in entries:
            _add_reference(references, name, entry, kind, required, line)
        return position

    if name in _SINGLE_PATH:
        _, position = _read_optionals(text, position, unclosed)
        raw, position = _read_group(text, position, unclosed)
        kind, required = _SINGLE_PATH[name]
        _add_reference(references, name, raw, kind, required, line)
        return position

    raise AssertionError(f"unhandled parser command: {name}")


def parse_source(text: str) -> ParsedSource:
    """Parse supported references while ignoring raw LaTeX constructs."""
    references: list[FileReference] = []
    graphics_dirs: list[str] = []
    graphics_extensions: list[str] = []
    issues: list[ParseIssue] = []
    has_documentclass = False
    cursor = 0
    # One line index per source keeps per-command line lookups O(log n)
    # instead of rescanning the text from offset zero for every command.
    lines = LineIndex(text)

    while cursor < len(text):
        if text[cursor] != "\\" or not is_control_sequence_start(text, cursor):
            cursor += 1
            continue

        raw = find_raw_construct(text, cursor)
        if raw is not None:
            if not raw.closed:
                issues.append(
                    ParseIssue(
                        lines.line_number(cursor),
                        f"unclosed {raw.label}",
                    )
                )
            cursor = max(raw.end, cursor + 1)
            continue

        match = _COMMAND_RE.match(text, cursor)
        if match is None:
            cursor += 1
            continue

        name = match.group(1)
        if name not in _HANDLED:
            cursor = match.end()
            continue

        line = lines.line_number(cursor)
        if name == "documentclass":
            has_documentclass = True

        unclosed: list[int] = []
        try:
            cursor = _handle_command(
                name,
                text,
                match.end(),
                line,
                references,
                graphics_dirs,
                graphics_extensions,
                unclosed,
            )
        except _ParseFailure as exc:
            issues.append(ParseIssue(line, f"{exc} in \\{name}"))
            if unclosed:
                # The rest of this source is unsafe and dependency cleanup
                # will be skipped, so do not rescan it: a failed scan of an
                # unclosed group always reaches end of input, and repeating
                # it for later constructs would be quadratic.
                break
            cursor = max(match.end(), cursor + 1)

    return ParsedSource(
        references=tuple(references),
        graphics_dirs=tuple(sorted(set(graphics_dirs))),
        graphics_extensions=tuple(sorted(set(graphics_extensions))),
        has_documentclass=has_documentclass,
        issues=tuple(issues),
    )

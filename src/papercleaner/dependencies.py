"""Static root discovery and conservative dependency graph construction."""

from __future__ import annotations

import os
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from papercleaner.comments import clean_comments, decode_source
from papercleaner.commands import parse_source
from papercleaner.models import (
    CommentOptions,
    DependencyPlan,
    FileReference,
    InputError,
    ParsedSource,
    RefKind,
    TexFileResult,
    Workspace,
)

DEFAULT_GRAPHICS_EXTENSIONS = (
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".eps",
    ".ps",
    ".svg",
)
_SOURCE_SUFFIXES = frozenset({".tex", ".sty", ".cls"})
_SOURCE_REF_KINDS = frozenset({RefKind.TEX, RefKind.STY, RefKind.CLS})
_IMAGE_REF_KINDS = frozenset({RefKind.GRAPHIC, RefKind.SVG, RefKind.PDF})
_MAGIC_ROOT_RE = re.compile(
    r"^[ \t]*%[ \t]*![ \t]*tex[ \t]+root[ \t]*=[ \t]*(.*?)[ \t]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DependencyGraph:
    roots: tuple[PurePosixPath, ...]
    reachable: frozenset[PurePosixPath]
    issues: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _Resolution:
    files: tuple[PurePosixPath, ...]
    issues: tuple[str, ...]
    warnings: tuple[str, ...]


def _canonical(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within(child: Path, parent: Path) -> bool:
    child_key = _canonical(child)
    parent_key = _canonical(parent)
    try:
        return os.path.commonpath([child_key, parent_key]) == parent_key
    except ValueError:
        return False


def _is_absolute_argument(value: str) -> bool:
    return value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value) is not None


def _has_extension(value: str) -> bool:
    stripped = value.rstrip("/")
    return bool(PurePosixPath(stripped).suffix) if stripped else False


def _path_variants(
    reference: FileReference, graphics_extensions: tuple[str, ...]
) -> tuple[str, ...]:
    argument = reference.argument
    variants = [argument]
    if argument.endswith("/"):
        return tuple(dict.fromkeys(variants))
    lower = argument.lower()
    if reference.kind == RefKind.TEX:
        if not _has_extension(argument):
            variants.append(argument + ".tex")
    elif reference.kind == RefKind.GRAPHIC:
        if not _has_extension(argument):
            suffixes = graphics_extensions or DEFAULT_GRAPHICS_EXTENSIONS
            variants.extend(argument + suffix for suffix in suffixes)
    elif reference.kind == RefKind.SVG:
        if not lower.endswith(".svg"):
            variants.append(argument + ".svg")
    elif reference.kind == RefKind.PDF:
        if not lower.endswith(".pdf"):
            variants.append(argument + ".pdf")
    elif reference.kind == RefKind.BIB:
        if not lower.endswith(".bib"):
            variants.append(argument + ".bib")
    elif reference.kind == RefKind.BIBSTYLE:
        if not lower.endswith(".bst"):
            variants.append(argument + ".bst")
    elif reference.kind == RefKind.STY:
        if not lower.endswith(".sty"):
            variants.append(argument + ".sty")
    elif reference.kind == RefKind.CLS:
        if not lower.endswith(".cls"):
            variants.append(argument + ".cls")
    return tuple(dict.fromkeys(variants))


class _Analyzer:
    def __init__(
        self,
        workspace: Workspace,
        tex_files: Mapping[PurePosixPath, TexFileResult],
    ) -> None:
        self.workspace = workspace
        self.tex_files = tex_files
        self.file_lookup = {
            _canonical(workspace.root.joinpath(*relative.parts)): relative
            for relative in workspace.files
        }
        self.issues: set[str] = set()
        self.warnings: set[str] = set()
        self._parsed: dict[PurePosixPath, ParsedSource | None] = {}
        self._analysis: dict[PurePosixPath, str] = {
            relative: result.analysis_text
            for relative, result in tex_files.items()
        }

    def _absolute(self, relative: PurePosixPath) -> Path:
        return self.workspace.root.joinpath(*relative.parts)

    def _lookup(self, path: Path) -> tuple[PurePosixPath | None, bool]:
        absolute = Path(os.path.abspath(os.fspath(path)))
        if not _is_within(absolute, self.workspace.root):
            return None, False
        return self.file_lookup.get(_canonical(absolute)), True

    def parsed(self, relative: PurePosixPath) -> ParsedSource | None:
        if relative in self._parsed:
            return self._parsed[relative]
        if relative.suffix.lower() not in _SOURCE_SUFFIXES:
            self._parsed[relative] = None
            return None

        text = self._analysis.get(relative)
        if text is None:
            path = self._absolute(relative)
            try:
                raw = path.read_bytes()
            except OSError as exc:
                self.issues.add(
                    f"{relative.as_posix()}: cannot read dependency source: {exc}"
                )
                self._parsed[relative] = None
                return None
            try:
                decoded = decode_source(raw)
            except InputError as exc:
                self.issues.add(
                    f"{relative.as_posix()}: cannot analyze dependencies: {exc}"
                )
                self._parsed[relative] = None
                return None
            cleaned = clean_comments(
                decoded.text, CommentOptions(remove_all=True, keep_ws=True)
            )
            text = cleaned.text
            self._analysis[relative] = text

        parsed = parse_source(text)
        self._parsed[relative] = parsed
        return parsed

    def record_parsed_issues(
        self, relative: PurePosixPath, parsed: ParsedSource
    ) -> None:
        for issue in parsed.issues:
            self.issues.add(
                f"{relative.as_posix()}:{issue.line}: {issue.message}"
            )

    def _magic_target(
        self, relative: PurePosixPath, line: int, value: str
    ) -> PurePosixPath | None:
        prefix = f"{relative.as_posix()}:{line}: magic root"
        if not value:
            self.warnings.add(f"{prefix} has an empty target")
            return None
        if any(character in value for character in "\\#{}%"):
            self.warnings.add(f"{prefix} is not a static path: {value!r}")
            return None
        if _is_absolute_argument(value):
            candidate = Path(value)
        else:
            candidate = self._absolute(relative).parent / value
        target, inside = self._lookup(candidate)
        if not inside:
            self.warnings.add(f"{prefix} points outside the workspace: {value}")
            return None
        if target is None:
            self.warnings.add(f"{prefix} target does not exist: {value}")
            return None
        if target.suffix.lower() != ".tex":
            self.warnings.add(f"{prefix} target is not a .tex file: {value}")
            return None
        return target

    def discover_roots(self) -> tuple[PurePosixPath, ...]:
        roots: set[PurePosixPath] = set()
        for relative in sorted(self.tex_files):
            parsed = self.parsed(relative)
            if parsed is not None and parsed.has_documentclass:
                roots.add(relative)

        for relative, result in sorted(self.tex_files.items()):
            for line, line_text in result.magic_comments:
                match = _MAGIC_ROOT_RE.match(line_text)
                if match is None:
                    continue
                target = self._magic_target(
                    relative, line, match.group(1).strip()
                )
                if target is not None:
                    roots.add(target)
        return tuple(sorted(roots))

    def _bases(
        self, source_relative: PurePosixPath, root_relative: PurePosixPath
    ) -> list[Path]:
        candidates = [
            self._absolute(source_relative).parent,
            self._absolute(root_relative).parent,
            self.workspace.root,
        ]
        deduplicated: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = _canonical(candidate)
            if key not in seen:
                seen.add(key)
                deduplicated.append(candidate)
        return deduplicated

    def resolve(
        self,
        source_relative: PurePosixPath,
        root_relative: PurePosixPath,
        reference: FileReference,
        graphics_dirs: tuple[str, ...] = (),
        graphics_extensions: tuple[str, ...] = (),
    ) -> _Resolution:
        label = (
            f"{source_relative.as_posix()}:{reference.line}: "
            f"\\{reference.command}{{{reference.argument}}}"
        )
        if _is_absolute_argument(reference.argument):
            return _Resolution(
                files=(),
                issues=(),
                warnings=(f"{label}: ignored absolute path",),
            )

        bases = self._bases(source_relative, root_relative)
        if reference.kind in _IMAGE_REF_KINDS and graphics_dirs:
            image_bases: list[Path] = []
            for graphics_dir in graphics_dirs:
                if _is_absolute_argument(graphics_dir):
                    continue
                for base in list(bases):
                    candidate = Path(
                        os.path.normpath(os.path.join(base, graphics_dir))
                    )
                    if _is_within(candidate, self.workspace.root):
                        image_bases.append(candidate)
            bases = list(dict.fromkeys(bases + image_bases))

        matches: set[PurePosixPath] = set()
        attempted_inside = False
        outside_seen = False
        for base in bases:
            for variant in _path_variants(reference, graphics_extensions):
                candidate = Path(os.path.normpath(os.path.join(base, variant)))
                target, inside = self._lookup(candidate)
                if not inside:
                    outside_seen = True
                    continue
                attempted_inside = True
                if target is not None:
                    matches.add(target)

        issues: list[str] = []
        warnings: list[str] = []
        if not matches:
            if reference.required and attempted_inside:
                issues.append(
                    f"unresolved reference in {source_relative.as_posix()}:"
                    f"{reference.line}: \\{reference.command}"
                    f"{{{reference.argument}}}"
                )
            if outside_seen:
                warnings.append(f"{label}: ignored path outside the workspace")

        return _Resolution(
            files=tuple(sorted(matches)),
            issues=tuple(issues),
            warnings=tuple(warnings),
        )


def build_dependency_graph(
    workspace: Workspace,
    tex_files: Mapping[PurePosixPath, TexFileResult],
) -> DependencyGraph:
    analyzer = _Analyzer(workspace, tex_files)
    roots = analyzer.discover_roots()
    if not roots:
        return DependencyGraph(
            roots=(),
            reachable=frozenset(),
            issues=(),
            warnings=tuple(sorted(analyzer.warnings)),
        )

    reachable: set[PurePosixPath] = set()
    for root in roots:
        visited: set[PurePosixPath] = set()
        queue: deque[PurePosixPath] = deque([root])
        collected: list[tuple[PurePosixPath, FileReference]] = []
        graphics_dirs: set[str] = set()
        graphics_extensions: set[str] = set()

        while queue:
            relative = queue.popleft()
            if relative in visited:
                continue
            visited.add(relative)
            parsed = analyzer.parsed(relative)
            if parsed is None:
                continue
            analyzer.record_parsed_issues(relative, parsed)
            graphics_dirs.update(parsed.graphics_dirs)
            graphics_extensions.update(parsed.graphics_extensions)

            for reference in parsed.references:
                collected.append((relative, reference))
                if reference.kind not in _SOURCE_REF_KINDS:
                    continue
                resolution = analyzer.resolve(relative, root, reference)
                analyzer.issues.update(resolution.issues)
                analyzer.warnings.update(resolution.warnings)
                for found in resolution.files:
                    reachable.add(found)
                    if (
                        found.suffix.lower() in _SOURCE_SUFFIXES
                        and found not in visited
                    ):
                        queue.append(found)

        reachable.update(visited)
        root_graphics_dirs = tuple(sorted(graphics_dirs))
        root_graphics_extensions = tuple(sorted(graphics_extensions))
        for relative, reference in collected:
            if reference.kind in _SOURCE_REF_KINDS:
                continue
            resolution = analyzer.resolve(
                relative,
                root,
                reference,
                root_graphics_dirs,
                root_graphics_extensions,
            )
            analyzer.issues.update(resolution.issues)
            analyzer.warnings.update(resolution.warnings)
            reachable.update(resolution.files)

    return DependencyGraph(
        roots=roots,
        reachable=frozenset(reachable),
        issues=tuple(sorted(analyzer.issues)),
        warnings=tuple(sorted(analyzer.warnings)),
    )


DELETABLE_EXTENSIONS = frozenset(
    {
        ".tex",
        ".bib",
        ".bst",
        ".sty",
        ".cls",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".eps",
        ".ps",
        ".svg",
        ".bmp",
        ".gif",
        ".tif",
        ".tiff",
        ".webp",
    }
)
_METADATA_PREFIXES = (
    "readme",
    "license",
    "copying",
    "notice",
    "authors",
    "changelog",
    "contributing",
)
_METADATA_NAMES = frozenset(
    {
        "makefile",
        "latexmkrc",
        ".latexmkrc",
        ".gitignore",
        ".gitattributes",
        ".gitmodules",
        ".editorconfig",
    }
)


def _is_hidden(relative: PurePosixPath) -> bool:
    return any(part.startswith(".") for part in relative.parts)


def _is_metadata(relative: PurePosixPath) -> bool:
    name = relative.name.lower()
    return name in _METADATA_NAMES or name.startswith(_METADATA_PREFIXES)


def analyze_dependencies(
    workspace: Workspace,
    tex_files: Mapping[PurePosixPath, TexFileResult],
) -> DependencyPlan:
    """Compute safe deletions or explain why the deletion stage is skipped."""
    graph = build_dependency_graph(workspace, tex_files)
    skip_reasons: list[str] = []
    if not graph.roots:
        skip_reasons.append("no root documents found")
    skip_reasons.extend(graph.issues)

    protected = set(graph.reachable)
    protected.update(graph.roots)
    file_lookup = {
        _canonical(workspace.root.joinpath(*relative.parts)): relative
        for relative in workspace.files
    }
    for root in graph.roots:
        candidate = (
            workspace.root.joinpath(*root.parent.parts) / f"{root.stem}.pdf"
        )
        root_pdf = file_lookup.get(_canonical(candidate))
        if root_pdf is not None:
            protected.add(root_pdf)

    deletions: list[PurePosixPath] = []
    if not skip_reasons:
        for relative in workspace.files:
            if relative.suffix.lower() not in DELETABLE_EXTENSIONS:
                continue
            if relative in protected:
                continue
            if _is_hidden(relative):
                continue
            if _is_metadata(relative):
                continue
            deletions.append(relative)

    return DependencyPlan(
        roots=graph.roots,
        reachable=graph.reachable,
        deletions=tuple(sorted(deletions)),
        skip_reasons=tuple(sorted(dict.fromkeys(skip_reasons))),
        warnings=graph.warnings,
    )

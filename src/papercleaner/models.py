"""Shared data contracts for papercleaner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath


class PaperCleanerError(Exception):
    """Expected failure that maps directly to a process exit code."""

    exit_code = 1


class ProcessingError(PaperCleanerError):
    """Processing or output I/O failed and no output was committed."""

    exit_code = 1


class UsageError(PaperCleanerError):
    """Command-line usage is invalid."""

    exit_code = 2


class InputError(PaperCleanerError):
    """The input is invalid or cannot be read safely."""

    exit_code = 2


class OutputError(PaperCleanerError):
    """The requested output location is invalid or already exists."""

    exit_code = 2


class InputKind(str, Enum):
    DIRECTORY = "directory"
    ARCHIVE = "archive"


class ArchiveKind(str, Enum):
    ZIP = "zip"
    TAR = "tar"
    TAR_GZ = "tar.gz"
    TAR_BZ2 = "tar.bz2"
    TAR_XZ = "tar.xz"


@dataclass(frozen=True)
class InputSpec:
    path: Path
    kind: InputKind
    archive_kind: ArchiveKind | None = None
    archive_suffix: str | None = None


@dataclass(frozen=True)
class Workspace:
    root: Path
    files: tuple[PurePosixPath, ...]
    directories: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class DecodedSource:
    text: str
    encoding: str
    bom: bool


@dataclass(frozen=True)
class CommentOptions:
    keep_eol: bool = False
    keep_ws: bool = False
    remove_all: bool = False


@dataclass(frozen=True)
class CommentResult:
    text: str
    comments_removed: int
    magic_comments_kept: int
    magic_comments: tuple[tuple[int, str], ...] = ()
    warnings: tuple[str, ...] = ()


class RefKind(str, Enum):
    TEX = "tex"
    GRAPHIC = "graphic"
    SVG = "svg"
    PDF = "pdf"
    BIB = "bib"
    BIBSTYLE = "bibstyle"
    STY = "sty"
    CLS = "cls"
    EXACT = "exact"


@dataclass(frozen=True)
class ParseIssue:
    line: int
    message: str


@dataclass(frozen=True)
class FileReference:
    command: str
    argument: str
    kind: RefKind
    required: bool
    line: int


@dataclass(frozen=True)
class ParsedSource:
    references: tuple[FileReference, ...]
    graphics_dirs: tuple[str, ...]
    graphics_extensions: tuple[str, ...]
    has_documentclass: bool
    issues: tuple[ParseIssue, ...]


@dataclass(frozen=True)
class TexFileResult:
    rel: PurePosixPath
    original_text: str
    output_text: str
    output_bytes: bytes
    analysis_text: str
    comments_removed: int
    magic_comments_kept: int
    magic_comments: tuple[tuple[int, str], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class DependencyPlan:
    roots: tuple[PurePosixPath, ...]
    reachable: frozenset[PurePosixPath]
    deletions: tuple[PurePosixPath, ...]
    skip_reasons: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RunOptions:
    input_path: Path
    output_path: Path | None
    deps: bool
    all_comments: bool
    keep_eol: bool
    keep_ws: bool


@dataclass(frozen=True)
class RunReport:
    output_path: Path
    tex_files: int
    comments_removed: int
    magic_comments_kept: int
    deps_requested: bool
    deleted: tuple[PurePosixPath, ...]
    dependency_skipped_reasons: tuple[str, ...]
    warnings: tuple[str, ...]

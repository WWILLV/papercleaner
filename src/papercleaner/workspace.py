"""Filesystem input, output, archive, and atomic-write support."""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from papercleaner.models import (
    ArchiveKind,
    InputError,
    InputKind,
    InputSpec,
    OutputError,
    ProcessingError,
    Workspace,
)

_VCS_DIRECTORIES = frozenset({".git", ".hg", ".svn"})
_ARCHIVE_SUFFIXES: tuple[tuple[str, ArchiveKind], ...] = (
    (".tar.bz2", ArchiveKind.TAR_BZ2),
    (".tar.gz", ArchiveKind.TAR_GZ),
    (".tar.xz", ArchiveKind.TAR_XZ),
    (".tgz", ArchiveKind.TAR_GZ),
    (".zip", ArchiveKind.ZIP),
    (".tar", ArchiveKind.TAR),
)
_SUPPORTED_ARCHIVES = ".zip, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz"


def match_archive_suffix(name: str) -> tuple[ArchiveKind, str] | None:
    lowered = name.casefold()
    for suffix, kind in _ARCHIVE_SUFFIXES:
        if lowered.endswith(suffix):
            return kind, name[-len(suffix) :]
    return None


def detect_input(path: Path) -> InputSpec:
    candidate = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    if not os.path.lexists(candidate):
        raise InputError(f"input does not exist: {candidate}")
    if candidate.is_symlink():
        raise InputError("symbolic-link inputs are not supported")
    if candidate.is_dir():
        return InputSpec(path=candidate, kind=InputKind.DIRECTORY)
    if candidate.is_file():
        matched = match_archive_suffix(candidate.name)
        if matched is None:
            raise InputError(
                f"unsupported archive format: {candidate.name}; "
                f"supported formats: {_SUPPORTED_ARCHIVES}"
            )
        kind, suffix = matched
        return InputSpec(
            path=candidate,
            kind=InputKind.ARCHIVE,
            archive_kind=kind,
            archive_suffix=suffix,
        )
    raise InputError(f"input is not a regular file or directory: {candidate}")


def _normcase(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_within(child: Path, parent: Path) -> bool:
    child_key = _normcase(child)
    parent_key = _normcase(parent)
    try:
        return os.path.commonpath([child_key, parent_key]) == parent_key
    except ValueError:
        return False


def _target_real_path(path: Path) -> Path:
    return Path(os.path.realpath(path.parent)) / path.name


def resolve_output(spec: InputSpec, output: Path | None) -> Path:
    if output is None:
        if spec.kind == InputKind.DIRECTORY:
            if not spec.path.name:
                raise OutputError("cannot derive an output name from the input path")
            target = spec.path.parent / f"{spec.path.name}-cleaned"
        else:
            if spec.archive_suffix is None:
                raise OutputError("archive input has no recognized suffix")
            stem = spec.path.name[: -len(spec.archive_suffix)]
            target = spec.path.parent / f"{stem}-cleaned{spec.archive_suffix}"
    else:
        target = Path(os.path.abspath(os.fspath(Path(output).expanduser())))
        if spec.kind == InputKind.ARCHIVE:
            matched = match_archive_suffix(target.name)
            if matched is None:
                raise OutputError(
                    "archive output must use a supported archive suffix: "
                    f"{_SUPPORTED_ARCHIVES}"
                )
            output_kind, _ = matched
            if output_kind != spec.archive_kind:
                raise OutputError(
                    "output archive format must match the input format"
                )

    if _normcase(target) == _normcase(spec.path):
        raise OutputError("output path must differ from the input path")
    if os.path.lexists(target):
        raise OutputError(f"output path already exists: {target}")
    if os.path.lexists(target.parent) and not target.parent.is_dir():
        raise OutputError(f"output parent is not a directory: {target.parent}")
    if spec.kind == InputKind.DIRECTORY:
        input_real = Path(os.path.realpath(spec.path))
        target_real = _target_real_path(target)
        if _is_within(target_real, input_real):
            raise OutputError("output directory must be outside the input directory")
    return target


def _relative_posix(root: Path, path: Path) -> PurePosixPath:
    return PurePosixPath(path.relative_to(root).as_posix())


def _walk_directory(
    root: Path,
    current: Path,
    files: list[PurePosixPath],
    directories: list[PurePosixPath],
) -> None:
    try:
        with os.scandir(current) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        raise ProcessingError(f"cannot read directory {current}: {exc}") from exc

    for entry in entries:
        path = Path(entry.path)
        relative = _relative_posix(root, path)
        if entry.is_symlink():
            raise InputError(f"symbolic links are not supported: {relative.as_posix()}")
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError as exc:
            raise ProcessingError(f"cannot inspect {path}: {exc}") from exc
        if is_dir:
            if entry.name.casefold() in _VCS_DIRECTORIES:
                continue
            directories.append(relative)
            _walk_directory(root, path, files, directories)
        elif is_file:
            files.append(relative)
        else:
            raise InputError(
                f"special files are not supported: {relative.as_posix()}"
            )


def scan_directory(root: Path) -> Workspace:
    root = Path(os.path.abspath(os.fspath(root)))
    if not root.is_dir():
        raise InputError(f"workspace root is not a directory: {root}")
    files: list[PurePosixPath] = []
    directories: list[PurePosixPath] = []
    _walk_directory(root, root, files, directories)
    return Workspace(
        root=root,
        files=tuple(sorted(files)),
        directories=tuple(sorted(directories)),
    )


_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_WINDOWS_INVALID_COMPONENT_CHARS = frozenset('<>:"|?*')


@contextmanager
def open_workspace(spec: InputSpec) -> Iterator[Workspace]:
    if spec.kind == InputKind.DIRECTORY:
        yield scan_directory(spec.path)
        return

    with tempfile.TemporaryDirectory(prefix="papercleaner-input-") as temporary:
        root = Path(temporary)
        extract_archive(spec, root)
        yield scan_directory(root)


def extract_archive(spec: InputSpec, destination: Path) -> None:
    if spec.kind != InputKind.ARCHIVE or spec.archive_kind is None:
        raise InputError("archive extraction requires archive input")
    destination.mkdir(parents=True, exist_ok=True)
    if spec.archive_kind == ArchiveKind.ZIP:
        _extract_zip(spec.path, destination)
    else:
        _extract_tar(spec.path, destination)


def _validate_component(part: str, original_name: str) -> None:
    if os.name == "nt":
        if any(
            char in _WINDOWS_INVALID_COMPONENT_CHARS or ord(char) < 32
            for char in part
        ):
            raise InputError(
                f"archive member has an invalid Windows path: {original_name!r}"
            )
        if part.endswith((" ", ".")):
            raise InputError(
                f"archive member has a trailing space or dot: {original_name!r}"
            )
        stem = part.split(".")[0].rstrip().upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise InputError(
                f"archive member uses a reserved Windows name: {original_name!r}"
            )


def _normalize_member_name(name: str) -> PurePosixPath | None:
    if "\x00" in name:
        raise InputError("archive member name contains a NUL byte")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise InputError(f"unsafe absolute archive member path: {name!r}")
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise InputError(f"unsafe traversal in archive member path: {name!r}")
        _validate_component(part, name)
        parts.append(part)
    return PurePosixPath(*parts) if parts else None


def _path_key(relative: PurePosixPath) -> str:
    value = relative.as_posix()
    return value.casefold() if os.name == "nt" else value


def _validate_member_entries(
    entries: list[tuple[PurePosixPath, bool]], archive_path: Path
) -> None:
    seen: dict[str, bool] = {}
    for relative, is_dir in entries:
        key = _path_key(relative)
        if key in seen:
            raise InputError(
                f"duplicate archive member path in {archive_path.name}: "
                f"{relative.as_posix()}"
            )
        seen[key] = is_dir

    files = {key for key, is_dir in seen.items() if not is_dir}
    for relative, _ in entries:
        for parent in relative.parents:
            if parent == PurePosixPath("."):
                continue
            if _path_key(parent) in files:
                raise InputError(
                    "archive contains a file used as a parent directory: "
                    f"{relative.as_posix()}"
                )


def _validate_zip_member_type(info: zipfile.ZipInfo) -> None:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        raise InputError(f"archive symbolic link is not supported: {info.filename}")
    if info.is_dir():
        if file_type not in (0, stat.S_IFDIR):
            raise InputError(f"archive member is not a directory: {info.filename}")
    elif file_type not in (0, stat.S_IFREG):
        raise InputError(
            f"archive member is not a regular file: {info.filename}"
        )


def _extract_zip(path: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries: list[tuple[PurePosixPath, bool, zipfile.ZipInfo]] = []
            for info in archive.infolist():
                _validate_zip_member_type(info)
                is_dir = info.is_dir()
                relative = _normalize_member_name(info.filename)
                if relative is None:
                    if is_dir:
                        continue
                    raise InputError("archive contains an empty file name")
                entries.append((relative, is_dir, info))

            _validate_member_entries(
                [(relative, is_dir) for relative, is_dir, _ in entries],
                path,
            )

            ordered = sorted(
                entries, key=lambda item: (not item[1], item[0].as_posix())
            )
            for relative, is_dir, info in ordered:
                target = destination.joinpath(*relative.parts)
                if is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except InputError:
        raise
    except (
        zipfile.BadZipFile,
        EOFError,
        RuntimeError,
        NotImplementedError,
    ) as exc:
        raise InputError(f"invalid ZIP archive {path.name}: {exc}") from exc


def _extract_tar(path: Path, destination: Path) -> None:
    try:
        with tarfile.open(path, "r:*") as archive:
            entries: list[tuple[PurePosixPath, bool, tarfile.TarInfo]] = []
            for member in archive.getmembers():
                is_dir = member.isdir()
                is_file = member.isfile()
                if not (is_dir or is_file):
                    raise InputError(
                        f"unsupported archive member type for {member.name!r}"
                    )
                relative = _normalize_member_name(member.name)
                if relative is None:
                    if is_dir:
                        continue
                    raise InputError("archive contains an empty file name")
                entries.append((relative, is_dir, member))

            _validate_member_entries(
                [(relative, is_dir) for relative, is_dir, _ in entries],
                path,
            )

            ordered = sorted(
                entries, key=lambda item: (not item[1], item[0].as_posix())
            )
            for relative, is_dir, member in ordered:
                target = destination.joinpath(*relative.parts)
                if is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise InputError(
                        f"cannot read archive member {member.name!r}"
                    )
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except InputError:
        raise
    except (tarfile.TarError, EOFError) as exc:
        raise InputError(f"invalid tar archive {path.name}: {exc}") from exc


def _workspace_path(workspace: Workspace, relative: PurePosixPath) -> Path:
    return workspace.root.joinpath(*relative.parts)


def write_directory_output(
    workspace: Workspace,
    output_path: Path,
    overrides: Mapping[PurePosixPath, bytes],
    deleted: Collection[PurePosixPath],
) -> None:
    deleted_set = set(deleted)
    output_path = Path(os.path.abspath(os.fspath(output_path)))
    if os.path.lexists(output_path):
        raise OutputError(f"output path already exists: {output_path}")

    parent = output_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProcessingError(
            f"cannot create output parent directory {parent}: {exc}"
        ) from exc
    if not parent.is_dir():
        raise ProcessingError(f"output parent is not a directory: {parent}")

    temporary: Path | None = None
    committed = False
    try:
        temporary = Path(
            tempfile.mkdtemp(prefix=".papercleaner-", dir=str(parent))
        )
        for relative in workspace.directories:
            temporary.joinpath(*relative.parts).mkdir(
                parents=True, exist_ok=True
            )
        for relative in workspace.files:
            if relative in deleted_set:
                continue
            target = temporary.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if relative in overrides:
                target.write_bytes(overrides[relative])
            else:
                shutil.copyfile(
                    _workspace_path(workspace, relative),
                    target,
                )
        if os.path.lexists(output_path):
            raise OutputError(f"output path already exists: {output_path}")
        try:
            os.replace(temporary, output_path)
        except OSError as exc:
            raise ProcessingError(
                f"cannot commit output directory {output_path}: {exc}"
            ) from exc
        committed = True
    except OSError as exc:
        raise ProcessingError(f"cannot write output directory: {exc}") from exc
    finally:
        if not committed and temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


_TAR_MODES = {
    ArchiveKind.TAR: "w",
    ArchiveKind.TAR_GZ: "w:gz",
    ArchiveKind.TAR_BZ2: "w:bz2",
    ArchiveKind.TAR_XZ: "w:xz",
}


def _visible_files(
    workspace: Workspace, deleted: set[PurePosixPath]
) -> tuple[PurePosixPath, ...]:
    return tuple(
        relative for relative in workspace.files if relative not in deleted
    )


def _file_object(
    workspace: Workspace,
    relative: PurePosixPath,
    overrides: Mapping[PurePosixPath, bytes],
):
    data = overrides.get(relative)
    if data is not None:
        return io.BytesIO(data)
    return _workspace_path(workspace, relative).open("rb")


def _write_zip_archive(
    workspace: Workspace,
    output: Path,
    overrides: Mapping[PurePosixPath, bytes],
    deleted: set[PurePosixPath],
) -> None:
    with zipfile.ZipFile(output, "w") as archive:
        for relative in workspace.directories:
            info = zipfile.ZipInfo(
                relative.as_posix() + "/",
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.create_system = 3
            info.external_attr = (0o40755 << 16) | 0x10
            archive.writestr(info, b"")
        for relative in _visible_files(workspace, deleted):
            info = zipfile.ZipInfo(
                relative.as_posix(),
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            # Per-entry setting: entries opened from a ZipInfo do not inherit
            # the constructor's compression, so this is the only place the
            # compression method is chosen.
            info.compress_type = zipfile.ZIP_DEFLATED
            with archive.open(info, "w") as destination:
                with _file_object(workspace, relative, overrides) as source_file:
                    shutil.copyfileobj(source_file, destination)


def _write_tar_archive(
    workspace: Workspace,
    output: Path,
    archive_kind: ArchiveKind,
    overrides: Mapping[PurePosixPath, bytes],
    deleted: set[PurePosixPath],
) -> None:
    with tarfile.open(
        output,
        _TAR_MODES[archive_kind],
        format=tarfile.PAX_FORMAT,
    ) as archive:
        for relative in workspace.directories:
            info = tarfile.TarInfo(relative.as_posix() + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info)
        for relative in _visible_files(workspace, deleted):
            source = _file_object(workspace, relative, overrides)
            with source:
                data = overrides.get(relative)
                if data is not None:
                    size = len(data)
                else:
                    size = _workspace_path(workspace, relative).stat().st_size
                info = tarfile.TarInfo(relative.as_posix())
                info.size = size
                info.mode = 0o644
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                archive.addfile(info, source)


def write_archive_output(
    spec: InputSpec,
    workspace: Workspace,
    output_path: Path,
    overrides: Mapping[PurePosixPath, bytes],
    deleted: Collection[PurePosixPath],
) -> None:
    if spec.kind != InputKind.ARCHIVE or spec.archive_kind is None:
        raise OutputError("archive output requires archive input")
    output_path = Path(os.path.abspath(os.fspath(output_path)))
    if os.path.lexists(output_path):
        raise OutputError(f"output path already exists: {output_path}")

    parent = output_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProcessingError(
            f"cannot create output parent directory {parent}: {exc}"
        ) from exc
    if not parent.is_dir():
        raise ProcessingError(f"output parent is not a directory: {parent}")

    deleted_set = set(deleted)
    temporary: Path | None = None
    committed = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".papercleaner-", dir=str(parent)
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        if spec.archive_kind == ArchiveKind.ZIP:
            _write_zip_archive(
                workspace, temporary, overrides, deleted_set
            )
        else:
            _write_tar_archive(
                workspace,
                temporary,
                spec.archive_kind,
                overrides,
                deleted_set,
            )
        if os.path.lexists(output_path):
            raise OutputError(f"output path already exists: {output_path}")
        try:
            os.replace(temporary, output_path)
        except OSError as exc:
            raise ProcessingError(
                f"cannot commit output archive {output_path}: {exc}"
            ) from exc
        committed = True
    except OSError as exc:
        raise ProcessingError(f"cannot write output archive: {exc}") from exc
    finally:
        if not committed and temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def write_output(
    spec: InputSpec,
    workspace: Workspace,
    output_path: Path,
    overrides: Mapping[PurePosixPath, bytes],
    deleted: Collection[PurePosixPath],
) -> None:
    if spec.kind == InputKind.DIRECTORY:
        write_directory_output(workspace, output_path, overrides, deleted)
    else:
        write_archive_output(
            spec, workspace, output_path, overrides, deleted
        )
